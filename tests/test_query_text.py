"""8-K text through :mod:`edgarpit.query` (decisions A-31, A-40).

The database is ``tests/pit_tables.py`` plus ``raw.filings_text`` from
``tests/doubles.py`` and the ``main.filings_text`` view dbt builds from it (the
raw table plus the issuer's ticker). Two Apple 8-K filings carry text: one with
Item 2.02 and 9.01 plus an ``EX-99.1`` exhibit, one with Item 9.01 only.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import doubles
import duckdb
import pit_tables
import polars as pl
import pytest

from edgarpit import query
from edgarpit.query import MissingTableError, TextResult
from edgarpit.schemas import duckdb_ddl

AAPL_CIK = "0000320193"
RY_CIK = "0001000275"

FIRST_8K_ACCEPTED = datetime(2025, 10, 30, 20, 5, 0, tzinfo=UTC)
SECOND_8K_ACCEPTED = datetime(2025, 11, 6, 21, 10, 0, tzinfo=UTC)

FIRST_8K = "0000320193-25-000080"
SECOND_8K = "0000320193-25-000091"
PRIMARY = "aapl-20251030.htm"
EXHIBIT = "a8kexhibit991.htm"

PIT_FUNDAMENTALS = [
    pit_tables.FundamentalRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        field="revenue",
        value=102_500_000_000.0,
        period_end=datetime(2025, 9, 27).date(),
        accession="0000320193-25-000070",
        acceptance_ts=datetime(2025, 10, 30, 20, 5, 0, tzinfo=UTC),
    ),
    pit_tables.FundamentalRow(
        cik=RY_CIK,
        ticker="RY",
        field="revenue",
        value=51_464_000_000.0,
        period_end=datetime(2023, 10, 31).date(),
        accession="0001193125-24-270294",
        acceptance_ts=datetime(2024, 12, 4, 16, 30, 0, tzinfo=UTC),
        unit="CAD",
        currency="CAD",
        form="40-F",
        source_taxonomy="ifrs-full",
        source_concept="Revenue",
    ),
]

EVENTS = [
    pit_tables.EventRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        accession=FIRST_8K,
        acceptance_ts=FIRST_8K_ACCEPTED,
        item_code="2.02",
        item_label="Results of Operations and Financial Condition",
    ),
    pit_tables.EventRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        accession=FIRST_8K,
        acceptance_ts=FIRST_8K_ACCEPTED,
        item_code="9.01",
        item_label="Financial Statements and Exhibits",
    ),
    pit_tables.EventRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        accession=SECOND_8K,
        acceptance_ts=SECOND_8K_ACCEPTED,
        item_code="9.01",
        item_label="Financial Statements and Exhibits",
    ),
]

TEXT_ROWS = [
    doubles.FilingsTextRow(
        accession=FIRST_8K,
        document=PRIMARY,
        section="2.02",
        section_order=1,
        acceptance_ts=FIRST_8K_ACCEPTED,
        text="Item 2.02 Results of Operations and Financial Condition\n\n"
        "The Company reported quarterly revenue of $102.5 billion.",
    ),
    doubles.FilingsTextRow(
        accession=FIRST_8K,
        document=PRIMARY,
        section="9.01",
        section_order=2,
        acceptance_ts=FIRST_8K_ACCEPTED,
        text="Item 9.01 Financial Statements and Exhibits\n\n(d) Exhibits\n99.1 Press release.",
    ),
    doubles.FilingsTextRow(
        accession=FIRST_8K,
        document=EXHIBIT,
        doc_type="EX-99.1",
        section="unsegmented",
        acceptance_ts=FIRST_8K_ACCEPTED,
        text="Apple reports fourth quarter results.",
    ),
    doubles.FilingsTextRow(
        accession=SECOND_8K,
        document=PRIMARY,
        section="unsegmented",
        acceptance_ts=SECOND_8K_ACCEPTED,
        text="Item 9.01 Financial Statements and Exhibits\n\n(d) Exhibits\n99.1 Press release.",
    ),
]


def _insert(con: duckdb.DuckDBPyConnection, table: str, frame: pl.DataFrame) -> None:
    con.register("_text_frame", frame)
    try:
        con.execute(f"INSERT INTO {table} SELECT * FROM _text_frame")
    finally:
        con.unregister("_text_frame")


def build_text_db(*, path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """Create the v1 tables, ``raw.filings_text`` and ``main.filings_text``.

    ``main.filings_text`` is ``raw.filings_text`` plus the issuer's ``ticker``
    (decision A-38), the shape the dbt model has. ``path`` selects the database
    file; without it the database is in memory and the open connection is
    returned, which the caller closes.
    """
    con = pit_tables.build_pit_db(PIT_FUNDAMENTALS, EVENTS, path=path)
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute(duckdb_ddl("filings_text"))
    _insert(con, "raw.filings_text", doubles.filings_text_frame(TEXT_ROWS))
    con.execute(
        """
        CREATE TABLE main.filings_text AS
        SELECT text.cik, text.accession, text.acceptance_ts, text.ts_imputed, text.document,
               text.doc_type, text.section, text.section_order, text.text, text.sha256,
               text.char_count, issuers.ticker
        FROM raw.filings_text AS text
        LEFT JOIN (
            SELECT DISTINCT cik, ticker FROM main.fundamentals_pit
            UNION
            SELECT DISTINCT cik, ticker FROM main.events_8k
        ) AS issuers USING (cik)
        """
    )
    return con


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = build_text_db()
    yield connection
    connection.close()


@pytest.fixture
def v1_con() -> Iterator[duckdb.DuckDBPyConnection]:
    """A 1.0.0-shaped database: the v1 tables only, no 8-K text model."""
    connection = pit_tables.build_pit_db(PIT_FUNDAMENTALS, EVENTS)
    yield connection
    connection.close()


def test_filing_text_returns_every_section_in_query_order(con: duckdb.DuckDBPyConnection) -> None:
    rows = query.filing_text(AAPL_CIK, db=con)

    order = [(row.acceptance_ts, row.accession, row.document, row.section_order) for row in rows]
    assert order == [
        (FIRST_8K_ACCEPTED, FIRST_8K, EXHIBIT, 1),
        (FIRST_8K_ACCEPTED, FIRST_8K, PRIMARY, 1),
        (FIRST_8K_ACCEPTED, FIRST_8K, PRIMARY, 2),
        (SECOND_8K_ACCEPTED, SECOND_8K, PRIMARY, 1),
    ]
    assert [row.section for row in rows] == ["unsegmented", "2.02", "9.01", "unsegmented"]


def test_filing_text_mirrors_the_table_columns(con: duckdb.DuckDBPyConnection) -> None:
    row = query.filing_text(AAPL_CIK, db=con)[1]

    assert isinstance(row, TextResult)
    assert (row.cik, row.ticker, row.accession) == (AAPL_CIK, "AAPL", FIRST_8K)
    assert row.document == PRIMARY
    assert row.doc_type == "primary"
    assert row.ts_imputed is False
    assert row.text.startswith("Item 2.02 Results of Operations")
    assert row.sha256 == doubles.sha256_of(row.text)
    assert row.char_count == len(row.text)


def test_item_filter_keeps_the_whole_filing_including_its_exhibit(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Decision A-40: an Item 2.02 filing brings its other sections and exhibits."""
    rows = query.filing_text(AAPL_CIK, item="2.02", db=con)

    assert [row.accession for row in rows] == [FIRST_8K] * 3
    assert {row.doc_type for row in rows} == {"primary", "EX-99.1"}
    assert {row.section for row in rows} == {"2.02", "9.01", "unsegmented"}


