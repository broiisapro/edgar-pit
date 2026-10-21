"""End-to-end filings and events ingest over the fixture cache.

Every test here runs ``ingest_filings`` against ``tests/fixtures/sec`` through
:class:`edgarpit.rawcache.RawCache`; no test touches the network.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from edgarpit.ingest.headers import parse_header
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

# Byte-for-byte copy of the cached EDGAR header of JPMorgan Chase's 2000-01-21
# 8-K (accession 0000019617-00-000005): a real pre-2001 header with no
# <ACCEPTANCE-DATETIME> (decision A-26). It is embedded rather than read from a
# live cache so the test runs in CI.
CHASE_CIK = "0000019617"
CHASE_8K = "0000019617-00-000005"
CHASE_8K_DATE = date(2000, 1, 21)
CHASE_HEADER = """<SEC-HEADER>0000019617-00-000005.hdr.sgml : 20000202
<ACCESSION-NUMBER>0000019617-00-000005
<TYPE>8-K
<PUBLIC-DOCUMENT-COUNT>2
<PERIOD>19991231
<ITEMS>5
<ITEMS>7
<FILING-DATE>20000121
<FILER>
<COMPANY-DATA>
<CONFORMED-NAME>CHASE MANHATTAN CORP /DE/
<CIK>0000019617
<ASSIGNED-SIC>6021
<IRS-NUMBER>132624428
<STATE-OF-INCORPORATION>DE
<FISCAL-YEAR-END>1231
</COMPANY-DATA>
<FILING-VALUES>
<FORM-TYPE>8-K
<ACT>34
<FILE-NUMBER>001-05805
<FILM-NUMBER>511045
</FILING-VALUES>
<BUSINESS-ADDRESS>
<STREET1>270 PARK AVE
<CITY>NEW YORK
<STATE>NY
<ZIP>10017
<PHONE>2122706000
</BUSINESS-ADDRESS>
<MAIL-ADDRESS>
<STREET1>270 PARK AVENUE
<CITY>NEW YORK
<STATE>NY
<ZIP>10017
</MAIL-ADDRESS>
<FORMER-COMPANY>
<FORMER-CONFORMED-NAME>CHEMICAL BANKING CORP
<DATE-CHANGED>19920703
</FORMER-COMPANY>
<FORMER-COMPANY>
<FORMER-CONFORMED-NAME>CHEMICAL NEW YORK CORP
<DATE-CHANGED>19880508
</FORMER-COMPANY>
</FILER>
</SEC-HEADER>
"""


def write_legacy_cache(
    tmp_path: Path,
    *,
    header: str = CHASE_HEADER,
    accession: str = CHASE_8K,
    with_submissions: bool = True,
) -> RawCache:
    """A cache holding Chase's 8-K (and its header) in the fetched layout.

    ``header`` is the stored header body, ``accession`` the directory key and
    ``with_submissions`` false writes an empty submissions document so the
    header becomes a header-only row (decision A-24).
    """
    cache = RawCache(tmp_path)
    recent: dict[str, list[Any]] = {
        "accessionNumber": [accession] if with_submissions else [],
        "filingDate": ["2000-01-21"] if with_submissions else [],
        "reportDate": ["1999-12-31"] if with_submissions else [],
        "acceptanceDateTime": [""] if with_submissions else [],
        "form": ["8-K"] if with_submissions else [],
        "items": [""] if with_submissions else [],
        "isXBRL": [0] if with_submissions else [],
        "primaryDocument": ["chase8k.htm"] if with_submissions else [],
    }
    document = {"cik": CHASE_CIK, "filings": {"recent": recent, "files": []}}
    url = f"https://data.sec.gov/submissions/CIK{CHASE_CIK}.json"
    cache.write(
        "submissions", CHASE_CIK, "", json.dumps(document).encode("utf-8"), url=url, http_status=200
    )
    cache.write(
        "headers",
        CHASE_CIK,
        accession,
        header.encode("utf-8"),
        url=f"https://www.sec.gov/Archives/edgar/data/19617/{accession.replace('-', '')}/"
        f"{accession}.hdr.sgml",
        http_status=200,
    )
    return cache


# Counts of filings whose header acceptance time differs from the submissions
# API value by more than 60 s, measured on 2026-10-17: every RY and Apple
# fixture filing is shifted (by 4 h or 5 h depending on DST) while every
# Shopify value agrees, which is exactly why the SGML header is authoritative
# (decision A-1).
EXPECTED_DISAGREEMENTS = {RY_CIK: 191, APPLE_CIK: 76, SHOP_CIK: 0}

# The one fixture filing whose header acceptance time is later than its
# ``filingDate`` 23:59:59 New York: an SEC paper filing (``<PAPER>`` in the
# header) whose acceptance happened 2018-03-15 although EDGAR dates it
# 2018-03-08. Its true acceptance is 2018-03-15 13:44:25 New York.
PAPER_ACCESSION = "9999999997-18-002767"
PAPER_ACCEPTED = datetime(2018, 3, 15, 17, 44, 25, tzinfo=UTC)

# RY's FY2023 40-F, the first version of the reference restatement, lives in a
# submissions page and has a cached header; removing its API row turns it into
# a header-only accession (decision A-24).
HEADER_ONLY_ACCESSION = "0001193125-23-285639"
HEADER_ONLY_PAGE = "CIK0001000275-submissions-005"


def fixture_row_count(cik: str) -> int:
    main = json.loads(RAW.latest("submissions", cik))
    arrays = [main["filings"]["recent"]]
    arrays += [
        json.loads(RAW.latest("submissions_page", cik, key))
        for key in RAW.keys("submissions_page", cik)
    ]
    return sum(len(rows["accessionNumber"]) for rows in arrays)


def copy_cik_cache(tmp_path: Path, cik: str = RY_CIK) -> Path:
    """Copy one issuer's fixture cache endpoints into ``tmp_path`` for edits."""
    root = tmp_path / "cache"
    for endpoint in ("submissions", "submissions_page", "companyfacts", "headers"):
        shutil.copytree(FIXTURES / "raw" / endpoint / cik, root / "raw" / endpoint / cik)
    return root


