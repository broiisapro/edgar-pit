"""In-memory builders for the Phase 2 tables.

The three tables follow the frozen contracts of ``docs/architecture.md``:
``raw.filings_text`` and ``raw.fx_daily`` (section 6) and
``main.fundamentals_pit_usd`` (section 7), so query and dbt work can be tested
without a fetch, a fixture tree or a dbt run. Callers pass :class:`FilingsTextRow`,
:class:`FxDailyRow` and :class:`FundamentalUsdRow` values to the frame builders,
which validate against the Pandera schemas where one exists, or to
:func:`build_phase2_db`, which creates the tables in a DuckDB database.

Every timestamp is timezone-aware UTC and every date is a ``datetime.date``.
The builders derive the values that must agree with another column
(``sha256``/``char_count`` of a section's ``text``, ``value_usd``/``asof_ts`` of
a converted fundamental) so a test can never build a frame the contract forbids.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import polars as pl

from edgarpit.schemas import FilingsTextSchema, FxDailySchema, duckdb_ddl

TORONTO = ZoneInfo("America/Toronto")

# ``main.fundamentals_pit_usd``: every ``fundamentals_pit`` column, then the
# conversion columns of decision A-32 (docs/architecture.md section 7).
FUNDAMENTALS_PIT_USD_DDL = """
CREATE TABLE main.fundamentals_pit_usd (
    cik VARCHAR(10),
    ticker VARCHAR,
    field VARCHAR,
    period_start DATE,
    period_end DATE,
    period_type VARCHAR,
    accession VARCHAR,
    form VARCHAR,
    acceptance_ts TIMESTAMPTZ,
    ts_imputed BOOLEAN,
    value DOUBLE,
    unit VARCHAR,
    currency VARCHAR,
    source_taxonomy VARCHAR,
    source_concept VARCHAR,
    concept_rank INTEGER,
    version INTEGER,
    first_value DOUBLE,
    is_restatement BOOLEAN,
    pct_change_vs_first DOUBLE,
    restatement_kind VARCHAR,
    value_usd DOUBLE,
    fx_rate DOUBLE,
    fx_rate_date DATE,
    fx_known_at TIMESTAMPTZ,
    converted BOOLEAN,
    asof_ts TIMESTAMPTZ
)
"""

FUNDAMENTALS_PIT_USD_COLUMNS: list[str] = [
    "cik",
    "ticker",
    "field",
    "period_start",
    "period_end",
    "period_type",
    "accession",
    "form",
    "acceptance_ts",
    "ts_imputed",
    "value",
    "unit",
    "currency",
    "source_taxonomy",
    "source_concept",
    "concept_rank",
    "version",
    "first_value",
    "is_restatement",
    "pct_change_vs_first",
    "restatement_kind",
    "value_usd",
    "fx_rate",
    "fx_rate_date",
    "fx_known_at",
    "converted",
    "asof_ts",
]

_US_DOLLAR = "USD"
_CAD_PER_USD = "CAD"


def sha256_of(text: str) -> str:
    """Return the lowercase hex SHA-256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fx_known_at(rate_date: date) -> datetime:
    """Return the instant a Valet rate for ``rate_date`` is known (decision A-32).

    The Bank of Canada publishes the daily rate in the afternoon; a rate is
    treated as known at 17:00 ``America/Toronto`` on its observation date,
    returned as a timezone-aware UTC datetime.
    """
    return datetime.combine(rate_date, time(17, 0), tzinfo=TORONTO).astimezone(UTC)


@dataclass(frozen=True)
class FilingsTextRow:
    """One ``raw.filings_text`` row; ``sha256`` and ``char_count`` follow ``text``.

    ``document`` is the EDGAR file name, ``doc_type`` is ``primary`` or
    ``EX-99.1``, ``section`` an EDGAR item code (``2.02``) or ``unsegmented`` and
    ``section_order`` the 1-based position in the document. ``acceptance_ts`` is
    the filing's acceptance time (UTC) and ``ts_imputed`` its imputed flag,
    copied from ``raw.filings``.
    """

    accession: str
    document: str
    section: str
    text: str
    acceptance_ts: datetime = datetime(2025, 10, 30, 20, 5, tzinfo=UTC)
    cik: str = "0000320193"
    doc_type: str = "primary"
    section_order: int = 1
    ts_imputed: bool = False


