"""Filing SGML headers: the authoritative acceptance time (decisions A-1, A-16).

EDGAR writes each filing's acceptance time into its ``.hdr.sgml`` header as
``<ACCEPTANCE-DATETIME>YYYYMMDDHHMMSS`` in America/New_York. That wall time is
the only acceptance time edgar-pit trusts: the submissions API's
``acceptanceDateTime`` is kept as ``acceptance_ts_api`` for diagnostics only.
The conversion to UTC is DST-aware (decision A-22): an ambiguous wall time (the
repeated hour when DST ends, 01:00-02:00) resolves to the later UTC instant so
the result is never earlier than the truth, and a wall time in the skipped hour
when DST starts raises ``ValueError``.

EDGAR headers of filings from before about 2001 carry no acceptance time.
:func:`parse_header` reports exactly that case as
:class:`MissingAcceptanceTime`, a ``ValueError`` whose ``header`` still carries
the form, period, items and ``<DATE-OF-FILING-DATE-CHANGE>`` floor (decision
A-26); every other malformation stays a plain ``ValueError`` so a corrupt cache
fails loudly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import polars as pl

from edgarpit.schemas import FilingsSchema

NEW_YORK = ZoneInfo("America/New_York")
"""Timezone of EDGAR's SGML wall times (``America/New_York``)."""

_ACCEPTANCE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")
_DATE_RE = re.compile(r"\d{8}")
_ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")
_TAG_RE = re.compile(r"<([A-Z0-9_.-]+)>([^<]*)")
_EVENT_FORMS = frozenset({"8-K", "8-K/A"})


@dataclass(frozen=True)
class HeaderInfo:
    """The fields edgar-pit keeps from one ``.hdr.sgml`` filing header.

    ``acceptance_ts`` is timezone-aware UTC, ``filing_date`` is New York local,
    ``period`` is the reported period (``None`` for headers without a non-empty
    ``<PERIOD>``, e.g. registration statements) and ``items`` is the 8-K item
    codes joined with ``,`` (``None`` when the header carries none).
    """

    accession: str
    form: str
    acceptance_ts: datetime
    filing_date: date
    period: date | None
    items: str | None


@dataclass(frozen=True)
class HeaderWithoutAcceptance:
    """The fields a header still provides when it carries no acceptance time.

    Returned on :class:`MissingAcceptanceTime` for pre-2001 headers (decision
    A-26). ``filing_date`` is the header's ``<FILING-DATE>``, ``period`` its
    ``<PERIOD>``, ``items`` its ``<ITEMS>`` codes joined with ``,`` and
    ``date_of_filing_date_change`` the latest ``<DATE-OF-FILING-DATE-CHANGE>``
    when the header has one (``None`` otherwise). All dates are New York local
    calendar dates.
    """

    accession: str
    form: str
    filing_date: date
    period: date | None
    items: str | None
    date_of_filing_date_change: date | None


class MissingAcceptanceTime(ValueError):
    """A header has no ``<ACCEPTANCE-DATETIME>`` value (decision A-26).

    ``header`` carries the fields that are still usable. Ingest does not treat
    this as corruption: the filing takes the A-23 fallback acceptance time, and
    the header's items and ``<DATE-OF-FILING-DATE-CHANGE>`` floor are kept.
    """

    def __init__(self, header: HeaderWithoutAcceptance) -> None:
        super().__init__(f"SGML header {header.accession} has no <ACCEPTANCE-DATETIME>")
        self.header = header


def _header_tags(text: str) -> dict[str, list[str]]:
    """Collect the header's ``<TAG>value`` lines per tag, in file order."""
    tags: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = _TAG_RE.fullmatch(line.strip())
        if match is not None:
            tags.setdefault(match.group(1), []).append(match.group(2).strip())
    return tags


def _required(tags: Mapping[str, list[str]], tag: str) -> str:
    """Return the first non-empty value of a required tag or raise ``ValueError``."""
    for value in tags.get(tag, []):
        if value:
            return value
    raise ValueError(f"SGML header is missing <{tag}>")


def _parse_day(tag: str, value: str) -> date:
    """Parse a ``YYYYMMDD`` tag value as ``date`` or raise ``ValueError``."""
    if not _DATE_RE.fullmatch(value):
        raise ValueError(f"malformed <{tag}>: {value!r}")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"malformed <{tag}>: {value!r}") from exc


