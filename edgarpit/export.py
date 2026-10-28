"""Deterministic Parquet release with a manifest (FR-1.11, decision S-7).

:func:`export_release` writes one Parquet file per released table
(``fundamentals_pit``, ``ratios_pit``, ``events_8k``, ``filings``,
``facts_raw``) plus ``manifest.json``. The Phase 2 tables ship in the same
release when the database holds them (decisions A-32 and A-38):
``fundamentals_pit_usd`` and ``filings_text`` from schema ``main`` and
``fx_daily`` from schema ``raw``. A v1.0.0 database has none of the three and
exports exactly the five Phase 1 files.

- every file is written with ``COPY ... ORDER BY`` over the table's key
  columns and fixed writer settings (ZSTD, fixed row-group size), and the data
  files carry no build timestamps or random ids, so exporting the same database
  twice produces byte-identical files;
- the build timestamp, the database file name and the SEC fetch date range
  (min/max ``fetched_at``) live in ``manifest.json`` only; the range comes from
  a raw ``manifest.jsonl`` when one is given, else from the ``build-info.json``
  the builder writes next to the database, else it is ``None``;
- each manifest file entry carries the file size in bytes, the row count, the
  SHA-256 of the written file and the schema (column names and types); the
  ``filings_text`` entry also carries ``text_bytes``, the uncompressed UTF-8
  size of the section text, so a release reviewer sees the size budget of the
  one table whose rows can be arbitrarily long.

The connection is used read-only in practice (the CLI opens it that way);
writing the Parquet files does not modify the database.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

PARQUET_ROW_GROUP_SIZE = 100_000
# ``filings_text`` rows are prose or a flattened table, commonly tens of
# kilobytes where a fundamentals row is tens of bytes. A reader touches only
# the row groups it needs (each section is a self-contained cell), so a smaller
# group keeps a scan of a few filings cheap and bounds the dictionary page of a
# group; 10_000 sections stay within a few megabytes uncompressed, a size that
# still compresses well under ZSTD. The default 100_000 would put hundreds of
# megabytes of text in one group.
FILINGS_TEXT_ROW_GROUP_SIZE = 10_000
PARQUET_COMPRESSION = "ZSTD"


@dataclass(frozen=True)
class ExportTable:
    """One released table: its name, SQL source and deterministic row order.

    ``order_by`` is the table's key (section 6/7 of ``docs/architecture.md``),
    so two exports of the same rows write the same bytes whatever order the
    rows were inserted in. ``row_group_size`` is the Parquet row-group size for
    this table (rows, not bytes). ``text_column`` names the free-text column
    whose uncompressed UTF-8 size the manifest reports, when the table has one.
    """

    name: str
    source: str
    order_by: str
    row_group_size: int = PARQUET_ROW_GROUP_SIZE
    text_column: str | None = None


EXPORTS: tuple[ExportTable, ...] = (
    ExportTable(
        name="fundamentals_pit",
        source="main.fundamentals_pit",
        order_by="cik, field, period_start, period_end, accession",
    ),
    ExportTable(
        name="ratios_pit",
        source="main.ratios_pit",
        order_by="cik, ratio, period_start, period_end, asof_ts",
    ),
    ExportTable(
        name="events_8k",
        source="main.events_8k",
        order_by="cik, accession, item_code",
    ),
    ExportTable(name="filings", source="main.filings", order_by="cik, accession"),
    ExportTable(
        name="facts_raw",
        source="raw.facts_raw",
        order_by="cik, taxonomy, concept, unit, period_start, period_end, accession",
    ),
)

# The Phase 2 tables (decisions A-32 and A-38). They are written only when the
# database has them, so a v1.0.0 database keeps its exact Phase 1 release.
PHASE2_EXPORTS: tuple[ExportTable, ...] = (
    ExportTable(
        name="fundamentals_pit_usd",
        source="main.fundamentals_pit_usd",
        order_by="cik, field, period_start, period_end, accession",
    ),
    ExportTable(
        name="filings_text",
        source="main.filings_text",
        order_by="accession, document, section_order, section",
        row_group_size=FILINGS_TEXT_ROW_GROUP_SIZE,
        text_column="text",
    ),
    ExportTable(name="fx_daily", source="raw.fx_daily", order_by="series, rate_date"),
)


@dataclass(frozen=True)
class FileEntry:
    """Manifest entry for one written Parquet file."""

    table: str
    file: str
    rows: int
    sha256: str
    bytes: int
    schema: dict[str, str]
    text_bytes: int | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "table": self.table,
            "file": self.file,
            "rows": self.rows,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "schema": self.schema,
        }
        if self.text_bytes is not None:
            payload["text_bytes"] = self.text_bytes
        return payload


@dataclass(frozen=True)
class FetchRange:
    """The oldest and newest ``fetched_at`` stamps in a raw manifest."""

    min: str
    max: str

    def to_json(self) -> dict[str, Any]:
        return {"min": self.min, "max": self.max}


@dataclass(frozen=True)
class Manifest:
    """The release manifest: everything that varies between two exports."""

    build_timestamp: str
    database: str | None
    sec_fetch_range: FetchRange | None
    files: list[FileEntry]

    def to_json(self) -> dict[str, Any]:
        return {
            "build_timestamp": self.build_timestamp,
            "database": self.database,
            "sec_fetch_range": (
                None if self.sec_fetch_range is None else self.sec_fetch_range.to_json()
            ),
            "files": [entry.to_json() for entry in self.files],
        }


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    row = con.execute(sql).fetchone()
    if row is None:  # pragma: no cover - aggregate queries always return a row
        raise RuntimeError(f"query returned no row: {sql}")
    return row[0]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema(con: duckdb.DuckDBPyConnection, source: str) -> dict[str, str]:
    rows = con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _table_exists(con: duckdb.DuckDBPyConnection, source: str) -> bool:
    """Return whether ``source`` (``schema.table``) exists as a table or view.

    DuckDB lists both kinds in ``information_schema.tables``; a dbt model may be
    materialised either way, and the export reads whichever is present.
    """
    schema, _, table = source.partition(".")
    row = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ? LIMIT 1",
        [schema, table],
    ).fetchone()
    return row is not None


def _text_bytes(con: duckdb.DuckDBPyConnection, table: ExportTable) -> int | None:
    """Return the uncompressed UTF-8 size of ``table``'s text column, or ``None``.

    ``strlen`` counts bytes (``length`` counts characters); ``sum`` over no rows
    is 0, so an empty table reports 0 rather than nothing.
    """
    if table.text_column is None:
        return None
    return int(
        _scalar(con, f"SELECT coalesce(sum(strlen({table.text_column})), 0) FROM {table.source}")
    )


def _database_name(con: duckdb.DuckDBPyConnection) -> str | None:
    for _id, _name, file in con.execute("PRAGMA database_list").fetchall():
        if file:
            return Path(file).name
    return None


def _fetch_range(raw_manifest: Path | None) -> FetchRange | None:
    """Read min/max ``fetched_at`` from a raw cache manifest, when given."""
    if raw_manifest is None:
        return None
    path = Path(raw_manifest)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"cannot read raw manifest {path}: {exc}") from exc
    stamps: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            stamp = row["fetched_at"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"malformed raw manifest {path} line {number}: {exc}") from exc
        if not isinstance(stamp, str) or not stamp:
            raise ValueError(f"malformed raw manifest {path} line {number}: no fetched_at")
        stamps.append(stamp)
    if not stamps:
        return None
    return FetchRange(min=min(stamps), max=max(stamps))


def _build_info_fetch_range(build_info: Path) -> FetchRange | None:
    """Read ``sec_fetch_range`` from a ``build-info.json`` written by the builder.

    Returns ``None`` when the document carries no range (a cache with an empty
    manifest). Raises :class:`FileNotFoundError` when the file cannot be read
    and :class:`ValueError` when it is not an object with a string ``min`` and
    ``max`` under ``sec_fetch_range``.
    """
    path = Path(build_info)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"cannot read build info {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed build info {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"malformed build info {path}: expected a JSON object")
    value = document.get("sec_fetch_range")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"malformed build info {path}: sec_fetch_range is not an object")
    minimum, maximum = value.get("min"), value.get("max")
    if not isinstance(minimum, str) or not isinstance(maximum, str):
        raise ValueError(f"malformed build info {path}: sec_fetch_range needs min and max")
    return FetchRange(min=minimum, max=maximum)


def export_release(
    con: duckdb.DuckDBPyConnection,
    out_dir: Path | str,
    *,
    raw_manifest: Path | None = None,
    build_info: Path | None = None,
) -> Manifest:
    """Write the Parquet release and ``manifest.json`` under ``out_dir``.

    Every table in :data:`EXPORTS` becomes ``<out_dir>/<table>.parquet`` with a
    deterministic row order and fixed writer settings; re-exporting the same
    database overwrites the files byte-for-byte identically. Each table in
    :data:`PHASE2_EXPORTS` is added when a table or view of that name exists in
    the database (``main.fundamentals_pit_usd``, ``main.filings_text``,
    ``raw.fx_daily``), so a v1.0.0 database exports the same five files as
    before and a Phase 2 database ships all eight. A table that is absent is
    skipped and no file of its name is written; files from an earlier export
    into the same directory are left in place, so export into a fresh
    directory and treat ``manifest.json`` as the list of what the release
    contains.

    ``manifest.json`` carries the build timestamp, the database file name, the
    SEC fetch date range and a per-file size in bytes, row count, SHA-256 and
    schema; ``filings_text`` also reports ``text_bytes``, the uncompressed
    UTF-8 size of ``text``. The fetch range comes from ``raw_manifest`` (a raw
    cache ``manifest.jsonl``) when given, else from ``build_info`` (the
    ``build-info.json`` of a built database), else it is ``None``; a raw
    manifest with no rows and a build info without a range also yield ``None``.
    Timestamps in the manifest are UTC ISO-8601 with a ``Z`` suffix. Raises
    :class:`duckdb.Error` for a missing Phase 1 table or a failed write,
    :class:`FileNotFoundError`/:class:`ValueError` for a bad raw manifest or
    build info.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    con.execute("SET TimeZone='UTC'")
    if raw_manifest is not None:
        fetch_range = _fetch_range(raw_manifest)
    elif build_info is not None:
        fetch_range = _build_info_fetch_range(build_info)
    else:
        fetch_range = None
    tables = list(EXPORTS)
    tables.extend(table for table in PHASE2_EXPORTS if _table_exists(con, table.source))
    entries: list[FileEntry] = []
    for table in tables:
        target = out / f"{table.name}.parquet"
        literal = target.as_posix().replace("'", "''")
        con.execute(
            f"COPY (SELECT * FROM {table.source} ORDER BY {table.order_by}) "
            f"TO '{literal}' (FORMAT PARQUET, COMPRESSION {PARQUET_COMPRESSION}, "
            f"ROW_GROUP_SIZE {table.row_group_size}, OVERWRITE)"
        )
        entries.append(
            FileEntry(
                table=table.name,
                file=target.name,
                rows=int(_scalar(con, f"SELECT count(*) FROM {table.source}")),
                sha256=_sha256(target),
                bytes=target.stat().st_size,
                schema=_schema(con, table.source),
                text_bytes=_text_bytes(con, table),
            )
        )
    manifest = Manifest(
        build_timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        database=_database_name(con),
        sec_fetch_range=fetch_range,
        files=entries,
    )
    (out / "manifest.json").write_text(
        json.dumps(manifest.to_json(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return manifest