@dataclass(frozen=True)
class FxDailyRow:
    """One ``raw.fx_daily`` row; ``known_at`` follows ``rate_date`` (A-32)."""

    rate_date: date
    value: float
    series: str = "FXUSDCAD"
    known_at: datetime | None = None
    source: str = "valet"


@dataclass(frozen=True)
class FundamentalUsdRow:
    """One ``main.fundamentals_pit_usd`` row.

    The first 21 fields mirror ``main.fundamentals_pit``; ``value`` is in
    ``unit``/``currency`` and ``acceptance_ts`` is the filing's acceptance time
    (UTC). The conversion columns follow decision A-32: ``fx_rate`` is the
    ``FXUSDCAD`` observation used (Canadian dollars per US dollar),
    ``fx_rate_date`` its observation date and ``fx_known_at`` the instant it is
    known. :func:`fundamentals_pit_usd_frame` fills ``value_usd``, ``converted``
    and ``asof_ts`` from those: a CAD value is divided by ``fx_rate``, a USD
    value is used as is with rate ``1.0`` and no observation, and a row with no
    usable rate keeps ``value_usd`` null with ``converted`` false.
    """

    cik: str
    field: str
    value: float
    period_end: date
    accession: str
    acceptance_ts: datetime
    period_start: date | None = None
    period_type: str = "FY"
    form: str = "40-F"
    ts_imputed: bool = False
    unit: str = _CAD_PER_USD
    currency: str | None = _CAD_PER_USD
    source_taxonomy: str = "ifrs-full"
    source_concept: str = "Revenue"
    concept_rank: int = 1
    ticker: str | None = None
    version: int = 1
    first_value: float | None = None
    is_restatement: bool = False
    pct_change_vs_first: float | None = None
    restatement_kind: str = "none"
    fx_rate: float | None = None
    fx_rate_date: date | None = None
    fx_known_at: datetime | None = None


def _filings_text_values(row: FilingsTextRow) -> tuple[object, ...]:
    return (
        row.cik,
        row.accession,
        row.acceptance_ts,
        row.ts_imputed,
        row.document,
        row.doc_type,
        row.section,
        row.section_order,
        row.text,
        sha256_of(row.text),
        len(row.text),
    )


def _filings_text_schema() -> dict[str, Any]:
    return {
        "cik": pl.String,
        "accession": pl.String,
        "acceptance_ts": pl.Datetime("us", "UTC"),
        "ts_imputed": pl.Boolean,
        "document": pl.String,
        "doc_type": pl.String,
        "section": pl.String,
        "section_order": pl.Int64,
        "text": pl.String,
        "sha256": pl.String,
        "char_count": pl.Int64,
    }


def _fx_daily_schema() -> dict[str, Any]:
    return {
        "series": pl.String,
        "rate_date": pl.Date,
        "value": pl.Float64,
        "known_at": pl.Datetime("us", "UTC"),
        "source": pl.String,
    }


def _fundamentals_usd_schema() -> dict[str, Any]:
    return {
        "cik": pl.String,
        "ticker": pl.String,
        "field": pl.String,
        "period_start": pl.Date,
        "period_end": pl.Date,
        "period_type": pl.String,
        "accession": pl.String,
        "form": pl.String,
        "acceptance_ts": pl.Datetime("us", "UTC"),
        "ts_imputed": pl.Boolean,
        "value": pl.Float64,
        "unit": pl.String,
        "currency": pl.String,
        "source_taxonomy": pl.String,
        "source_concept": pl.String,
        "concept_rank": pl.Int64,
        "version": pl.Int64,
        "first_value": pl.Float64,
        "is_restatement": pl.Boolean,
        "pct_change_vs_first": pl.Float64,
        "restatement_kind": pl.String,
        "value_usd": pl.Float64,
        "fx_rate": pl.Float64,
        "fx_rate_date": pl.Date,
        "fx_known_at": pl.Datetime("us", "UTC"),
        "converted": pl.Boolean,
        "asof_ts": pl.Datetime("us", "UTC"),
    }