def _new_york_to_utc(local: datetime) -> datetime:
    """Convert a naive America/New_York wall time to an aware UTC datetime.

    A wall time that exists exactly once maps directly. A wall time in the
    repeated hour at the end of DST resolves to the later UTC instant (decision
    A-22). A wall time in the hour skipped at the start of DST raises
    ``ValueError`` because it never happened.
    """
    candidates: list[datetime] = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=NEW_YORK, fold=fold)
        round_trip = aware.astimezone(UTC).astimezone(NEW_YORK).replace(tzinfo=None)
        if round_trip == local:
            candidates.append(aware.astimezone(UTC))
    if not candidates:
        raise ValueError(
            f"{local:%Y-%m-%d %H:%M:%S} does not exist in America/New_York "
            "(skipped hour at the start of DST)"
        )
    return max(candidates)


def parse_header(text: str) -> HeaderInfo:
    """Parse an SGML filing header.

    ``text`` is the body of a ``.hdr.sgml`` file. Returns a :class:`HeaderInfo`
    whose ``acceptance_ts`` is the ``<ACCEPTANCE-DATETIME>`` wall time
    interpreted in ``America/New_York`` and converted to an aware UTC
    ``datetime``; ambiguous local times resolve to the later UTC instant and
    skipped local times raise ``ValueError`` (decision A-22). ``period`` is
    ``None`` when ``<PERIOD>`` is absent or empty. ``items`` joins every
    ``<ITEMS>`` line with ``,`` in file order and is ``None`` when the header
    has none.

    Raises :class:`MissingAcceptanceTime` when ``<ACCEPTANCE-DATETIME>`` is
    absent or empty (pre-2001 headers, decision A-26); the exception carries
    the form, filing date, period, items and any
    ``<DATE-OF-FILING-DATE-CHANGE>``. Raises plain ``ValueError`` naming the
    tag when any other required tag (``<ACCESSION-NUMBER>``, ``<TYPE>``,
    ``<FILING-DATE>``) is missing or malformed, when
    ``<ACCEPTANCE-DATETIME>`` is malformed, or when ``<PERIOD>`` or
    ``<DATE-OF-FILING-DATE-CHANGE>`` is present but malformed, so a corrupt
    cache never passes silently.
    """
    tags = _header_tags(text)
    accession = _required(tags, "ACCESSION-NUMBER")
    if not _ACCESSION_RE.fullmatch(accession):
        raise ValueError(f"malformed <ACCESSION-NUMBER>: {accession!r}")
    form = _required(tags, "TYPE")
    filing_date = _parse_day("FILING-DATE", _required(tags, "FILING-DATE"))
    periods = [value for value in tags.get("PERIOD", []) if value]
    period = _parse_day("PERIOD", periods[0]) if periods else None
    items = [value for value in tags.get("ITEMS", []) if value]
    items_text = ",".join(items) if items else None
    acceptances = [value for value in tags.get("ACCEPTANCE-DATETIME", []) if value]
    if not acceptances:
        changes = [
            _parse_day("DATE-OF-FILING-DATE-CHANGE", value)
            for value in tags.get("DATE-OF-FILING-DATE-CHANGE", [])
            if value
        ]
        raise MissingAcceptanceTime(
            HeaderWithoutAcceptance(
                accession=accession,
                form=form,
                filing_date=filing_date,
                period=period,
                items=items_text,
                date_of_filing_date_change=max(changes) if changes else None,
            )
        )
    acceptance = acceptances[0]
    match = _ACCEPTANCE_RE.fullmatch(acceptance)
    if match is None:
        raise ValueError(f"malformed <ACCEPTANCE-DATETIME>: {acceptance!r}")
    try:
        local = datetime(
            int(match[1]),
            int(match[2]),
            int(match[3]),
            int(match[4]),
            int(match[5]),
            int(match[6]),
        )
    except ValueError as exc:
        raise ValueError(f"malformed <ACCEPTANCE-DATETIME>: {acceptance!r}") from exc
    return HeaderInfo(
        accession=accession,
        form=form,
        acceptance_ts=_new_york_to_utc(local),
        filing_date=filing_date,
        period=period,
        items=items_text,
    )


def _end_of_day_utc(day: pl.Expr) -> pl.Expr:
    """Return the UTC instant of 23:59:59 New York on a ``DATE`` expression.

    ``day`` is a Polars expression holding a date. The result is a
    timezone-aware UTC datetime expression; an ambiguous local end of day (DST
    ending) resolves to the later instant and a skipped one raises (decision
    A-22), so the bound is never earlier than the truth.
    """
    return (
        (day.cast(pl.Datetime("us")) + pl.duration(hours=23, minutes=59, seconds=59))
        .dt.replace_time_zone("America/New_York", ambiguous="latest", non_existent="raise")
        .dt.convert_time_zone("UTC")
    )


