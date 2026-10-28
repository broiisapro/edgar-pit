"""``main.fundamentals_pit_usd`` against hand-computed values (decisions A-32, A-38).

The module builds one development database from the staging CSVs plus five
hand-written facts rows that put a CAD value on a Friday period end, a CAD value
on a Sunday period end, a CAD value before the Bank of Canada series starts, a
EUR value, and a CAD value whose rate is published after the filing was
accepted. Every expected number below is computed by hand from
``tests/fixtures/staging/fx_daily.csv``, so the tests pin the conversion rule,
not a snapshot of it.

The last test is a Hypothesis property over generated observations and period
ends: the rate a row records is always the latest observation on or before its
period end, and a period end before every observation is never converted.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest
from dbt.cli.main import dbtRunner, dbtRunnerResult
from hypothesis import given, settings
from hypothesis import strategies as st

from edgarpit.devdb import create_dev_db

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSFORM_DIR = REPO_ROOT / "transform"
STAGING_DIR = Path(__file__).parent / "fixtures" / "staging"

RY_CIK = "0001000275"
AAPL_CIK = "0000320193"

TORONTO = ZoneInfo("America/Toronto")

# The observation dates and values this module asserts against
# (tests/fixtures/staging/fx_daily.csv, taken from the Bank of Canada series).
FRIDAY_RATE = 1.3683  # 2023-11-03, a Friday
FRIDAY_PERIOD_END = date(2023, 11, 3)
SUNDAY_PERIOD_END = date(2023, 12, 31)
FRIDAY_BEFORE_SUNDAY = date(2023, 12, 29)
FRIDAY_BEFORE_SUNDAY_RATE = 1.3226
EARLY_PERIOD_END = date(2016, 12, 30)  # before the first observation (2017-01-03)
LATE_RATE_DATE = date(2024, 1, 31)
LATE_RATE = 1.3397

# A CIK with no submissions row, used by the property test so its generated
# facts cannot collide with the staging fixtures.
SYNTHETIC_CIK = "0000000001"


def fx_known_at(rate_date: date) -> datetime:
    """Return 17:00 ``America/Toronto`` on ``rate_date`` as a UTC instant."""
    return datetime.combine(rate_date, time(17, 0), tzinfo=TORONTO).astimezone(UTC)


def write_fx_case_staging(directory: Path) -> None:
    """Copy the staging CSVs into ``directory`` and append the FX-case facts."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in (
        "filings.csv",
        "events_8k.csv",
        "facts_raw.csv",
        "filings_text.csv",
        "fx_daily.csv",
    ):
        shutil.copy(STAGING_DIR / name, directory / name)
    rows = [
        # A CAD value whose period ends on a Friday, with a Friday observation.
        "0001000275,ifrs-full,Revenue,CAD,1000000000.0,2022-11-04,2023-11-03,,,40-F,"
        "0001193125-23-285639,2023-11-30,",
        # A CAD value whose period ends on the Sunday; the latest observation is
        # the Friday before it.
        "0001000275,ifrs-full,Revenue,CAD,1322600000.0,2023-01-01,2023-12-31,,,40-F,"
        "0001193125-24-270294,2024-12-04,",
        # A CAD value before the first observation: never converted.
        "0001000275,ifrs-full,Revenue,CAD,900000000.0,2016-01-01,2016-12-30,,,40-F,"
        "0001193125-22-294864,2022-11-30,",
        # A EUR value: out of scope, never converted.
        "0000320193,us-gaap,Revenues,EUR,1000000000.0,2020-01-01,2020-12-31,,,10-K,"
        "0000320193-25-000079,2025-10-31,",
        # A rate published after the filing's acceptance time: the converted row
        # is known at the rate, not at the filing. Impossible in real data (a
        # filing reports past periods) but the model must still be safe.
        "0001000275,ifrs-full,Revenue,CAD,1350000000.0,2024-01-01,2024-01-31,,,40-F,"
        "0001193125-23-285639,2023-11-30,",
    ]
    with (directory / "facts_raw.csv").open("a", encoding="utf-8") as handle:
        handle.write("\n".join(rows) + "\n")


