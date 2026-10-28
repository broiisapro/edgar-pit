"""Tests for fetching 8-K filing indexes and documents (decisions A-30, A-37).

Every HTTP response is served from the committed real 8-K fixtures under
``tests/fixtures/sec_text`` through respx, into a temporary raw cache, so these
tests never touch the network. ``discover.exhibit_documents`` lands with its own
issue and is replaced here by a double that reads the fixtures' own file list.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx
import polars as pl
import pytest
import respx

from edgarpit.config import Settings
from edgarpit.rawcache import RawCache, document_key
from edgarpit.schemas import duckdb_ddl
from edgarpit.sec_client import SecClient, classify, document_url, filing_index_url
from edgarpit.text import discover, fetch
from edgarpit.text.discover import DocumentRef

REPO_ROOT = Path(__file__).resolve().parents[1]
TEXT_ROOT = Path(__file__).resolve().parent / "fixtures" / "sec_text"
RAW = TEXT_ROOT / "raw"
EXPECTED_PATH = Path(__file__).resolve().parent / "fixtures" / "text_expected.json"

APPLE_CIK = "0000320193"
SHOP_CIK = "0001594805"
APPLE_Q4_2025 = "0000320193-25-000077"
APPLE_Q3_2026 = "0000320193-26-000018"
APPLE_AMENDMENT = "0001140361-26-035325"
APPLE_OTHER = "0001140361-24-040659"
SHOP_Q2_2026 = "0001594805-26-000046"

ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")

_EVENT_DTYPES: dict[str, pl.DataType] = {
    "cik": pl.String,
    "accession": pl.String,
    "form": pl.String,
    "acceptance_ts": pl.Datetime("us", "UTC"),
    "ts_imputed": pl.Boolean,
    "item_code": pl.String,
    "item_label": pl.String,
}

# The fixture 8-Ks and 8-K/As with the item codes their submissions rows carry,
# ordered here independently of the database so the expected order is explicit.
EVENTS = [
    (
        APPLE_CIK,
        APPLE_OTHER,
        "8-K",
        datetime(2024, 9, 10, 13, 6, 34, tzinfo=UTC),
        False,
        "7.01",
        "Regulation FD Disclosure",
    ),
    (
        APPLE_CIK,
        APPLE_Q4_2025,
        "8-K",
        datetime(2025, 10, 30, 20, 30, 35, tzinfo=UTC),
        False,
        "2.02",
        "Results of Operations and Financial Condition",
    ),
    (
        APPLE_CIK,
        APPLE_Q4_2025,
        "8-K",
        datetime(2025, 10, 30, 20, 30, 35, tzinfo=UTC),
        False,
        "9.01",
        "Financial Statements and Exhibits",
    ),
    (
        APPLE_CIK,
        APPLE_Q3_2026,
        "8-K",
        datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC),
        False,
        "2.02",
        "Results of Operations and Financial Condition",
    ),
    (
        APPLE_CIK,
        APPLE_Q3_2026,
        "8-K",
        datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC),
        False,
        "9.01",
        "Financial Statements and Exhibits",
    ),
    (
        APPLE_CIK,
        APPLE_AMENDMENT,
        "8-K/A",
        datetime(2026, 9, 1, 20, 30, 35, tzinfo=UTC),
        True,
        "5.02",
        "Departure of Directors or Certain Officers",
    ),
    (
        SHOP_CIK,
        SHOP_Q2_2026,
        "8-K",
        datetime(2026, 8, 5, 11, 1, 47, tzinfo=UTC),
        False,
        "2.02",
        "Results of Operations and Financial Condition",
    ),
]


class FakeClock:
    """Monotonic clock whose ``sleep`` advances it, so no test actually waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def manifest_rows() -> list[dict[str, object]]:
    """Return the fixture manifest rows (decision A-15 format)."""
    path = RAW / "manifest.jsonl"
    rows: list[dict[str, object]] = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert rows, f"{path} is empty"
    return rows


def stored_path(row: dict[str, object]) -> Path:
    """Return the on-disk path of a fixture manifest row."""
    return TEXT_ROOT / str(row["path"])


def index_rows() -> dict[tuple[str, str], Path]:
    """Map ``(cik, accession)`` to the stored ``-index-headers.html`` page."""
    pages: dict[tuple[str, str], Path] = {}
    for row in manifest_rows():
        if row["endpoint"] != "filing_index":
            continue
        parts = str(row["path"]).split("/")
        pages[(str(row["cik"]), parts[3])] = stored_path(row)
    return pages


