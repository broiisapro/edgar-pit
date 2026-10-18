"""Tests for the data-quality report (FR-1.10) on a hand-built database.

The database is created with DuckDB DDL that matches the frozen table
contracts; every QA section gets at least one real row, and the key-uniqueness
check is exercised both clean and violated (through the CLI, which must exit
1 on a violation).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
from typer.testing import CliRunner

from edgarpit.cli import app
from edgarpit.qa import (
    FilingCount,
    KeyViolation,
    LegacyEventCount,
    MultiCurrency,
    RatioGap,
    run_qa,
)

RUNNER = CliRunner()
RY_CIK = "0001000275"
APPLE_CIK = "0000320193"
EMPTY_CIK = "0000000001"
RY_40F = "0001193125-23-285639"
RY_RESTATED_40F = "0001193125-24-270294"
APPLE_10Q = "0000320193-23-000106"
APPLE_8K = "0000320193-24-000006"

_MAIN_FUNDAMENTALS_DDL = """
CREATE TABLE main.fundamentals_pit (
    cik VARCHAR, ticker VARCHAR, field VARCHAR,
    period_start DATE, period_end DATE, period_type VARCHAR,
    accession VARCHAR, form VARCHAR, acceptance_ts TIMESTAMPTZ, ts_imputed BOOLEAN,
    value DOUBLE, unit VARCHAR, currency VARCHAR,
    source_taxonomy VARCHAR, source_concept VARCHAR, concept_rank INTEGER,
    version INTEGER, first_value DOUBLE, is_restatement BOOLEAN,
    pct_change_vs_first DOUBLE
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

_RAW_FILINGS_DDL = """
CREATE TABLE raw.filings (
    cik VARCHAR(10), accession VARCHAR, form VARCHAR, filing_date DATE, report_date DATE,
    acceptance_ts TIMESTAMPTZ, acceptance_ts_source VARCHAR, acceptance_ts_api TIMESTAMPTZ,
    ts_imputed BOOLEAN, primary_document VARCHAR, items VARCHAR, is_amendment BOOLEAN,
    is_xbrl BOOLEAN, source VARCHAR
)
"""

_MAIN_FILINGS_DDL = """
CREATE TABLE main.filings AS SELECT *, CAST(NULL AS VARCHAR) AS ticker FROM raw.filings WHERE 1 = 0
"""

_RAW_FACTS_DDL = """
CREATE TABLE raw.facts_raw (
    cik VARCHAR(10), taxonomy VARCHAR, concept VARCHAR, unit VARCHAR, value DOUBLE,
    period_start DATE, period_end DATE, fy INTEGER, fp VARCHAR, form VARCHAR,
    accession VARCHAR, filed DATE, frame VARCHAR
)
"""

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


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


FUNDAMENTALS_ROWS = [
    (
        RY_CIK,
        "RY",
        "revenue",
        date(2021, 11, 1),
        date(2022, 10, 31),
        "FY",
        RY_40F,
        "40-F",
        utc(2022, 12, 1, 11),
        False,
        100.0,
        "USD",
        "USD",
        "ifrs-full",
        "Revenue",
        1,
        1,
        100.0,
        False,
        None,
    ),
    (
        RY_CIK,
        "RY",
        "revenue",
        date(2022, 11, 1),
        date(2023, 10, 31),
        "FY",
        RY_40F,
        "40-F",
        utc(2023, 11, 30, 11, 45, 12),
        False,
        56129000000.0,
        "CAD",
        "CAD",
        "ifrs-full",
        "Revenue",
        1,
        1,
        56129000000.0,
        False,
        None,
    ),
    (
        RY_CIK,
        "RY",
        "revenue",
        date(2022, 11, 1),
        date(2023, 10, 31),
        "FY",
        RY_RESTATED_40F,
        "40-F",
        utc(2024, 12, 3, 21),
        False,
        51464000000.0,
        "CAD",
        "CAD",
        "ifrs-full",
        "Revenue",
        1,
        2,
        56129000000.0,
        True,
        (51464000000.0 - 56129000000.0) / 56129000000.0,
    ),
    (
        RY_CIK,
        "RY",
        "total_assets",
        None,
        date(2023, 10, 31),
        "FY",
        RY_40F,
        "40-F",
        utc(2023, 11, 30, 11, 45, 12),
        False,
        1000000.0,
        "CAD",
        "CAD",
        "ifrs-full",
        "Assets",
        1,
        1,
        1000000.0,
        False,
        None,
    ),
    (
        APPLE_CIK,
        "AAPL",
        "revenue",
        date(2022, 9, 25),
        date(2023, 9, 30),
        "FY",
        APPLE_10Q,
        "10-Q",
        utc(2023, 11, 3, 3, 59, 59),
        True,
        383285000000.0,
        "USD",
        "USD",
        "us-gaap",
        "Revenues",
        1,
        1,
        383285000000.0,
        False,
        None,
    ),
    (
        APPLE_CIK,
        "AAPL",
        "eps_diluted",
        date(2022, 9, 25),
        date(2023, 9, 30),
        "FY",
        APPLE_10Q,
        "10-Q",
        utc(2023, 11, 3, 3, 59, 59),
        True,
        6.13,
        "shares",
        None,
        "us-gaap",
        "EarningsPerShareDiluted",
        1,
        1,
        6.13,
        False,
        None,
    ),
]

RAW_FILINGS_ROWS = [
    (
        RY_CIK,
        RY_40F,
        "40-F",
        date(2023, 11, 30),
        date(2023, 10, 31),
        utc(2023, 11, 30, 11, 45, 12),
        "sgml_header",
        utc(2023, 11, 30, 11, 45, 12),
        False,
        "ry-2023.htm",
        None,
        False,
        True,
        "recent",
    ),
    (
        RY_CIK,
        RY_RESTATED_40F,
        "40-F",
        date(2024, 12, 3),
        date(2024, 10, 31),
        utc(2024, 12, 3, 21),
        "sgml_header",
        utc(2024, 12, 4, 1),
        False,
        "ry-2024.htm",
        None,
        False,
        True,
        "recent",
    ),
    (
        APPLE_CIK,
        APPLE_10Q,
        "10-Q",
        date(2023, 11, 2),
        date(2023, 9, 30),
        utc(2023, 11, 3, 3, 59, 59),
        "filing_date_eod",
        None,
        True,
        "aapl-10q.htm",
        None,
        False,
        True,
        "recent",
    ),
]

EVENTS_ROWS = [
    (
        APPLE_CIK,
        "AAPL",
        APPLE_8K,
        "8-K",
        utc(2024, 2, 1, 21, 30),
        False,
        "2.02",
        "Results of Operations",
    )
]


def build_qa_db(path: Path, *, duplicate_event: bool = False) -> Path:
    con = duckdb.connect(str(path))
    try:
        con.execute("SET TimeZone='UTC'")
        con.execute("CREATE SCHEMA raw")
        for ddl in (
            _MAIN_FUNDAMENTALS_DDL,
            _MAIN_RATIOS_DDL,
            _MAIN_EVENTS_DDL,
            _RAW_FILINGS_DDL,
            _RAW_FACTS_DDL,
            _RAW_UNIVERSE_DDL,
            _RAW_CONCEPT_MAP_DDL,
        ):
            con.execute(ddl)
        con.execute(_MAIN_FILINGS_DDL)
        con.execute(
            "INSERT INTO raw.universe VALUES "
            "('RY', ?, 'ROYAL BANK OF CANADA', 'CA', '40-F'), "
            "('AAPL', ?, 'Apple Inc.', 'US', '10-K'), "
            "('EMPTY', ?, 'No Filings Inc.', 'US', '10-K')",
            [RY_CIK, APPLE_CIK, EMPTY_CIK],
        )
        con.execute(
            "INSERT INTO raw.concept_map VALUES "
            "('revenue', 'us-gaap', 'Revenues', 1, 'duration', 'currency'), "
            "('revenue', 'ifrs-full', 'Revenue', 1, 'duration', 'currency'), "
            "('total_assets', 'us-gaap', 'Assets', 1, 'instant', 'currency'), "
            "('total_assets', 'ifrs-full', 'Assets', 1, 'instant', 'currency'), "
            "('eps_diluted', 'us-gaap', 'EarningsPerShareDiluted', 1, 'duration', "
            "'currency_per_share')"
        )
        con.executemany(
            "INSERT INTO raw.filings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            RAW_FILINGS_ROWS,
        )
        con.execute(
            "INSERT INTO main.filings SELECT *, "
            "CASE cik WHEN ? THEN 'RY' WHEN ? THEN 'AAPL' END FROM raw.filings",
            [RY_CIK, APPLE_CIK],
        )
        con.executemany(
            "INSERT INTO main.fundamentals_pit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?, ?)",
            FUNDAMENTALS_ROWS,
        )
        con.execute(
            "INSERT INTO main.ratios_pit VALUES "
            "(?, 'net_margin', ?, ?, 'FY', ?, 0.1, 'USD', 'net_income', ?, ?, "
            "'revenue', ?, ?)",
            [
                RY_CIK,
                date(2022, 11, 1),
                date(2023, 10, 31),
                utc(2024, 12, 3, 21),
                RY_40F,
                utc(2023, 11, 30, 11, 45, 12),
                RY_40F,
                utc(2023, 11, 30, 11, 45, 12),
            ],
        )
        con.executemany("INSERT INTO main.events_8k VALUES (?, ?, ?, ?, ?, ?, ?, ?)", EVENTS_ROWS)
        if duplicate_event:
            con.executemany(
                "INSERT INTO main.events_8k VALUES (?, ?, ?, ?, ?, ?, ?, ?)", EVENTS_ROWS
            )
        con.executemany(
            "INSERT INTO raw.facts_raw VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    APPLE_CIK,
                    "us-gaap",
                    "Revenues",
                    "USD",
                    1.0,
                    date(2022, 9, 25),
                    date(2023, 9, 30),
                    2023,
                    "FY",
                    "10-K",
                    APPLE_10Q,
                    date(2023, 11, 2),
                    None,
                ),
                (
                    APPLE_CIK,
                    "us-gaap",
                    "Revenues",
                    "USD",
                    2.0,
                    date(2022, 9, 25),
                    date(2023, 9, 30),
                    2023,
                    "FY",
                    "10-K",
                    APPLE_10Q,
                    date(2023, 11, 2),
                    None,
                ),
                (
                    APPLE_CIK,
                    "us-gaap",
                    "IncomeTaxExpenseBenefit",
                    "USD",
                    100.0,
                    date(2022, 9, 25),
                    date(2023, 9, 30),
                    2023,
                    "FY",
                    "10-K",
                    APPLE_10Q,
                    date(2023, 11, 2),
                    None,
                ),
                (
                    APPLE_CIK,
                    "us-gaap",
                    "IncomeTaxExpenseBenefit",
                    "USD",
                    120.0,
                    date(2022, 9, 25),
                    date(2023, 9, 30),
                    2023,
                    "FY",
                    "10-K",
                    APPLE_8K,
                    date(2023, 11, 2),
                    None,
                ),
                (
                    RY_CIK,
                    "ifrs-full",
                    "Revenue",
                    "CAD",
                    56129000000.0,
                    date(2022, 11, 1),
                    date(2023, 10, 31),
                    2023,
                    "FY",
                    "40-F",
                    RY_40F,
                    date(2023, 11, 30),
                    None,
                ),
            ],
        )
    finally:
        con.close()
    return path


