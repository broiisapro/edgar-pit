"""Tests for the FastAPI service in :mod:`api.main`."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pit_tables
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.main import app
from edgarpit import __version__, query

RY_CIK = "0001000275"
AAPL_CIK = "0000320193"

RY_ORIGINAL_ACCEPTED = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
RY_RESTATED_ACCEPTED = datetime(2024, 12, 4, 16, 30, 0, tzinfo=UTC)
FIRST_EVENT_AT = datetime(2025, 1, 30, 21, 30, 0, tzinfo=UTC)
SECOND_EVENT_AT = datetime(2025, 2, 11, 13, 0, 0, tzinfo=UTC)

FUNDAMENTALS = [
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
    pit_tables.EventRow(
        cik=AAPL_CIK,
        ticker="AAPL",
        accession="0000320193-25-000012",
        acceptance_ts=SECOND_EVENT_AT,
        item_code="2.02",
        item_label="Results of Operations and Financial Condition",
    ),
]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "edgarpit.duckdb"
    con = pit_tables.build_pit_db(FUNDAMENTALS, EVENTS, path=path)
    con.close()
    return path


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("EDGARPIT_DB", str(db_path))
    with TestClient(app) as test_client:
        yield test_client


def get_json(client: TestClient, url: str, **params: Any) -> Any:
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_version_matches_the_package_version(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert __version__ == "1.0.0"
    assert app.version == __version__
    assert schema["info"]["version"] == __version__


def test_asof_returns_the_version_known_at_the_time(client: TestClient) -> None:
    body = get_json(client, "/asof", cik="1000275", field="revenue", asof="2024-01-15T00:00:00Z")

    assert body == [
        {
            "cik": RY_CIK,
            "ticker": "RY",
            "field": "revenue",
            "value": 56_129_000_000.0,
            "unit": "CAD",
            "currency": "CAD",
            "period_start": "2022-11-01",
            "period_end": "2023-10-31",
            "period_type": "FY",
            "accession": "0001193125-23-285639",
            "form": "40-F",
            "acceptance_ts": "2023-11-30T11:45:12Z",
            "version": 1,
            "is_restatement": False,
            "ts_imputed": False,
            "restatement_kind": "none",
        }
    ]


def test_asof_ticker_is_case_insensitive(client: TestClient) -> None:
    body = get_json(client, "/asof", ticker="ry", field="revenue", asof="2024-01-15T00:00:00Z")

    assert [row["cik"] for row in body] == [RY_CIK]


def test_asof_restatement_boundary(client: TestClient) -> None:
    params = {"ticker": "RY", "field": "revenue"}
    before = get_json(client, "/asof", **params, asof="2024-12-04T16:29:59Z")
    at = get_json(client, "/asof", **params, asof="2024-12-04T16:30:00Z")
    after = get_json(client, "/asof", **params, asof="2024-12-04T16:30:01Z")

    assert (before[0]["value"], before[0]["version"]) == (56_129_000_000.0, 1)
    assert (at[0]["value"], at[0]["version"]) == (51_464_000_000.0, 2)
    assert at[0]["acceptance_ts"] == "2024-12-04T16:30:00Z"
    assert after[0]["is_restatement"] is True
    assert after[0]["restatement_kind"] == "revision"


def test_asof_period_filter_and_n(client: TestClient) -> None:
    annual = get_json(
        client,
        "/asof",
        cik=RY_CIK,
        field="revenue",
        asof="2024-06-01T00:00:00Z",
        period="FY",
        n=10,
    )
    quarterly = get_json(
        client,
        "/asof",
        cik=RY_CIK,
        field="revenue",
        asof="2024-06-01T00:00:00Z",
        period="Q",
    )
    newest = get_json(client, "/asof", cik=RY_CIK, field="revenue", asof="2024-06-01T00:00:00Z")

    assert [row["period_end"] for row in annual] == ["2023-10-31", "2022-10-31"]
    assert [row["period_type"] for row in quarterly] == ["Q"]
    assert [row["period_end"] for row in newest] == ["2023-10-31"]


def test_asof_empty_result_is_200_with_empty_list(client: TestClient) -> None:
    body = get_json(client, "/asof", cik=RY_CIK, field="assets", asof="2024-06-01T00:00:00Z")

    assert body == []


@pytest.mark.parametrize(
    "params",
    [
        {"field": "revenue", "asof": "2024-01-15T00:00:00Z"},
        {"ticker": "RY", "cik": RY_CIK, "field": "revenue", "asof": "2024-01-15T00:00:00Z"},
    ],
)
def test_asof_requires_exactly_one_identifier(client: TestClient, params: dict[str, str]) -> None:
    response = client.get("/asof", params=params)

    assert response.status_code == 422
    assert "exactly one" in response.json()["detail"]


@pytest.mark.parametrize("asof", ["2024-01-15T00:00:00", "2024-01-15"])
def test_asof_rejects_naive_timestamps_with_a_clear_message(client: TestClient, asof: str) -> None:
    response = client.get("/asof", params={"ticker": "RY", "field": "revenue", "asof": asof})

    assert response.status_code == 422
    assert "timezone" in response.json()["detail"]


def test_asof_rejects_malformed_timestamps(client: TestClient) -> None:
    response = client.get(
        "/asof", params={"ticker": "RY", "field": "revenue", "asof": "not-a-timestamp"}
    )

    assert response.status_code == 422


def test_asof_rejects_unknown_period_and_non_positive_n(client: TestClient) -> None:
    bad_period = client.get(
        "/asof",
        params={
            "ticker": "RY",
            "field": "revenue",
            "asof": "2024-01-15T00:00:00Z",
            "period": "OTHER",
        },
    )
    zero_n = client.get(
        "/asof",
        params={"ticker": "RY", "field": "revenue", "asof": "2024-01-15T00:00:00Z", "n": 0},
    )

    assert bad_period.status_code == 422
    assert zero_n.status_code == 422


def test_asof_unknown_issuer_is_404(client: TestClient) -> None:
    unknown_ticker = client.get(
        "/asof", params={"ticker": "ZZZZ", "field": "revenue", "asof": "2024-01-15T00:00:00Z"}
    )
    unknown_cik = client.get(
        "/asof",
        params={"cik": "9999999999", "field": "revenue", "asof": "2024-01-15T00:00:00Z"},
    )

    assert unknown_ticker.status_code == 404
    assert unknown_cik.status_code == 404


def test_cik_must_be_digits(client: TestClient) -> None:
    response = client.get(
        "/asof", params={"cik": "RY", "field": "revenue", "asof": "2024-01-15T00:00:00Z"}
    )

    assert response.status_code == 422
    assert "digits" in response.json()["detail"]


def test_history_returns_every_version(client: TestClient) -> None:
    body = get_json(client, "/history", cik=RY_CIK, field="revenue")

    assert [(row["period_end"], row["version"], row["accession"]) for row in body] == [
        ("2022-10-31", 1, "0001193125-22-280000"),
        ("2023-10-31", 1, "0001193125-23-285639"),
        ("2023-10-31", 2, "0001193125-24-270294"),
        ("2024-01-31", 1, "0001193125-24-100000"),
    ]
    assert body[2]["acceptance_ts"] == "2024-12-04T16:30:00Z"


def test_history_period_filter(client: TestClient) -> None:
    annual = get_json(client, "/history", cik=RY_CIK, field="revenue", period="FY")
    quarterly = get_json(client, "/history", cik=RY_CIK, field="revenue", period="Q")

    assert len(annual) == 3
    assert [row["period_type"] for row in quarterly] == ["Q"]


def test_history_error_paths(client: TestClient) -> None:
    empty = get_json(client, "/history", cik=RY_CIK, field="assets")
    unknown = client.get("/history", params={"ticker": "ZZZZ", "field": "revenue"})
    both = client.get("/history", params={"ticker": "RY", "cik": RY_CIK, "field": "revenue"})

    assert empty == []
    assert unknown.status_code == 404
    assert both.status_code == 422


def test_events_returns_items_ordered_by_acceptance_time(client: TestClient) -> None:
    body = get_json(client, "/events", cik=AAPL_CIK)

    assert [(row["acceptance_ts"], row["item_code"]) for row in body] == [
        ("2025-01-30T21:30:00Z", "2.02"),
        ("2025-01-30T21:30:00Z", "9.01"),
        ("2025-02-11T13:00:00Z", "2.02"),
    ]
    assert body[0]["item_label"] == "Results of Operations and Financial Condition"
    assert body[0]["ticker"] == "AAPL"


def test_events_item_filter(client: TestClient) -> None:
    body = get_json(client, "/events", ticker="aapl", item="9.01")

    assert [(row["accession"], row["item_code"]) for row in body] == [
        ("0000320193-25-000008", "9.01")
    ]


def test_events_from_to_are_inclusive(client: TestClient) -> None:
    params = {"cik": AAPL_CIK}
    both = get_json(
        client,
        "/events",
        **params,
        **{"from": "2025-01-30T21:30:00Z", "to": "2025-02-11T13:00:00Z"},
    )
    after_first = get_json(client, "/events", **params, **{"from": "2025-01-30T21:30:01Z"})
    before_second = get_json(client, "/events", **params, **{"to": "2025-02-11T12:59:59Z"})
    same_instant = get_json(
        client,
        "/events",
        **params,
        **{"from": "2025-02-11T13:00:00Z", "to": "2025-02-11T13:00:00Z"},
    )
    none = get_json(client, "/events", **params, **{"from": "2025-02-11T13:00:01Z"})

    assert len(both) == 3
    assert len(after_first) == 1
    assert len(before_second) == 2
    assert [row["acceptance_ts"] for row in same_instant] == ["2025-02-11T13:00:00Z"]
    assert none == []


def test_events_rejects_naive_bounds(client: TestClient) -> None:
    naive_from = client.get("/events", params={"cik": AAPL_CIK, "from": "2025-01-30T21:30:00"})
    naive_to = client.get("/events", params={"cik": AAPL_CIK, "to": "2025-02-11T13:00:00"})

    assert naive_from.status_code == 422
    assert naive_to.status_code == 422
    assert "timezone" in naive_from.json()["detail"]
    assert "timezone" in naive_to.json()["detail"]


def test_events_unknown_issuer_is_404(client: TestClient) -> None:
    response = client.get("/events", params={"ticker": "ZZZZ"})

    assert response.status_code == 404


def test_response_models_mirror_the_query_dataclasses() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert tuple(schemas["AsOfResponse"]["properties"]) == tuple(
        field.name for field in dataclasses.fields(query.AsOfResult)
    )
    assert tuple(schemas["EventResponse"]["properties"]) == tuple(
        field.name for field in dataclasses.fields(query.EventResult)
    )


def test_openapi_documents_every_parameter(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert set(schema["paths"]) == {"/health", "/asof", "/history", "/events", "/text"}
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            assert operation.get("summary"), (path, method)
            for parameter in operation.get("parameters", []):
                assert parameter.get("description"), (path, method, parameter["name"])

    assert client.get("/docs").status_code == 200


def test_each_request_opens_one_read_only_connection(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_connect = duckdb.connect

    def spy(*args: Any, **kwargs: Any) -> duckdb.DuckDBPyConnection:
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(api_main.duckdb, "connect", spy)
    get_json(client, "/asof", cik=RY_CIK, field="revenue", asof="2024-01-15T00:00:00Z")

    assert len(calls) == 1
    (path,), kwargs = calls[0]
    assert str(path) == str(db_path)
    assert kwargs.get("read_only") is True