def remove_accession(path: Path, accession: str) -> None:
    """Delete one filing's row from every parallel array of a submissions document."""
    document = json.loads(path.read_text(encoding="utf-8"))
    filings = document.get("filings")
    arrays: dict[str, Any] = (
        filings["recent"] if isinstance(filings, dict) and "recent" in filings else document
    )
    accessions = arrays["accessionNumber"]
    count = len(accessions)
    index = accessions.index(accession)
    for values in arrays.values():
        if isinstance(values, list) and len(values) == count:
            del values[index]
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")


def fallback_bound(filing_date: date, api: datetime | None) -> datetime:
    """The A-23 fallback: greatest(filing_date 23:59:59 New York, api + 6 h)."""
    end_of_day = datetime.combine(filing_date, time(23, 59, 59), tzinfo=NEW_YORK).astimezone(UTC)
    if api is None:
        return end_of_day
    return max(end_of_day, api + timedelta(hours=6))


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


def test_header_acceptance_never_exceeds_the_fallback() -> None:
    """The A-23 fallback must never be earlier than the truth.

    The fallback is ``greatest(filing_date 23:59:59 New York,
    acceptance_ts_api + 6 h)``, so on every fixture filing that has a header
    the header's acceptance time must be at or before it -- with no exceptions.
    """
    violations: set[str] = set()
    for cik in CIKS:
        filings, _ = ingest_filings(RAW, cik, LABELS)
        header_accessions = set(RAW.keys("headers", cik))
        for row in filings.iter_rows(named=True):
            if row["accession"] not in header_accessions:
                continue
            bound = fallback_bound(row["filing_date"], row["acceptance_ts_api"])
            if row["acceptance_ts"] > bound:
                violations.add(row["accession"])
    assert violations == set()


def test_paper_filing_fallback_covers_its_true_acceptance(tmp_path: Path) -> None:
    """A-23 on a temp copy of the cache with the paper filing's header removed.

    The filing-date bound alone (2018-03-09 04:59:59Z) is seven days before the
    true acceptance (2018-03-15 17:44:25Z); the API value is four hours late,
    so the fallback must take the ``acceptance_ts_api + 6 h`` term.
    """
    root = copy_cik_cache(tmp_path)
    shutil.rmtree(root / "raw" / "headers" / RY_CIK / PAPER_ACCESSION)
    filings, _ = ingest_filings(RawCache(root), RY_CIK, LABELS)
    row = filings.filter(pl.col("accession") == PAPER_ACCESSION).row(0, named=True)
    assert row["acceptance_ts"] >= PAPER_ACCEPTED
    assert row["acceptance_ts"] == datetime(2018, 3, 16, 3, 44, 25, tzinfo=UTC)
    assert row["acceptance_ts_source"] == "filing_date_eod"
    assert row["ts_imputed"] is True


def test_header_only_accession_becomes_a_filings_row(tmp_path: Path) -> None:
    """A-24: a cached header with no submissions row still yields a filings row.

    The row is built entirely from the header; without decision A-24 the
    accession would be absent from ``raw.filings`` and dbt would impute its
    facts from ``filed``.
    """
    root = copy_cik_cache(tmp_path)
    page = next((root / "raw" / "submissions_page" / RY_CIK / HEADER_ONLY_PAGE).glob("*.json"))
    remove_accession(page, HEADER_ONLY_ACCESSION)
    filings, _ = ingest_filings(RawCache(root), RY_CIK, LABELS)
    FilingsSchema.validate(filings)
    rows = filings.filter(pl.col("accession") == HEADER_ONLY_ACCESSION)
    assert rows.height == 1
    row = rows.row(0, named=True)
    header = parse_header(RAW.latest("headers", RY_CIK, HEADER_ONLY_ACCESSION).decode("utf-8"))
    assert row["cik"] == RY_CIK
    assert row["form"] == header.form == "40-F"
    assert row["filing_date"] == header.filing_date
    assert row["report_date"] == header.period
    assert row["acceptance_ts"] == header.acceptance_ts
    assert row["acceptance_ts_source"] == "sgml_header"
    assert row["acceptance_ts_api"] is None
    assert row["ts_imputed"] is False
    assert row["primary_document"] is None
    assert row["items"] == header.items
    assert row["is_amendment"] is False
    assert row["is_xbrl"] is False
    assert row["source"] == "header"
    # The removed submissions row reappears as the header-only row.
    assert len(filings) == fixture_row_count(RY_CIK)


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


