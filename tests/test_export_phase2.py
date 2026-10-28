"""Tests for the Phase 2 tables in the Parquet release (decisions A-32, A-38).

Each test builds its own database from the frozen contracts of
``docs/architecture.md``: the five v1.0.0 tables, ``main.fundamentals_pit_usd``
(section 7), and ``main.filings_text`` and ``raw.fx_daily`` (section 6, created
through :func:`edgarpit.schemas.duckdb_ddl`). The release is checked for the
three new files, their manifest entries, the text-size report, byte determinism
across exports and insertion orders, the fixed row order, and the guarantee
that a database without the Phase 2 tables exports the Phase 1 files with the
same bytes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq
from typer.testing import CliRunner

from edgarpit.cli import app
from edgarpit.export import (
    EXPORTS,
    FILINGS_TEXT_ROW_GROUP_SIZE,
    PARQUET_ROW_GROUP_SIZE,
    PHASE2_EXPORTS,
    Manifest,
    export_release,
)
from edgarpit.schemas import duckdb_ddl

RUNNER = CliRunner()
APPLE_CIK = "0000320193"
RY_CIK = "0001000275"
APPLE_8K = "0000320193-25-000080"
APPLE_8K_LATER = "0000320193-25-000090"
ACCEPTANCE = datetime(2025, 10, 30, 20, 5, tzinfo=UTC)

_FUNDAMENTALS_PIT_DDL = """
CREATE TABLE main.fundamentals_pit (
    cik VARCHAR(10), ticker VARCHAR, field VARCHAR,
    period_start DATE, period_end DATE, period_type VARCHAR,
    accession VARCHAR, form VARCHAR, acceptance_ts TIMESTAMPTZ, ts_imputed BOOLEAN,
    value DOUBLE, unit VARCHAR, currency VARCHAR,
    source_taxonomy VARCHAR, source_concept VARCHAR, concept_rank INTEGER,
    version INTEGER, first_value DOUBLE, is_restatement BOOLEAN,
    pct_change_vs_first DOUBLE, restatement_kind VARCHAR
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

# Every column of ``main.fundamentals_pit`` in its frozen order, then the
# conversion columns of decision A-32 (docs/architecture.md section 7).
_FUNDAMENTALS_PIT_USD_DDL = """
CREATE TABLE main.fundamentals_pit_usd (
    cik VARCHAR(10), ticker VARCHAR, field VARCHAR,
    period_start DATE, period_end DATE, period_type VARCHAR,
    accession VARCHAR, form VARCHAR, acceptance_ts TIMESTAMPTZ, ts_imputed BOOLEAN,
    value DOUBLE, unit VARCHAR, currency VARCHAR,
    source_taxonomy VARCHAR, source_concept VARCHAR, concept_rank INTEGER,
    version INTEGER, first_value DOUBLE, is_restatement BOOLEAN,
    pct_change_vs_first DOUBLE, restatement_kind VARCHAR,
    value_usd DOUBLE, fx_rate DOUBLE, fx_rate_date DATE, fx_known_at TIMESTAMPTZ,
    converted BOOLEAN, asof_ts TIMESTAMPTZ
)
"""

_RY_VALUE = 51_464_000_000.0
_RY_RATE = 1.3659
_RY_VALUE_USD = _RY_VALUE / _RY_RATE


def text_row(
    accession: str,
    document: str,
    section: str,
    section_order: int,
    text: str,
    *,
    doc_type: str = "primary",
    acceptance_ts: datetime = ACCEPTANCE,
) -> tuple[object, ...]:
    """Return one ``main.filings_text`` tuple; ``sha256`` and ``char_count`` follow ``text``."""
    return (
        APPLE_CIK,
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


def default_filings_text_rows() -> list[tuple[object, ...]]:
    """Four sections of two 8-K filings, in a deliberately unordered list."""
    return [
        text_row(
            APPLE_8K_LATER,
            "aapl-20251101.htm",
            "2.02",
            1,
            "Item 2.02 Results of Operations and Financial Condition\n\n"
            "The Company reported quarterly revenue of $102.5 billion.",
        ),
        text_row(
            APPLE_8K,
            "aapl-20251030.htm",
            "9.01",
            2,
            "Item 9.01 Financial Statements and Exhibits\n\n(d) Exhibits\n99.1 Press release.",
        ),
        text_row(
            APPLE_8K,
            "aapl-20251030.htm",
            "2.02",
            1,
            "Item 2.02 Results of Operations and Financial Condition\n\n"
            "Apple's quarterly revenue was €102.5 billion, a record.",
        ),
        text_row(
            APPLE_8K,
            "a8kexhibit991.htm",
            "unsegmented",
            1,
            "Apple reports fourth quarter results.",
            doc_type="EX-99.1",
        ),
    ]


def default_fx_rows() -> list[tuple[object, ...]]:
    """Three consecutive ``FXUSDCAD`` observations, in reverse date order."""
    return [
        ("FXUSDCAD", date(2023, 10, 31), 1.3659, datetime(2023, 10, 31, 21, tzinfo=UTC), "valet"),
        ("FXUSDCAD", date(2023, 10, 30), 1.3831, datetime(2023, 10, 30, 21, tzinfo=UTC), "valet"),
        ("FXUSDCAD", date(2023, 10, 27), 1.3875, datetime(2023, 10, 27, 21, tzinfo=UTC), "valet"),
    ]


def default_usd_rows() -> list[tuple[object, ...]]:
    """One USD row (converted at 1.0) and one CAD row converted with a rate."""
    return [
        (
            APPLE_CIK,
            "AAPL",
            "revenue",
            None,
            date(2024, 9, 28),
            "FY",
            "0000320193-24-000123",
            "10-K",
            datetime(2024, 11, 1, 20, 30, tzinfo=UTC),
            False,
            391_035_000_000.0,
            "USD",
            "USD",
            "us-gaap",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            1,
            1,
            391_035_000_000.0,
            False,
            None,
            "none",
            391_035_000_000.0,
            1.0,
            None,
            None,
            True,
            datetime(2024, 11, 1, 20, 30, tzinfo=UTC),
        ),
        (
            RY_CIK,
            "RY",
            "revenue",
            date(2022, 11, 1),
            date(2023, 10, 31),
            "FY",
            "0001193125-24-273268",
            "40-F",
            datetime(2024, 12, 4, 21, tzinfo=UTC),
            False,
            _RY_VALUE,
            "CAD",
            "CAD",
            "ifrs-full",
            "Revenue",
            1,
            1,
            _RY_VALUE,
            False,
            None,
            "none",
            _RY_VALUE_USD,
            _RY_RATE,
            date(2023, 10, 31),
            datetime(2023, 10, 31, 21, tzinfo=UTC),
            True,
            datetime(2024, 12, 4, 21, tzinfo=UTC),
        ),
    ]


def insert_phase1(con: duckdb.DuckDBPyConnection) -> None:
    """Create and fill the five v1.0.0 tables."""
    for ddl in (
        _FUNDAMENTALS_PIT_DDL,
        _RATIOS_DDL,
        _EVENTS_DDL,
        _FILINGS_DDL,
        _FACTS_DDL,
    ):
        con.execute(ddl)
    con.execute(
        """
        INSERT INTO main.fundamentals_pit VALUES
        (?, 'RY', 'revenue', ?, ?, 'FY', '0001193125-23-285639', '40-F', ?, false,
         56129000000.0, 'CAD', 'CAD', 'ifrs-full', 'Revenue', 1, 1, 56129000000.0,
         false, NULL, 'none'),
        (?, 'RY', 'revenue', ?, ?, 'FY', '0001193125-24-270294', '40-F', ?, false,
         51464000000.0, 'CAD', 'CAD', 'ifrs-full', 'Revenue', 1, 2, 56129000000.0,
         true, -0.0831, 'revision')
        """,
        [
            RY_CIK,
            date(2022, 11, 1),
            date(2023, 10, 31),
            datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
            RY_CIK,
            date(2022, 11, 1),
            date(2023, 10, 31),
            datetime(2024, 12, 3, 21, tzinfo=UTC),
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


def add_phase2_tables(
    con: duckdb.DuckDBPyConnection,
    *,
    reverse: bool = False,
    filings_text_rows: list[tuple[object, ...]] | None = None,
    fx_rows: list[tuple[object, ...]] | None = None,
    usd_rows: list[tuple[object, ...]] | None = None,
) -> None:
    """Create and fill ``main.fundamentals_pit_usd``, ``main.filings_text`` and ``raw.fx_daily``.

    A ``None`` row list uses the default rows of the matching builder; an empty
    list leaves the table empty. ``reverse`` inserts each table's rows in the
    opposite order, so a test can show that the exported bytes do not depend on
    insertion order.
    """
    con.execute(_FUNDAMENTALS_PIT_USD_DDL)
    con.execute(duckdb_ddl("filings_text", "main"))
    con.execute(duckdb_ddl("fx_daily", "raw"))
    tables = {
        "main.fundamentals_pit_usd": default_usd_rows() if usd_rows is None else usd_rows,
        "main.filings_text": (
            default_filings_text_rows() if filings_text_rows is None else filings_text_rows
        ),
        "raw.fx_daily": default_fx_rows() if fx_rows is None else fx_rows,
    }
    for table, rows in tables.items():
        selected = list(reversed(rows)) if reverse else rows
        if not selected:
            continue
        placeholders = ", ".join("?" for _ in selected[0])
        con.executemany(f"INSERT INTO {table} VALUES ({placeholders})", selected)


def build_db(
    path: Path,
    *,
    phase2: bool = True,
    reverse: bool = False,
    filings_text_rows: list[tuple[object, ...]] | None = None,
    fx_rows: list[tuple[object, ...]] | None = None,
    usd_rows: list[tuple[object, ...]] | None = None,
) -> Path:
    """Build a release-test database at ``path`` and return it."""
    con = duckdb.connect(str(path))
    try:
        con.execute("SET TimeZone='UTC'")
        con.execute("CREATE SCHEMA raw")
        insert_phase1(con)
        if phase2:
            add_phase2_tables(
                con,
                reverse=reverse,
                filings_text_rows=filings_text_rows,
                fx_rows=fx_rows,
                usd_rows=usd_rows,
            )
    finally:
        con.close()
    return path


def run_export(db: Path, out: Path) -> Manifest:
    """Export ``db`` to ``out`` and return the manifest object."""
    con = duckdb.connect(str(db), read_only=True)
    try:
        return export_release(con, out)
    finally:
        con.close()


def manifest_json(out: Path) -> dict[str, Any]:
    """Return the written ``manifest.json`` as a dictionary."""
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def entries_by_table(out: Path) -> dict[str, dict[str, Any]]:
    """Return the manifest's file entries keyed by table name."""
    return {entry["table"]: entry for entry in manifest_json(out)["files"]}


def test_phase2_tables_join_the_release(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    out = tmp_path / "release"
    manifest = run_export(db, out)

    assert [entry.table for entry in manifest.files] == [
        table.name for table in (*EXPORTS, *PHASE2_EXPORTS)
    ]
    entries = entries_by_table(out)
    for name in ("fundamentals_pit_usd", "filings_text", "fx_daily"):
        path = out / f"{name}.parquet"
        assert path.is_file()
        assert entries[name]["bytes"] == path.stat().st_size
        assert entries[name]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert entries["fundamentals_pit_usd"]["rows"] == 2
    assert entries["fundamentals_pit_usd"]["schema"]["value_usd"] == "DOUBLE"
    assert entries["fundamentals_pit_usd"]["schema"]["asof_ts"] == "TIMESTAMP WITH TIME ZONE"
    assert entries["filings_text"]["rows"] == 4
    assert entries["filings_text"]["schema"]["text"] == "VARCHAR"
    assert entries["filings_text"]["schema"]["section_order"] == "INTEGER"
    assert entries["fx_daily"]["rows"] == 3
    assert entries["fx_daily"]["schema"]["rate_date"] == "DATE"
    assert entries["fx_daily"]["schema"]["known_at"] == "TIMESTAMP WITH TIME ZONE"


def test_v1_database_exports_only_the_phase1_files(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb", phase2=False)
    out = tmp_path / "release"
    manifest = run_export(db, out)

    assert [entry.table for entry in manifest.files] == [table.name for table in EXPORTS]
    for table in PHASE2_EXPORTS:
        assert not (out / f"{table.name}.parquet").exists()
    assert all("text_bytes" not in entry for entry in manifest_json(out)["files"])


def test_phase1_files_are_byte_identical_with_and_without_phase2_tables(
    tmp_path: Path,
) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb", phase2=False)
    before = run_export(db, tmp_path / "release-v1")
    before_bytes = {
        entry.file: (tmp_path / "release-v1" / entry.file).read_bytes() for entry in before.files
    }

    con = duckdb.connect(str(db))
    try:
        add_phase2_tables(con)
    finally:
        con.close()
    after = run_export(db, tmp_path / "release-phase2")

    for entry in before.files:
        written = (tmp_path / "release-phase2" / entry.file).read_bytes()
        assert written == before_bytes[entry.file], entry.file
        assert hashlib.sha256(written).hexdigest() == entry.sha256
    assert [entry.table for entry in after.files][-3:] == [table.name for table in PHASE2_EXPORTS]


def test_exporting_the_same_database_twice_is_byte_identical(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    first = run_export(db, tmp_path / "release-a")
    second = run_export(db, tmp_path / "release-b")

    for entry in first.files:
        one = (tmp_path / "release-a" / entry.file).read_bytes()
        two = (tmp_path / "release-b" / entry.file).read_bytes()
        assert one == two, entry.file
        assert hashlib.sha256(one).hexdigest() == entry.sha256
    assert [entry.sha256 for entry in first.files] == [entry.sha256 for entry in second.files]

    first_json = manifest_json(tmp_path / "release-a")
    second_json = manifest_json(tmp_path / "release-b")
    assert first_json.pop("build_timestamp") != ""
    assert second_json.pop("build_timestamp") != ""
    assert first_json == second_json


def test_bytes_do_not_depend_on_insertion_order(tmp_path: Path) -> None:
    one = build_db(tmp_path / "one.duckdb")
    other = build_db(tmp_path / "other.duckdb", reverse=True)
    first = run_export(one, tmp_path / "release-one")
    second = run_export(other, tmp_path / "release-two")

    assert [entry.sha256 for entry in first.files] == [entry.sha256 for entry in second.files]
    for entry in first.files:
        assert (tmp_path / "release-one" / entry.file).read_bytes() == (
            tmp_path / "release-two" / entry.file
        ).read_bytes(), entry.file


def test_exported_rows_follow_the_key_order(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    out = tmp_path / "release"
    run_export(db, out)

    con = duckdb.connect(str(db), read_only=True)
    try:
        for table in PHASE2_EXPORTS:
            parquet = (out / f"{table.name}.parquet").as_posix()
            expected = con.execute(
                f"SELECT * FROM {table.source} ORDER BY {table.order_by}"
            ).fetchall()
            written = con.execute(f"SELECT * FROM parquet_scan('{parquet}')").fetchall()
            assert written == expected, table.name
    finally:
        con.close()


def test_filings_text_manifest_reports_uncompressed_text_bytes(tmp_path: Path) -> None:
    rows = default_filings_text_rows()
    db = build_db(tmp_path / "edgarpit.duckdb", filings_text_rows=rows)
    out = tmp_path / "release"
    run_export(db, out)

    observed = entries_by_table(out)["filings_text"]["text_bytes"]
    expected_bytes = sum(len(str(row[8]).encode("utf-8")) for row in rows)
    expected_chars = sum(int(str(row[10])) for row in rows)
    assert observed == expected_bytes
    assert expected_bytes > expected_chars  # the sections carry non-ASCII text
    assert "text_bytes" not in entries_by_table(out)["fx_daily"]


def test_filings_text_uses_a_smaller_row_group_size(tmp_path: Path) -> None:
    rows = [
        text_row(APPLE_8K, f"doc-{number}.htm", "unsegmented", 1, f"Section {number}.")
        for number in range(2 * FILINGS_TEXT_ROW_GROUP_SIZE + 1)
    ]
    db = build_db(tmp_path / "edgarpit.duckdb", filings_text_rows=rows)
    out = tmp_path / "release"
    run_export(db, out)

    assert FILINGS_TEXT_ROW_GROUP_SIZE == 10_000
    assert FILINGS_TEXT_ROW_GROUP_SIZE < PARQUET_ROW_GROUP_SIZE
    text_file = pq.ParquetFile(out / "filings_text.parquet")
    assert text_file.metadata.num_rows == len(rows)
    assert text_file.metadata.num_row_groups >= 2
    assert pq.ParquetFile(out / "filings.parquet").metadata.num_row_groups == 1


def test_phase2_parquet_rereads_equal_the_source_tables(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    out = tmp_path / "release"
    run_export(db, out)

    con = duckdb.connect(str(db), read_only=True)
    try:
        for table in PHASE2_EXPORTS:
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
    finally:
        con.close()


def test_filings_text_is_read_from_the_main_schema(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb", phase2=False)
    con = duckdb.connect(str(db))
    try:
        con.execute(duckdb_ddl("filings_text", "raw"))
        rows = default_filings_text_rows()
        placeholders = ", ".join("?" for _ in rows[0])
        con.executemany(f"INSERT INTO raw.filings_text VALUES ({placeholders})", rows)
    finally:
        con.close()
    out = tmp_path / "release"
    manifest = run_export(db, out)

    assert "filings_text" not in [entry.table for entry in manifest.files]
    assert not (out / "filings_text.parquet").exists()


def test_a_phase2_view_is_exported(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb", phase2=False)
    con = duckdb.connect(str(db))
    try:
        con.execute(duckdb_ddl("filings_text", "raw"))
        rows = default_filings_text_rows()
        placeholders = ", ".join("?" for _ in rows[0])
        con.executemany(f"INSERT INTO raw.filings_text VALUES ({placeholders})", rows)
        con.execute("CREATE VIEW main.filings_text AS SELECT * FROM raw.filings_text")
    finally:
        con.close()
    out = tmp_path / "release"
    run_export(db, out)

    entries = entries_by_table(out)
    assert entries["filings_text"]["rows"] == len(rows)
    assert (out / "filings_text.parquet").is_file()


def test_empty_phase2_tables_are_exported_with_zero_rows(tmp_path: Path) -> None:
    db = build_db(
        tmp_path / "edgarpit.duckdb",
        filings_text_rows=[],
        fx_rows=[],
        usd_rows=[],
    )
    out = tmp_path / "release"
    run_export(db, out)

    entries = entries_by_table(out)
    for name in ("fundamentals_pit_usd", "filings_text", "fx_daily"):
        assert entries[name]["rows"] == 0
        assert (out / f"{name}.parquet").is_file()
    assert entries["filings_text"]["text_bytes"] == 0


def test_extra_fundamentals_pit_usd_columns_flow_through(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    con = duckdb.connect(str(db))
    try:
        con.execute("ALTER TABLE main.fundamentals_pit_usd ADD COLUMN unconverted_reason VARCHAR")
        con.execute("UPDATE main.fundamentals_pit_usd SET unconverted_reason = 'no_rate'")
    finally:
        con.close()
    out = tmp_path / "release"
    run_export(db, out)

    assert entries_by_table(out)["fundamentals_pit_usd"]["schema"]["unconverted_reason"] == (
        "VARCHAR"
    )
    con = duckdb.connect(str(db), read_only=True)
    try:
        reasons = con.execute(
            "SELECT DISTINCT unconverted_reason "
            f"FROM parquet_scan('{(out / 'fundamentals_pit_usd.parquet').as_posix()}')"
        ).fetchall()
    finally:
        con.close()
    assert reasons == [("no_rate",)]


def test_cli_export_lists_the_phase2_files(tmp_path: Path) -> None:
    db = build_db(tmp_path / "edgarpit.duckdb")
    out = tmp_path / "release"
    result = RUNNER.invoke(app, ["export", "--db", str(db), "--out", str(out)])

    assert result.exit_code == 0, result.output
    for table in PHASE2_EXPORTS:
        assert f"{table.name}.parquet" in result.stdout
    assert len(manifest_json(out)["files"]) == len(EXPORTS) + len(PHASE2_EXPORTS)
