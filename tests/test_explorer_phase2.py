"""The explorer's Phase 2 controls: USD values and 8-K text (issue #113).

The page must show the "Reported / USD" switch and the 8-K text section only
for a database that has ``main.fundamentals_pit_usd`` and ``main.filings_text``,
so a 1.0.0 release renders exactly as before. Rendering is driven headlessly with
``streamlit.testing.v1.AppTest``; the frame and availability helpers are also
tested directly.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import doubles
import duckdb
import pit_tables
import polars as pl
import pytest
from streamlit.testing.v1 import AppTest

from edgarpit import query
from edgarpit.schemas import duckdb_ddl
from explorer import app as explorer

APP_PATH = Path(__file__).resolve().parents[1] / "explorer" / "app.py"

RY_CIK = "0001000275"
AAPL_CIK = "0000320193"
SHOP_CIK = "0001594805"

RY_ORIGINAL_ACCEPTED = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
RY_RESTATED_ACCEPTED = datetime(2024, 12, 4, 16, 30, 0, tzinfo=UTC)
AAPL_ACCEPTED = datetime(2024, 11, 1, 16, 30, 6, tzinfo=UTC)
SHOP_ACCEPTED = datetime(2023, 10, 31, 15, 0, 0, tzinfo=UTC)
AAPL_8K_ACCEPTED = datetime(2025, 10, 30, 20, 5, 0, tzinfo=UTC)
SHOP_8K_ACCEPTED = datetime(2025, 11, 6, 21, 10, 0, tzinfo=UTC)

FX_RATE = 1.3659
OCT_31_RATE_KNOWN_AT = doubles.fx_known_at(date(2023, 10, 31))

AAPL_8K = "0000320193-25-000080"
SHOP_8K = "0001594805-25-000010"

FUNDAMENTALS = [
    pit_tables.FundamentalRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        field="revenue",
        value=391_035_000_000.0,
        period_end=date(2024, 9, 28),
        accession="0000320193-24-000123",
        acceptance_ts=AAPL_ACCEPTED,
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
    # A filing whose acceptance time precedes the rate for its period end, the
    # state an early or imputed acceptance time produces (decision A-1/A-23):
    # the reported value is known, the USD value is not yet.
    pit_tables.FundamentalRow(
        cik=RY_CIK,
        ticker="RY",
        field="total_assets",
        value=2_000_000_000_000.0,
        period_start=None,
        period_end=date(2023, 12, 31),
        accession="0001193125-24-010101",
        acceptance_ts=datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
        unit="CAD",
        currency="CAD",
        form="40-F",
        source_taxonomy="ifrs-full",
        source_concept="Assets",
    ),
    pit_tables.FundamentalRow(
        cik=SHOP_CIK,
        ticker="SHOP",
        field="revenue",
        value=7_000_000_000.0,
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

EVENTS = [
    pit_tables.EventRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        accession=AAPL_8K,
        acceptance_ts=AAPL_8K_ACCEPTED,
        item_code="2.02",
        item_label="Results of Operations and Financial Condition",
    ),
    pit_tables.EventRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        accession=AAPL_8K,
        acceptance_ts=AAPL_8K_ACCEPTED,
        item_code="9.01",
        item_label="Financial Statements and Exhibits",
    ),
    pit_tables.EventRow(
        cik=SHOP_CIK,
        ticker="SHOP",
        accession=SHOP_8K,
        acceptance_ts=SHOP_8K_ACCEPTED,
        item_code="9.01",
        item_label="Financial Statements and Exhibits",
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
        restatement_kind="revision",
        fx_rate=FX_RATE,
        fx_rate_date=date(2023, 10, 31),
        fx_known_at=OCT_31_RATE_KNOWN_AT,
    ),
    doubles.FundamentalUsdRow(
        cik=AAPL_CIK,
        field="revenue",
        value=391_035_000_000.0,
        period_end=date(2024, 9, 28),
        accession="0000320193-24-000123",
        acceptance_ts=AAPL_ACCEPTED,
        unit="USD",
        currency="USD",
        source_taxonomy="us-gaap",
        source_concept="Revenues",
    ),
    doubles.FundamentalUsdRow(
        cik=RY_CIK,
        field="total_assets",
        value=2_000_000_000_000.0,
        period_end=date(2023, 12, 31),
        accession="0001193125-24-010101",
        acceptance_ts=datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC),
        source_concept="Assets",
        fx_rate=1.3200,
        fx_rate_date=date(2023, 12, 29),
        fx_known_at=datetime(2023, 12, 29, 22, 0, tzinfo=UTC),
    ),
    doubles.FundamentalUsdRow(
        cik=SHOP_CIK,
        field="revenue",
        value=7_000_000_000.0,
        period_end=date(2023, 10, 31),
        accession="0001594805-23-000042",
        acceptance_ts=SHOP_ACCEPTED,
        form="6-K",
        fx_rate=FX_RATE,
        fx_rate_date=date(2023, 10, 31),
        fx_known_at=OCT_31_RATE_KNOWN_AT,
    ),
]

TEXT_ROWS = [
    doubles.FilingsTextRow(
        accession=AAPL_8K,
        document="aapl-20251030.htm",
        section="2.02",
        section_order=1,
        acceptance_ts=AAPL_8K_ACCEPTED,
        text="Item 2.02 Results of Operations and Financial Condition\n\n"
        "The Company reported quarterly revenue of $102.5 billion.",
    ),
    doubles.FilingsTextRow(
        accession=AAPL_8K,
        document="aapl-20251030.htm",
        section="9.01",
        section_order=2,
        acceptance_ts=AAPL_8K_ACCEPTED,
        text="Item 9.01 Financial Statements and Exhibits\n\n(d) Exhibits\n99.1 Press release.",
    ),
    doubles.FilingsTextRow(
        accession=AAPL_8K,
        document="a8kexhibit991.htm",
        doc_type="EX-99.1",
        section="unsegmented",
        acceptance_ts=AAPL_8K_ACCEPTED,
        text="Apple reports fourth quarter results.",
    ),
    doubles.FilingsTextRow(
        accession=SHOP_8K,
        cik=SHOP_CIK,
        document="shop-20251106.htm",
        section="unsegmented",
        acceptance_ts=SHOP_8K_ACCEPTED,
        text="Item 9.01 Financial Statements and Exhibits\n\n(d) Exhibits\n99.1 Press release.",
    ),
]


def _insert(con: duckdb.DuckDBPyConnection, table: str, frame: pl.DataFrame) -> None:
    con.register("_explorer_frame", frame)
    try:
        con.execute(f"INSERT INTO {table} SELECT * FROM _explorer_frame")
    finally:
        con.unregister("_explorer_frame")


def build_db(*, path: Path) -> None:
    """Write a database with the v1 tables and both Phase 2 read models."""
    con = pit_tables.build_pit_db(FUNDAMENTALS, EVENTS, path=path)
    try:
        con.execute(doubles.FUNDAMENTALS_PIT_USD_DDL)
        _insert(con, "main.fundamentals_pit_usd", doubles.fundamentals_pit_usd_frame(USD_ROWS))
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
    finally:
        con.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "edgarpit.duckdb"
    build_db(path=path)
    return path


@pytest.fixture
def v1_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "v1.duckdb"
    con = pit_tables.build_pit_db(FUNDAMENTALS, EVENTS, path=path)
    con.close()
    return path


@pytest.fixture
def app_test(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AppTest]:
    monkeypatch.setenv("EDGARPIT_DB", str(db_path))
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    yield at


def versions_table(at: AppTest) -> pl.DataFrame:
    return next(
        element.value for element in at.dataframe if "known_at_asof" in element.value.columns
    )


def select_issuer(at: AppTest, prefix: str) -> None:
    issuer_box = at.sidebar.selectbox(key="issuer")
    issuer_box.set_value(next(option for option in issuer_box.options if option.startswith(prefix)))
    at.run()


def test_table_exists_reads_the_phase2_models(db_path: Path, v1_db_path: Path) -> None:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        assert explorer.table_exists(con, "main.fundamentals_pit_usd") is True
        assert explorer.table_exists(con, "main.filings_text") is True
        assert explorer.table_exists(con, "main.no_such_table") is False
    finally:
        con.close()

    assert explorer.table_exists(db_path, "main.fundamentals_pit_usd") is True
    assert explorer.table_exists(v1_db_path, "main.fundamentals_pit_usd") is False
    assert explorer.table_exists(v1_db_path, "main.filings_text") is False


def test_usd_known_at_is_never_earlier_than_the_rate_publication() -> None:
    period_end = date(2023, 10, 31)

    assert explorer.usd_known_at(
        datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC), period_end
    ) == datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
    assert explorer.usd_known_at(SHOP_ACCEPTED, period_end) == OCT_31_RATE_KNOWN_AT


def test_versions_frame_in_usd_waits_for_the_rate(db_path: Path) -> None:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = query.history(SHOP_CIK, "revenue", db=con, currency="USD")
        before = explorer.versions_frame(rows, datetime(2023, 10, 31, tzinfo=UTC), currency="USD")
        after = explorer.versions_frame(rows, datetime(2023, 11, 1, tzinfo=UTC), currency="USD")
        reported = explorer.versions_frame(rows, datetime(2023, 10, 31, 16, 0, tzinfo=UTC))
    finally:
        con.close()

    assert before["known_at_asof"].to_list() == [False]
    assert before["status"].to_list() == ["not yet accepted"]
    assert after["known_at_asof"].to_list() == [True]
    assert after["status"].to_list() == ["version 1"]
    # Reported knowledge follows the acceptance time, one hour after 15:00 UTC;
    # the USD value still waits for the 21:00 UTC publication.
    assert reported["known_at_asof"].to_list() == [True]
    usd_same_instant = explorer.versions_frame(
        rows, datetime(2023, 10, 31, 16, 0, tzinfo=UTC), currency="USD"
    )
    assert usd_same_instant["known_at_asof"].to_list() == [False]


def _text_row(
    accession: str,
    document: str,
    section: str,
    order: int,
    accepted: datetime,
    *,
    cik: str = AAPL_CIK,
    doc_type: str = "primary",
) -> query.TextResult:
    text = f"{document} {section}"
    return query.TextResult(
        cik=cik,
        ticker="AAPL" if cik == AAPL_CIK else "SHOP",
        accession=accession,
        acceptance_ts=accepted,
        ts_imputed=False,
        document=document,
        doc_type=doc_type,
        section=section,
        section_order=order,
        text=text,
        sha256=doubles.sha256_of(text),
        char_count=len(text),
    )


def test_text_filings_groups_sections_by_accession() -> None:
    rows = [
        _text_row(
            AAPL_8K, "a8kexhibit991.htm", "unsegmented", 1, AAPL_8K_ACCEPTED, doc_type="EX-99.1"
        ),
        _text_row(AAPL_8K, "aapl-20251030.htm", "2.02", 1, AAPL_8K_ACCEPTED),
        _text_row(AAPL_8K, "aapl-20251030.htm", "9.01", 2, AAPL_8K_ACCEPTED),
        _text_row(SHOP_8K, "shop-20251106.htm", "unsegmented", 1, SHOP_8K_ACCEPTED, cik=SHOP_CIK),
    ]

    filings = explorer.text_filings(rows)

    assert [(filing.accession, filing.acceptance_ts) for filing in filings] == [
        (AAPL_8K, AAPL_8K_ACCEPTED),
        (SHOP_8K, SHOP_8K_ACCEPTED),
    ]
    assert [(section.document, section.section_order) for section in filings[0].sections] == [
        ("a8kexhibit991.htm", 1),
        ("aapl-20251030.htm", 1),
        ("aapl-20251030.htm", 2),
    ]
    assert explorer.text_filings([]) == []


def test_text_overview_frame_has_a_link_and_counts() -> None:
    rows = [
        _text_row(AAPL_8K, "aapl-20251030.htm", "2.02", 1, AAPL_8K_ACCEPTED),
        _text_row(AAPL_8K, "aapl-20251030.htm", "9.01", 2, AAPL_8K_ACCEPTED),
        _text_row(
            AAPL_8K, "a8kexhibit991.htm", "unsegmented", 1, AAPL_8K_ACCEPTED, doc_type="EX-99.1"
        ),
    ]

    frame = explorer.text_overview_frame(explorer.text_filings(rows))

    assert frame.columns == ["accepted", "accession", "documents", "sections", "filing"]
    assert frame["documents"].to_list() == ["aapl-20251030.htm, a8kexhibit991.htm"]
    assert frame["sections"].to_list() == [3]
    assert frame["accepted"].to_list() == ["2025-10-30T20:05:00Z"]
    assert frame["filing"].to_list() == [
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019325000080/0000320193-25-000080-index.htm"
    ]


def test_values_toggle_switches_the_table_between_reported_and_usd(app_test: AppTest) -> None:
    assert not app_test.exception

    select_issuer(app_test, "RY")
    app_test.sidebar.selectbox(key="field").set_value("revenue")
    app_test.date_input(key="asof").set_value(date(2025, 1, 1))
    app_test.run()
    reported = versions_table(app_test)
    assert reported["unit"].tolist() == ["CAD", "CAD"]
    assert reported["value"].tolist() == [56_129_000_000.0, 51_464_000_000.0]

    values = app_test.sidebar.radio(key="values")
    assert values.options == ["Reported", "USD"]
    values.set_value("USD")
    app_test.run()

    assert not app_test.exception
    usd = versions_table(app_test)
    assert usd["unit"].tolist() == ["USD", "USD"]
    assert usd["value"].tolist() == pytest.approx(
        [56_129_000_000.0 / FX_RATE, 51_464_000_000.0 / FX_RATE]
    )
    assert usd["status"].tolist() == ["version 1", "restatement"]
    spec = json.loads(app_test.get("vega_lite_chart")[0].spec)
    assert spec["encoding"]["y"]["title"] == "value (USD)"


def test_usd_toggle_keeps_a_value_unknown_before_its_rate(app_test: AppTest) -> None:
    """Classic bug (decision A-32): a CAD value needs the rate that converts it."""
    select_issuer(app_test, "RY")
    app_test.sidebar.selectbox(key="field").set_value("total_assets")
    app_test.date_input(key="asof").set_value(date(2023, 12, 15))
    app_test.run()

    reported = versions_table(app_test)
    assert reported["known_at_asof"].tolist() == [True]
    assert app_test.metric[0].value == "1 of 1"

    app_test.sidebar.radio(key="values").set_value("USD")
    app_test.run()

    assert not app_test.exception
    before = versions_table(app_test)
    assert before["known_at_asof"].tolist() == [False]
    assert before["status"].tolist() == ["not yet accepted"]
    assert before["unit"].tolist() == ["USD"]
    assert app_test.metric[0].value == "0 of 1"

    app_test.date_input(key="asof").set_value(date(2024, 1, 1))
    app_test.run()
    after = versions_table(app_test)
    assert after["known_at_asof"].tolist() == [True]
    assert app_test.metric[0].value == "1 of 1"


def test_text_section_lists_item_202_filings_and_each_section(app_test: AppTest) -> None:
    assert not app_test.exception
    assert "8-K text" in [subheader.value for subheader in app_test.subheader]

    texts = [element.value for element in app_test.text]
    assert any(text.startswith("Item 2.02 Results of Operations") for text in texts)
    assert any(text.startswith("Item 9.01 Financial Statements") for text in texts)
    assert any(text == "Apple reports fourth quarter results." for text in texts)

    labels = [element.label for element in app_test.get("expander")]
    assert any(label.startswith("aapl-20251030.htm · section 2.02") for label in labels)
    assert any(label.startswith("a8kexhibit991.htm · section unsegmented") for label in labels)

    markdown = " ".join(element.value for element in app_test.markdown)
    assert AAPL_8K in markdown
    assert "0000320193-25-000080-index.htm" in markdown


def test_text_section_only_shows_filings_with_item_202(app_test: AppTest) -> None:
    select_issuer(app_test, "SHOP")

    assert not app_test.exception
    assert any("no Item 2.02 filing text" in message.value for message in app_test.info)
    texts = [element.value for element in app_test.text]
    assert not any(text.startswith("Item 9.01 Financial Statements") for text in texts)


def test_a_v1_database_keeps_the_original_page(
    v1_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EDGARPIT_DB", str(v1_db_path))
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()

    assert not at.exception
    assert all(radio.label != "Values" for radio in at.sidebar.radio)
    assert "8-K text" not in [subheader.value for subheader in at.subheader]
    select_issuer(at, "RY")
    at.sidebar.selectbox(key="field").set_value("revenue")
    at.run()
    assert versions_table(at)["unit"].tolist() == ["CAD", "CAD"]
    assert at.get("vega_lite_chart")
