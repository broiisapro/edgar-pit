"""Tests for the staging schemas: the frozen column contract."""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb
import polars as pl
import pytest
from pandera.errors import SchemaError

from edgarpit.schemas import (
    COLUMNS,
    DUCKDB_TYPES,
    SCHEMAS,
    Events8kSchema,
    FactsRawSchema,
    FilingsSchema,
    duckdb_ddl,
)


def valid_filings() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "cik": ["0000320193"],
            "accession": ["0000320193-25-000079"],
            "form": ["10-K"],
            "filing_date": pl.Series([date(2025, 10, 31)], dtype=pl.Date),
            "report_date": pl.Series([date(2025, 9, 27)], dtype=pl.Date),
            "acceptance_ts": pl.Series(
                [datetime(2025, 10, 31, 18, 1, 59, tzinfo=UTC)], dtype=pl.Datetime("us", "UTC")
            ),
            "acceptance_ts_source": ["sgml_header"],
            "acceptance_ts_api": pl.Series([None], dtype=pl.Datetime("us", "UTC")),
            "ts_imputed": [False],
            "primary_document": ["aapl-20250927.htm"],
            "items": pl.Series([None], dtype=pl.String),
            "is_amendment": [False],
            "is_xbrl": [True],
            "source": ["recent"],
        }
    )


def valid_events() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "cik": ["0000320193", "0000320193"],
            "accession": ["0000320193-25-000080", "0000320193-25-000080"],
            "form": ["8-K", "8-K"],
            "acceptance_ts": pl.Series(
                [datetime(2025, 10, 30, 20, 5, 0, tzinfo=UTC)] * 2, dtype=pl.Datetime("us", "UTC")
            ),
            "ts_imputed": [False, False],
            "item_code": ["2.02", "9.01"],
            "item_label": [
                "Results of Operations and Financial Condition",
                "Financial Statements and Exhibits",
            ],
        }
    )


def valid_facts() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "cik": ["0000320193", "0000320193"],
            "taxonomy": ["us-gaap", "dei"],
            "concept": [
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "EntityCommonStockSharesOutstanding",
            ],
            "unit": ["USD", "shares"],
            "value": [416_161_000_000.0, 14_840_000_000.0],
            "period_start": pl.Series([date(2024, 9, 29), None], dtype=pl.Date),
            "period_end": pl.Series([date(2025, 9, 27), date(2025, 10, 24)], dtype=pl.Date),
            "fy": [2025, 2025],
            "fp": ["FY", "FY"],
            "form": ["10-K", "10-K"],
            "accession": ["0000320193-25-000079", "0000320193-25-000079"],
            "filed": pl.Series([date(2025, 10, 31), date(2025, 10, 31)], dtype=pl.Date),
            "frame": pl.Series([None, None], dtype=pl.String),
        }
    )


def test_valid_frames_pass() -> None:
    FilingsSchema.validate(valid_filings())
    Events8kSchema.validate(valid_events())
    FactsRawSchema.validate(valid_facts())


def test_empty_frames_pass() -> None:
    FilingsSchema.validate(valid_filings().head(0))
    Events8kSchema.validate(valid_events().head(0))
    FactsRawSchema.validate(valid_facts().head(0))


@pytest.mark.parametrize("bad_cik", ["32193", "00003201930", "0000320193x", ""])
def test_bad_cik_is_rejected(bad_cik: str) -> None:
    with pytest.raises(SchemaError):
        FilingsSchema.validate(valid_filings().with_columns(pl.lit(bad_cik).alias("cik")))


@pytest.mark.parametrize(
    "bad_accession",
    ["000032019325000079", "0000320193-2025-000079", "0000320193-25-00007", "abc"],
)
def test_bad_accession_is_rejected(bad_accession: str) -> None:
    with pytest.raises(SchemaError):
        FilingsSchema.validate(
            valid_filings().with_columns(pl.lit(bad_accession).alias("accession"))
        )


def test_naive_acceptance_timestamp_is_rejected() -> None:
    naive = valid_filings().with_columns(
        pl.Series("acceptance_ts", [datetime(2025, 10, 31, 18, 1, 59)], dtype=pl.Datetime("us"))
    )
    with pytest.raises(SchemaError):
        FilingsSchema.validate(naive)


def test_unknown_acceptance_source_is_rejected() -> None:
    with pytest.raises(SchemaError):
        FilingsSchema.validate(
            valid_filings().with_columns(pl.lit("submissions_api").alias("acceptance_ts_source"))
        )


def test_unknown_taxonomy_is_rejected() -> None:
    with pytest.raises(SchemaError):
        FactsRawSchema.validate(valid_facts().with_columns(pl.lit("srt").alias("taxonomy")))


def test_column_order_is_enforced() -> None:
    reordered = valid_filings().select("accession", "cik", *COLUMNS["filings"][2:])
    with pytest.raises(SchemaError):
        FilingsSchema.validate(reordered)


def test_extra_and_missing_columns_are_rejected() -> None:
    with pytest.raises(SchemaError):
        FilingsSchema.validate(valid_filings().with_columns(pl.lit(1).alias("extra")))
    with pytest.raises(SchemaError):
        FilingsSchema.validate(valid_filings().drop("source"))


def test_null_in_non_nullable_column_is_rejected() -> None:
    with pytest.raises(SchemaError):
        FilingsSchema.validate(
            valid_filings().with_columns(pl.lit(None, dtype=pl.String).alias("form"))
        )


def test_duplicate_accession_is_rejected() -> None:
    with pytest.raises(SchemaError):
        FilingsSchema.validate(pl.concat([valid_filings(), valid_filings()]))


def test_duplicate_8k_item_key_is_rejected() -> None:
    with pytest.raises(SchemaError):
        Events8kSchema.validate(pl.concat([valid_events(), valid_events()]))


def test_columns_match_schema_order() -> None:
    for table, schema in SCHEMAS.items():
        assert list(schema.to_schema().columns) == COLUMNS[table]


def test_duckdb_types_cover_exactly_the_contract_columns() -> None:
    assert set(DUCKDB_TYPES) == set(COLUMNS)
    for table, columns in COLUMNS.items():
        assert list(DUCKDB_TYPES[table]) == columns


def test_duckdb_ddl_creates_queryable_tables() -> None:
    con = duckdb.connect()
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute("SET TimeZone='UTC'")
        for table in COLUMNS:
            con.execute(duckdb_ddl(table))
        con.execute(duckdb_ddl("filings", schema="main"))
        columns = [row[0] for row in con.execute("DESCRIBE raw.filings").fetchall()]
        assert columns == COLUMNS["filings"]
        types = {row[0]: row[1] for row in con.execute("DESCRIBE raw.facts_raw").fetchall()}
        assert types["value"] == "DOUBLE"
        assert types["period_start"] == "DATE"
        con.execute("INSERT INTO raw.filings SELECT * FROM raw.filings")
    finally:
        con.close()


def test_duckdb_ddl_rejects_unknown_table_and_schema() -> None:
    with pytest.raises(ValueError):
        duckdb_ddl("facts")
    with pytest.raises(ValueError):
        duckdb_ddl("filings", schema="raw; DROP TABLE filings")
