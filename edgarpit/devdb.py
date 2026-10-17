"""Build the foundation development database with the frozen ``raw`` tables.

Until the ingest pipeline exists (Epic 01), dbt and query development runs
against ``<out_dir>/edgarpit.duckdb``, filled from staging CSVs in
``tests/fixtures/staging`` when they are present and left empty otherwise.
Every CSV is validated with the Pandera schema of its table before it is
loaded, and every connection sets the session timezone to UTC (decision A-5).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import polars as pl
from polars.datatypes import DataType, DataTypeClass

from edgarpit.schemas import COLUMNS, DUCKDB_TYPES, SCHEMAS, duckdb_ddl

DEFAULT_STAGING_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "staging"

_POLARS_TYPES: dict[str, DataType | DataTypeClass] = {
    "VARCHAR(10)": pl.String,
    "VARCHAR": pl.String,
    "DATE": pl.Date,
    "TIMESTAMPTZ": pl.Datetime("us", "UTC"),
    "BOOLEAN": pl.Boolean,
    "DOUBLE": pl.Float64,
    "INTEGER": pl.Int64,
}


def _read_staging_csv(table: str, path: Path) -> pl.DataFrame:
    overrides = {column: _POLARS_TYPES[dtype] for column, dtype in DUCKDB_TYPES[table].items()}
    return pl.read_csv(path, schema_overrides=overrides).select(COLUMNS[table])


def create_dev_db(out_dir: Path, staging_dir: Path | None = None) -> Path:
    """Recreate ``<out_dir>/edgarpit.duckdb`` with the three ``raw`` tables.

    Each ``<table>.csv`` found in ``staging_dir`` (default
    ``tests/fixtures/staging``) is validated with its Pandera schema, then
    loaded; tables without a CSV stay empty. Per-table row counts are printed
    and the database path is returned.
    """
    out_dir = Path(out_dir)
    staging = DEFAULT_STAGING_DIR if staging_dir is None else Path(staging_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "edgarpit.duckdb"
    for stale in (db_path, db_path.with_suffix(".duckdb.wal")):
        if stale.exists():
            stale.unlink()

    counts: dict[str, int] = {}
    con = duckdb.connect(str(db_path))
    try:
        con.execute("SET TimeZone='UTC'")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for table in COLUMNS:
            con.execute(duckdb_ddl(table))
            csv_path = staging / f"{table}.csv"
            if csv_path.exists():
                frame = _read_staging_csv(table, csv_path)
                SCHEMAS[table].validate(frame)
                con.register("staging_frame", frame)
                con.execute(f"INSERT INTO raw.{table} SELECT * FROM staging_frame")
                con.unregister("staging_frame")
                counts[table] = frame.height
            else:
                counts[table] = 0
    finally:
        con.close()

    for table, count in counts.items():
        print(f"raw.{table}: {count} rows")
    return db_path


def main(argv: list[str] | None = None) -> int:
    """``python -m edgarpit.devdb --out DIR [--staging DIR]``."""
    parser = argparse.ArgumentParser(description="Create the edgar-pit development database.")
    parser.add_argument("--out", type=Path, required=True, help="directory for edgarpit.duckdb")
    parser.add_argument(
        "--staging",
        type=Path,
        default=None,
        help="directory of staging CSVs (default: tests/fixtures/staging)",
    )
    args = parser.parse_args(argv)
    db_path = create_dev_db(args.out, args.staging)
    print(f"database: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