def default_filings_text() -> list[FilingsTextRow]:
    """Two sections of one 8-K and one unsegmented exhibit, in a plausible shape."""
    return [
        FilingsTextRow(
            accession="0000320193-25-000080",
            document="aapl-20251030.htm",
            section="2.02",
            text="Item 2.02 Results of Operations and Financial Condition\n\n"
            "The Company reported quarterly revenue of $102.5 billion.",
            section_order=1,
        ),
        FilingsTextRow(
            accession="0000320193-25-000080",
            document="aapl-20251030.htm",
            section="9.01",
            text="Item 9.01 Financial Statements and Exhibits\n\n(d) Exhibits\n99.1 Press release.",
            section_order=2,
        ),
        FilingsTextRow(
            accession="0000320193-25-000080",
            document="a8kexhibit991.htm",
            section="unsegmented",
            text="Apple reports fourth quarter results.",
            doc_type="EX-99.1",
        ),
    ]


def filings_text_frame(rows: Iterable[FilingsTextRow] | None = None) -> pl.DataFrame:
    """Return a ``raw.filings_text`` frame valid under :class:`FilingsTextSchema`.

    ``rows`` defaults to :func:`default_filings_text`. ``sha256`` and
    ``char_count`` are derived from each row's ``text``; the frame is validated
    before it is returned, so a row that breaks the contract raises
    :class:`pandera.errors.SchemaError`.
    """
    selected = default_filings_text() if rows is None else list(rows)
    frame = pl.DataFrame(
        [_filings_text_values(row) for row in selected],
        schema=_filings_text_schema(),
        orient="row",
    )
    FilingsTextSchema.validate(frame)
    return frame


def default_fx_daily() -> list[FxDailyRow]:
    """Three consecutive business days of ``FXUSDCAD``."""
    return [
        FxDailyRow(rate_date=date(2023, 10, 27), value=1.3875),
        FxDailyRow(rate_date=date(2023, 10, 30), value=1.3831),
        FxDailyRow(rate_date=date(2023, 10, 31), value=1.3659),
    ]


def fx_daily_frame(rows: Iterable[FxDailyRow] | None = None) -> pl.DataFrame:
    """Return a ``raw.fx_daily`` frame valid under :class:`FxDailySchema`.

    ``rows`` defaults to :func:`default_fx_daily`. ``known_at`` is derived from
    ``rate_date`` (17:00 ``America/Toronto`` in UTC) unless the row sets it; the
    frame is validated before it is returned.
    """
    selected = default_fx_daily() if rows is None else list(rows)
    frame = pl.DataFrame(
        [
            (
                row.series,
                row.rate_date,
                row.value,
                row.known_at if row.known_at is not None else fx_known_at(row.rate_date),
                row.source,
            )
            for row in selected
        ],
        schema=_fx_daily_schema(),
        orient="row",
    )
    FxDailySchema.validate(frame)
    return frame


def _converted(row: FundamentalUsdRow) -> tuple[float | None, bool, datetime]:
    """Return ``(value_usd, converted, asof_ts)`` for a row (decision A-32).

    A USD row converts at 1.0 with no observation and is known at its
    ``acceptance_ts``. A row with a positive ``fx_rate`` converts its CAD value
    to USD and is known at the later of ``acceptance_ts`` and ``fx_known_at``, so
    it can never be returned before both the filing and the rate exist. Any
    other row stays unconverted (``value_usd`` null) and is known at its
    ``acceptance_ts``.
    """
    if row.currency == _US_DOLLAR:
        return row.value, True, row.acceptance_ts
    if row.fx_rate is not None and row.fx_rate > 0 and row.fx_known_at is not None:
        return row.value / row.fx_rate, True, max(row.acceptance_ts, row.fx_known_at)
    return None, False, row.acceptance_ts


