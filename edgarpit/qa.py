"""Data-quality report over a built database (FR-1.10).

:func:`run_qa` reads the column-contract tables (``main.fundamentals_pit``,
``main.ratios_pit``, ``main.events_8k``, ``main.filings``) plus the raw tables
(``raw.facts_raw``, ``raw.filings``, ``raw.concept_map``, ``raw.universe``) and
returns a :class:`QaReport`:

- coverage per issuer: the canonical fields x fiscal years populated by FY rows
  and every universe issuer with no rows at all;
- restatement counts per issuer, field and ``restatement_kind`` plus the largest
  revisions with both values and both acceptance times (the boundary-check
  audit list, which excludes split adjustments);
- imputed acceptance timestamps by source and header-vs-API disagreements over
  60 s for header-sourced rows (count and worst ten);
- header-required filings (a ``us-gaap``/``ifrs-full`` fact carrier or a
  ``8-K``/``8-K/A``) whose acceptance time had to be imputed, counted per
  issuer: the cached headers that carry no ``<ACCEPTANCE-DATETIME>`` (decision
  A-26) plus any required header the fetch stage could not retrieve;
- 8-K events accepted before the 2004 item-code change, counted per issuer (the
  pre-2004 item codes are not reliable metadata);
- filings by ``source`` (``recent``, page file names, ``header``) and by
  ``acceptance_ts_source``;
- unmapped ``us-gaap``/``ifrs-full`` concepts by frequency;
- unit anomalies: more than one currency per issuer and field, and observed
  unit families outside the families ``raw.concept_map`` declares;
- ratio gaps: periods with at least one input in ``fundamentals_pit`` but no
  ``ratios_pit`` row, counted by reason (mixed currency, zero denominator,
  ``OTHER`` period, missing input) with the first ten examples;
- conflicting values within one accession (decision A-7);
- key uniqueness of every contract table (and the raw tables); any violation
  makes :attr:`QaReport.ok` false. The report is read-only: nothing is written.

The Phase 2 sections (decisions A-32, A-38, A-39 and A-40) cover the tables
``raw.fx_daily``, ``main.fundamentals_pit_usd`` and ``raw.filings_text``. They
are computed only when their table exists: a database built before those tables
existed reports each section as "not built" in :func:`format_phase2` and never
raises. An ``asof_ts`` in ``main.fundamentals_pit_usd`` earlier than the
acceptance time or the FX known time behind it is a point-in-time key violation
and makes :attr:`QaReport.ok` false, like a duplicated contract key.

Timestamps are UTC; the connection's session timezone is set to UTC (decision
A-5). A missing table of the Phase 1 contract is a query error, not a report
entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any

import duckdb

# (table, key columns) checked for uniqueness; key columns are trusted
# identifiers from the frozen contracts, never user input. The two Phase 2 raw
# tables are skipped when the database predates them.
KEY_CHECKS: tuple[tuple[str, str], ...] = (
    ("main.fundamentals_pit", "cik, field, period_start, period_end, accession"),
    ("main.ratios_pit", "cik, ratio, period_start, period_end, asof_ts"),
    ("main.events_8k", "accession, item_code"),
    ("main.filings", "accession"),
    ("raw.filings", "accession"),
    ("raw.filings_text", "accession, document, section, section_order"),
    ("raw.fx_daily", "series, rate_date"),
)

_ANOMALY_LIMIT = 10

# New York date the 8-K item codes were expanded; earlier codes are unreliable.
LEGACY_ITEMS_BEFORE = "2004-08-23"

# The Bank of Canada Valet FXUSDCAD series starts on 2017-01-03 (decision
# A-39); a CAD period ending on or after that date must find a rate when the FX
# table is current.
FX_FIRST_DATE = date(2017, 1, 3)

# A rate more than five business days before the newest acceptance time is
# stale (decision A-32: rates are business daily).
FX_STALE_BUSINESS_DAYS = 5

ITEM_2_02 = "2.02"

# Why a period with an input produced no ``ratios_pit`` row; ``no candidate``
# means both inputs are present with one non-null currency and a non-zero
# denominator, so the dbt model should have produced a row.
RATIO_GAP_REASONS: tuple[str, ...] = (
    "mixed currency",
    "zero denominator",
    "OTHER period",
    "missing input",
    "no candidate",
)


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    """Return the single value of a one-row query."""
    row = con.execute(sql).fetchone()
    if row is None:  # pragma: no cover - aggregate queries always return a row
        raise RuntimeError(f"query returned no row: {sql}")
    return row[0]


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    """True when ``schema.table`` exists as a table or a view.

    Phase 2 sections are optional, so the report asks before it queries. dbt
    materializes ``main`` models as views, hence the check reads
    ``information_schema.tables`` (which lists views) rather than
    ``duckdb_tables()``.
    """
    row = con.execute(
        """
        SELECT count(*)
        FROM information_schema.tables
        WHERE table_schema = ? AND table_name = ?
        """,
        [schema, table],
    ).fetchone()
    return bool(row and row[0])


def _column_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str, column: str) -> bool:
    """True when ``schema.table`` exists and has ``column``."""
    row = con.execute(
        """
        SELECT count(*)
        FROM information_schema.columns
        WHERE table_schema = ? AND table_name = ? AND column_name = ?
        """,
        [schema, table, column],
    ).fetchone()
    return bool(row and row[0])


def _business_days_between(earlier: date, later: date) -> int:
    """Number of Monday-Friday dates after ``earlier`` and on or before ``later``.

    Public holidays are not modelled: the count is calendar weekdays, which can
    only overstate a gap, so a staleness alarm fires no later than it should.
    """
    days = (later - earlier).days
    if days <= 0:
        return 0
    weeks, remainder = divmod(days, 7)
    start = earlier + timedelta(days=weeks * 7)
    return weeks * 5 + sum(
        1 for offset in range(1, remainder + 1) if (start + timedelta(days=offset)).weekday() < 5
    )


def _iso_date(value: date | None) -> str:
    """``value`` as ISO-8601, or ``'-'`` when null."""
    return "-" if value is None else value.isoformat()


def _iso_ts(value: datetime) -> str:
    """Format an aware UTC timestamp as ISO-8601 with a ``Z`` suffix."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _days(count: int) -> str:
    """``count`` with a singular or plural ``day``."""
    return f"{count} day" if count == 1 else f"{count} days"


