"""Tests for the deterministic Parquet release (FR-1.11).

The release is checked for byte determinism, manifest contents (row counts,
hashes, schemas, build timestamp, database name, SEC fetch range) and
round-trip equality: re-reading every Parquet file through DuckDB must match
the source table row for row.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
from typer.testing import CliRunner

from edgarpit.cli import app
from edgarpit.export import EXPORTS, PARQUET_ROW_GROUP_SIZE, export_release

RUNNER = CliRunner()
RY_CIK = "0001000275"

_FUNDAMENTALS_DDL = """
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

_RATIOS_DDL = """
CREATE TABLE main.ratios_pit (
    cik VARCHAR, ratio VARCHAR, period_start DATE, period_end DATE, period_type VARCHAR,
    asof_ts TIMESTAMPTZ, value DOUBLE, currency VARCHAR,
    numerator_field VARCHAR, numerator_accession VARCHAR, numerator_acceptance_ts TIMESTAMPTZ,
    denominator_field VARCHAR, denominator_accession VARCHAR,
    denominator_acceptance_ts TIMESTAMPTZ
)
"""

_EVENTS_DDL = """
CREATE TABLE main.events_8k (
    cik VARCHAR, ticker VARCHAR, accession VARCHAR, form VARCHAR,
    acceptance_ts TIMESTAMPTZ, ts_imputed BOOLEAN, item_code VARCHAR, item_label VARCHAR
)
"""

_FILINGS_DDL = """
CREATE TABLE main.filings (
    cik VARCHAR(10), accession VARCHAR, form VARCHAR, filing_date DATE, report_date DATE,
    acceptance_ts TIMESTAMPTZ, acceptance_ts_source VARCHAR, acceptance_ts_api TIMESTAMPTZ,
    ts_imputed BOOLEAN, primary_document VARCHAR, items VARCHAR, is_amendment BOOLEAN,
    is_xbrl BOOLEAN, source VARCHAR, ticker VARCHAR
)
"""

_FACTS_DDL = """
CREATE TABLE raw.facts_raw (
    cik VARCHAR(10), taxonomy VARCHAR, concept VARCHAR, unit VARCHAR, value DOUBLE,
    period_start DATE, period_end DATE, fy INTEGER, fp VARCHAR, form VARCHAR,
    accession VARCHAR, filed DATE, frame VARCHAR
)
"""


def build_export_db(path: Path) -> Path:
    con = duckdb.connect(str(path))
    try:
        con.execute("SET TimeZone='UTC'")
        con.execute("CREATE SCHEMA raw")
        for ddl in (_FUNDAMENTALS_DDL, _RATIOS_DDL, _EVENTS_DDL, _FILINGS_DDL, _FACTS_DDL):
            con.execute(ddl)
        con.execute(
            "INSERT INTO main.fundamentals_pit VALUES "
            "(?, 'RY', 'revenue', ?, ?, 'FY', '0001193125-23-285639', '40-F', ?, false, "
            "56129000000.0, 'CAD', 'CAD', 'ifrs-full', 'Revenue', 1, 1, 56129000000.0, "
            "false, NULL),"
            "(?, 'RY', 'revenue', ?, ?, 'FY', '0001193125-24-270294', '40-F', ?, false, "
            "51464000000.0, 'CAD', 'CAD', 'ifrs-full', 'Revenue', 1, 2, 56129000000.0, "
            "true, -0.0831),"
            "(?, 'RY', 'total_assets', NULL, ?, 'FY', '0001193125-23-285639', '40-F', ?, "
            "false, 1000000.0, 'CAD', 'CAD', 'ifrs-full', 'Assets', 1, 1, 1000000.0, "
            "false, NULL)",
            [
                RY_CIK,
                date(2022, 11, 1),
                date(2023, 10, 31),
                datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
                RY_CIK,
                date(2022, 11, 1),
                date(2023, 10, 31),
                datetime(2024, 12, 3, 21, tzinfo=UTC),
                RY_CIK,
                date(2023, 10, 31),
                datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
            ],
        )
        con.execute(
            "INSERT INTO main.ratios_pit VALUES (?, 'net_margin', ?, ?, 'FY', ?, 0.5, 'CAD', "
            "'net_income', '0001193125-23-285639', ?, 'revenue', '0001193125-23-285639', ?)",
            [
                RY_CIK,
                date(2022, 11, 1),
                date(2023, 10, 31),
                datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
                datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
                datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
            ],
        )
        con.execute(
            "INSERT INTO main.events_8k VALUES "
            "(?, 'RY', '0000320193-24-000006', '8-K', ?, false, '2.02', 'Results of Operations')",
            [RY_CIK, datetime(2024, 2, 1, 21, 30, tzinfo=UTC)],
        )
        con.execute(
            "INSERT INTO main.filings VALUES (?, '0001193125-23-285639', '40-F', ?, ?, ?, "
            "'sgml_header', ?, false, 'ry.htm', NULL, false, true, 'recent', 'RY')",
            [
                RY_CIK,
                date(2023, 11, 30),
                date(2023, 10, 31),
                datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
                datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
            ],
        )
        con.execute(
            "INSERT INTO raw.facts_raw VALUES (?, 'ifrs-full', 'Revenue', 'CAD', 56129000000.0, "
            "?, ?, 2023, 'FY', '40-F', '0001193125-23-285639', ?, NULL)",
            [RY_CIK, date(2022, 11, 1), date(2023, 10, 31), date(2023, 11, 30)],
        )
    finally:
        con.close()
    return path