def default_fundamentals_usd() -> list[FundamentalUsdRow]:
    """One converted CAD revenue row and one unconverted row without a rate."""
    return [
        FundamentalUsdRow(
            cik="0001000275",
            field="revenue",
            value=51_464_000_000.0,
            period_start=date(2022, 11, 1),
            period_end=date(2023, 10, 31),
            accession="0001193125-24-273268",
            acceptance_ts=datetime(2024, 12, 4, 21, 0, tzinfo=UTC),
            fx_rate=1.3659,
            fx_rate_date=date(2023, 10, 31),
            fx_known_at=fx_known_at(date(2023, 10, 31)),
        ),
        FundamentalUsdRow(
            cik="0001000275",
            field="revenue",
            value=56_129_000_000.0,
            period_start=date(2022, 11, 1),
            period_end=date(2023, 10, 31),
            accession="0001193125-23-285639",
            acceptance_ts=datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
        ),
    ]


def fundamentals_pit_usd_frame(
    rows: Iterable[FundamentalUsdRow] | None = None,
) -> pl.DataFrame:
    """Return a ``main.fundamentals_pit_usd`` frame with the section 7 columns.

    ``rows`` defaults to :func:`default_fundamentals_usd`. ``value_usd``,
    ``converted`` and ``asof_ts`` are derived by :func:`_converted`; the other
    conversion columns are taken from the row. No Pandera schema is frozen for
    this dbt output model, so the frame is built with the documented dtypes and
    the caller checks the values.
    """
    selected = default_fundamentals_usd() if rows is None else list(rows)
    records: list[tuple[object, ...]] = []
    for row in selected:
        value_usd, converted, asof_ts = _converted(row)
        records.append(
            (
                row.cik,
                row.ticker,
                row.field,
                row.period_start,
                row.period_end,
                row.period_type,
                row.accession,
                row.form,
                row.acceptance_ts,
                row.ts_imputed,
                row.value,
                row.unit,
                row.currency,
                row.source_taxonomy,
                row.source_concept,
                row.concept_rank,
                row.version,
                row.first_value,
                row.is_restatement,
                row.pct_change_vs_first,
                row.restatement_kind,
                value_usd,
                row.fx_rate,
                row.fx_rate_date,
                row.fx_known_at,
                converted,
                asof_ts,
            )
        )
    return pl.DataFrame(records, schema=_fundamentals_usd_schema(), orient="row").select(
        FUNDAMENTALS_PIT_USD_COLUMNS
    )


def build_phase2_db(
    *,
    filings_text: pl.DataFrame | None = None,
    fx_daily: pl.DataFrame | None = None,
    fundamentals_usd: pl.DataFrame | None = None,
    path: str | Path | None = None,
) -> duckdb.DuckDBPyConnection:
    """Create the three Phase 2 tables, filled with the given frames.

    ``raw.filings_text`` and ``raw.fx_daily`` are created with
    :func:`edgarpit.schemas.duckdb_ddl`; ``main.fundamentals_pit_usd`` with
    :data:`FUNDAMENTALS_PIT_USD_DDL`. A ``None`` frame uses the default rows of
    the matching builder, so ``build_phase2_db()`` returns a usable database.
    ``path`` selects the database file; without it the database is in memory.
    The session timezone is UTC (decision A-5) and the returned connection is
    open: the caller closes it.
    """
    con = duckdb.connect(":memory:" if path is None else str(path))
    con.execute("SET TimeZone='UTC'")
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute("CREATE SCHEMA IF NOT EXISTS main")
    frames = {
        "filings_text": filings_text_frame() if filings_text is None else filings_text,
        "fx_daily": fx_daily_frame() if fx_daily is None else fx_daily,
    }
    for table, frame in frames.items():
        con.execute(duckdb_ddl(table))
        con.register("_doubles_frame", frame)
        try:
            con.execute(f"INSERT INTO raw.{table} SELECT * FROM _doubles_frame")
        finally:
            con.unregister("_doubles_frame")
    con.execute(FUNDAMENTALS_PIT_USD_DDL)
    usd = fundamentals_pit_usd_frame() if fundamentals_usd is None else fundamentals_usd
    con.register("_doubles_usd", usd)
    try:
        con.execute("INSERT INTO main.fundamentals_pit_usd SELECT * FROM _doubles_usd")
    finally:
        con.unregister("_doubles_usd")
    return con
