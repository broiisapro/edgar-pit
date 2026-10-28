"""Pandera schemas for the DuckDB ``raw`` staging tables.

This module is the executable form of the staging contract (section 6 of
``docs/architecture.md``): the column names, order, dtypes and nullability of
``raw.filings``, ``raw.events_8k`` and ``raw.facts_raw``. ``COLUMNS`` and
``duckdb_ddl`` exist so the loader (Epic 01) and the development database
builder create exactly the tables these schemas validate.

The Phase 2 raw tables, ``raw.filings_text`` and ``raw.fx_daily`` (Epic 04 and
Epic 05), are declared in the parallel ``PHASE2_SCHEMAS``, ``PHASE2_COLUMNS``
and ``PHASE2_DUCKDB_TYPES`` mappings with the same shape as the three staging
tables. They are kept apart from ``COLUMNS``/``DUCKDB_TYPES`` so that the Epic
01 staging contract, the staging CSVs under ``tests/fixtures/staging`` and
``load.load_tables`` stay exactly as they are until their own loaders land;
``duckdb_ddl`` accepts a table from either mapping.
"""

from __future__ import annotations

import re

import pandera.polars as pa
import polars as pl
from pandera.api.polars.types import PolarsData

CIK_PATTERN = r"^\d{10}$"
ACCESSION_PATTERN = r"^\d{10}-\d{2}-\d{6}$"
TAXONOMIES = ["us-gaap", "ifrs-full", "dei"]
ACCEPTANCE_TS_SOURCES = ["sgml_header", "filing_date_eod"]
SHA256_PATTERN = r"^[0-9a-f]{64}$"

# 8-K text (decision A-31): the primary document and every EX-99.1 exhibit, and
# a section named by an EDGAR item code (``2.02``) or ``unsegmented``.
DOC_TYPES = ["primary", "EX-99.1"]
UNSEGMENTED = "unsegmented"
SECTION_PATTERN = r"^(\d{1,2}\.\d{2}|unsegmented)$"

# FX (decision A-32): the Bank of Canada Valet daily series.
FX_SOURCES = ["valet"]


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


class FilingsTextSchema(pa.DataFrameModel):
    """One row per (8-K document, section) of extracted text (section 6 ``raw.filings_text``).

    ``document`` is the EDGAR file name of the source document (for example
    ``aapl-20260730.htm``), ``doc_type`` is ``primary`` for the 8-K/8-K/A
    primary document or ``EX-99.1`` for an exhibit, ``section`` is the EDGAR
    item code the text belongs to (``2.02``) or ``unsegmented`` when the
    document could not be split confidently, and ``section_order`` is the
    1-based position of the section in the document. ``text`` is the cleaned
    plain text, ``sha256`` its lowercase hex digest and ``char_count`` its
    length in characters. ``acceptance_ts`` and ``ts_imputed`` are copied from
    ``raw.filings`` for the accession, so a section is known at exactly the
    time its filing was accepted. The unique key is
    ``(accession, document, section, section_order)``.
    """

    cik: str = pa.Field(str_matches=CIK_PATTERN)
    accession: str = pa.Field(str_matches=ACCESSION_PATTERN)
    acceptance_ts: pl.Datetime("us", "UTC")  # type: ignore[valid-type]
    ts_imputed: bool
    document: str = pa.Field(str_length={"min_value": 1})
    doc_type: str = pa.Field(isin=DOC_TYPES)
    section: str = pa.Field(str_matches=SECTION_PATTERN)
    section_order: int = pa.Field(ge=1)
    text: str
    sha256: str = pa.Field(str_matches=SHA256_PATTERN)
    char_count: int = pa.Field(ge=0)

    @pa.dataframe_check
    def char_count_matches_text(  # type: ignore[misc]  # pandera passes the model class, not an instance
        cls, data: PolarsData
    ) -> pl.LazyFrame:
        """``char_count`` is the character length of ``text``."""
        matches = pl.col("char_count") == pl.col("text").str.len_chars()
        return data.lazyframe.select(matches.alias("char_count_matches_text"))

    class Config:
        strict = True
        ordered = True
        unique = ("accession", "document", "section", "section_order")