def _period_label(period_start: date | None, period_end: date) -> str:
    """``start..end`` with ``instant`` for a null start, as the CLI prints it."""
    start = _iso_date(period_start) if period_start is not None else "instant"
    return f"{start}..{period_end.isoformat()}"


@dataclass(frozen=True)
class IssuerCoverage:
    """One issuer's fundamentals coverage."""

    cik: str
    ticker: str | None
    name: str | None
    rows: int
    fy_years: list[int]
    by_field: dict[str, list[int]]


@dataclass(frozen=True)
class UniverseIssuer:
    """A universe issuer with no ``fundamentals_pit`` rows at all."""

    cik: str
    ticker: str
    name: str


@dataclass(frozen=True)
class RestatementCount:
    """How many restated versions an issuer has for one field and kind."""

    cik: str
    field: str
    kind: str
    count: int


@dataclass(frozen=True)
class Restatement:
    """One restated version with the first version it changed away from."""

    cik: str
    ticker: str | None
    field: str
    period_start: date | None
    period_end: date
    first_value: float
    first_acceptance_ts: datetime
    value: float
    acceptance_ts: datetime
    pct_change_vs_first: float | None


@dataclass(frozen=True)
class AcceptanceDisagreement:
    """A filing whose header and API acceptance times differ by over 60 s."""

    cik: str
    accession: str
    acceptance_ts: datetime
    acceptance_ts_api: datetime
    seconds: float


@dataclass(frozen=True)
class LegacyEventCount:
    """8-K events one issuer accepted before the 2004 item-code change."""

    cik: str
    ticker: str | None
    count: int


@dataclass(frozen=True)
class HeaderWithoutAcceptanceTime:
    """Header-required filings one issuer timed with the A-23 fallback.

    A header-required filing carries a ``us-gaap``/``ifrs-full`` fact or is an
    ``8-K``/``8-K/A``, so the fetch stage retrieved its SGML header. A count
    here means the header yielded no acceptance time (pre-2001 headers, decision
    A-26) or was never retrieved, and the build used the conservative
    ``filing_date_eod`` bound instead.
    """

    cik: str
    ticker: str | None
    count: int


@dataclass(frozen=True)
class FilingCount:
    """How many ``raw.filings`` rows carry one value of a column."""

    source: str
    count: int


@dataclass(frozen=True)
class RatioGap:
    """A period with an input but no ``ratios_pit`` row, and the reason why."""

    cik: str
    ratio: str
    period_start: date | None
    period_end: date
    period_type: str
    reason: str


@dataclass(frozen=True)
class UnmappedConcept:
    """A ``us-gaap``/``ifrs-full`` concept with no row in ``raw.concept_map``."""

    taxonomy: str
    concept: str
    count: int


@dataclass(frozen=True)
class MultiCurrency:
    """One issuer and field reported in more than one currency."""

    cik: str
    field: str
    currencies: list[str]


@dataclass(frozen=True)
class UnexpectedUnit:
    """A unit whose family is outside the families declared for the field."""

    cik: str
    field: str
    unit: str
    expected_families: list[str]


@dataclass(frozen=True)
class ConflictingValue:
    """One accession carrying several values for the same fact key (A-7)."""

    cik: str
    taxonomy: str
    concept: str
    unit: str
    period_start: date | None
    period_end: date
    accession: str
    values: list[float]


@dataclass(frozen=True)
class KeyViolation:
    """Duplicate rows for a contract table's key."""

    table: str
    key: str
    duplicates: int


@dataclass(frozen=True)
class FxSeriesCoverage:
    """Coverage of one ``raw.fx_daily`` series.

    ``first_date``/``last_date`` bound the observation dates, ``rows`` counts
    stored rows (not distinct dates), and the largest gap is the widest interval
    between two consecutive observation dates, in calendar and Monday-Friday
    business days. ``business_days_since_last`` counts business days after
    ``last_date`` up to the UTC date of ``latest_acceptance_ts`` (the newest
    ``raw.filings.acceptance_ts``); ``stale`` is true when that count exceeds
    five, or when there is no observation at all and filings exist.
    """

    series: str
    rows: int
    first_date: date | None
    last_date: date | None
    largest_gap_calendar_days: int | None
    largest_gap_business_days: int | None
    latest_acceptance_ts: datetime | None
    business_days_since_last: int | None
    stale: bool


@dataclass(frozen=True)
class FxCoverage:
    """The ``raw.fx_daily`` section; ``built`` is false when the table is absent.

    ``latest_acceptance_ts`` is the newest ``raw.filings`` acceptance time used
    as the staleness reference. ``stale`` is true when any series is stale, and
    when the table exists, has no observation at all, and filings exist.
    """

    built: bool = False
    latest_acceptance_ts: datetime | None = None
    by_series: list[FxSeriesCoverage] = field(default_factory=list)

    @property
    def stale(self) -> bool:
        """True when a series is more than five business days behind."""
        if not self.built:
            return False
        if not self.by_series:
            return self.latest_acceptance_ts is not None
        return any(series.stale for series in self.by_series)


@dataclass(frozen=True)
class ConversionCount:
    """``main.fundamentals_pit_usd`` rows sharing a conversion status and currency.

    ``converted`` is the row's flag; ``unconverted_reason`` is
    ``no_rate``/``currency_out_of_scope`` for an unconverted row, null for a
    converted one (and for every row of a database whose model predates the
    column); ``currency`` is the row's source currency, null when it has none.
    """

    converted: bool
    unconverted_reason: str | None
    currency: str | None
    count: int


@dataclass(frozen=True)
class NoRateIssuer:
    """An issuer with ``no_rate`` rows whose period ends on or after 2017-01-03."""

    cik: str
    ticker: str | None
    rows: int


@dataclass(frozen=True)
class AsOfViolation:
    """A converted row whose ``asof_ts`` precedes when it could be known.

    Either ``asof_ts < acceptance_ts`` or ``asof_ts < fx_known_at`` (decision
    A-32), which lets a value appear before its filing or its exchange rate.
    """

    cik: str
    field: str
    period_start: date | None
    period_end: date
    accession: str
    acceptance_ts: datetime
    fx_known_at: datetime | None
    asof_ts: datetime