def test_run_qa_populates_every_section(tmp_path: Path) -> None:
    db = build_qa_db(tmp_path / "edgarpit.duckdb")
    con = duckdb.connect(str(db), read_only=True)
    try:
        report = run_qa(con)
    finally:
        con.close()

    assert report.canonical_fields == 3
    assert report.universe_issuers == 3

    coverage = {issuer.cik: issuer for issuer in report.coverage}
    assert set(coverage) == {RY_CIK, APPLE_CIK}
    ryan = coverage[RY_CIK]
    assert ryan.ticker == "RY"
    assert ryan.rows == 4
    assert ryan.by_field == {"revenue": [2022, 2023], "total_assets": [2023]}
    assert ryan.fy_years == [2022, 2023]
    apple = coverage[APPLE_CIK]
    assert apple.by_field == {"revenue": [2023], "eps_diluted": [2023]}
    assert [issuer.cik for issuer in report.issuers_without_rows] == [EMPTY_CIK]
    assert report.issuers_without_rows[0].ticker == "EMPTY"

    assert len(report.restatement_counts) == 1
    assert (report.restatement_counts[0].cik, report.restatement_counts[0].field) == (
        RY_CIK,
        "revenue",
    )
    assert report.restatement_counts[0].count == 1
    assert len(report.top_restatements) == 1
    restated = report.top_restatements[0]
    assert restated.first_value == 56129000000.0
    assert restated.value == 51464000000.0
    assert restated.first_acceptance_ts == utc(2023, 11, 30, 11, 45, 12)
    assert restated.acceptance_ts == utc(2024, 12, 3, 21)
    assert restated.period_start == date(2022, 11, 1)
    assert restated.period_end == date(2023, 10, 31)
    assert restated.pct_change_vs_first is not None
    assert restated.pct_change_vs_first < -0.08

    assert report.imputed_by_source == {"filing_date_eod": 1}
    assert report.disagreement_count == 1
    assert len(report.disagreements) == 1
    disagreement = report.disagreements[0]
    assert disagreement.accession == RY_RESTATED_40F
    assert disagreement.acceptance_ts == utc(2024, 12, 3, 21)
    assert disagreement.acceptance_ts_api == utc(2024, 12, 4, 1)
    assert disagreement.seconds == 14400.0

    assert [
        (concept.taxonomy, concept.concept, concept.count) for concept in report.unmapped_concepts
    ] == [("us-gaap", "IncomeTaxExpenseBenefit", 2)]

    assert report.multi_currency == [
        MultiCurrency(cik=RY_CIK, field="revenue", currencies=["CAD", "USD"])
    ]
    assert len(report.unexpected_units) == 1
    unexpected = report.unexpected_units[0]
    assert (unexpected.cik, unexpected.field, unexpected.unit) == (
        APPLE_CIK,
        "eps_diluted",
        "shares",
    )
    assert unexpected.expected_families == ["currency_per_share"]

    assert len(report.conflicting_values) == 1
    conflict = report.conflicting_values[0]
    assert (conflict.cik, conflict.taxonomy, conflict.concept) == (
        APPLE_CIK,
        "us-gaap",
        "Revenues",
    )
    assert conflict.accession == APPLE_10Q
    assert conflict.values == [1.0, 2.0]

    assert report.legacy_events == []
    assert report.filings_by_source == [FilingCount(source="recent", count=3)]
    assert report.filings_by_acceptance_source == [
        FilingCount(source="filing_date_eod", count=1),
        FilingCount(source="sgml_header", count=2),
    ]
    assert report.ratio_gap_counts == {"missing input": 2}
    assert [gap.reason for gap in report.ratio_gaps] == ["missing input", "missing input"]

    assert report.key_violations == []
    assert report.ok