class FxDailySchema(pa.DataFrameModel):
    """One row per (series, rate date) of a daily FX series (section 6 ``raw.fx_daily``).

    ``series`` is the Bank of Canada Valet series id (``FXUSDCAD``: Canadian
    dollars per US dollar), ``rate_date`` the observation date (a business day,
    ``DATE``), ``value`` the rate in ``series``' quotation (strictly positive,
    ``DOUBLE``) and ``known_at`` the instant the rate is treated as known:
    ``rate_date`` 17:00 ``America/Toronto`` in UTC (decision A-32), which is
    never earlier than publication. ``source`` is ``valet``. The unique key is
    ``(series, rate_date)``.
    """

    series: str = pa.Field(str_length={"min_value": 1})
    rate_date: pl.Date
    value: float = pa.Field(gt=0)
    known_at: pl.Datetime("us", "UTC")  # type: ignore[valid-type]
    source: str = pa.Field(isin=FX_SOURCES)

    class Config:
        strict = True
        ordered = True
        unique = ("series", "rate_date")


# The Phase 2 raw tables. They are deliberately not entries of ``SCHEMAS``,
# ``COLUMNS`` and ``DUCKDB_TYPES``: those three describe the Epic 01 staging
# tables that ``load.load_tables`` writes and that the staging CSVs in
# ``tests/fixtures/staging`` mirror. Their loaders arrive with their own issues
# and will then move these tables into the three staging mappings.
PHASE2_SCHEMAS: dict[str, type[pa.DataFrameModel]] = {
    "filings_text": FilingsTextSchema,
    "fx_daily": FxDailySchema,
}

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

PHASE2_COLUMNS: dict[str, list[str]] = {
    "filings_text": [
        "cik",
        "accession",
        "acceptance_ts",
        "ts_imputed",
        "document",
        "doc_type",
        "section",
        "section_order",
        "text",
        "sha256",
        "char_count",
    ],
    "fx_daily": [
        "series",
        "rate_date",
        "value",
        "known_at",
        "source",
    ],
}

PHASE2_DUCKDB_TYPES: dict[str, dict[str, str]] = {
    "filings_text": {
        "cik": "VARCHAR(10)",
        "accession": "VARCHAR",
        "acceptance_ts": "TIMESTAMPTZ",
        "ts_imputed": "BOOLEAN",
        "document": "VARCHAR",
        "doc_type": "VARCHAR",
        "section": "VARCHAR",
        "section_order": "INTEGER",
        "text": "VARCHAR",
        "sha256": "VARCHAR",
        "char_count": "INTEGER",
    },
    "fx_daily": {
        "series": "VARCHAR",
        "rate_date": "DATE",
        "value": "DOUBLE",
        "known_at": "TIMESTAMPTZ",
        "source": "VARCHAR",
    },
}

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def duckdb_ddl(table: str, schema: str = "raw") -> str:
    """Return the ``CREATE TABLE`` statement for a raw table.

    ``table`` is one of the staging tables (``filings``, ``events_8k``,
    ``facts_raw``) or a Phase 2 table (``filings_text``, ``fx_daily``);
    ``schema`` is the DuckDB schema to create it in. Raises ``ValueError`` for
    an unknown table or an invalid schema name.
    """
    types = DUCKDB_TYPES.get(table) or PHASE2_DUCKDB_TYPES.get(table)
    if types is None:
        known = ", ".join(sorted({*DUCKDB_TYPES, *PHASE2_DUCKDB_TYPES}))
        raise ValueError(f"unknown staging table {table!r}; expected one of {known}")
    if not _IDENTIFIER_RE.fullmatch(schema):
        raise ValueError(f"invalid DuckDB schema name {schema!r}")
    columns = ",\n    ".join(f"{name} {dtype}" for name, dtype in types.items())
    return f"CREATE TABLE {schema}.{table} (\n    {columns}\n)"