def test_header_without_acceptance_falls_back_and_keeps_its_items(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A-26 on the cached Chase header: a fallback row, not an error.

    The header has no ``<ACCEPTANCE-DATETIME>`` and the submissions row has no
    ``items``, so the fallback must be the filing-date end of day and the
    header's ``<ITEMS>5``/``<ITEMS>7`` must still produce two 8-K events.
    """
    cache = write_legacy_cache(tmp_path)
    with caplog.at_level(logging.WARNING, logger="edgarpit.ingest.submissions"):
        filings, events = ingest_filings(cache, CHASE_CIK, LABELS)
    FilingsSchema.validate(filings)
    Events8kSchema.validate(events)
    row = filings.row(0, named=True)
    assert row["accession"] == CHASE_8K
    assert row["acceptance_ts"] == fallback_bound(CHASE_8K_DATE, None)
    assert row["acceptance_ts"] == datetime(2000, 1, 22, 4, 59, 59, tzinfo=UTC)
    assert row["acceptance_ts_source"] == "filing_date_eod"
    assert row["ts_imputed"] is True
    assert row["items"] == "5,7"
    assert events["item_code"].to_list() == ["5", "7"]

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "1 cached headers have no <ACCEPTANCE-DATETIME>" in message
    assert CHASE_8K in message


def test_missing_acceptance_warning_names_the_first_five_accessions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = write_legacy_cache(tmp_path)
    for number in (2, 3, 4, 6, 7, 8):
        accession = f"0000019617-00-{number:06d}"
        header = CHASE_HEADER.replace(CHASE_8K, accession)
        cache.write(
            "headers",
            CHASE_CIK,
            accession,
            header.encode("utf-8"),
            url=f"https://www.sec.gov/Archives/edgar/data/19617/{accession.replace('-', '')}/"
            f"{accession}.hdr.sgml",
            http_status=200,
        )
    with caplog.at_level(logging.WARNING, logger="edgarpit.ingest.submissions"):
        ingest_filings(cache, CHASE_CIK, LABELS)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "7 cached headers have no <ACCEPTANCE-DATETIME>" in message
    for number in range(2, 7):
        assert f"0000019617-00-{number:06d}" in message
    assert "0000019617-00-000008" not in message


def test_date_of_filing_date_change_raises_the_fallback(tmp_path: Path) -> None:
    """A-26: a filing EDGAR re-dated forward is never timed before the later date.

    The live corpus has one such header (Apple's 1996 8-K, filing date
    1996-12-13, ``<DATE-OF-FILING-DATE-CHANGE>`` 1996-12-20); this temp copy
    reproduces the shape on the Chase header.
    """
    later = CHASE_HEADER.replace(
        "<FILING-DATE>20000121",
        "<FILING-DATE>20000121\n<DATE-OF-FILING-DATE-CHANGE>20000128",
    )
    cache = write_legacy_cache(tmp_path, header=later)
    filings, _ = ingest_filings(cache, CHASE_CIK, LABELS)
    row = filings.row(0, named=True)
    assert row["acceptance_ts"] == datetime(2000, 1, 29, 4, 59, 59, tzinfo=UTC)
    assert row["acceptance_ts_source"] == "filing_date_eod"
    assert row["ts_imputed"] is True


def test_header_only_without_acceptance_still_becomes_a_row(tmp_path: Path) -> None:
    """A-24 still applies: the cached header is used, with the A-23 time."""
    cache = write_legacy_cache(tmp_path, with_submissions=False)
    filings, events = ingest_filings(cache, CHASE_CIK, LABELS)
    assert filings.height == 1
    row = filings.row(0, named=True)
    assert row["source"] == "header"
    assert row["accession"] == CHASE_8K
    assert row["acceptance_ts"] == fallback_bound(CHASE_8K_DATE, None)
    assert row["acceptance_ts_source"] == "filing_date_eod"
    assert row["ts_imputed"] is True
    assert row["items"] == "5,7"
    assert events["item_code"].to_list() == ["5", "7"]


def test_malformed_header_still_fails_the_ingest(tmp_path: Path) -> None:
    """A corrupt header stays loud; only a missing acceptance time is tolerated."""
    broken = CHASE_HEADER.replace("<ACCESSION-NUMBER>0000019617-00-000005\n", "")
    cache = write_legacy_cache(tmp_path, header=broken)
    with pytest.raises(ValueError, match="<ACCESSION-NUMBER>") as raised:
        ingest_filings(cache, CHASE_CIK, LABELS)
    assert type(raised.value) is ValueError


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
