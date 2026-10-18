"""Tests for universe fetch orchestration (FR-1.2, decision A-1).

Every HTTP response is served from the committed fixture tree through respx,
so nothing here touches the network. The fixture tree stores a header for
every kept filing; a fetch stores only the headers decision A-1 requires
(every ``us-gaap``/``ifrs-full`` fact accession and every 8-K/8-K-A), so the
expected header set is recomputed independently in this file.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from edgarpit.config import Settings
from edgarpit.fetch import FetchError, _fact_accessions, fetch_universe
from edgarpit.rawcache import RawCache
from edgarpit.sec_client import SecClient, classify
from edgarpit.universe import Issuer, load_universe

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = REPO_ROOT / "config" / "universe.csv"
FIXTURES = Path(__file__).parent / "fixtures" / "sec"
RAW = FIXTURES / "raw"
RY_CIK = "0001000275"
APPLE_CIK = "0000320193"
SHOP_CIK = "0001594805"
EVENT_FORMS = frozenset({"8-K", "8-K/A"})


class FakeClock:
    """Monotonic clock whose ``sleep`` advances it and records the durations."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_settings(**overrides: object) -> Settings:
    return Settings(sec_user_agent="Test User test@example.com", **overrides)


def fixture_issuers(tickers: Sequence[str] = ("RY", "AAPL", "SHOP")) -> list[Issuer]:
    universe = {issuer.ticker: issuer for issuer in load_universe(UNIVERSE_PATH)}
    return [universe[ticker] for ticker in tickers]


def latest_file(directory: Path, pattern: str) -> Path:
    paths = sorted(directory.glob(pattern))
    assert paths, f"no fixture file {pattern} under {directory}"
    return paths[-1]


def submissions_blocks(cik: str) -> list[dict[str, Any]]:
    main = json.loads(latest_file(RAW / "submissions" / cik, "*.json").read_text())
    blocks = [main["filings"]["recent"]]
    for path in sorted((RAW / "submissions_page" / cik).glob("*/*.json")):
        blocks.append(json.loads(path.read_text(encoding="utf-8")))
    return blocks


def expected_required(cik: str) -> set[tuple[str, str]]:
    """Recompute the A-1 header rule from the fixture documents alone."""
    accessions: set[str] = set()
    for block in submissions_blocks(cik):
        for form, accession in zip(block["form"], block["accessionNumber"], strict=True):
            if form in EVENT_FORMS:
                accessions.add(str(accession))
    facts = json.loads(latest_file(RAW / "companyfacts" / cik, "*.json").read_text())
    for taxonomy in ("us-gaap", "ifrs-full"):
        for entry in facts["facts"].get(taxonomy, {}).values():
            for rows in entry["units"].values():
                for row in rows:
                    accessions.add(str(row["accn"]))
    return {(cik, accession) for accession in accessions}


def fixture_response(request: httpx.Request) -> httpx.Response:
    """Serve a request from the fixture tree, or 404 when it is not stored."""
    endpoint, cik, key = classify(str(request.url))
    directory = RAW / endpoint / (cik or "_")
    if key:
        directory = directory / key
    suffix = ".hdr.sgml" if endpoint == "headers" else ".json"
    paths = sorted(directory.glob(f"*{suffix}"))
    if not paths:
        return httpx.Response(404)
    return httpx.Response(200, content=paths[-1].read_bytes())


def mock_fixtures(mock: respx.MockRouter) -> tuple[respx.Route, respx.Route]:
    """Route data.sec.gov and www.sec.gov/Archives through the fixture tree."""
    data = mock.route(url__regex=r"https://data\.sec\.gov/.*").mock(side_effect=fixture_response)
    archives = mock.route(url__regex=r"https://www\.sec\.gov/Archives/.*").mock(
        side_effect=fixture_response
    )
    return data, archives


def build_client(tmp_path: Path, clock: FakeClock) -> SecClient:
    return SecClient(
        make_settings(),
        cache=RawCache(tmp_path),
        clock=clock.monotonic,
        sleep=clock.sleep,
    )


