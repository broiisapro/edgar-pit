#!/usr/bin/env python3
"""Build the offline SEC fixtures under ``tests/fixtures/sec`` from live EDGAR.

The fixtures are real SEC responses for three issuers, trimmed by removing
array rows only: no value and no key inside a kept entry is ever edited.
Concepts outside ``KEEP_CONCEPTS`` and units that lose every row are dropped
entirely, which is the only structural removal besides rows.

- Royal Bank of Canada, CIK 0001000275 (40-F/IFRS, CAD; reference restatement)
- Apple Inc., CIK 0000320193 (US GAAP, 10-K/A, many 8-Ks)
- Shopify Inc., CIK 0001594805 (40-F until 2024, then 10-K)

Layout (decision A-4, uncompressed so PR diffs are reviewable)::

    tests/fixtures/sec/raw/submissions/<cik10>/<fetched_at>.json
    tests/fixtures/sec/raw/submissions_page/<cik10>/<page-stem>/<fetched_at>.json
    tests/fixtures/sec/raw/companyfacts/<cik10>/<fetched_at>.json
    tests/fixtures/sec/raw/headers/<cik10>/<accession>/<fetched_at>.hdr.sgml
    tests/fixtures/sec/raw/manifest.jsonl

``fetched_at`` is ``YYYYMMDDTHHMMSSffffffZ`` (UTC, microseconds).  The
manifest carries one row per stored file with the SHA-256 of the stored bytes
and ``"trimmed": true`` (decision A-15).

Trim rules:
- ``filings.recent`` and each paginated page keep rows whose form is
  ``10-K``, ``10-K/A``, ``10-Q``, ``10-Q/A``, ``40-F``, ``40-F/A``, ``20-F``,
  ``20-F/A``, ``6-K`` or ``6-K/A`` with filing date >= 2018-01-01, plus
  ``8-K``/``8-K/A`` with filing date >= 2022-01-01.  All column arrays in a
  document are trimmed by the same indices.  Every page file is written even
  when it trims to zero rows, so pagination stays exercised.  A row is also
  kept when its accession carries a kept companyfacts fact, so Epic 01 can
  join every kept fact to a filing.
- companyfacts keeps every ``dei`` concept plus the concepts in
  ``KEEP_CONCEPTS`` (all concepts of the canonical fields for both taxonomies
  and a few deliberately unmapped ones), with fact rows whose ``end`` is
  >= 2018-01-01.
- One SGML header is fetched and stored for every accession referenced by a
  kept filing row or a kept fact (the URL needs only cik + accession, so it
  exists even for accessions missing from submissions).

The tool talks to SEC's documented endpoints only, at <= 4 requests/second,
with a declared ``User-Agent`` read from ``SEC_USER_AGENT`` (environment or
the repository ``.env``).  It refuses to run without a valid "Name email"
agent.  Re-running reproduces the tree; only ``fetched_at`` names and fields
change.  Only the standard library is used so the script runs before the
project environment exists.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "sec"
RAW_DIR = FIXTURE_ROOT / "raw"

ISSUERS: tuple[tuple[str, str], ...] = (
    ("0001000275", "Royal Bank of Canada"),
    ("0000320193", "Apple Inc."),
    ("0001594805", "Shopify Inc."),
)

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

# Apple's only 10-K/A was filed 2010-01-25, before the cutoff, but the
# amendment case must be in the fixtures, so its row is always kept.
FORCED_ACCESSIONS: dict[str, frozenset[str]] = {
    "0000320193": frozenset({"0001193125-10-012091"}),
}

# One concept list per taxonomy.  ``KEEP_CONCEPTS`` covers every canonical
# field of the concept map (both taxonomies, ordered fallbacks included) plus
# deliberately unmapped concepts used by QA reports.
KEEP_CONCEPTS: dict[str, frozenset[str]] = {
    "us-gaap": frozenset(
        {
            # revenue
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            # net income
            "NetIncomeLoss",
            "ProfitLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            # total assets / liabilities / equity
            "Assets",
            "Liabilities",
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            # cash flow
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            # cash
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            # shares
            "CommonStockSharesOutstanding",
            "CommonStockSharesIssued",
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfSharesOutstandingBasic",
            # earnings per share
            "EarningsPerShareDiluted",
            "EarningsPerShareBasic",
            # operating income / gross profit
            "OperatingIncomeLoss",
            "GrossProfit",
            # current assets / liabilities
            "AssetsCurrent",
            "LiabilitiesCurrent",
            # debt
            "LongTermDebtNoncurrent",
            "LongTermDebt",
            "LongTermDebtCurrent",
            # further canonical fields
            "InterestExpense",
            "InterestExpenseNonoperating",
            "PaymentsOfDividendsCommonStock",
            "PaymentsOfDividends",
            "PaymentsForRepurchaseOfCommonStock",
            "ResearchAndDevelopmentExpense",
            # deliberately unmapped
            "SellingGeneralAndAdministrativeExpense",
            "AdvertisingExpense",
            "IncomeTaxExpenseBenefit",
            "InventoryNet",
            "AccountsReceivableNetCurrent",
            "AccountsPayableCurrent",
            "CostOfGoodsAndServicesSold",
        }
    ),
    "ifrs-full": frozenset(
        {
            # revenue
            "Revenue",
            "RevenueFromContractsWithCustomers",
            # net income
            "ProfitLoss",
            "ProfitLossAttributableToOwnersOfParent",
            "ProfitLossFromContinuingOperations",
            # total assets / liabilities / equity
            "Assets",
            "Liabilities",
            "Equity",
            "EquityAndLiabilities",
            "EquityAttributableToOwnersOfParent",
            # cash flow
            "CashFlowsFromUsedInOperatingActivities",
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            # cash
            "CashAndCashEquivalents",
            # shares
            "NumberOfSharesOutstanding",
            "NumberOfSharesIssued",
            # earnings per share
            "DilutedEarningsLossPerShare",
            "DilutedEarningsLossPerShareFromContinuingOperations",
            "BasicEarningsLossPerShare",
            "BasicEarningsLossPerShareFromContinuingOperations",
            # operating income / gross profit
            "ProfitLossFromOperatingActivities",
            "GrossProfit",
            # current assets / liabilities
            "CurrentAssets",
            "CurrentLiabilities",
            # debt
            "LongtermBorrowings",
            "NoncurrentPortionOfLongtermBorrowings",
            "CurrentPortionOfLongtermBorrowings",
            # further canonical fields
            "FinanceCosts",
            "DividendsPaidClassifiedAsFinancingActivities",
            "PaymentsForRepurchaseOfEquity",
            "ResearchAndDevelopmentExpense",
            # deliberately unmapped
            "CostOfSales",
            "AdministrativeExpense",
            "InterestRevenueCalculatedUsingEffectiveInterestMethod",
            "OtherOperatingIncomeExpense",
            "CashFlowsFromUsedInInvestingActivities",
        }
    ),
}

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_ACCEPTANCE_RE = re.compile(r"<ACCEPTANCE-DATETIME>(\d{14})")

DATA_HOST = "https://data.sec.gov"
ARCHIVES_HOST = "https://www.sec.gov"
RATE_LIMIT_RPS = 4.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5


class FixtureError(RuntimeError):
    """Raised when the fixtures cannot be built."""


def find_user_agent() -> str:
    """Return ``SEC_USER_AGENT`` from the environment or a repository .env.

    In a worktree the .env lives at the main checkout, so the search walks up
    from the repository root and takes the first .env that defines the key.
    """
    value = os.environ.get("SEC_USER_AGENT", "")
    if value:
        return value
    for directory in (REPO_ROOT, *REPO_ROOT.parents):
        env_file = directory / ".env"
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SEC_USER_AGENT="):
                value = line.partition("=")[2].strip().strip("'\"")
                if value:
                    return value
    return ""


def validate_user_agent(value: str) -> str:
    """Return the agent when it is "Name email", else raise ``FixtureError``."""
    agent = value.strip()
    tokens = agent.split()
    if len(tokens) < 2 or not _EMAIL_RE.fullmatch(tokens[-1]):
        raise FixtureError(
            "SEC_USER_AGENT must look like 'Name email' (for example "
            "'Jane Doe jane@example.com'); set it in the environment or in .env. "
            f"Got: {value!r}"
        )
    return agent


class RateLimiter:
    """Minimum spacing between request starts; guarantees <= 4 req/s."""

    def __init__(self, rate: float) -> None:
        self._spacing = 1.0 / rate
        self._next_at = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_at:
            time.sleep(self._next_at - now)
            now = time.monotonic()
        self._next_at = now + self._spacing


def _fetch(url: str, *, agent: str, limiter: RateLimiter) -> tuple[int, bytes]:
    """GET ``url`` with retries; return ``(status, body)``."""
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        limiter.wait()
        request = urllib.request.Request(
            url,
            headers={"User-Agent": agent, "Accept-Encoding": "gzip"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return response.status, body
        except urllib.error.HTTPError as error:
            if error.code == 403:
                raise FixtureError(
                    f"HTTP 403 for {url}; SEC rejected the request. Check that "
                    "SEC_USER_AGENT is a real 'Name email' with your contact address."
                ) from error
            if error.code == 404:
                return 404, b""
            last_error = f"HTTP {error.code}"
            if error.code not in RETRY_STATUSES:
                raise FixtureError(f"unexpected {last_error} for {url}") from error
            retry_after = error.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after and retry_after.isdigit()
                else 0.5 * attempt
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = f"{type(error).__name__}: {error}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(0.5 * attempt)
    raise FixtureError(
        f"giving up on {url} after {MAX_ATTEMPTS} attempts ({last_error})"
    )


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def json_bytes(document: Any) -> bytes:
    return json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8")


def keep_row(form: Any, filing_date: Any) -> bool:
    """True when a submissions row passes the form/date filter."""
    return (
        isinstance(form, str)
        and isinstance(filing_date, str)
        and filing_date >= ANNUAL_CUTOFF
        and (
            form in ANNUAL_FORMS
            or (form in EVENT_FORMS and filing_date >= EVENT_CUTOFF)
        )
    )


def trim_arrays(arrays: dict[str, Any], keep_indices: Iterable[int]) -> dict[str, Any]:
    """Rebuild every list value of ``arrays`` from the same row indices."""
    indices = list(keep_indices)
    lengths = {len(value) for value in arrays.values() if isinstance(value, list)}
    if len(lengths) > 1:
        raise FixtureError(f"submissions arrays disagree on length: {sorted(lengths)}")
    return {
        key: [value[index] for index in indices] if isinstance(value, list) else value
        for key, value in arrays.items()
    }


def select_rows(arrays: dict[str, Any], extra_accessions: frozenset[str]) -> list[int]:
    accessions = arrays["accessionNumber"]
    forms = arrays["form"]
    dates = arrays["filingDate"]
    return [
        index
        for index in range(len(accessions))
        if keep_row(forms[index], dates[index]) or accessions[index] in extra_accessions
    ]


def trim_submissions_main(
    document: dict[str, Any], extra_accessions: frozenset[str]
) -> dict[str, Any]:
    """Trim ``filings.recent``; keep the pagination list untouched."""
    filings = document.get("filings")
    if not isinstance(filings, dict) or "recent" not in filings:
        raise FixtureError("submissions document has no filings.recent block")
    trimmed = dict(document)
    trimmed_filings = dict(filings)
    trimmed_filings["recent"] = trim_arrays(
        filings["recent"], select_rows(filings["recent"], extra_accessions)
    )
    trimmed["filings"] = trimmed_filings
    return trimmed


def trim_submissions_page(
    page: dict[str, Any], extra_accessions: frozenset[str]
) -> dict[str, Any]:
    """Trim a paginated submissions page (a bare dict of parallel arrays)."""
    return trim_arrays(page, select_rows(page, extra_accessions))


def trim_companyfacts(document: dict[str, Any]) -> dict[str, Any]:
    """Keep dei plus ``KEEP_CONCEPTS``; keep fact rows with ``end`` >= cutoff."""
    facts = document.get("facts")
    if not isinstance(facts, dict):
        raise FixtureError("companyfacts document has no facts block")
    kept_facts: dict[str, Any] = {}
    for taxonomy, concepts in facts.items():
        if taxonomy == "dei":
            keep: frozenset[str] | None = None
        elif taxonomy in KEEP_CONCEPTS:
            keep = KEEP_CONCEPTS[taxonomy]
        else:
            continue
        kept_concepts: dict[str, Any] = {}
        for concept, entry in concepts.items():
            if keep is not None and concept not in keep:
                continue
            kept_units: dict[str, Any] = {}
            for unit, rows in entry.get("units", {}).items():
                rows = [
                    row
                    for row in rows
                    if isinstance(row.get("end"), str) and row["end"] >= FACT_CUTOFF
                ]
                if rows:
                    kept_units[unit] = rows
            if kept_units:
                entry = dict(entry)
                entry["units"] = kept_units
                kept_concepts[concept] = entry
        if kept_concepts:
            kept_facts[taxonomy] = kept_concepts
    trimmed = dict(document)
    trimmed["facts"] = kept_facts
    return trimmed


def fact_accessions(document: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for concepts in document["facts"].values():
        for entry in concepts.values():
            for rows in entry["units"].values():
                for row in rows:
                    accession = row.get("accn")
                    if isinstance(accession, str):
                        found.add(accession)
    return found


def submissions_rows(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Flatten the parallel arrays of a submissions document or page."""
    filings = document.get("filings")
    if isinstance(filings, dict) and "recent" in filings:
        arrays = filings["recent"]
    else:
        arrays = document
    keys = [key for key, value in arrays.items() if isinstance(value, list)]
    rows: dict[str, dict[str, Any]] = {}
    for index in range(len(arrays["accessionNumber"])):
        rows[arrays["accessionNumber"][index]] = {
            key: arrays[key][index] for key in keys
        }
    return rows