def connect(db: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db), read_only=True)


def test_export_writes_every_table_and_manifest(tmp_path: Path) -> None:
    db = build_export_db(tmp_path / "edgarpit.duckdb")
    raw_manifest = tmp_path / "raw" / "manifest.jsonl"
    raw_manifest.parent.mkdir()
    raw_manifest.write_text(
        json.dumps(
            {
                "path": "raw/submissions/0001000275/a.json.zst",
                "fetched_at": "20261017T040215210934Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "path": "raw/headers/0001000275/x/b.hdr.sgml.zst",
                "fetched_at": "20261017T040325728091Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "release"
    con = connect(db)
    try:
        manifest = export_release(con, out, raw_manifest=raw_manifest)
    finally:
        con.close()

    assert [entry.table for entry in manifest.files] == [table.name for table in EXPORTS]
    assert [entry.rows for entry in manifest.files] == [3, 1, 1, 1, 1]
    assert manifest.database == "edgarpit.duckdb"
    assert manifest.sec_fetch_range is not None
    assert manifest.sec_fetch_range.min == "20261017T040215210934Z"
    assert manifest.sec_fetch_range.max == "20261017T040325728091Z"
    assert manifest.build_timestamp.endswith("Z")
    datetime.fromisoformat(manifest.build_timestamp.replace("Z", "+00:00"))

    payload = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert payload["database"] == "edgarpit.duckdb"
    assert payload["sec_fetch_range"] == {
        "min": "20261017T040215210934Z",
        "max": "20261017T040325728091Z",
    }
    for entry, written in zip(payload["files"], manifest.files, strict=True):
        path = out / entry["file"]
        assert path.is_file()
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["rows"] == written.rows
    fundamentals = payload["files"][0]
    assert fundamentals["schema"]["cik"] == "VARCHAR"
    assert fundamentals["schema"]["value"] == "DOUBLE"
    assert fundamentals["schema"]["acceptance_ts"] == "TIMESTAMP WITH TIME ZONE"
    assert fundamentals["schema"]["period_start"] == "DATE"


def test_export_is_byte_identical_across_runs(tmp_path: Path) -> None:
    db = build_export_db(tmp_path / "edgarpit.duckdb")
    con = connect(db)
    try:
        first = export_release(con, tmp_path / "release-a")
        second = export_release(con, tmp_path / "release-b")
    finally:
        con.close()
    for entry in first.files:
        one = (tmp_path / "release-a" / entry.file).read_bytes()
        two = (tmp_path / "release-b" / entry.file).read_bytes()
        assert one == two, entry.file
        assert hashlib.sha256(one).hexdigest() == entry.sha256
    assert [entry.sha256 for entry in first.files] == [entry.sha256 for entry in second.files]
    first_json = json.loads((tmp_path / "release-a" / "manifest.json").read_text())
    second_json = json.loads((tmp_path / "release-b" / "manifest.json").read_text())
    assert first_json.pop("build_timestamp") != "" and second_json.pop("build_timestamp") != ""
    assert first_json == second_json


def test_parquet_rereads_equal_the_source_tables(tmp_path: Path) -> None:
    db = build_export_db(tmp_path / "edgarpit.duckdb")
    out = tmp_path / "release"
    con = connect(db)
    try:
        export_release(con, out)
        for table in EXPORTS:
            parquet = (out / f"{table.name}.parquet").as_posix()
            difference = con.execute(
                f"""
                (SELECT * FROM parquet_scan('{parquet}')
                 EXCEPT SELECT * FROM {table.source})
                UNION ALL
                (SELECT * FROM {table.source}
                 EXCEPT SELECT * FROM parquet_scan('{parquet}'))
                """
            ).fetchall()
            assert difference == [], table.name
            rows = con.execute(f"SELECT count(*) FROM parquet_scan('{parquet}')").fetchone()
            assert rows is not None
            assert rows[0] == len(con.execute(f"SELECT * FROM {table.source}").fetchall())
    finally:
        con.close()


def test_export_records_a_fixed_row_group_size(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    db = build_export_db(tmp_path / "edgarpit.duckdb")
    out = tmp_path / "release"
    con = connect(db)
    try:
        export_release(con, out)
    finally:
        con.close()
    parquet_file = pq.ParquetFile(out / "fundamentals_pit.parquet")
    assert parquet_file.metadata.num_row_groups == 1
    assert PARQUET_ROW_GROUP_SIZE == 100_000


def test_sec_fetch_range_is_null_without_a_raw_manifest(tmp_path: Path) -> None:
    db = build_export_db(tmp_path / "edgarpit.duckdb")
    out = tmp_path / "release"
    con = connect(db)
    try:
        manifest = export_release(con, out)
    finally:
        con.close()
    assert manifest.sec_fetch_range is None
    payload = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert payload["sec_fetch_range"] is None


def test_bad_raw_manifest_is_rejected(tmp_path: Path) -> None:
    db = build_export_db(tmp_path / "edgarpit.duckdb")
    bad = tmp_path / "manifest.jsonl"
    bad.write_text('{"path": "x"}\n', encoding="utf-8")
    con = connect(db)
    try:
        try:
            export_release(con, tmp_path / "release", raw_manifest=bad)
        except ValueError as error:
            assert "line 1" in str(error) and "fetched_at" in str(error)
        else:  # pragma: no cover - the check must fire
            raise AssertionError("malformed raw manifest was accepted")
    finally:
        con.close()


def test_cli_export_writes_the_release(tmp_path: Path) -> None:
    db = build_export_db(tmp_path / "edgarpit.duckdb")
    out = tmp_path / "release"
    result = RUNNER.invoke(app, ["export", "--db", str(db), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "manifest.json").is_file()
    for table in EXPORTS:
        assert (out / f"{table.name}.parquet").is_file()
        assert f"{table.name}.parquet" in result.stdout


def test_cli_export_with_data_records_the_fetch_range(tmp_path: Path) -> None:
    db = build_export_db(tmp_path / "edgarpit.duckdb")
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    (raw / "manifest.jsonl").write_text(
        json.dumps({"fetched_at": "20261017T040215210934Z"}) + "\n", encoding="utf-8"
    )
    result = RUNNER.invoke(
        app,
        [
            "export",
            "--db",
            str(db),
            "--out",
            str(tmp_path / "release"),
            "--data",
            str(tmp_path / "data"),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / "release" / "manifest.json").read_text())
    assert payload["sec_fetch_range"] == {
        "min": "20261017T040215210934Z",
        "max": "20261017T040215210934Z",
    }


def test_cli_export_with_a_missing_data_manifest_exits_1(tmp_path: Path) -> None:
    db = build_export_db(tmp_path / "edgarpit.duckdb")
    result = RUNNER.invoke(
        app,
        [
            "export",
            "--db",
            str(db),
            "--out",
            str(tmp_path / "release"),
            "--data",
            str(tmp_path / "empty"),
        ],
    )
    assert result.exit_code == 1
    assert "no raw manifest under" in result.stderr


def test_cli_export_unreadable_database_exits_1(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app, ["export", "--db", str(tmp_path / "absent.duckdb"), "--out", str(tmp_path / "release")]
    )
    assert result.exit_code == 1
    assert result.stderr.startswith("error: ")
