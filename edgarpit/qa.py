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
  60 s (count and worst ten);
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
- key uniqueness of every contract table (and ``raw.filings``); any violation
  makes :attr:`QaReport.ok` false. The report is read-only: nothing is written.

Timestamps are UTC; the connection's session timezone is set to UTC (decision
A-5). A missing table is a query error, not a report entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import duckdb

# (table, key columns) checked for uniqueness; key columns are trusted
# identifiers from the frozen contracts, never user input.
KEY_CHECKS: tuple[tuple[str, str], ...] = (
    ("main.fundamentals_pit", "cik, field, period_start, period_end, accession"),
    ("main.ratios_pit", "cik, ratio, period_start, period_end, asof_ts"),
    ("main.events_8k", "accession, item_code"),
    ("main.filings", "accession"),
    ("raw.filings", "accession"),
)

_ANOMALY_LIMIT = 10

# New York date the 8-K item codes were expanded; earlier codes are unreliable.
LEGACY_ITEMS_BEFORE = "2004-08-23"

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


@dataclass
class QaReport:
    """Everything :func:`run_qa` found; ``ok`` is false on any key violation."""

    canonical_fields: int = 0
    universe_issuers: int = 0
    coverage: list[IssuerCoverage] = field(default_factory=list)
    issuers_without_rows: list[UniverseIssuer] = field(default_factory=list)
    restatement_counts: list[RestatementCount] = field(default_factory=list)
    top_restatements: list[Restatement] = field(default_factory=list)
    imputed_by_source: dict[str, int] = field(default_factory=dict)
    disagreement_count: int = 0
    disagreements: list[AcceptanceDisagreement] = field(default_factory=list)
    legacy_events: list[LegacyEventCount] = field(default_factory=list)
    filings_by_source: list[FilingCount] = field(default_factory=list)
    filings_by_acceptance_source: list[FilingCount] = field(default_factory=list)
    unmapped_concepts: list[UnmappedConcept] = field(default_factory=list)
    multi_currency: list[MultiCurrency] = field(default_factory=list)
    unexpected_units: list[UnexpectedUnit] = field(default_factory=list)
    ratio_gap_counts: dict[str, int] = field(default_factory=dict)
    ratio_gaps: list[RatioGap] = field(default_factory=list)
    conflicting_values: list[ConflictingValue] = field(default_factory=list)
    key_violations: list[KeyViolation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every contract table key is unique."""
        return not self.key_violations


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
              AND abs(epoch(acceptance_ts) - epoch(acceptance_ts_api)) > 60
            ORDER BY seconds DESC, accession
            LIMIT {_ANOMALY_LIMIT}
            """
        ).fetchall()
    ]
    return imputed, disagreement_count, worst


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


def _key_violations(con: duckdb.DuckDBPyConnection) -> list[KeyViolation]:
    violations: list[KeyViolation] = []
    for table, key in KEY_CHECKS:
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
    multi_currency, unexpected_units = _unit_anomalies(con)
    ratio_gap_counts, ratio_gaps = _ratio_gaps(con)
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
        legacy_events=_legacy_events(con),
        filings_by_source=_filings_by(con, "source"),
        filings_by_acceptance_source=_filings_by(con, "acceptance_ts_source"),
        unmapped_concepts=_unmapped(con),
        multi_currency=multi_currency,
        unexpected_units=unexpected_units,
        ratio_gap_counts=ratio_gap_counts,
        ratio_gaps=ratio_gaps,
        conflicting_values=_conflicts(con),
        key_violations=_key_violations(con),
    )
