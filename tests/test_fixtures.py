"""Fixture-tree checks for the real SEC responses under tests/fixtures/sec.

These tests never touch the network.  They verify the tree produced by
``tools/build_fixtures.py``: layout, size budget, manifest hashes, the
required issuer cases, and that every kept accession has a parsed header.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SEC_ROOT = FIXTURE_ROOT / "sec"
RAW_DIR = SEC_ROOT / "raw"
MANIFEST_PATH = RAW_DIR / "manifest.jsonl"

CIKS = ("0001000275", "0000320193", "0001594805")
APPLE_CIK = "0000320193"
RY_CIK = "0001000275"

RY_FY2023_40F = "0001193125-23-285639"
RY_FY2023_RESTATED_40F = "0001193125-24-270294"
APPLE_10K_AMENDMENT = "0001193125-10-012091"

ANNUAL_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "40-F",
        "40-F/A",
        "20-F",
        "20-F/A",
        "6-K",
        "6-K/A",
    }
)
EVENT_FORMS = frozenset({"8-K", "8-K/A"})
ANNUAL_CUTOFF = "2018-01-01"
EVENT_CUTOFF = "2022-01-01"
FACT_CUTOFF = "2018-01-01"

STAMP_RE = re.compile(r"\d{8}T\d{6}\d{6}Z")
ACCEPTANCE_RE = re.compile(r"<ACCEPTANCE-DATETIME>(\d{14})")
ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")

SIZE_BUDGET_BYTES = 20 * 1024 * 1024


def load_manifest() -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows, "manifest.jsonl is empty"
    return rows


def iter_submissions_rows(cik: str) -> Iterator[dict[str, Any]]:
    """Yield every kept row of a submissions document or page for ``cik``."""
    paths = sorted((RAW_DIR / "submissions" / cik).glob("*.json"))
    paths += sorted((RAW_DIR / "submissions_page" / cik).glob("*/*.json"))
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        filings = document.get("filings")
        arrays = (
            filings["recent"] if isinstance(filings, dict) and "recent" in filings else document
        )
        for index in range(len(arrays["accessionNumber"])):
            yield {key: arrays[key][index] for key in arrays if isinstance(arrays[key], list)}


def load_companyfacts(cik: str) -> dict[str, Any]:
    paths = sorted((RAW_DIR / "companyfacts" / cik).glob("*.json"))
    assert len(paths) == 1, f"expected one companyfacts file for {cik}, got {paths}"
    return json.loads(paths[0].read_text(encoding="utf-8"))


def header_paths(cik: str, accession: str | None = None) -> list[Path]:
    base = RAW_DIR / "headers" / cik
    if accession is not None:
        return sorted((base / accession).glob("*.hdr.sgml"))
    return sorted(base.glob("*/*.hdr.sgml"))


def header_acceptance(cik: str, accession: str) -> datetime:
    paths = header_paths(cik, accession)
    assert paths, f"no header for {accession}"
    text = paths[0].read_text(encoding="utf-8", errors="replace")
    match = ACCEPTANCE_RE.search(text)
    assert match, f"header for {accession} has no ACCEPTANCE-DATETIME"
    parsed = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    return parsed.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


def fact_rows(
    cik: str, taxonomies: tuple[str, ...] = ("us-gaap", "ifrs-full")
) -> Iterator[dict[str, Any]]:
    document = load_companyfacts(cik)
    for taxonomy in taxonomies:
        for concept, entry in document["facts"].get(taxonomy, {}).items():
            for unit, rows in entry["units"].items():
                for row in rows:
                    yield {
                        "taxonomy": taxonomy,
                        "concept": concept,
                        "unit": unit,
                        **row,
                    }


def test_fixture_tree_layout() -> None:
    rows = load_manifest()
    manifest_paths = {row["path"] for row in rows}
    stored = {
        path.relative_to(SEC_ROOT).as_posix() for path in RAW_DIR.rglob("*") if path.is_file()
    }
    stored.discard("raw/manifest.jsonl")
    assert stored == manifest_paths, "manifest and stored files disagree"
    for row in rows:
        assert row["path"].startswith("raw/")
        parts = row["path"].split("/")
        assert len(parts[1]) > 0
        stamp = parts[-1].split(".")[0]
        assert STAMP_RE.fullmatch(stamp), f"bad fetched_at stamp in {row['path']}"
        assert row["fetched_at"] == stamp
        assert row["endpoint"] in {
            "submissions",
            "submissions_page",
            "companyfacts",
            "headers",
        }
        assert row["trimmed"] is True
        if row["endpoint"] == "headers":
            assert parts[0:3] == ["raw", "headers", row["cik"]]
            assert ACCESSION_RE.fullmatch(parts[3])
        else:
            assert parts[2] == row["cik"]


def test_size_budget() -> None:
    total = sum(path.stat().st_size for path in FIXTURE_ROOT.rglob("*") if path.is_file())
    assert total < SIZE_BUDGET_BYTES, f"fixtures are {total / 1e6:.1f} MB, budget is 20 MB"


def test_manifest_hashes_match_files() -> None:
    for row in load_manifest():
        body = (SEC_ROOT / row["path"]).read_bytes()
        assert len(body) == row["bytes"], row["path"]
        assert hashlib.sha256(body).hexdigest() == row["sha256"], row["path"]
        assert row["http_status"] == 200, row["path"]


def test_manifest_urls_are_real_sec_urls_for_the_cik() -> None:
    for row in load_manifest():
        cik = row["cik"]
        assert re.fullmatch(r"\d{10}", cik)
        url = row["url"]
        parts = row["path"].split("/")
        if row["endpoint"] == "submissions":
            assert url == f"https://data.sec.gov/submissions/CIK{cik}.json"
        elif row["endpoint"] == "submissions_page":
            page = parts[3]
            assert url == f"https://data.sec.gov/submissions/{page}.json"
            assert page.startswith(f"CIK{cik}-submissions-")
        elif row["endpoint"] == "companyfacts":
            assert url == f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        else:
            accession = parts[3]
            assert ACCESSION_RE.fullmatch(accession)
            expected = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{accession.replace('-', '')}/{accession}.hdr.sgml"
            )
            assert url == expected


def test_every_page_file_from_submissions_is_kept() -> None:
    for cik in CIKS:
        main = sorted((RAW_DIR / "submissions" / cik).glob("*.json"))
        assert len(main) == 1
        document = json.loads(main[0].read_text(encoding="utf-8"))
        page_names = [entry["name"] for entry in document["filings"].get("files", [])]
        stored = sorted((RAW_DIR / "submissions_page" / cik).glob("*/*.json"))
        assert len(stored) == len(page_names)
        assert {path.parent.name + ".json" for path in stored} == set(page_names)


def test_kept_submissions_rows_respect_the_filter() -> None:
    for cik in CIKS:
        for row in iter_submissions_rows(cik):
            form = row["form"]
            filing_date = row["filingDate"]
            assert form in ANNUAL_FORMS | EVENT_FORMS, row
            if form in EVENT_FORMS:
                assert filing_date >= EVENT_CUTOFF, row
            elif (cik, row["accessionNumber"]) != (APPLE_CIK, APPLE_10K_AMENDMENT):
                assert filing_date >= ANNUAL_CUTOFF, row


def test_headers_parse_and_convert_to_utc() -> None:
    total = 0
    for cik in CIKS:
        for path in header_paths(cik):
            text = path.read_text(encoding="utf-8", errors="replace")
            assert ACCEPTANCE_RE.search(text), path
            total += 1
    assert total > 0
    # 2023-11-30 is EST (UTC-5): 06:45:12 local -> 11:45:12Z.
    assert header_acceptance(RY_CIK, RY_FY2023_40F) == datetime(
        2023, 11, 30, 11, 45, 12, tzinfo=UTC
    )


def test_every_kept_fact_accession_has_a_header() -> None:
    for cik in CIKS:
        accessions = {row["accn"] for row in fact_rows(cik)}
        assert accessions
        missing = sorted(accession for accession in accessions if not header_paths(cik, accession))
        assert missing == [], f"{cik} fact accessions without headers: {missing}"


def test_royal_bank_revenue_restatement_versions() -> None:
    versions = [
        row
        for row in fact_rows(RY_CIK, ("ifrs-full",))
        if row["concept"] == "Revenue"
        and row["unit"] == "CAD"
        and row["end"] == "2023-10-31"
        and row["start"] == "2022-11-01"
    ]
    values = {row["val"]: row["accn"] for row in versions}
    assert values.get(56129000000) == RY_FY2023_40F
    assert values.get(51464000000) == RY_FY2023_RESTATED_40F
    rows_by_accession = {row["accessionNumber"]: row for row in iter_submissions_rows(RY_CIK)}
    assert rows_by_accession[RY_FY2023_40F]["filingDate"] == "2023-11-30"
    assert rows_by_accession[RY_FY2023_RESTATED_40F]["filingDate"] == "2024-12-04"
    assert header_paths(RY_CIK, RY_FY2023_40F)
    assert header_paths(RY_CIK, RY_FY2023_RESTATED_40F)


def test_apple_amendment_is_kept_with_a_header() -> None:
    rows_by_accession = {row["accessionNumber"]: row for row in iter_submissions_rows(APPLE_CIK)}
    amendment = rows_by_accession[APPLE_10K_AMENDMENT]
    assert amendment["form"] == "10-K/A"
    assert amendment["filingDate"] == "2010-01-25"
    assert header_paths(APPLE_CIK, APPLE_10K_AMENDMENT)


def test_apple_has_many_8ks_with_headers() -> None:
    eight_ks = [
        row
        for row in iter_submissions_rows(APPLE_CIK)
        if row["form"] in EVENT_FORMS and row["filingDate"] >= EVENT_CUTOFF
    ]
    assert len(eight_ks) >= 20, f"only {len(eight_ks)} Apple 8-Ks"
    for row in eight_ks:
        assert header_paths(APPLE_CIK, row["accessionNumber"]), row["accessionNumber"]


def test_annual_filing_with_period_end_before_asof_but_accepted_after() -> None:
    """A period end can precede an as-of time while the filing that carried
    the value was accepted after it; the date rule alone must not decide."""
    asof = datetime(2024, 1, 15, tzinfo=UTC)
    cases = []
    for cik in CIKS:
        for row in fact_rows(cik):
            start, end = row.get("start"), row.get("end")
            if not isinstance(start, str) or not isinstance(end, str):
                continue
            if not 350 <= (date.fromisoformat(end) - date.fromisoformat(start)).days <= 380:
                continue
            if not end < asof.date().isoformat():
                continue
            acceptance = header_acceptance(cik, row["accn"])
            if acceptance > asof:
                cases.append((cik, row["concept"], end, row["accn"], acceptance))
    assert cases, "no annual filing with period end before the as-of but accepted after it"
    # The RY FY2023 restatement published on 2024-12-04 is one such case.
    assert any(case[0] == RY_CIK and case[3] == RY_FY2023_RESTATED_40F for case in cases)


def test_fact_rows_are_recent_enough() -> None:
    for cik in CIKS:
        for row in fact_rows(cik):
            assert row["end"] >= FACT_CUTOFF, row
