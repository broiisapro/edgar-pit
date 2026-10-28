"""Tests for the staging schemas: the frozen column contract."""

from __future__ import annotations

from datetime import UTC, date, datetime

import doubles
import duckdb
import polars as pl
import pytest
from pandera.errors import SchemaError

from edgarpit.schemas import (
    COLUMNS,
    DUCKDB_TYPES,
    PHASE2_COLUMNS,
    PHASE2_DUCKDB_TYPES,
    PHASE2_SCHEMAS,
    SCHEMAS,
    Events8kSchema,
    FactsRawSchema,
    FilingsSchema,
    FilingsTextSchema,
    FxDailySchema,
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


# --- Phase 2 raw tables: raw.filings_text and raw.fx_daily -------------------


def test_phase2_frames_pass() -> None:
    FilingsTextSchema.validate(doubles.filings_text_frame())
    FxDailySchema.validate(doubles.fx_daily_frame())


def test_phase2_empty_frames_pass() -> None:
    FilingsTextSchema.validate(doubles.filings_text_frame().head(0))
    FxDailySchema.validate(doubles.fx_daily_frame().head(0))


def test_phase2_columns_match_schema_order() -> None:
    for table, schema in PHASE2_SCHEMAS.items():
        assert list(schema.to_schema().columns) == PHASE2_COLUMNS[table]


def test_phase2_duckdb_types_cover_exactly_the_columns() -> None:
    assert set(PHASE2_DUCKDB_TYPES) == set(PHASE2_COLUMNS)
    for table, columns in PHASE2_COLUMNS.items():
        assert list(PHASE2_DUCKDB_TYPES[table]) == columns


def test_phase2_tables_leave_the_epic1_staging_contract_alone() -> None:
    # The Phase 2 tables are declared beside the staging tables, not in them:
    # load.load_tables and the staging CSVs still describe exactly three tables.
    assert set(SCHEMAS) == {"filings", "events_8k", "facts_raw"}
    assert set(COLUMNS) == {"filings", "events_8k", "facts_raw"}
    assert set(DUCKDB_TYPES) == set(COLUMNS)
    assert set(PHASE2_SCHEMAS) == {"filings_text", "fx_daily"}


def test_duckdb_ddl_creates_the_phase2_tables() -> None:
    con = duckdb.connect()
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute("SET TimeZone='UTC'")
        for table in PHASE2_COLUMNS:
            con.execute(duckdb_ddl(table))
        for table, columns in PHASE2_COLUMNS.items():
            described = [row[0] for row in con.execute(f"DESCRIBE raw.{table}").fetchall()]
            assert described == columns
        types = {row[0]: row[1] for row in con.execute("DESCRIBE raw.fx_daily").fetchall()}
        assert types["rate_date"] == "DATE"
        assert types["known_at"] == "TIMESTAMP WITH TIME ZONE"
        assert types["value"] == "DOUBLE"
        con.execute("INSERT INTO raw.filings_text SELECT * FROM raw.filings_text")
    finally:
        con.close()


def test_filings_text_rejects_unknown_doc_type() -> None:
    frame = doubles.filings_text_frame().with_columns(pl.lit("EX-99.2").alias("doc_type"))
    with pytest.raises(SchemaError):
        FilingsTextSchema.validate(frame)


@pytest.mark.parametrize("bad_section", ["Item 2.02", "2.2", "", "unsegmented.1"])
def test_filings_text_rejects_unknown_section(bad_section: str) -> None:
    frame = doubles.filings_text_frame().with_columns(pl.lit(bad_section).alias("section"))
    with pytest.raises(SchemaError):
        FilingsTextSchema.validate(frame)


@pytest.mark.parametrize("bad_sha", ["ABC", "0" * 63, "g" * 64, "A" * 64])
def test_filings_text_rejects_bad_sha256(bad_sha: str) -> None:
    frame = doubles.filings_text_frame().with_columns(pl.lit(bad_sha).alias("sha256"))
    with pytest.raises(SchemaError):
        FilingsTextSchema.validate(frame)


@pytest.mark.parametrize("bad_order", [0, -1])
def test_filings_text_rejects_non_positive_section_order(bad_order: int) -> None:
    frame = doubles.filings_text_frame().with_columns(pl.lit(bad_order).alias("section_order"))
    with pytest.raises(SchemaError):
        FilingsTextSchema.validate(frame)


def test_filings_text_rejects_a_char_count_that_disagrees_with_the_text() -> None:
    # Classic bug: a count taken before whitespace normalisation, or in bytes
    # rather than characters, still looks plausible. The schema catches it.
    frame = doubles.filings_text_frame().with_columns(
        (pl.col("char_count") + 1).alias("char_count")
    )
    with pytest.raises(SchemaError, match="char_count_matches_text"):
        FilingsTextSchema.validate(frame)


def test_filings_text_rejects_a_duplicate_section_key() -> None:
    frame = doubles.filings_text_frame()
    with pytest.raises(SchemaError):
        FilingsTextSchema.validate(pl.concat([frame, frame.head(1)]))


def test_fx_daily_rejects_a_non_positive_rate() -> None:
    frame = doubles.fx_daily_frame().with_columns(pl.lit(0.0).alias("value"))
    with pytest.raises(SchemaError):
        FxDailySchema.validate(frame)


def test_fx_daily_rejects_an_unknown_source() -> None:
    frame = doubles.fx_daily_frame().with_columns(pl.lit("ecb").alias("source"))
    with pytest.raises(SchemaError):
        FxDailySchema.validate(frame)


def test_fx_daily_rejects_a_duplicate_series_date() -> None:
    frame = doubles.fx_daily_frame()
    with pytest.raises(SchemaError):
        FxDailySchema.validate(pl.concat([frame, frame.head(1)]))


def test_fx_daily_rejects_a_naive_known_at() -> None:
    frame = doubles.fx_daily_frame().with_columns(
        pl.Series("known_at", [datetime(2023, 10, 31, 21, 0)] * 3, dtype=pl.Datetime("us"))
    )
    with pytest.raises(SchemaError):
        FxDailySchema.validate(frame)


def test_fundamentals_pit_usd_double_follows_the_conversion_rule() -> None:
    frame = doubles.fundamentals_pit_usd_frame()
    assert frame.columns == doubles.FUNDAMENTALS_PIT_USD_COLUMNS
    converted, unconverted = frame.iter_rows(named=True)
    # A CAD row converts at the observation's rate and is known only once both
    # the filing and the rate exist.
    assert converted["value_usd"] == pytest.approx(converted["value"] / converted["fx_rate"])
    assert converted["converted"] is True
    assert converted["asof_ts"] == max(converted["acceptance_ts"], converted["fx_known_at"])
    # A row without a rate keeps its value in the original currency and is
    # flagged, never silently converted at 1.0.
    assert unconverted["value_usd"] is None
    assert unconverted["fx_rate"] is None
    assert unconverted["converted"] is False
    assert unconverted["asof_ts"] == unconverted["acceptance_ts"]


def test_fundamentals_pit_usd_double_converts_usd_at_one() -> None:
    row = doubles.FundamentalUsdRow(
        cik="0000320193",
        field="revenue",
        value=416_161_000_000.0,
        period_end=date(2025, 9, 27),
        accession="0000320193-25-000079",
        acceptance_ts=datetime(2025, 10, 31, 18, 1, 59, tzinfo=UTC),
        unit="USD",
        currency="USD",
        source_taxonomy="us-gaap",
    )
    frame = doubles.fundamentals_pit_usd_frame([row])
    record = frame.row(0, named=True)
    assert record["value_usd"] == row.value
    assert record["converted"] is True
    assert record["fx_rate"] is None
    assert record["asof_ts"] == row.acceptance_ts


def test_fundamentals_pit_usd_double_waits_for_a_late_rate() -> None:
    # The rate is published after the filing: the row is known at the rate, not
    # at the filing, so a query at the acceptance time cannot see it.
    late_rate = datetime(2023, 12, 1, 22, 0, tzinfo=UTC)
    row = doubles.FundamentalUsdRow(
        cik="0001000275",
        field="revenue",
        value=51_464_000_000.0,
        period_end=date(2023, 10, 31),
        accession="0001193125-23-285639",
        acceptance_ts=datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
        fx_rate=1.3659,
        fx_rate_date=date(2023, 10, 31),
        fx_known_at=late_rate,
    )
    record = doubles.fundamentals_pit_usd_frame([row]).row(0, named=True)
    assert record["asof_ts"] == late_rate
    assert record["asof_ts"] > record["acceptance_ts"]

