"""Tests for the Streamlit explorer in :mod:`explorer.app`."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pit_tables
import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "explorer" / "app.py"

RY_CIK = "0001000275"
AAPL_CIK = "0000320193"

AAPL_ACCEPTED = datetime(2024, 11, 1, 16, 30, 6, tzinfo=UTC)
RY_ORIGINAL_ACCEPTED = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
RY_RESTATED_ACCEPTED = datetime(2024, 12, 4, 16, 30, 0, tzinfo=UTC)
FIRST_EVENT_AT = datetime(2025, 1, 30, 21, 30, 0, tzinfo=UTC)

FUNDAMENTALS = [
    pit_tables.FundamentalRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        field="revenue",
        value=391_035_000_000.0,
        period_start=date(2023, 10, 1),
        period_end=date(2024, 9, 28),
        accession="0000320193-24-000123",
        acceptance_ts=AAPL_ACCEPTED,
        source_concept="RevenueFromContractWithCustomerExcludingAssessedTax",
    ),
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
    ),
]

EVENTS = [
    pit_tables.EventRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        accession="0000320193-25-000008",
        acceptance_ts=FIRST_EVENT_AT,
        item_code="2.02",
        item_label="Results of Operations and Financial Condition",
    ),
    pit_tables.EventRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        accession="0000320193-25-000008",
        acceptance_ts=FIRST_EVENT_AT,
        item_code="9.01",
        item_label="Financial Statements and Exhibits",
    ),
]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "edgarpit.duckdb"
    con = pit_tables.build_pit_db(FUNDAMENTALS, EVENTS, path=path)
    con.close()
    return path


@pytest.fixture
def app_test(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setenv("EDGARPIT_DB", str(db_path))
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    return at


def versions_table(at: AppTest) -> pd.DataFrame:
    return next(
        element.value for element in at.dataframe if "known_at_asof" in element.value.columns
    )


def events_table(at: AppTest) -> pd.DataFrame:
    return next(element.value for element in at.dataframe if "item_code" in element.value.columns)


def test_app_renders_the_chart_and_the_events_table(app_test: AppTest) -> None:
    assert not app_test.exception

    charts = app_test.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].spec)
    assert spec["encoding"]["x"]["field"] == "period_end"
    assert spec["encoding"]["color"]["field"] == "status"
    assert spec["encoding"]["shape"]["field"] == "status"
    assert spec["encoding"]["color"]["title"] == "version status"
    tooltip_fields = {entry["field"] for entry in spec["encoding"]["tooltip"]}
    assert {"value", "unit", "accession", "accepted"} <= tooltip_fields

    versions = versions_table(app_test)
    assert versions["status"].tolist() == ["version 1"]

    events = events_table(app_test)
    assert events["item_code"].tolist() == ["2.02", "9.01"]
    assert events["item_label"].tolist()[0] == "Results of Operations and Financial Condition"
    assert events["filing"].tolist()[0] == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019325000008/0000320193-25-000008-index.htm"
    )


def test_asof_input_changes_which_versions_are_known(app_test: AppTest) -> None:
    issuer_box = app_test.sidebar.selectbox(key="issuer")
    ry_option = next(option for option in issuer_box.options if option.startswith("RY"))
    issuer_box.set_value(ry_option)
    app_test.run()
    assert not app_test.exception

    asof_input = app_test.date_input(key="asof")
    asof_input.set_value(date(2023, 1, 1))
    app_test.run()
    before = versions_table(app_test)
    assert before["known_at_asof"].tolist() == [True, False, False]
    assert before["status"].tolist() == ["version 1", "not yet accepted", "not yet accepted"]
    assert app_test.metric[0].value == "1 of 3"

    app_test.date_input(key="asof").set_value(date(2023, 12, 1))
    app_test.run()
    middle = versions_table(app_test)
    assert middle["known_at_asof"].tolist() == [True, True, False]
    assert middle["status"].tolist() == ["version 1", "version 1", "not yet accepted"]

    app_test.date_input(key="asof").set_value(date(2025, 1, 1))
    app_test.run()
    after = versions_table(app_test)
    assert after["known_at_asof"].tolist() == [True, True, True]
    assert after["status"].tolist() == ["version 1", "version 1", "restatement"]
    assert app_test.metric[0].value == "3 of 3"

    assert before["known_at_asof"].tolist() != after["known_at_asof"].tolist()


def test_missing_database_shows_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing.duckdb"
    monkeypatch.setenv("EDGARPIT_DB", str(missing))

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()

    assert not at.exception
    messages = [error.value for error in at.error]
    assert any(str(missing) in message for message in messages)
    assert not at.get("vega_lite_chart")