@dataclass(frozen=True)
class UsdConversion:
    """The ``main.fundamentals_pit_usd`` section.

    ``counts`` groups every row by conversion status and currency;
    ``no_rate_issuers`` lists issuers whose CAD period ends on or after
    2017-01-03 without a rate (those must be zero while FX is current);
    ``asof_violations`` are point-in-time key violations, counted by
    ``asof_violation_count`` and capped in the detail list. ``built`` is false
    when the model does not exist.
    """

    built: bool = False
    counts: list[ConversionCount] = field(default_factory=list)
    no_rate_rows: int = 0
    no_rate_issuers: list[NoRateIssuer] = field(default_factory=list)
    asof_violation_count: int = 0
    asof_violations: list[AsOfViolation] = field(default_factory=list)


@dataclass(frozen=True)
class MissingTextIssuer:
    """An issuer with Item 2.02 8-Ks that have no ``raw.filings_text`` rows."""

    cik: str
    ticker: str | None
    filings: int


@dataclass(frozen=True)
class TextCoverage:
    """The ``raw.filings_text`` section (decision A-40).

    ``item_202_filings`` counts distinct ``raw.events_8k`` accessions carrying
    item 2.02; ``item_202_without_text`` those with no text row at all, and
    ``issuers_without_text`` the top issuers by that count. ``primary_documents``
    and ``unsegmented_primaries`` count distinct primary documents (a whole
    document fell back to ``section='unsegmented'``); ``unsegmented_share`` is
    their ratio. ``zero_length_sections`` counts rows whose text or
    ``char_count`` is empty; ``duplicate_sha256_accessions`` counts text hashes
    shared by more than one accession (press releases can legitimately repeat).
    """

    built: bool = False
    item_202_filings: int = 0
    item_202_without_text: int = 0
    issuers_without_text: list[MissingTextIssuer] = field(default_factory=list)
    primary_documents: int = 0
    unsegmented_primaries: int = 0
    zero_length_sections: int = 0
    duplicate_sha256_accessions: int = 0

    @property
    def unsegmented_share(self) -> float | None:
        """Share of primary documents that are one ``unsegmented`` row, or null."""
        if not self.primary_documents:
            return None
        return self.unsegmented_primaries / self.primary_documents


