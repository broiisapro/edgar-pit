"""Tests for the rate-limited, retrying SEC client (decisions A-2, A-3)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import httpx
import pytest
import respx

from edgarpit.config import ConfigError, Settings
from edgarpit.rawcache import RawCache
from edgarpit.sec_client import (
    OfflineMiss,
    SecAccessDenied,
    SecClient,
    SecClientError,
    SecNotFound,
    classify,
    company_tickers_url,
    companyfacts_url,
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


def test_get_json_rejects_non_object_payloads() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(RY_SUBMISSIONS).mock(return_value=httpx.Response(200, json=[1, 2, 3]))
        client = SecClient(make_settings())
        with pytest.raises(SecClientError, match="expected a JSON object"):
            client.get_json(RY_SUBMISSIONS)


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