def apply_acceptance(filings: pl.DataFrame, headers: Mapping[str, HeaderInfo]) -> pl.DataFrame:
    """Attach acceptance times and return a frame valid under ``FilingsSchema``.

    ``filings`` is a submissions frame with the ``FilingsSchema`` columns whose
    ``acceptance_ts``, ``acceptance_ts_source`` and ``ts_imputed`` are still
    null; ``headers`` maps an accession to its parsed :class:`HeaderInfo`. For a
    filing whose accession is in ``headers``, ``acceptance_ts`` is the header's
    aware UTC time, ``acceptance_ts_source`` is ``'sgml_header'`` and
    ``ts_imputed`` is false. Every other filing falls back to the later of
    ``filing_date`` 23:59:59 America/New_York and ``acceptance_ts_api + 6 h``
    (the API term only when it is present), with
    ``acceptance_ts_source='filing_date_eod'`` and ``ts_imputed=true``
    (decision A-23). The six-hour margin covers the submissions API's observed
    four- to five-hour shift in either direction, so the fallback stays an upper
    bound on the true acceptance time even for paper filings (which EDGAR dates
    up to a week before acceptance) and never leaks a value early; the
    end-of-day term is the bound for holdings with no API timestamp. The result
    is validated against ``FilingsSchema`` before it is returned;
    ``acceptance_ts_api`` is passed through untouched.
    """
    header_ts = pl.col("accession").replace_strict(
        {accession: info.acceptance_ts for accession, info in headers.items()},
        default=None,
        return_dtype=pl.Datetime("us", "UTC"),
    )
    has_header = header_ts.is_not_null()
    fallback_ts = pl.max_horizontal(
        _end_of_day_utc(pl.col("filing_date")),
        pl.col("acceptance_ts_api") + pl.duration(hours=6),
    )
    result = filings.with_columns(
        pl.when(has_header).then(header_ts).otherwise(fallback_ts).alias("acceptance_ts"),
        pl.when(has_header)
        .then(pl.lit("sgml_header"))
        .otherwise(pl.lit("filing_date_eod"))
        .alias("acceptance_ts_source"),
        (~has_header).alias("ts_imputed"),
    )
    FilingsSchema.validate(result)
    return result


def apply_acceptance_floors(filings: pl.DataFrame, floors: Mapping[str, date]) -> pl.DataFrame:
    """Raise imputed acceptance times to their accession's A-26 floor.

    ``filings`` is a frame returned by :func:`apply_acceptance`; ``floors`` maps
    an accession whose cached header has no ``<ACCEPTANCE-DATETIME>`` to the
    latest date in its ``<DATE-OF-FILING-DATE-CHANGE>`` (decision A-26). For
    those accessions, when ``acceptance_ts`` is imputed, it is raised to at
    least that date's 23:59:59 America/New_York; the existing fallback term is
    kept when it is later, so this can only move a value later (never earlier
    than the truth). Rows whose acceptance time came from an SGML header and
    accessions without a floor are untouched. The result is validated against
    ``FilingsSchema`` and returned; ``acceptance_ts_api`` is passed through.
    """
    if not floors:
        FilingsSchema.validate(filings)
        return filings
    floor_date = pl.col("accession").replace_strict(floors, default=None, return_dtype=pl.Date)
    result = filings.with_columns(
        pl.when(pl.col("ts_imputed"))
        .then(pl.max_horizontal(pl.col("acceptance_ts"), _end_of_day_utc(floor_date)))
        .otherwise(pl.col("acceptance_ts"))
        .alias("acceptance_ts")
    )
    FilingsSchema.validate(result)
    return result


def required_accessions(
    filings: pl.DataFrame, fact_accessions: Iterable[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Return the ``(cik, accession)`` pairs whose SGML headers must be fetched.

    ``filings`` is a submissions frame (``FilingsSchema`` columns; only
    ``cik``, ``accession`` and ``form`` are read). ``fact_accessions`` is an
    iterable of ``(cik, accession)`` pairs taken from companyfacts, already
    restricted to ``us-gaap``/``ifrs-full`` facts. The result is the sorted,
    de-duplicated union of every given pair -- including accessions that do not
    appear in ``filings``, which is common for facts filed before the
    submissions API's coverage -- and every ``8-K``/``8-K/A`` row of
    ``filings`` (decision A-1). Headers are immutable, so this is the set the
    fetch stage pulls once and caches forever.
    """
    pairs: set[tuple[str, str]] = {(cik, accession) for cik, accession in fact_accessions}
    pairs.update(
        (str(row[0]), str(row[1]))
        for row in filings.select("cik", "accession", "form").iter_rows()
        if row[2] in _EVENT_FORMS
    )
    return sorted(pairs)
