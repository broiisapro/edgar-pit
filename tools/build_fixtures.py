#!/usr/bin/env python3
"""Build the offline SEC fixtures under ``tests/fixtures/sec`` from live EDGAR.

The fixtures are real SEC responses for three issuers, trimmed by removing
array rows only: no value and no key inside a kept entry is ever edited.
Concepts outside :func:`keep_concepts` and units that lose every row are
dropped entirely, which is the only structural removal besides rows.

- Royal Bank of Canada, CIK 0001000275 (40-F/IFRS, CAD; reference restatement)
- Apple Inc., CIK 0000320193 (US GAAP, 10-K/A, many 8-Ks)
- Shopify Inc., CIK 0001594805 (40-F until 2024, then 10-K)

Layout (decision A-4, uncompressed so PR diffs are reviewable)::

    tests/fixtures/sec/raw/submissions/<cik10>/<fetched_at>.json
    tests/fixtures/sec/raw/submissions_page/<cik10>/<page-stem>/<fetched_at>.json
    tests/fixtures/sec/raw/companyfacts/<cik10>/<fetched_at>.json
    tests/fixtures/sec/raw/headers/<cik10>/<accession>/<fetched_at>.hdr.sgml
    tests/fixtures/sec/raw/company_tickers/_/<fetched_at>.json
    tests/fixtures/sec/raw/filing_index/<cik10>/<accession>/<fetched_at>.html
    tests/fixtures/sec/raw/documents/<cik10>/<accession>__<filename>/<fetched_at>.<ext>
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
- companyfacts keeps every ``dei`` concept plus :func:`keep_concepts`: every
  concept of every canonical field in ``config/concept_map.yaml`` (both
  taxonomies) and a few deliberately unmapped ones, with fact rows whose
  ``end`` is >= 2018-01-01.
- One SGML header is fetched and stored for every accession referenced by a
  kept filing row or a kept fact (the URL needs only cik + accession, so it
  exists even for accessions missing from submissions).

The tool talks to SEC's documented endpoints only, at <= 4 requests/second,
with a declared ``User-Agent`` read from ``SEC_USER_AGENT`` (environment or
the repository ``.env``).  It refuses to run without a valid "Name email"
agent.  Re-running reproduces the tree; only ``fetched_at`` names and fields
change.  The HTTP path uses only the standard library; the trim step reads
``config/concept_map.yaml`` through :mod:`edgarpit.concepts`, so full builds
run with ``uv run python tools/build_fixtures.py``.

``--staging`` rebuilds ``tests/fixtures/staging/{filings,facts_raw,events_8k}.csv``
and ``tests/fixtures/expected_asof.json`` from the stored fixture tree (no
network, no User-Agent) for the selection in ``STAGING_FIELDS`` and
``STAGING_PERIODS``: FY2022-FY2024 annual facts for the seven fields, two
quarterly periods per issuer, every version present in the fixtures, one
deliberately unmapped concept per issuer, the matching filings rows, and the
exploded items of the ten most recent Apple 8-Ks.  Within each
``(cik, field, period, taxonomy)`` group the lowest-ranked mapped concept
present in the fixtures is used.  Timestamps come from the SGML headers
(America/New_York -> UTC) and headers match ``edgarpit.schemas.COLUMNS``.
Run it with ``uv run python tools/build_fixtures.py --staging``.

``--company-tickers`` refreshes only ``raw/company_tickers/_/<fetched_at>.json``
(decision A-17): SEC's ``company_tickers.json`` trimmed, by deleting entries
only, to the tickers in ``config/universe.csv`` plus ``EXTRA_TICKERS``.  The
rest of the fixture tree is left untouched and the manifest is rewritten with
the refreshed row.  Run it with ``uv run python tools/build_fixtures.py
--company-tickers``.

``--text`` builds the 8-K text fixtures for the filings in :data:`TEXT_ITEM_2_02`,
:data:`TEXT_OTHER_8KS` and :data:`TEXT_8K_AMENDMENTS`, which must already be
present in the submissions fixtures (decision A-30). They join the same cache
as everything else (decision A-44)::

    tests/fixtures/sec/raw/filing_index/<cik>/<accession>/<fetched_at>.html
    tests/fixtures/sec/raw/documents/<cik>/<accession>__<filename>/<fetched_at>.<ext>
    tests/fixtures/sec/raw/manifest.jsonl

Every filing's ``<accession>-index-headers.html`` is fetched, then its primary
document and every ``EX-99.1`` it lists.  Bytes are stored exactly as SEC
served them, so the text manifest rows carry ``"trimmed": false``.  The mode
replaces only the ``filing_index`` and ``documents`` rows and files, leaving
the other endpoints of the cache untouched, so it can be rerun on its own.
Run it with ``uv run python tools/build_fixtures.py --text``.

Every mode shares one manifest.  A full run replaces the submissions, page,
companyfacts and header rows and leaves the text, FX and ``company_tickers``
entries (written by ``--text``, ``tools/build_fx_fixture.py`` and
``--company-tickers``) in place, so the cache can be refreshed endpoint by
endpoint without losing the rest.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "sec"
RAW_DIR = FIXTURE_ROOT / "raw"
STAGING_DIR = REPO_ROOT / "tests" / "fixtures" / "staging"
EXPECTED_ASOF_PATH = REPO_ROOT / "tests" / "fixtures" / "expected_asof.json"
UNIVERSE_PATH = REPO_ROOT / "config" / "universe.csv"

# The endpoints a full run refetches and rewrites. Everything else in the cache
# (the 8-K text from ``--text``, the FX series from tools/build_fx_fixture.py
# and company_tickers from ``--company-tickers``) is left untouched, so one
# cache serves every endpoint (decision A-44).
FULL_BUILD_ENDPOINTS: tuple[str, ...] = (
    "submissions",
    "submissions_page",
    "companyfacts",
    "headers",
)
TEXT_ENDPOINTS: tuple[str, ...] = ("filing_index", "documents")

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

# Staging selection (``--staging``): canonical fields and the fiscal periods
# the staging CSVs cover.  Duration facts must match both boundaries; instant
# facts match the period end (their period_start is null).
STAGING_FIELDS: tuple[str, ...] = (
    "revenue",
    "net_income",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "cash",
    "eps_diluted",
)

STAGING_PERIODS: dict[str, dict[str, tuple[str, str]]] = {
    "0001000275": {
        "FY2022": ("2021-11-01", "2022-10-31"),
        "FY2023": ("2022-11-01", "2023-10-31"),
        "FY2024": ("2023-11-01", "2024-10-31"),
        "Q1-2024": ("2023-11-01", "2024-01-31"),
        "Q2-2024": ("2024-02-01", "2024-04-30"),
    },
    "0000320193": {
        "FY2022": ("2021-09-26", "2022-09-24"),
        "FY2023": ("2022-09-25", "2023-09-30"),
        "FY2024": ("2023-10-01", "2024-09-28"),
        "Q1-2024": ("2023-10-01", "2023-12-30"),
        "Q2-2024": ("2023-12-31", "2024-03-30"),
    },
    "0001594805": {
        "FY2022": ("2022-01-01", "2022-12-31"),
        "FY2023": ("2023-01-01", "2023-12-31"),
        "FY2024": ("2024-01-01", "2024-12-31"),
        "Q1-2024": ("2024-01-01", "2024-03-31"),
        "Q2-2024": ("2024-04-01", "2024-06-30"),
    },
}

# One deliberately unmapped concept per issuer, kept in the fixtures and in
# the staging facts so QA and the concept-extension path have real input.
UNMAPPED_SELECTION: dict[str, tuple[str, str]] = {
    "0001000275": ("ifrs-full", "CashFlowsFromUsedInInvestingActivities"),
    "0000320193": ("us-gaap", "IncomeTaxExpenseBenefit"),
    "0001594805": ("us-gaap", "IncomeTaxExpenseBenefit"),
}

APPLE_CIK = "0000320193"
SHOPIFY_CIK = "0001594805"
APPLE_8K_COUNT = 10

# 8-K text fixtures (``--text``, decision A-30).  Royal Bank of Canada files
# 6-Ks rather than 8-Ks and has no 8-K row in the submissions fixtures, so it
# contributes no text fixture.
#
# Item 2.02 (results of operations): at least eight Apple and four Shopify
# filings, chosen across the formats the parser must survive -- Workiva pages
# with hidden inline XBRL, the newer EDGAR layout, press releases with wide
# financial tables, and (for Shopify) a filing that also carries Item 7.01.
TEXT_ITEM_2_02: dict[str, tuple[str, ...]] = {
    APPLE_CIK: (
        "0000320193-22-000006",
        "0000320193-22-000069",
        "0000320193-23-000005",
        "0000320193-23-000104",
        "0000320193-24-000067",
        "0000320193-25-000007",
        "0000320193-25-000077",
        "0000320193-26-000018",
    ),
    SHOPIFY_CIK: (
        "0001594805-25-000011",
        "0001594805-25-000072",
        "0001594805-26-000006",
        "0001594805-26-000046",
    ),
}

# Two 8-Ks without Item 2.02: multi-item officer changes and a Regulation FD
# disclosure, both without an exhibit.
TEXT_OTHER_8KS: dict[str, tuple[str, ...]] = {
    APPLE_CIK: ("0001193125-22-225365", "0001140361-24-040659"),
}

# The only 8-K/A in the submissions fixtures.
TEXT_8K_AMENDMENTS: dict[str, tuple[str, ...]] = {
    APPLE_CIK: ("0001140361-26-035325",),
}

# The trimmed ``company_tickers.json`` fixture keeps the universe's tickers and
# a few real tickers outside it, so resolution sees non-universe entries too.
EXTRA_TICKERS: tuple[str, ...] = ("IBM", "TSLA", "NFLX", "AMD", "INTC")
EXPECTED_ASOF: dict[str, Any] = {
    "cik": "0001000275",
    "field": "revenue",
    "asof": "2024-01-15T00:00:00Z",
    "period": "FY",
    "accession": "0001193125-23-285639",
}

# Deliberately unmapped concepts kept beyond the concept map, so QA reports
# and the concept-extension path have real unmapped input in the fixtures.
EXTRA_CONCEPTS: dict[str, frozenset[str]] = {
    "us-gaap": frozenset(
        {
            "SalesRevenueGoodsNet",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            "CommonStockSharesIssued",
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfSharesOutstandingBasic",
            "LongTermDebtCurrent",
            "InterestExpense",
            "InterestExpenseNonoperating",
            "PaymentsOfDividendsCommonStock",
            "PaymentsOfDividends",
            "PaymentsForRepurchaseOfCommonStock",
            "SellingGeneralAndAdministrativeExpense",
            "AdvertisingExpense",
            "IncomeTaxExpenseBenefit",
            "AccountsPayableCurrent",
            "CostOfGoodsAndServicesSold",
        }
    ),
    "ifrs-full": frozenset(
        {
            "ProfitLossFromContinuingOperations",
            "EquityAndLiabilities",
            "NumberOfSharesIssued",
            "DilutedEarningsLossPerShareFromContinuingOperations",
            "BasicEarningsLossPerShareFromContinuingOperations",
            "NoncurrentPortionOfLongtermBorrowings",
            "CurrentPortionOfLongtermBorrowings",
            "FinanceCosts",
            "DividendsPaidClassifiedAsFinancingActivities",
            "PaymentsForRepurchaseOfEquity",
            "CostOfSales",
            "AdministrativeExpense",
            "InterestRevenueCalculatedUsingEffectiveInterestMethod",
            "OtherOperatingIncomeExpense",
            "CashFlowsFromUsedInInvestingActivities",
        }
    ),
}


@lru_cache(maxsize=1)
def keep_concepts() -> dict[str, frozenset[str]]:
    """Return the concepts to keep per taxonomy.

    Every concept of every canonical field in ``config/concept_map.yaml``
    (both taxonomies, ordered fallbacks included) plus
    :data:`EXTRA_CONCEPTS`. The mapped set is derived through
    :meth:`edgarpit.concepts.ConceptMap.specs`, so a concept added to the map
    is kept in the fixtures automatically; the map is the only hand-kept
    source of truth.
    """
    from edgarpit.concepts import load_concept_map

    mapped: dict[str, set[str]] = {"us-gaap": set(), "ifrs-full": set()}
    for spec in load_concept_map().specs():
        mapped["us-gaap"].update(spec.us_gaap)
        mapped["ifrs-full"].update(spec.ifrs_full)
    return {
        taxonomy: frozenset(mapped[taxonomy] | EXTRA_CONCEPTS.get(taxonomy, frozenset()))
        for taxonomy in mapped
    }


_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_ACCEPTANCE_RE = re.compile(r"<ACCEPTANCE-DATETIME>(\d{14})")

DATA_HOST = "https://data.sec.gov"
ARCHIVES_HOST = "https://www.sec.gov"
COMPANY_TICKERS_URL = f"{ARCHIVES_HOST}/files/company_tickers.json"
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
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * attempt
            if attempt < MAX_ATTEMPTS:
                time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = f"{type(error).__name__}: {error}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(0.5 * attempt)
    raise FixtureError(f"giving up on {url} after {MAX_ATTEMPTS} attempts ({last_error})")


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
        and (form in ANNUAL_FORMS or (form in EVENT_FORMS and filing_date >= EVENT_CUTOFF))
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


def trim_submissions_page(page: dict[str, Any], extra_accessions: frozenset[str]) -> dict[str, Any]:
    """Trim a paginated submissions page (a bare dict of parallel arrays)."""
    return trim_arrays(page, select_rows(page, extra_accessions))


def trim_companyfacts(document: dict[str, Any]) -> dict[str, Any]:
    """Keep dei plus :func:`keep_concepts`; keep fact rows with ``end`` >= cutoff."""
    facts = document.get("facts")
    if not isinstance(facts, dict):
        raise FixtureError("companyfacts document has no facts block")
    wanted = keep_concepts()
    kept_facts: dict[str, Any] = {}
    for taxonomy, concepts in facts.items():
        if taxonomy == "dei":
            keep: frozenset[str] | None = None
        elif taxonomy in wanted:
            keep = wanted[taxonomy]
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
    arrays = filings["recent"] if isinstance(filings, dict) and "recent" in filings else document
    keys = [key for key, value in arrays.items() if isinstance(value, list)]
    rows: dict[str, dict[str, Any]] = {}
    for index in range(len(arrays["accessionNumber"])):
        rows[arrays["accessionNumber"][index]] = {key: arrays[key][index] for key in keys}
    return rows


def header_url(cik: str, accession: str) -> str:
    return (
        f"{ARCHIVES_HOST}/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}/{accession}.hdr.sgml"
    )


class Manifest:
    """Collects ``raw/manifest.jsonl`` rows while storing fixture files.

    ``root`` is the fixture root the ``path`` column is relative to; the
    default is the one merged SEC cache, and every mode writes into it
    (decision A-44).
    """

    def __init__(
        self, rows: list[dict[str, Any]] | None = None, *, root: Path | None = None
    ) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])
        self.root = FIXTURE_ROOT if root is None else root

    def discard(self, endpoints: Iterable[str]) -> None:
        """Delete the stored files and manifest rows of ``endpoints``.

        ``endpoints`` are cache endpoint names (``submissions``, ``documents``,
        ...). Rows of every other endpoint stay in the manifest, so a mode that
        refreshes one part of the cache never drops the rest.
        """
        wanted = set(endpoints)
        kept: list[dict[str, Any]] = []
        for row in self.rows:
            if row["endpoint"] in wanted:
                (self.root / row["path"]).unlink(missing_ok=True)
            else:
                kept.append(row)
        self.rows = kept

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
        trimmed: bool = True,
    ) -> Path:
        relative = Path("raw") / endpoint / (cik or "_")
        if key:
            relative = relative / key
        stamp = now_stamp()
        relative = relative / f"{stamp}{suffix}"
        path = self.root / relative
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
                "trimmed": trimmed,
            }
        )
        return path

    def write(self) -> None:
        rows = sorted(self.rows, key=lambda row: row["path"])
        lines = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        (self.root / "raw" / "manifest.jsonl").write_text(lines, encoding="utf-8")


def load_manifest_rows(root: Path | None = None) -> list[dict[str, Any]]:
    """Read the stored fixture manifest under ``root``, or return an empty list.

    ``root`` defaults to :data:`FIXTURE_ROOT`; the default is resolved at call
    time, so a caller (or a test) can point the module at another cache.
    """
    path = (FIXTURE_ROOT if root is None else root) / "raw" / "manifest.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def read_universe_tickers() -> set[str]:
    """Return the tickers of ``config/universe.csv``."""
    with UNIVERSE_PATH.open(encoding="utf-8", newline="") as handle:
        return {row["ticker"] for row in csv.DictReader(handle)}


def trim_company_tickers(document: dict[str, Any], tickers: set[str]) -> dict[str, Any]:
    """Keep the entries whose ``ticker`` is wanted; top-level keys are unchanged.

    Entries are deleted whole; no value or key inside a kept entry is edited
    (decision A-15). Raises :class:`FixtureError` when a wanted ticker is not
    in the document.
    """
    kept = {
        key: entry
        for key, entry in document.items()
        if isinstance(entry, dict) and entry.get("ticker") in tickers
    }
    found = {entry["ticker"] for entry in kept.values()}
    missing = sorted(tickers - found)
    if missing:
        raise FixtureError(f"company_tickers.json is missing tickers: {missing}")
    return kept


def build_company_tickers(*, agent: str, limiter: RateLimiter) -> dict[str, Any]:
    """Fetch, trim and store ``company_tickers.json`` without touching the tree."""
    status, body = _fetch(COMPANY_TICKERS_URL, agent=agent, limiter=limiter)
    tickers = read_universe_tickers() | set(EXTRA_TICKERS)
    trimmed = trim_company_tickers(json.loads(body), tickers)
    body_out = json_bytes(trimmed)

    manifest = Manifest(load_manifest_rows())
    kept_rows: list[dict[str, Any]] = []
    for row in manifest.rows:
        if row["endpoint"] == "company_tickers":
            (FIXTURE_ROOT / row["path"]).unlink(missing_ok=True)
        else:
            kept_rows.append(row)
    manifest.rows = kept_rows
    path = manifest.store(
        endpoint="company_tickers",
        cik="",
        key="",
        suffix=".json",
        url=COMPANY_TICKERS_URL,
        body=body_out,
        http_status=status,
    )
    manifest.write()
    return {
        "path": path.relative_to(FIXTURE_ROOT).as_posix(),
        "tickers": len(trimmed),
        "bytes": len(body_out),
    }


def build(*, agent: str, limiter: RateLimiter) -> dict[str, Any]:
    """Fetch the filings, companyfacts and header fixtures into the cache.

    The submissions, page, companyfacts and header rows are replaced; the text,
    FX and ``company_tickers`` entries are kept, so a full run and the other
    modes compose on one merged cache (decision A-44).
    """
    manifest = Manifest(load_manifest_rows())
    manifest.discard(FULL_BUILD_ENDPOINTS)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
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
        issuer["companyfacts_concepts"] = sum(len(c) for c in companyfacts["facts"].values())

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
            summary["missing_from_submissions"].append({"cik": cik, "accession": accession})

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


@dataclass(frozen=True)
class IndexDocument:
    """One ``<DOCUMENT>`` block of an EDGAR ``-index-headers.html`` page.

    ``document_type`` is the SGML ``<TYPE>`` (for example ``8-K`` or
    ``EX-99.1``), ``sequence`` its 1-based position in the filing, and
    ``filename`` the name of the document in the accession directory.
    """

    document_type: str
    sequence: int
    filename: str


_INDEX_DOCUMENT_RE = re.compile(r"&lt;DOCUMENT&gt;(.*?)&lt;/DOCUMENT&gt;", re.S)
_INDEX_TYPE_RE = re.compile(r"&lt;TYPE&gt;([^\n&<]*)")
_INDEX_SEQUENCE_RE = re.compile(r"&lt;SEQUENCE&gt;(\d+)")
_INDEX_FILENAME_RE = re.compile(r"&lt;FILENAME&gt;([^\n&<]*)")
_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def text_selection() -> list[tuple[str, str]]:
    """Return the ``(cik, accession)`` pairs of the 8-K text fixtures.

    Output order is the fetch order: every Item 2.02 filing (Apple, then
    Shopify), the non-2.02 filings, then the amendment.  The selection is
    fixed in :data:`TEXT_ITEM_2_02`, :data:`TEXT_OTHER_8KS` and
    :data:`TEXT_8K_AMENDMENTS` so a rerun stores the same filings.
    """
    pairs: list[tuple[str, str]] = []
    for group in (TEXT_ITEM_2_02, TEXT_OTHER_8KS, TEXT_8K_AMENDMENTS):
        for cik, accessions in group.items():
            pairs.extend((cik, accession) for accession in accessions)
    return pairs


def filing_index_url(cik: str, accession: str) -> str:
    """Return the ``-index-headers.html`` URL of ``accession`` for ``cik``."""
    return (
        f"{ARCHIVES_HOST}/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}/{accession}-index-headers.html"
    )


def document_url(cik: str, accession: str, filename: str) -> str:
    """Return the archive URL of one document inside ``accession``."""
    return f"{ARCHIVES_HOST}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{filename}"


def parse_index_documents(text: str) -> list[IndexDocument]:
    """Parse the document list of an EDGAR ``-index-headers.html`` page.

    ``text`` is the decoded HTML; the page embeds its index header as escaped
    SGML, so the ``<DOCUMENT>`` blocks appear as ``&lt;DOCUMENT&gt;``.  The
    result is ordered by ``<SEQUENCE>``.  Raises :class:`FixtureError` when
    the page lists no documents or a block lacks ``TYPE``, ``SEQUENCE`` or
    ``FILENAME``, which means SEC changed the page and the selection logic
    must be re-checked against it.
    """
    blocks = _INDEX_DOCUMENT_RE.findall(text)
    if not blocks:
        raise FixtureError("index-headers page lists no <DOCUMENT> blocks")
    documents: list[IndexDocument] = []
    for block in blocks:
        document_type = _INDEX_TYPE_RE.search(block)
        sequence = _INDEX_SEQUENCE_RE.search(block)
        filename = _INDEX_FILENAME_RE.search(block)
        if document_type is None or sequence is None or filename is None:
            raise FixtureError(
                f"index-headers <DOCUMENT> block lacks TYPE, SEQUENCE or FILENAME: {block[:120]!r}"
            )
        documents.append(
            IndexDocument(
                document_type=document_type.group(1).strip(),
                sequence=int(sequence.group(1)),
                filename=filename.group(1).strip(),
            )
        )
    return sorted(documents, key=lambda document: document.sequence)


def _check_filing_kind(cik: str, accession: str, row: dict[str, Any]) -> None:
    """Assert a selected filing matches the category it was selected for."""
    items = {item.strip() for item in (row.get("items") or "").split(",") if item.strip()}
    form = row.get("form")
    if accession in TEXT_ITEM_2_02.get(cik, ()):
        if form not in EVENT_FORMS or "2.02" not in items:
            raise FixtureError(
                f"{accession} is selected for Item 2.02 but the submissions row is "
                f"form {form!r} with items {row.get('items')!r}"
            )
    elif accession in TEXT_OTHER_8KS.get(cik, ()):
        if form != "8-K" or "2.02" in items:
            raise FixtureError(
                f"{accession} is selected as a non-2.02 8-K but the submissions row is "
                f"form {form!r} with items {row.get('items')!r}"
            )
    elif form != "8-K/A":
        raise FixtureError(f"{accession} is selected as the 8-K/A but its form is {form!r}")


def build_text(*, agent: str, limiter: RateLimiter, root: Path | None = None) -> dict[str, Any]:
    """Fetch and store the 8-K text fixtures in the merged SEC cache.

    For every pair from :func:`text_selection` the tool fetches the
    ``-index-headers.html`` page, checks the selected filing's form and items
    against the stored submissions fixtures, then fetches the primary document
    (the index type equal to the form, whose filename must equal the
    submissions ``primaryDocument``) and every ``EX-99.1`` the page lists.
    Bodies are stored unmodified under the decision A-30 layout, and the
    ``filing_index`` and ``documents`` rows of the cache manifest are rewritten
    from scratch while every other endpoint stays, so a rerun reproduces those
    two endpoints (only ``fetched_at`` names change) without touching the rest
    of the merged tree (decision A-44). ``root`` is the cache root and defaults
    to :data:`FIXTURE_ROOT`, resolved at call time. Returns per-issuer counts.
    """
    cache_root = FIXTURE_ROOT if root is None else root
    manifest = Manifest(load_manifest_rows(cache_root), root=cache_root)
    manifest.discard(TEXT_ENDPOINTS)
    raw_dir = cache_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    index = read_submissions_index(raw_dir)
    summary: dict[str, Any] = {"issuers": {}, "filings": 0, "documents": 0, "exhibits": 0}

    for cik, accession in text_selection():
        entry = index.get(cik, {}).get(accession)
        if entry is None:
            raise FixtureError(f"{accession} is not in the submissions fixtures for {cik}")
        row, _source = entry
        _check_filing_kind(cik, accession, row)

        url = filing_index_url(cik, accession)
        status, body = _fetch(url, agent=agent, limiter=limiter)
        if status != 200 or not body:
            raise FixtureError(f"missing index-headers for {accession} (HTTP {status})")
        manifest.store(
            endpoint="filing_index",
            cik=cik,
            key=accession,
            suffix=".html",
            url=url,
            body=body,
            http_status=status,
            trimmed=False,
        )
        documents = parse_index_documents(body.decode("utf-8", "replace"))
        primary = [document for document in documents if document.document_type == row["form"]]
        if len(primary) != 1:
            raise FixtureError(
                f"{accession}: expected one {row['form']} document in the index, "
                f"found {len(primary)}"
            )
        primary_document = row.get("primaryDocument")
        if primary_document and primary[0].filename != primary_document:
            raise FixtureError(
                f"{accession}: index primary {primary[0].filename!r} does not match "
                f"submissions primaryDocument {primary_document!r}"
            )
        exhibits = [document for document in documents if document.document_type == "EX-99.1"]
        if accession in TEXT_ITEM_2_02.get(cik, ()) and not exhibits:
            raise FixtureError(f"{accession} has Item 2.02 but the index lists no EX-99.1")

        issuer = summary["issuers"].setdefault(cik, {"filings": 0, "documents": 0, "exhibits": 0})
        issuer["filings"] += 1
        summary["filings"] += 1
        for document in [primary[0], *exhibits]:
            if not _FILENAME_RE.fullmatch(document.filename):
                raise FixtureError(
                    f"{accession}: unexpected document filename {document.filename!r}"
                )
            suffix = Path(document.filename).suffix
            if not suffix:
                raise FixtureError(f"{accession}: document {document.filename!r} has no extension")
            document_body_status, document_body = _fetch(
                document_url(cik, accession, document.filename),
                agent=agent,
                limiter=limiter,
            )
            if document_body_status != 200 or not document_body:
                raise FixtureError(
                    f"missing document {document.filename} for {accession} "
                    f"(HTTP {document_body_status})"
                )
            manifest.store(
                endpoint="documents",
                cik=cik,
                key=f"{accession}__{document.filename}",
                suffix=suffix,
                url=document_url(cik, accession, document.filename),
                body=document_body,
                http_status=document_body_status,
                trimmed=False,
            )
            issuer["documents"] += 1
            summary["documents"] += 1
            if document.document_type == "EX-99.1":
                issuer["exhibits"] += 1
                summary["exhibits"] += 1
        print(f"[{cik}] {accession} stored ({len(exhibits)} EX-99.1)", flush=True)

    manifest.write()
    return summary


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def header_acceptance(cik: str, accession: str) -> datetime:
    """Parse a stored SGML header's acceptance time (New York) as UTC."""
    paths = sorted((RAW_DIR / "headers" / cik / accession).glob("*.hdr.sgml"))
    if len(paths) != 1:
        raise FixtureError(f"expected one header for {cik}/{accession}, found {len(paths)}")
    match = _ACCEPTANCE_RE.search(paths[0].read_text(encoding="utf-8", errors="replace"))
    if match is None:
        raise FixtureError(f"header {cik}/{accession} has no ACCEPTANCE-DATETIME")
    stamp = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    return stamp.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