def test_ok_is_false_on_a_duplicated_key(tmp_path: Path) -> None:
    db = build_qa_db(tmp_path / "edgarpit.duckdb", duplicate_event=True)
    con = duckdb.connect(str(db), read_only=True)
    try:
        report = run_qa(con)
    finally:
        con.close()
    assert not report.ok
    assert report.key_violations == [
        KeyViolation(table="main.events_8k", key="accession, item_code", duplicates=1)
    ]


def test_cli_qa_prints_sections_and_exits_0(tmp_path: Path) -> None:
    db = build_qa_db(tmp_path / "edgarpit.duckdb")
    result = RUNNER.invoke(app, ["qa", "--db", str(db)])
    assert result.exit_code == 0, result.output
    for section in (
        "Coverage",
        "Restatements",
        "Imputed timestamps",
        "Header-vs-API disagreements over 60 s: 1",
        "Legacy 8-K events (accepted before 2004-08-23; item codes are unreliable)",
        "Filings by source",
        "Filings by acceptance_ts_source",
        "Unmapped concepts",
        "Unit anomalies",
        "Ratio gaps (input present, no ratios_pit row)",
        "Conflicting values within one accession",
        "Key uniqueness: OK",
    ):
        assert section in result.stdout, section
    assert "  recent: 3" in result.stdout
    assert "  sgml_header: 2" in result.stdout
    assert "  missing input: 2" in result.stdout
    assert "0001193125-24-270294" in result.stdout
    assert "0001000275 revenue" in result.stdout


