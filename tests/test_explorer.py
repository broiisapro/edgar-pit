"""Tests for the Streamlit explorer in :mod:`explorer.app`."""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd
import pit_tables
import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "explorer" / "app.py"

BUILD_INFO_RANGE = {
    "min": "20261017T040215210934Z",
    "max": "20261017T040326728091Z",
}

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
        restatement_kind="revision",
    ),
    # Apple's FY2019 diluted EPS as reported, then re-reported for the 2020
    # 4-for-1 stock split (decision A-25): a version, not a restatement.
    pit_tables.FundamentalRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        field="eps_diluted",
        value=11.89,
        period_start=date(2018, 9, 30),
        period_end=date(2019, 9, 28),
        accession="0000320193-19-000119",
        acceptance_ts=datetime(2019, 10, 31, 20, 5, 0, tzinfo=UTC),
        unit="USD/shares",
        currency="USD",
        source_concept="EarningsPerShareDiluted",
        first_value=11.89,
    ),
    pit_tables.FundamentalRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        field="eps_diluted",
        value=2.97,
        period_start=date(2018, 9, 30),
        period_end=date(2019, 9, 28),
        accession="0000320193-20-000096",
        acceptance_ts=datetime(2020, 10, 30, 20, 5, 0, tzinfo=UTC),
        unit="USD/shares",
        currency="USD",
        source_concept="EarningsPerShareDiluted",
        version=2,
        first_value=11.89,
        is_restatement=True,
        restatement_kind="split_adjustment",
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

    app_test.sidebar.selectbox(key="field").set_value("revenue")
    app_test.date_input(key="asof").set_value(date(2025, 1, 1))
    app_test.run()
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


def test_split_adjustments_are_not_shown_as_restatements(app_test: AppTest) -> None:
    """A-25: only a revision is a restatement; a split gets its own status."""
    app_test.sidebar.selectbox(key="field").set_value("eps_diluted")
    app_test.run()
    assert not app_test.exception

    versions = versions_table(app_test)
    assert versions["status"].tolist() == ["version 1", "split adjustment"]
    assert versions["restatement_kind"].tolist() == ["none", "split_adjustment"]

    spec = json.loads(app_test.get("vega_lite_chart")[0].spec)
    domain = spec["encoding"]["color"]["scale"]["domain"]
    assert "restatement" in domain
    assert "split adjustment" in domain


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


@dataclass
class ReleaseServer:
    """A local HTTP server standing in for a GitHub release download.

    ``root`` holds the served files, ``base`` is the URL prefix and ``hits``
    counts requests per path so tests can prove the download happens once.
    """

    root: Path
    base: str
    hits: Counter[str]