def document_rows() -> dict[tuple[str, str, str], Path]:
    """Map ``(cik, accession, filename)`` to the stored document."""
    documents: dict[tuple[str, str, str], Path] = {}
    for row in manifest_rows():
        if row["endpoint"] != "documents":
            continue
        parts = str(row["path"]).split("/")
        accession, filename = parts[3].split("__", 1)
        documents[(str(row["cik"]), accession, filename)] = stored_path(row)
    return documents


def documents_of(cik: str, accession: str) -> list[tuple[str, Path]]:
    """Return the stored ``(filename, path)`` pairs of one fixture filing."""
    return sorted(
        (filename, path)
        for (row_cik, row_accession, filename), path in document_rows().items()
        if (row_cik, row_accession) == (cik, accession)
    )


def fixture_documents() -> dict[str, list[DocumentRef]]:
    """Return the fixture's own reference list per accession, primary first.

    The primary document is the stored document that is not an ``EX-99.1`` in
    ``tests/fixtures/text_expected.json``; the exhibits are its ``exhibit_99_1``
    entry. This is what the double for ``discover.exhibit_documents`` returns.
    """
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))["exhibit_99_1"]
    references: dict[str, list[DocumentRef]] = {}
    for (cik, accession), _path in index_rows().items():
        exhibits = list(expected.get(accession, []))
        primaries = [name for name, _path in documents_of(cik, accession) if name not in exhibits]
        assert len(primaries) == 1, (accession, exhibits)
        references[accession] = [
            DocumentRef(accession=accession, filename=primaries[0], doc_type="primary"),
            *(
                DocumentRef(accession=accession, filename=exhibit, doc_type="EX-99.1")
                for exhibit in exhibits
            ),
        ]
    return references


def fake_exhibit_documents(index_headers_html: str) -> list[DocumentRef]:
    """Double for the discovery function: the fixture pages' own file list."""
    match = ACCESSION_RE.search(index_headers_html)
    assert match is not None, "a fixture index page names no accession"
    return fixture_documents()[match.group(0)]


