"""Incremental fetch: one submissions probe per issuer (decision A-34).

Every HTTP response is served from the committed fixture tree through respx, so
nothing here touches the network. The first run in each integration test is a
normal full fetch that warms the cache; the incremental run then has to prove,
request by request, what it did and did not fetch. :class:`Recorder` keeps the
exact URL list, so "a skipped issuer costs exactly one request" is asserted
against the URLs issued, not only against the report's counters.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from edgarpit.config import Settings
from edgarpit.fetch import fetch_universe, submissions_unchanged
from edgarpit.rawcache import RawCache
from edgarpit.sec_client import SecClient, classify, submissions_url
from edgarpit.universe import Issuer, load_universe

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "sec"
RAW = FIXTURES / "raw"
RY_CIK = "0001000275"
APPLE_CIK = "0000320193"
SEC_URLS = r"https://(data|www)\.sec\.gov/.*"


def fixture_body(url: str) -> bytes | None:
    """Return the committed fixture bytes for ``url``, or ``None`` when not stored."""
    endpoint, cik, key = classify(url)
    directory = RAW / endpoint / (cik or "_")
    if key:
        directory = directory / key
    suffix = ".hdr.sgml" if endpoint == "headers" else ".json"
    paths = sorted(directory.glob(f"*{suffix}"))
    if not paths:
        return None
    return paths[-1].read_bytes()


class Recorder:
    """Serve fixture (or overridden) bytes and record every URL requested."""

    def __init__(self, overrides: Mapping[str, bytes] | None = None) -> None:
        self.calls: list[str] = []
        self.overrides: dict[str, bytes] = dict(overrides or {})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        body = self.overrides.get(url)
        if body is None:
            body = fixture_body(url)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)


def build_client(root: Path) -> SecClient:
    """A client writing into ``root`` with a frozen clock and a no-op sleep."""
    return SecClient(
        Settings(sec_user_agent="Test User test@example.com"),
        cache=RawCache(root),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )


def fixture_issuers(tickers: Sequence[str] = ("RY", "AAPL", "SHOP")) -> list[Issuer]:
    universe = {
        issuer.ticker: issuer for issuer in load_universe(REPO_ROOT / "config" / "universe.csv")
    }
    return [universe[ticker] for ticker in tickers]


def page_count(cik: str) -> int:
    return len(list((RAW / "submissions_page" / cik).glob("*/*.json")))


def stored_files(root: Path) -> set[str]:
    """Every raw-cache file except the manifest."""
    return {
        path.relative_to(root).as_posix()
        for path in (root / "raw").rglob("*")
        if path.is_file() and path.name != "manifest.jsonl"
    }


def submissions(
    rows: Sequence[tuple[str, str | None]],
    *,
    forms: Sequence[str] | None = None,
    files: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """A minimal submissions document; ``rows`` are ``(accession, acceptance)``."""
    return {
        "filings": {
            "recent": {
                "accessionNumber": [accession for accession, _ in rows],
                "acceptanceDateTime": [accepted for _, accepted in rows],
                "form": list(forms) if forms is not None else ["8-K"] * len(rows),
            },
            "files": [dict(page) for page in files],
        }
    }


def page(name: str, filing_count: int) -> dict[str, Any]:
    return {"name": name, "filingCount": filing_count, "filingFrom": "2020-01-01"}


def with_new_filing(body: bytes | None, accession: str) -> bytes:
    """Return ``body`` with one extra Form 4 at the top of ``filings.recent``.

    Every column grows by one value, like a real submissions document; the form
    needs no SGML header, so the changed issuer's full path stays fixture-served.
    """
    assert body is not None
    document = json.loads(body)
    recent = document["filings"]["recent"]
    template = {name: values[0] for name, values in recent.items()}
    template.update(
        accessionNumber=accession,
        form="4",
        filingDate="2026-10-27",
        acceptanceDateTime="2026-10-27T15:00:00.000Z",
        reportDate="2026-10-27",
        items="",
        primaryDocument="",
        isXBRL=0,
        isInlineXBRL=0,
        isXBRLNumeric=0,
    )
    for name, values in recent.items():
        values.insert(0, template[name])
    return json.dumps(document).encode()


# --- submissions_unchanged: the skip decision itself -----------------------


def test_a_missing_cache_is_a_change() -> None:
    fresh = submissions([("0001000275-24-000001", "2024-01-02T12:00:00.000Z")])
    assert submissions_unchanged(None, fresh) is False


def test_identical_documents_are_unchanged() -> None:
    document = submissions([("0001000275-24-000001", "2024-01-02T12:00:00.000Z")])
    assert submissions_unchanged(document, document) is True


def test_two_empty_documents_are_unchanged() -> None:
    """An issuer that has never filed anything must still be skippable."""
    assert submissions_unchanged(submissions([]), submissions([])) is True


def test_an_empty_document_differs_from_a_populated_one() -> None:
    fresh = submissions([("0001000275-24-000001", "2024-01-02T12:00:00.000Z")])
    assert submissions_unchanged(submissions([]), fresh) is False
    assert submissions_unchanged(fresh, submissions([])) is False


def test_a_new_filing_is_a_change() -> None:
    cached = submissions([("0001000275-24-000001", "2024-01-02T12:00:00.000Z")])
    fresh = submissions(
        [
            ("0001000275-24-000002", "2024-02-01T12:00:00.000Z"),
            ("0001000275-24-000001", "2024-01-02T12:00:00.000Z"),
        ]
    )
    assert submissions_unchanged(cached, fresh) is False


def test_an_amended_filing_is_a_change() -> None:
    """An 8-K/A is a new accession, so the newest accession and the count move."""
    cached = submissions([("0001000275-24-000001", "2024-01-02T12:00:00.000Z")], forms=["8-K"])
    fresh = submissions(
        [
            ("0001000275-24-000002", "2024-01-03T12:00:00.000Z"),
            ("0001000275-24-000001", "2024-01-02T12:00:00.000Z"),
        ],
        forms=["8-K/A", "8-K"],
    )
    assert submissions_unchanged(cached, fresh) is False


def test_a_reordered_recent_list_is_unchanged() -> None:
    """Order alone is not data: the newest accession is decided by acceptance."""
    older = ("0001000275-24-000001", "2024-01-02T12:00:00.000Z")
    newer = ("0001000275-24-000002", "2024-02-02T12:00:00.000Z")
    cached = submissions([older, newer])
    fresh = submissions([newer, older])
    assert submissions_unchanged(cached, fresh) is True


def test_a_new_page_is_a_change_before_its_rows_arrive() -> None:
    """A page is announced in ``filings.files`` in the same response that adds it."""
    recent = [("0001000275-24-000001", "2024-01-02T12:00:00.000Z")]
    cached = submissions(recent, files=[page("CIK0001000275-submissions-001.json", 2000)])
    fresh = submissions(
        recent,
        files=[
            page("CIK0001000275-submissions-001.json", 2000),
            page("CIK0001000275-submissions-002.json", 1),
        ],
    )
    assert submissions_unchanged(cached, fresh) is False


def test_a_growing_page_is_a_change() -> None:
    recent = [("0001000275-24-000001", "2024-01-02T12:00:00.000Z")]
    cached = submissions(recent, files=[page("CIK0001000275-submissions-001.json", 2000)])
    fresh = submissions(recent, files=[page("CIK0001000275-submissions-001.json", 2001)])
    assert submissions_unchanged(cached, fresh) is False


def test_the_total_count_not_the_recent_length_decides() -> None:
    """A row rolling from ``recent`` into a page is not a new filing."""
    newest = ("0001000275-24-000003", "2024-03-02T12:00:00.000Z")
    second = ("0001000275-24-000002", "2024-02-02T12:00:00.000Z")
    cached = submissions([newest, second], files=[page("CIK0001000275-submissions-001.json", 3)])
    fresh = submissions([newest], files=[page("CIK0001000275-submissions-001.json", 4)])
    assert submissions_unchanged(cached, fresh) is True


def test_rows_without_an_acceptance_time_fall_back_to_recent_order() -> None:
    cached = submissions([("0001000275-24-000001", None), ("0001000275-24-000002", None)])
    fresh = submissions([("0001000275-24-000003", None), ("0001000275-24-000002", None)])
    assert submissions_unchanged(cached, fresh) is False
    assert submissions_unchanged(cached, cached) is True


def test_an_unparseable_acceptance_time_is_treated_as_missing() -> None:
    cached = submissions([("0001000275-24-000001", "not-a-date")])
    fresh = submissions([("0001000275-24-000001", None)])
    assert submissions_unchanged(cached, fresh) is True


def test_a_missing_filings_object_in_the_fresh_document_is_rejected() -> None:
    with pytest.raises(ValueError, match="no 'filings' object"):
        submissions_unchanged(None, {"cik": 1000275})


def test_a_missing_recent_object_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"no 'filings\.recent' object"):
        submissions_unchanged(None, {"filings": {"files": []}})


def test_an_unreadable_fresh_document_is_fetched_in_full() -> None:
    """A row without an accession or a page without a count cannot prove sameness."""
    cached = submissions([("0001000275-24-000001", "2024-01-02T12:00:00.000Z")])
    no_accession = {
        "filings": {
            "recent": {"acceptanceDateTime": ["2024-01-02T12:00:00.000Z"], "form": ["8-K"]},
            "files": [],
        }
    }
    no_count = submissions(
        [("0001000275-24-000001", "2024-01-02T12:00:00.000Z")],
        files=[{"name": "CIK0001000275-submissions-001.json"}],
    )
    assert submissions_unchanged(cached, no_accession) is False
    assert submissions_unchanged(cached, no_count) is False


def test_an_unreadable_cached_document_counts_as_a_change() -> None:
    """A corrupted predecessor must not raise and must not skip the issuer."""
    fresh = submissions([("0001000275-24-000001", "2024-01-02T12:00:00.000Z")])
    assert submissions_unchanged({"cik": 1000275}, fresh) is False
    assert submissions_unchanged({"filings": {"recent": "not-a-list"}}, fresh) is False


# --- fetch_universe(incremental=True) over the fixture universe -------------


def test_an_unchanged_universe_costs_one_request_per_issuer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    issuers = fixture_issuers()
    client = build_client(tmp_path)
    recorder = Recorder()
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=SEC_URLS).mock(side_effect=recorder)
        first = fetch_universe(issuers, tmp_path, client=client)
        assert first.ok
        assert first.skipped_unchanged == 0
        before = stored_files(tmp_path)
        recorder.calls.clear()
        with caplog.at_level(logging.INFO, logger="edgarpit.fetch"):
            second = fetch_universe(issuers, tmp_path, client=client, incremental=True)

    assert second.ok
    assert second.skipped_unchanged == 3
    assert second.requests == 3
    assert recorder.calls == [submissions_url(issuer.cik or "") for issuer in issuers]
    for row in second.issuers:
        assert row.unchanged
        assert (row.submissions, row.pages, row.companyfacts) == (1, 0, 0)
        assert row.requests == 1
        assert (row.headers_fetched, row.headers_cached) == (0, 0)
    messages = [record.getMessage() for record in caplog.records]
    assert "incremental fetch: skipped 3 of 3 issuers unchanged" in messages

    new_files = stored_files(tmp_path) - before
    assert len(new_files) == 3
    assert all(name.startswith("raw/submissions/") for name in new_files)
    cache = RawCache(tmp_path)
    for issuer in issuers:
        cik = issuer.cik or ""
        assert cache.latest("submissions", cik) == fixture_body(submissions_url(cik))


def test_one_changed_issuer_is_fetched_fully_while_the_others_are_skipped(tmp_path: Path) -> None:
    issuers = fixture_issuers()
    client = build_client(tmp_path)
    recorder = Recorder()
    new_accession = "0001000275-26-000001"
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=SEC_URLS).mock(side_effect=recorder)
        first = fetch_universe(issuers, tmp_path, client=client)
        assert first.ok
        recorder.overrides[submissions_url(RY_CIK)] = with_new_filing(
            fixture_body(submissions_url(RY_CIK)), new_accession
        )
        recorder.calls.clear()
        second = fetch_universe(issuers, tmp_path, client=client, incremental=True)

    assert second.ok
    assert second.skipped_unchanged == 2
    ryan, apple, shop = second.issuers
    assert not ryan.unchanged
    assert (ryan.submissions, ryan.pages, ryan.companyfacts) == (1, page_count(RY_CIK), 1)
    assert ryan.requests == 2 + page_count(RY_CIK)
    assert (ryan.headers_fetched, ryan.headers_cached) == (0, first.issuers[0].headers_fetched)
    assert apple.unchanged and shop.unchanged
    assert (apple.requests, shop.requests) == (1, 1)
    stored = json.loads(RawCache(tmp_path).latest("submissions", RY_CIK) or b"{}")
    assert stored["filings"]["recent"]["accessionNumber"][0] == new_accession


def test_a_missing_cache_behaves_like_a_full_fetch(tmp_path: Path) -> None:
    issuers = fixture_issuers(("RY", "AAPL"))
    recorder = Recorder()
    incremental_dir = tmp_path / "incremental"
    full_dir = tmp_path / "full"
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=SEC_URLS).mock(side_effect=recorder)
        incremental = fetch_universe(
            issuers, incremental_dir, client=build_client(incremental_dir), incremental=True
        )
        full = fetch_universe(issuers, full_dir, client=build_client(full_dir))

    assert incremental.ok
    assert incremental.skipped_unchanged == 0
    for incremental_row, full_row in zip(incremental.issuers, full.issuers, strict=True):
        assert not incremental_row.unchanged
        assert incremental_row.submissions == full_row.submissions == 1
        assert incremental_row.pages == full_row.pages == page_count(incremental_row.cik)
        assert incremental_row.companyfacts == full_row.companyfacts == 1
        assert incremental_row.headers_fetched == full_row.headers_fetched
        assert incremental_row.requests == full_row.requests
    assert full.skipped_unchanged == 0


def test_refresh_false_with_incremental_probes_and_reuses_documents(tmp_path: Path) -> None:
    """``refresh`` still decides what a changed issuer re-fetches."""
    issuers = fixture_issuers(("RY", "AAPL"))
    client = build_client(tmp_path)
    recorder = Recorder()
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=SEC_URLS).mock(side_effect=recorder)
        assert fetch_universe(issuers, tmp_path, client=client).ok
        recorder.overrides[submissions_url(RY_CIK)] = with_new_filing(
            fixture_body(submissions_url(RY_CIK)), "0001000275-26-000002"
        )
        recorder.calls.clear()
        report = fetch_universe(issuers, tmp_path, client=client, refresh=False, incremental=True)

    assert report.ok
    assert report.skipped_unchanged == 1
    ryan, apple = report.issuers
    assert not ryan.unchanged
    assert (ryan.submissions, ryan.pages, ryan.companyfacts) == (1, 0, 0)
    assert ryan.requests == 1
    assert apple.unchanged
    assert len(recorder.calls) == 2


def test_an_offline_client_skips_unchanged_issuers_without_writing(tmp_path: Path) -> None:
    """The offline reader needs no User-Agent, no transport and writes nothing."""
    issuers = fixture_issuers(("RY", "AAPL"))
    recorder = Recorder()
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=SEC_URLS).mock(side_effect=recorder)
        assert fetch_universe(issuers, tmp_path, client=build_client(tmp_path)).ok
    before = stored_files(tmp_path)
    offline = SecClient(Settings(sec_user_agent=""), offline_dir=FIXTURES)

    report = fetch_universe(issuers, tmp_path, client=offline, incremental=True)

    assert report.ok
    assert report.skipped_unchanged == 2
    assert all(row.unchanged for row in report.issuers)
    assert report.requests == 2
    assert stored_files(tmp_path) == before


@pytest.mark.parametrize("body", [b"not json", b'{"cik": 320193}'])
def test_an_unreadable_cached_submissions_copy_is_replaced_by_a_full_fetch(
    tmp_path: Path, body: bytes
) -> None:
    issuers = fixture_issuers(("AAPL",))
    RawCache(tmp_path).write(
        "submissions",
        APPLE_CIK,
        "",
        body,
        url=submissions_url(APPLE_CIK),
        http_status=200,
    )
    recorder = Recorder()
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=SEC_URLS).mock(side_effect=recorder)
        report = fetch_universe(issuers, tmp_path, client=build_client(tmp_path), incremental=True)

    assert report.ok
    assert report.skipped_unchanged == 0
    row = report.issuers[0]
    assert not row.unchanged
    assert row.companyfacts == 1
    assert RawCache(tmp_path).latest("submissions", APPLE_CIK) == fixture_body(
        submissions_url(APPLE_CIK)
    )


def test_a_failed_probe_fails_only_that_issuer(tmp_path: Path) -> None:
    issuers = fixture_issuers(("RY", "AAPL"))
    client = build_client(tmp_path)
    recorder = Recorder()
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=SEC_URLS).mock(side_effect=recorder)
        assert fetch_universe(issuers, tmp_path, client=client).ok

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == submissions_url(RY_CIK):
            return httpx.Response(404)
        return recorder(request)

    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=SEC_URLS).mock(side_effect=handler)
        report = fetch_universe(issuers, tmp_path, client=client, incremental=True)

    assert not report.ok
    ryan, apple = report.issuers
    assert ryan.errors and "404" in ryan.errors[0]
    assert not ryan.unchanged
    assert (ryan.pages, ryan.companyfacts) == (0, 0)
    assert apple.unchanged
    assert report.skipped_unchanged == 1
