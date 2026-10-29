"""USD reads through :mod:`edgarpit.query` (decisions A-32, A-38).

The database is ``tests/pit_tables.py`` plus a ``main.fundamentals_pit_usd``
built from ``tests/doubles.py``, so ``main.fundamentals_pit`` and
``main.events_8k`` stay usable for issuer resolution and the USD model is the
same shape dbt produces.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import doubles
import duckdb
import pit_tables
import polars as pl
import pytest

from edgarpit import query
from edgarpit.query import MissingTableError

RY_CIK = "0001000275"
AAPL_CIK = "0000320193"
SHOP_CIK = "0001594805"

RY_ORIGINAL_ACCEPTED = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
RY_RESTATED_ACCEPTED = datetime(2024, 12, 4, 16, 30, 0, tzinfo=UTC)
AAPL_ACCEPTED = datetime(2024, 11, 1, 16, 30, 6, tzinfo=UTC)

# The rate for 2023-10-31 is published at 21:00 UTC (17:00 America/Toronto).
OCT_31_RATE_KNOWN_AT = doubles.fx_known_at(date(2023, 10, 31))
FX_RATE = 1.3659

# Shopify's 8-K was accepted the same afternoon, before the rate for its period
# end existed: the USD value must stay invisible until 21:00 UTC.
SHOP_ACCEPTED = datetime(2023, 10, 31, 15, 0, 0, tzinfo=UTC)

PIT_FUNDAMENTALS = [
    pit_tables.FundamentalRow(
        cik=RY_CIK,
        ticker="RY",
        field="revenue",
        value=49_000_000_000.0,
        period_start=date(2021, 11, 1),
        period_end=date(2022, 10, 31),
        accession="0001193125-22-280000",
        acceptance_ts=datetime(2022, 11, 30, 11, 45, 12, tzinfo=UTC),
        unit="CAD",
        currency="CAD",
        form="40-F",
        source_taxonomy="ifrs-full",
        source_concept="Revenue",
    ),
    pit_tables.FundamentalRow(
        cik=RY_CIK,
        ticker="RY",
        field="revenue",
        value=56_129_000_000.0,
        period_start=date(2022, 11, 1),
        period_end=date(2023, 10, 31),
        accession="0001193125-23-285639",
        acceptance_ts=RY_ORIGINAL_ACCEPTED,
        unit="CAD",
        currency="CAD",
        form="40-F",
        source_taxonomy="ifrs-full",
        source_concept="Revenue",
        first_value=56_129_000_000.0,
    ),
    pit_tables.FundamentalRow(
        cik=RY_CIK,
        ticker="RY",
        field="revenue",
        value=51_464_000_000.0,
        period_start=date(2022, 11, 1),
        period_end=date(2023, 10, 31),
        accession="0001193125-24-270294",
        acceptance_ts=RY_RESTATED_ACCEPTED,
        unit="CAD",
        currency="CAD",
        form="40-F",
        source_taxonomy="ifrs-full",
        source_concept="Revenue",
        version=2,
        first_value=56_129_000_000.0,
        is_restatement=True,
        restatement_kind="revision",
    ),
    pit_tables.FundamentalRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        field="revenue",
        value=391_035_000_000.0,
        period_start=date(2023, 10, 1),
        period_end=date(2024, 9, 28),
        accession="0000320193-24-000123",
        acceptance_ts=AAPL_ACCEPTED,
        form="10-K",
        source_concept="RevenueFromContractWithCustomerExcludingAssessedTax",
    ),
    pit_tables.FundamentalRow(
        cik=SHOP_CIK,
        ticker="SHOP",
        field="revenue",
        value=7_000_000_000.0,
        period_start=date(2022, 11, 1),
        period_end=date(2023, 10, 31),
        accession="0001594805-23-000042",
        acceptance_ts=SHOP_ACCEPTED,
        unit="CAD",
        currency="CAD",
        form="6-K",
        source_taxonomy="ifrs-full",
        source_concept="Revenue",
    ),
]

USD_ROWS = [
    doubles.FundamentalUsdRow(
        cik=RY_CIK,
        field="revenue",
        value=56_129_000_000.0,
        period_start=date(2022, 11, 1),
        period_end=date(2023, 10, 31),
        accession="0001193125-23-285639",
        acceptance_ts=RY_ORIGINAL_ACCEPTED,
        fx_rate=FX_RATE,
        fx_rate_date=date(2023, 10, 31),
        fx_known_at=OCT_31_RATE_KNOWN_AT,
    ),
    doubles.FundamentalUsdRow(
        cik=RY_CIK,
        field="revenue",
        value=51_464_000_000.0,
        period_start=date(2022, 11, 1),
        period_end=date(2023, 10, 31),
        accession="0001193125-24-270294",
        acceptance_ts=RY_RESTATED_ACCEPTED,
        version=2,
        first_value=56_129_000_000.0,
        is_restatement=True,
        pct_change_vs_first=51_464_000_000.0 / 56_129_000_000.0 - 1.0,
        restatement_kind="revision",
        fx_rate=FX_RATE,
        fx_rate_date=date(2023, 10, 31),
        fx_known_at=OCT_31_RATE_KNOWN_AT,
    ),
    # FY2022 has no observation at or before its period end, so it stays
    # unconverted and must never appear in a USD answer.
    doubles.FundamentalUsdRow(
        cik=RY_CIK,
        field="revenue",
        value=49_000_000_000.0,
        period_start=date(2021, 11, 1),
        period_end=date(2022, 10, 31),
        accession="0001193125-22-280000",
        acceptance_ts=datetime(2022, 11, 30, 11, 45, 12, tzinfo=UTC),
    ),
    doubles.FundamentalUsdRow(
        cik=RY_CIK,
        field="eps_diluted",
        value=9.25,
        period_start=date(2022, 11, 1),
        period_end=date(2023, 10, 31),
        accession="0001193125-23-285639",
        acceptance_ts=RY_ORIGINAL_ACCEPTED,
        unit="CAD/shares",
        currency="CAD",
        source_concept="EarningsPerShareDiluted",
        fx_rate=FX_RATE,
        fx_rate_date=date(2023, 10, 31),
        fx_known_at=OCT_31_RATE_KNOWN_AT,
    ),
    doubles.FundamentalUsdRow(
        cik=AAPL_CIK,
        field="revenue",
        value=391_035_000_000.0,
        period_start=date(2023, 10, 1),
        period_end=date(2024, 9, 28),
        accession="0000320193-24-000123",
        acceptance_ts=AAPL_ACCEPTED,
        form="10-K",
        unit="USD",
        currency="USD",
        source_taxonomy="us-gaap",
        source_concept="RevenueFromContractWithCustomerExcludingAssessedTax",
    ),
    doubles.FundamentalUsdRow(
        cik=SHOP_CIK,
        field="revenue",
        value=7_000_000_000.0,
        period_start=date(2022, 11, 1),
        period_end=date(2023, 10, 31),
        accession="0001594805-23-000042",
        acceptance_ts=SHOP_ACCEPTED,
        form="6-K",
        fx_rate=FX_RATE,
        fx_rate_date=date(2023, 10, 31),
        fx_known_at=OCT_31_RATE_KNOWN_AT,
    ),
]


def _insert(con: duckdb.DuckDBPyConnection, table: str, frame: pl.DataFrame) -> None:
    con.register("_phase2_frame", frame)
    try:
        con.execute(f"INSERT INTO {table} SELECT * FROM _phase2_frame")
    finally:
        con.unregister("_phase2_frame")


def build_usd_db(*, path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """Create the v1 tables and ``main.fundamentals_pit_usd`` from :data:`USD_ROWS`.

    ``path`` selects the database file; without it the database is in memory and
    the open connection is returned, which the caller closes.
    """
    con = pit_tables.build_pit_db(PIT_FUNDAMENTALS, path=path)
    con.execute(doubles.FUNDAMENTALS_PIT_USD_DDL)
    _insert(con, "main.fundamentals_pit_usd", doubles.fundamentals_pit_usd_frame(USD_ROWS))
    return con


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = build_usd_db()
    yield connection
    connection.close()


@pytest.fixture
def v1_con() -> Iterator[duckdb.DuckDBPyConnection]:
    """A 1.0.0-shaped database: the v1 tables only, no FX model."""
    connection = pit_tables.build_pit_db(PIT_FUNDAMENTALS)
    yield connection
    connection.close()


def test_asof_usd_returns_the_converted_value_and_unit(con: duckdb.DuckDBPyConnection) -> None:
    result = query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", currency="USD", db=con)[0]

    assert result.value == pytest.approx(56_129_000_000.0 / FX_RATE)
    assert result.currency == "USD"
    assert result.unit == "USD"
    assert result.acceptance_ts == RY_ORIGINAL_ACCEPTED
    assert result.asof_ts == RY_ORIGINAL_ACCEPTED
    assert result.version == 1
    assert result.period_end == date(2023, 10, 31)


def test_asof_ts_is_the_exact_known_time_of_a_usd_value(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Decision A-41: a USD result carries the row's own known time, not a bound."""
    shop = query.asof(SHOP_CIK, "revenue", "2023-11-01T00:00:00Z", currency="USD", db=con)[0]

    assert shop.acceptance_ts == SHOP_ACCEPTED
    assert shop.asof_ts == OCT_31_RATE_KNOWN_AT
    assert shop.asof_ts > shop.acceptance_ts
    assert shop.value == pytest.approx(7_000_000_000.0 / FX_RATE)


