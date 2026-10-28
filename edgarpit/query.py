"""Point-in-time queries over the built DuckDB database (decisions A-12, A-16).

Every function decides what was known from ``acceptance_ts`` alone: a value is
known at ``asof_ts`` if and only if its filing's ``acceptance_ts <= asof_ts``.
The one exception is USD conversion (decision A-32): a converted row is known at
its ``asof_ts`` column, ``max(acceptance_ts, fx_known_at)``, because the value
in USD also depends on the FX rate. Connections opened here are read-only and
always run ``SET TimeZone='UTC'`` (decision A-5); a connection passed in is used
as given and left open.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, cast

import duckdb

from edgarpit.config import Settings

_PERIOD_TYPES = ("FY", "Q")

_ASOF_COLUMNS = (
    "cik, ticker, field, value, unit, currency, period_start, period_end, "
    "period_type, accession, form, acceptance_ts, version, is_restatement, ts_imputed, "
    "restatement_kind"
)

# ``main.fundamentals_pit_usd`` (decision A-32) has the same columns as
# ``fundamentals_pit`` plus the conversion columns; ``value`` is the converted
# ``value_usd``. ``unit`` and ``currency`` are mapped to USD in Python.
_USD_ASOF_COLUMNS = (
    "cik, ticker, field, value_usd AS value, unit, currency, period_start, period_end, "
    "period_type, accession, form, acceptance_ts, version, is_restatement, ts_imputed, "
    "restatement_kind"
)

_EVENT_COLUMNS = "cik, ticker, accession, form, acceptance_ts, ts_imputed, item_code, item_label"

_TEXT_COLUMNS = (
    "cik, ticker, accession, acceptance_ts, ts_imputed, document, doc_type, section, "
    "section_order, text, sha256, char_count"
)

_USD_TABLE = "main.fundamentals_pit_usd"
_TEXT_TABLE = "main.filings_text"

_ISSUER_UNION = (
    "SELECT cik, ticker FROM main.fundamentals_pit UNION SELECT cik, ticker FROM main.events_8k"
)


class MissingTableError(RuntimeError):
    """The database lacks a table the requested mode needs.

    Raised instead of a raw DuckDB catalog error so the caller (and the REST
    service) can say which model is missing and why: a 1.0.0 release predates
    the Phase 2 read surface (decisions A-32, A-38).
    """


@dataclass(frozen=True)
class AsOfResult:
    """One ``fundamentals_pit`` row as it was known at a point in time.

    ``value`` is in ``unit`` (an ISO currency code for monetary units, or
    ``shares``/``pure``), ``acceptance_ts`` is timezone-aware UTC, ``period_end``
    is the end of the fiscal period and ``version`` counts the filing versions
    of that period ordered by ``(acceptance_ts, accession)``.
    ``restatement_kind`` is ``none``, ``split_adjustment`` (a share-based
    value re-reported for a stock split) or ``revision`` (every other
    restatement; decision A-25).
    """

    cik: str
    ticker: str | None
    field: str
    value: float
    unit: str
    currency: str | None
    period_start: date | None
    period_end: date
    period_type: str
    accession: str
    form: str
    acceptance_ts: datetime
    version: int
    is_restatement: bool
    ts_imputed: bool
    restatement_kind: str


@dataclass(frozen=True)
class EventResult:
    """One 8-K item with the timestamp of the filing that carried it.

    ``acceptance_ts`` is timezone-aware UTC; ``item_code`` is the EDGAR item
    code (e.g. ``2.02``) and ``item_label`` its label from ``config/8k_items.yaml``
    (``unknown`` when unmapped).
    """

    cik: str
    ticker: str | None
    accession: str
    form: str
    acceptance_ts: datetime
    ts_imputed: bool
    item_code: str
    item_label: str


@dataclass(frozen=True)
class TextResult:
    """One section of one 8-K document as stored in ``main.filings_text``.

    ``acceptance_ts`` is the timezone-aware UTC acceptance time of the filing
    that carried the text, and the only thing that decides when the text was
    known; ``ts_imputed`` says that time was imputed (decision A-23).
    ``document`` is the EDGAR file name, ``doc_type`` is ``primary`` (the
    8-K/8-K/A document) or ``EX-99.1`` (an exhibit), ``section`` is an EDGAR
    item code (``2.02``) or ``unsegmented`` when the document was not split
    confidently (decision A-40), and ``section_order`` is the 1-based position
    of the section in the document. ``text`` is the cleaned plain text,
    ``sha256`` its lowercase hex SHA-256 and ``char_count`` its length in
    characters.
    """

    cik: str
    ticker: str | None
    accession: str
    acceptance_ts: datetime
    ts_imputed: bool
    document: str
    doc_type: str
    section: str
    section_order: int
    text: str
    sha256: str
    char_count: int


def _parse_timestamp(value: datetime | str, name: str) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    Accepts an aware :class:`datetime.datetime` or an ISO-8601 string carrying
    ``Z`` or an explicit offset. Raises :class:`ValueError` for malformed strings
    and for naive datetimes (or strings without a timezone).
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{name} must be an ISO-8601 timestamp with a timezone "
                f"(e.g. '2024-01-15T00:00:00Z' or '+05:30'); got {value!r}"
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"{name} must be timezone-aware (use 'Z' or an explicit offset); got {value!r}"
        )
    return parsed.astimezone(UTC)


def _validate_period(period: str, name: str) -> None:
    if period not in _PERIOD_TYPES:
        allowed = " or ".join(repr(item) for item in _PERIOD_TYPES)
        raise ValueError(f"{name} must be {allowed}; got {period!r}")


@contextmanager
def _connect(
    db: str | Path | duckdb.DuckDBPyConnection | None,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield a connection with ``TimeZone='UTC'``.

    ``None`` uses :attr:`edgarpit.config.Settings.edgarpit_db`; a path is opened
    read-only; a passed connection is used as given and left open.
    """
    if isinstance(db, duckdb.DuckDBPyConnection):
        con = db
        owned = False
    else:
        path = Settings().edgarpit_db if db is None else Path(db)
        con = duckdb.connect(str(path), read_only=True)
        owned = True
    try:
        con.execute("SET TimeZone='UTC'")
        yield con
    finally:
        if owned:
            con.close()


