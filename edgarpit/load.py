"""Validate staged DataFrames and load them into DuckDB (decisions A-8, A-16, A-20).

Two loaders write the ``raw`` schema of ``<out_dir>/edgarpit.duckdb``:
:func:`load_tables` replaces the Pandera-validated staging tables from the
ingest frames, and :func:`load_reference_tables` replaces ``raw.concept_map``
and ``raw.universe`` from ``config/concept_map.yaml`` and a universe frame.
Both set the connection's session timezone to UTC
(decision A-5) and both are transactional: a validation error or a failed
insert leaves the previously loaded tables untouched.

:func:`build_from_raw` runs the whole offline pipeline for a raw cache:
ingest every cached issuer, load the frames and the reference tables, then run
the dbt project with ``dbt build``. ``python -m edgarpit.load --raw DIR --out
DIR`` does the same and prints the loaded row counts and the dbt summary.
A successful build also writes ``<out_dir>/build-info.json`` next to the
database: the package version, the raw cache path, per-CIK document counts and
the min/max ``fetched_at`` from the raw manifest, the row count of every
contract table and the dbt node summary. ``edgarpit export`` reads the fetch
range from that file, so a built database carries its provenance.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections import Counter
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import polars as pl
from polars.datatypes import DataType, DataTypeClass

from edgarpit import __version__
from edgarpit.concepts import (
    DEFAULT_CONCEPT_MAP_PATH,
    ConceptMap,
    load_concept_map,
)
from edgarpit.fx.valet import parse_valet
from edgarpit.ingest.companyfacts import ingest_facts
from edgarpit.ingest.submissions import ingest_filings, load_item_labels
from edgarpit.rawcache import RawCache
from edgarpit.schemas import COLUMNS, DUCKDB_TYPES, SCHEMAS, duckdb_ddl
from edgarpit.text.ingest import ingest_text

if TYPE_CHECKING:
    from dbt.cli.main import dbtRunnerResult

DB_NAME = "edgarpit.duckdb"
BUILD_INFO_NAME = "build-info.json"
DBT_TARGET_DIRNAME = "dbt-target"
TRANSFORM_DIR = Path(__file__).resolve().parents[1] / "transform"

LOGGER = logging.getLogger("edgarpit.load")

# Tables counted into build-info.json, in report order: the staging tables and
# reference tables first, then the dbt models.
BUILD_INFO_TABLES: tuple[str, ...] = (
    "raw.filings",
    "raw.events_8k",
    "raw.facts_raw",
    "raw.filings_text",
    "raw.fx_daily",
    "raw.concept_map",
    "raw.universe",
    "main.fundamentals_pit",
    "main.fundamentals_pit_usd",
    "main.ratios_pit",
    "main.events_8k",
    "main.filings",
    "main.filings_text",
)

DEFAULT_UNIVERSE_CSV = Path(__file__).resolve().parents[1] / "config" / "universe.csv"
DEFAULT_UNIVERSE_LARGE_CSV = Path(__file__).resolve().parents[1] / "config" / "universe_large.csv"

# The project's universe files, most specific first. ``build_from_raw`` loads
# the rows of both that the cache actually holds, so a build of the 250-issuer
# cache carries a ticker, country and filer type for every cached issuer and
# never lists an issuer the cache has no filings for (decision A-20; the
# 40-issuer file is a subset of the large one since decision A-36).
UNIVERSE_PATHS: tuple[Path, ...] = (DEFAULT_UNIVERSE_CSV, DEFAULT_UNIVERSE_LARGE_CSV)

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
    "DATE": pl.Date,
    "TIMESTAMPTZ": pl.Datetime("us", "UTC"),
    "BOOLEAN": pl.Boolean,
    "DOUBLE": pl.Float64,
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
    before ``ifrs-full`` and rank 1 first, over
    :meth:`edgarpit.concepts.ConceptMap.specs`.
    """
    rows: list[tuple[str, str, str, int, str, str]] = []
    for spec in concept_map.specs():
        for taxonomy, concepts in (("us-gaap", spec.us_gaap), ("ifrs-full", spec.ifrs_full)):
            for rank, concept in enumerate(concepts, start=1):
                rows.append((spec.field, taxonomy, concept, rank, spec.kind, spec.unit_family))
    schema = {name: _POLARS_TYPES[dtype] for name, dtype in REFERENCE_TYPES["concept_map"].items()}
    return pl.DataFrame(rows, schema=schema, orient="row")