def insert_rows(db: Path, table: str, rows: list[tuple[object, ...]]) -> None:
    """Append rows to a built database table, for a section-only test."""
    if not rows:  # pragma: no cover - callers always pass rows
        raise ValueError("insert_rows needs at least one row")
    placeholders = ", ".join("?" * len(rows[0]))
    con = duckdb.connect(str(db))
    try:
        con.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
    finally:
        con.close()


def fund(
    field: str,
    period_start: date | None,
    period_end: date,
    period_type: str,
    accession: str,
    acceptance_ts: datetime,
    value: float,
    unit: str = "USD",
) -> tuple[object, ...]:
    """A RY ``fundamentals_pit`` row with the contract columns and a version 1."""
    return (
        RY_CIK,
        "RY",
        field,
        period_start,
        period_end,
        period_type,
        accession,
        "10-K",
        acceptance_ts,
        False,
        value,
        unit,
        unit,
        "us-gaap",
        "Revenues",
        1,
        1,
        value,
        False,
        None,
    )


def test_legacy_events_use_the_new_york_boundary(tmp_path: Path) -> None:
    db = build_qa_db(tmp_path / "edgarpit.duckdb")
    insert_rows(
        db,
        "main.events_8k",
        [
            # 2004-08-22 20:00 New York: before the cutoff, although the UTC date
            # is already the 23rd.
            (
                APPLE_CIK,
                "AAPL",
                "0000320193-04-000001",
                "8-K",
                utc(2004, 8, 23),
                False,
                "5",
                "Other Events",
            ),
            # 2004-08-23 00:00 New York: the first minute of the new item codes.
            (
                APPLE_CIK,
                "AAPL",
                "0000320193-04-000002",
                "8-K",
                utc(2004, 8, 23, 4),
                False,
                "2.02",
                "Results of Operations",
            ),
        ],
    )
    con = duckdb.connect(str(db), read_only=True)
    try:
        report = run_qa(con)
    finally:
        con.close()
    assert report.legacy_events == [LegacyEventCount(cik=APPLE_CIK, ticker="AAPL", count=1)]