def test_full_fetch_stores_the_required_fixture_content(tmp_path: Path) -> None:
    issuers = fixture_issuers()
    clock = FakeClock()
    client = build_client(tmp_path, clock)
    with respx.mock(assert_all_called=False) as mock:
        data_route, _ = mock_fixtures(mock)
        report = fetch_universe(issuers, tmp_path, client=client)

    assert report.ok
    assert report.errors == []
    assert report.issuers_ok == 3
    assert report.issuers_failed == 0
    assert report.headers_404 == []
    assert report.headers_fetched == sum(
        len(expected_required(issuer.cik or "")) for issuer in issuers
    )
    assert [issuer.cik for issuer in report.issuers] == [RY_CIK, APPLE_CIK, SHOP_CIK]
    assert report.requests == data_route.call_count + sum(
        len(expected_required(issuer.cik or "")) for issuer in issuers
    )
    for issuer, report_row in zip(issuers, report.issuers, strict=True):
        cik = issuer.cik or ""
        required = expected_required(cik)
        pages = len(list((RAW / "submissions_page" / cik).glob("*/*.json")))
        assert report_row.submissions == 1
        assert report_row.companyfacts == 1
        assert report_row.documents == 2
        assert report_row.pages == pages
        assert report_row.headers_fetched == len(required)
        assert report_row.headers_cached == 0
        assert report_row.headers_not_found == 0
        assert report_row.requests == 2 + pages + len(required)
        stored = {path.name for path in (tmp_path / "raw" / "headers" / cik).iterdir()}
        assert stored == {accession for _, accession in required}

    cache = RawCache(tmp_path)
    for cik in (RY_CIK, APPLE_CIK, SHOP_CIK):
        assert (
            cache.latest("submissions", cik)
            == latest_file(RAW / "submissions" / cik, "*.json").read_bytes()
        )
        assert (
            cache.latest("companyfacts", cik)
            == latest_file(RAW / "companyfacts" / cik, "*.json").read_bytes()
        )
        fixture_pages = {
            path.parent.name for path in (RAW / "submissions_page" / cik).glob("*/*.json")
        }
        assert set(cache.keys("submissions_page", cik)) == fixture_pages
        for page in fixture_pages:
            assert (
                cache.latest("submissions_page", cik, page)
                == latest_file(RAW / "submissions_page" / cik / page, "*.json").read_bytes()
            )


def test_second_run_fetches_zero_headers(tmp_path: Path) -> None:
    issuers = fixture_issuers()
    clock = FakeClock()
    client = build_client(tmp_path, clock)
    total_headers = sum(len(expected_required(issuer.cik or "")) for issuer in issuers)
    with respx.mock(assert_all_called=False) as mock:
        _, archive_route = mock_fixtures(mock)
        first = fetch_universe(issuers, tmp_path, client=client)
        assert first.ok
        assert archive_route.call_count == total_headers
        second = fetch_universe(issuers, tmp_path, client=client)

    assert archive_route.call_count == total_headers
    assert second.ok
    for issuer, report_row in zip(issuers, second.issuers, strict=True):
        assert report_row.headers_fetched == 0
        assert report_row.headers_cached == len(expected_required(issuer.cik or ""))
        assert report_row.submissions == 1  # documents are re-fetched when refreshing


def test_refresh_false_reuses_a_warm_cache_without_requests(tmp_path: Path) -> None:
    issuers = fixture_issuers()
    clock = FakeClock()
    client = build_client(tmp_path, clock)
    with respx.mock(assert_all_called=False) as mock:
        data_route, archive_route = mock_fixtures(mock)
        assert fetch_universe(issuers, tmp_path, client=client).ok
        calls = (data_route.call_count, archive_route.call_count)
        warm = fetch_universe(issuers, tmp_path, client=client, refresh=False)

    assert warm.ok
    assert warm.requests == 0
    assert (data_route.call_count, archive_route.call_count) == calls
    for issuer, report_row in zip(issuers, warm.issuers, strict=True):
        assert report_row.submissions == 0
        assert report_row.pages == 0
        assert report_row.companyfacts == 0
        assert report_row.headers_fetched == 0
        assert report_row.headers_cached == len(expected_required(issuer.cik or ""))


def test_a_header_404_is_recorded_and_tolerated(tmp_path: Path) -> None:
    issuers = fixture_issuers(("AAPL",))
    required = expected_required(APPLE_CIK)
    missing = sorted(required)[0][1]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"{missing}.hdr.sgml"):
            return httpx.Response(404)
        return fixture_response(request)

    clock = FakeClock()
    client = build_client(tmp_path, clock)
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=r"https://(data|www)\.sec\.gov/.*").mock(side_effect=handler)
        report = fetch_universe(issuers, tmp_path, client=client)

    assert report.ok
    assert report.errors == []
    row = report.issuers[0]
    assert row.headers_not_found == 1
    assert row.headers_404 == [missing]
    assert report.headers_not_found == 1
    assert row.headers_fetched == len(required) - 1
    assert not (tmp_path / "raw" / "headers" / APPLE_CIK / missing).exists()