def _require_table(con: duckdb.DuckDBPyConnection, name: str, hint: str) -> None:
    """Raise :class:`MissingTableError` unless ``con`` has the table ``name``.

    ``name`` is schema-qualified (``main.fundamentals_pit_usd``); ``hint`` says
    what the table serves so the error can name the mode and the fix.
    """
    schema, _, table = name.partition(".")
    found = con.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchone()
    if found is None:
        raise MissingTableError(f"{name} is missing from the database; {hint}")


def _resolve_cik(con: duckdb.DuckDBPyConnection, identifier: str) -> str:
    """Return the 10-character CIK for ``identifier``.

    All-digit strings of up to 10 characters are CIKs (zero-padded); anything
    else is a ticker, matched case-insensitively against the issuers present in
    ``main.fundamentals_pit`` and ``main.events_8k``. Raises :class:`LookupError`
    when no issuer matches or a ticker matches several CIKs.
    """
    text = identifier.strip()
    if text.isdigit() and len(text) <= 10:
        cik = text.zfill(10)
        row = con.execute(
            f"SELECT cik FROM ({_ISSUER_UNION}) WHERE cik = ? LIMIT 1", [cik]
        ).fetchone()
        if row is None:
            raise LookupError(f"no issuer with CIK {cik}")
        return cik
    matches = [
        cast(str, row[0])
        for row in con.execute(
            f"SELECT DISTINCT cik FROM ({_ISSUER_UNION}) "
            "WHERE lower(ticker) = lower(?) ORDER BY cik",
            [text],
        ).fetchall()
    ]
    if not matches:
        raise LookupError(f"no issuer with ticker {text.upper()!r}")
    if len(matches) > 1:
        raise LookupError(
            f"ticker {text.upper()!r} matches several CIKs: {', '.join(sorted(matches))}"
        )
    return matches[0]


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _asof_result(row: tuple[Any, ...]) -> AsOfResult:
    return AsOfResult(
        cik=row[0],
        ticker=row[1],
        field=row[2],
        value=row[3],
        unit=row[4],
        currency=row[5],
        period_start=row[6],
        period_end=row[7],
        period_type=row[8],
        accession=row[9],
        form=row[10],
        acceptance_ts=_utc(row[11]),
        version=row[12],
        is_restatement=row[13],
        ts_imputed=row[14],
        restatement_kind=row[15],
    )


