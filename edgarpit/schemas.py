"""Pandera schemas for the DuckDB ``raw`` staging tables.

This module is the executable form of the staging contract (section 6 of
``docs/architecture.md``): the column names, order, dtypes and nullability of
``raw.filings``, ``raw.events_8k`` and ``raw.facts_raw``. ``COLUMNS`` and
``duckdb_ddl`` exist so the loader (Epic 01) and the development database
builder create exactly the tables these schemas validate.
"""

from __future__ import annotations

import re

import pandera.polars as pa
import polars as pl

CIK_PATTERN = r"^\d{10}$"
ACCESSION_PATTERN = r"^\d{10}-\d{2}-\d{6}$"
TAXONOMIES = ["us-gaap", "ifrs-full", "dei"]
ACCEPTANCE_TS_SOURCES = ["sgml_header", "filing_date_eod"]


class FilingsSchema(pa.DataFrameModel):
    """One row per filing (section 6 ``raw.filings``)."""

    cik: str = pa.Field(str_matches=CIK_PATTERN)
    accession: str = pa.Field(str_matches=ACCESSION_PATTERN, unique=True)
    form: str
    filing_date: pl.Date
    report_date: pl.Date = pa.Field(nullable=True)
    acceptance_ts: pl.Datetime("us", "UTC")  # type: ignore[valid-type]
    acceptance_ts_source: str = pa.Field(isin=ACCEPTANCE_TS_SOURCES)
    acceptance_ts_api: pl.Datetime("us", "UTC") = pa.Field(  # type: ignore[valid-type]
        nullable=True
    )
    ts_imputed: bool
    primary_document: str = pa.Field(nullable=True)
    items: str = pa.Field(nullable=True)
    is_amendment: bool
    is_xbrl: bool
    source: str

    class Config:
        strict = True
        ordered = True


class Events8kSchema(pa.DataFrameModel):
    """One row per (8-K or 8-K/A, item) (section 6 ``raw.events_8k``)."""

    cik: str = pa.Field(str_matches=CIK_PATTERN)
    accession: str = pa.Field(str_matches=ACCESSION_PATTERN)
    form: str
    acceptance_ts: pl.Datetime("us", "UTC")  # type: ignore[valid-type]
    ts_imputed: bool
    item_code: str
    item_label: str

    class Config:
        strict = True
        ordered = True
        unique = ("accession", "item_code")


class FactsRawSchema(pa.DataFrameModel):
    """One row per companyfacts entry (section 6 ``raw.facts_raw``)."""

    cik: str = pa.Field(str_matches=CIK_PATTERN)
    taxonomy: str = pa.Field(isin=TAXONOMIES)
    concept: str
    unit: str
    value: float
    period_start: pl.Date = pa.Field(nullable=True)
    period_end: pl.Date
    fy: int = pa.Field(nullable=True)
    fp: str = pa.Field(nullable=True)
    form: str
    accession: str = pa.Field(str_matches=ACCESSION_PATTERN)
    filed: pl.Date
    frame: str = pa.Field(nullable=True)

    class Config:
        strict = True
        ordered = True


SCHEMAS: dict[str, type[pa.DataFrameModel]] = {
    "filings": FilingsSchema,
    "events_8k": Events8kSchema,
    "facts_raw": FactsRawSchema,
}

COLUMNS: dict[str, list[str]] = {
    "filings": [
        "cik",
        "accession",
        "form",
        "filing_date",
        "report_date",
        "acceptance_ts",
        "acceptance_ts_source",
        "acceptance_ts_api",
        "ts_imputed",
        "primary_document",
        "items",
        "is_amendment",
        "is_xbrl",
        "source",
    ],
    "events_8k": [
        "cik",
        "accession",
        "form",
        "acceptance_ts",
        "ts_imputed",
        "item_code",
        "item_label",
    ],
    "facts_raw": [
        "cik",
        "taxonomy",
        "concept",
        "unit",
        "value",
        "period_start",
        "period_end",
        "fy",
        "fp",
        "form",
        "accession",
        "filed",
        "frame",
    ],
}

DUCKDB_TYPES: dict[str, dict[str, str]] = {
    "filings": {
        "cik": "VARCHAR(10)",
        "accession": "VARCHAR",
        "form": "VARCHAR",
        "filing_date": "DATE",
        "report_date": "DATE",
        "acceptance_ts": "TIMESTAMPTZ",
        "acceptance_ts_source": "VARCHAR",
        "acceptance_ts_api": "TIMESTAMPTZ",
        "ts_imputed": "BOOLEAN",
        "primary_document": "VARCHAR",
        "items": "VARCHAR",
        "is_amendment": "BOOLEAN",
        "is_xbrl": "BOOLEAN",
        "source": "VARCHAR",
    },
    "events_8k": {
        "cik": "VARCHAR(10)",
        "accession": "VARCHAR",
        "form": "VARCHAR",
        "acceptance_ts": "TIMESTAMPTZ",
        "ts_imputed": "BOOLEAN",
        "item_code": "VARCHAR",
        "item_label": "VARCHAR",
    },
    "facts_raw": {
        "cik": "VARCHAR(10)",
        "taxonomy": "VARCHAR",
        "concept": "VARCHAR",
        "unit": "VARCHAR",
        "value": "DOUBLE",
        "period_start": "DATE",
        "period_end": "DATE",
        "fy": "INTEGER",
        "fp": "VARCHAR",
        "form": "VARCHAR",
        "accession": "VARCHAR",
        "filed": "DATE",
        "frame": "VARCHAR",
    },
}

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def duckdb_ddl(table: str, schema: str = "raw") -> str:
    """Return the ``CREATE TABLE`` statement for a staging table.

    ``table`` is one of ``filings``, ``events_8k`` or ``facts_raw``; ``schema``
    is the DuckDB schema to create it in. Raises ``ValueError`` for an unknown
    table or an invalid schema name.
    """
    if table not in DUCKDB_TYPES:
        known = ", ".join(sorted(DUCKDB_TYPES))
        raise ValueError(f"unknown staging table {table!r}; expected one of {known}")
    if not _IDENTIFIER_RE.fullmatch(schema):
        raise ValueError(f"invalid DuckDB schema name {schema!r}")
    columns = ",\n    ".join(f"{name} {dtype}" for name, dtype in DUCKDB_TYPES[table].items())
    return f"CREATE TABLE {schema}.{table} (\n    {columns}\n)"