def test_persistent_503_fails_that_issuer_only(tmp_path: Path) -> None:
    issuers = fixture_issuers(("RY", "AAPL"))
    clock = FakeClock()
    client = build_client(tmp_path, clock)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/xbrl/companyfacts/CIK{RY_CIK}.json":
            return httpx.Response(503)
        return fixture_response(request)

    with respx.mock(assert_all_called=False) as mock:
        failure = mock.route(
            url__regex=rf"https://data\.sec\.gov/api/xbrl/companyfacts/CIK{RY_CIK}\.json"
        ).mock(side_effect=handler)
        mock_fixtures(mock)
        report = fetch_universe(issuers, tmp_path, client=client)

    assert not report.ok
    ryan, apple = report.issuers
    assert ryan.errors and "503" in ryan.errors[0]
    assert ryan.companyfacts == 0
    assert ryan.headers_fetched == 0
    assert failure.call_count == 6  # six attempts, then the issuer fails
    assert apple.errors == []
    assert apple.headers_fetched == len(expected_required(APPLE_CIK))


def test_requests_are_spaced_across_issuers_with_one_limiter(tmp_path: Path) -> None:
    issuers = fixture_issuers(("RY", "SHOP"))
    clock = FakeClock()
    client = build_client(tmp_path, clock)
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(clock.now)
        return fixture_response(request)

    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=r"https://(data|www)\.sec\.gov/.*").mock(side_effect=handler)
        assert fetch_universe(issuers, tmp_path, client=client).ok

    assert len(starts) == 2 * 2 + 17 + len(expected_required(RY_CIK)) + len(
        expected_required(SHOP_CIK)
    )
    for earlier, later in itertools.pairwise(starts):
        assert later - earlier >= 1.0 / 5.0


def test_a_missing_cik_fails_only_that_issuer(tmp_path: Path) -> None:
    issuers = [
        Issuer("NONE", None, "No CIK Inc.", "US", "10-K"),
        *fixture_issuers(("SHOP",)),
    ]
    clock = FakeClock()
    client = build_client(tmp_path, clock)
    with respx.mock(assert_all_called=False) as mock:
        mock_fixtures(mock)
        report = fetch_universe(issuers, tmp_path, client=client)

    assert not report.ok
    assert "no CIK" in report.issuers[0].errors[0]
    assert report.issuers[0].requests == 0
    assert report.issuers[1].errors == []
    assert report.issuers[1].headers_fetched == len(expected_required(SHOP_CIK))
    assert (report.issuers_ok, report.issuers_failed) == (1, 1)


def fact_entry(accn: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "start": "2022-11-01",
        "end": "2023-10-31",
        "val": 1,
        "accn": accn,
        "fy": 2023,
        "fp": "FY",
        "form": "40-F",
        "filed": "2023-11-30",
    }
    entry.update(overrides)
    return entry


def test_fact_accessions_use_the_staging_flattener() -> None:
    """The shared flattener decides what drives a header fetch, not a private walk.

    ``dei`` facts are excluded and entries the loader cannot stage (no
    ``form``/``filed``) are skipped, so the fetch and the build agree on the
    required accessions.
    """
    document = {
        "cik": 1000275,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            fact_entry("0001000275-23-000001"),
                            fact_entry("0001000275-23-000002", form=None),
                        ]
                    }
                }
            },
            "ifrs-full": {"Revenue": {"units": {"CAD": [fact_entry("0001000275-23-000003")]}}},
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": [fact_entry("0001000275-23-000004")]}
                }
            },
        },
    }

    assert _fact_accessions(RY_CIK, document) == [
        (RY_CIK, "0001000275-23-000001"),
        (RY_CIK, "0001000275-23-000003"),
    ]


def test_fact_accessions_reject_a_document_without_facts() -> None:
    with pytest.raises(FetchError, match="no 'facts' object"):
        _fact_accessions(RY_CIK, {"cik": 1000275})


def test_a_malformed_companyfacts_fails_only_that_issuer(tmp_path: Path) -> None:
    issuers = fixture_issuers(("AAPL",))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/xbrl/companyfacts/CIK{APPLE_CIK}.json":
            return httpx.Response(200, json={"cik": 320193})
        return fixture_response(request)

    clock = FakeClock()
    client = build_client(tmp_path, clock)
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=r"https://(data|www)\.sec\.gov/.*").mock(side_effect=handler)
        report = fetch_universe(issuers, tmp_path, client=client)

    assert not report.ok
    row = report.issuers[0]
    assert row.errors and "no 'facts' object" in row.errors[0]
    assert row.headers_fetched == 0
    assert row.companyfacts == 1