def header_url(cik: str, accession: str) -> str:
    return (
        f"{ARCHIVES_HOST}/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}/{accession}.hdr.sgml"
    )


class Manifest:
    """Collects ``raw/manifest.jsonl`` rows while storing fixture files."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def store(
        self,
        *,
        endpoint: str,
        cik: str,
        key: str,
        suffix: str,
        url: str,
        body: bytes,
        http_status: int,
    ) -> Path:
        relative = Path("raw") / endpoint / cik
        if key:
            relative = relative / key
        stamp = now_stamp()
        relative = relative / f"{stamp}{suffix}"
        path = FIXTURE_ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():  # pragma: no cover - microsecond names make this absurd
            raise FixtureError(f"refusing to overwrite {path}")
        path.write_bytes(body)
        self.rows.append(
            {
                "path": relative.as_posix(),
                "url": url,
                "endpoint": endpoint,
                "cik": cik,
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "fetched_at": stamp,
                "http_status": http_status,
                "trimmed": True,
            }
        )
        return path

    def write(self) -> None:
        rows = sorted(self.rows, key=lambda row: row["path"])
        lines = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        )
        (RAW_DIR / "manifest.jsonl").write_text(lines, encoding="utf-8")


def build(*, agent: str, limiter: RateLimiter) -> dict[str, Any]:
    if RAW_DIR.exists():
        shutil.rmtree(RAW_DIR)
    RAW_DIR.mkdir(parents=True)
    manifest = Manifest()
    summary: dict[str, Any] = {"issuers": {}, "missing_from_submissions": []}

    for cik, name in ISSUERS:
        print(f"[{cik}] {name}", flush=True)
        issuer: dict[str, Any] = {}

        status, body = _fetch(
            f"{DATA_HOST}/submissions/CIK{cik}.json", agent=agent, limiter=limiter
        )
        document = json.loads(body)
        pages = document["filings"].get("files", [])
        issuer["submissions_pages"] = len(pages)

        page_docs: dict[str, dict[str, Any]] = {}
        for page in pages:
            page_name = page["name"]
            _, page_body = _fetch(
                f"{DATA_HOST}/submissions/{page_name}", agent=agent, limiter=limiter
            )
            page_docs[page_name] = json.loads(page_body)

        _, cf_body = _fetch(
            f"{DATA_HOST}/api/xbrl/companyfacts/CIK{cik}.json",
            agent=agent,
            limiter=limiter,
        )
        companyfacts = trim_companyfacts(json.loads(cf_body))
        fact_acces = fact_accessions(companyfacts)
        issuer["companyfacts_accessions"] = len(fact_acces)
        issuer["companyfacts_concepts"] = sum(
            len(c) for c in companyfacts["facts"].values()
        )

        extra = frozenset(fact_acces) | FORCED_ACCESSIONS.get(cik, frozenset())

        main_trimmed = trim_submissions_main(document, extra)
        manifest.store(
            endpoint="submissions",
            cik=cik,
            key="",
            suffix=".json",
            url=f"{DATA_HOST}/submissions/CIK{cik}.json",
            body=json_bytes(main_trimmed),
            http_status=status,
        )

        kept_rows: dict[str, dict[str, Any]] = submissions_rows(main_trimmed)
        for page in pages:
            page_name = page["name"]
            trimmed_page = trim_submissions_page(page_docs[page_name], extra)
            manifest.store(
                endpoint="submissions_page",
                cik=cik,
                key=page_name.removesuffix(".json"),
                suffix=".json",
                url=f"{DATA_HOST}/submissions/{page_name}",
                body=json_bytes(trimmed_page),
                http_status=200,
            )
            kept_rows.update(submissions_rows(trimmed_page))

        manifest.store(
            endpoint="companyfacts",
            cik=cik,
            key="",
            suffix=".json",
            url=f"{DATA_HOST}/api/xbrl/companyfacts/CIK{cik}.json",
            body=json_bytes(companyfacts),
            http_status=200,
        )

        kept_filing_accessions = set(kept_rows)
        issuer["filings_kept"] = len(kept_filing_accessions)
        for accession in sorted(extra - kept_filing_accessions):
            summary["missing_from_submissions"].append(
                {"cik": cik, "accession": accession}
            )

        accessions = sorted(kept_filing_accessions | extra)
        fetched = missing = 0
        print(f"[{cik}] {len(accessions)} headers to fetch", flush=True)
        for number, accession in enumerate(accessions, start=1):
            url = header_url(cik, accession)
            header_status, header_body = _fetch(url, agent=agent, limiter=limiter)
            if header_status == 404 or not header_body:
                missing += 1
                print(f"[{cik}] missing header for {accession}", flush=True)
                continue
            if not _ACCEPTANCE_RE.search(header_body.decode("utf-8", "replace")):
                raise FixtureError(f"header {accession} has no ACCEPTANCE-DATETIME")
            manifest.store(
                endpoint="headers",
                cik=cik,
                key=accession,
                suffix=".hdr.sgml",
                url=url,
                body=header_body,
                http_status=header_status,
            )
            fetched += 1
            if number % 50 == 0:
                print(f"[{cik}] {number}/{len(accessions)} headers", flush=True)
        issuer["headers_fetched"] = fetched
        issuer["headers_missing"] = missing
        summary["issuers"][cik] = issuer

    manifest.write()
    return summary


def main() -> int:
    try:
        agent = validate_user_agent(find_user_agent())
    except FixtureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    limiter = RateLimiter(RATE_LIMIT_RPS)
    try:
        summary = build(agent=agent, limiter=limiter)
    except FixtureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    total_bytes = sum(
        path.stat().st_size for path in FIXTURE_ROOT.rglob("*") if path.is_file()
    )
    summary["fixture_bytes"] = total_bytes
    print(json.dumps(summary, indent=2))
    print(f"fixtures written to {FIXTURE_ROOT} ({total_bytes / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