def _event_result(row: tuple[Any, ...]) -> EventResult:
    return EventResult(
        cik=row[0],
        ticker=row[1],
        accession=row[2],
        form=row[3],
        acceptance_ts=_utc(row[4]),
        ts_imputed=row[5],
        item_code=row[6],
        item_label=row[7],
    )


def _usd_unit(unit: str, currency: str | None) -> str:
    """Return ``unit`` with its reported currency replaced by ``USD``.

    Conversion changes the currency and nothing else, so ``CAD`` becomes
    ``USD`` and ``CAD/shares`` becomes ``USD/shares``; a unit that does not
    carry the reported currency (``shares``, ``pure``) is returned unchanged.
    """
    if currency is None:
        return unit
    if unit == currency:
        return "USD"
    prefix = f"{currency}/"
    if unit.startswith(prefix):
        return f"USD/{unit[len(prefix) :]}"
    return unit


def _usd_result(row: tuple[Any, ...]) -> AsOfResult:
    """Build the USD view of a ``fundamentals_pit_usd`` row (decision A-32)."""
    result = _asof_result(row)
    return replace(result, unit=_usd_unit(result.unit, result.currency), currency="USD")


def _text_result(row: tuple[Any, ...]) -> TextResult:
    return TextResult(
        cik=row[0],
        ticker=row[1],
        accession=row[2],
        acceptance_ts=_utc(row[3]),
        ts_imputed=row[4],
        document=row[5],
        doc_type=row[6],
        section=row[7],
        section_order=row[8],
        text=row[9],
        sha256=row[10],
        char_count=row[11],
    )


