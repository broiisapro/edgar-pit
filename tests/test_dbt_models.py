"""Run the dbt models with ``dbt build`` against a development database in a temp dir."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest
from dbt.cli.main import dbtRunner, dbtRunnerResult

from edgarpit.devdb import create_dev_db

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSFORM_DIR = REPO_ROOT / "transform"
STAGING_DIR = Path(__file__).parent / "fixtures" / "staging"

RY_CIK = "0001000275"
RY_FY2023_40F = "0001193125-23-285639"
RY_FY2023_RESTATED_40F = "0001193125-24-270294"
RY_FY2023_ACCEPTED = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
RY_FY2023_RESTATED_ACCEPTED = datetime(2024, 12, 4, 11, 26, 17, tzinfo=UTC)
SHOP_CIK = "0001594805"
AAPL_CIK = "0000320193"

FUNDAMENTALS_PIT_COLUMNS = [
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
]

FILINGS_COLUMNS = [
    "cik",
    "accession",
    "form",
    "filing_date",
    "report_date",
    "acceptance_ts",
    "acceptance_ts_source",
    "acceptance_ts_api",
    "ts_imputed",
    "primary_document",
    "items",
    "is_amendment",
    "is_xbrl",
    "source",
    "ticker",
]

EVENTS_8K_COLUMNS = [
    "cik",
    "accession",
    "form",
    "acceptance_ts",
    "ts_imputed",
    "item_code",
    "item_label",
    "ticker",
]

RATIOS_PIT_COLUMNS = [
    "cik",
    "ratio",
    "period_start",
    "period_end",
    "period_type",
    "asof_ts",
    "value",
    "currency",
    "numerator_field",
    "numerator_accession",
    "numerator_acceptance_ts",
    "denominator_field",
    "denominator_accession",
    "denominator_acceptance_ts",
]


@dataclass(frozen=True)
class DbtBuild:
    """A finished dbt run, the database it wrote and its artifacts dir."""

    db: Path
    target: Path
    result: dbtRunnerResult


def run_dbt(db_path: Path, target_path: Path, *args: str) -> dbtRunnerResult:
    os.environ["EDGARPIT_DB"] = str(db_path)
    runner = dbtRunner()
    return runner.invoke(
        [
            "build",
            "--project-dir",
            str(TRANSFORM_DIR),
            "--profiles-dir",
            str(TRANSFORM_DIR),
            "--target",
            "fixtures",
            "--target-path",
            str(target_path),
            *args,
        ]
    )


def query(db_path: Path, sql: str, params: list[object] | None = None) -> list[tuple[object, ...]]:
    con = duckdb.connect(str(db_path))
    try:
        con.execute("SET TimeZone='UTC'")
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def table_columns(db_path: Path, table: str) -> list[str]:
    rows = query(
        db_path,
        "select column_name from information_schema.columns "
        "where table_schema = 'main' and table_name = ? order by ordinal_position",
        [table],
    )
    return [str(row[0]) for row in rows]


def write_synthetic_staging(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("filings.csv", "facts_raw.csv", "events_8k.csv"):
        shutil.copy(STAGING_DIR / name, directory / name)
    rows = [
        "0000320193,us-gaap,Revenues,USD,1000000000.0,2017-10-01,2018-09-29,2018,FY,10-K,"
        "0000320193-99-000777,2018-11-05,",
        "0000320193,us-gaap,CommonStockSharesOutstanding,shares,15500000000.0,,2024-09-28,"
        "2024,FY,10-K,0000320193-24-000123,2024-11-01,",
        "0000320193,us-gaap,CommonStockSharesOutstanding,shares,15500000000.0,,2024-09-28,"
        "2024,FY,10-K,0000320193-24-000123,2024-11-01,",
        "0000320193,us-gaap,NetIncomeLoss,USD,78000000000.0,2023-10-01,2024-06-29,2024,Q3,"
        "10-K,0000320193-24-000123,2024-11-01,",
        "0000320193,us-gaap,StockholdersEquity,CAD,78000000000.0,,2024-09-28,2025,FY,10-K,"
        "0000320193-25-000079,2025-10-31,",
    ]
    with (directory / "facts_raw.csv").open("a", encoding="utf-8") as handle:
        handle.write("\n".join(rows) + "\n")


@pytest.fixture(scope="module")
def staging_build(tmp_path_factory: pytest.TempPathFactory) -> DbtBuild:
    out_dir = tmp_path_factory.mktemp("staging-db")
    target = tmp_path_factory.mktemp("staging-target")
    db_path = create_dev_db(out_dir)
    return DbtBuild(db_path, target, run_dbt(db_path, target))


@pytest.fixture(scope="module")
def synthetic_build(tmp_path_factory: pytest.TempPathFactory) -> DbtBuild:
    staging_dir = tmp_path_factory.mktemp("synthetic-staging")
    write_synthetic_staging(staging_dir)
    out_dir = tmp_path_factory.mktemp("synthetic-db")
    target = tmp_path_factory.mktemp("synthetic-target")
    db_path = create_dev_db(out_dir, staging_dir=staging_dir)
    return DbtBuild(db_path, target, run_dbt(db_path, target))


@pytest.fixture(scope="module")
def injected_build(tmp_path_factory: pytest.TempPathFactory) -> DbtBuild:
    out_dir = tmp_path_factory.mktemp("injected-db")
    target = tmp_path_factory.mktemp("injected-target")
    db_path = create_dev_db(out_dir)
    result = run_dbt(db_path, target, "--vars", "{inject_future_ratio: true}")
    return DbtBuild(db_path, target, result)


def test_dbt_build_succeeds_on_the_staging_data(staging_build: DbtBuild) -> None:
    assert staging_build.result.success, staging_build.result.exception


def test_dbt_build_succeeds_on_the_synthetic_data(synthetic_build: DbtBuild) -> None:
    assert synthetic_build.result.success, synthetic_build.result.exception


def test_output_models_match_the_column_contract(staging_build: DbtBuild) -> None:
    assert table_columns(staging_build.db, "fundamentals_pit") == FUNDAMENTALS_PIT_COLUMNS
    assert table_columns(staging_build.db, "ratios_pit") == RATIOS_PIT_COLUMNS
    assert table_columns(staging_build.db, "filings") == FILINGS_COLUMNS
    assert table_columns(staging_build.db, "events_8k") == EVENTS_8K_COLUMNS


def test_ry_fy2023_revenue_versions_and_restatement(staging_build: DbtBuild) -> None:
    rows = query(
        staging_build.db,
        """
        select version, value, is_restatement, pct_change_vs_first, accession,
               acceptance_ts, period_type, currency, ticker, source_taxonomy,
               source_concept, concept_rank, form
        from main.fundamentals_pit
        where cik = ? and field = 'revenue'
          and period_start = date '2022-11-01' and period_end = date '2023-10-31'
        order by version
        """,
        [RY_CIK],
    )

    assert len(rows) == 2
    first, second = rows
    assert first == (
        1,
        56_129_000_000.0,
        False,
        0.0,
        RY_FY2023_40F,
        RY_FY2023_ACCEPTED,
        "FY",
        "CAD",
        "RY",
        "ifrs-full",
        "Revenue",
        1,
        "40-F",
    )
    assert second[0] == 2
    assert second[1] == 51_464_000_000.0
    assert second[2] is True
    assert second[3] == pytest.approx(51_464_000_000.0 / 56_129_000_000.0 - 1.0)
    assert second[4] == RY_FY2023_RESTATED_40F
    assert second[5] == RY_FY2023_RESTATED_ACCEPTED


def test_shop_fy2022_revenue_repeat_is_not_a_restatement(staging_build: DbtBuild) -> None:
    rows = query(
        staging_build.db,
        """
        select version, value, is_restatement, pct_change_vs_first, accession, period_type
        from main.fundamentals_pit
        where cik = ? and field = 'revenue'
          and period_start = date '2022-01-01' and period_end = date '2022-12-31'
        order by version
        """,
        [SHOP_CIK],
    )

    assert len(rows) == 2
    assert rows[0][0:4] == (1, 5_599_864_000.0, False, 0.0)
    assert rows[1][0:3] == (2, 5_600_000_000.0, False)
    assert rows[1][3] == pytest.approx(136_000.0 / 5_599_864_000.0)
    assert rows[1][4] == "0001594805-24-000007"
    assert {row[5] for row in rows} == {"FY"}


def test_instant_period_type_is_fy_only_at_a_fiscal_year_end(staging_build: DbtBuild) -> None:
    rows = query(
        staging_build.db,
        """
        select period_end, period_type
        from main.fundamentals_pit
        where cik = ? and field = 'total_assets' and period_start is null
        order by period_end
        """,
        [AAPL_CIK],
    )

    assert rows == [
        (date(2022, 9, 24), "FY"),
        (date(2023, 9, 30), "FY"),
        (date(2023, 12, 30), "Q"),
        (date(2024, 3, 30), "Q"),
        (date(2024, 9, 28), "FY"),
    ]


def test_versions_are_gapless_and_start_at_one(staging_build: DbtBuild) -> None:
    gaps = query(
        staging_build.db,
        """
        select cik, field, period_start, period_end, min(version), max(version), count(*)
        from main.fundamentals_pit
        group by cik, field, period_start, period_end
        having min(version) <> 1 or max(version) <> count(*)
        """,
    )
    first_versions = query(
        staging_build.db,
        """
        select count(*)
        from main.fundamentals_pit
        where version = 1 and (first_value <> value or is_restatement)
        """,
    )

    assert gaps == []
    assert first_versions == [(0,)]


def test_filings_and_events_carry_the_ticker(staging_build: DbtBuild) -> None:
    filings = query(
        staging_build.db,
        "select ticker from main.filings where accession = ?",
        [RY_FY2023_40F],
    )
    events = query(
        staging_build.db,
        "select distinct ticker from main.events_8k",
    )

    assert filings == [("RY",)]
    assert events == [("AAPL",)]


def test_missing_accession_falls_back_to_filed_end_of_day(synthetic_build: DbtBuild) -> None:
    rows = query(
        synthetic_build.db,
        """
        select acceptance_ts, ts_imputed, period_type, version, currency, is_restatement
        from main.fundamentals_pit
        where cik = ? and field = 'revenue' and accession = '0000320193-99-000777'
        """,
        [AAPL_CIK],
    )

    assert rows == [
        (
            datetime(2018, 11, 6, 4, 59, 59, tzinfo=UTC),
            True,
            "FY",
            1,
            "USD",
            False,
        )
    ]


def test_currency_is_null_for_share_units(synthetic_build: DbtBuild) -> None:
    rows = query(
        synthetic_build.db,
        """
        select currency, unit, ticker, period_type
        from main.fundamentals_pit
        where cik = ? and field = 'shares_outstanding'
        """,
        [AAPL_CIK],
    )

    assert rows == [(None, "shares", "AAPL", "FY")]


def test_other_period_types_are_kept_out_of_fy_and_q(synthetic_build: DbtBuild) -> None:
    rows = query(
        synthetic_build.db,
        """
        select period_type, period_start, period_end
        from main.fundamentals_pit
        where cik = ? and field = 'net_income' and period_start = date '2023-10-01'
          and period_end = date '2024-06-29'
        """,
        [AAPL_CIK],
    )

    assert rows == [("OTHER", date(2023, 10, 1), date(2024, 6, 29))]


def test_stg_facts_removes_exact_duplicates(synthetic_build: DbtBuild) -> None:
    counts = query(
        synthetic_build.db,
        """
        select
            (select count(*) from main.stg_facts),
            (select count(*) from raw.facts_raw)
        """,
    )

    assert counts == [(145, 146)]


def test_mixed_currency_inputs_produce_no_ratio_row(synthetic_build: DbtBuild) -> None:
    rows = query(
        synthetic_build.db,
        """
        select count(*)
        from main.ratios_pit
        where cik = ? and ratio in ('roe', 'leverage')
          and period_end = date '2024-09-28'
          and asof_ts >= ?
        """,
        [AAPL_CIK, datetime(2025, 10, 31, 18, 1, 59, tzinfo=UTC)],
    )

    assert rows == [(0,)]


def test_ry_fy2023_roe_tracks_both_40f_acceptances(staging_build: DbtBuild) -> None:
    rows = query(
        staging_build.db,
        """
        select asof_ts, value, numerator_accession, denominator_accession,
               numerator_acceptance_ts, denominator_acceptance_ts, currency,
               period_type, period_start, period_end
        from main.ratios_pit
        where cik = ? and ratio = 'roe'
          and period_start = date '2022-11-01' and period_end = date '2023-10-31'
        order by asof_ts
        """,
        [RY_CIK],
    )

    original = [row for row in rows if row[0] == RY_FY2023_ACCEPTED]
    restated = [row for row in rows if row[0] == RY_FY2023_RESTATED_ACCEPTED]
    assert len(original) == 1
    assert len(restated) == 1
    assert original[0] == (
        RY_FY2023_ACCEPTED,
        14_866_000_000.0 / 117_760_000_000.0,
        RY_FY2023_40F,
        RY_FY2023_40F,
        RY_FY2023_ACCEPTED,
        RY_FY2023_ACCEPTED,
        "CAD",
        "FY",
        date(2022, 11, 1),
        date(2023, 10, 31),
    )
    assert restated[0][0] == RY_FY2023_RESTATED_ACCEPTED
    assert restated[0][1] == pytest.approx(14_612_000_000.0 / 115_147_000_000.0)
    assert restated[0][2] == restated[0][3] == RY_FY2023_RESTATED_40F
    assert restated[0][4] == restated[0][5] == RY_FY2023_RESTATED_ACCEPTED


def test_ratio_rows_satisfy_the_as_of_invariant(staging_build: DbtBuild) -> None:
    violations = query(
        staging_build.db,
        """
        select count(*)
        from main.ratios_pit
        where numerator_acceptance_ts > asof_ts
           or denominator_acceptance_ts > asof_ts
           or asof_ts <> greatest(numerator_acceptance_ts, denominator_acceptance_ts)
           or currency is null
        """,
    )
    periods = query(
        staging_build.db,
        """
        select count(*)
        from main.ratios_pit
        where (ratio = 'leverage' and period_start is not null)
           or (ratio in ('roe', 'net_margin') and period_start is null)
        """,
    )

    assert violations == [(0,)]
    assert periods == [(0,)]


def test_injected_future_ratio_fails_the_future_input_test(injected_build: DbtBuild) -> None:
    assert injected_build.result.success is False
    run_results = json.loads((injected_build.target / "run_results.json").read_text())
    failed = {
        result["unique_id"] for result in run_results["results"] if result["status"] == "fail"
    }

    assert failed == {"test.edgarpit.assert_ratio_inputs_not_from_future"}
    future_rows = query(
        injected_build.db,
        """
        select count(*)
        from main.ratios_pit
        where numerator_acceptance_ts > asof_ts
        """,
    )
    assert future_rows == [(1,)]
