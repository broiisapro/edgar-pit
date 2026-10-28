"""Tests for the rate-limited, retrying SEC client (decisions A-2, A-3)."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from tenacity import RetryCallState

from edgarpit.config import ConfigError, Settings
from edgarpit.fx.valet import valet_url
from edgarpit.rawcache import RawCache, document_key
from edgarpit.sec_client import (
    MAX_BACKOFF_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    OfflineMiss,
    SecAccessDenied,
    SecClient,
    SecClientError,
    SecNotFound,
    _parse_retry_after,
    _retry_wait,
    classify,
    company_tickers_url,
    companyconcept_url,
    companyfacts_url,
    document_url,
    filing_index_url,
    frames_url,
    header_url,
    submissions_page_url,
    submissions_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
REPO_ROOT = Path(__file__).parent.parent
RY = "0001000275"
RY_SUBMISSIONS = submissions_url(RY)


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


def test_classify_round_trips_known_urls() -> None:
    assert classify(submissions_url("1000275")) == ("submissions", "0001000275", "")
    assert classify(companyfacts_url("0001000275")) == ("companyfacts", "0001000275", "")
    assert classify(submissions_page_url("CIK0001594805-submissions-001.json")) == (
        "submissions_page",
        "0001594805",
        "CIK0001594805-submissions-001",
    )
    assert classify(header_url("1000275", "0001193125-23-285639")) == (
        "headers",
        "0001000275",
        "0001193125-23-285639",
    )
    assert classify(company_tickers_url()) == ("company_tickers", "", "")
    with pytest.raises(ValueError, match="cannot classify"):
        classify("https://example.com/nope")
    with pytest.raises(ValueError, match="does not match"):
        classify(
            "https://www.sec.gov/Archives/edgar/data/320193/000119312510012091/"
            "0001193125-10-012090.hdr.sgml"
        )
    with pytest.raises(ValueError, match="CIK must be digits"):
        submissions_url("not-a-cik")


def test_user_agent_is_validated_before_any_request() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(RY_SUBMISSIONS).mock(return_value=httpx.Response(200, json={}))
        for agent in (None, "", "not-an-email", " Name  "):
            with pytest.raises(ConfigError):
                SecClient(Settings(sec_user_agent=agent))
        assert route.call_count == 0


def test_online_sends_user_agent_and_accept_encoding() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["user-agent"] = request.headers["User-Agent"]
        captured["accept-encoding"] = request.headers["Accept-Encoding"]
        return httpx.Response(200, json={"ok": True})

    client = SecClient(make_settings(), transport=httpx.MockTransport(handler))
    assert client.get_json(RY_SUBMISSIONS) == {"ok": True}
    assert captured["user-agent"] == "Test User test@example.com"
    assert captured["accept-encoding"] == "gzip, deflate"


def test_rate_limit_spaces_requests_and_no_window_holds_more_than_five() -> None:
    clock = FakeClock()
    client = SecClient(make_settings(rate_limit_rps=5.0), clock=clock.monotonic, sleep=clock.sleep)
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(clock.now)
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=False) as mock:
        mock.get(RY_SUBMISSIONS).mock(side_effect=handler)
        for _ in range(50):
            assert client.get_json(RY_SUBMISSIONS) == {"ok": True}

    assert len(starts) == 50
    assert starts[-1] - starts[0] >= 9.8
    windows = [sum(1 for start in starts if begin <= start < begin + 1.0) for begin in starts]
    assert max(windows) <= 5


def test_rate_limit_uses_the_configured_rate() -> None:
    clock = FakeClock()
    client = SecClient(make_settings(rate_limit_rps=2.0), clock=clock.monotonic, sleep=clock.sleep)
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(clock.now)
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=False) as mock:
        mock.get(RY_SUBMISSIONS).mock(side_effect=handler)
        for _ in range(4):
            client.get_json(RY_SUBMISSIONS)

    assert starts[0] == 0.0
    for earlier, later in itertools.pairwise(starts):
        assert later - earlier >= 0.5
    assert starts[-1] - starts[0] >= 1.5


def test_retry_after_is_honoured_and_success_is_cached(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = RawCache(tmp_path)
    client = SecClient(make_settings(), cache=cache, clock=clock.monotonic, sleep=clock.sleep)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(RY_SUBMISSIONS).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200, json={"cik": "0001000275"}),
            ]
        )
        assert client.get_json(RY_SUBMISSIONS) == {"cik": "0001000275"}
    assert route.call_count == 2
    assert clock.sleeps == [2.0]
    rows = cache.manifest_rows()
    assert len(rows) == 1
    assert rows[0]["endpoint"] == "submissions"
    assert rows[0]["http_status"] == 200
    body = cache.latest("submissions", RY)
    assert body is not None
    assert json.loads(body) == {"cik": "0001000275"}


def _retry_state(attempt: int, error: BaseException) -> RetryCallState:
    state = RetryCallState(retry_object=None, fn=lambda: None, args=(), kwargs={})
    state.attempt_number = attempt
    state.set_exception((type(error), error, error.__traceback__))
    return state


def test_parse_retry_after_accepts_delta_seconds_and_http_dates() -> None:
    now = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
    assert _parse_retry_after("120", now=now) == 120.0
    assert _parse_retry_after(" 2 ", now=now) == 2.0
    assert _parse_retry_after("Sat, 17 Oct 2026 12:02:30 GMT", now=now) == 150.0
    assert _parse_retry_after("Sun, 06 Nov 1994 08:49:37 GMT", now=now) == 0.0
    assert _parse_retry_after("Sun Nov  6 08:49:37 1994", now=now) == 0.0
    assert _parse_retry_after("garbage", now=now) is None
    assert _parse_retry_after(None, now=now) is None


def test_retry_after_http_date_in_the_past_means_no_wait() -> None:
    clock = FakeClock()
    client = SecClient(make_settings(), clock=clock.monotonic, sleep=clock.sleep)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(RY_SUBMISSIONS).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "Sun, 06 Nov 1994 08:49:37 GMT"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        assert client.get_json(RY_SUBMISSIONS) == {"ok": True}
    assert route.call_count == 2
    assert clock.sleeps[0] == 0.0


def test_retry_after_http_date_in_the_future_is_capped() -> None:
    clock = FakeClock()
    client = SecClient(make_settings(), clock=clock.monotonic, sleep=clock.sleep)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(RY_SUBMISSIONS).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "Fri, 01 Jan 2100 00:00:00 GMT"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        assert client.get_json(RY_SUBMISSIONS) == {"ok": True}
    assert route.call_count == 2
    assert clock.sleeps == [MAX_RETRY_AFTER_SECONDS]


def test_retry_after_delta_seconds_is_capped() -> None:
    clock = FakeClock()
    client = SecClient(make_settings(), clock=clock.monotonic, sleep=clock.sleep)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(RY_SUBMISSIONS).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "9999"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        assert client.get_json(RY_SUBMISSIONS) == {"ok": True}
    assert route.call_count == 2
    assert clock.sleeps == [120.0]


def test_backoff_is_capped_at_sixty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("edgarpit.sec_client.random.uniform", lambda low, high: 1.0)
    state = _retry_state(10, RuntimeError("transport down"))
    assert MAX_BACKOFF_SECONDS == 60.0
    assert _retry_wait(state) == 60.0
    state.attempt_number = 3
    assert _retry_wait(state) == 2.0


def test_failed_responses_are_not_cached(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = RawCache(tmp_path)
    client = SecClient(make_settings(), cache=cache, clock=clock.monotonic, sleep=clock.sleep)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(RY_SUBMISSIONS).mock(return_value=httpx.Response(503))
        with pytest.raises(SecClientError):
            client.get_json(RY_SUBMISSIONS)
    assert cache.manifest_rows() == []
    assert not cache.raw_dir.exists() or not any(cache.raw_dir.rglob("*.*"))


def test_six_retryable_failures_raise_after_exactly_six_attempts() -> None:
    clock = FakeClock()
    client = SecClient(make_settings(), clock=clock.monotonic, sleep=clock.sleep)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(RY_SUBMISSIONS).mock(return_value=httpx.Response(503))
        with pytest.raises(SecClientError, match="HTTP 503"):
            client.get_json(RY_SUBMISSIONS)
    assert route.call_count == 6
    assert len(clock.sleeps) == 5


def test_server_errors_are_retried_with_jittered_backoff() -> None:
    clock = FakeClock()
    client = SecClient(make_settings(), clock=clock.monotonic, sleep=clock.sleep)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(RY_SUBMISSIONS).mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(502),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        assert client.get_json(RY_SUBMISSIONS) == {"ok": True}
    assert route.call_count == 3
    assert all(seconds > 0 for seconds in clock.sleeps)


def test_transport_errors_are_retried() -> None:
    clock = FakeClock()
    client = SecClient(make_settings(), clock=clock.monotonic, sleep=clock.sleep)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(RY_SUBMISSIONS).mock(
            side_effect=[
                httpx.ConnectError("connection refused"),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        assert client.get_json(RY_SUBMISSIONS) == {"ok": True}
    assert route.call_count == 2


def test_access_denied_is_not_retried() -> None:
    clock = FakeClock()
    client = SecClient(make_settings(), clock=clock.monotonic, sleep=clock.sleep)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(RY_SUBMISSIONS).mock(return_value=httpx.Response(403))
        with pytest.raises(SecAccessDenied, match="SEC_USER_AGENT"):
            client.get_json(RY_SUBMISSIONS)
    assert route.call_count == 1
    assert clock.sleeps == []


def test_not_found_is_not_retried() -> None:
    clock = FakeClock()
    client = SecClient(make_settings(), clock=clock.monotonic, sleep=clock.sleep)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(RY_SUBMISSIONS).mock(return_value=httpx.Response(404))
        with pytest.raises(SecNotFound):
            client.get_json(RY_SUBMISSIONS)
    assert route.call_count == 1


def test_offline_mode_reads_fixtures_without_building_a_transport() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"offline mode must not send requests: {request.url}")

    client = SecClient(
        Settings(sec_user_agent=None),
        offline_dir=FIXTURES,
        transport=httpx.MockTransport(explode),
    )
    assert client.offline
    with respx.mock(assert_all_called=False) as mock:
        submissions = client.get_json(submissions_url(RY))
        assert submissions["cik"] == "0001000275"
        companyfacts = client.get_json(companyfacts_url(RY))
        assert companyfacts["cik"] == 1000275
        assert "facts" in companyfacts
        header = client.get_text(header_url(RY, "0001193125-23-285639"))
        assert "<ACCEPTANCE-DATETIME>" in header
        with pytest.raises(OfflineMiss):
            client.get_json(submissions_url("9999999999"))
        assert mock.calls.call_count == 0


def test_offline_mode_needs_no_user_agent_and_never_retries() -> None:
    client = SecClient(Settings(sec_user_agent="not-an-email"), offline_dir=FIXTURES)
    assert client.get_json(companyfacts_url(RY))["cik"] == 1000275


def company_tickers_fixture() -> dict[str, Any]:
    paths = sorted((FIXTURES / "raw" / "company_tickers" / "_").glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_company_tickers_is_cached_in_its_global_slot(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = RawCache(tmp_path)
    client = SecClient(make_settings(), cache=cache, clock=clock.monotonic, sleep=clock.sleep)
    document = company_tickers_fixture()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(company_tickers_url()).mock(return_value=httpx.Response(200, json=document))
        assert client.get_json(company_tickers_url()) == document
    rows = cache.manifest_rows()
    assert len(rows) == 1
    assert rows[0]["endpoint"] == "company_tickers"
    assert rows[0]["cik"] == ""
    assert rows[0]["path"].startswith("raw/company_tickers/_/")
    body = cache.latest("company_tickers", "")
    assert body is not None
    assert json.loads(body) == document


def test_offline_mode_serves_company_tickers() -> None:
    client = SecClient(Settings(sec_user_agent=None), offline_dir=FIXTURES)
    document = client.get_json(company_tickers_url())
    assert {entry["ticker"] for entry in document.values()} >= {"RY", "AAPL", "SHOP"}


def test_get_json_rejects_non_object_payloads() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(RY_SUBMISSIONS).mock(return_value=httpx.Response(200, json=[1, 2, 3]))
        client = SecClient(make_settings())
        with pytest.raises(SecClientError, match="expected a JSON object"):
            client.get_json(RY_SUBMISSIONS)


# --- Phase 2 endpoints: filing index, documents and Valet (decisions A-30, A-32) ---

RY_ACCESSION = "0001193125-23-285639"
AAPL_ACCESSION = "0000320193-25-000080"


def test_filing_index_url_and_classification() -> None:
    url = filing_index_url("1000275", RY_ACCESSION)
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/1000275/000119312523285639/"
        "0001193125-23-285639-index-headers.html"
    )
    assert classify(url) == ("filing_index", "0001000275", RY_ACCESSION)


def test_document_url_and_classification_keep_the_issuer_cik() -> None:
    # The filing agent's CIK prefixes the accession; the cache still belongs to
    # the issuer whose directory holds the document.
    url = document_url("0001000275", RY_ACCESSION, "exhibit99-1.htm")
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/1000275/000119312523285639/exhibit99-1.htm"
    )
    assert classify(url) == ("documents", "0001000275", f"{RY_ACCESSION}__exhibit99-1.htm")


def test_document_url_percent_encodes_the_file_name() -> None:
    url = document_url("320193", AAPL_ACCESSION, "a b&c.htm")
    assert url.endswith("/a%20b%26c.htm")
    assert classify(url) == ("documents", "0000320193", f"{AAPL_ACCESSION}__a b&c.htm")


@pytest.mark.parametrize("filename", ["", "a/b.htm", "a\\b.htm"])
def test_document_url_rejects_path_separators(filename: str) -> None:
    with pytest.raises(ValueError, match="invalid document file name"):
        document_url("320193", AAPL_ACCESSION, filename)


def test_classify_rejects_a_document_directory_without_an_accession() -> None:
    with pytest.raises(ValueError, match="cannot classify"):
        classify("https://www.sec.gov/Archives/edgar/data/320193/not-an-accession/doc.htm")
    with pytest.raises(ValueError, match="no accession directory"):
        classify("https://www.sec.gov/Archives/edgar/data/320193/123456/doc.htm")
    with pytest.raises(ValueError, match="cannot classify"):
        classify("https://www.sec.gov/Archives/edgar/data/320193/000032019325000080/")


def test_valet_url_shape_and_bounds() -> None:
    assert valet_url("FXUSDCAD") == ("https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json")
    assert valet_url("FXUSDCAD", date(2017, 1, 3)) == (
        "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?start_date=2017-01-03"
    )
    assert valet_url("FXUSDCAD", end=date(2023, 10, 31)) == (
        "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?end_date=2023-10-31"
    )
    assert valet_url("FXUSDCAD", date(2017, 1, 3), date(2023, 10, 31)) == (
        "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
        "?start_date=2017-01-03&end_date=2023-10-31"
    )
    assert classify(valet_url("FXUSDCAD", date(2017, 1, 3))) == ("fx", "", "FXUSDCAD")


@pytest.mark.parametrize("series", ["", "FX USDCAD", "FX/USDCAD", "..", "a..b", ".hidden"])
def test_valet_url_rejects_a_malformed_series(series: str) -> None:
    with pytest.raises(ValueError, match="invalid Valet series id"):
        valet_url(series)


def test_classify_rejects_a_valet_path_traversal() -> None:
    with pytest.raises(ValueError, match="invalid Valet series id"):
        classify("https://www.bankofcanada.ca/valet/observations/../json")


def test_valet_url_rejects_a_reversed_range() -> None:
    with pytest.raises(ValueError, match="is after end"):
        valet_url("FXUSDCAD", date(2023, 10, 31), date(2017, 1, 3))


def test_documents_are_cached_under_the_issuer_with_their_extension(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = RawCache(tmp_path)
    client = SecClient(make_settings(), cache=cache, clock=clock.monotonic, sleep=clock.sleep)
    url = document_url("1000275", RY_ACCESSION, "exhibit99-1.htm")
    body = "<html><body>Royal Bank reports third quarter results</body></html>"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url).mock(return_value=httpx.Response(200, text=body))
        assert client.get_text(url) == body
    row = cache.manifest_rows()[0]
    assert row["endpoint"] == "documents"
    assert row["cik"] == "0001000275"
    assert row["path"].startswith(f"raw/documents/0001000275/{RY_ACCESSION}__exhibit99-1.htm/")
    assert row["path"].endswith(".htm.zst")
    key = document_key(RY_ACCESSION, "exhibit99-1.htm")
    assert cache.latest("documents", "0001000275", key) == body.encode("utf-8")
    assert cache.keys("documents", "0001000275") == [key]
    assert cache.verify().ok


def test_filing_index_and_fx_are_cached_in_their_slots(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = RawCache(tmp_path)
    client = SecClient(make_settings(), cache=cache, clock=clock.monotonic, sleep=clock.sleep)
    index_url = filing_index_url("0000320193", AAPL_ACCESSION)
    fx_url = valet_url("FXUSDCAD")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(index_url).mock(return_value=httpx.Response(200, text="<html>index</html>"))
        mock.get(fx_url).mock(return_value=httpx.Response(200, json={"observations": []}))
        assert client.get_text(index_url) == "<html>index</html>"
        assert client.get_json(fx_url) == {"observations": []}
    endpoints = {row["endpoint"]: row for row in cache.manifest_rows()}
    assert endpoints["filing_index"]["path"].startswith(
        f"raw/filing_index/0000320193/{AAPL_ACCESSION}/"
    )
    assert endpoints["filing_index"]["path"].endswith(".html.zst")
    assert endpoints["fx"]["cik"] == ""
    assert endpoints["fx"]["path"].startswith("raw/fx/_/FXUSDCAD/")
    stored = cache.latest("fx", "", "FXUSDCAD")
    assert stored is not None
    assert json.loads(stored) == {"observations": []}
    assert cache.latest("fx", "_", "FXUSDCAD") == stored
    assert cache.verify().ok


def test_frames_url_and_classification() -> None:
    url = frames_url("dei", "EntityPublicFloat", "USD", "CY2026Q3I")
    assert url == "https://data.sec.gov/api/xbrl/frames/dei/EntityPublicFloat/USD/CY2026Q3I.json"
    assert classify(url) == ("frames", "", "dei__EntityPublicFloat__USD__CY2026Q3I")
    assert classify(frames_url("us-gaap", "Assets", "USD-per-shares", "CY2025Q4I")) == (
        "frames",
        "",
        "us-gaap__Assets__USD-per-shares__CY2025Q4I",
    )
    with pytest.raises(ValueError, match="invalid XBRL unit"):
        frames_url("dei", "EntityPublicFloat", "", "CY2026Q3I")
    with pytest.raises(ValueError, match="invalid XBRL taxonomy"):
        frames_url("..", "Assets", "USD", "CY2026Q3I")
    with pytest.raises(ValueError, match="invalid XBRL tag"):
        frames_url("dei", "Entity__PublicFloat", "USD", "CY2026Q3I")


def test_companyconcept_url_and_classification() -> None:
    url = companyconcept_url("1000275", "ifrs-full", "Assets")
    assert url == (
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0001000275/ifrs-full/Assets.json"
    )
    assert classify(url) == ("companyconcept", "0001000275", "ifrs-full__Assets")
    with pytest.raises(ValueError, match="CIK must be digits"):
        companyconcept_url("not-a-cik", "us-gaap", "Assets")
    with pytest.raises(ValueError, match="invalid XBRL tag"):
        companyconcept_url("1000275", "us-gaap", "Assets/../../../etc/passwd")


def test_xbrl_documents_are_cached_in_their_slots(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = RawCache(tmp_path)
    client = SecClient(make_settings(), cache=cache, clock=clock.monotonic, sleep=clock.sleep)
    frame = frames_url("dei", "EntityPublicFloat", "USD", "CY2026Q3I")
    concept = companyconcept_url(RY, "ifrs-full", "Assets")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(frame).mock(return_value=httpx.Response(200, json={"data": []}))
        mock.get(concept).mock(return_value=httpx.Response(200, json={"units": {}}))
        assert client.get_json(frame) == {"data": []}
        assert client.get_json(concept) == {"units": {}}
    endpoints = {row["endpoint"]: row for row in cache.manifest_rows()}
    assert endpoints["frames"]["cik"] == ""
    assert endpoints["frames"]["path"].startswith(
        "raw/frames/_/dei__EntityPublicFloat__USD__CY2026Q3I/"
    )
    assert endpoints["companyconcept"]["cik"] == RY
    assert endpoints["companyconcept"]["path"].startswith(
        f"raw/companyconcept/{RY}/ifrs-full__Assets/"
    )
    assert cache.latest("frames", "", "dei__EntityPublicFloat__USD__CY2026Q3I") is not None
    assert cache.latest("companyconcept", RY, "ifrs-full__Assets") is not None
    assert cache.verify().ok


@pytest.mark.network
def test_live_submissions_smoke(tmp_path: Path) -> None:
    """One live smoke request; excluded from CI by the ``network`` marker."""
    agent = _user_agent_from_env_file()
    if not agent:
        pytest.skip("SEC_USER_AGENT is not configured")
    with SecClient(Settings(sec_user_agent=agent), cache=RawCache(tmp_path)) as client:
        payload = client.get_json(submissions_url(RY))
    assert payload["cik"] == "0001000275"
    assert payload["name"]
    assert any(tmp_path.rglob("*.json.zst"))


def _user_agent_from_env_file() -> str:
    for directory in (REPO_ROOT, *REPO_ROOT.parents):
        env_file = directory / ".env"
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("SEC_USER_AGENT="):
                return line.partition("=")[2].strip().strip("'\"")
    return ""