def _read_universe(path: Path) -> pl.DataFrame:
    """Read a universe CSV: five columns, zero-padded CIK strings."""
    try:
        frame = pl.read_csv(path, schema_overrides={"cik": pl.String})
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"universe CSV not found: {path}") from exc
    return _validate_universe(frame, str(path))


def _validate_universe(frame: pl.DataFrame, source: str) -> pl.DataFrame:
    """Select and check the five universe columns of ``frame``.

    ``source`` names the CSV or caller in the error messages. Returns the frame
    with the columns in ``_UNIVERSE_COLUMNS`` order and ``cik`` as a string.
    Raises :class:`ValueError` for a missing column or a CIK that is not ten
    digits.
    """
    missing = [column for column in _UNIVERSE_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"universe {source} is missing columns {missing}")
    frame = frame.select(_UNIVERSE_COLUMNS)
    malformed = frame.filter(~pl.col("cik").cast(pl.String).str.contains(r"^\d{10}$"))
    if malformed.height:
        ciks = malformed.get_column("cik").to_list()
        raise ValueError(f"universe {source} has CIKs that are not 10 digits: {ciks}")
    return frame.with_columns(pl.col("cik").cast(pl.String))


def reference_universe(
    cached_ciks: Collection[str], *, paths: Sequence[Path] = UNIVERSE_PATHS
) -> pl.DataFrame:
    """Return the ``raw.universe`` rows of the issuers a raw cache holds.

    ``cached_ciks`` are the ten-digit CIKs with a cached submissions document,
    i.e. the issuers a build can produce rows for. ``paths`` are universe CSVs
    in precedence order, by default ``config/universe.csv`` then
    ``config/universe_large.csv``. Returns every row of those files whose CIK is
    in ``cached_ciks``, once per CIK and sorted by CIK, with the columns
    ``ticker, cik, name, country, filer_type`` and ``cik`` as a zero-padded
    ten-character string.

    An issuer listed in both files keeps its first row, because the two files
    describe the same issuer; a CIK whose rows disagree on any of the four
    metadata columns raises :class:`ValueError` naming it rather than letting
    file order decide which ticker a consumer sees. Raises
    :class:`FileNotFoundError` for a missing file and :class:`ValueError` for a
    malformed one.
    """
    frames = [_read_universe(Path(path)) for path in paths]
    if not frames:
        raise ValueError("no universe CSV given")
    combined = pl.concat(frames)
    variants = (
        combined.unique()
        .group_by("cik")
        .agg(pl.len().alias("rows"))
        .filter(pl.col("rows") > 1)
        .sort("cik")
    )
    if variants.height:
        ciks = variants.get_column("cik").to_list()
        raise ValueError(f"universe files disagree about CIK(s) {ciks}")
    wanted = sorted(set(cached_ciks))
    return (
        combined.filter(pl.col("cik").is_in(wanted))
        .unique(subset="cik", keep="first", maintain_order=True)
        .sort("cik")
    )


def _empty_contract_frame(table: str) -> pl.DataFrame:
    """Return an empty frame with exactly the contract columns and dtypes of ``table``."""
    schema = {name: _POLARS_TYPES[dtype] for name, dtype in DUCKDB_TYPES[table].items()}
    return pl.DataFrame(schema=schema).select(COLUMNS[table])


