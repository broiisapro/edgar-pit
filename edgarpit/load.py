"""Validate staged DataFrames and load them into DuckDB (decisions A-8, A-16)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl


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
    to UTC before loading (decision A-5).
    """
    raise NotImplementedError


def build_from_raw(raw_dir: Path, out_dir: Path) -> Path:
    """Build the database from a raw cache directory.

    Runs ingest -> load -> dbt and returns the path of the built DuckDB file
    (``<out_dir>/edgarpit.duckdb``).
    """
    raise NotImplementedError
