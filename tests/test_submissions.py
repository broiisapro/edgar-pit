"""Submissions parsing: columnar arrays, pagination, dedupe and diagnostics.

Fixture tests read the real SEC responses under ``tests/fixtures/sec`` through
:class:`edgarpit.rawcache.RawCache`; synthetic documents cover the API edge
cases the fixtures do not contain. No test here touches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from edgarpit.ingest.headers import HeaderInfo, apply_acceptance, parse_header
from edgarpit.ingest.submissions import parse_submissions
from edgarpit.rawcache import RawCache
from edgarpit.schemas import FilingsSchema

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
CACHE = RawCache(FIXTURES)

RY_CIK = "0001000275"
APPLE_CIK = "0000320193"
SHOP_CIK = "0001594805"
CIKS = (RY_CIK, APPLE_CIK, SHOP_CIK)

APPLE_PAGE = "CIK0000320193-submissions-001.json"
APPLE_10K_AMENDMENT = "0001193125-10-012091"
APPLE_8K = "0000320193-26-000018"


def cached_text(endpoint: str, cik: str, key: str | None = None) -> str:
    body = CACHE.latest(endpoint, cik, key)
    assert body is not None, f"no cached {endpoint} for {cik}/{key}"
    return body.decode("utf-8")


def cached_json(endpoint: str, cik: str, key: str | None = None) -> dict[str, Any]:
    document = json.loads(cached_text(endpoint, cik, key))
    assert isinstance(document, dict)
    return document


def issuer_inputs(cik: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    main = cached_json("submissions", cik)
    pages = [
        cached_json("submissions_page", cik, key) for key in CACHE.keys("submissions_page", cik)
    ]
    return main, pages


def parse_issuer(cik: str) -> pl.DataFrame:
    main, pages = issuer_inputs(cik)
    return parse_submissions(main, pages)


def fixture_headers(cik: str) -> dict[str, HeaderInfo]:
    return {
        accession: parse_header(cached_text("headers", cik, accession))
        for accession in CACHE.keys("headers", cik)
    }


def arrays_of(document: dict[str, Any]) -> dict[str, list[Any]]:
    filings = document.get("filings")
    if isinstance(filings, dict) and "recent" in filings:
        return filings["recent"]
    return document


def fixture_row_count(cik: str) -> int:
    main, pages = issuer_inputs(cik)
    return sum(len(arrays_of(doc)["accessionNumber"]) for doc in (main, *pages))


def test_row_counts_match_the_fixture_arrays() -> None:
    for cik in CIKS:
        frame = parse_issuer(cik)
        assert len(frame) == fixture_row_count(cik)
        assert frame["cik"].to_list() == [cik] * len(frame)


def test_every_accession_is_unique() -> None:
    for cik in CIKS:
        frame = parse_issuer(cik)
        assert frame["accession"].is_unique().all()
        assert frame["accession"].str.contains(r"^\d{10}-\d{2}-\d{6}$").all()


def test_rows_from_pages_are_present_with_their_page_name() -> None:
    main, pages = issuer_inputs(RY_CIK)
    recent_accessions = set(arrays_of(main)["accessionNumber"])
    page_sources: dict[str, str] = {}
    for key, page in zip(CACHE.keys("submissions_page", RY_CIK), pages, strict=True):
        for accession in arrays_of(page)["accessionNumber"]:
            page_sources.setdefault(accession, f"{key}.json")
    page_only = {
        accession: source
        for accession, source in page_sources.items()
        if accession not in recent_accessions
    }
    assert page_only, "no RY accession is unique to a page in the fixtures"
    frame = parse_issuer(RY_CIK)
    sources = dict(zip(frame["accession"], frame["source"], strict=True))
    for accession, source in page_only.items():
        assert sources[accession] == source

    apple = parse_issuer(APPLE_CIK)
    sources = dict(zip(apple["accession"], apple["source"], strict=True))
    assert sources[APPLE_10K_AMENDMENT] == APPLE_PAGE
    assert sources[APPLE_10K_AMENDMENT] not in {"recent", ""}


def test_zero_row_pages_are_handled() -> None:
    empty_pages = [
        key
        for key in CACHE.keys("submissions_page", RY_CIK)
        if not arrays_of(cached_json("submissions_page", RY_CIK, key))["accessionNumber"]
    ]
    assert empty_pages, "the fixtures no longer exercise a zero-row page"
    frame = parse_issuer(RY_CIK)
    sources = set(frame["source"])
    assert all(f"{key}.json" not in sources for key in empty_pages)
    assert len(frame) == fixture_row_count(RY_CIK)


def test_acceptance_columns_are_left_for_apply_acceptance() -> None:
    frame = parse_issuer(APPLE_CIK)
    assert frame["acceptance_ts"].is_null().all()
    assert frame["acceptance_ts_source"].is_null().all()
    assert frame["ts_imputed"].is_null().all()
    assert frame["acceptance_ts_api"].is_null().sum() == 0


def test_frame_validates_under_filings_schema_after_apply_acceptance() -> None:
    for cik in CIKS:
        frame = apply_acceptance(parse_issuer(cik), fixture_headers(cik))
        FilingsSchema.validate(frame)
        assert frame["acceptance_ts_source"].is_in(["sgml_header", "filing_date_eod"]).all()


def test_apple_8k_api_timestamp_differs_from_the_header_time() -> None:
    """Documents the submissions-API shift that makes the SGML header authoritative."""
    frame = parse_issuer(APPLE_CIK)
    row = frame.filter(pl.col("accession") == APPLE_8K).row(0, named=True)
    assert row["acceptance_ts_api"] == datetime(2026, 7, 31, 0, 30, 28, tzinfo=UTC)
    header = fixture_headers(APPLE_CIK)[APPLE_8K]
    assert header.acceptance_ts == datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC)
    delta = row["acceptance_ts_api"] - header.acceptance_ts
    assert delta.total_seconds() == 4 * 3600


def test_nullable_fields_and_flags_from_fixtures() -> None:
    apple = parse_issuer(APPLE_CIK)
    amendment = apple.filter(pl.col("accession") == APPLE_10K_AMENDMENT).row(0, named=True)
    assert amendment["form"] == "10-K/A"
    assert amendment["is_amendment"] is True
    eight_ks = apple.filter(pl.col("form").is_in(["8-K", "8-K/A"]))
    assert eight_ks["items"].null_count() == 0
    assert eight_ks["is_amendment"].sum() == 1  # the single 8-K/A
    six_k = parse_issuer(RY_CIK).filter(pl.col("form") == "6-K")
    assert six_k["items"].null_count() == len(six_k)
    assert six_k["primary_document"].null_count() == 0
    assert apple["is_xbrl"].dtype == pl.Boolean


def test_duplicate_accession_keeps_the_recent_row() -> None:
    accession = "0000320193-24-000001"
    main = {
        "cik": 320193,
        "filings": {
            "recent": _document_arrays(accession=accession, form="10-K"),
            "files": [{"name": APPLE_PAGE}],
        },
    }
    page = _document_arrays(accession=accession, form="10-K/A")
    frame = parse_submissions(main, [page])
    assert frame["cik"].to_list() == ["0000320193"]
    assert frame["form"].to_list() == ["10-K"]
    assert frame["source"].to_list() == ["recent"]


def test_zero_row_page_without_files_is_allowed() -> None:
    main = {"cik": "0000000000", "filings": {"recent": _document_arrays(), "files": []}}
    frame = parse_submissions(main, [])
    assert len(frame) == 1
    assert frame["report_date"].to_list() == [None]
    assert frame["items"].to_list() == [None]


def test_api_acceptance_timestamp_is_parsed_literally() -> None:
    main = {
        "cik": "0000000000",
        "filings": {
            "recent": _document_arrays(acceptance="2026-07-31T00:30:28.000Z"),
            "files": [],
        },
    }
    frame = parse_submissions(main, [])
    assert frame["acceptance_ts_api"].to_list() == [datetime(2026, 7, 31, 0, 30, 28, tzinfo=UTC)]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda arrays: arrays.update(
            {"accessionNumber": [*arrays["accessionNumber"], "0000000000-24-000002"]}
        ),
        lambda arrays: arrays.update({"filingDate": [""]}),
        lambda arrays: arrays.update({"form": [""]}),
        lambda arrays: arrays.update({"accessionNumber": ["not-an-accession"]}),
    ],
)
def test_malformed_documents_raise_value_error(mutate: Any) -> None:
    arrays = _document_arrays()
    mutate(arrays)
    main = {"cik": "0000000000", "filings": {"recent": arrays, "files": []}}
    with pytest.raises(ValueError):
        parse_submissions(main, [])


def test_page_count_must_match_filings_files() -> None:
    main = {
        "cik": "0000000000",
        "filings": {"recent": _document_arrays(), "files": [{"name": APPLE_PAGE}]},
    }
    with pytest.raises(ValueError, match="page files"):
        parse_submissions(main, [])


def _document_arrays(
    accession: str = "0000000000-24-000001",
    form: str = "10-K",
    acceptance: str = "2024-01-02T21:00:00.000Z",
) -> dict[str, list[Any]]:
    """One-row columnar arrays in the submissions API's layout."""
    return {
        "accessionNumber": [accession],
        "filingDate": ["2024-01-02"],
        "reportDate": [""],
        "acceptanceDateTime": [acceptance],
        "act": ["34"],
        "form": [form],
        "fileNumber": ["001-00000"],
        "filmNumber": ["24000001"],
        "items": [""],
        "core_type": ["10-K"],
        "size": [1000],
        "isXBRL": [1],
        "isInlineXBRL": [1],
        "isXBRLNumeric": [1],
        "primaryDocument": ["doc.htm"],
        "primaryDocDescription": ["10-K"],
    }