def load_tables(
    con: duckdb.DuckDBPyConnection,
    *,
    filings: pl.DataFrame,
    events_8k: pl.DataFrame,
    facts_raw: pl.DataFrame,
    filings_text: pl.DataFrame,
    fx_daily: pl.DataFrame,
) -> None:
    """Validate each frame with its Pandera schema and replace the ``raw.*`` tables.

    ``filings``, ``events_8k``, ``facts_raw``, ``filings_text`` and ``fx_daily``
    must be valid under :class:`edgarpit.schemas.FilingsSchema`,
    :class:`edgarpit.schemas.Events8kSchema`,
    :class:`edgarpit.schemas.FactsRawSchema`,
    :class:`edgarpit.schemas.FilingsTextSchema` and
    :class:`edgarpit.schemas.FxDailySchema`; an empty frame of the table's
    contract columns is the way to load nothing. The connection's timezone is
    set to UTC before loading (decision A-5). Each frame is validated before any
    statement is executed, then all five tables are dropped, recreated from
    :func:`edgarpit.schemas.duckdb_ddl` and filled in one transaction, so an
    invalid frame raises :class:`pandera.errors.SchemaError` with nothing
    written. All timestamps are timezone-aware UTC instants.
    """
    frames = {
        "filings": filings,
        "events_8k": events_8k,
        "facts_raw": facts_raw,
        "filings_text": filings_text,
        "fx_daily": fx_daily,
    }
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
    universe: pl.DataFrame | None = None,
) -> None:
    """Replace ``raw.concept_map`` and ``raw.universe`` with the reference data.

    ``raw.concept_map(field, taxonomy, concept, concept_rank, kind,
    unit_family)`` is flattened from ``concept_map`` (default:
    :func:`edgarpit.concepts.load_concept_map` on ``config/concept_map.yaml``);
    ``concept_rank`` is 1-based, 1 being the field's preferred concept.
    ``raw.universe(ticker, cik, name, country, filer_type)`` comes from
    ``universe`` when given (the rows
    :func:`reference_universe` selected for a cache), otherwise from
    ``universe_csv`` (default ``config/universe.csv``), with ``cik`` kept as a
    zero-padded 10-digit string. ``universe`` and ``universe_csv`` are mutually
    exclusive. Both tables are dropped, recreated and filled in one transaction;
    a malformed map, CSV or frame raises before anything is written. The
    connection's timezone is set to UTC (decision A-5) so ``concept_rank`` etc.
    load deterministically; these tables carry no timestamps.
    """
    if universe is not None and universe_csv is not None:
        raise ValueError("pass either universe or universe_csv, not both")
    if concept_map is None:
        concept_map = load_concept_map(DEFAULT_CONCEPT_MAP_PATH)
    concept_frame = _concept_map_frame(concept_map)
    if universe is not None:
        universe_frame = _validate_universe(universe, "frame")
    else:
        universe_frame = _read_universe(
            DEFAULT_UNIVERSE_CSV if universe_csv is None else universe_csv
        )
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


def _cross_issuer_rank(frame: pl.DataFrame) -> pl.DataFrame:
    """Order rows of one accession so the preferred issuer's row comes first.

    A row whose CIK matches the accession's own ten-digit prefix (the filer in
    the common case) is ``_borrowed=False`` and ranks before a row another
    issuer listed; ties fall back to the smallest CIK and then to the caller's
    order, which is deterministic (issuers ascend and each issuer's rows keep
    their submissions order).
    """
    return frame.with_columns(
        (pl.col("accession").str.slice(0, 10) != pl.col("cik")).alias("_borrowed")
    ).sort("accession", "_borrowed", "cik", maintain_order=True)


