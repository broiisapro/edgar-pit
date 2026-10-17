"""End-to-end filings and events ingest over the fixture cache.

Every test here runs ``ingest_filings`` against ``tests/fixtures/sec`` through
:class:`edgarpit.rawcache.RawCache`; no test touches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from edgarpit.ingest.submissions import ingest_filings, load_item_labels
from edgarpit.rawcache import RawCache
from edgarpit.schemas import Events8kSchema, FilingsSchema

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
RAW = RawCache(FIXTURES)
LABELS = load_item_labels()

RY_CIK = "0001000275"
APPLE_CIK = "0000320193"
SHOP_CIK = "0001594805"
CIKS = (RY_CIK, APPLE_CIK, SHOP_CIK)

APPLE_8K = "0000320193-26-000018"
NEW_YORK = ZoneInfo("America/New_York")

# Counts of filings whose header acceptance time differs from the submissions
# API value by more than 60 s, measured on 2026-10-17: every RY and Apple
# fixture filing is shifted (by 4 h or 5 h depending on DST) while every
# Shopify value agrees, which is exactly why the SGML header is authoritative
# (decision A-1).
EXPECTED_DISAGREEMENTS = {RY_CIK: 191, APPLE_CIK: 76, SHOP_CIK: 0}

# The one fixture filing whose header acceptance time is later than its
# ``filingDate`` 23:59:59 New York: an SEC paper filing (``<PAPER>`` in the
# header) whose acceptance happened 2018-03-15 although EDGAR dates it
# 2018-03-08. For paper filings the ``filing_date_eod`` fallback is therefore
# not an upper bound; for every other fixture filing it is.
PAPER_ACCESSION = "9999999997-18-002767"


def fixture_row_count(cik: str) -> int:
    main = json.loads(RAW.latest("submissions", cik))
    arrays = [main["filings"]["recent"]]
    arrays += [
        json.loads(RAW.latest("submissions_page", cik, key))
        for key in RAW.keys("submissions_page", cik)
    ]
    return sum(len(rows["accessionNumber"]) for rows in arrays)


def test_ingest_filings_validates_and_keeps_every_fixture_row() -> None:
    for cik in CIKS:
        filings, events = ingest_filings(RAW, cik, LABELS)
        FilingsSchema.validate(filings)
        Events8kSchema.validate(events)
        assert len(filings) == fixture_row_count(cik)
        assert filings["acceptance_ts"].is_null().sum() == 0
        assert filings["acceptance_ts_source"].is_in(["sgml_header", "filing_date_eod"]).all()
        if cik != RY_CIK:
            assert events.height > 0
        else:
            assert events.height == 0  # RY has no 8-Ks in the fixtures


def test_ingest_filings_attaches_every_cached_header() -> None:
    for cik in CIKS:
        filings, _ = ingest_filings(RAW, cik, LABELS)
        header_accessions = set(RAW.keys("headers", cik))
        with_headers = filings.filter(pl.col("accession").is_in(sorted(header_accessions)))
        assert with_headers.height == filings.height
        assert with_headers["acceptance_ts_source"].eq("sgml_header").all()
        assert with_headers["ts_imputed"].sum() == 0


def test_ingest_filings_events_carry_labels() -> None:
    _, events = ingest_filings(RAW, APPLE_CIK, LABELS)
    apple = events.filter(pl.col("accession") == APPLE_8K)
    assert dict(zip(apple["item_code"], apple["item_label"], strict=True)) == {
        "2.02": LABELS["2.02"],
        "9.01": LABELS["9.01"],
    }


def test_header_vs_api_disagreements_are_counted_and_bounded() -> None:
    counts: dict[str, tuple[int, int]] = {}
    for cik in CIKS:
        filings, _ = ingest_filings(RAW, cik, LABELS)
        with_api = filings.filter(pl.col("acceptance_ts_api").is_not_null())
        delta = (with_api["acceptance_ts"] - with_api["acceptance_ts_api"]).abs()
        disagreements = int((delta > timedelta(seconds=60)).sum())
        counts[cik] = (disagreements, with_api.height)
        assert 0 <= disagreements <= with_api.height
        assert disagreements == EXPECTED_DISAGREEMENTS[cik]
    print("header-vs-API disagreements > 60 s (count, filings with API):", counts)


def test_header_acceptance_never_exceeds_the_filing_date_end_of_day() -> None:
    """The ``filing_date_eod`` fallback must never be earlier than the truth.

    Decision A-1 makes the fallback an upper bound because EDGAR dates an
    after-hours filing the next day. This checks the bound on every fixture
    filing that has a header; the only known exception is the paper filing
    named above.
    """
    violations: set[str] = set()
    for cik in CIKS:
        filings, _ = ingest_filings(RAW, cik, LABELS)
        header_accessions = set(RAW.keys("headers", cik))
        for row in filings.iter_rows(named=True):
            if row["accession"] not in header_accessions:
                continue
            bound = datetime.combine(row["filing_date"], time(23, 59, 59), tzinfo=NEW_YORK)
            if row["acceptance_ts"] > bound.astimezone(UTC):
                violations.add(row["accession"])
    assert violations == {PAPER_ACCESSION}


def test_missing_submissions_raises() -> None:
    with pytest.raises(ValueError, match="no cached submissions document"):
        ingest_filings(RAW, "9999999999", LABELS)


def test_missing_page_raises(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    cik = "0000000000"
    main = {
        "cik": cik,
        "filings": {
            "recent": _minimal_recent(),
            "files": [{"name": f"CIK{cik}-submissions-001.json"}],
        },
    }
    cache.write(
        "submissions",
        cik,
        "",
        json.dumps(main).encode("utf-8"),
        url=f"https://data.sec.gov/submissions/CIK{cik}.json",
        http_status=200,
    )
    with pytest.raises(ValueError, match="missing cached submissions page"):
        ingest_filings(cache, cik, LABELS)


def _minimal_recent() -> dict[str, list[Any]]:
    return {
        "accessionNumber": ["0000000000-24-000001"],
        "filingDate": ["2024-01-02"],
        "reportDate": [""],
        "acceptanceDateTime": ["2024-01-02T21:00:00.000Z"],
        "form": ["10-K"],
        "items": [""],
        "isXBRL": [1],
        "primaryDocument": ["doc.htm"],
    }