def read_submissions_index(
    raw_dir: Path | None = None,
) -> dict[str, dict[str, tuple[dict[str, Any], str]]]:
    """Map each CIK and accession to its submissions row and source document.

    ``raw_dir`` is the cache's ``raw`` directory; it defaults to
    :data:`RAW_DIR`, resolved at call time.
    """
    directory = RAW_DIR if raw_dir is None else raw_dir
    index: dict[str, dict[str, tuple[dict[str, Any], str]]] = {}
    for cik, _ in ISSUERS:
        entries: dict[str, tuple[dict[str, Any], str]] = {}
        for path in sorted((directory / "submissions" / cik).glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            for accession, row in submissions_rows(document).items():
                entries[accession] = (row, "recent")
        for path in sorted((directory / "submissions_page" / cik).glob("*/*.json")):
            source = f"{path.parent.name}.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            for accession, row in submissions_rows(document).items():
                entries.setdefault(accession, (row, source))
        index[cik] = entries
    return index


def read_companyfacts(cik: str) -> dict[str, Any]:
    paths = sorted((RAW_DIR / "companyfacts" / cik).glob("*.json"))
    if len(paths) != 1:
        raise FixtureError(f"expected one companyfacts file for {cik}, found {len(paths)}")
    return json.loads(paths[0].read_text(encoding="utf-8"))


def companyfact_rows(cik: str) -> list[dict[str, Any]]:
    """Flatten a fixture companyfacts document into ``facts_raw``-shaped rows."""
    document = read_companyfacts(cik)
    rows: list[dict[str, Any]] = []
    for taxonomy, concepts in document["facts"].items():
        for concept, entry in concepts.items():
            for unit, facts in entry["units"].items():
                for fact in facts:
                    rows.append(
                        {
                            "cik": cik,
                            "taxonomy": taxonomy,
                            "concept": concept,
                            "unit": unit,
                            "value": float(fact["val"]),
                            "period_start": fact.get("start"),
                            "period_end": fact["end"],
                            "fy": fact.get("fy"),
                            "fp": fact.get("fp"),
                            "form": fact["form"],
                            "accession": fact["accn"],
                            "filed": fact["filed"],
                            "frame": fact.get("frame"),
                        }
                    )
    return rows


def _fact_matches_period(fact: dict[str, Any], period: tuple[str, str]) -> bool:
    start, end = period
    if fact["period_start"] is None:
        return fact["period_end"] == end
    return fact["period_start"] == start and fact["period_end"] == end


def select_staging_facts() -> list[dict[str, Any]]:
    """Select the staging facts for the canonical fields and periods.

    Within a ``(cik, field, period, taxonomy)`` group the rows of the
    lowest-ranked concept present in the fixtures are kept.  A period whose
    values never change keeps only the earliest version (companyfacts repeats
    it in later filings as a comparative); a period with more than one value
    keeps every version, so the RY FY2023 revenue restatement arrives with
    both 56,129,000,000 and 51,464,000,000.  One deliberately unmapped
    concept per issuer is added for the same periods.
    """
    from edgarpit.concepts import load_concept_map

    concept_map = load_concept_map()
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    unmapped: list[dict[str, Any]] = []
    for cik, _ in ISSUERS:
        taxonomy_want, concept_want = UNMAPPED_SELECTION[cik]
        for fact in companyfact_rows(cik):
            periods = STAGING_PERIODS[cik].values()
            if fact["taxonomy"] == taxonomy_want and fact["concept"] == concept_want:
                if any(_fact_matches_period(fact, period) for period in periods):
                    unmapped.append(fact)
                continue
            mapped = concept_map.lookup(fact["taxonomy"], fact["concept"])
            if mapped is None or mapped[0] not in STAGING_FIELDS:
                continue
            field = mapped[0]
            for label, period in STAGING_PERIODS[cik].items():
                if _fact_matches_period(fact, period):
                    grouped.setdefault((cik, field, label, fact["taxonomy"]), []).append(fact)
    selected: list[dict[str, Any]] = []
    for (_cik, _field, _label, taxonomy), rows in grouped.items():
        ranks = {concept_map.lookup(taxonomy, row["concept"])[1] for row in rows}
        best = min(ranks)
        selected.extend(
            row for row in rows if concept_map.lookup(taxonomy, row["concept"])[1] == best
        )
    return drop_repeat_comparatives(dedupe_facts(selected + unmapped))


def dedupe_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop exact duplicate companyfacts entries (decision A-7)."""
    seen: set[tuple[Any, ...]] = set()
    kept: list[dict[str, Any]] = []
    for fact in sorted(facts, key=fact_sort_key):
        key = (
            fact["cik"],
            fact["taxonomy"],
            fact["concept"],
            fact["unit"],
            fact["value"],
            fact["period_start"],
            fact["period_end"],
            fact["accession"],
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(fact)
    return kept


def drop_repeat_comparatives(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one row per unchanged period; every version of a changed one.

    Companyfacts repeats a period's numbers in every later filing that uses
    them as a comparative.  A period whose values never change contributes
    only its earliest row; once a period has more than one value all of its
    versions stay, so a restated value keeps its full history.
    """
    groups: dict[tuple[str, str, str, str, str | None, str], list[dict[str, Any]]] = {}
    for fact in sorted(facts, key=fact_sort_key):
        key = (
            fact["cik"],
            fact["taxonomy"],
            fact["concept"],
            fact["unit"],
            fact["period_start"],
            fact["period_end"],
        )
        groups.setdefault(key, []).append(fact)
    kept: list[dict[str, Any]] = []
    for rows in groups.values():
        if len({row["value"] for row in rows}) > 1:
            kept.extend(rows)
        else:
            kept.append(min(rows, key=lambda row: (row["filed"], row["accession"])))
    return sorted(kept, key=fact_sort_key)


def fact_sort_key(fact: dict[str, Any]) -> tuple[Any, ...]:
    return (
        fact["cik"],
        fact["taxonomy"],
        fact["concept"],
        fact["period_start"] or "",
        fact["period_end"],
        fact["accession"],
        fact["value"],
    )


def select_staging_events(
    index: dict[str, dict[str, tuple[dict[str, Any], str]]],
) -> list[dict[str, Any]]:
    """Explode the items of the most recent Apple 8-Ks (labels are ``unknown``)."""
    apple = [
        (accession, row)
        for accession, (row, _source) in index[APPLE_CIK].items()
        if row["form"] in EVENT_FORMS
    ]
    apple.sort(key=lambda pair: (pair[1]["filingDate"], pair[0]), reverse=True)
    events: list[dict[str, Any]] = []
    chosen = 0
    for accession, row in apple:
        items = (row.get("items") or "").split(",")
        codes = [code.strip() for code in items if code.strip()]
        codes = list(dict.fromkeys(codes))
        if not codes:
            continue
        acceptance = iso_utc(header_acceptance(APPLE_CIK, accession))
        for code in codes:
            events.append(
                {
                    "cik": APPLE_CIK,
                    "accession": accession,
                    "form": row["form"],
                    "acceptance_ts": acceptance,
                    "ts_imputed": "false",
                    "item_code": code,
                    "item_label": "unknown",
                }
            )
        chosen += 1
        if chosen == APPLE_8K_COUNT:
            break
    if chosen < APPLE_8K_COUNT:
        raise FixtureError(f"only {chosen} Apple 8-Ks with items in the fixtures")
    events.sort(key=lambda event: (event["cik"], event["accession"], event["item_code"]))
    return events


def select_staging_filings(
    index: dict[str, dict[str, tuple[dict[str, Any], str]]],
    facts: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the filings rows for every accession the staging tables reference."""
    accessions_by_cik: dict[str, set[str]] = {}
    for fact in facts:
        accessions_by_cik.setdefault(fact["cik"], set()).add(fact["accession"])
    for event in events:
        accessions_by_cik.setdefault(event["cik"], set()).add(event["accession"])
    filings: list[dict[str, Any]] = []
    for cik in sorted(accessions_by_cik):
        for accession in sorted(accessions_by_cik[cik]):
            entry = index[cik].get(accession)
            if entry is None:
                raise FixtureError(f"no submissions row for {cik}/{accession}")
            row, source = entry
            acceptance_api = row.get("acceptanceDateTime") or None
            filings.append(
                {
                    "cik": cik,
                    "accession": accession,
                    "form": row["form"],
                    "filing_date": row["filingDate"],
                    "report_date": row.get("reportDate") or None,
                    "acceptance_ts": iso_utc(header_acceptance(cik, accession)),
                    "acceptance_ts_source": "sgml_header",
                    "acceptance_ts_api": str(acceptance_api) if acceptance_api else None,
                    "ts_imputed": "false",
                    "primary_document": row.get("primaryDocument") or None,
                    "items": row.get("items") or None,
                    "is_amendment": str(row["form"].endswith("/A")).lower(),
                    "is_xbrl": str(bool(row.get("isXBRL") or row.get("isInlineXBRL"))).lower(),
                    "source": source,
                }
            )
    return filings


def write_staging_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow(
                ["" if row.get(column) is None else str(row[column]) for column in columns]
            )


def derive_expected_asof(
    facts: list[dict[str, Any]], filings: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build expected_asof.json from the selected staging rows, never by hand."""
    from edgarpit.concepts import load_concept_map

    concept_map = load_concept_map()
    cik = EXPECTED_ASOF["cik"]
    accession = EXPECTED_ASOF["accession"]
    candidates = [
        fact
        for fact in facts
        if fact["cik"] == cik
        and fact["accession"] == accession
        and fact["period_end"] == "2023-10-31"
        and (concept_map.lookup(fact["taxonomy"], fact["concept"]) or ("", 0))[0] == "revenue"
    ]
    if len(candidates) != 1:
        raise FixtureError(
            f"expected exactly one revenue row for {cik}/{accession}, found {len(candidates)}"
        )
    fact = candidates[0]
    filing = next((row for row in filings if row["accession"] == accession), None)
    if filing is None:
        raise FixtureError(f"no staging filings row for {accession}")
    value: float | int = fact["value"]
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    unit = fact["unit"]
    return {
        "cik": cik,
        "field": "revenue",
        "asof": EXPECTED_ASOF["asof"],
        "period": EXPECTED_ASOF["period"],
        "value": value,
        "unit": unit,
        "currency": unit if re.fullmatch(r"[A-Z]{3}", unit) else None,
        "period_start": fact["period_start"],
        "period_end": fact["period_end"],
        "accession": accession,
        "acceptance_ts": filing["acceptance_ts"],
    }


def build_staging() -> dict[str, int]:
    """Write the staging CSVs and ``expected_asof.json`` from the fixture tree."""
    try:
        from edgarpit.schemas import COLUMNS
    except ImportError as error:  # pragma: no cover - environment guidance
        raise FixtureError(
            "--staging needs the project environment: "
            "run `uv run python tools/build_fixtures.py --staging`"
        ) from error
    index = read_submissions_index()
    facts = select_staging_facts()
    events = select_staging_events(index)
    filings = select_staging_filings(index, facts, events)
    write_staging_csv(STAGING_DIR / "filings.csv", COLUMNS["filings"], filings)
    write_staging_csv(STAGING_DIR / "facts_raw.csv", COLUMNS["facts_raw"], facts)
    write_staging_csv(STAGING_DIR / "events_8k.csv", COLUMNS["events_8k"], events)
    expected = derive_expected_asof(facts, filings)
    EXPECTED_ASOF_PATH.write_text(
        json.dumps(expected, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return {
        "filings": len(filings),
        "facts_raw": len(facts),
        "events_8k": len(events),
        "total": len(filings) + len(facts) + len(events),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download and trim the SEC fixtures, or rebuild the staging CSVs."
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--staging",
        action="store_true",
        help="write tests/fixtures/staging/*.csv and expected_asof.json from the fixture tree",
    )
    modes.add_argument(
        "--company-tickers",
        action="store_true",
        help="refresh only raw/company_tickers/_/<fetched_at>.json (decision A-17)",
    )
    modes.add_argument(
        "--text",
        action="store_true",
        help="fetch the 8-K index, primary documents and EX-99.1 files into "
        "tests/fixtures/sec/raw (decisions A-30, A-44)",
    )
    args = parser.parse_args(argv)
    if args.staging:
        try:
            counts = build_staging()
        except FixtureError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(json.dumps(counts, indent=2))
        print(f"staging written to {STAGING_DIR}")
        return 0
    try:
        agent = validate_user_agent(find_user_agent())
    except FixtureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    limiter = RateLimiter(RATE_LIMIT_RPS)
    if args.company_tickers:
        try:
            summary_tickers = build_company_tickers(agent=agent, limiter=limiter)
        except FixtureError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(json.dumps(summary_tickers, indent=2))
        print(f"company tickers written to {FIXTURE_ROOT}")
        return 0
    if args.text:
        try:
            text_summary = build_text(agent=agent, limiter=limiter)
        except FixtureError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        text_bytes = sum(
            path.stat().st_size
            for endpoint in TEXT_ENDPOINTS
            for path in (RAW_DIR / endpoint).rglob("*")
            if path.is_file()
        )
        text_summary["fixture_bytes"] = text_bytes
        print(json.dumps(text_summary, indent=2))
        print(f"text fixtures written to {RAW_DIR} ({text_bytes / 1e6:.2f} MB)")
        return 0
    try:
        summary = build(agent=agent, limiter=limiter)
    except FixtureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    total_bytes = sum(path.stat().st_size for path in FIXTURE_ROOT.rglob("*") if path.is_file())
    summary["fixture_bytes"] = total_bytes
    print(json.dumps(summary, indent=2))
    print(f"fixtures written to {FIXTURE_ROOT} ({total_bytes / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