@pytest.fixture
def discover_double(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``discover.exhibit_documents`` with the fixture-backed double."""
    monkeypatch.setattr(discover, "exhibit_documents", fake_exhibit_documents)


def fixture_response(request: httpx.Request) -> httpx.Response:
    """Serve a request from the fixture tree, or 404 when it is not stored."""
    endpoint, cik, key = classify(str(request.url))
    directory = RAW / endpoint / (cik or "_")
    if key:
        directory = directory / key
    suffix = ".html" if endpoint == "filing_index" else Path(key).suffix
    paths = sorted(directory.glob(f"*{suffix}"))
    if not paths:
        return httpx.Response(404)
    return httpx.Response(200, content=paths[-1].read_bytes())


def events_db(path: Path, rows: Sequence[tuple[object, ...]] = EVENTS) -> Path:
    """Create a database holding ``raw.events_8k`` with ``rows``."""
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute(duckdb_ddl("events_8k"))
        frame = pl.DataFrame(list(rows), schema=_EVENT_DTYPES, orient="row")
        con.register("_events", frame)
        try:
            con.execute("INSERT INTO raw.events_8k SELECT * FROM _events")
        finally:
            con.unregister("_events")
    finally:
        con.close()
    return path


def make_client(tmp_path: Path) -> SecClient:
    """Return an online client writing into ``tmp_path`` without waiting."""
    clock = FakeClock()
    return SecClient(
        Settings(sec_user_agent="Test User test@example.com"),
        cache=RawCache(tmp_path),
        clock=clock.monotonic,
        sleep=clock.sleep,
    )


def expected_urls(pairs: Sequence[tuple[str, str]]) -> set[str]:
    """Return the index and document URLs of ``pairs`` from the fixture tree."""
    urls: set[str] = set()
    for cik, accession in pairs:
        urls.add(filing_index_url(cik, accession))
        for filename, _path in documents_of(cik, accession):
            urls.add(document_url(cik, accession, filename))
    return urls


def requested_urls(route: respx.Route) -> set[str]:
    """Return the URLs a respx route answered."""
    return {str(call.request.url) for call in route.calls}


def test_select_text_accessions_filters_by_item_and_orders_by_acceptance(
    tmp_path: Path,
) -> None:
    db = events_db(tmp_path / "edgarpit.duckdb")

    assert fetch.select_text_accessions(db, ["2.02"]) == [
        (APPLE_CIK, APPLE_Q4_2025),
        (APPLE_CIK, APPLE_Q3_2026),
        (SHOP_CIK, SHOP_Q2_2026),
    ]
    assert fetch.select_text_accessions(db, ["7.01"]) == [(APPLE_CIK, APPLE_OTHER)]
    assert fetch.select_text_accessions(db, ["5.02"]) == [(APPLE_CIK, APPLE_AMENDMENT)]
    assert fetch.select_text_accessions(db, ["2.02", "7.01"]) == [
        (APPLE_CIK, APPLE_OTHER),
        (APPLE_CIK, APPLE_Q4_2025),
        (APPLE_CIK, APPLE_Q3_2026),
        (SHOP_CIK, SHOP_Q2_2026),
    ]


def test_select_text_accessions_honours_limit_and_empty_selections(tmp_path: Path) -> None:
    db = events_db(tmp_path / "edgarpit.duckdb")

    assert fetch.select_text_accessions(db, ["2.02"], limit=2) == [
        (APPLE_CIK, APPLE_Q4_2025),
        (APPLE_CIK, APPLE_Q3_2026),
    ]
    assert fetch.select_text_accessions(db, ["2.02"], limit=0) == []
    assert fetch.select_text_accessions(db, []) == []
    assert fetch.select_text_accessions(db, [" ", ""]) == []


def test_select_text_accessions_rejects_a_negative_limit_and_a_missing_database(
    tmp_path: Path,
) -> None:
    db = events_db(tmp_path / "edgarpit.duckdb")

    with pytest.raises(ValueError, match="zero or positive"):
        fetch.select_text_accessions(db, ["2.02"], limit=-1)
    with pytest.raises(FileNotFoundError, match="no database"):
        fetch.select_text_accessions(tmp_path / "absent.duckdb", ["2.02"])


def test_select_text_accessions_needs_the_events_table(tmp_path: Path) -> None:
    db = tmp_path / "edgarpit.duckdb"
    con = duckdb.connect(str(db))
    try:
        con.execute("CREATE TABLE unrelated (x INTEGER)")
    finally:
        con.close()

    with pytest.raises(ValueError, match=r"raw\.events_8k"):
        fetch.select_text_accessions(db, ["2.02"])


def test_fetch_text_documents_stores_every_selected_index_and_document(
    tmp_path: Path, discover_double: None
) -> None:
    db = events_db(tmp_path / "edgarpit.duckdb")
    data = tmp_path / "data"
    client = make_client(data)
    pairs = [(APPLE_CIK, APPLE_Q4_2025), (APPLE_CIK, APPLE_Q3_2026), (SHOP_CIK, SHOP_Q2_2026)]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url__regex=r"https://www\.sec\.gov/Archives/edgar/data/.*").mock(
            side_effect=fixture_response
        )
        report = fetch.fetch_text_documents(db, data, ["2.02"], client=client)

    assert report.filings == 3
    assert report.indexes_fetched == 3
    assert report.documents_fetched == 6
    assert report.already_cached == 0
    assert report.failed == ()
    assert requested_urls(route) == expected_urls(pairs)
    assert route.call_count == 9

    cache = RawCache(data)
    for cik, accession in pairs:
        assert (
            cache.latest("filing_index", cik, accession)
            == index_rows()[(cik, accession)].read_bytes()
        )
        for filename, path in documents_of(cik, accession):
            assert (
                cache.latest("documents", cik, document_key(accession, filename))
                == path.read_bytes()
            )


def test_a_second_run_fetches_nothing(tmp_path: Path, discover_double: None) -> None:
    db = events_db(tmp_path / "edgarpit.duckdb")
    data = tmp_path / "data"
    client = make_client(data)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url__regex=r"https://www\.sec\.gov/Archives/edgar/data/.*").mock(
            side_effect=fixture_response
        )
        first = fetch.fetch_text_documents(db, data, ["2.02"], client=client)
        calls = route.call_count
        second = fetch.fetch_text_documents(db, data, ["2.02"], client=client)

    assert first.indexes_fetched == 3
    assert second.filings == 3
    assert second.indexes_fetched == 0
    assert second.documents_fetched == 0
    assert second.already_cached == 9
    assert second.failed == ()
    assert route.call_count == calls


def test_limit_fetches_only_the_first_filings(tmp_path: Path, discover_double: None) -> None:
    db = events_db(tmp_path / "edgarpit.duckdb")
    data = tmp_path / "data"
    client = make_client(data)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url__regex=r"https://www\.sec\.gov/Archives/edgar/data/.*").mock(
            side_effect=fixture_response
        )
        report = fetch.fetch_text_documents(db, data, ["2.02"], limit=1, client=client)

    assert report.filings == 1
    assert report.indexes_fetched == 1
    assert report.documents_fetched == 2
    assert requested_urls(route) == expected_urls([(APPLE_CIK, APPLE_Q4_2025)])


def test_an_index_404_is_recorded_and_the_run_continues(
    tmp_path: Path, discover_double: None
) -> None:
    db = events_db(tmp_path / "edgarpit.duckdb")
    data = tmp_path / "data"
    client = make_client(data)

    def handler(request: httpx.Request) -> httpx.Response:
        if APPLE_Q3_2026 in request.url.path:
            return httpx.Response(404)
        return fixture_response(request)

    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=r"https://www\.sec\.gov/Archives/edgar/data/.*").mock(
            side_effect=handler
        )
        report = fetch.fetch_text_documents(db, data, ["2.02"], client=client)

    assert report.filings == 3
    assert report.failed == (APPLE_Q3_2026,)
    assert report.indexes_fetched == 2
    assert report.documents_fetched == 4
    cache = RawCache(data)
    assert cache.latest("filing_index", APPLE_CIK, APPLE_Q3_2026) is None
    assert cache.latest("filing_index", SHOP_CIK, SHOP_Q2_2026) is not None


def test_a_document_404_is_recorded_and_the_filing_continues(
    tmp_path: Path, discover_double: None
) -> None:
    db = events_db(tmp_path / "edgarpit.duckdb")
    data = tmp_path / "data"
    client = make_client(data)
    missing = document_key(APPLE_Q3_2026, "a8-kex991q3202606272026.htm")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/a8-kex991q3202606272026.htm"):
            return httpx.Response(404)
        return fixture_response(request)

    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=r"https://www\.sec\.gov/Archives/edgar/data/.*").mock(
            side_effect=handler
        )
        report = fetch.fetch_text_documents(db, data, ["2.02"], client=client)

    assert report.filings == 3
    assert report.failed == (missing,)
    assert report.indexes_fetched == 3
    assert report.documents_fetched == 5
    assert RawCache(data).latest("documents", APPLE_CIK, missing) is None


def test_an_empty_selection_returns_without_building_a_client(tmp_path: Path) -> None:
    """With nothing to fetch the run needs neither a User-Agent nor a cache."""
    db = events_db(tmp_path / "edgarpit.duckdb")

    report = fetch.fetch_text_documents(db, tmp_path / "data", ["99.99"])

    assert report == fetch.TextFetchReport(0, 0, 0, 0, ())


def test_a_client_without_a_cache_is_refused(tmp_path: Path, discover_double: None) -> None:
    db = events_db(tmp_path / "edgarpit.duckdb")
    client = SecClient(Settings(sec_user_agent="Test User test@example.com"))

    with pytest.raises(ValueError, match="no raw cache"):
        fetch.fetch_text_documents(db, tmp_path / "data", ["2.02"], client=client)


def test_a_client_is_built_from_the_settings_when_none_is_given(
    tmp_path: Path, discover_double: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an injected client the run uses ``SEC_USER_AGENT`` and ``data_dir``."""
    db = events_db(tmp_path / "edgarpit.duckdb")
    data = tmp_path / "data"
    monkeypatch.setenv("SEC_USER_AGENT", "Test User test@example.com")
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__regex=r"https://www\.sec\.gov/Archives/edgar/data/.*").mock(
            side_effect=fixture_response
        )
        report = fetch.fetch_text_documents(db, data, ["2.02"], limit=1)

    assert report.indexes_fetched == 1
    assert report.documents_fetched == 2
    assert report.failed == ()
    cache = RawCache(data)
    assert cache.latest("filing_index", APPLE_CIK, APPLE_Q4_2025) is not None


def test_the_report_is_frozen_and_carries_the_five_counts() -> None:
    report = fetch.TextFetchReport(
        filings=1, indexes_fetched=1, documents_fetched=2, already_cached=3, failed=("x",)
    )

    assert [field.name for field in dataclasses.fields(fetch.TextFetchReport)] == [
        "filings",
        "indexes_fetched",
        "documents_fetched",
        "already_cached",
        "failed",
    ]
    assert report.failed == ("x",)
    with pytest.raises(AttributeError):
        report.filings = 2  # type: ignore[misc]  # frozen dataclass refuses assignment