def run_dbt(db_path: Path, target_path: Path, *args: str) -> dbtRunnerResult:
    """Run ``dbt build`` against ``db_path`` with the ``fixtures`` target."""
    previous = os.environ.get("EDGARPIT_DB")
    os.environ["EDGARPIT_DB"] = str(db_path)
    try:
        runner = dbtRunner()
        return runner.invoke(
            [
                "build",
                "--project-dir",
                str(TRANSFORM_DIR),
                "--profiles-dir",
                str(TRANSFORM_DIR),
                "--target",
                "fixtures",
                "--target-path",
                str(target_path),
                *args,
            ]
        )
    finally:
        if previous is None:
            os.environ.pop("EDGARPIT_DB", None)
        else:
            os.environ["EDGARPIT_DB"] = previous


@pytest.fixture(scope="module")
def staging_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a dev database from the staging fixtures plus the FX-case rows."""
    staging = tmp_path_factory.mktemp("usd-staging")
    write_fx_case_staging(staging)
    out_dir = tmp_path_factory.mktemp("usd-db")
    db_path = create_dev_db(out_dir, staging_dir=staging)
    result = run_dbt(db_path, tmp_path_factory.mktemp("usd-target"))
    assert result.success, result.exception
    return db_path


@pytest.fixture(scope="module")
def property_build(staging_build: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A private copy of the built database, so the property test can mutate it."""
    path = tmp_path_factory.mktemp("usd-property") / "edgarpit.duckdb"
    shutil.copy(staging_build, path)
    yield path


def query(db_path: Path, sql: str, params: list[object] | None = None) -> list[tuple[object, ...]]:
    """Run one UTC-session query and return all rows."""
    con = duckdb.connect(str(db_path))
    try:
        con.execute("SET TimeZone='UTC'")
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def revenue_row(
    db_path: Path, cik: str, period_end: date, accession: str | None = None
) -> dict[str, object]:
    """Return the USD model row of a revenue period, keyed by column name.

    ``accession`` narrows the lookup when a period has more than one version.
    """
    rows = query(
        db_path,
        """
        select period_end, acceptance_ts, value, value_usd, fx_rate, fx_rate_date,
               fx_known_at, converted, asof_ts, unconverted_reason
        from main.fundamentals_pit_usd
        where cik = ? and field = 'revenue' and period_end = ?
          and (? is null or accession = ?)
        """,
        [cik, period_end, accession, accession],
    )
    assert len(rows) == 1, rows
    keys = (
        "period_end",
        "acceptance_ts",
        "value",
        "value_usd",
        "fx_rate",
        "fx_rate_date",
        "fx_known_at",
        "converted",
        "asof_ts",
        "unconverted_reason",
    )
    return dict(zip(keys, rows[0], strict=True))


def test_cad_value_on_a_friday_period_end_uses_that_fridays_rate(staging_build: Path) -> None:
    row = revenue_row(staging_build, RY_CIK, FRIDAY_PERIOD_END)

    assert row["value"] == 1_000_000_000.0
    assert row["fx_rate"] == FRIDAY_RATE
    assert row["fx_rate_date"] == FRIDAY_PERIOD_END
    assert row["fx_known_at"] == fx_known_at(FRIDAY_PERIOD_END)
    assert row["value_usd"] == pytest.approx(1_000_000_000.0 / FRIDAY_RATE)
    assert row["converted"] is True
    assert row["unconverted_reason"] is None
    # The filing was accepted after the rate was published, so the filing is
    # what makes the value known.
    assert row["asof_ts"] == row["acceptance_ts"]


def test_cad_value_on_a_sunday_period_end_uses_the_friday_rate(staging_build: Path) -> None:
    row = revenue_row(staging_build, RY_CIK, SUNDAY_PERIOD_END)

    assert row["fx_rate"] == FRIDAY_BEFORE_SUNDAY_RATE
    assert row["fx_rate_date"] == FRIDAY_BEFORE_SUNDAY
    assert row["fx_known_at"] == fx_known_at(FRIDAY_BEFORE_SUNDAY)
    # 1,322,600,000 CAD / 1.3226 CAD per USD is exactly one billion USD.
    assert row["value_usd"] == pytest.approx(1_000_000_000.0)
    assert row["converted"] is True
    assert row["asof_ts"] == row["acceptance_ts"]


