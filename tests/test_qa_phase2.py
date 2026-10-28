"""Tests for the Phase 2 data-quality sections (decisions A-32, A-38, A-39, A-40).

Every section is exercised on a clean database and on a seeded defect, and a
v1.0.0 database without the Phase 2 tables reports each section as "not built"
instead of raising. The databases here carry the architecture section 6/7
columns plus ``main.fundamentals_pit_usd.unconverted_reason`` (decision A-38);
the test doubles under ``tests/doubles.py`` are not used so the report is proven
against the contract rather than against a builder.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from edgarpit.qa import (
    AsOfViolation,
    ConversionCount,
    FxSeriesCoverage,
    KeyViolation,
    MissingTextIssuer,
    NoRateIssuer,
    QaReport,
    TextCoverage,
    format_phase2,
    run_qa,
)

TORONTO = ZoneInfo("America/Toronto")
AAPL_CIK = "0000320193"
RY_CIK = "0001000275"
A1 = "0000320193-26-000001"
A2 = "0000320193-26-000002"
A3 = "0000320193-26-000003"
RY_8K = "0001000275-26-000001"
REFERENCE_ACCEPTANCE = datetime(2026, 10, 28, 20, 5, tzinfo=UTC)
DEFAULT_ACCEPTANCE = datetime(2023, 11, 3, 3, 59, 59, tzinfo=UTC)

_RAW_UNIVERSE_DDL = """
CREATE TABLE raw.universe (
    ticker VARCHAR, cik VARCHAR(10), name VARCHAR, country VARCHAR, filer_type VARCHAR
)
"""

_RAW_CONCEPT_MAP_DDL = """
CREATE TABLE raw.concept_map (
    field VARCHAR, taxonomy VARCHAR, concept VARCHAR, concept_rank INTEGER,
    kind VARCHAR, unit_family VARCHAR
)
"""

_RAW_FILINGS_DDL = """
CREATE TABLE raw.filings (
    cik VARCHAR(10), accession VARCHAR, form VARCHAR, filing_date DATE, report_date DATE,
    acceptance_ts TIMESTAMPTZ, acceptance_ts_source VARCHAR, acceptance_ts_api TIMESTAMPTZ,
    ts_imputed BOOLEAN, primary_document VARCHAR, items VARCHAR, is_amendment BOOLEAN,
    is_xbrl BOOLEAN, source VARCHAR
)
"""

_RAW_EVENTS_DDL = """
CREATE TABLE raw.events_8k (
    cik VARCHAR(10), accession VARCHAR, form VARCHAR, acceptance_ts TIMESTAMPTZ,
    ts_imputed BOOLEAN, item_code VARCHAR, item_label VARCHAR
)
"""

_RAW_FACTS_DDL = """
CREATE TABLE raw.facts_raw (
    cik VARCHAR(10), taxonomy VARCHAR, concept VARCHAR, unit VARCHAR, value DOUBLE,
    period_start DATE, period_end DATE, fy INTEGER, fp VARCHAR, form VARCHAR,
    accession VARCHAR, filed DATE, frame VARCHAR
)
"""

_MAIN_FUNDAMENTALS_DDL = """
CREATE TABLE main.fundamentals_pit (
    cik VARCHAR, ticker VARCHAR, field VARCHAR,
    period_start DATE, period_end DATE, period_type VARCHAR,
    accession VARCHAR, form VARCHAR, acceptance_ts TIMESTAMPTZ, ts_imputed BOOLEAN,
    value DOUBLE, unit VARCHAR, currency VARCHAR,
    source_taxonomy VARCHAR, source_concept VARCHAR, concept_rank INTEGER,
    version INTEGER, first_value DOUBLE, is_restatement BOOLEAN,
    pct_change_vs_first DOUBLE, restatement_kind VARCHAR
)
"""

_MAIN_RATIOS_DDL = """
CREATE TABLE main.ratios_pit (
    cik VARCHAR, ratio VARCHAR, period_start DATE, period_end DATE, period_type VARCHAR,
    asof_ts TIMESTAMPTZ, value DOUBLE, currency VARCHAR,
    numerator_field VARCHAR, numerator_accession VARCHAR, numerator_acceptance_ts TIMESTAMPTZ,
    denominator_field VARCHAR, denominator_accession VARCHAR,
    denominator_acceptance_ts TIMESTAMPTZ
)
"""

_MAIN_EVENTS_DDL = """
CREATE TABLE main.events_8k (
    cik VARCHAR, ticker VARCHAR, accession VARCHAR, form VARCHAR,
    acceptance_ts TIMESTAMPTZ, ts_imputed BOOLEAN, item_code VARCHAR, item_label VARCHAR
)
"""

_RAW_FILINGS_TEXT_DDL = """
CREATE TABLE raw.filings_text (
    cik VARCHAR(10), accession VARCHAR, acceptance_ts TIMESTAMPTZ, ts_imputed BOOLEAN,
    document VARCHAR, doc_type VARCHAR, section VARCHAR, section_order INTEGER,
    text VARCHAR, sha256 VARCHAR, char_count INTEGER
)
"""

_RAW_FX_DAILY_DDL = """
CREATE TABLE raw.fx_daily (
    series VARCHAR, rate_date DATE, value DOUBLE, known_at TIMESTAMPTZ, source VARCHAR
)
"""

_USD_COLUMNS = [
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
    "unconverted_reason",
    "asof_ts",
]


def _usd_ddl(*, reason_column: bool) -> str:
    reason = "unconverted_reason VARCHAR," if reason_column else ""
    return f"""
    CREATE TABLE main.fundamentals_pit_usd (
        cik VARCHAR(10), ticker VARCHAR, field VARCHAR,
        period_start DATE, period_end DATE, period_type VARCHAR,
        accession VARCHAR, form VARCHAR, acceptance_ts TIMESTAMPTZ, ts_imputed BOOLEAN,
        value DOUBLE, unit VARCHAR, currency VARCHAR,
        source_taxonomy VARCHAR, source_concept VARCHAR, concept_rank INTEGER,
        version INTEGER, first_value DOUBLE, is_restatement BOOLEAN,
        pct_change_vs_first DOUBLE, restatement_kind VARCHAR,
        value_usd DOUBLE, fx_rate DOUBLE, fx_rate_date DATE, fx_known_at TIMESTAMPTZ,
        converted BOOLEAN, {reason} asof_ts TIMESTAMPTZ
    )
    """


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def fx_known_at(rate_date: date) -> datetime:
    """17:00 America/Toronto on ``rate_date`` in UTC (decision A-32)."""
    return datetime.combine(rate_date, time(17), tzinfo=TORONTO).astimezone(UTC)


def build_db(path: Path, *, phase2: bool = True, reason_column: bool = True) -> Path:
    """Create a database with the Phase 1 contract and, optionally, Phase 2 tables.

    The Phase 1 tables are empty except for the universe and one filing per
    accession used by the tests; a test inserts the rows its section needs.
    """
    con = duckdb.connect(str(path))
    try:
        con.execute("SET TimeZone='UTC'")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute("CREATE SCHEMA IF NOT EXISTS main")
        for ddl in (
            _RAW_UNIVERSE_DDL,
            _RAW_CONCEPT_MAP_DDL,
            _RAW_FILINGS_DDL,
            _RAW_EVENTS_DDL,
            _RAW_FACTS_DDL,
            _MAIN_FUNDAMENTALS_DDL,
            _MAIN_RATIOS_DDL,
            _MAIN_EVENTS_DDL,
        ):
            con.execute(ddl)
        con.execute(
            "CREATE TABLE main.filings AS "
            "SELECT *, CAST(NULL AS VARCHAR) AS ticker FROM raw.filings WHERE 1 = 0"
        )
        con.execute(
            "INSERT INTO raw.universe VALUES "
            "('AAPL', ?, 'Apple Inc.', 'US', '10-K'), "
            "('RY', ?, 'ROYAL BANK OF CANADA', 'CA', '40-F')",
            [AAPL_CIK, RY_CIK],
        )
        filings = [
            (
                AAPL_CIK,
                A1,
                "8-K",
                date(2026, 10, 28),
                None,
                REFERENCE_ACCEPTANCE,
                "sgml_header",
                None,
                False,
                "aapl-8k.htm",
                "2.02",
                False,
                True,
                "recent",
            ),
            (
                AAPL_CIK,
                A2,
                "8-K",
                date(2026, 10, 28),
                None,
                REFERENCE_ACCEPTANCE,
                "sgml_header",
                None,
                False,
                "aapl-8k2.htm",
                "2.02",
                False,
                True,
                "recent",
            ),
            (
                AAPL_CIK,
                A3,
                "8-K",
                date(2026, 10, 28),
                None,
                REFERENCE_ACCEPTANCE,
                "sgml_header",
                None,
                False,
                "aapl-8k3.htm",
                "2.02",
                False,
                True,
                "recent",
            ),
            (
                RY_CIK,
                RY_8K,
                "8-K",
                date(2026, 10, 28),
                None,
                REFERENCE_ACCEPTANCE,
                "sgml_header",
                None,
                False,
                "ry-8k.htm",
                "2.02",
                False,
                True,
                "recent",
            ),
        ]
        con.executemany(
            "INSERT INTO raw.filings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            filings,
        )
        con.execute(
            "INSERT INTO main.filings SELECT *, "
            "CASE cik WHEN ? THEN 'AAPL' ELSE 'RY' END FROM raw.filings",
            [AAPL_CIK],
        )
        if phase2:
            con.execute(_RAW_FILINGS_TEXT_DDL)
            con.execute(_RAW_FX_DAILY_DDL)
            con.execute(_usd_ddl(reason_column=reason_column))
    finally:
        con.close()
    return path


def connect(db: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db), read_only=True)


def report_of(db: Path) -> QaReport:
    """Run the QA report against ``db`` and close the connection."""
    con = connect(db)
    try:
        return run_qa(con)
    finally:
        con.close()


def insert(db: Path, sql: str, rows: list[tuple[object, ...]]) -> None:
    """Insert ``rows`` through ``sql``, an INSERT statement with placeholders."""
    con = duckdb.connect(str(db))
    try:
        con.executemany(sql, rows)
    finally:
        con.close()


def text_row(
    accession: str,
    document: str,
    section: str,
    text: str = "Item 2.02 results of operations.",
    *,
    cik: str = AAPL_CIK,
    doc_type: str = "primary",
    section_order: int = 1,
    acceptance_ts: datetime = REFERENCE_ACCEPTANCE,
) -> tuple[object, ...]:
    return (
        cik,
        accession,
        acceptance_ts,
        False,
        document,
        doc_type,
        section,
        section_order,
        text,
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        len(text),
    )


def insert_text(db: Path, rows: list[tuple[object, ...]]) -> None:
    insert(
        db,
        "INSERT INTO raw.filings_text VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def event(
    accession: str, cik: str = AAPL_CIK, ticker: str = "AAPL", item_code: str = "2.02"
) -> tuple[object, ...]:
    return (
        cik,
        ticker,
        accession,
        "8-K",
        REFERENCE_ACCEPTANCE,
        False,
        item_code,
        "Results of Operations",
    )


def insert_events(db: Path, rows: list[tuple[object, ...]]) -> None:
    """Insert events into both ``raw.events_8k`` and ``main.events_8k``.

    The report reads item 2.02 coverage from the raw table, while the Phase 1
    sections read the main table; a consistent fixture writes both.
    """
    insert(
        db,
        "INSERT INTO raw.events_8k VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(row[0], row[2], row[3], row[4], row[5], row[6], row[7]) for row in rows],
    )
    insert(
        db,
        "INSERT INTO main.events_8k VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def fx_row(rate_date: date, value: float = 1.35, series: str = "FXUSDCAD") -> tuple[object, ...]:
    return (series, rate_date, value, fx_known_at(rate_date), "valet")


def insert_fx(db: Path, rows: list[tuple[object, ...]]) -> None:
    insert(
        db,
        "INSERT INTO raw.fx_daily VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def usd_values(
    *,
    cik: str = AAPL_CIK,
    ticker: str = "AAPL",
    field: str = "revenue",
    period_start: date | None = None,
    period_end: date = date(2023, 9, 30),
    accession: str = A1,
    acceptance_ts: datetime = DEFAULT_ACCEPTANCE,
    value: float = 100.0,
    unit: str = "USD",
    currency: str = "USD",
    value_usd: float | None = 100.0,
    fx_rate: float | None = None,
    fx_rate_date: date | None = None,
    known_at: datetime | None = None,
    converted: bool = True,
    unconverted_reason: str | None = None,
    asof_ts: datetime | None = None,
    with_reason: bool = True,
) -> dict[str, object]:
    """One ``main.fundamentals_pit_usd`` row keyed by column name."""
    row: dict[str, object] = {
        "cik": cik,
        "ticker": ticker,
        "field": field,
        "period_start": period_start,
        "period_end": period_end,
        "period_type": "FY",
        "accession": accession,
        "form": "10-K",
        "acceptance_ts": acceptance_ts,
        "ts_imputed": False,
        "value": value,
        "unit": unit,
        "currency": currency,
        "source_taxonomy": "us-gaap",
        "source_concept": "Revenues",
        "concept_rank": 1,
        "version": 1,
        "first_value": value,
        "is_restatement": False,
        "pct_change_vs_first": None,
        "restatement_kind": "none",
        "value_usd": value_usd,
        "fx_rate": fx_rate,
        "fx_rate_date": fx_rate_date,
        "fx_known_at": known_at,
        "converted": converted,
        "unconverted_reason": unconverted_reason,
        "asof_ts": acceptance_ts if asof_ts is None else asof_ts,
    }
    if not with_reason:
        row.pop("unconverted_reason")
    return row


def insert_usd(db: Path, rows: list[dict[str, object]], *, with_reason: bool = True) -> None:
    columns = [column for column in _USD_COLUMNS if with_reason or column != "unconverted_reason"]
    placeholders = ", ".join("?" * len(columns))
    insert(
        db,
        f"INSERT INTO main.fundamentals_pit_usd ({', '.join(columns)}) VALUES ({placeholders})",
        [tuple(row[column] for column in columns) for row in rows],
    )


def test_v1_database_reports_phase2_sections_not_built(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb", phase2=False)
    report = report_of(db)

    assert report.fx_coverage.built is False
    assert report.usd_conversion.built is False
    assert report.text_coverage.built is False
    assert report.key_violations == []
    assert report.ok
    assert format_phase2(report) == [
        "FX daily (raw.fx_daily)",
        "  not built",
        "USD conversion (main.fundamentals_pit_usd)",
        "  not built",
        "8-K text (raw.filings_text)",
        "  not built",
    ]


def test_fx_clean_series_has_no_gap_or_staleness(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    insert_fx(
        db,
        [
            fx_row(date(2026, 10, 26)),
            fx_row(date(2026, 10, 27), 1.36),
            fx_row(date(2026, 10, 28), 1.37),
        ],
    )
    report = report_of(db)

    assert report.fx_coverage.built
    assert report.fx_coverage.latest_acceptance_ts == REFERENCE_ACCEPTANCE
    assert report.fx_coverage.by_series == [
        FxSeriesCoverage(
            series="FXUSDCAD",
            rows=3,
            first_date=date(2026, 10, 26),
            last_date=date(2026, 10, 28),
            largest_gap_calendar_days=1,
            largest_gap_business_days=1,
            latest_acceptance_ts=REFERENCE_ACCEPTANCE,
            business_days_since_last=0,
            stale=False,
        )
    ]
    assert report.fx_coverage.stale is False
    lines = format_phase2(report)
    assert "  FXUSDCAD: 3 rows, 2026-10-26..2026-10-28" in lines
    assert "  largest gap: 1 day calendar, 1 day business" in lines
    assert "  stale: no" in lines


def test_fx_gap_and_staleness_fire_on_seeded_defect(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    insert_fx(db, [fx_row(date(2026, 10, 1)), fx_row(date(2026, 10, 20))])
    report = report_of(db)

    series = report.fx_coverage.by_series[0]
    assert series.largest_gap_calendar_days == 19
    assert series.largest_gap_business_days == 13
    assert series.business_days_since_last == 6
    assert series.stale is True
    assert report.fx_coverage.stale is True
    assert "  largest gap: 19 days calendar, 13 days business" in format_phase2(report)


def test_fx_five_business_days_old_is_not_stale(tmp_path: Path) -> None:
    """The boundary is "older than five": five business days is still current."""
    db = build_db(tmp_path / "edgarpit.duckdb")
    insert_fx(db, [fx_row(date(2026, 10, 21))])
    report = report_of(db)

    series = report.fx_coverage.by_series[0]
    assert series.business_days_since_last == 5
    assert series.stale is False


def test_empty_fx_table_is_stale_when_filings_exist(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    report = report_of(db)

    assert report.fx_coverage.built
    assert report.fx_coverage.by_series == []
    assert report.fx_coverage.stale is True
    lines = format_phase2(report)
    assert "  no observations" in lines
    assert "  stale: yes" in lines


def test_usd_clean_conversion_counts_and_no_violations(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    insert_usd(
        db,
        [
            usd_values(),
            usd_values(
                period_end=date(2023, 10, 31),
                acceptance_ts=utc(2023, 11, 30, 12),
                value=135.0,
                unit="CAD",
                currency="CAD",
                value_usd=100.0,
                fx_rate=1.35,
                fx_rate_date=date(2023, 10, 31),
                known_at=fx_known_at(date(2023, 10, 31)),
                asof_ts=utc(2023, 11, 30, 12),
            ),
            usd_values(
                period_end=date(2016, 12, 31),
                acceptance_ts=utc(2017, 2, 1),
                unit="CAD",
                currency="CAD",
                value_usd=None,
                converted=False,
                unconverted_reason="no_rate",
            ),
            usd_values(
                period_end=date(2023, 9, 30),
                acceptance_ts=utc(2023, 11, 3),
                value=5.0,
                unit="EUR",
                currency="EUR",
                value_usd=None,
                converted=False,
                unconverted_reason="currency_out_of_scope",
            ),
        ],
    )
    report = report_of(db)

    assert report.usd_conversion.built
    assert report.usd_conversion.counts == [
        ConversionCount(converted=True, unconverted_reason=None, currency="CAD", count=1),
        ConversionCount(converted=True, unconverted_reason=None, currency="USD", count=1),
        ConversionCount(
            converted=False, unconverted_reason="currency_out_of_scope", currency="EUR", count=1
        ),
        ConversionCount(converted=False, unconverted_reason="no_rate", currency="CAD", count=1),
    ]
    # A no_rate row for a period before the first observation is legitimate.
    assert report.usd_conversion.no_rate_rows == 0
    assert report.usd_conversion.no_rate_issuers == []
    assert report.usd_conversion.asof_violation_count == 0
    assert report.ok


def test_usd_no_rate_after_first_observation_is_reported(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    insert_usd(
        db,
        [
            usd_values(
                period_end=date(2020, 10, 31),
                acceptance_ts=utc(2020, 12, 1),
                unit="CAD",
                currency="CAD",
                value_usd=None,
                converted=False,
                unconverted_reason="no_rate",
            ),
            usd_values(
                cik=RY_CIK,
                ticker="RY",
                accession=RY_8K,
                period_end=date(2021, 10, 31),
                acceptance_ts=utc(2021, 12, 1),
                unit="CAD",
                currency="CAD",
                value_usd=None,
                converted=False,
                unconverted_reason="no_rate",
            ),
            usd_values(
                period_end=date(2016, 12, 31),
                acceptance_ts=utc(2017, 2, 1),
                unit="CAD",
                currency="CAD",
                value_usd=None,
                converted=False,
                unconverted_reason="no_rate",
            ),
            # The first observation date itself is convertible, so a no_rate row
            # for it is a defect (decision A-39 boundary).
            usd_values(
                period_end=date(2017, 1, 3),
                acceptance_ts=utc(2017, 2, 1),
                unit="CAD",
                currency="CAD",
                value_usd=None,
                converted=False,
                unconverted_reason="no_rate",
            ),
        ],
    )
    report = report_of(db)

    assert report.usd_conversion.no_rate_rows == 3
    assert report.usd_conversion.no_rate_issuers == [
        NoRateIssuer(cik=AAPL_CIK, ticker="AAPL", rows=2),
        NoRateIssuer(cik=RY_CIK, ticker="RY", rows=1),
    ]
    lines = format_phase2(report)
    assert "No-rate rows after 2017-01-03 (must be zero while FX is current): 3" in lines


def test_usd_asof_violations_fire_and_make_report_not_ok(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    acceptance = utc(2026, 1, 5, 12)
    known = fx_known_at(date(2026, 1, 6))
    insert_usd(
        db,
        [
            # asof_ts equals acceptance_ts but precedes the FX known time.
            usd_values(
                accession="0000320193-26-000010",
                period_end=date(2025, 9, 30),
                acceptance_ts=acceptance,
                unit="CAD",
                currency="CAD",
                value_usd=100.0,
                fx_rate=1.35,
                fx_rate_date=date(2026, 1, 6),
                known_at=known,
                asof_ts=acceptance,
            ),
            # asof_ts follows the FX known time but precedes acceptance_ts.
            usd_values(
                accession="0000320193-26-000011",
                field="total_assets",
                period_end=date(2025, 12, 31),
                acceptance_ts=utc(2026, 2, 5, 12),
                unit="CAD",
                currency="CAD",
                value_usd=200.0,
                fx_rate=1.35,
                fx_rate_date=date(2026, 1, 6),
                known_at=known,
                asof_ts=utc(2026, 2, 4, 12),
            ),
        ],
    )
    report = report_of(db)

    assert report.usd_conversion.asof_violation_count == 2
    assert report.usd_conversion.asof_violations == [
        AsOfViolation(
            cik=AAPL_CIK,
            field="revenue",
            period_start=None,
            period_end=date(2025, 9, 30),
            accession="0000320193-26-000010",
            acceptance_ts=acceptance,
            fx_known_at=known,
            asof_ts=acceptance,
        ),
        AsOfViolation(
            cik=AAPL_CIK,
            field="total_assets",
            period_start=None,
            period_end=date(2025, 12, 31),
            accession="0000320193-26-000011",
            acceptance_ts=utc(2026, 2, 5, 12),
            fx_known_at=known,
            asof_ts=utc(2026, 2, 4, 12),
        ),
    ]
    assert not report.ok
    lines = format_phase2(report)
    assert "Asof_ts violations (asof_ts earlier than acceptance_ts or fx_known_at): 2" in lines
    assert any("before fx_known_at 2026-01-06T22:00:00Z" in line for line in lines)
    assert any("before acceptance_ts 2026-02-05T12:00:00Z" in line for line in lines)


def test_usd_table_without_reason_column_is_tolerated(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb", reason_column=False)
    insert_usd(
        db,
        [
            usd_values(
                unit="CAD",
                currency="CAD",
                acceptance_ts=utc(2016, 1, 4),
                period_end=date(2015, 12, 31),
                value_usd=None,
                converted=False,
                with_reason=False,
            )
        ],
        with_reason=False,
    )
    report = report_of(db)

    assert report.usd_conversion.built
    assert report.usd_conversion.counts == [
        ConversionCount(converted=False, unconverted_reason=None, currency="CAD", count=1)
    ]
    assert report.usd_conversion.no_rate_issuers == []
    assert "  unconverted unknown CAD: 1" in format_phase2(report)


def test_text_clean_coverage_has_no_findings(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    insert_events(db, [event(A1), event(A2)])
    insert_text(
        db,
        [
            text_row(A1, "aapl-8k.htm", "2.02", "Item 2.02 results."),
            text_row(A2, "aapl-8k2.htm", "2.02", "Item 2.02 other results."),
            text_row(
                A2,
                "aapl-ex991.htm",
                "unsegmented",
                "Exhibit 99.1 press release.",
                doc_type="EX-99.1",
            ),
        ],
    )
    report = report_of(db)

    assert report.text_coverage == TextCoverage(
        built=True,
        item_202_filings=2,
        item_202_without_text=0,
        issuers_without_text=[],
        primary_documents=2,
        unsegmented_primaries=0,
        zero_length_sections=0,
        duplicate_sha256_accessions=0,
    )
    assert report.text_coverage.unsegmented_share == 0.0
    lines = format_phase2(report)
    assert "  Item 2.02 filings: 2, missing text: 0" in lines
    assert "  primary documents: 2, unsegmented: 0 (0.0%)" in lines


def test_text_missing_item_202_text_lists_top_issuers(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    insert_events(db, [event(A1), event(A2), event(RY_8K, RY_CIK, "RY")])
    insert_text(db, [text_row(A1, "aapl-8k.htm", "2.02")])
    report = report_of(db)

    assert report.text_coverage.item_202_filings == 3
    assert report.text_coverage.item_202_without_text == 2
    assert report.text_coverage.issuers_without_text == [
        MissingTextIssuer(cik=AAPL_CIK, ticker="AAPL", filings=1),
        MissingTextIssuer(cik=RY_CIK, ticker="RY", filings=1),
    ]
    lines = format_phase2(report)
    assert "  Item 2.02 filings: 3, missing text: 2" in lines
    assert f"  {AAPL_CIK} AAPL: 1 filings" in lines


def test_text_unsegmented_share_and_zero_length_fire(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    insert_events(db, [event(A1), event(A2)])
    insert_text(
        db,
        [
            text_row(A1, "aapl-8k.htm", "unsegmented", "Item 2.02 with an odd layout."),
            text_row(A2, "aapl-8k2.htm", "2.02", ""),
        ],
    )
    report = report_of(db)

    assert report.text_coverage.primary_documents == 2
    assert report.text_coverage.unsegmented_primaries == 1
    assert report.text_coverage.unsegmented_share == 0.5
    assert report.text_coverage.zero_length_sections == 1
    lines = format_phase2(report)
    assert "  primary documents: 2, unsegmented: 1 (50.0%)" in lines
    assert "  zero-length sections: 1" in lines


def test_text_without_primaries_has_no_unsegmented_share(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    insert_text(
        db,
        [text_row(A1, "aapl-ex991.htm", "unsegmented", "Press release.", doc_type="EX-99.1")],
    )
    report = report_of(db)

    assert report.text_coverage.primary_documents == 0
    assert report.text_coverage.unsegmented_share is None
    assert "  primary documents: 0, unsegmented: 0 (-)" in format_phase2(report)


def test_text_duplicate_hash_across_accessions_is_counted_only(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    shared = "Item 2.02 results of operations."
    insert_text(
        db,
        [
            text_row(A1, "aapl-8k.htm", "2.02", shared),
            text_row(A2, "aapl-8k2.htm", "2.02", shared),
            # The same text in another section of the same accession is not a
            # cross-accession duplicate.
            text_row(A1, "aapl-ex991.htm", "unsegmented", shared, doc_type="EX-99.1"),
        ],
    )
    report = report_of(db)

    assert report.text_coverage.duplicate_sha256_accessions == 1
    assert report.key_violations == []
    assert report.ok
    assert "  duplicate sha256 across accessions: 1" in format_phase2(report)


def test_phase2_key_violations_join_the_report(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    row = text_row(A1, "aapl-8k.htm", "2.02")
    insert_text(db, [row, row])
    insert_fx(db, [fx_row(date(2026, 10, 27)), fx_row(date(2026, 10, 27), 1.36)])
    report = report_of(db)

    assert report.key_violations == [
        KeyViolation(
            table="raw.filings_text",
            key="accession, document, section, section_order",
            duplicates=1,
        ),
        KeyViolation(table="raw.fx_daily", key="series, rate_date", duplicates=1),
    ]
    assert not report.ok
