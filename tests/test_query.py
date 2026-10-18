"""Unit tests for :mod:`edgarpit.query` against ``tests/pit_tables.py`` data."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pit_tables
import pytest

from edgarpit import query
from edgarpit.query import AsOfResult, EventResult

RY_CIK = "0001000275"
AAPL_CIK = "0000320193"
SHOP_CIK = "0001594805"

RY_ORIGINAL_ACCEPTED = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
RY_RESTATED_ACCEPTED = datetime(2024, 12, 4, 16, 30, 0, tzinfo=UTC)

RY_FUNDAMENTALS = [
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
        pct_change_vs_first=51_464_000_000.0 / 56_129_000_000.0 - 1.0,
        restatement_kind="revision",
    ),
    pit_tables.FundamentalRow(
        cik=RY_CIK,
        ticker="RY",
        field="revenue",
        value=15_000_000_000.0,
        period_start=date(2023, 11, 1),
        period_end=date(2024, 1, 31),
        period_type="Q",
        accession="0001193125-24-100000",
        acceptance_ts=datetime(2024, 3, 5, 12, 0, 0, tzinfo=UTC),
        unit="CAD",
        currency="CAD",
        form="6-K",
        source_taxonomy="ifrs-full",
        source_concept="Revenue",
    ),
    pit_tables.FundamentalRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        field="revenue",
        value=391_035_000_000.0,
        period_start=date(2023, 10, 1),
        period_end=date(2024, 9, 28),
        accession="0000320193-24-000123",
        acceptance_ts=datetime(2024, 11, 1, 16, 30, 6, tzinfo=UTC),
        unit="USD",
        currency="USD",
        form="10-K",
        source_concept="RevenueFromContractWithCustomerExcludingAssessedTax",
    ),
]

EVENTS = [
    pit_tables.EventRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        accession="0000320193-25-000008",
        acceptance_ts=datetime(2025, 1, 30, 21, 30, 0, tzinfo=UTC),
        item_code="2.02",
        item_label="Results of Operations and Financial Condition",
    ),
    pit_tables.EventRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        accession="0000320193-25-000008",
        acceptance_ts=datetime(2025, 1, 30, 21, 30, 0, tzinfo=UTC),
        item_code="9.01",
        item_label="Financial Statements and Exhibits",
    ),
    pit_tables.EventRow(
        cik=SHOP_CIK,
        ticker="SHOP",
        accession="0001594805-25-000010",
        acceptance_ts=datetime(2025, 2, 11, 12, 0, 0, tzinfo=UTC),
        item_code="2.02",
        item_label="Results of Operations and Financial Condition",
        form="8-K/A",
    ),
]


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = pit_tables.build_pit_db(RY_FUNDAMENTALS, EVENTS)
    yield connection
    connection.close()


def test_asof_returns_the_latest_version_known_at_the_time(
    con: duckdb.DuckDBPyConnection,
) -> None:
    results = query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", "FY", db=con)

    assert len(results) == 1
    row = results[0]
    assert row == AsOfResult(
        cik=RY_CIK,
        ticker="RY",
        field="revenue",
        value=56_129_000_000.0,
        unit="CAD",
        currency="CAD",
        period_start=date(2022, 11, 1),
        period_end=date(2023, 10, 31),
        period_type="FY",
        accession="0001193125-23-285639",
        form="40-F",
        acceptance_ts=RY_ORIGINAL_ACCEPTED,
        version=1,
        is_restatement=False,
        ts_imputed=False,
        restatement_kind="none",
    )


def test_asof_boundary_acceptance_time_is_inclusive(
    con: duckdb.DuckDBPyConnection,
) -> None:
    original = query.asof(RY_CIK, "revenue", RY_ORIGINAL_ACCEPTED, db=con)[0]
    restated = query.asof(RY_CIK, "revenue", RY_RESTATED_ACCEPTED, db=con)[0]

    assert original.accession == "0001193125-23-285639"
    assert original.version == 1
    assert restated.accession == "0001193125-24-270294"
    assert restated.version == 2
    assert restated.is_restatement is True
    assert restated.restatement_kind == "revision"


def test_asof_returns_the_restatement_after_it_was_accepted(
    con: duckdb.DuckDBPyConnection,
) -> None:
    results = query.asof(RY_CIK, "revenue", "2025-06-01T00:00:00Z", db=con)

    assert [row.accession for row in results] == ["0001193125-24-270294"]
    assert results[0].value == 51_464_000_000.0
    assert results[0].is_restatement is True


def test_asof_returns_n_periods_newest_first(con: duckdb.DuckDBPyConnection) -> None:
    results = query.asof(RY_CIK, "revenue", "2025-06-01T00:00:00Z", "FY", n=2, db=con)

    assert [row.period_end for row in results] == [date(2023, 10, 31), date(2022, 10, 31)]
    assert [row.version for row in results] == [2, 1]


def test_asof_filters_period_type(con: duckdb.DuckDBPyConnection) -> None:
    annual = query.asof(RY_CIK, "revenue", "2025-06-01T00:00:00Z", "FY", n=10, db=con)
    quarterly = query.asof(RY_CIK, "revenue", "2025-06-01T00:00:00Z", "Q", n=10, db=con)

    assert {row.period_type for row in annual} == {"FY"}
    assert [row.period_end for row in quarterly] == [date(2024, 1, 31)]


def test_asof_is_scoped_to_the_issuer_and_field(con: duckdb.DuckDBPyConnection) -> None:
    apple = query.asof(AAPL_CIK, "revenue", "2025-06-01T00:00:00Z", db=con)

    assert [row.cik for row in apple] == [AAPL_CIK]
    assert apple[0].value == 391_035_000_000.0
    assert apple[0].unit == "USD"


def test_asof_accepts_iso_strings_with_offsets(con: duckdb.DuckDBPyConnection) -> None:
    same_instant = query.asof(RY_CIK, "revenue", "2024-01-15T05:30:00+05:30", db=con)
    before_restatement = query.asof(RY_CIK, "revenue", "2024-12-04T06:00:00-10:00", db=con)

    assert same_instant[0].acceptance_ts == RY_ORIGINAL_ACCEPTED
    assert before_restatement[0].accession == "0001193125-23-285639"


@pytest.mark.parametrize(
    "asof_ts",
    [
        datetime(2024, 1, 15),
        "2024-01-15T00:00:00",
        "2024-01-15",
        "not a timestamp",
    ],
)
def test_asof_rejects_naive_or_malformed_timestamps(
    con: duckdb.DuckDBPyConnection, asof_ts: datetime | str
) -> None:
    with pytest.raises(ValueError, match="timezone") as exc_info:
        query.asof(RY_CIK, "revenue", asof_ts, db=con)
    assert type(exc_info.value) is ValueError


def test_asof_rejects_invalid_n(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ValueError, match="n"):
        query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", n=0, db=con)


def test_asof_rejects_unsupported_period(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ValueError, match="period"):
        # exercise the runtime guard for a period outside the Literal
        query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", "OTHER", db=con)  # type: ignore[arg-type]


def test_unknown_identifier_raises_lookup_error(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(LookupError, match="ticker"):
        query.asof("ZZZZ", "revenue", "2024-01-15T00:00:00Z", db=con)
    with pytest.raises(LookupError, match="CIK"):
        query.asof("9999999999", "revenue", "2024-01-15T00:00:00Z", db=con)
    with pytest.raises(LookupError):
        query.history("ZZZZ", "revenue", db=con)
    with pytest.raises(LookupError):
        query.events("ZZZZ", db=con)
    with pytest.raises(LookupError):
        query.fields("ZZZZ", db=con)


def test_numeric_identifiers_are_zero_padded_ciks(con: duckdb.DuckDBPyConnection) -> None:
    results = query.asof("1000275", "revenue", "2024-01-15T00:00:00Z", db=con)

    assert results[0].cik == RY_CIK


def test_ticker_identifiers_are_case_insensitive(con: duckdb.DuckDBPyConnection) -> None:
    results = query.asof("ry", "revenue", "2024-01-15T00:00:00Z", db=con)

    assert results[0].ticker == "RY"


def test_events_only_issuer_resolves(con: duckdb.DuckDBPyConnection) -> None:
    assert query.asof("SHOP", "revenue", "2025-06-01T00:00:00Z", db=con) == []
    assert [row.cik for row in query.events("shop", db=con)] == [SHOP_CIK]


def test_empty_result_is_an_empty_list(con: duckdb.DuckDBPyConnection) -> None:
    assert query.asof(AAPL_CIK, "assets", "2025-06-01T00:00:00Z", db=con) == []


def test_db_none_reads_the_settings_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "edgarpit.duckdb"
    con = pit_tables.build_pit_db(RY_FUNDAMENTALS, EVENTS, path=db_path)
    con.close()
    monkeypatch.setenv("EDGARPIT_DB", str(db_path))

    results = query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z")

    assert results[0].accession == "0001193125-23-285639"


def test_database_paths_are_opened_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "edgarpit.duckdb"
    connection = pit_tables.build_pit_db(RY_FUNDAMENTALS, EVENTS, path=db_path)
    connection.close()

    calls: list[dict[str, object]] = []
    real_connect = duckdb.connect

    def spy(*args: object, **kwargs: object) -> duckdb.DuckDBPyConnection:
        calls.append(dict(kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(query.duckdb, "connect", spy)

    results = query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", db=db_path)

    assert results[0].accession == "0001193125-23-285639"
    assert calls and all(call.get("read_only") is True for call in calls)


def test_passed_connection_is_used_as_given_and_stays_open(
    con: duckdb.DuckDBPyConnection,
) -> None:
    assert query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", db=con)

    con.execute(
        "INSERT INTO main.events_8k VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            SHOP_CIK,
            "0001594805-25-000011",
            "8-K",
            datetime(2025, 3, 1, 12, 0, 0, tzinfo=UTC),
            False,
            "5.02",
            "Departure of Directors or Certain Officers",
            "SHOP",
        ],
    )

    assert query.events(SHOP_CIK, db=con)[-1].item_code == "5.02"


def test_connection_timezone_is_set_to_utc(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("SET TimeZone='America/New_York'")

    row = query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", db=con)[0]

    assert con.execute("SELECT current_setting('TimeZone')").fetchone() == ("UTC",)
    assert row.acceptance_ts == RY_ORIGINAL_ACCEPTED
    assert row.acceptance_ts.tzinfo is not None


def test_history_returns_every_version_in_order(con: duckdb.DuckDBPyConnection) -> None:
    rows = query.history(RY_CIK, "revenue", db=con)

    assert [(row.period_end, row.version) for row in rows] == [
        (date(2022, 10, 31), 1),
        (date(2023, 10, 31), 1),
        (date(2023, 10, 31), 2),
        (date(2024, 1, 31), 1),
    ]
    assert [row.value for row in rows] == [
        49_000_000_000.0,
        56_129_000_000.0,
        51_464_000_000.0,
        15_000_000_000.0,
    ]
    assert [row.restatement_kind for row in rows] == ["none", "none", "revision", "none"]
    assert rows[-1].period_type == "Q"


def test_history_filters_period_type(con: duckdb.DuckDBPyConnection) -> None:
    rows = query.history(RY_CIK, "revenue", "FY", db=con)

    assert {row.period_type for row in rows} == {"FY"}
    assert len(rows) == 3


def test_history_rejects_unsupported_period(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ValueError, match="period"):
        # exercise the runtime guard for a period outside the Literal
        query.history(RY_CIK, "revenue", "OTHER", db=con)  # type: ignore[arg-type]


def test_events_orders_by_acceptance_time(con: duckdb.DuckDBPyConnection) -> None:
    rows = query.events(AAPL_CIK, db=con)

    assert all(isinstance(row, EventResult) for row in rows)
    assert [(row.accession, row.item_code) for row in rows] == [
        ("0000320193-25-000008", "2.02"),
        ("0000320193-25-000008", "9.01"),
    ]
    assert rows[0].acceptance_ts == datetime(2025, 1, 30, 21, 30, 0, tzinfo=UTC)
    assert rows[0].ticker == "AAPL"


def test_events_filters_item_code(con: duckdb.DuckDBPyConnection) -> None:
    rows = query.events(AAPL_CIK, item="9.01", db=con)

    assert [row.item_code for row in rows] == ["9.01"]


def test_events_time_bounds_are_inclusive(con: duckdb.DuckDBPyConnection) -> None:
    first = datetime(2025, 1, 30, 21, 30, 0, tzinfo=UTC)
    second = datetime(2025, 2, 11, 12, 0, 0, tzinfo=UTC)

    assert [row.accession for row in query.events(SHOP_CIK, start=second, db=con)] == [
        "0001594805-25-000010"
    ]
    assert query.events(AAPL_CIK, end=first, db=con)
    assert query.events(AAPL_CIK, end="2025-01-30T21:29:59Z", db=con) == []
    assert query.events(SHOP_CIK, item="2.02", start=second, end=second, db=con) != []
    assert query.events(SHOP_CIK, start="2025-02-11T12:00:01Z", db=con) == []
    assert query.events(SHOP_CIK, end="2025-02-11T11:59:59Z", db=con) == []


@pytest.mark.parametrize("bound", ["2025-01-30T21:30:00", datetime(2025, 1, 30, 21, 30)])
def test_events_rejects_naive_bounds(con: duckdb.DuckDBPyConnection, bound: datetime | str) -> None:
    with pytest.raises(ValueError, match="timezone"):
        query.events(AAPL_CIK, start=bound, db=con)
    with pytest.raises(ValueError, match="timezone"):
        query.events(AAPL_CIK, end=bound, db=con)


def test_issuers_lists_cik_ticker_pairs(con: duckdb.DuckDBPyConnection) -> None:
    assert query.issuers(db=con) == [
        (AAPL_CIK, "AAPL"),
        (RY_CIK, "RY"),
        (SHOP_CIK, "SHOP"),
    ]


def test_fields_are_sorted_and_scoped(con: duckdb.DuckDBPyConnection) -> None:
    assert query.fields(RY_CIK, db=con) == ["revenue"]
    assert query.fields("SHOP", db=con) == []