@pytest.fixture(autouse=True)
def _isolate_release_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("EDGARPIT_DB_URL", "EDGARPIT_DB_SHA256"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def release_server(tmp_path: Path) -> ReleaseServer:
    root = tmp_path / "release"
    root.mkdir()
    hits: Counter[str] = Counter()

    class Handler(BaseHTTPRequestHandler):
        """Serve files from ``root``; ``huge.duckdb`` declares an oversized body."""

        def do_GET(self) -> None:
            name = self.path.split("?", 1)[0].lstrip("/")
            hits[name] += 1
            if name == "huge.duckdb":
                self.send_response(200)
                self.send_header("Content-Length", str(500 * 1024 * 1024 + 1))
                self.end_headers()
                return
            path = root / name
            if not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """Keep request logging out of the test output."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ReleaseServer(
            root=root,
            base=f"http://127.0.0.1:{server.server_address[1]}",
            hits=hits,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
def released_database(release_server: ReleaseServer, db_path: Path) -> ReleaseServer:
    (release_server.root / "edgarpit.duckdb").write_bytes(db_path.read_bytes())
    (release_server.root / "manifest.json").write_text(
        json.dumps(
            {
                "build_timestamp": "2026-10-20T12:00:00Z",
                "sec_fetch_range": BUILD_INFO_RANGE,
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    return release_server


def run_app() -> AppTest:
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    return at


def test_released_database_is_downloaded_rendered_and_cached(
    released_database: ReleaseServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EDGARPIT_DB", str(tmp_path / "missing.duckdb"))
    monkeypatch.setenv("EDGARPIT_DB_URL", f"{released_database.base}/edgarpit.duckdb")

    at = run_app()
    assert not at.exception
    captions = [caption.value for caption in at.caption]
    assert any(
        "SEC fetch range: 2026-10-17 04:02 UTC to 2026-10-17 04:03 UTC" in text
        and "2026-10-20 12:00 UTC" in text
        for text in captions
    )
    assert any(option.startswith("AAPL") for option in at.sidebar.selectbox(key="issuer").options)
    assert at.get("vega_lite_chart")

    at.sidebar.selectbox(key="field").set_value("revenue")
    at.run()
    assert not at.exception
    assert released_database.hits["edgarpit.duckdb"] == 1
    assert released_database.hits["manifest.json"] == 1


def test_local_build_info_supplies_the_sec_fetch_range(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (db_path.parent / "build-info.json").write_text(
        json.dumps({"package_version": "1.0.0", "sec_fetch_range": BUILD_INFO_RANGE}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EDGARPIT_DB", str(db_path))

    at = run_app()

    assert not at.exception
    captions = [caption.value for caption in at.caption]
    assert any(
        "SEC fetch range: 2026-10-17 04:02 UTC to 2026-10-17 04:03 UTC" in text for text in captions
    )
    assert at.get("vega_lite_chart")


def test_checksum_mismatch_shows_an_error_page(
    released_database: ReleaseServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EDGARPIT_DB", str(tmp_path / "missing.duckdb"))
    monkeypatch.setenv("EDGARPIT_DB_URL", f"{released_database.base}/edgarpit.duckdb")
    monkeypatch.setenv("EDGARPIT_DB_SHA256", "0" * 64)

    at = run_app()

    assert not at.exception
    errors = [error.value for error in at.error]
    assert any("EDGARPIT_DB_SHA256" in message for message in errors)
    assert not at.get("vega_lite_chart")


def test_non_http_database_url_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGARPIT_DB", str(tmp_path / "missing.duckdb"))
    monkeypatch.setenv("EDGARPIT_DB_URL", f"file://{tmp_path}/edgarpit.duckdb")

    at = run_app()

    assert not at.exception
    errors = [error.value for error in at.error]
    assert any("http(s) URL" in message for message in errors)
    assert not at.get("vega_lite_chart")


def test_malformed_checksum_is_rejected_before_downloading(
    released_database: ReleaseServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EDGARPIT_DB", str(tmp_path / "missing.duckdb"))
    monkeypatch.setenv("EDGARPIT_DB_URL", f"{released_database.base}/edgarpit.duckdb")
    monkeypatch.setenv("EDGARPIT_DB_SHA256", "not-a-digest")

    at = run_app()

    assert not at.exception
    errors = [error.value for error in at.error]
    assert any("64-character hexadecimal" in message for message in errors)
    assert released_database.hits["edgarpit.duckdb"] == 0


def test_oversized_download_is_rejected(
    release_server: ReleaseServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EDGARPIT_DB", str(tmp_path / "missing.duckdb"))
    monkeypatch.setenv("EDGARPIT_DB_URL", f"{release_server.base}/huge.duckdb")

    at = run_app()

    assert not at.exception
    errors = [error.value for error in at.error]
    assert any("500 MB" in message for message in errors)
    assert not at.get("vega_lite_chart")


def test_a_body_that_is_not_a_database_shows_an_error_page(
    released_database: ReleaseServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (released_database.root / "broken.duckdb").write_text("<html>not a database</html>")
    monkeypatch.setenv("EDGARPIT_DB", str(tmp_path / "missing.duckdb"))
    monkeypatch.setenv("EDGARPIT_DB_URL", f"{released_database.base}/broken.duckdb")

    at = run_app()

    assert not at.exception
    errors = [error.value for error in at.error]
    assert any("not a readable DuckDB database" in message for message in errors)
    assert not at.get("vega_lite_chart")