def asof(
    identifier: str,
    field: str,
    asof_ts: datetime | str,
    period: Literal["FY", "Q"] = "FY",
    n: int = 1,
    *,
    db: str | Path | duckdb.DuckDBPyConnection | None = None,
    currency: Literal["USD"] | None = None,
) -> list[AsOfResult]:
    """Return the ``n`` most recent periods known at ``asof_ts``.

    Candidates are ``main.fundamentals_pit`` rows with ``acceptance_ts <= asof_ts``
    and the requested ``period_type``; per period the row with the maximum
    ``(acceptance_ts, version)`` is kept; periods are ordered by ``period_end``
    descending (``period_start`` descending, nulls last, then ``accession`` break
    ties) and the first ``n`` are returned. ``identifier`` is a ticker or a CIK
    (digits, zero-padded), matched case-insensitively for tickers; values are in
    the row's ``unit`` and timestamps are timezone-aware UTC. ``asof_ts`` accepts
    an aware datetime or an ISO-8601 string with ``Z`` or an offset. Raises
    :class:`LookupError` for an unknown or ambiguous identifier and
    :class:`ValueError` for ``n < 1``, an unsupported ``period`` or
    ``currency``, malformed timestamps or naive timestamps (no timezone). Opens a
    read-only connection unless a connection is passed.

    ``currency="USD"`` reads ``main.fundamentals_pit_usd`` instead (decision
    A-32): only converted rows are candidates and the point-in-time rule applies
    to the row's ``asof_ts`` (``max(acceptance_ts, fx_known_at)``) rather than
    ``acceptance_ts``, so a CAD value is not returned before the FX rate that
    produced its USD amount was published. ``value`` is ``value_usd``,
    ``currency`` is ``"USD"`` and ``unit`` carries the reported unit with its
    currency replaced (``CAD`` → ``USD``, ``CAD/shares`` → ``USD/shares``);
    ``acceptance_ts`` stays the filing's own acceptance time. Raises
    :class:`MissingTableError` when the database has no
    ``main.fundamentals_pit_usd`` (a 1.0.0 release predates FX conversion).
    """
    if currency is not None and currency != "USD":
        raise ValueError(f"currency must be 'USD' or None; got {currency!r}")
    _validate_period(period, "period")
    if n < 1:
        raise ValueError(f"n must be at least 1; got {n}")
    asof_utc = _parse_timestamp(asof_ts, "asof_ts")
    with _connect(db) as con:
        if currency == "USD":
            _require_table(
                con,
                _USD_TABLE,
                "currency='USD' reads the Phase 2 FX model of decision A-32, which a "
                "1.0.0 release does not have; rebuild the database with `edgarpit build` "
                "or call asof without currency",
            )
            cik = _resolve_cik(con, identifier)
            rows = con.execute(
                f"""
                SELECT {_USD_ASOF_COLUMNS}
                FROM main.fundamentals_pit_usd
                WHERE cik = ? AND field = ? AND period_type = ? AND converted AND asof_ts <= ?
                QUALIFY row_number() OVER (
                    PARTITION BY period_start, period_end
                    ORDER BY asof_ts DESC, version DESC, accession ASC
                ) = 1
                ORDER BY period_end DESC, period_start DESC NULLS LAST, accession ASC
                LIMIT ?
                """,
                [cik, field, period, asof_utc, n],
            ).fetchall()
            return [_usd_result(row) for row in rows]
        cik = _resolve_cik(con, identifier)
        rows = con.execute(
            f"""
            SELECT {_ASOF_COLUMNS}
            FROM main.fundamentals_pit
            WHERE cik = ? AND field = ? AND period_type = ? AND acceptance_ts <= ?
            QUALIFY row_number() OVER (
                PARTITION BY period_start, period_end
                ORDER BY acceptance_ts DESC, version DESC, accession ASC
            ) = 1
            ORDER BY period_end DESC, period_start DESC NULLS LAST, accession ASC
            LIMIT ?
            """,
            [cik, field, period, asof_utc, n],
        ).fetchall()
    return [_asof_result(row) for row in rows]


