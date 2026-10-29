"""Tests for :mod:`edgarpit.load` (validated staging and reference tables)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import doubles
import duckdb
import polars as pl
import pytest
import yaml
from pandera.errors import SchemaError

from edgarpit import __version__
from edgarpit.concepts import DEFAULT_CONCEPT_MAP_PATH, ConceptSpec, load_concept_map
from edgarpit.load import (
    BUILD_INFO_NAME,
    REFERENCE_TYPES,
    _deduplicate_filings,
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
RY_FY2023_40F = "0001193125-23-285639"
RY_FY2023_ACCEPTANCE = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
APPLE_CIK = "0000320193"
APPLE_8K = "0000320193-25-000071"

# The endpoints a Phase 1 cache holds. The committed fixture cache also carries
# the 8-K text and FX series (decision A-44), so the tests that need a cache
# without them copy only these.
PHASE1_ENDPOINTS = ("submissions", "submissions_page", "companyfacts", "headers")


def phase1_cache(source: Path, target: Path) -> Path:
    """Copy the Phase 1 endpoints of ``source`` into ``target`` and return it.

    The result has the decision A-4 layout under ``target/raw`` and no filing
    index, document or FX entry, which is what a cache built before the Phase 2
    fetch commands look like.
    """
    for endpoint in PHASE1_ENDPOINTS:
        shutil.copytree(source / "raw" / endpoint, target / "raw" / endpoint)
    return target


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
        filings_text=frames["filings_text"],
        fx_daily=frames["fx_daily"],
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
            filings_text=frames["filings_text"],
            fx_daily=frames["fx_daily"],
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
            filings_text=empty_frame("filings_text"),
            fx_daily=empty_frame("fx_daily"),
        )

        assert con.execute("SELECT count(*) FROM raw.filings").fetchone() == (3,)
        assert con.execute("SELECT count(*) FROM raw.events_8k").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM raw.facts_raw").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM raw.filings_text").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM raw.fx_daily").fetchone() == (0,)
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
                filings_text=empty_frame("filings_text"),
                fx_daily=empty_frame("fx_daily"),
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
                filings_text=frames["filings_text"],
                fx_daily=frames["fx_daily"],
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


def test_reference_tables_read_the_public_specs_iterator(tmp_path: Path) -> None:
    """The loader consumes ``ConceptMap.specs()``, not the private mapping.

    A map-shaped object that offers only :meth:`specs` still loads, so the
    loader cannot be reaching into ``_by_field`` behind the public API.
    """

    class SpecsOnly:
        def specs(self) -> list[ConceptSpec]:
            return [
                ConceptSpec(
                    field="revenue",
                    kind="duration",
                    unit_family="currency",
                    us_gaap=["Revenues"],
                    ifrs_full=["Revenue"],
                )
            ]

    con = connect(tmp_path)
    try:
        load_reference_tables(con, concept_map=SpecsOnly())
        assert con.execute(
            "SELECT field, taxonomy, concept, concept_rank, kind, unit_family FROM raw.concept_map"
        ).fetchall() == [
            ("revenue", "us-gaap", "Revenues", 1, "duration", "currency"),
            ("revenue", "ifrs-full", "Revenue", 1, "duration", "currency"),
        ]
    finally:
        con.close()


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
    assert info["tables"]["main.fundamentals_pit"] > 0
    assert info["tables"]["main.ratios_pit"] > 0
    assert info["dbt"].startswith("PASS=")
    assert "ERROR=0" in info["dbt"]
    assert {"raw.filings", "raw.events_8k", "raw.facts_raw"} <= set(info["tables"])
    assert {"main.fundamentals_pit", "main.ratios_pit", "main.events_8k", "main.filings"} <= set(
        info["tables"]
    )


def test_build_universe_is_the_cached_issuers_with_their_tickers(tmp_path: Path) -> None:
    """Every cached issuer is in ``raw.universe`` and every fact carries its ticker.

    The fixture cache holds three issuers; a build that loaded
    ``config/universe.csv`` wholesale would list 40 and leave the ticker of any
    issuer that file does not carry null.
    """
    db = build_from_raw(FIXTURE_RAW, tmp_path / "build")
    cached = sorted(
        entry.name
        for entry in (FIXTURE_RAW / "raw" / "submissions").iterdir()
        if entry.is_dir() and entry.name.isdigit()
    )

    con = duckdb.connect(str(db))
    try:
        assert con.execute("SELECT cik FROM raw.universe ORDER BY cik").fetchall() == [
            (cik,) for cik in cached
        ]
        assert con.execute(
            "SELECT count(*) FROM raw.universe WHERE ticker IS NULL OR ticker = ''"
        ).fetchone() == (0,)
        assert con.execute(
            "SELECT count(*) FROM main.fundamentals_pit WHERE ticker IS NULL"
        ).fetchone() == (0,)
        assert con.execute(
            "SELECT count(*) FROM main.filings WHERE ticker IS NULL"
        ).fetchone() == (0,)
    finally:
        con.close()


def test_a_phase1_cache_builds_with_the_phase2_tables_empty(tmp_path: Path) -> None:
    """A cache without filing indexes or FX keeps ``build --offline`` working.

    Decision A-37: the database is a function of the cache, so the two Phase 2
    raw tables exist and are empty, their dbt models exist and are empty, and
    the USD conversion model still covers every Phase 1 fundamental. The cache
    is the Phase 1 endpoints of the fixtures, which is what a cache written
    before the Phase 2 fetch commands holds (the committed cache also carries
    the 8-K text and the FX series).
    """
    out = tmp_path / "build"
    db = build_from_raw(phase1_cache(FIXTURE_RAW, tmp_path / "cache"), out)

    con = duckdb.connect(str(db))
    try:
        con.execute("SET TimeZone='UTC'")
        assert con.execute("SELECT count(*) FROM raw.filings_text").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM raw.fx_daily").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM main.filings_text").fetchone() == (0,)
        assert (
            con.execute("SELECT count(*) FROM main.fundamentals_pit_usd").fetchone()
            == con.execute("SELECT count(*) FROM main.fundamentals_pit").fetchone()
        )
        # Without an FXUSDCAD series no CAD row converts: the reasons are the
        # null of a converted USD row, `no_rate` for CAD and
        # `currency_out_of_scope` for share units.
        reasons = dict(
            con.execute(
                "SELECT unconverted_reason, count(*) FROM main.fundamentals_pit_usd "
                "GROUP BY unconverted_reason ORDER BY unconverted_reason"
            ).fetchall()
        )
        assert reasons.get(None, 0) > 0
        assert reasons.get("no_rate", 0) > 0
        assert set(reasons) <= {None, "no_rate", "currency_out_of_scope"}
    finally:
        con.close()


def test_build_from_raw_ingests_cached_text_and_fx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache with a filing index and an FX series fills both tables (A-37)."""
    cache = phase1_cache(FIXTURE_RAW, tmp_path / "cache")
    index_dir = cache / "raw" / "filing_index" / APPLE_CIK / APPLE_8K
    index_dir.mkdir(parents=True)
    (index_dir / "20261028T000000000000Z.html").write_text("<html></html>", encoding="utf-8")
    fx_dir = cache / "raw" / "fx" / "_" / "FXUSDCAD"
    fx_dir.mkdir(parents=True)
    (fx_dir / "20261028T000000000000Z.json").write_text(
        '{"observations": [{"d": "2023-10-31", "FXUSDCAD": {"v": "1.3659"}}]}',
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_ingest_text(
        raw: object, cik: str, accessions: list[str], filings: pl.DataFrame
    ) -> pl.DataFrame:
        seen["text_call"] = (cik, list(accessions), filings["accession"].to_list())
        return doubles.filings_text_frame()

    def fake_parse_valet(doc: dict[str, object], series: str) -> pl.DataFrame:
        seen["fx_call"] = (series, doc)
        return doubles.fx_daily_frame()

    monkeypatch.setattr("edgarpit.load.ingest_text", fake_ingest_text)
    monkeypatch.setattr("edgarpit.load.parse_valet", fake_parse_valet)

    db = build_from_raw(cache, tmp_path / "build")

    assert seen["text_call"] == (APPLE_CIK, [APPLE_8K], [APPLE_8K])
    assert seen["fx_call"] == (
        "FXUSDCAD",
        {"observations": [{"d": "2023-10-31", "FXUSDCAD": {"v": "1.3659"}}]},
    )
    info = json.loads((tmp_path / "build" / BUILD_INFO_NAME).read_text(encoding="utf-8"))
    assert info["tables"]["raw.filings_text"] == 3
    assert info["tables"]["raw.fx_daily"] == 3
    assert info["tables"]["main.filings_text"] == 3
    con = duckdb.connect(str(db))
    try:
        con.execute("SET TimeZone='UTC'")
        assert con.execute("SELECT count(*) FROM raw.filings_text").fetchone() == (3,)
        assert con.execute("SELECT count(*) FROM raw.fx_daily").fetchone() == (3,)
        assert con.execute("SELECT DISTINCT ticker FROM main.filings_text").fetchall() == [
            ("AAPL",)
        ]
        assert con.execute(
            "SELECT count(*) FROM raw.fx_daily WHERE known_at < rate_date"
        ).fetchone() == (0,)
    finally:
        con.close()


def test_cross_issuer_duplicate_accessions_keep_the_filer_row() -> None:
    """When the accession prefix names an issuer, that issuer's row wins."""
    filings = pl.DataFrame(
        {
            "cik": ["0001141391", "0000019617", "0000019617"],
            "accession": [
                "0000019617-04-000164",
                "0000019617-04-000164",
                "0000019617-05-000155",
            ],
        }
    )
    events = pl.DataFrame(
        {
            "cik": ["0001141391", "0000019617"],
            "accession": ["0000019617-04-000164", "0000019617-04-000164"],
            "item_code": ["5", "5"],
        }
    )

    deduplicated, deduplicated_events = _deduplicate_filings(filings, events)

    assert deduplicated.height == 2
    assert deduplicated["accession"].n_unique() == 2
    kept = deduplicated.filter(pl.col("accession") == "0000019617-04-000164")
    assert kept["cik"].to_list() == ["0000019617"]
    assert deduplicated_events.height == 1
    assert deduplicated_events["cik"].to_list() == ["0000019617"]


def test_cross_issuer_duplicate_accessions_fall_back_to_the_smallest_cik() -> None:
    """A filing agent prefixes the accession, so no issuer owns it; CIK order wins."""
    filings = pl.DataFrame(
        {
            "cik": ["0001594805", "0000009631"],
            "accession": ["0001085146-24-000941", "0001085146-24-000941"],
        }
    )
    events = pl.DataFrame(
        {
            "cik": ["0001594805", "0000009631", "0000009631"],
            "accession": [
                "0001085146-24-000941",
                "0001085146-24-000941",
                "0001085146-24-000941",
            ],
            "item_code": ["2.02", "2.02", "5.02"],
        }
    )

    deduplicated, deduplicated_events = _deduplicate_filings(filings, events)

    assert deduplicated["cik"].to_list() == ["0000009631"]
    assert deduplicated_events["cik"].unique().to_list() == ["0000009631"]
    assert sorted(deduplicated_events["item_code"].to_list()) == ["2.02", "5.02"]
