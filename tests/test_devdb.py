"""Tests for :mod:`edgarpit.devdb`."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest
from pandera.errors import SchemaError

from edgarpit.devdb import create_dev_db

FILINGS_CSV = (
    "cik,accession,form,filing_date,report_date,acceptance_ts,acceptance_ts_source,"
    "acceptance_ts_api,ts_imputed,primary_document,items,is_amendment,is_xbrl,source\n"
    "0000320193,0000320193-25-000079,10-K,2025-10-31,2025-09-27,"
    "2025-10-31T18:01:59Z,sgml_header,,false,aapl-20250927.htm,,false,true,recent\n"
    "0000320193,0000320193-25-000080,8-K,2025-10-30,,"
    "2025-10-30T20:05:00Z,filing_date_eod,2025-10-31T00:05:00.000Z,true,,"
    "2.02|9.01,false,true,recent\n"
)

FACTS_CSV = (
    "cik,taxonomy,concept,unit,value,period_start,period_end,fy,fp,form,accession,filed,frame\n"
    "0000320193,us-gaap,Revenues,USD,416161000000,2024-09-29,2025-09-27,2025,FY,"
    "10-K,0000320193-25-000079,2025-10-31,\n"
)

EVENTS_8K_CSV = (
    "cik,accession,form,acceptance_ts,ts_imputed,item_code,item_label\n"
    "0000320193,0000320193-25-000080,8-K,2025-10-30T20:05:00Z,true,2.02,"
    "Results of Operations and Financial Condition\n"
    "0000320193,0000320193-25-000080,8-K,2025-10-30T20:05:00Z,true,9.01,"
    "Financial Statements and Exhibits\n"
)


def write_staging(staging: Path) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "filings.csv").write_text(FILINGS_CSV, encoding="utf-8")
    (staging / "facts_raw.csv").write_text(FACTS_CSV, encoding="utf-8")
    (staging / "events_8k.csv").write_text(EVENTS_8K_CSV, encoding="utf-8")


def raw_tables(db_path: Path) -> set[str]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw'"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        con.close()


def test_empty_staging_leaves_empty_tables(tmp_path: Path) -> None:
    db_path = create_dev_db(tmp_path / "out", staging_dir=tmp_path / "absent")

    assert db_path == tmp_path / "out" / "edgarpit.duckdb"
    assert raw_tables(db_path) == {"filings", "events_8k", "facts_raw"}
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        for table in ("filings", "events_8k", "facts_raw"):
            assert con.execute(f"SELECT count(*) FROM raw.{table}").fetchone() == (0,)
    finally:
        con.close()


def test_populated_staging_loads_validated_rows(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    write_staging(staging)

    db_path = create_dev_db(tmp_path / "out", staging_dir=staging)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        con.execute("SET TimeZone = 'UTC'")
        assert con.execute("SELECT count(*) FROM raw.filings").fetchone() == (2,)
        assert con.execute("SELECT count(*) FROM raw.events_8k").fetchone() == (2,)
        assert con.execute("SELECT count(*) FROM raw.facts_raw").fetchone() == (1,)

        filing_date, acceptance_ts = con.execute(
            "SELECT filing_date, acceptance_ts FROM raw.filings "
            "WHERE accession = '0000320193-25-000079'"
        ).fetchone()
        assert filing_date == date(2025, 10, 31)
        assert acceptance_ts == datetime(2025, 10, 31, 18, 1, 59, tzinfo=UTC)

        assert con.execute("SELECT current_setting('TimeZone')").fetchone() == ("UTC",)

        label = con.execute(
            "SELECT item_label FROM raw.events_8k WHERE item_code = '2.02'"
        ).fetchone()
        assert label == ("Results of Operations and Financial Condition",)
    finally:
        con.close()


def test_existing_database_is_recreated(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    write_staging(staging)
    db_path = create_dev_db(tmp_path / "out", staging_dir=staging)
    assert db_path.exists()

    empty = create_dev_db(tmp_path / "out", staging_dir=tmp_path / "absent")

    assert empty == db_path
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM raw.filings").fetchone() == (0,)
    finally:
        con.close()


def test_invalid_staging_csv_is_rejected(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "filings.csv").write_text(
        FILINGS_CSV.replace("0000320193,0000320193-25-000079", "32193,0000320193-25-000079"),
        encoding="utf-8",
    )

    with pytest.raises(SchemaError):
        create_dev_db(tmp_path / "out", staging_dir=staging)


def test_main_creates_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from edgarpit.devdb import main

    write_staging(tmp_path / "staging")
    exit_code = main(["--out", str(tmp_path / "out"), "--staging", str(tmp_path / "staging")])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "raw.filings: 2 rows" in out
    assert (tmp_path / "out" / "edgarpit.duckdb").exists()