def history(
    identifier: str,
    field: str,
    period: Literal["FY", "Q"] | None = None,
    *,
    db: str | Path | duckdb.DuckDBPyConnection | None = None,
    currency: Literal["USD"] | None = None,
) -> list[AsOfResult]:
    """Return every version of ``field`` for ``identifier``.

    Versions are ordered by ``period_end``, then ``period_start`` (nulls first),
    then ``version`` and ``accession``; ``period`` filters on ``period_type`` when
    given. ``identifier`` is a ticker or a CIK (digits, zero-padded), matched
    case-insensitively for tickers; values are in the row's ``unit`` and
    timestamps are timezone-aware UTC. Raises :class:`LookupError` for an unknown
    or ambiguous identifier and :class:`ValueError` for an unsupported ``period``
    or ``currency``. Opens a read-only connection unless a connection is passed.

    ``currency="USD"`` reads ``main.fundamentals_pit_usd`` instead (decision
    A-32) and returns only converted rows, in the same order; ``value`` is
    ``value_usd``, ``currency`` is ``"USD"`` and ``unit`` carries the reported
    unit with its currency replaced (``CAD`` → ``USD``, ``CAD/shares`` →
    ``USD/shares``). Rows without an FX rate are absent rather than returned
    with a null value. Raises :class:`MissingTableError` when the database has
    no ``main.fundamentals_pit_usd`` (a 1.0.0 release predates FX conversion).
    """
    if currency is not None and currency != "USD":
        raise ValueError(f"currency must be 'USD' or None; got {currency!r}")
    if period is not None:
        _validate_period(period, "period")
    with _connect(db) as con:
        if currency == "USD":
            _require_table(
                con,
                _USD_TABLE,
                "currency='USD' reads the Phase 2 FX model of decision A-32, which a "
                "1.0.0 release does not have; rebuild the database with `edgarpit build` "
                "or call history without currency",
            )
            cik = _resolve_cik(con, identifier)
            sql = (
                f"SELECT {_USD_ASOF_COLUMNS} FROM main.fundamentals_pit_usd "
                "WHERE cik = ? AND field = ? AND converted"
            )
            params: list[Any] = [cik, field]
            if period is not None:
                sql += " AND period_type = ?"
                params.append(period)
            sql += (
                " ORDER BY period_end ASC, period_start ASC NULLS FIRST, version ASC, accession ASC"
            )
            rows = con.execute(sql, params).fetchall()
            return [_usd_result(row) for row in rows]
        cik = _resolve_cik(con, identifier)
        sql = f"SELECT {_ASOF_COLUMNS} FROM main.fundamentals_pit WHERE cik = ? AND field = ?"
        params = [cik, field]
        if period is not None:
            sql += " AND period_type = ?"
            params.append(period)
        sql += " ORDER BY period_end ASC, period_start ASC NULLS FIRST, version ASC, accession ASC"
        rows = con.execute(sql, params).fetchall()
    return [_asof_result(row) for row in rows]


def events(
    identifier: str,
    item: str | None = None,
    start: datetime | str | None = None,
    end: datetime | str | None = None,
    *,
    db: str | Path | duckdb.DuckDBPyConnection | None = None,
) -> list[EventResult]:
    """Return 8-K items for ``identifier`` ordered by ``acceptance_ts``.

    ``item`` filters on the item code (``"2.02"``); ``start`` and ``end`` bound
    ``acceptance_ts`` inclusively and, like the other timestamps, accept aware
    datetimes or ISO-8601 strings with ``Z`` or an offset (naive inputs raise
    :class:`ValueError`). Rows are ordered by ``acceptance_ts``, then
    ``accession`` and ``item_code``; timestamps are timezone-aware UTC. Raises
    :class:`LookupError` for an unknown or ambiguous identifier. Opens a
    read-only connection unless a connection is passed.
    """
    start_utc = _parse_timestamp(start, "start") if start is not None else None
    end_utc = _parse_timestamp(end, "end") if end is not None else None
    with _connect(db) as con:
        cik = _resolve_cik(con, identifier)
        sql = f"SELECT {_EVENT_COLUMNS} FROM main.events_8k WHERE cik = ?"
        params: list[Any] = [cik]
        if item is not None:
            sql += " AND item_code = ?"
            params.append(item)
        if start_utc is not None:
            sql += " AND acceptance_ts >= ?"
            params.append(start_utc)
        if end_utc is not None:
            sql += " AND acceptance_ts <= ?"
            params.append(end_utc)
        sql += " ORDER BY acceptance_ts ASC, accession ASC, item_code ASC"
        rows = con.execute(sql, params).fetchall()
    return [_event_result(row) for row in rows]


