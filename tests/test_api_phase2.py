"""REST surface for USD values and 8-K text (issue #113).

The database is ``tests/pit_tables.py`` plus a ``main.fundamentals_pit_usd``
from ``tests/doubles.py`` and a ``main.filings_text`` view of ``raw.filings_text``
(decision A-38), so the endpoints are exercised against the shapes dbt produces
without running dbt.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import doubles
import duckdb
import pit_tables
import polars as pl
import pytest
from fastapi.testclient import TestClient

from api.main import app
from edgarpit import query
from edgarpit.schemas import duckdb_ddl

RY_CIK = "0001000275"
AAPL_CIK = "0000320193"
SHOP_CIK = "0001594805"

RY_ORIGINAL_ACCEPTED = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
RY_RESTATED_ACCEPTED = datetime(2024, 12, 4, 16, 30, 0, tzinfo=UTC)
AAPL_ACCEPTED = datetime(2025, 10, 30, 20, 5, 0, tzinfo=UTC)
SHOP_ACCEPTED = datetime(2023, 10, 31, 15, 0, 0, tzinfo=UTC)

FX_RATE = 1.3659
OCT_31_RATE_KNOWN_AT = doubles.fx_known_at(date(2023, 10, 31))

AAPL_8K = "0000320193-25-000080"

FUNDAMENTALS = [
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
        value=102_500_000_000.0,
        period_end=date(2025, 9, 27),
        accession="0000320193-25-000070",
        acceptance_ts=AAPL_ACCEPTED,
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
        acceptance_ts=AAPL_ACCEPTED,
        item_code="2.02",
        item_label="Results of Operations and Financial Condition",
    ),
    pit_tables.EventRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        accession=AAPL_8K,
        acceptance_ts=AAPL_ACCEPTED,
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
        value=102_500_000_000.0,
        period_end=date(2025, 9, 27),
        accession="0000320193-25-000070",
        acceptance_ts=AAPL_ACCEPTED,
        unit="USD",
        currency="USD",
        source_taxonomy="us-gaap",
        source_concept="Revenues",
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
        acceptance_ts=AAPL_ACCEPTED,
        text="Item 2.02 Results of Operations and Financial Condition\n\n"
        "The Company reported quarterly revenue of $102.5 billion.",
    ),
    doubles.FilingsTextRow(
        accession=AAPL_8K,
        document="aapl-20251030.htm",
        section="9.01",
        section_order=2,
        acceptance_ts=AAPL_ACCEPTED,
        text="Item 9.01 Financial Statements and Exhibits\n\n(d) Exhibits\n99.1 Press release.",
    ),
    doubles.FilingsTextRow(
        accession=AAPL_8K,
        document="a8kexhibit991.htm",
        doc_type="EX-99.1",
        section="unsegmented",
        acceptance_ts=AAPL_ACCEPTED,
        text="Apple reports fourth quarter results.",
    ),
]


def _insert(con: duckdb.DuckDBPyConnection, table: str, frame: pl.DataFrame) -> None:
    con.register("_api_frame", frame)
    try:
        con.execute(f"INSERT INTO {table} SELECT * FROM _api_frame")
    finally:
        con.unregister("_api_frame")


def build_phase2_db(*, path: Path) -> None:
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
    build_phase2_db(path=path)
    return path


@pytest.fixture
def v1_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "v1.duckdb"
    con = pit_tables.build_pit_db(FUNDAMENTALS, EVENTS, path=path)
    con.close()
    return path


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("EDGARPIT_DB", str(db_path))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def v1_client(v1_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("EDGARPIT_DB", str(v1_db_path))
    with TestClient(app) as test_client:
        yield test_client


def get_json(client: TestClient, url: str, **params: Any) -> Any:
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_asof_usd_returns_the_converted_value(client: TestClient) -> None:
    body = get_json(
        client,
        "/asof",
        cik=RY_CIK,
        field="revenue",
        asof="2024-01-15T00:00:00Z",
        currency="USD",
    )

    assert len(body) == 1
    assert body[0]["value"] == pytest.approx(56_129_000_000.0 / FX_RATE)
    assert body[0]["currency"] == "USD"
    assert body[0]["unit"] == "USD"
    assert body[0]["acceptance_ts"] == "2023-11-30T11:45:12Z"
    assert body[0]["accession"] == "0001193125-23-285639"


def test_asof_usd_waits_for_the_rate_that_converts_the_value(client: TestClient) -> None:
    """Classic bug (decision A-32): the acceptance time alone is not enough."""
    params = {"ticker": "SHOP", "field": "revenue", "currency": "USD"}
    before = get_json(client, "/asof", **params, asof="2023-10-31T20:59:59Z")
    at_publication = get_json(client, "/asof", **params, asof="2023-10-31T21:00:00Z")

    assert before == []
    assert len(at_publication) == 1
    assert at_publication[0]["value"] == pytest.approx(7_000_000_000.0 / FX_RATE)
    assert at_publication[0]["acceptance_ts"] == "2023-10-31T15:00:00Z"


def test_asof_usd_restatement_boundary(client: TestClient) -> None:
    params = {"ticker": "RY", "field": "revenue", "currency": "USD"}
    before = get_json(client, "/asof", **params, asof="2024-12-04T16:29:59Z")
    at = get_json(client, "/asof", **params, asof="2024-12-04T16:30:00Z")

    assert (before[0]["version"], before[0]["is_restatement"]) == (1, False)
    assert before[0]["value"] == pytest.approx(56_129_000_000.0 / FX_RATE)
    assert (at[0]["version"], at[0]["is_restatement"]) == (2, True)
    assert at[0]["restatement_kind"] == "revision"
    assert at[0]["value"] == pytest.approx(51_464_000_000.0 / FX_RATE)


def test_history_usd_returns_only_converted_versions(client: TestClient) -> None:
    body = get_json(client, "/history", cik=RY_CIK, field="revenue", currency="USD")
    reported = get_json(client, "/history", cik=RY_CIK, field="revenue")

    assert [(row["period_end"], row["version"]) for row in body] == [
        ("2023-10-31", 1),
        ("2023-10-31", 2),
    ]
    assert {row["currency"] for row in body} == {"USD"}
    assert {row["unit"] for row in reported} == {"CAD"}


@pytest.mark.parametrize("currency", ["CAD", "usd", "EUR"])
def test_currency_other_than_usd_is_rejected(client: TestClient, currency: str) -> None:
    asof = client.get(
        "/asof",
        params={
            "ticker": "RY",
            "field": "revenue",
            "asof": "2024-01-15T00:00:00Z",
            "currency": currency,
        },
    )
    history = client.get(
        "/history", params={"ticker": "RY", "field": "revenue", "currency": currency}
    )

    assert asof.status_code == 422
    assert history.status_code == 422


def test_text_returns_every_section_in_order(client: TestClient) -> None:
    body = get_json(client, "/text", ticker="aapl")

    assert [(row["accession"], row["document"], row["section_order"]) for row in body] == [
        (AAPL_8K, "a8kexhibit991.htm", 1),
        (AAPL_8K, "aapl-20251030.htm", 1),
        (AAPL_8K, "aapl-20251030.htm", 2),
    ]
    assert body[0]["doc_type"] == "EX-99.1"
    assert body[0]["section"] == "unsegmented"
    assert body[0]["acceptance_ts"] == "2025-10-30T20:05:00Z"
    assert body[0]["ticker"] == "AAPL"
    assert body[1]["sha256"] == doubles.sha256_of(body[1]["text"])
    assert body[1]["char_count"] == len(body[1]["text"])


def test_text_item_filter_keeps_the_filings_exhibit(client: TestClient) -> None:
    body = get_json(client, "/text", cik=AAPL_CIK, item="2.02")

    assert len(body) == 3
    assert {row["doc_type"] for row in body} == {"primary", "EX-99.1"}
    assert get_json(client, "/text", cik=AAPL_CIK, item="5.02") == []


def test_text_time_bounds_are_inclusive(client: TestClient) -> None:
    params = {"cik": AAPL_CIK}
    body = get_json(
        client,
        "/text",
        **params,
        start="2025-10-30T20:05:00Z",
        end="2025-10-30T20:05:00Z",
    )

    assert len(body) == 3
    assert get_json(client, "/text", **params, end="2025-10-30T20:04:59Z") == []


def test_text_rejects_naive_bounds_and_unknown_issuers(client: TestClient) -> None:
    naive = client.get("/text", params={"cik": AAPL_CIK, "start": "2025-10-30T20:05:00"})
    unknown = client.get("/text", params={"ticker": "ZZZZ"})
    both = client.get("/text", params={"ticker": "AAPL", "cik": AAPL_CIK})
    neither = client.get("/text")

    assert naive.status_code == 422
    assert "timezone" in naive.json()["detail"]
    assert unknown.status_code == 404
    assert both.status_code == 422
    assert neither.status_code == 422


def test_a_database_without_the_phase2_models_answers_501(v1_client: TestClient) -> None:
    """A 1.0.0 release keeps answering v1 reads and names the missing model."""
    usd = v1_client.get(
        "/asof",
        params={
            "ticker": "RY",
            "field": "revenue",
            "asof": "2024-01-15T00:00:00Z",
            "currency": "USD",
        },
    )
    text = v1_client.get("/text", params={"ticker": "AAPL"})
    reported = v1_client.get(
        "/asof", params={"ticker": "RY", "field": "revenue", "asof": "2024-01-15T00:00:00Z"}
    )

    assert usd.status_code == 501
    assert "main.fundamentals_pit_usd" in usd.json()["detail"]
    assert text.status_code == 501
    assert "main.filings_text" in text.json()["detail"]
    assert reported.status_code == 200


def test_response_models_mirror_the_query_dataclasses() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert tuple(schemas["TextResponse"]["properties"]) == tuple(
        field.name for field in dataclasses.fields(query.TextResult)
    )


def test_openapi_documents_the_phase2_surface(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    asof_parameters = {
        parameter["name"]: parameter for parameter in schema["paths"]["/asof"]["get"]["parameters"]
    }
    text_operation = schema["paths"]["/text"]["get"]

    assert {"ticker", "cik", "item", "start", "end"} == {
        parameter["name"] for parameter in text_operation["parameters"]
    }
    assert text_operation["summary"]
    for parameter in text_operation["parameters"]:
        assert parameter["description"]
    assert asof_parameters["currency"]["description"]
    # A one-value Literal is published as ``const`` by pydantic v2.
    assert asof_parameters["currency"]["schema"]["anyOf"][0]["const"] == "USD"
    assert client.get("/docs").status_code == 200
