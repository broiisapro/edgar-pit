"""Validate staged DataFrames and load them into DuckDB (decisions A-8, A-16, A-20).

Two loaders write the ``raw`` schema of ``<out_dir>/edgarpit.duckdb``:
:func:`load_tables` replaces the Pandera-validated staging tables from the
ingest frames, and :func:`load_reference_tables` replaces ``raw.concept_map``
and ``raw.universe`` from ``config/concept_map.yaml`` and
``config/universe.csv``. Both set the connection's session timezone to UTC
(decision A-5) and both are transactional: a validation error or a failed
insert leaves the previously loaded tables untouched.

:func:`build_from_raw` runs the whole offline pipeline for a raw cache:
ingest every cached issuer, load the frames and the reference tables, then run
the dbt project with ``dbt build``. ``python -m edgarpit.load --raw DIR --out
DIR`` does the same and prints the loaded row counts and the dbt summary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import polars as pl
from polars.datatypes import DataType, DataTypeClass

from edgarpit.concepts import (
    DEFAULT_CONCEPT_MAP_PATH,
    ConceptMap,
    load_concept_map,
)
from edgarpit.ingest.companyfacts import ingest_facts
from edgarpit.ingest.submissions import ingest_filings, load_item_labels
from edgarpit.rawcache import RawCache
from edgarpit.schemas import COLUMNS, SCHEMAS, duckdb_ddl

if TYPE_CHECKING:
    from dbt.cli.main import dbtRunnerResult

DB_NAME = "edgarpit.duckdb"
DBT_TARGET_DIRNAME = "dbt-target"
TRANSFORM_DIR = Path(__file__).resolve().parents[1] / "transform"

DEFAULT_UNIVERSE_CSV = Path(__file__).resolve().parents[1] / "config" / "universe.csv"

_CIK_DIR = re.compile(r"^\d{10}$")

REFERENCE_TYPES: dict[str, dict[str, str]] = {
    "concept_map": {
        "field": "VARCHAR",
        "taxonomy": "VARCHAR",
        "concept": "VARCHAR",
        "concept_rank": "INTEGER",
        "kind": "VARCHAR",
        "unit_family": "VARCHAR",
    },
    "universe": {
        "ticker": "VARCHAR",
        "cik": "VARCHAR(10)",
        "name": "VARCHAR",
        "country": "VARCHAR",
        "filer_type": "VARCHAR",
    },
}

_UNIVERSE_COLUMNS = list(REFERENCE_TYPES["universe"])

_POLARS_TYPES: dict[str, DataType | DataTypeClass] = {
    "VARCHAR(10)": pl.String,
    "VARCHAR": pl.String,
    "INTEGER": pl.Int64,
}


@contextmanager
def _transaction(con: duckdb.DuckDBPyConnection) -> Iterator[None]:
    con.execute("BEGIN TRANSACTION")
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _insert_frame(con: duckdb.DuckDBPyConnection, table: str, frame: pl.DataFrame) -> None:
    con.register("_load_frame", frame)
    try:
        con.execute(f"INSERT INTO raw.{table} SELECT * FROM _load_frame")
    finally:
        con.unregister("_load_frame")


def _replace_table(con: duckdb.DuckDBPyConnection, table: str, frame: pl.DataFrame) -> None:
    con.execute(f"DROP TABLE IF EXISTS raw.{table}")
    con.execute(duckdb_ddl(table))
    _insert_frame(con, table, frame)


def _reference_ddl(table: str) -> str:
    if table not in REFERENCE_TYPES:
        known = ", ".join(sorted(REFERENCE_TYPES))
        raise ValueError(f"unknown reference table {table!r}; expected one of {known}")
    columns = ",\n    ".join(f"{name} {dtype}" for name, dtype in REFERENCE_TYPES[table].items())
    return f"CREATE TABLE raw.{table} (\n    {columns}\n)"


def _concept_map_frame(concept_map: ConceptMap) -> pl.DataFrame:
    """Flatten a validated concept map into the reference-table rows.

    One row per ``(field, taxonomy, concept)`` in field order, ``us-gaap``
    before ``ifrs-full`` and rank 1 first. ``ConceptMap`` exposes no public
    iterator over its ordered ``ConceptSpec`` objects, so the map's protected
    ``_by_field`` mapping is read directly; a public accessor would remove
    that coupling.
    """
    rows: list[tuple[str, str, str, int, str, str]] = []
    for field, spec in concept_map._by_field.items():
        for taxonomy, concepts in (("us-gaap", spec.us_gaap), ("ifrs-full", spec.ifrs_full)):
            for rank, concept in enumerate(concepts, start=1):
                rows.append((field, taxonomy, concept, rank, spec.kind, spec.unit_family))
    schema = {name: _POLARS_TYPES[dtype] for name, dtype in REFERENCE_TYPES["concept_map"].items()}
    return pl.DataFrame(rows, schema=schema, orient="row")


def _read_universe(path: Path) -> pl.DataFrame:
    """Read ``config/universe.csv``: five columns, zero-padded CIK strings."""
    try:
        frame = pl.read_csv(path, schema_overrides={"cik": pl.String})
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"universe CSV not found: {path}") from exc
    missing = [column for column in _UNIVERSE_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"universe CSV {path} is missing columns {missing}")
    frame = frame.select(_UNIVERSE_COLUMNS)
    malformed = frame.filter(~pl.col("cik").str.contains(r"^\d{10}$"))
    if malformed.height:
        ciks = malformed.get_column("cik").to_list()
        raise ValueError(f"universe CSV {path} has CIKs that are not 10 digits: {ciks}")
    return frame


def load_tables(
    con: duckdb.DuckDBPyConnection,
    *,
    filings: pl.DataFrame,
    events_8k: pl.DataFrame,
    facts_raw: pl.DataFrame,
) -> None:
    """Validate each frame with its Pandera schema and replace the ``raw.*`` tables.

    ``filings``, ``events_8k`` and ``facts_raw`` must be valid under
    :class:`edgarpit.schemas.FilingsSchema`,
    :class:`edgarpit.schemas.Events8kSchema` and
    :class:`edgarpit.schemas.FactsRawSchema`. The connection's timezone is set
    to UTC before loading (decision A-5). Each frame is validated before any
    statement is executed, then all three tables are dropped, recreated from
    :func:`edgarpit.schemas.duckdb_ddl` and filled in one transaction, so an
    invalid frame raises :class:`pandera.errors.SchemaError` with nothing
    written. All timestamps are timezone-aware UTC instants.
    """
    frames = {"filings": filings, "events_8k": events_8k, "facts_raw": facts_raw}
    for table, frame in frames.items():
        SCHEMAS[table].validate(frame)
    con.execute("SET TimeZone='UTC'")
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    with _transaction(con):
        for table, frame in frames.items():
            _replace_table(con, table, frame)


def load_reference_tables(
    con: duckdb.DuckDBPyConnection,
    *,
    concept_map: ConceptMap | None = None,
    universe_csv: Path | None = None,
) -> None:
    """Replace ``raw.concept_map`` and ``raw.universe`` with the reference data.

    ``raw.concept_map(field, taxonomy, concept, concept_rank, kind,
    unit_family)`` is flattened from ``concept_map`` (default:
    :func:`edgarpit.concepts.load_concept_map` on ``config/concept_map.yaml``);
    ``concept_rank`` is 1-based, 1 being the field's preferred concept.
    ``raw.universe(ticker, cik, name, country, filer_type)`` is read from
    ``universe_csv`` (default ``config/universe.csv``) with ``cik`` kept as a
    zero-padded 10-digit string. Both tables are dropped, recreated and filled
    in one transaction; a malformed map or CSV raises before anything is
    written. The connection's timezone is set to UTC (decision A-5) so
    ``concept_rank`` etc. load deterministically; these tables carry no
    timestamps.
    """
    if concept_map is None:
        concept_map = load_concept_map(DEFAULT_CONCEPT_MAP_PATH)
    concept_frame = _concept_map_frame(concept_map)
    universe_frame = _read_universe(DEFAULT_UNIVERSE_CSV if universe_csv is None else universe_csv)
    con.execute("SET TimeZone='UTC'")
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    with _transaction(con):
        for table, frame in (("concept_map", concept_frame), ("universe", universe_frame)):
            con.execute(f"DROP TABLE IF EXISTS raw.{table}")
            con.execute(_reference_ddl(table))
            _insert_frame(con, table, frame)


def _cached_ciks(raw: RawCache) -> list[str]:
    """Return the sorted CIKs that have a cached submissions document."""
    directory = raw.raw_dir / "submissions"
    ciks = (
        sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.is_dir() and _CIK_DIR.fullmatch(entry.name)
        )
        if directory.is_dir()
        else []
    )
    if not ciks:
        raise FileNotFoundError(f"no cached submissions documents under {directory}")
    return ciks


def _run_dbt(db_path: Path, target_path: Path) -> dbtRunnerResult:
    """Run ``dbt build --target build`` against ``db_path`` and return the result.

    dbt is imported lazily so that importing :mod:`edgarpit.load` stays cheap
    for the non-build commands. The dbt profile reads the database path from
    ``EDGARPIT_DB``, which is set for the duration of the run and restored
    afterwards. Artifacts (including ``run_results.json``) are written under
    ``target_path``; every path given to dbt is absolute.
    """
    from dbt.cli.main import dbtRunner

    previous = os.environ.get("EDGARPIT_DB")
    os.environ["EDGARPIT_DB"] = str(db_path)
    try:
        runner = dbtRunner()
        return runner.invoke(
            [
                "build",
                "--project-dir",
                str(TRANSFORM_DIR),
                "--profiles-dir",
                str(TRANSFORM_DIR),
                "--target",
                "build",
                "--target-path",
                str(target_path),
            ]
        )
    finally:
        if previous is None:
            os.environ.pop("EDGARPIT_DB", None)
        else:
            os.environ["EDGARPIT_DB"] = previous


def build_from_raw(raw_dir: Path, out_dir: Path) -> Path:
    """Build the database from a raw cache directory.

    ``raw_dir`` is a cache root as written by the fetcher (``raw/submissions``,
    ``raw/submissions_page``, ``raw/companyfacts``, ``raw/headers``). Every CIK
    with a cached submissions document, in ascending order, is ingested with
    :func:`edgarpit.ingest.submissions.ingest_filings` (item labels from
    ``config/8k_items.yaml``) and :func:`edgarpit.ingest.companyfacts.ingest_facts`;
    the frames are concatenated, validated and loaded into
    ``<out_dir>/edgarpit.duckdb`` together with the reference tables, the
    connection is closed, and dbt runs ``build --target build`` on the project
    in ``transform/`` with ``EDGARPIT_DB`` pointing at the database. dbt
    artifacts go to ``<out_dir>/dbt-target``.

    Returns the database path. Raises the ingest, Pandera, DuckDB or dbt error
    unchanged, and :class:`RuntimeError` when dbt reports a failed node, so a
    caller never receives a database whose tests did not pass. No network is
    used: everything comes from the cache.
    """
    raw = RawCache(Path(raw_dir))
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / DB_NAME
    item_labels = load_item_labels()
    filings_frames: list[pl.DataFrame] = []
    events_frames: list[pl.DataFrame] = []
    facts_frames: list[pl.DataFrame] = []
    for cik in _cached_ciks(raw):
        filings, events_8k = ingest_filings(raw, cik, item_labels)
        filings_frames.append(filings)
        events_frames.append(events_8k)
        facts_frames.append(ingest_facts(raw, cik))
    con = duckdb.connect(str(db_path))
    try:
        load_tables(
            con,
            filings=pl.concat(filings_frames),
            events_8k=pl.concat(events_frames),
            facts_raw=pl.concat(facts_frames),
        )
        load_reference_tables(con)
    finally:
        con.close()
    result = _run_dbt(db_path, out / DBT_TARGET_DIRNAME)
    if not result.success:
        failed = result.exception if result.exception is not None else "dbt reported failed nodes"
        raise RuntimeError(f"dbt build failed for {db_path}: {failed}")
    return db_path


def _dbt_summary(target_path: Path) -> str:
    """Return the dbt node-status summary of a finished run."""
    results = json.loads((target_path / "run_results.json").read_text(encoding="utf-8"))
    statuses = Counter(result["status"] for result in results["results"])
    passed = statuses["success"] + statuses["pass"]
    failed = statuses["error"] + statuses["fail"]
    return (
        f"PASS={passed} WARN={statuses['warn']} ERROR={failed} "
        f"SKIP={statuses['skipped']} TOTAL={len(results['results'])}"
    )


def main(argv: list[str] | None = None) -> int:
    """``python -m edgarpit.load --raw DIR --out DIR``.

    Builds the database from the raw cache, prints the row count of every
    ``raw`` table and the dbt node summary, then the database path. Returns 0;
    a build failure propagates as an exception and a non-zero exit.
    """
    parser = argparse.ArgumentParser(
        prog="python -m edgarpit.load",
        description="Build edgarpit.duckdb from a raw cache: ingest, load, dbt.",
    )
    parser.add_argument("--raw", type=Path, required=True, help="raw cache directory")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    args = parser.parse_args(argv)
    db_path = build_from_raw(args.raw, args.out)
    con = duckdb.connect(str(db_path))
    try:
        for table in (*COLUMNS, *REFERENCE_TYPES):
            row = con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()
            assert row is not None
            print(f"raw.{table}: {int(row[0])} rows")
    finally:
        con.close()
    print(f"dbt build: {_dbt_summary(args.out / DBT_TARGET_DIRNAME)}")
    print(f"database: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