@dataclass
class QaReport:
    """Everything :func:`run_qa` found; ``ok`` is false on any key violation.

    A point-in-time key violation (``asof_ts`` before ``acceptance_ts`` or
    ``fx_known_at``) counts like a duplicated key.
    """

    canonical_fields: int = 0
    universe_issuers: int = 0
    coverage: list[IssuerCoverage] = field(default_factory=list)
    issuers_without_rows: list[UniverseIssuer] = field(default_factory=list)
    restatement_counts: list[RestatementCount] = field(default_factory=list)
    top_restatements: list[Restatement] = field(default_factory=list)
    imputed_by_source: dict[str, int] = field(default_factory=dict)
    disagreement_count: int = 0
    disagreements: list[AcceptanceDisagreement] = field(default_factory=list)
    headers_without_acceptance: list[HeaderWithoutAcceptanceTime] = field(default_factory=list)
    legacy_events: list[LegacyEventCount] = field(default_factory=list)
    filings_by_source: list[FilingCount] = field(default_factory=list)
    filings_by_acceptance_source: list[FilingCount] = field(default_factory=list)
    unmapped_concepts: list[UnmappedConcept] = field(default_factory=list)
    multi_currency: list[MultiCurrency] = field(default_factory=list)
    unexpected_units: list[UnexpectedUnit] = field(default_factory=list)
    ratio_gap_counts: dict[str, int] = field(default_factory=dict)
    ratio_gaps: list[RatioGap] = field(default_factory=list)
    conflicting_values: list[ConflictingValue] = field(default_factory=list)
    fx_coverage: FxCoverage = field(default_factory=FxCoverage)
    usd_conversion: UsdConversion = field(default_factory=UsdConversion)
    text_coverage: TextCoverage = field(default_factory=TextCoverage)
    key_violations: list[KeyViolation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every contract key is unique and no ``asof_ts`` leak exists.

        Duplicated keys and ``main.fundamentals_pit_usd`` rows whose ``asof_ts``
        precedes their acceptance time or FX known time both make a point-in-time
        table unusable, so either is a non-zero exit.
        """
        return not self.key_violations and not self.usd_conversion.asof_violation_count


def _coverage(con: duckdb.DuckDBPyConnection) -> tuple[list[IssuerCoverage], int]:
    universe = con.execute(
        "SELECT cik, ticker, name FROM raw.universe ORDER BY cik, ticker"
    ).fetchall()
    universe_names = {row[0]: row[2] for row in universe}
    universe_tickers = {row[0]: row[1] for row in universe}
    summary = con.execute(
        """
        SELECT cik, any_value(ticker), count(*)
        FROM main.fundamentals_pit
        GROUP BY cik
        ORDER BY cik
        """
    ).fetchall()
    years: dict[str, dict[str, list[int]]] = {}
    for cik, field_name, year in con.execute(
        """
        SELECT cik, field, year(period_end)
        FROM main.fundamentals_pit
        WHERE period_type = 'FY'
        GROUP BY ALL
        ORDER BY cik, field, year(period_end)
        """
    ).fetchall():
        years.setdefault(cik, {}).setdefault(field_name, []).append(int(year))
    coverage = [
        IssuerCoverage(
            cik=cik,
            ticker=ticker or universe_tickers.get(cik),
            name=universe_names.get(cik),
            rows=int(rows),
            fy_years=sorted(
                {year for field_years in years.get(cik, {}).values() for year in field_years}
            ),
            by_field={name: sorted(set(values)) for name, values in years.get(cik, {}).items()},
        )
        for cik, ticker, rows in summary
    ]
    return coverage, len(universe)


def _restatements(
    con: duckdb.DuckDBPyConnection,
) -> tuple[list[RestatementCount], list[Restatement]]:
    """Restated versions by kind, and the largest ``revision`` rows.

    Split adjustments move share-based values by a near-integer factor and are
    not accounting revisions, so the boundary-check list carries only
    ``revision`` rows; the counts keep every kind so a spike in split
    adjustments stays visible.
    """
    counts = [
        RestatementCount(cik=row[0], field=row[1], kind=row[2], count=int(row[3]))
        for row in con.execute(
            """
            SELECT cik, field, restatement_kind, count(*)
            FROM main.fundamentals_pit
            WHERE is_restatement
            GROUP BY cik, field, restatement_kind
            ORDER BY count(*) DESC, cik, field, restatement_kind
            """
        ).fetchall()
    ]
    top = [
        Restatement(
            cik=row[0],
            ticker=row[1],
            field=row[2],
            period_start=row[3],
            period_end=row[4],
            first_value=float(row[5]),
            first_acceptance_ts=row[6],
            value=float(row[7]),
            acceptance_ts=row[8],
            pct_change_vs_first=None if row[9] is None else float(row[9]),
        )
        for row in con.execute(
            f"""
            WITH firsts AS (
                SELECT cik, field, period_start, period_end, value, acceptance_ts
                FROM main.fundamentals_pit
                WHERE version = 1
            )
            SELECT r.cik, r.ticker, r.field, r.period_start, r.period_end,
                   f.value, f.acceptance_ts, r.value, r.acceptance_ts, r.pct_change_vs_first
            FROM main.fundamentals_pit AS r
            JOIN firsts AS f
              ON r.cik = f.cik
             AND r.field = f.field
             AND r.period_start IS NOT DISTINCT FROM f.period_start
             AND r.period_end = f.period_end
            WHERE r.is_restatement AND r.restatement_kind = 'revision'
            ORDER BY abs(r.pct_change_vs_first) DESC NULLS LAST, r.cik, r.field, r.period_end
            LIMIT {_ANOMALY_LIMIT}
            """
        ).fetchall()
    ]
    return counts, top


def _timestamps(
    con: duckdb.DuckDBPyConnection,
) -> tuple[dict[str, int], int, list[AcceptanceDisagreement]]:
    """Imputed counts and header-vs-API disagreements.

    The disagreement count and the worst list compare only header-sourced
    acceptances: an imputed ``acceptance_ts`` is the conservative A-23 upper
    bound (``filing_date`` end of day or ``acceptance_ts_api + 6 h``), so it
    differs from the API by construction and would swamp the diagnostic.
    """
    imputed = {
        row[0]: int(row[1])
        for row in con.execute(
            """
            SELECT acceptance_ts_source, count(*)
            FROM raw.filings
            WHERE ts_imputed
            GROUP BY acceptance_ts_source
            ORDER BY acceptance_ts_source
            """
        ).fetchall()
    }
    disagreement_count = int(
        _scalar(
            con,
            """
            SELECT count(*)
            FROM raw.filings
            WHERE acceptance_ts_api IS NOT NULL AND acceptance_ts IS NOT NULL
              AND acceptance_ts_source = 'sgml_header'
              AND abs(epoch(acceptance_ts) - epoch(acceptance_ts_api)) > 60
            """,
        )
    )
    worst = [
        AcceptanceDisagreement(
            cik=row[0],
            accession=row[1],
            acceptance_ts=row[2],
            acceptance_ts_api=row[3],
            seconds=float(row[4]),
        )
        for row in con.execute(
            f"""
            SELECT cik, accession, acceptance_ts, acceptance_ts_api,
                   abs(epoch(acceptance_ts) - epoch(acceptance_ts_api)) AS seconds
            FROM raw.filings
            WHERE acceptance_ts_api IS NOT NULL AND acceptance_ts IS NOT NULL
              AND acceptance_ts_source = 'sgml_header'
              AND abs(epoch(acceptance_ts) - epoch(acceptance_ts_api)) > 60
            ORDER BY seconds DESC, accession
            LIMIT {_ANOMALY_LIMIT}
            """
        ).fetchall()
    ]
    return imputed, disagreement_count, worst


def _headers_without_acceptance(
    con: duckdb.DuckDBPyConnection,
) -> list[HeaderWithoutAcceptanceTime]:
    """Header-required filings per issuer whose acceptance time was imputed.

    A filing needs a header when it carries a ``us-gaap``/``ifrs-full`` fact or
    is an ``8-K``/``8-K/A`` (decision A-1); the fetch stage retrieves exactly
    those headers. Counting the imputed rows of that set gives the headers that
    produced no acceptance time (pre-2001 headers without
    ``<ACCEPTANCE-DATETIME>``, decision A-26), plus a required header that
    could not be retrieved. Rows are ordered by CIK.
    """
    return [
        HeaderWithoutAcceptanceTime(cik=row[0], ticker=row[1], count=int(row[2]))
        for row in con.execute(
            """
            WITH header_required AS (
                SELECT DISTINCT accession
                FROM raw.facts_raw
                WHERE taxonomy IN ('us-gaap', 'ifrs-full')
                UNION
                SELECT accession
                FROM raw.filings
                WHERE form IN ('8-K', '8-K/A')
            ), imputed AS (
                SELECT filings.cik, count(*) AS count
                FROM raw.filings AS filings
                JOIN header_required ON header_required.accession = filings.accession
                WHERE filings.acceptance_ts_source = 'filing_date_eod'
                GROUP BY filings.cik
            )
            SELECT imputed.cik, universe.ticker, imputed.count
            FROM imputed
            LEFT JOIN raw.universe AS universe ON universe.cik = imputed.cik
            ORDER BY imputed.cik
            """
        ).fetchall()
    ]


def _legacy_events(con: duckdb.DuckDBPyConnection) -> list[LegacyEventCount]:
    """8-K events accepted in New York before 2004-08-23, per issuer.

    The item codes on those filings predate the item expansion, so their
    ``item_code``/``item_label`` metadata is not reliable; the count marks
    coverage that audits should not read as events.
    """
    return [
        LegacyEventCount(cik=row[0], ticker=row[1], count=int(row[2]))
        for row in con.execute(
            f"""
            SELECT cik, any_value(ticker), count(*)
            FROM main.events_8k
            WHERE timezone('America/New_York', acceptance_ts)
                  < TIMESTAMP '{LEGACY_ITEMS_BEFORE}'
            GROUP BY cik
            ORDER BY cik
            """
        ).fetchall()
    ]


def _filings_by(con: duckdb.DuckDBPyConnection, column: str) -> list[FilingCount]:
    """``raw.filings`` row counts per value of ``column``, ordered by value.

    ``column`` is a trusted contract column name (``source`` or
    ``acceptance_ts_source``), never user input.
    """
    return [
        FilingCount(source=row[0], count=int(row[1]))
        for row in con.execute(
            f"SELECT {column}, count(*) FROM raw.filings GROUP BY {column} ORDER BY {column}"
        ).fetchall()
    ]


def _ratio_gaps(con: duckdb.DuckDBPyConnection) -> tuple[dict[str, int], list[RatioGap]]:
    """Periods with an input but no ``ratios_pit`` row, by reason and example.

    The period set is the latest version of each ratio input in
    ``fundamentals_pit``, paired across the two fields with the model's period
    rules (equal ``period_type`` and period end; a duration numerator may pair
    with an instant denominator). A period counts as a gap when ``ratios_pit``
    holds no row for it. The reason follows the model's filters in order:
    ``OTHER`` periods are never considered, a missing or unpaired input is
    ``missing input``, a non-matching currency is ``mixed currency`` and a zero
    denominator is ``zero denominator``. Returns the counts per reason and the
    first :data:`_ANOMALY_LIMIT` gaps ordered by reason, issuer and period.
    """
    rows = con.execute(
        """
        WITH ratio_fields(ratio, numerator_field, denominator_field) AS (
            VALUES
                ('roe', 'net_income', 'total_equity'),
                ('leverage', 'total_liabilities', 'total_equity'),
                ('net_margin', 'net_income', 'revenue')
        ),
        latest AS (
            SELECT * EXCLUDE (row_num)
            FROM (
                SELECT
                    facts.cik,
                    facts.field,
                    facts.period_start,
                    facts.period_end,
                    facts.period_type,
                    facts.value,
                    facts.currency,
                    row_number() OVER (
                        PARTITION BY
                            facts.cik, facts.field, facts.period_start,
                            facts.period_end, facts.period_type
                        ORDER BY facts.acceptance_ts DESC, facts.version DESC
                    ) AS row_num
                FROM main.fundamentals_pit AS facts
                WHERE facts.field IN (
                    SELECT numerator_field FROM ratio_fields
                    UNION
                    SELECT denominator_field FROM ratio_fields
                )
            )
            WHERE row_num = 1
        ),
        numerator_periods AS (
            SELECT ratio_fields.ratio, numerator.*
            FROM ratio_fields
            JOIN latest AS numerator ON numerator.field = ratio_fields.numerator_field
        ),
        denominator_periods AS (
            SELECT ratio_fields.ratio, denominator.*
            FROM ratio_fields
            JOIN latest AS denominator ON denominator.field = ratio_fields.denominator_field
        ),
        periods AS (
            SELECT
                coalesce(numerator.ratio, denominator.ratio) AS ratio,
                coalesce(numerator.cik, denominator.cik) AS cik,
                coalesce(numerator.period_start, denominator.period_start) AS period_start,
                coalesce(numerator.period_end, denominator.period_end) AS period_end,
                coalesce(numerator.period_type, denominator.period_type) AS period_type,
                numerator.period_start AS numerator_start,
                denominator.period_start AS denominator_start,
                numerator.value AS numerator_value,
                numerator.currency AS numerator_currency,
                denominator.value AS denominator_value,
                denominator.currency AS denominator_currency
            FROM numerator_periods AS numerator
            FULL OUTER JOIN denominator_periods AS denominator
                ON denominator.ratio = numerator.ratio
                AND denominator.cik = numerator.cik
                AND denominator.period_end = numerator.period_end
                AND denominator.period_type = numerator.period_type
                AND (
                    (numerator.period_start IS NULL AND denominator.period_start IS NULL)
                    OR (numerator.period_start IS NOT NULL AND denominator.period_start IS NULL)
                    OR (
                        numerator.period_start IS NOT NULL
                        AND denominator.period_start = numerator.period_start
                    )
                )
        ),
        gaps AS (
            SELECT
                periods.*,
                CASE
                    WHEN periods.period_type = 'OTHER' THEN 'OTHER period'
                    WHEN periods.numerator_value IS NULL
                        OR periods.denominator_value IS NULL
                        OR (
                            periods.numerator_start IS NULL
                            AND periods.denominator_start IS NOT NULL
                        )
                        THEN 'missing input'
                    WHEN periods.numerator_currency IS NULL
                        OR periods.denominator_currency IS NULL
                        OR periods.numerator_currency <> periods.denominator_currency
                        THEN 'mixed currency'
                    WHEN periods.denominator_value = 0 THEN 'zero denominator'
                    ELSE 'no candidate'
                END AS reason
            FROM periods
            WHERE NOT EXISTS (
                SELECT 1
                FROM main.ratios_pit AS ratios
                WHERE ratios.cik = periods.cik
                  AND ratios.ratio = periods.ratio
                  AND ratios.period_end = periods.period_end
                  AND ratios.period_type = periods.period_type
                  AND ratios.period_start IS NOT DISTINCT FROM periods.period_start
            )
        )
        SELECT cik, ratio, period_start, period_end, period_type, reason
        FROM gaps
        ORDER BY reason, cik, ratio, period_end
        """
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row[5]] = counts.get(row[5], 0) + 1
    ordered = sorted(
        rows, key=lambda row: (RATIO_GAP_REASONS.index(row[5]), row[0], row[1], row[3])
    )
    gaps = [
        RatioGap(
            cik=row[0],
            ratio=row[1],
            period_start=row[2],
            period_end=row[3],
            period_type=row[4],
            reason=row[5],
        )
        for row in ordered[:_ANOMALY_LIMIT]
    ]
    return {reason: counts[reason] for reason in RATIO_GAP_REASONS if reason in counts}, gaps


def _unmapped(con: duckdb.DuckDBPyConnection) -> list[UnmappedConcept]:
    return [
        UnmappedConcept(taxonomy=row[0], concept=row[1], count=int(row[2]))
        for row in con.execute(
            """
            SELECT f.taxonomy, f.concept, count(*)
            FROM raw.facts_raw AS f
            LEFT JOIN raw.concept_map AS m
              ON f.taxonomy = m.taxonomy AND f.concept = m.concept
            WHERE m.concept IS NULL AND f.taxonomy IN ('us-gaap', 'ifrs-full')
            GROUP BY f.taxonomy, f.concept
            ORDER BY count(*) DESC, f.taxonomy, f.concept
            """
        ).fetchall()
    ]


def _unit_anomalies(
    con: duckdb.DuckDBPyConnection,
) -> tuple[list[MultiCurrency], list[UnexpectedUnit]]:
    multi = [
        MultiCurrency(cik=row[0], field=row[1], currencies=list(row[2]))
        for row in con.execute(
            """
            SELECT cik, field, list(DISTINCT currency ORDER BY currency)
            FROM main.fundamentals_pit
            WHERE currency IS NOT NULL
            GROUP BY cik, field
            HAVING count(DISTINCT currency) > 1
            ORDER BY cik, field
            """
        ).fetchall()
    ]
    unexpected = [
        UnexpectedUnit(cik=row[0], field=row[1], unit=row[2], expected_families=list(row[3]))
        for row in con.execute(
            """
            WITH expected AS (
                SELECT field, list(DISTINCT unit_family ORDER BY unit_family) AS families
                FROM raw.concept_map
                GROUP BY field
            ), observed AS (
                SELECT DISTINCT cik, field, unit,
                       CASE
                           WHEN regexp_matches(unit, '^[A-Z]{3}$') THEN 'currency'
                           WHEN unit = 'shares' THEN 'shares'
                           WHEN regexp_matches(unit, '^[A-Z]{3}/shares$') THEN 'currency_per_share'
                           ELSE 'unknown'
                       END AS family
                FROM main.fundamentals_pit
            )
            SELECT o.cik, o.field, o.unit, coalesce(e.families, [])
            FROM observed AS o
            LEFT JOIN expected AS e ON o.field = e.field
            WHERE NOT list_contains(coalesce(e.families, []), o.family)
            ORDER BY o.cik, o.field, o.unit
            """
        ).fetchall()
    ]
    return multi, unexpected


def _conflicts(con: duckdb.DuckDBPyConnection) -> list[ConflictingValue]:
    return [
        ConflictingValue(
            cik=row[0],
            taxonomy=row[1],
            concept=row[2],
            unit=row[3],
            period_start=row[4],
            period_end=row[5],
            accession=row[6],
            values=[float(value) for value in row[7]],
        )
        for row in con.execute(
            """
            SELECT cik, taxonomy, concept, unit, period_start, period_end, accession,
                   list(DISTINCT value ORDER BY value) AS values
            FROM raw.facts_raw
            GROUP BY ALL
            HAVING count(DISTINCT value) > 1
            ORDER BY cik, accession, concept
            """
        ).fetchall()
    ]


def _fx_coverage(con: duckdb.DuckDBPyConnection) -> FxCoverage:
    """``raw.fx_daily`` range, gaps and staleness against the newest acceptance.

    The staleness reference is the UTC date of the latest
    ``raw.filings.acceptance_ts``: a series ending well before the newest filing
    has no observation near that filing's period end. Business days are
    Monday-Friday (no holiday calendar), so the count never understates a gap.
    """
    if not _table_exists(con, "raw", "fx_daily"):
        return FxCoverage()
    latest_row = con.execute("SELECT max(acceptance_ts) FROM raw.filings").fetchone()
    latest: datetime | None = latest_row[0] if latest_row else None
    reference = None if latest is None else latest.astimezone(UTC).date()
    row_counts = {
        row[0]: int(row[1])
        for row in con.execute(
            "SELECT series, count(*) FROM raw.fx_daily GROUP BY series ORDER BY series"
        ).fetchall()
    }
    dates: dict[str, list[date]] = {}
    for series, rate_date in con.execute(
        "SELECT DISTINCT series, rate_date FROM raw.fx_daily ORDER BY series, rate_date"
    ).fetchall():
        dates.setdefault(series, []).append(rate_date)
    by_series: list[FxSeriesCoverage] = []
    for series, row_count in row_counts.items():
        observed = dates.get(series, [])
        first = observed[0] if observed else None
        last = observed[-1] if observed else None
        largest_calendar: int | None = None
        largest_business: int | None = None
        for previous, current in pairwise(observed):
            calendar_gap = (current - previous).days
            business_gap = _business_days_between(previous, current)
            largest_calendar = (
                calendar_gap if largest_calendar is None else max(largest_calendar, calendar_gap)
            )
            largest_business = (
                business_gap if largest_business is None else max(largest_business, business_gap)
            )
        since_last = (
            None if reference is None or last is None else _business_days_between(last, reference)
        )
        by_series.append(
            FxSeriesCoverage(
                series=series,
                rows=row_count,
                first_date=first,
                last_date=last,
                largest_gap_calendar_days=largest_calendar,
                largest_gap_business_days=largest_business,
                latest_acceptance_ts=latest,
                business_days_since_last=since_last,
                stale=reference is not None
                and (since_last is None or since_last > FX_STALE_BUSINESS_DAYS),
            )
        )
    return FxCoverage(built=True, latest_acceptance_ts=latest, by_series=by_series)


def _usd_conversion(con: duckdb.DuckDBPyConnection) -> UsdConversion:
    """Conversion coverage and ``asof_ts`` leaks of ``main.fundamentals_pit_usd``.

    Rows are grouped by ``(converted, unconverted_reason, currency)``. The
    reason column is read only when the model has it: a database whose model
    predates decision A-38 keeps its ``converted`` flag and reports every
    unconverted row with a null reason instead of failing. ``no_rate`` rows are
    listed per issuer only from ``FX_FIRST_DATE`` on, because a period end
    before the first observation cannot have a rate (decision A-39). A row whose
    ``asof_ts`` precedes ``acceptance_ts`` or ``fx_known_at`` is a key violation.
    """
    if not _table_exists(con, "main", "fundamentals_pit_usd"):
        return UsdConversion()
    reason = "unconverted_reason"
    if not _column_exists(con, "main", "fundamentals_pit_usd", "unconverted_reason"):
        reason = "CAST(NULL AS VARCHAR)"
    counts = [
        ConversionCount(
            converted=bool(row[0]),
            unconverted_reason=row[1],
            currency=row[2],
            count=int(row[3]),
        )
        for row in con.execute(
            f"""
            SELECT converted, {reason} AS unconverted_reason, currency, count(*)
            FROM main.fundamentals_pit_usd
            GROUP BY converted, unconverted_reason, currency
            ORDER BY converted DESC, unconverted_reason, currency
            """
        ).fetchall()
    ]
    no_rate_issuers = [
        NoRateIssuer(cik=row[0], ticker=row[1], rows=int(row[2]))
        for row in con.execute(
            f"""
            SELECT cik, any_value(ticker), count(*)
            FROM main.fundamentals_pit_usd
            WHERE NOT converted
              AND {reason} = 'no_rate'
              AND period_end >= DATE '{FX_FIRST_DATE.isoformat()}'
            GROUP BY cik
            ORDER BY cik
            """
        ).fetchall()
    ]
    asof_violation_count = int(
        _scalar(
            con,
            """
            SELECT count(*)
            FROM main.fundamentals_pit_usd
            WHERE asof_ts < acceptance_ts
               OR (fx_known_at IS NOT NULL AND asof_ts < fx_known_at)
            """,
        )
    )
    asof_violations = [
        AsOfViolation(
            cik=row[0],
            field=row[1],
            period_start=row[2],
            period_end=row[3],
            accession=row[4],
            acceptance_ts=row[5],
            fx_known_at=row[6],
            asof_ts=row[7],
        )
        for row in con.execute(
            f"""
            SELECT cik, field, period_start, period_end, accession,
                   acceptance_ts, fx_known_at, asof_ts
            FROM main.fundamentals_pit_usd
            WHERE asof_ts < acceptance_ts
               OR (fx_known_at IS NOT NULL AND asof_ts < fx_known_at)
            ORDER BY cik, field, period_end, accession, asof_ts
            LIMIT {_ANOMALY_LIMIT}
            """
        ).fetchall()
    ]
    return UsdConversion(
        built=True,
        counts=counts,
        no_rate_rows=sum(issuer.rows for issuer in no_rate_issuers),
        no_rate_issuers=no_rate_issuers,
        asof_violation_count=asof_violation_count,
        asof_violations=asof_violations,
    )


def _text_coverage(con: duckdb.DuckDBPyConnection) -> TextCoverage:
    """Item 2.02 coverage, segmentation and text duplications in ``raw.filings_text``.

    A filing counts as covered when it has any ``raw.filings_text`` row; a
    primary document counts as unsegmented when it produced a single
    ``section='unsegmented'`` row (decision A-40). Empty text or an empty
    ``char_count`` is a zero-length section. A hash shared by more than one
    accession is counted but is not a failure: press releases repeat text.
    """
    if not _table_exists(con, "raw", "filings_text"):
        return TextCoverage()
    item_filings = int(
        _scalar(
            con,
            f"SELECT count(DISTINCT accession) FROM raw.events_8k WHERE item_code = '{ITEM_2_02}'",
        )
    )
    missing_rows = con.execute(
        f"""
        WITH items AS (
            SELECT DISTINCT accession, cik
            FROM raw.events_8k
            WHERE item_code = '{ITEM_2_02}'
        )
        SELECT items.cik, universe.ticker, count(*)
        FROM items
        LEFT JOIN raw.universe AS universe ON universe.cik = items.cik
        WHERE NOT EXISTS (
            SELECT 1
            FROM raw.filings_text AS text
            WHERE text.accession = items.accession
        )
        GROUP BY items.cik, universe.ticker
        ORDER BY count(*) DESC, items.cik
        LIMIT {_ANOMALY_LIMIT}
        """
    ).fetchall()
    missing_total = int(
        _scalar(
            con,
            f"""
            SELECT count(*)
            FROM (
                SELECT DISTINCT accession
                FROM raw.events_8k
                WHERE item_code = '{ITEM_2_02}'
            ) AS items
            WHERE NOT EXISTS (
                SELECT 1
                FROM raw.filings_text AS text
                WHERE text.accession = items.accession
            )
            """,
        )
    )
    primary_documents = int(
        _scalar(
            con,
            """
            SELECT count(*)
            FROM (
                SELECT DISTINCT accession, document
                FROM raw.filings_text
                WHERE doc_type = 'primary'
            )
            """,
        )
    )
    unsegmented_primaries = int(
        _scalar(
            con,
            """
            SELECT count(*)
            FROM (
                SELECT DISTINCT accession, document
                FROM raw.filings_text
                WHERE doc_type = 'primary' AND section = 'unsegmented'
            )
            """,
        )
    )
    return TextCoverage(
        built=True,
        item_202_filings=item_filings,
        item_202_without_text=missing_total,
        issuers_without_text=[
            MissingTextIssuer(cik=row[0], ticker=row[1], filings=int(row[2]))
            for row in missing_rows
        ],
        primary_documents=primary_documents,
        unsegmented_primaries=unsegmented_primaries,
        zero_length_sections=int(
            _scalar(
                con,
                """
                SELECT count(*)
                FROM raw.filings_text
                WHERE char_count = 0 OR length(text) = 0
                """,
            )
        ),
        duplicate_sha256_accessions=int(
            _scalar(
                con,
                """
                SELECT count(*)
                FROM (
                    SELECT sha256
                    FROM raw.filings_text
                    GROUP BY sha256
                    HAVING count(DISTINCT accession) > 1
                )
                """,
            )
        ),
    )


def _key_violations(con: duckdb.DuckDBPyConnection) -> list[KeyViolation]:
    """Duplicate rows per contract key; tables a database predates are skipped."""
    violations: list[KeyViolation] = []
    for table, key in KEY_CHECKS:
        schema, _, name = table.partition(".")
        if not _table_exists(con, schema, name):
            continue
        duplicates = int(
            _scalar(
                con,
                f"SELECT count(*) FROM ("
                f"SELECT {key} FROM {table} GROUP BY {key} HAVING count(*) > 1"
                f")",
            )
        )
        if duplicates:
            violations.append(KeyViolation(table=table, key=key, duplicates=duplicates))
    return violations


def run_qa(con: duckdb.DuckDBPyConnection) -> QaReport:
    """Run every data-quality check and return the report.

    ``con`` is a DuckDB connection to a built database; its session timezone is
    set to UTC (decision A-5) and the connection is left open. The report does
    not modify the database. Raises :class:`duckdb.Error` when a required table
    is missing or a query fails.
    """
    con.execute("SET TimeZone='UTC'")
    canonical_fields = int(_scalar(con, "SELECT count(DISTINCT field) FROM raw.concept_map"))
    coverage, universe_issuers = _coverage(con)
    covered = {issuer.cik for issuer in coverage}
    universe = con.execute(
        "SELECT cik, ticker, name FROM raw.universe ORDER BY cik, ticker"
    ).fetchall()
    issuers_without_rows = [
        UniverseIssuer(cik=row[0], ticker=row[1], name=row[2])
        for row in universe
        if row[0] not in covered
    ]
    restatement_counts, top_restatements = _restatements(con)
    imputed, disagreement_count, disagreements = _timestamps(con)
    headers_without_acceptance = _headers_without_acceptance(con)
    multi_currency, unexpected_units = _unit_anomalies(con)
    ratio_gap_counts, ratio_gaps = _ratio_gaps(con)
    fx_coverage = _fx_coverage(con)
    usd_conversion = _usd_conversion(con)
    text_coverage = _text_coverage(con)
    return QaReport(
        canonical_fields=canonical_fields,
        universe_issuers=universe_issuers,
        coverage=coverage,
        issuers_without_rows=issuers_without_rows,
        restatement_counts=restatement_counts,
        top_restatements=top_restatements,
        imputed_by_source=imputed,
        disagreement_count=disagreement_count,
        disagreements=disagreements,
        headers_without_acceptance=headers_without_acceptance,
        legacy_events=_legacy_events(con),
        filings_by_source=_filings_by(con, "source"),
        filings_by_acceptance_source=_filings_by(con, "acceptance_ts_source"),
        unmapped_concepts=_unmapped(con),
        multi_currency=multi_currency,
        unexpected_units=unexpected_units,
        ratio_gap_counts=ratio_gap_counts,
        ratio_gaps=ratio_gaps,
        conflicting_values=_conflicts(con),
        fx_coverage=fx_coverage,
        usd_conversion=usd_conversion,
        text_coverage=text_coverage,
        key_violations=_key_violations(con),
    )


def format_phase2(report: QaReport) -> list[str]:
    """Render the Phase 2 QA sections as CLI output lines.

    ``report`` is a :class:`QaReport`; the returned strings follow the layout of
    the existing ``edgarpit qa`` output (a section title, then two-space
    indented entries). A section whose table does not exist renders as
    ``not built``; no query is made while formatting, so the function is pure.
    Timestamps render as ISO-8601 UTC, dates as ISO-8601 and the unsegmented
    share as a percentage.
    """
    lines: list[str] = ["FX daily (raw.fx_daily)"]
    fx = report.fx_coverage
    if not fx.built:
        lines.append("  not built")
    elif not fx.by_series:
        lines.append("  no observations")
        lines.append(
            f"  latest acceptance_ts: {_iso_ts(fx.latest_acceptance_ts)}"
            if fx.latest_acceptance_ts is not None
            else "  latest acceptance_ts: none"
        )
        lines.append("  stale: yes" if fx.stale else "  stale: no")
    else:
        for series in fx.by_series:
            lines.append(
                f"  {series.series}: {series.rows} rows, "
                f"{_iso_date(series.first_date)}..{_iso_date(series.last_date)}"
            )
            if series.largest_gap_calendar_days is None:
                lines.append("  largest gap: none")
            else:
                assert series.largest_gap_business_days is not None
                lines.append(
                    f"  largest gap: {_days(series.largest_gap_calendar_days)} calendar, "
                    f"{_days(series.largest_gap_business_days)} business"
                )
            lines.append(
                f"  latest acceptance_ts: {_iso_ts(series.latest_acceptance_ts)}"
                if series.latest_acceptance_ts is not None
                else "  latest acceptance_ts: none"
            )
            lines.append(
                f"  business days since last rate: {series.business_days_since_last}"
                if series.business_days_since_last is not None
                else "  business days since last rate: -"
            )
            lines.append("  stale: yes" if series.stale else "  stale: no")

    lines.append("USD conversion (main.fundamentals_pit_usd)")
    usd = report.usd_conversion
    if not usd.built:
        lines.append("  not built")
    else:
        if not usd.counts:
            lines.append("  no rows")
        for counted in usd.counts:
            currency = counted.currency or "-"
            if counted.converted:
                lines.append(f"  converted {currency}: {counted.count}")
            else:
                lines.append(
                    f"  unconverted {counted.unconverted_reason or 'unknown'} "
                    f"{currency}: {counted.count}"
                )
        lines.append(
            f"No-rate rows after 2017-01-03 (must be zero while FX is current): {usd.no_rate_rows}"
        )
        for issuer in usd.no_rate_issuers:
            lines.append(f"  {issuer.cik} {issuer.ticker or '-'}: {issuer.rows} rows")
        lines.append(
            "Asof_ts violations (asof_ts earlier than acceptance_ts or fx_known_at): "
            f"{usd.asof_violation_count}"
        )
        for violation in usd.asof_violations:
            before: list[str] = []
            if violation.asof_ts < violation.acceptance_ts:
                before.append(f"acceptance_ts {_iso_ts(violation.acceptance_ts)}")
            if violation.fx_known_at is not None and violation.asof_ts < violation.fx_known_at:
                before.append(f"fx_known_at {_iso_ts(violation.fx_known_at)}")
            lines.append(
                f"  {violation.cik} {violation.field} "
                f"{_period_label(violation.period_start, violation.period_end)} "
                f"{violation.accession}: asof_ts {_iso_ts(violation.asof_ts)} "
                f"before {', '.join(before)}"
            )

    lines.append("8-K text (raw.filings_text)")
    text = report.text_coverage
    if not text.built:
        lines.append("  not built")
    else:
        lines.append(
            f"  Item 2.02 filings: {text.item_202_filings}, "
            f"missing text: {text.item_202_without_text}"
        )
        for missing in text.issuers_without_text:
            lines.append(f"  {missing.cik} {missing.ticker or '-'}: {missing.filings} filings")
        share = text.unsegmented_share
        share_label = "-" if share is None else f"{share:.1%}"
        lines.append(
            f"  primary documents: {text.primary_documents}, "
            f"unsegmented: {text.unsegmented_primaries} ({share_label})"
        )
        lines.append(f"  zero-length sections: {text.zero_length_sections}")
        lines.append(f"  duplicate sha256 across accessions: {text.duplicate_sha256_accessions}")
    return lines