def filing_text(
    identifier: str,
    *,
    item: str | None = None,
    start: datetime | str | None = None,
    end: datetime | str | None = None,
    db: str | Path | duckdb.DuckDBPyConnection | None = None,
) -> list[TextResult]:
    """Return the stored 8-K text of ``identifier`` ordered by acceptance time.

    Rows come from ``main.filings_text`` (decision A-31): every section of the
    primary 8-K/8-K/A document plus one ``unsegmented`` row per ``EX-99.1``
    exhibit. ``identifier`` is a ticker or a CIK (digits, zero-padded), matched
    case-insensitively for tickers against the issuers of
    ``main.fundamentals_pit`` and ``main.events_8k``. ``item`` keeps only the
    filings whose ``main.events_8k`` rows contain that item code (``"2.02"``)
    and returns all sections and exhibits of those filings (decision A-40), so
    Item 2.02 text is found by joining the text to the event on the accession.
    ``start`` and ``end`` bound ``acceptance_ts`` inclusively and, like the
    other timestamps, accept aware datetimes or ISO-8601 strings with ``Z`` or
    an offset (naive inputs raise :class:`ValueError`). Rows are ordered by
    ``acceptance_ts``, then ``accession``, ``document`` and ``section_order``;
    timestamps are timezone-aware UTC.

    Raises :class:`LookupError` for an unknown or ambiguous identifier,
    :class:`ValueError` for malformed or naive timestamps and
    :class:`MissingTableError` when the database has no ``main.filings_text``
    (a 1.0.0 release predates 8-K text). Opens a read-only connection unless a
    connection is passed.
    """
    start_utc = _parse_timestamp(start, "start") if start is not None else None
    end_utc = _parse_timestamp(end, "end") if end is not None else None
    with _connect(db) as con:
        _require_table(
            con,
            _TEXT_TABLE,
            "filing_text reads the Phase 2 8-K text model of decision A-31, which a "
            "1.0.0 release does not have; rebuild the database with `edgarpit build`",
        )
        cik = _resolve_cik(con, identifier)
        sql = f"SELECT {_TEXT_COLUMNS} FROM main.filings_text WHERE cik = ?"
        params: list[Any] = [cik]
        if item is not None:
            sql += (
                " AND accession IN ("
                "SELECT accession FROM main.events_8k WHERE cik = ? AND item_code = ?"
                ")"
            )
            params.extend([cik, item])
        if start_utc is not None:
            sql += " AND acceptance_ts >= ?"
            params.append(start_utc)
        if end_utc is not None:
            sql += " AND acceptance_ts <= ?"
            params.append(end_utc)
        sql += " ORDER BY acceptance_ts ASC, accession ASC, document ASC, section_order ASC"
        rows = con.execute(sql, params).fetchall()
    return [_text_result(row) for row in rows]


def issuers(
    *,
    db: str | Path | duckdb.DuckDBPyConnection | None = None,
) -> list[tuple[str, str | None]]:
    """Return every issuer in the database as ``(cik, ticker)`` pairs.

    The pairs come from the distinct issuers of ``main.fundamentals_pit`` and
    ``main.events_8k``, ordered by ticker (nulls last) and CIK; ``ticker`` is
    ``None`` when the tables do not carry one. Opens a read-only connection
    unless a connection is passed.
    """
    with _connect(db) as con:
        rows = con.execute(
            f"SELECT cik, ticker FROM ({_ISSUER_UNION}) ORDER BY ticker NULLS LAST, cik"
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def fields(
    identifier: str,
    *,
    db: str | Path | duckdb.DuckDBPyConnection | None = None,
) -> list[str]:
    """Return the canonical fields available for ``identifier``, sorted.

    ``identifier`` is a ticker or a CIK (digits, zero-padded), matched
    case-insensitively for tickers; only fields present in
    ``main.fundamentals_pit`` are returned. Raises :class:`LookupError` for an
    unknown or ambiguous identifier. Opens a read-only connection unless a
    connection is passed.
    """
    with _connect(db) as con:
        cik = _resolve_cik(con, identifier)
        rows = con.execute(
            "SELECT DISTINCT field FROM main.fundamentals_pit WHERE cik = ? ORDER BY field",
            [cik],
        ).fetchall()
    return [row[0] for row in rows]