def test_filings_are_counted_by_source_and_acceptance_source(tmp_path: Path) -> None:
    db = build_qa_db(tmp_path / "edgarpit.duckdb")
    rows: list[tuple[object, ...]] = [
        (
            APPLE_CIK,
            "0000320193-03-000001",
            "8-K",
            date(2003, 5, 1),
            None,
            utc(2003, 5, 1, 20),
            "sgml_header",
            None,
            False,
            "a8k.htm",
            "5",
            False,
            True,
            "0000320193-submissions-001.json",
        ),
        (
            APPLE_CIK,
            "0000320193-23-000001",
            "8-K",
            date(2023, 12, 1),
            None,
            utc(2023, 12, 1, 21),
            "sgml_header",
            None,
            False,
            "b8k.htm",
            "2.02",
            False,
            True,
            "header",
        ),
    ]
    insert_rows(db, "raw.filings", rows)
    insert_rows(db, "main.filings", [(*row, "AAPL") for row in rows])
    con = duckdb.connect(str(db), read_only=True)
    try:
        report = run_qa(con)
    finally:
        con.close()
    assert report.filings_by_source == [
        FilingCount(source="0000320193-submissions-001.json", count=1),
        FilingCount(source="header", count=1),
        FilingCount(source="recent", count=3),
    ]
    assert report.filings_by_acceptance_source == [
        FilingCount(source="filing_date_eod", count=1),
        FilingCount(source="sgml_header", count=4),
    ]