def test_item_filter_matches_the_filings_own_item_list(con: duckdb.DuckDBPyConnection) -> None:
    both = query.filing_text(AAPL_CIK, item="9.01", db=con)
    none = query.filing_text(AAPL_CIK, item="5.02", db=con)

    assert [row.accession for row in both] == [FIRST_8K] * 3 + [SECOND_8K]
    assert none == []


def test_time_bounds_are_inclusive_on_acceptance_ts(con: duckdb.DuckDBPyConnection) -> None:
    first_only = query.filing_text(AAPL_CIK, end=FIRST_8K_ACCEPTED, db=con)
    second_only = query.filing_text(AAPL_CIK, start=SECOND_8K_ACCEPTED, db=con)
    before_any = query.filing_text(AAPL_CIK, end="2025-10-30T20:04:59Z", db=con)

    assert {row.accession for row in first_only} == {FIRST_8K}
    assert [row.accession for row in second_only] == [SECOND_8K]
    assert before_any == []


def test_iso_strings_with_offsets_are_accepted(con: duckdb.DuckDBPyConnection) -> None:
    rows = query.filing_text(
        AAPL_CIK, start="2025-10-30T10:05:00-10:00", end="2025-10-30T10:05:00-10:00", db=con
    )

    assert {row.accession for row in rows} == {FIRST_8K}


@pytest.mark.parametrize("bound", ["2025-10-30T20:05:00", datetime(2025, 10, 30, 20, 5)])
def test_naive_bounds_are_rejected(con: duckdb.DuckDBPyConnection, bound: datetime | str) -> None:
    with pytest.raises(ValueError, match="timezone"):
        query.filing_text(AAPL_CIK, start=bound, db=con)
    with pytest.raises(ValueError, match="timezone"):
        query.filing_text(AAPL_CIK, end=bound, db=con)


def test_issuer_without_text_returns_an_empty_list(con: duckdb.DuckDBPyConnection) -> None:
    assert query.filing_text(RY_CIK, db=con) == []
    assert query.filing_text("aapl", db=con)


def test_unknown_identifier_raises_lookup_error(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(LookupError, match="ticker"):
        query.filing_text("ZZZZ", db=con)


def test_a_database_without_the_text_model_names_the_missing_table(
    v1_con: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(MissingTableError, match=r"main\.filings_text"):
        query.filing_text(AAPL_CIK, db=v1_con)
    # The 1.0.0 read paths keep working.
    assert query.events(AAPL_CIK, db=v1_con)
    assert query.asof(AAPL_CIK, "revenue", "2025-12-01T00:00:00Z", db=v1_con)