def test_cad_value_before_the_first_observation_is_not_converted(staging_build: Path) -> None:
    row = revenue_row(staging_build, RY_CIK, EARLY_PERIOD_END)

    assert row["value"] == 900_000_000.0
    assert row["value_usd"] is None
    assert row["fx_rate"] is None
    assert row["fx_rate_date"] is None
    assert row["fx_known_at"] is None
    assert row["converted"] is False
    assert row["unconverted_reason"] == "no_rate"
    # An unconverted row is known at its filing, never later.
    assert row["asof_ts"] == row["acceptance_ts"]


def test_a_currency_out_of_scope_is_not_converted(staging_build: Path) -> None:
    row = revenue_row(staging_build, AAPL_CIK, date(2020, 12, 31))

    assert row["value"] == 1_000_000_000.0
    assert row["value_usd"] is None
    assert row["fx_rate"] is None
    assert row["converted"] is False
    assert row["unconverted_reason"] == "currency_out_of_scope"
    assert row["asof_ts"] == row["acceptance_ts"]


def test_a_usd_value_is_converted_at_one(staging_build: Path) -> None:
    row = revenue_row(staging_build, AAPL_CIK, date(2023, 9, 30))

    assert row["value"] == 383_285_000_000.0
    assert row["value_usd"] == row["value"]
    assert row["fx_rate"] == 1.0
    assert row["fx_rate_date"] is None
    assert row["fx_known_at"] is None
    assert row["converted"] is True
    assert row["unconverted_reason"] is None
    assert row["asof_ts"] == row["acceptance_ts"]


def test_a_converted_row_waits_for_a_rate_published_after_the_filing(staging_build: Path) -> None:
    """``asof_ts = greatest(acceptance_ts, fx_known_at)`` (decision A-32).

    The synthetic fact is accepted on 2023-11-30 but its period ends on
    2024-01-31, so the rate it needs is published a month after the filing. A
    query at the acceptance time must not see the converted value.
    """
    row = revenue_row(staging_build, RY_CIK, LATE_RATE_DATE, "0001193125-23-285639")

    assert row["acceptance_ts"] == datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
    assert row["fx_rate"] == LATE_RATE
    assert row["fx_rate_date"] == LATE_RATE_DATE
    assert row["fx_known_at"] == fx_known_at(LATE_RATE_DATE)
    assert row["value_usd"] == pytest.approx(1_350_000_000.0 / LATE_RATE)
    assert row["asof_ts"] == fx_known_at(LATE_RATE_DATE)
    assert row["asof_ts"] > row["acceptance_ts"]


def test_no_converted_row_uses_a_rate_after_its_period_end(staging_build: Path) -> None:
    rows = query(
        staging_build,
        """
        select count(*)
        from main.fundamentals_pit_usd
        where fx_rate_date > period_end
        """,
    )
    assert rows == [(0,)]


def test_the_row_key_and_phase1_columns_are_preserved(staging_build: Path) -> None:
    """The model is a projection of fundamentals_pit plus the conversion columns."""
    pit = query(
        staging_build,
        "select cik, field, period_start, period_end, accession, value, currency, version "
        "from main.fundamentals_pit order by all",
    )
    usd = query(
        staging_build,
        "select cik, field, period_start, period_end, accession, value, currency, version "
        "from main.fundamentals_pit_usd order by all",
    )
    assert usd == pit

    columns = [
        row[0]
        for row in query(
            staging_build,
            "select column_name from information_schema.columns "
            "where table_schema = 'main' and table_name = 'fundamentals_pit_usd' "
            "order by ordinal_position",
        )
    ]
    assert columns[-7:] == [
        "value_usd",
        "fx_rate",
        "fx_rate_date",
        "fx_known_at",
        "converted",
        "asof_ts",
        "unconverted_reason",
    ]


