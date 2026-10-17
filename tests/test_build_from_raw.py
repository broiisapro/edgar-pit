"""End-to-end build from the fixture raw cache (issue #27)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from edgarpit.load import DBT_TARGET_DIRNAME, build_from_raw, main

FIXTURE_RAW = Path(__file__).parent / "fixtures" / "sec"
EXPECTED_ASOF = Path(__file__).parent / "fixtures" / "expected_asof.json"

RY_CIK = "0001000275"
RY_FY2023_40F = "0001193125-23-285639"
RY_FY2023_RESTATED_40F = "0001193125-24-270294"
RY_FY2023_ACCEPTED = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
RY_FY2023_RESTATED_ACCEPTED = datetime(2024, 12, 4, 11, 26, 17, tzinfo=UTC)

TABLES = (
    "raw.filings",
    "raw.events_8k",
    "raw.facts_raw",
    "raw.concept_map",
    "raw.universe",
    "main.fundamentals_pit",
    "main.ratios_pit",
    "main.filings",
    "main.events_8k",
)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    out_dir = tmp_path_factory.mktemp("built")
    yield build_from_raw(FIXTURE_RAW, out_dir)


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path))
    con.execute("SET TimeZone='UTC'")
    return con


def table_rows(db_path: Path, table: str) -> list[tuple[object, ...]]:
    schema, name = table.split(".")
    con = connect(db_path)
    try:
        columns = [
            str(row[0])
            for row in con.execute(
                "select column_name from information_schema.columns "
                "where table_schema = ? and table_name = ? order by ordinal_position",
                [schema, name],
            ).fetchall()
        ]
        order_by = ", ".join(columns)
        return con.execute(f"select * from {table} order by {order_by}").fetchall()
    finally:
        con.close()


def test_build_returns_the_database_and_dbt_artifacts(built: Path) -> None:
    assert built.name == "edgarpit.duckdb"
    assert built.is_file()
    assert (built.parent / DBT_TARGET_DIRNAME / "run_results.json").is_file()

    con = connect(built)
    try:
        for table in TABLES:
            count = con.execute(f"select count(*) from {table}").fetchone()
            assert count is not None and count[0] > 0, table
    finally:
        con.close()


def test_ry_fy2023_revenue_versions_have_header_acceptance_times(built: Path) -> None:
    con = connect(built)
    try:
        rows = con.execute(
            """
            select version, value, accession, acceptance_ts, ts_imputed, period_type
            from main.fundamentals_pit
            where cik = ? and field = 'revenue'
              and period_start = date '2022-11-01' and period_end = date '2023-10-31'
            order by version
            """,
            [RY_CIK],
        ).fetchall()
    finally:
        con.close()

    assert rows == [
        (1, 56_129_000_000.0, RY_FY2023_40F, RY_FY2023_ACCEPTED, False, "FY"),
        (2, 51_464_000_000.0, RY_FY2023_RESTATED_40F, RY_FY2023_RESTATED_ACCEPTED, False, "FY"),
    ]


def test_expected_asof_is_reproduced(built: Path) -> None:
    expected = json.loads(EXPECTED_ASOF.read_text(encoding="utf-8"))
    asof_ts = datetime.fromisoformat(expected["asof"].replace("Z", "+00:00"))
    con = connect(built)
    try:
        row = con.execute(
            """
            select cik, field, value, unit, currency, period_start, period_end,
                   period_type, accession, acceptance_ts, version
            from (
                select
                    *,
                    row_number() over (
                        partition by period_start, period_end
                        order by acceptance_ts desc, version desc
                    ) as row_num
                from main.fundamentals_pit
                where cik = ? and field = ? and period_type = ? and acceptance_ts <= ?
            )
            where row_num = 1
            order by period_end desc
            limit 1
            """,
            [expected["cik"], expected["field"], expected["period"], asof_ts],
        ).fetchone()
    finally:
        con.close()

    assert row is not None
    assert row[0] == expected["cik"]
    assert row[1] == expected["field"]
    assert row[2] == expected["value"]
    assert row[3] == expected["unit"]
    assert row[4] == expected["currency"]
    assert row[5].isoformat() == expected["period_start"]
    assert row[6].isoformat() == expected["period_end"]
    assert row[7] == expected["period"]
    assert row[8] == expected["accession"]
    assert row[9] == datetime.fromisoformat(expected["acceptance_ts"].replace("Z", "+00:00"))
    assert row[10] >= 1


def test_main_prints_counts_and_the_build_is_deterministic(
    built: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    second_out = tmp_path / "second"
    exit_code = main(["--raw", str(FIXTURE_RAW), "--out", str(second_out)])

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert "raw.filings: " in printed
    assert "raw.concept_map: " in printed
    assert "dbt build: PASS=" in printed
    assert "ERROR=0" in printed
    assert f"database: {second_out / 'edgarpit.duckdb'}" in printed

    second_db = second_out / "edgarpit.duckdb"
    for table in TABLES:
        assert table_rows(built, table) == table_rows(second_db, table), table