def _deduplicate_filings(
    filings: pl.DataFrame, events_8k: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Resolve accessions the submissions API lists under several issuers.

    SEC lists some filings (ownership schedules, Forms 3 and 4) in the
    submissions history of every CIK involved, and a filing agent's CIK can
    prefix the accession, so the same accession can arrive from more than one
    issuer. The frozen contract keys ``raw.filings`` by accession alone, so the
    repeats are resolved here, before validation: the filer's own row wins,
    otherwise the smallest CIK. ``raw.events_8k`` is deduplicated on
    ``(accession, item_code)`` with the same rule. Both frames keep every
    column and their input rows otherwise untouched.
    """
    filings = _cross_issuer_rank(filings).unique(
        subset="accession", keep="first", maintain_order=True
    )
    events_8k = _cross_issuer_rank(events_8k).unique(
        subset=["accession", "item_code"], keep="first", maintain_order=True
    )
    return filings.drop("_borrowed"), events_8k.drop("_borrowed")


def _indexed_accessions(raw: RawCache) -> dict[str, list[str]]:
    """Return the accessions with a cached filing index, per CIK.

    A ``filing_index`` directory entry is created by ``fetch-text`` for every
    8-K whose index headers were fetched, so its presence is exactly the set of
    accessions whose text :func:`edgarpit.text.ingest.ingest_text` can extract.
    The result is ordered by CIK and accession, and is empty when the cache
    holds no filing index.
    """
    directory = raw.raw_dir / "filing_index"
    if not directory.is_dir():
        return {}
    indexed: dict[str, list[str]] = {}
    for entry in sorted(directory.iterdir()):
        if not entry.is_dir() or not _CIK_DIR.fullmatch(entry.name):
            continue
        accessions = raw.keys("filing_index", entry.name)
        if accessions:
            indexed[entry.name] = accessions
    return indexed


def _ingest_filings_text(raw: RawCache, filings: pl.DataFrame) -> pl.DataFrame:
    """Extract every cached filing index into a ``raw.filings_text`` frame.

    ``filings`` is the frame about to be loaded into ``raw.filings``; the row of
    each indexed accession supplies the CIK, acceptance time and imputed flag,
    so text is known exactly when its filing was accepted. Returns an empty
    frame with the contract's columns when the cache holds no filing index.
    Raises whatever :func:`edgarpit.text.ingest.ingest_text` raises for an
    accession missing from ``filings`` or a missing cached document.
    """
    indexed = _indexed_accessions(raw)
    frames: list[pl.DataFrame] = []
    for cik, accessions in indexed.items():
        issuer = filings.filter(pl.col("accession").is_in(accessions))
        frames.append(ingest_text(raw, cik, accessions, issuer))
    if not frames:
        return _empty_contract_frame("filings_text")
    return pl.concat(frames)


def _ingest_fx_daily(raw: RawCache) -> pl.DataFrame:
    """Parse every cached FX series into a ``raw.fx_daily`` frame.

    Each key under ``raw/fx/_/`` is a series id (``FXUSDCAD``) and the latest
    stored document for it is parsed with
    :func:`edgarpit.fx.valet.parse_valet`; series are concatenated in key order,
    so the result is a function of the cache alone. Returns an empty frame with
    the contract's columns when no series is cached.
    """
    frames: list[pl.DataFrame] = []
    for series in raw.keys("fx", ""):
        body = raw.latest("fx", "", series)
        if body is None:
            continue
        frames.append(parse_valet(json.loads(body), series))
    if not frames:
        return _empty_contract_frame("fx_daily")
    return pl.concat(frames)


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


def _manifest_documents(raw: RawCache) -> dict[str, dict[str, int]]:
    """Per-CIK document counts by endpoint from the raw cache manifest.

    Every manifest row is one stored document, so a re-fetched submissions
    document counts twice (versioning is the cache's job). Rows without a CIK
    (the global ``company_tickers`` document) are not per-issuer and are
    skipped. The result is ordered by CIK and endpoint, so the written file is
    stable across builds over the same cache.
    """
    documents: dict[str, dict[str, int]] = {}
    for row in raw.manifest_rows():
        cik = row.get("cik")
        endpoint = row.get("endpoint")
        if not isinstance(cik, str) or not cik or not isinstance(endpoint, str):
            continue
        counts = documents.setdefault(cik, {})
        counts[endpoint] = counts.get(endpoint, 0) + 1
    return {cik: dict(sorted(counts.items())) for cik, counts in sorted(documents.items())}


def _manifest_fetch_range(raw: RawCache) -> dict[str, str] | None:
    """The min and max ``fetched_at`` stamps in the raw manifest, or ``None``."""
    stamps = [
        row["fetched_at"] for row in raw.manifest_rows() if isinstance(row.get("fetched_at"), str)
    ]
    if not stamps:
        return None
    return {"min": min(stamps), "max": max(stamps)}


def _table_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Row count of every table in :data:`BUILD_INFO_TABLES`, in that order."""
    counts: dict[str, int] = {}
    for table in BUILD_INFO_TABLES:
        row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert row is not None
        counts[table] = int(row[0])
    return counts


def _write_build_info(out_dir: Path, raw: RawCache, counts: dict[str, int], dbt: str) -> Path:
    """Write ``<out_dir>/build-info.json`` and return its path.

    The document records the package version, the raw cache path, per-CIK
    document counts, the raw manifest's ``fetched_at`` range, the contract
    table row counts and the dbt node summary. It carries no wall-clock time:
    the only timestamps are the cache's own fetch stamps.
    """
    document: dict[str, object] = {
        "package_version": __version__,
        "raw_cache": str(raw.root.resolve()),
        "documents": _manifest_documents(raw),
        "sec_fetch_range": _manifest_fetch_range(raw),
        "tables": counts,
        "dbt": dbt,
    }
    path = out_dir / BUILD_INFO_NAME
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def build_from_raw(raw_dir: Path, out_dir: Path) -> Path:
    """Build the database from a raw cache directory.

    ``raw_dir`` is a cache root as written by the fetcher (``raw/submissions``,
    ``raw/submissions_page``, ``raw/companyfacts``, ``raw/headers``, and the
    Phase 2 ``raw/filing_index`` and ``raw/fx`` slots). Every CIK with a cached
    submissions document, in ascending order, is ingested with
    :func:`edgarpit.ingest.submissions.ingest_filings` (item labels from
    ``config/8k_items.yaml``) and :func:`edgarpit.ingest.companyfacts.ingest_facts`;
    the frames are concatenated, cross-issuer repeats of one accession are
    resolved (see :func:`_deduplicate_filings`), validated and loaded into
    ``<out_dir>/edgarpit.duckdb`` together with the reference tables, the
    connection is closed, and dbt runs ``build --target build`` on the project
    in ``transform/`` with ``EDGARPIT_DB`` pointing at the database. dbt
    artifacts go to ``<out_dir>/dbt-target``. After a successful dbt run,
    ``<out_dir>/build-info.json`` is written next to the database with the
    package version, the raw cache path, per-CIK document counts, the raw
    manifest's ``fetched_at`` range, the contract table row counts and the dbt
    node summary.

    ``raw.filings_text`` is filled by
    :func:`edgarpit.text.ingest.ingest_text` for every accession with a cached
    filing index and ``raw.fx_daily`` by :func:`edgarpit.fx.valet.parse_valet`
    for every cached FX series (decision A-37); a cache without those endpoints
    loads both tables empty, so a Phase 1 cache still builds. ``raw.universe``
    carries exactly the cached issuers that the project's universe files list
    (:func:`reference_universe`), so every row the build produces can be joined
    to its ticker and no issuer the cache lacks is listed.

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
    cached_ciks = _cached_ciks(raw)
    for cik in cached_ciks:
        filings, events_8k = ingest_filings(raw, cik, item_labels)
        filings_frames.append(filings)
        events_frames.append(events_8k)
        facts_frames.append(ingest_facts(raw, cik))
    filings = pl.concat(filings_frames)
    events_8k = pl.concat(events_frames)
    deduplicated_filings, deduplicated_events = _deduplicate_filings(filings, events_8k)
    dropped_filings = filings.height - deduplicated_filings.height
    dropped_events = events_8k.height - deduplicated_events.height
    if dropped_filings or dropped_events:
        LOGGER.warning(
            "resolved %d cross-issuer duplicate filing rows and %d event rows",
            dropped_filings,
            dropped_events,
        )
    con = duckdb.connect(str(db_path))
    try:
        load_tables(
            con,
            filings=deduplicated_filings,
            events_8k=deduplicated_events,
            facts_raw=pl.concat(facts_frames),
            filings_text=_ingest_filings_text(raw, deduplicated_filings),
            fx_daily=_ingest_fx_daily(raw),
        )
        universe = reference_universe(cached_ciks)
        unmapped = sorted(set(cached_ciks) - set(universe.get_column("cik").to_list()))
        if unmapped:
            LOGGER.warning(
                "%d cached issuers have no row in the universe files: %s",
                len(unmapped),
                ", ".join(unmapped),
            )
        load_reference_tables(con, universe=universe)
    finally:
        con.close()
    result = _run_dbt(db_path, out / DBT_TARGET_DIRNAME)
    if not result.success:
        failed = result.exception if result.exception is not None else "dbt reported failed nodes"
        raise RuntimeError(f"dbt build failed for {db_path}: {failed}")
    con = duckdb.connect(str(db_path))
    try:
        counts = _table_counts(con)
    finally:
        con.close()
    _write_build_info(out, raw, counts, _dbt_summary(out / DBT_TARGET_DIRNAME))
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