def test_ratio_gaps_classify_every_reason(tmp_path: Path) -> None:
    db = build_qa_db(tmp_path / "edgarpit.duckdb")
    insert_rows(
        db,
        "main.fundamentals_pit",
        [
            # net_margin FY2020: USD numerator against a CAD denominator.
            fund(
                "net_income",
                date(2019, 11, 1),
                date(2020, 10, 31),
                "FY",
                "0001000275-20-000001",
                utc(2020, 12, 1, 12),
                100.0,
            ),
            fund(
                "revenue",
                date(2019, 11, 1),
                date(2020, 10, 31),
                "FY",
                "0001000275-20-000002",
                utc(2020, 12, 1, 13),
                900.0,
                unit="CAD",
            ),
            # roe FY2021: matching currency, zero equity.
            fund(
                "net_income",
                date(2020, 11, 1),
                date(2021, 10, 31),
                "FY",
                "0001000275-21-000001",
                utc(2021, 12, 1, 12),
                50.0,
            ),
            fund(
                "total_equity",
                None,
                date(2021, 10, 31),
                "FY",
                "0001000275-21-000002",
                utc(2021, 12, 1, 13),
                0.0,
            ),
            # A six-month revenue period is OTHER: the model never considers it.
            fund(
                "revenue",
                date(2023, 11, 1),
                date(2024, 4, 30),
                "OTHER",
                "0001000275-24-000001",
                utc(2024, 5, 1, 12),
                10.0,
                unit="CAD",
            ),
        ],
    )
    con = duckdb.connect(str(db), read_only=True)
    try:
        report = run_qa(con)
    finally:
        con.close()
    assert report.ratio_gap_counts == {
        "mixed currency": 1,
        "zero denominator": 1,
        "OTHER period": 1,
        "missing input": 5,
    }
    assert report.ratio_gaps[0] == RatioGap(
        cik=RY_CIK,
        ratio="net_margin",
        period_start=date(2019, 11, 1),
        period_end=date(2020, 10, 31),
        period_type="FY",
        reason="mixed currency",
    )
    assert report.ratio_gaps[1].reason == "zero denominator"
    assert report.ratio_gaps[1].ratio == "roe"
    assert report.ratio_gaps[2].reason == "OTHER period"
    assert {gap.reason for gap in report.ratio_gaps} == set(report.ratio_gap_counts)


def test_cli_qa_duplicated_key_exits_1(tmp_path: Path) -> None:
    db = build_qa_db(tmp_path / "edgarpit.duckdb", duplicate_event=True)
    result = RUNNER.invoke(app, ["qa", "--db", str(db)])
    assert result.exit_code == 1
    assert "Key uniqueness: violated" in result.stdout
    assert "main.events_8k (accession, item_code): 1 duplicate keys" in result.stdout
    assert "key uniqueness violated in main.events_8k" in result.stderr


def test_cli_qa_unreadable_database_exits_1(tmp_path: Path) -> None:
    result = RUNNER.invoke(app, ["qa", "--db", str(tmp_path / "absent.duckdb")])
    assert result.exit_code == 1
    assert result.stderr.startswith("error: ")