def test_a_future_fx_observation_changes_nothing(
    staging_build: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Decision A-32: a rate dated after every period end is never selected.

    The build runs with ``inject_future_fx``, which adds a 2099-12-31
    observation; the dbt test ``assert_fx_observation_not_after_period_end``
    must still pass and every converted row must keep the same rate, value and
    ``asof_ts`` as the uninterrupted build.
    """
    injected_db = tmp_path_factory.mktemp("usd-injected") / "edgarpit.duckdb"
    shutil.copy(staging_build, injected_db)
    result = run_dbt(
        injected_db,
        tmp_path_factory.mktemp("usd-injected-target"),
        "--vars",
        "{inject_future_fx: true}",
    )
    assert result.success, result.exception

    columns = (
        "cik, field, period_start, period_end, accession, value_usd, fx_rate, "
        "fx_rate_date, converted, asof_ts"
    )
    assert query(injected_db, f"select {columns} from main.fundamentals_pit_usd order by all") == (
        query(staging_build, f"select {columns} from main.fundamentals_pit_usd order by all")
    )
    assert query(
        injected_db, "select count(*) from main.fundamentals_pit_usd where fx_rate = 999.0"
    ) == [(0,)]


RATE_DATES = st.dates(min_value=date(2017, 1, 3), max_value=date(2027, 1, 1))
PERIOD_ENDS = st.dates(min_value=date(2016, 12, 1), max_value=date(2027, 6, 30))


@settings(max_examples=40, deadline=None)
@given(
    observations=st.lists(
        st.tuples(RATE_DATES, st.floats(min_value=0.5, max_value=5.0)),
        unique_by=lambda observation: observation[0],
        min_size=0,
        max_size=8,
    ),
    period_ends=st.lists(PERIOD_ENDS, min_size=1, max_size=4, unique=True),
)
def test_the_chosen_rate_is_the_latest_on_or_before_the_period_end(
    property_build: Path,
    observations: list[tuple[date, float]],
    period_ends: list[date],
) -> None:
    """The model records the latest observation on or before the period end.

    Each example writes its own observations and facts rows inside a
    transaction that is rolled back, so the view is evaluated against exactly
    the generated data and the shared database is left untouched.
    """
    con = duckdb.connect(str(property_build))
    try:
        con.execute("SET TimeZone='UTC'")
        con.execute("BEGIN")
        try:
            con.execute("DELETE FROM raw.fx_daily")
            con.execute("DELETE FROM raw.facts_raw WHERE cik = ?", [SYNTHETIC_CIK])
            if observations:
                con.executemany(
                    "INSERT INTO raw.fx_daily VALUES ('FXUSDCAD', ?, ?, ?, 'valet')",
                    [
                        (rate_date, value, fx_known_at(rate_date))
                        for rate_date, value in observations
                    ],
                )
            con.executemany(
                "INSERT INTO raw.facts_raw VALUES (?, 'ifrs-full', 'Revenue', 'CAD', 1000000000.0,"
                " ?, ?, NULL, NULL, '40-F', '0001193125-23-285639', ?, NULL)",
                [
                    (
                        SYNTHETIC_CIK,
                        period_end - timedelta(days=364),
                        period_end,
                        period_end + timedelta(days=30),
                    )
                    for period_end in period_ends
                ],
            )
            rows = con.execute(
                """
                select period_end, fx_rate_date, fx_rate, converted, unconverted_reason
                from main.fundamentals_pit_usd
                where cik = ?
                order by period_end
                """,
                [SYNTHETIC_CIK],
            ).fetchall()
            assert len(rows) == len(period_ends)
            for period_end, rate_date, rate, converted, reason in rows:
                eligible = [day for day, _ in observations if day <= period_end]
                if eligible:
                    chosen = max(eligible)
                    assert converted is True, (period_end, observations)
                    assert rate_date == chosen
                    assert rate == next(value for day, value in observations if day == chosen)
                    assert reason is None
                else:
                    assert converted is False, (period_end, observations)
                    assert rate_date is None
                    assert rate is None
                    assert reason == "no_rate"
        finally:
            con.execute("ROLLBACK")
    finally:
        con.close()
