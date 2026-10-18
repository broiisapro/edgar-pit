"""Tests for :mod:`edgarpit.load` (validated staging and reference tables)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest
import yaml
from pandera.errors import SchemaError

from edgarpit import __version__
from edgarpit.concepts import DEFAULT_CONCEPT_MAP_PATH, load_concept_map
from edgarpit.load import (
    BUILD_INFO_NAME,
    REFERENCE_TYPES,
    build_from_raw,
    load_reference_tables,
    load_tables,
)
from edgarpit.schemas import COLUMNS, DUCKDB_TYPES

REPO_ROOT = Path(__file__).resolve().parents[1]
STAGING_DIR = Path(__file__).parent / "fixtures" / "staging"
FIXTURE_RAW = Path(__file__).parent / "fixtures" / "sec"
UNIVERSE_CSV = REPO_ROOT / "config" / "universe.csv"

RY_CIK = "0001000275"
APPLE_CIK = "0000320193"
SHOP_CIK = "0001594805"
RY_FY2023_40F = "0001193125-23-285639"
RY_FY2023_ACCEPTANCE = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)


def polars_type(dtype: str) -> Any:
    return {
        "VARCHAR(10)": pl.String,
        "VARCHAR": pl.String,
        "DATE": pl.Date,
        "TIMESTAMPTZ": pl.Datetime("us", "UTC"),
        "BOOLEAN": pl.Boolean,
        "DOUBLE": pl.Float64,
        "INTEGER": pl.Int64,
    }[dtype]


def frame_schema(table: str) -> dict[str, Any]:
    return {column: polars_type(dtype) for column, dtype in DUCKDB_TYPES[table].items()}


def staging_frame(table: str) -> pl.DataFrame:
    return pl.read_csv(
        STAGING_DIR / f"{table}.csv",
        schema_overrides=frame_schema(table),
    ).select(COLUMNS[table])


def empty_frame(table: str) -> pl.DataFrame:
    return pl.DataFrame(schema=frame_schema(table)).select(COLUMNS[table])


def connect(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(tmp_path / "edgarpit.duckdb"))


def raw_table_names(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw'"
    ).fetchall()
    return {row[0] for row in rows}


def load_staging(con: duckdb.DuckDBPyConnection) -> dict[str, pl.DataFrame]:
    frames = {table: staging_frame(table) for table in COLUMNS}
    load_tables(
        con,
        filings=frames["filings"],
        events_8k=frames["events_8k"],
        facts_raw=frames["facts_raw"],
    )
    return frames


def test_staging_round_trip(tmp_path: Path) -> None:
    frames = {table: staging_frame(table) for table in COLUMNS}
    con = connect(tmp_path)
    try:
        load_tables(
            con,
            filings=frames["filings"],
            events_8k=frames["events_8k"],
            facts_raw=frames["facts_raw"],
        )
        assert con.execute("SELECT current_setting('TimeZone')").fetchone() == ("UTC",)
        for table, frame in frames.items():
            restored = con.execute(f"SELECT * FROM raw.{table}").pl().select(frame.columns)
            restored = restored.cast(frame.schema)
            assert restored.sort(frame.columns).equals(frame.sort(frame.columns)), table
    finally:
        con.close()


def test_load_tables_replaces_previous_contents(tmp_path: Path) -> None:
    con = connect(tmp_path)
    try:
        load_staging(con)
        subset = staging_frame("filings").head(3)
        load_tables(
            con,
            filings=subset,
            events_8k=empty_frame("events_8k"),
            facts_raw=empty_frame("facts_raw"),
        )

        assert con.execute("SELECT count(*) FROM raw.filings").fetchone() == (3,)
        assert con.execute("SELECT count(*) FROM raw.events_8k").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM raw.facts_raw").fetchone() == (0,)
        restored = con.execute("SELECT * FROM raw.filings").pl().cast(frame_schema("filings"))
        assert restored.sort("accession").equals(subset.sort("accession"))
    finally:
        con.close()


def test_invalid_frame_is_rejected_with_nothing_written(tmp_path: Path) -> None:
    con = connect(tmp_path)
    try:
        bad_filings = staging_frame("filings").with_columns(pl.lit("32193").alias("cik"))
        with pytest.raises(SchemaError):
            load_tables(
                con,
                filings=bad_filings,
                events_8k=empty_frame("events_8k"),
                facts_raw=empty_frame("facts_raw"),
            )

        assert raw_table_names(con) == set()
    finally:
        con.close()


def test_invalid_frame_leaves_previous_tables_untouched(tmp_path: Path) -> None:
    con = connect(tmp_path)
    try:
        frames = load_staging(con)
        bad_facts = frames["facts_raw"].with_columns(pl.lit("XYZ").alias("taxonomy"))

        with pytest.raises(SchemaError):
            load_tables(
                con,
                filings=frames["filings"],
                events_8k=frames["events_8k"],
                facts_raw=bad_facts,
            )

        count = con.execute("SELECT count(*) FROM raw.facts_raw").fetchone()
        assert count == (frames["facts_raw"].height,)
        taxonomy = con.execute(
            "SELECT count(*) FROM raw.facts_raw WHERE taxonomy = 'XYZ'"
        ).fetchone()
        assert taxonomy == (0,)
    finally:
        con.close()


def test_timestamps_stay_utc_instants(tmp_path: Path) -> None:
    con = connect(tmp_path)
    try:
        load_staging(con)
        assert con.execute("SELECT current_setting('TimeZone')").fetchone() == ("UTC",)
        assert con.execute("SELECT typeof(acceptance_ts) FROM raw.filings LIMIT 1").fetchone() == (
            "TIMESTAMP WITH TIME ZONE",
        )
        row = con.execute(
            "SELECT acceptance_ts FROM raw.filings WHERE accession = ?", [RY_FY2023_40F]
        ).fetchone()
        assert row == (RY_FY2023_ACCEPTANCE,)
    finally:
        con.close()


def expected_concept_rows() -> list[tuple[str, str, str, int, str, str]]:
    document = yaml.safe_load(DEFAULT_CONCEPT_MAP_PATH.read_text(encoding="utf-8"))
    rows: list[tuple[str, str, str, int, str, str]] = []
    for field, definition in document["fields"].items():
        for taxonomy in ("us-gaap", "ifrs-full"):
            for rank, concept in enumerate(definition[taxonomy], start=1):
                rows.append(
                    (field, taxonomy, concept, rank, definition["kind"], definition["unit_family"])
                )
    return rows


def test_reference_tables_match_the_yaml_and_csv(tmp_path: Path) -> None:
    con = connect(tmp_path)
    try:
        load_reference_tables(con)

        assert con.execute("SELECT typeof(cik) FROM raw.universe LIMIT 1").fetchone() == (
            "VARCHAR",
        )
        concept_rows = con.execute(
            "SELECT field, taxonomy, concept, concept_rank, kind, unit_family FROM raw.concept_map"
        ).fetchall()
        assert concept_rows == expected_concept_rows()

        universe = pl.read_csv(UNIVERSE_CSV, schema_overrides={"cik": pl.String})
        universe_rows = con.execute(
            "SELECT ticker, cik, name, country, filer_type FROM raw.universe"
        ).fetchall()
        assert universe_rows == [tuple(row) for row in universe.iter_rows()]
        assert ("RY", RY_CIK, "ROYAL BANK OF CANADA", "CA", "40-F") in universe_rows
    finally:
        con.close()


def test_reference_tables_honour_injected_map_and_csv(tmp_path: Path) -> None:
    map_path = tmp_path / "concept_map.yaml"
    map_path.write_text(
        "fields:\n"
        "  revenue:\n"
        "    kind: duration\n"
        "    unit_family: currency\n"
        "    us-gaap:\n"
        "      - Revenues\n"
        "      - SalesRevenueNet\n"
        "    ifrs-full:\n"
        "      - Revenue\n",
        encoding="utf-8",
    )
    universe_path = tmp_path / "universe.csv"
    universe_path.write_text(
        "ticker,cik,name,country,filer_type\nRY,0001000275,ROYAL BANK,CA,40-F\n",
        encoding="utf-8",
    )
    con = connect(tmp_path)
    try:
        load_reference_tables(
            con, concept_map=load_concept_map(map_path), universe_csv=universe_path
        )

        assert con.execute(
            "SELECT field, taxonomy, concept, concept_rank, kind, unit_family FROM raw.concept_map"
        ).fetchall() == [
            ("revenue", "us-gaap", "Revenues", 1, "duration", "currency"),
            ("revenue", "us-gaap", "SalesRevenueNet", 2, "duration", "currency"),
            ("revenue", "ifrs-full", "Revenue", 1, "duration", "currency"),
        ]
        assert con.execute(
            "SELECT ticker, cik, name, country, filer_type FROM raw.universe"
        ).fetchall() == [("RY", RY_CIK, "ROYAL BANK", "CA", "40-F")]
    finally:
        con.close()


def test_reference_tables_replace_previous_contents(tmp_path: Path) -> None:
    con = connect(tmp_path)
    try:
        load_reference_tables(con)
        con.execute("INSERT INTO raw.universe VALUES ('XX', '0000000001', 'X', 'US', '10-K')")

        load_reference_tables(con)

        universe = pl.read_csv(UNIVERSE_CSV, schema_overrides={"cik": pl.String})
        count = con.execute("SELECT count(*) FROM raw.universe").fetchone()
        assert count == (universe.height,)
        assert con.execute("SELECT count(*) FROM raw.universe WHERE ticker = 'XX'").fetchone() == (
            0,
        )
    finally:
        con.close()


def test_malformed_universe_cik_is_rejected(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.csv"
    universe_path.write_text(
        "ticker,cik,name,country,filer_type\nRY,1000275,ROYAL BANK,CA,40-F\n",
        encoding="utf-8",
    )
    con = connect(tmp_path)
    try:
        with pytest.raises(ValueError, match="not 10 digits"):
            load_reference_tables(con, universe_csv=universe_path)

        assert raw_table_names(con) == set()
    finally:
        con.close()


def test_reference_table_shapes_are_frozen() -> None:
    assert REFERENCE_TYPES["concept_map"] == {
        "field": "VARCHAR",
        "taxonomy": "VARCHAR",
        "concept": "VARCHAR",
        "concept_rank": "INTEGER",
        "kind": "VARCHAR",
        "unit_family": "VARCHAR",
    }
    assert REFERENCE_TYPES["universe"] == {
        "ticker": "VARCHAR",
        "cik": "VARCHAR(10)",
        "name": "VARCHAR",
        "country": "VARCHAR",
        "filer_type": "VARCHAR",
    }


def test_build_from_raw_writes_build_info_next_to_the_database(tmp_path: Path) -> None:
    out = tmp_path / "build"
    db = build_from_raw(FIXTURE_RAW, out)

    assert db == out / "edgarpit.duckdb"
    info = json.loads((out / BUILD_INFO_NAME).read_text(encoding="utf-8"))
    assert info["package_version"] == __version__
    assert info["raw_cache"] == str(FIXTURE_RAW.resolve())

    manifest_text = (FIXTURE_RAW / "raw" / "manifest.jsonl").read_text(encoding="utf-8")
    manifest = [json.loads(line) for line in manifest_text.splitlines() if line.strip()]
    stamps = [row["fetched_at"] for row in manifest]
    assert info["sec_fetch_range"] == {"min": min(stamps), "max": max(stamps)}

    expected_ciks = {row["cik"] for row in manifest if row.get("cik")}
    assert set(info["documents"]) == expected_ciks
    assert info["documents"][RY_CIK]["submissions"] == 1
    assert info["documents"][RY_CIK]["companyfacts"] == 1
    assert info["documents"][RY_CIK]["headers"] > 0

    assert info["tables"]["raw.filings"] > 0
    assert info["tables"]["raw.facts_raw"] > 0
    assert info["tables"]["raw.universe"] == 40
    assert info["tables"]["main.fundamentals_pit"] > 0
    assert info["tables"]["main.ratios_pit"] > 0
    assert info["dbt"].startswith("PASS=")
    assert "ERROR=0" in info["dbt"]
    assert {"raw.filings", "raw.events_8k", "raw.facts_raw"} <= set(info["tables"])
    assert {"main.fundamentals_pit", "main.ratios_pit", "main.events_8k", "main.filings"} <= set(
        info["tables"]
    )