def test_usd_asof_ts_is_never_earlier_than_the_acceptance_time(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Classic bug (decision A-41): a converted row's known time covers both inputs."""
    rows = query.history(RY_CIK, "revenue", db=con, currency="USD") + query.history(
        SHOP_CIK, "revenue", db=con, currency="USD"
    )

    assert rows
    for row in rows:
        assert row.asof_ts is not None
        assert row.asof_ts >= row.acceptance_ts
    shop = next(row for row in rows if row.cik == SHOP_CIK)
    assert shop.asof_ts == max(shop.acceptance_ts, OCT_31_RATE_KNOWN_AT)


def test_reported_reads_carry_no_asof_ts(con: duckdb.DuckDBPyConnection) -> None:
    asof = query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", db=con)
    history = query.history(RY_CIK, "revenue", db=con)

    assert asof and history
    assert {row.asof_ts for row in asof} == {None}
    assert {row.asof_ts for row in history} == {None}


def test_asof_usd_converts_a_per_share_unit_family(con: duckdb.DuckDBPyConnection) -> None:
    result = query.asof(RY_CIK, "eps_diluted", "2024-01-15T00:00:00Z", currency="USD", db=con)[0]

    assert result.unit == "USD/shares"
    assert result.currency == "USD"
    assert result.value == pytest.approx(9.25 / FX_RATE)


def test_asof_usd_leaves_a_usd_row_at_par(con: duckdb.DuckDBPyConnection) -> None:
    result = query.asof(AAPL_CIK, "revenue", "2025-01-01T00:00:00Z", currency="USD", db=con)[0]

    assert result.value == 391_035_000_000.0
    assert result.unit == "USD"
    assert result.currency == "USD"
    assert result.acceptance_ts == AAPL_ACCEPTED


def test_a_cad_value_waits_for_the_rate_that_converts_it(con: duckdb.DuckDBPyConnection) -> None:
    """Classic bug (decision A-32): the filing alone does not make the USD value known."""
    accepted_before_the_rate = query.asof(
        SHOP_CIK, "revenue", "2023-10-31T20:59:59Z", currency="USD", db=con
    )
    at_the_publication = query.asof(
        SHOP_CIK, "revenue", "2023-10-31T21:00:00Z", currency="USD", db=con
    )
    reported = query.asof(SHOP_CIK, "revenue", "2023-10-31T20:59:59Z", db=con)

    assert SHOP_ACCEPTED < OCT_31_RATE_KNOWN_AT
    assert accepted_before_the_rate == []
    assert [row.acceptance_ts for row in at_the_publication] == [SHOP_ACCEPTED]
    assert at_the_publication[0].value == pytest.approx(7_000_000_000.0 / FX_RATE)
    assert [row.value for row in reported] == [7_000_000_000.0]


def test_a_restatement_is_not_returned_in_usd_before_it_was_accepted(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Classic bug (decision A-12): the as-of rule still applies to the USD model."""
    before = query.asof(RY_CIK, "revenue", "2024-06-01T00:00:00Z", currency="USD", db=con)
    boundary = query.asof(RY_CIK, "revenue", RY_RESTATED_ACCEPTED, currency="USD", db=con)
    after = query.asof(RY_CIK, "revenue", "2025-01-01T00:00:00Z", currency="USD", db=con)

    assert [(row.value, row.version) for row in before] == [
        (pytest.approx(56_129_000_000.0 / FX_RATE), 1)
    ]
    assert [(row.value, row.version) for row in boundary] == [
        (pytest.approx(51_464_000_000.0 / FX_RATE), 2)
    ]
    assert after[0].is_restatement is True
    assert after[0].restatement_kind == "revision"


def test_asof_usd_returns_n_periods_and_filters_period_type(
    con: duckdb.DuckDBPyConnection,
) -> None:
    annual = query.asof(
        RY_CIK, "revenue", "2025-01-01T00:00:00Z", "FY", n=10, currency="USD", db=con
    )
    quarterly = query.asof(
        RY_CIK, "revenue", "2025-01-01T00:00:00Z", "Q", n=10, currency="USD", db=con
    )

    # FY2022 is unconverted, so the USD answer holds only the FY2023 period.
    assert [row.period_end for row in annual] == [date(2023, 10, 31)]
    assert quarterly == []


def test_history_usd_returns_only_converted_rows_in_order(
    con: duckdb.DuckDBPyConnection,
) -> None:
    rows = query.history(RY_CIK, "revenue", db=con, currency="USD")

    assert [(row.period_end, row.version) for row in rows] == [
        (date(2023, 10, 31), 1),
        (date(2023, 10, 31), 2),
    ]
    assert [row.value for row in rows] == [
        pytest.approx(56_129_000_000.0 / FX_RATE),
        pytest.approx(51_464_000_000.0 / FX_RATE),
    ]
    assert {row.currency for row in rows} == {"USD"}
    assert {row.unit for row in rows} == {"USD"}
    assert [row.asof_ts for row in rows] == [RY_ORIGINAL_ACCEPTED, RY_RESTATED_ACCEPTED]


def test_history_usd_filters_period_type(con: duckdb.DuckDBPyConnection) -> None:
    assert query.history(RY_CIK, "revenue", "Q", db=con, currency="USD") == []
    assert len(query.history(RY_CIK, "revenue", "FY", db=con, currency="USD")) == 2


def test_reported_reads_are_unchanged_by_the_usd_model(con: duckdb.DuckDBPyConnection) -> None:
    reported = query.history(RY_CIK, "revenue", db=con)

    assert [row.value for row in reported] == [49_000_000_000.0, 56_129_000_000.0, 51_464_000_000.0]
    assert {row.unit for row in reported} == {"CAD"}
    assert {row.currency for row in reported} == {"CAD"}


def test_a_database_without_the_fx_model_names_the_missing_table(
    v1_con: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(MissingTableError, match=r"main\.fundamentals_pit_usd"):
        query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", currency="USD", db=v1_con)
    with pytest.raises(MissingTableError, match=r"main\.fundamentals_pit_usd"):
        query.history(RY_CIK, "revenue", db=v1_con, currency="USD")
    # The 1.0.0 read path keeps working.
    assert query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", db=v1_con)


@pytest.mark.parametrize("currency", ["CAD", "usd", "EUR"])
def test_unsupported_currency_is_rejected(con: duckdb.DuckDBPyConnection, currency: str) -> None:
    with pytest.raises(ValueError, match="currency"):
        # exercise the runtime guard for a currency outside the Literal
        query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", currency=currency, db=con)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="currency"):
        query.history(RY_CIK, "revenue", db=con, currency=currency)  # type: ignore[arg-type]


def test_usd_reads_keep_accepts_and_errors_of_the_reported_reads(
    con: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(ValueError, match="timezone"):
        query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00", currency="USD", db=con)
    with pytest.raises(ValueError, match="n"):
        query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", n=0, currency="USD", db=con)
    with pytest.raises(LookupError, match="ticker"):
        query.asof("ZZZZ", "revenue", "2024-01-15T00:00:00Z", currency="USD", db=con)
    assert query.asof("ry", "revenue", "2024-01-15T00:00:00Z", currency="USD", db=con)
