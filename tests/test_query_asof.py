"""Lookahead classic-bug tests and the as-of property test (PRD §12).

These tests are the heart of the point-in-time contract: a value is known at
``asof_ts`` if and only if its filing's ``acceptance_ts <= asof_ts``. Each
classic-bug test can fail against a deliberately broken ``asof`` (for example
one that filters on ``period_end`` or that skips the per-period version choice).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal

import pit_tables
from hypothesis import given, settings
from hypothesis import strategies as st

from edgarpit import query

RY_CIK = "0001000275"
AAPL_CIK = "0000320193"

RY_ORIGINAL_ACCEPTED = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
RY_RESTATED_ACCEPTED = datetime(2024, 12, 4, 16, 30, 0, tzinfo=UTC)

RY_ORIGINAL_VALUE = 56_129_000_000.0
RY_RESTATED_VALUE = 51_464_000_000.0

RY_ROWS = [
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
        value=RY_ORIGINAL_VALUE,
        period_start=date(2022, 11, 1),
        period_end=date(2023, 10, 31),
        accession="0001193125-23-285639",
        acceptance_ts=RY_ORIGINAL_ACCEPTED,
        unit="CAD",
        currency="CAD",
        form="40-F",
        source_taxonomy="ifrs-full",
        source_concept="Revenue",
        first_value=RY_ORIGINAL_VALUE,
    ),
    pit_tables.FundamentalRow(
        cik=RY_CIK,
        ticker="RY",
        field="revenue",
        value=RY_RESTATED_VALUE,
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
        first_value=RY_ORIGINAL_VALUE,
        is_restatement=True,
        restatement_kind="revision",
    ),
]

EVENT_ROWS = [
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
        cik=AAPL_CIK,
        ticker="AAPL",
        accession="0000320193-25-000012",
        acceptance_ts=datetime(2025, 2, 11, 13, 0, 0, tzinfo=UTC),
        item_code="2.02",
        item_label="Results of Operations and Financial Condition",
    ),
]


def test_restatement_accepted_after_asof_is_not_returned() -> None:
    """Before the restatement was accepted only the original value is known."""
    con = pit_tables.build_pit_db(RY_ROWS)
    try:
        before = query.asof(RY_CIK, "revenue", "2024-01-15T00:00:00Z", db=con)
        assert [(row.value, row.accession, row.version) for row in before] == [
            (RY_ORIGINAL_VALUE, "0001193125-23-285639", 1)
        ]
        assert before[0].is_restatement is False
        assert before[0].restatement_kind == "none"

        one_second_after = query.asof(
            RY_CIK, "revenue", RY_RESTATED_ACCEPTED + timedelta(seconds=1), db=con
        )
        assert [(row.value, row.accession, row.version) for row in one_second_after] == [
            (RY_RESTATED_VALUE, "0001193125-24-270294", 2)
        ]
        assert one_second_after[0].is_restatement is True
        assert one_second_after[0].restatement_kind == "revision"

        at_the_boundary = query.asof(RY_CIK, "revenue", RY_RESTATED_ACCEPTED, db=con)
        assert [(row.value, row.version) for row in at_the_boundary] == [(RY_RESTATED_VALUE, 2)]

        just_before = query.asof(
            RY_CIK, "revenue", RY_RESTATED_ACCEPTED - timedelta(seconds=1), db=con
        )
        assert [(row.value, row.version) for row in just_before] == [(RY_ORIGINAL_VALUE, 1)]
    finally:
        con.close()


def test_period_end_before_asof_but_filed_after_is_excluded() -> None:
    """FY2024 ends 2024-10-31, before the as-of, but its only version was
    accepted on 2025-01-15; a period_end rule would leak it."""
    rows = [
        *RY_ROWS,
        pit_tables.FundamentalRow(
            cik=RY_CIK,
            ticker="RY",
            field="revenue",
            value=58_000_000_000.0,
            period_start=date(2023, 11, 1),
            period_end=date(2024, 10, 31),
            accession="0001193125-25-010000",
            acceptance_ts=datetime(2025, 1, 15, 16, 0, 0, tzinfo=UTC),
            unit="CAD",
            currency="CAD",
            form="40-F",
            source_taxonomy="ifrs-full",
            source_concept="Revenue",
        ),
    ]
    asof_ts = datetime(2024, 12, 1, tzinfo=UTC)
    con = pit_tables.build_pit_db(rows)
    try:
        before_the_filing = query.asof(RY_CIK, "revenue", asof_ts, "FY", n=10, db=con)
        assert [(row.period_end, row.acceptance_ts) for row in before_the_filing] == [
            (date(2023, 10, 31), RY_ORIGINAL_ACCEPTED),
            (date(2022, 10, 31), datetime(2022, 11, 30, 11, 45, 12, tzinfo=UTC)),
        ]
        assert all(row.acceptance_ts <= asof_ts for row in before_the_filing)

        after_the_filing = query.asof(RY_CIK, "revenue", "2025-01-16T00:00:00Z", db=con)
        assert after_the_filing[0].period_end == date(2024, 10, 31)
        assert after_the_filing[0].value == 58_000_000_000.0
    finally:
        con.close()


def test_history_returns_every_version_in_order() -> None:
    con = pit_tables.build_pit_db(RY_ROWS)
    try:
        rows = query.history(RY_CIK, "revenue", db=con)

        assert [(row.period_end, row.version, row.accession, row.value) for row in rows] == [
            (date(2022, 10, 31), 1, "0001193125-22-280000", 49_000_000_000.0),
            (date(2023, 10, 31), 1, "0001193125-23-285639", RY_ORIGINAL_VALUE),
            (date(2023, 10, 31), 2, "0001193125-24-270294", RY_RESTATED_VALUE),
        ]
        acceptance_times = [row.acceptance_ts for row in rows]
        assert acceptance_times == sorted(acceptance_times)
    finally:
        con.close()


def test_events_bounds_are_inclusive() -> None:
    con = pit_tables.build_pit_db([], EVENT_ROWS)
    try:
        first = datetime(2025, 1, 30, 21, 30, 0, tzinfo=UTC)
        second = datetime(2025, 2, 11, 13, 0, 0, tzinfo=UTC)

        both = query.events(AAPL_CIK, start=first, end=second, db=con)
        assert [row.acceptance_ts for row in both] == [first, first, second]

        assert len(query.events(AAPL_CIK, start=first + timedelta(seconds=1), db=con)) == 1
        assert len(query.events(AAPL_CIK, end=second - timedelta(seconds=1), db=con)) == 2
        assert len(query.events(AAPL_CIK, start=second, end=second, db=con)) == 1
        assert query.events(AAPL_CIK, start=second + timedelta(seconds=1), db=con) == []
        assert len(query.events(AAPL_CIK, start="2025-01-30T21:30:00Z", db=con)) == 3
        assert len(query.events(AAPL_CIK, end="2025-02-11T13:00:00Z", db=con)) == 3
    finally:
        con.close()


_CIKS = ("0001000275", "0000320193", "0001594805")
_FIELDS = ("revenue", "net_income")
_PERIOD_TYPES = ("FY", "Q", "OTHER")
_ACCEPTANCE_POOL = tuple(
    datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=90 * step) for step in range(21)
)
_QUERY_PERIODS: tuple[Literal["FY", "Q"], ...] = ("FY", "Q")


def _accession(cik: str, sequence: int) -> str:
    return f"{cik}-{sequence // 1_000_000 % 100:02d}-{sequence % 1_000_000:06d}"


@st.composite
def _fundamental_rows(draw: st.DrawFn) -> list[pit_tables.FundamentalRow]:
    """Random point-in-time rows with unique period identities per group.

    Acceptance times come from a 21-value pool, so ties (several filings
    accepted at the same instant) occur in most examples. Versions are numbered
    by ``(acceptance_ts, accession)`` exactly as the dbt model numbers them.
    """
    rows: list[pit_tables.FundamentalRow] = []
    sequence = 0
    issuers = draw(st.lists(st.sampled_from(_CIKS), min_size=1, max_size=3, unique=True))
    for cik in issuers:
        for field in draw(st.lists(st.sampled_from(_FIELDS), min_size=1, max_size=2, unique=True)):
            period_type = draw(st.sampled_from(_PERIOD_TYPES))
            period_ends = draw(
                st.lists(
                    st.dates(min_value=date(2019, 1, 1), max_value=date(2024, 12, 31)),
                    min_size=1,
                    max_size=4,
                    unique=True,
                )
            )
            for period_end in period_ends:
                period_start = draw(st.one_of(st.none(), st.just(period_end - timedelta(days=365))))
                version_count = draw(st.integers(min_value=1, max_value=3))
                acceptances = draw(
                    st.lists(
                        st.sampled_from(_ACCEPTANCE_POOL),
                        min_size=version_count,
                        max_size=version_count,
                    )
                )
                accessions = []
                for _ in range(version_count):
                    sequence += 1
                    accessions.append(_accession(cik, sequence))
                order = sorted(range(version_count), key=lambda i: (acceptances[i], accessions[i]))
                version = {index: rank for rank, index in enumerate(order, start=1)}
                for index in range(version_count):
                    rows.append(
                        pit_tables.FundamentalRow(
                            cik=cik,
                            field=field,
                            value=float(
                                draw(st.integers(min_value=-1_000_000, max_value=1_000_000))
                            ),
                            period_start=period_start,
                            period_end=period_end,
                            accession=accessions[index],
                            acceptance_ts=acceptances[index],
                            period_type=period_type,
                            version=version[index],
                        )
                    )
    return rows


@st.composite
def _asof_case(
    draw: st.DrawFn,
) -> tuple[list[pit_tables.FundamentalRow], str, str, datetime, Literal["FY", "Q"], int]:
    rows = draw(_fundamental_rows())
    target = draw(st.sampled_from(rows))
    asof = draw(
        st.one_of(
            st.sampled_from(_ACCEPTANCE_POOL),
            st.sampled_from(_ACCEPTANCE_POOL).map(lambda ts: ts + timedelta(seconds=1)),
            st.datetimes(
                min_value=_ACCEPTANCE_POOL[0] - timedelta(days=1),
                max_value=_ACCEPTANCE_POOL[-1] + timedelta(days=1),
                timezones=st.just(UTC),
            ),
        )
    )
    period = draw(st.sampled_from(_QUERY_PERIODS))
    n = draw(st.integers(min_value=1, max_value=6))
    return rows, target.cik, target.field, asof, period, n


@settings(max_examples=100, deadline=None)
@given(case=_asof_case())
def test_property_no_row_after_asof(
    case: tuple[list[pit_tables.FundamentalRow], str, str, datetime, Literal["FY", "Q"], int],
) -> None:
    rows, cik, field, asof, period, n = case
    con = pit_tables.build_pit_db(rows)
    try:
        results = query.asof(cik, field, asof, period, n=n, db=con)
    finally:
        con.close()

    assert all(row.acceptance_ts <= asof for row in results), "a row accepted after asof leaked"
    assert len(results) <= n

    candidates = [
        row
        for row in rows
        if row.cik == cik
        and row.field == field
        and row.period_type == period
        and row.acceptance_ts <= asof
    ]
    winners: dict[tuple[date | None, date], pit_tables.FundamentalRow] = {}
    for row in candidates:
        key = (row.period_start, row.period_end)
        current = winners.get(key)
        if current is None or (row.acceptance_ts, row.version) > (
            current.acceptance_ts,
            current.version,
        ):
            winners[key] = row
    expected = sorted(winners.values(), key=lambda row: row.period_end, reverse=True)[:n]

    assert len(results) == len(expected)
    for result, oracle in zip(results, expected, strict=True):
        assert result.cik == cik
        assert result.field == field
        assert result.period_type == period
        assert result.acceptance_ts == oracle.acceptance_ts
        assert result.version == oracle.version
        assert result.accession == oracle.accession
        assert result.value == oracle.value
        assert result.period_end == oracle.period_end
        assert result.period_start == oracle.period_start

    period_ends = [row.period_end for row in results]
    assert period_ends == sorted(set(period_ends), reverse=True), "periods not strictly descending"
