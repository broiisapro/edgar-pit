#!/usr/bin/env python3
"""Build ``config/universe_large.csv`` from SEC sources (decision A-33).

The file holds the large Phase 2 universe: every Canadian issuer that the SEC
frames API exposes and that can be resolved to a US ticker, plus a US
large-cap list ranked by public float. It is produced from SEC data only:

- ``https://www.sec.gov/files/company_tickers.json`` resolves a CIK to its
  tickers (the same document ``edgarpit universe resolve`` uses).
- ``https://data.sec.gov/api/xbrl/frames/dei/EntityPublicFloat/USD/<period>.json``
  ranks US issuers by public float in US dollars.
- ``https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/<period>.json``
  and the float frames above list the Canadian issuers, whose entries carry
  ``loc`` ``CA-*``. The ``ifrs-full`` ``Assets`` frames named by decision A-33
  are probed too; the SEC currently serves none (404 for every period), so they
  contribute nothing but are retried on each run.
- ``https://data.sec.gov/api/xbrl/companyconcept/CIK<10 digits>/dei/EntityPublicFloat.json``
  supplies each top US candidate's float history, which is how the builder
  detects and undoes the power-of-ten scale errors some filers started
  reporting in 2022 (see :func:`adjusted_float`).
- ``https://data.sec.gov/submissions/CIK<10 digits>.json`` supplies the entity
  name, the listing and address, and the filing history that proves the issuer
  filed a 10-K, 40-F or 20-F in the last two years.

Frames are calendar-quarter snapshots. A single quarter is a partial list (a
float fact is tagged once a year, a cover fact once a year for 40-F filers), so
the builder reads the :data:`FRAME_QUARTERS` quarters ending with the quarter
that contains ``--as-of`` and keeps each issuer's newest value. Frames carry no
acceptance time, so nothing here decides what was known when: this is a static
issuer list for a fetch run, not a point-in-time table.

Selection rules, in order:

1. Seed: every row of ``config/universe.csv`` is kept unchanged, so the large
   universe is always a superset of the Phase 1 universe.
2. Canadian: every issuer with ``loc`` ``CA-*`` in a cover or float frame whose
   submissions address is Canadian (or unknown, falling back to the frame
   location), that resolves to a US ticker, and that filed a 10-K, 40-F or 20-F
   in the last :data:`ANNUAL_WINDOW_DAYS` days at ``--as-of``.
3. US: issuers with ``loc`` ``US-*`` in the float frames, ranked by the newest
   float value (ties broken by accession then CIK), filtered by the same annual
   report rule, added until the file holds ``--target-total`` rows. When the
   Canadian set already reaches that target, at least :data:`MIN_US` US issuers
   are kept so the US large-cap list is never empty.
4. Excluded everywhere: funds, commodity trusts and blank-check companies
   (:data:`EXCLUDED_SIC`), issuers without a plain ticker in
   ``company_tickers.json``, and one row per CIK (a second share class of the
   same CIK never adds a row).

The SEC serves no ``ifrs-full`` frames (every taxonomy/tag/unit/period probed
returns 404), and many MJDS filers tag no ``dei`` cover fact at all, so a
40-F filer that neither tags ``dei`` nor appears in ``config/universe.csv``
cannot be discovered from frames. The build reports how many such issuers it
cannot see; a per-CIK submissions sweep is the documented follow-up.

Responses are cached immutably under ``--cache`` (never committed): frames and
submissions at <= 5 requests/second through
:class:`edgarpit.sec_client.SecClient`, stamped files plus ``raw/manifest.jsonl``
rows. Re-running with the same cache and ``--as-of`` writes the same file;
``--offline`` reads the cache only and needs no User-Agent.

Usage::

    export SEC_USER_AGENT='Name email@example.com'
    uv run python tools/build_universe_large.py --cache /tmp/ep-d/universe
    uv run python tools/build_universe_large.py --cache /tmp/ep-d/universe --offline

Exit codes: ``0`` on success, ``2`` on a configuration, cache or SEC data
error (the message names the issuer or URL).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from edgarpit.config import ConfigError, Settings
from edgarpit.rawcache import GLOBAL_CIK, MANIFEST_NAME, RawCache, utc_stamp
from edgarpit.sec_client import (
    DATA_HOST,
    SecClient,
    SecClientError,
    SecNotFound,
    company_tickers_url,
    submissions_page_url,
    submissions_url,
)
from edgarpit.universe import (
    Issuer,
    UniverseError,
    load_universe,
    ticker_map,
    write_universe,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "config" / "universe_large.csv"
SMALL_UNIVERSE = REPO_ROOT / "config" / "universe.csv"

FRAMES_PATH = "/api/xbrl/frames"
CONCEPTS_PATH = "/api/xbrl/companyconcept"
FLOAT_SPEC = ("dei", "EntityPublicFloat", "USD")
COVER_SPEC = ("dei", "EntityCommonStockSharesOutstanding", "shares")
# A-33 also names ifrs-full balance-sheet frames as a Canadian source. The SEC
# frames API serves no ifrs-full frames today (every period returns 404), so
# these are probed on every run and simply contribute nothing until it does.
IFRS_SPECS = (("ifrs-full", "Assets", "CAD"), ("ifrs-full", "Assets", "USD"))
CANADIAN_SPECS = (COVER_SPEC, FLOAT_SPEC, *IFRS_SPECS)

FRAME_QUARTERS = 8
TARGET_TOTAL = 250
MIN_US = 25
ANNUAL_FORMS = ("10-K", "40-F", "20-F")
ANNUAL_WINDOW_DAYS = 730

# Some filers report dei:EntityPublicFloat scaled by a power of ten (a factor
# of 1,000 or 1,000,000 shows up in the frames API from 2022 on). A jump of
# this size between two consecutive observations marks the scale error; the
# newest value is divided by that power of ten. A value still above
# MAX_FLOAT_USD after the correction is treated as unusable.
SCALE_JUMP_RATIO = 100.0
MAX_FLOAT_USD = 5_000_000_000_000.0
US_PROBE_FLOOR = 60

# EDGAR state/country codes for the Canadian provinces and territories plus
# the federal code; the address text ("Ontario, Canada") is checked first.
CANADIAN_CODES = frozenset(
    {"A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "B0", "B1", "B2", "Z4"}
)

# Blank-check companies and investment/commodity trusts, by SIC code: they
# file annual reports but hold no operating business.
EXCLUDED_SIC = frozenset({"6221", "6722", "6726", "6733", "6770"})

TICKER_RE = re.compile(r"[A-Z][A-Z0-9.]*")
STAMP_RE = re.compile(r"\d{8}T\d{6}\d{6}Z")
STATE_RE = re.compile(r"[A-Z]{2}")


class UniverseBuildError(RuntimeError):
    """SEC data or the cache cannot produce a universe row."""


# --------------------------------------------------------------------------
# Frame cache
# --------------------------------------------------------------------------


def frames_url(taxonomy: str, tag: str, unit: str, period: str) -> str:
    """URL of one XBRL frame document.

    ``taxonomy`` is e.g. ``dei``, ``tag`` e.g. ``EntityPublicFloat``, ``unit``
    e.g. ``USD`` or ``shares``, and ``period`` an instant frame such as
    ``CY2026Q2I``.
    """
    return f"{DATA_HOST}{FRAMES_PATH}/{taxonomy}/{tag}/{unit}/{period}.json"


def frame_key(taxonomy: str, tag: str, unit: str, period: str) -> str:
    """Cache key of a frame: ``<taxonomy>__<tag>__<unit>__<period>``."""
    return f"{taxonomy}__{tag}__{unit}__{period}"


class DocumentStore:
    """Immutable store for tool documents under ``<root>/raw/universe/_/``.

    :class:`edgarpit.rawcache.RawCache` classifies URLs by endpoint and has no
    ``frames`` or ``companyconcept`` endpoint yet, so the documents this tool
    needs are stored here in the same shape it would write:
    ``raw/universe/_/<key>/<stamp>.json`` with a ``raw/manifest.jsonl`` row
    (``endpoint='universe'``, ``cik=''``, SHA-256 and byte size of the stored
    body, ``http_status``) and an exclusive create that never overwrites.
    Bodies are stored byte-for-byte as received after HTTP content decoding;
    readers take the newest stamp per key.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _directory(self, key: str) -> Path:
        return self.root / "raw" / "universe" / GLOBAL_CIK / key

    def latest(self, key: str) -> dict[str, Any] | None:
        """Return the newest stored document for ``key``, or ``None``."""
        directory = self._directory(key)
        if not directory.is_dir():
            return None
        files = sorted(path for path in directory.glob("*.json") if STAMP_RE.fullmatch(path.stem))
        if not files:
            return None
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise UniverseBuildError(f"cached document {files[-1]} is not a JSON object")
        return payload

    def write(self, key: str, body: bytes, *, url: str, http_status: int) -> Path:
        """Store ``body`` under a fresh stamp and append the manifest row."""
        stamp = utc_stamp()
        final = self._directory(key) / f"{stamp}.json"
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f"{final.name}.tmp")
        tmp.write_bytes(body)
        try:
            os.link(tmp, final)
        except FileExistsError:
            raise FileExistsError(f"refusing to overwrite cached document {final}") from None
        finally:
            tmp.unlink(missing_ok=True)
        row = {
            "path": final.relative_to(self.root).as_posix(),
            "url": url,
            "endpoint": "universe",
            "cik": "",
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "fetched_at": stamp,
            "http_status": http_status,
        }
        manifest = self.root / "raw" / MANIFEST_NAME
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return final


# --------------------------------------------------------------------------
# Frames and companyconcept documents
# --------------------------------------------------------------------------


def companyconcept_url(cik: str, taxonomy: str, tag: str) -> str:
    """URL of a company-concept document (all facts of one tag for one CIK)."""
    return f"{DATA_HOST}{CONCEPTS_PATH}/CIK{cik}/{taxonomy}/{tag}.json"


def read_document(
    client: SecClient,
    store: DocumentStore,
    key: str,
    url: str,
    *,
    offline: bool,
    counter: list[int],
) -> dict[str, Any] | None:
    """Read one JSON document from the cache or SEC; ``None`` on HTTP 404.

    Online mode fetches through ``client`` and stores the body in ``store``;
    offline mode reads the cache only. ``counter`` is incremented once per
    network request (never for cache reads). Raises
    :class:`UniverseBuildError` when a body is not a JSON object.
    """
    if offline:
        return store.latest(key)
    counter[0] += 1
    try:
        text = client.get_text(url)
    except SecNotFound:
        return None
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise UniverseBuildError(f"document {url} is not a JSON object")
    store.write(key, text.encode("utf-8"), url=url, http_status=200)
    return payload


def frame_periods(as_of: date, count: int = FRAME_QUARTERS) -> list[str]:
    """Instant frame periods ending with ``as_of``'s quarter, newest first.

    ``as_of`` is a calendar date (no timezone). The result holds ``count``
    entries such as ``CY2026Q4I``, one per calendar quarter. The current
    quarter may not be published yet; :func:`read_frame` treats a 404 as an
    empty frame.
    """
    index = as_of.year * 4 + (as_of.month - 1) // 3
    periods: list[str] = []
    for offset in range(count):
        quarter = index - offset
        periods.append(f"CY{quarter // 4}Q{quarter % 4 + 1}I")
    return periods


@dataclass(frozen=True)
class FrameRow:
    """One fact of a frame document.

    Attributes:
        cik: 10-character zero-padded CIK.
        name: entity name as written in the frame.
        loc: business location as ``<country>-<state>`` (e.g. ``US-CA``,
            ``CA-ON``), possibly empty or absent.
        end: period end date as an ISO ``YYYY-MM-DD`` string.
        value: the reported value in the frame's unit (USD or shares).
        accession: accession of the filing that carried the fact.
    """

    cik: str
    name: str
    loc: str
    end: str
    value: float
    accession: str


def parse_frame(document: Mapping[str, Any], period: str) -> list[FrameRow]:
    """Parse the ``data`` array of a frame document into :class:`FrameRow`.

    ``period`` names the frame for error messages. Rows are returned in
    document order; a row without an integer ``cik``, a string ``end`` or a
    numeric ``val`` raises :class:`UniverseBuildError` because a malformed
    frame would silently shrink the universe.
    """
    data = document.get("data")
    if not isinstance(data, list):
        raise UniverseBuildError(f"frame {period} has no data array")
    rows: list[FrameRow] = []
    for entry in data:
        if not isinstance(entry, Mapping):
            raise UniverseBuildError(f"frame {period} has a non-object data row")
        cik = entry.get("cik")
        end = entry.get("end")
        value = entry.get("val")
        if isinstance(cik, bool) or not isinstance(cik, int) or not 0 < cik <= 9_999_999_999:
            raise UniverseBuildError(f"frame {period} has an invalid cik {cik!r}")
        if not isinstance(end, str) or not end:
            raise UniverseBuildError(f"frame {period} row {cik} has no end date")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise UniverseBuildError(f"frame {period} row {cik} has no numeric val")
        loc = entry.get("loc")
        name = entry.get("entityName")
        accession = entry.get("accn")
        rows.append(
            FrameRow(
                cik=f"{cik:010d}",
                name=name if isinstance(name, str) else "",
                loc=loc if isinstance(loc, str) else "",
                end=end,
                value=float(value),
                accession=accession if isinstance(accession, str) else "",
            )
        )
    return rows


def latest_by_cik(rows: Iterable[FrameRow]) -> dict[str, FrameRow]:
    """Keep each CIK's newest row by ``(end, accession)``.

    The newest period wins; an equal period is broken by the larger accession
    so the choice never depends on document order. Returns a mapping from
    10-character CIK to row.
    """
    latest: dict[str, FrameRow] = {}
    for row in rows:
        current = latest.get(row.cik)
        if current is None or (row.end, row.accession) > (current.end, current.accession):
            latest[row.cik] = row
    return latest


def float_history(document: Mapping[str, Any], source: str) -> list[tuple[date, float]]:
    """The USD ``dei:EntityPublicFloat`` observations of a companyconcept doc.

    ``source`` labels the document in error messages. Returns ``(period_end,
    value)`` pairs sorted by date; ``value`` is in US dollars. Raises
    :class:`UniverseBuildError` on a malformed row, because a silently skipped
    row could hide the scale jump the caller looks for.
    """
    units = document.get("units")
    if not isinstance(units, Mapping):
        raise UniverseBuildError(f"{source} has no units object")
    rows = units.get("USD")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise UniverseBuildError(f"{source} has a non-array USD unit")
    history: list[tuple[date, float]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise UniverseBuildError(f"{source} has a non-object fact")
        end = row.get("end")
        value = row.get("val")
        if not isinstance(end, str) or not end:
            raise UniverseBuildError(f"{source} has a fact without an end date")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise UniverseBuildError(f"{source} has a fact without a numeric val")
        try:
            history.append((date.fromisoformat(end), float(value)))
        except ValueError as error:
            raise UniverseBuildError(f"{source} has a malformed end date {end!r}") from error
    return sorted(history)


def adjusted_float(value: float, history: Sequence[tuple[date, float]]) -> float:
    """Undo a power-of-ten scale error in the newest public-float value.

    ``value`` is the newest ``dei:EntityPublicFloat`` in USD and ``history``
    the issuer's observations from ``companyconcept``. Some filers report the
    float scaled by 1,000 or 1,000,000 from 2022 on, which shows up as a jump
    of at least :data:`SCALE_JUMP_RATIO` between two consecutive observations.
    When such a jump exists, the value immediately before it is the baseline;
    the returned value is ``value`` divided (or multiplied) by the nearest
    power of ten that brings it back to the baseline's scale, but only for
    factors of 1,000 to 1,000,000 (a real company does not grow that fast
    between two annual reports). Without a jump, or without a positive
    baseline, the value is returned unchanged.
    """
    previous: float | None = None
    baseline: float | None = None
    for _, current in sorted(history):
        if previous is not None and previous > 0 and current > 0:
            ratio = current / previous
            if ratio >= SCALE_JUMP_RATIO or ratio <= 1.0 / SCALE_JUMP_RATIO:
                baseline = previous
                break
        previous = current
    if baseline is None or baseline <= 0 or value <= 0:
        return value
    ratio = value / baseline
    if ratio <= 0:
        return value
    factor = round(math.log10(ratio)) if ratio >= 1 else -round(math.log10(1.0 / ratio))
    if 3 <= abs(factor) <= 6:
        return value / (10.0**factor)
    return value


def read_frame(
    client: SecClient,
    store: DocumentStore,
    spec: tuple[str, str, str],
    period: str,
    *,
    offline: bool,
    counter: list[int],
) -> list[FrameRow]:
    """Read one frame from the cache or SEC; ``[]`` when the frame is absent.

    Online mode fetches through ``client`` (rate limiter, retries, User-Agent)
    and stores the body in ``store``; a 404 means the quarter has no frame and
    is not an error. Offline mode reads the cache only and returns ``[]`` for a
    frame that was never stored. ``counter`` is incremented once per network
    request, so the caller can report the run's request count.
    """
    taxonomy, tag, unit = spec
    payload = read_document(
        client,
        store,
        frame_key(taxonomy, tag, unit, period),
        frames_url(taxonomy, tag, unit, period),
        offline=offline,
        counter=counter,
    )
    if payload is None:
        return []
    return parse_frame(payload, period)


# --------------------------------------------------------------------------
# Submissions
# --------------------------------------------------------------------------


def address_country(addresses: Mapping[str, Any] | None) -> str | None:
    """Classify a submissions address as ``CA``, ``US`` or ``None``.

    Checks the business address first and the mailing address second. A
    description or country mentioning Canada (or an EDGAR Canadian province
    code ``A0``-``A9``/``B0``-``B2``/``Z4``) is ``CA``; a non-foreign
    two-letter state code is ``US``; any other foreign address (including a
    missing one) is ``None``.
    """
    if not isinstance(addresses, Mapping):
        return None
    for section in ("business", "mailing"):
        block = addresses.get(section)
        if not isinstance(block, Mapping):
            continue
        code = block.get("stateOrCountry") or block.get("countryCode")
        description = " ".join(
            str(block.get(key) or "") for key in ("stateOrCountryDescription", "country")
        ).lower()
        if "canada" in description:
            return "CA"
        if isinstance(code, str) and code in CANADIAN_CODES:
            return "CA"
        is_state = isinstance(code, str) and STATE_RE.fullmatch(code) is not None
        if is_state and block.get("isForeignLocation") != 1:
            return "US"
    return None


def annual_filer_type(filings: Iterable[tuple[str, date]], cutoff: date) -> str | None:
    """The annual form filed on or after ``cutoff``, or ``None``.

    ``filings`` holds ``(form, filing_date)`` pairs; amendments such as
    ``10-K/A`` are ignored. When several annual forms qualify, the latest
    filing date wins and a tie prefers ``10-K``, then ``40-F``, then ``20-F``
    (the order of :data:`ANNUAL_FORMS`). Returns the base form, which becomes
    the universe row's ``filer_type``.
    """
    order = {form: rank for rank, form in enumerate(ANNUAL_FORMS)}
    best: tuple[date, int] | None = None
    chosen: str | None = None
    for form, filed in filings:
        rank = order.get(form)
        if rank is None or filed < cutoff:
            continue
        key = (filed, -rank)
        if best is None or key > best:
            best = key
            chosen = form
    return chosen


def primary_ticker(submission_tickers: Sequence[str], mapped_tickers: Sequence[str]) -> str | None:
    """Pick one plain ticker for a CIK, or ``None``.

    ``submission_tickers`` is the ``tickers`` array of the submissions
    document, which the SEC lists with the primary listing first; the first
    well-formed entry wins. When that array is empty, ``mapped_tickers`` (the
    tickers ``company_tickers.json`` lists for the CIK, sorted) supplies the
    first entry without a class/warrant suffix, then the shortest and
    lexicographically smallest one. Preferred-share and warrant symbols such as
    ``ABC-PD`` or ``ABC-WT`` never qualify.
    """
    for ticker in submission_tickers:
        if TICKER_RE.fullmatch(ticker):
            return ticker
    candidates = [ticker for ticker in mapped_tickers if TICKER_RE.fullmatch(ticker)]
    if not candidates:
        return None
    return min(candidates, key=lambda ticker: (len(ticker), ticker))


def sic_excluded(sic: Any) -> bool:
    """True when ``sic`` (a string or int) is a fund, trust or blank check."""
    if sic is None:
        return False
    return str(sic).strip() in EXCLUDED_SIC


def _filing_rows(document: Mapping[str, Any], source: str) -> list[tuple[str, date]]:
    """Every ``(form, filing_date)`` pair of a submissions document or page."""
    filings = document.get("filings")
    arrays: Any = (
        filings["recent"] if isinstance(filings, Mapping) and "recent" in filings else document
    )
    if not isinstance(arrays, Mapping):
        raise UniverseBuildError(f"submissions {source} has no filing arrays")
    forms = arrays.get("form")
    dates = arrays.get("filingDate")
    if not isinstance(forms, list) or not isinstance(dates, list) or len(forms) != len(dates):
        raise UniverseBuildError(f"submissions {source} has malformed filing arrays")
    pairs: list[tuple[str, date]] = []
    for form, text in zip(forms, dates, strict=True):
        if not isinstance(form, str) or not form:
            raise UniverseBuildError(f"submissions {source} has a filing without a form")
        if not isinstance(text, str) or not text:
            raise UniverseBuildError(f"submissions {source} has a filing without a date")
        try:
            pairs.append((form, date.fromisoformat(text)))
        except ValueError as error:
            raise UniverseBuildError(
                f"submissions {source} has a malformed filing date {text!r}"
            ) from error
    return pairs


def _page_urls(document: Mapping[str, Any]) -> list[str]:
    """URLs of the paginated submissions pages, in the document's order."""
    filings = document.get("filings")
    files = filings.get("files") if isinstance(filings, Mapping) else None
    if not isinstance(files, list):
        return []
    urls: list[str] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            raise UniverseBuildError("submissions filings.files has a non-object entry")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise UniverseBuildError("submissions filings.files has an entry without a name")
        urls.append(submissions_page_url(name))
    return urls


def read_filings(
    client: SecClient,
    document: Mapping[str, Any],
    cik: str,
    cutoff: date,
    *,
    offline: bool,
    counter: list[int],
) -> list[tuple[str, date]]:
    """Every filing of one issuer down to ``cutoff``, from recent plus pages.

    The ``filings.recent`` block holds at least the last 1,000 filings and is
    enough for two years for almost every issuer. When its oldest row is newer
    than ``cutoff`` and the document lists pages, the pages are read in order
    until one reaches ``cutoff`` (or they run out), because a missing annual
    report would silently drop the issuer. ``counter`` counts network requests
    (never cache reads in offline mode).
    """
    pairs = _filing_rows(document, f"CIK{cik}")
    if pairs and min(filed for _, filed in pairs) <= cutoff:
        return pairs
    known = {(form, filed) for form, filed in pairs}
    for url in _page_urls(document):
        if not offline:
            counter[0] += 1
        page = client.get_json(url)
        for pair in _filing_rows(page, url):
            if pair not in known:
                known.add(pair)
                pairs.append(pair)
        if pairs and min(filed for _, filed in pairs) <= cutoff:
            break
    return pairs


@dataclass
class SubmissionFacts:
    """What the builder needs from one issuer's submissions document.

    Attributes:
        cik: 10-character zero-padded CIK.
        name: SEC entity name.
        tickers: every ticker in the document, primary first.
        country: ``CA``, ``US`` or ``None`` when the address is foreign/absent.
        filer_type: latest annual form in the window, or ``None``.
        sic: SIC code as a string, or ``None``.
    """

    cik: str
    name: str
    tickers: tuple[str, ...]
    country: str | None
    filer_type: str | None
    sic: str | None


def submission_facts(
    client: SecClient,
    document: Mapping[str, Any],
    cik: str,
    cutoff: date,
    *,
    offline: bool,
    counter: list[int],
) -> SubmissionFacts:
    """Extract name, tickers, country, annual form and SIC for one CIK."""
    name = document.get("name")
    if not isinstance(name, str) or not name.strip():
        raise UniverseBuildError(f"submissions CIK{cik} has no entity name")
    raw_tickers = document.get("tickers")
    tickers: tuple[str, ...] = ()
    if isinstance(raw_tickers, list):
        tickers = tuple(ticker for ticker in raw_tickers if isinstance(ticker, str) and ticker)
    sic = document.get("sic")
    pairs = read_filings(client, document, cik, cutoff, offline=offline, counter=counter)
    return SubmissionFacts(
        cik=cik,
        name=name.strip(),
        tickers=tickers,
        country=address_country(document.get("addresses")),
        filer_type=annual_filer_type(pairs, cutoff),
        sic=str(sic).strip() if sic is not None else None,
    )


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def merge_universe(
    seeded: Sequence[Issuer],
    canadian: Sequence[Issuer],
    us_ranked: Sequence[Issuer],
    *,
    target_total: int,
    min_us: int,
) -> list[Issuer]:
    """Combine seeded, Canadian and US rows into the sorted file.

    Seeded rows always win and keep their fields verbatim. Every Canadian row
    whose CIK is not seeded is added. US rows are added in the given order
    until the file holds ``target_total`` rows, but never fewer than ``min_us``
    when the Canadian rows alone reach the target. A duplicate ticker or CIK
    raises :class:`UniverseError`; the result is sorted by
    ``(country, ticker)``, so the file order never depends on input order.
    """
    rows: list[Issuer] = []
    by_cik: dict[str, Issuer] = {}
    by_ticker: dict[str, Issuer] = {}

    def add(issuer: Issuer) -> bool:
        if issuer.cik is None:
            raise UniverseError(f"issuer {issuer.ticker} has no CIK")
        if issuer.cik in by_cik:
            return False
        existing = by_ticker.get(issuer.ticker)
        if existing is not None:
            raise UniverseError(
                f"ticker {issuer.ticker} is used by both {existing.cik} and {issuer.cik}"
            )
        rows.append(issuer)
        by_cik[issuer.cik] = issuer
        by_ticker[issuer.ticker] = issuer
        return True

    for issuer in seeded:
        add(issuer)
    for issuer in canadian:
        add(issuer)
    slots = max(target_total - len(rows), min_us)
    added = 0
    for issuer in us_ranked:
        if added >= slots:
            break
        if add(issuer):
            added += 1
    return sorted(rows, key=lambda issuer: (issuer.country, issuer.ticker))


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


@dataclass
class BuildReport:
    """Counts and timings the CLI prints and the PR body quotes."""

    periods: list[str] = field(default_factory=list)
    requests: int = 0
    frames_read: int = 0
    frames_missing: int = 0
    seeded: int = 0
    canadian_candidates: int = 0
    canadian_rows: int = 0
    us_candidates: int = 0
    us_rows: int = 0
    rejected_no_ticker: int = 0
    rejected_no_annual: int = 0
    rejected_fund: int = 0
    rejected_not_canadian: int = 0
    rejected_implausible_float: int = 0
    rows: list[Issuer] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def counts_by_country(self) -> dict[str, int]:
        """Number of rows per country, keys sorted."""
        counts: dict[str, int] = defaultdict(int)
        for issuer in self.rows:
            counts[issuer.country] += 1
        return dict(sorted(counts.items()))

    def counts_by_filer_type(self) -> dict[str, int]:
        """Number of rows per annual form, keys sorted."""
        counts: dict[str, int] = defaultdict(int)
        for issuer in self.rows:
            counts[issuer.filer_type] += 1
        return dict(sorted(counts.items()))


def find_user_agent() -> str:
    """``SEC_USER_AGENT`` from the environment or the repository ``.env``.

    In a worktree the ``.env`` lives at the main checkout, so the search walks
    up from the repository root and takes the first file that defines the key.
    Returns ``""`` when no value is found; :class:`SecClient` then fails with
    its own message before the first request.
    """
    value = os.environ.get("SEC_USER_AGENT", "")
    if value:
        return value.strip().strip("'\"")
    for directory in (REPO_ROOT, *REPO_ROOT.parents):
        env_file = directory / ".env"
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SEC_USER_AGENT="):
                found = line.partition("=")[2].strip().strip("'\"")
                if found:
                    return found
    return ""


def _reject(report: BuildReport, reason: str) -> None:
    """Increment the rejection counter named by ``reason``."""
    if reason == "no_ticker":
        report.rejected_no_ticker += 1
    elif reason == "no_annual":
        report.rejected_no_annual += 1
    elif reason == "fund":
        report.rejected_fund += 1
    else:  # pragma: no cover - internal invariant
        raise UniverseBuildError(f"unknown rejection reason {reason!r}")


def _ticker_for(facts: SubmissionFacts, tickers: Mapping[str, str]) -> str | None:
    """Primary ticker that ``company_tickers.json`` maps to this CIK."""
    valid = [ticker for ticker in facts.tickers if tickers.get(ticker) == facts.cik]
    mapped = tuple(sorted(ticker for ticker, cik in tickers.items() if cik == facts.cik))
    return primary_ticker(valid, mapped)


def _issuer(facts: SubmissionFacts, ticker: str, country: str) -> Issuer:
    """Build a universe row; requires an annual form (checked by the caller)."""
    if facts.filer_type is None:  # pragma: no cover - guarded by the caller
        raise UniverseBuildError(f"CIK{facts.cik} has no annual form")
    return Issuer(
        ticker=ticker,
        cik=facts.cik,
        name=facts.name,
        country=country,
        filer_type=facts.filer_type,
    )


def build(
    cache_dir: Path,
    *,
    as_of: date,
    offline: bool,
    target_total: int,
    min_us: int,
) -> BuildReport:
    """Build the universe from the cache or SEC.

    ``cache_dir`` holds the immutable raw cache (frames, submissions,
    ``company_tickers``). ``as_of`` is the calendar date whose quarter ends the
    frame window and whose two-year annual-report window is applied; it is a
    date, not a timestamp. Raises :class:`ConfigError` for a missing
    User-Agent, :class:`UniverseBuildError` for malformed SEC data and
    :class:`edgarpit.sec_client.SecClientError` for HTTP failures.
    """
    started = time.monotonic()
    report = BuildReport(periods=frame_periods(as_of))
    counter = [0]
    store = DocumentStore(cache_dir)
    if offline:
        client = SecClient(Settings(), offline_dir=cache_dir)
        frame_client: SecClient | None = None
    else:
        settings = Settings(sec_user_agent=find_user_agent())
        client = SecClient(settings, cache=RawCache(cache_dir))
        # The shared cache classifies URLs by endpoint and has no `frames`
        # endpoint yet, so frames and companyconcept documents are stored by
        # DocumentStore and fetched through a client without the shared cache.
        # The two clients are never used concurrently, so the 5 req/s cap still
        # holds in every request window.
        frame_client = SecClient(settings)
    cutoff = as_of - timedelta(days=ANNUAL_WINDOW_DAYS)
    seeded = sorted(
        (issuer for issuer in load_universe(SMALL_UNIVERSE) if issuer.cik is not None),
        key=lambda issuer: (issuer.country, issuer.ticker),
    )
    report.seeded = len(seeded)
    seeded_ciks = {issuer.cik for issuer in seeded}
    try:
        if not offline:
            counter[0] += 1
        tickers_doc = client.get_json(company_tickers_url())
        tickers = ticker_map(tickers_doc)
        float_rows: list[FrameRow] = []
        canadian_rows: list[FrameRow] = []
        for period in report.periods:
            for spec in CANADIAN_SPECS:
                rows = read_frame(
                    frame_client or client,
                    store,
                    spec,
                    period,
                    offline=offline,
                    counter=counter,
                )
                report.frames_read += 1
                report.frames_missing += int(not rows)
                canadian_rows.extend(rows)
                if spec == FLOAT_SPEC:
                    float_rows.extend(rows)

        latest_float = latest_by_cik(float_rows)
        canadian_ciks = {row.cik for row in canadian_rows if row.loc.startswith("CA-")}
        us_ranked_rows = sorted(
            (row for row in latest_float.values() if row.loc.startswith("US-") and row.value > 0),
            key=lambda row: (-row.value, row.cik),
        )
        report.canadian_candidates = len(canadian_ciks)
        report.us_candidates = len(us_ranked_rows)

        def read_facts(cik: str) -> SubmissionFacts:
            if not offline:
                counter[0] += 1
            document = client.get_json(submissions_url(cik))
            return submission_facts(client, document, cik, cutoff, offline=offline, counter=counter)

        def check(facts: SubmissionFacts, ticker: str | None) -> bool:
            """True when the issuer belongs in the file."""
            if ticker is None:
                _reject(report, "no_ticker")
            elif facts.filer_type is None:
                _reject(report, "no_annual")
            elif sic_excluded(facts.sic):
                _reject(report, "fund")
            else:
                return True
            return False

        canadian: list[Issuer] = []
        chosen_ciks: set[str] = set()
        for cik in sorted(canadian_ciks - seeded_ciks):
            facts = read_facts(cik)
            ticker = _ticker_for(facts, tickers)
            if not check(facts, ticker) or ticker is None:
                continue
            country = facts.country or "CA"
            if country != "CA":
                report.rejected_not_canadian += 1
                continue
            canadian.append(_issuer(facts, ticker, "CA"))
            chosen_ciks.add(cik)

        us_needed = max(target_total - report.seeded - len(canadian), min_us)
        # The raw float frame contains power-of-ten scale errors, so the top
        # candidates by raw value are validated against their own history
        # before ranking. The probe is wider than the slots so a corrected
        # issuer can still enter the list.
        probe = min(len(us_ranked_rows), max(3 * us_needed, US_PROBE_FLOOR))
        corrected: list[tuple[float, FrameRow]] = []
        for row in us_ranked_rows[:probe]:
            if row.cik in seeded_ciks:
                continue
            key = f"companyconcept__{row.cik}__{FLOAT_SPEC[0]}__{FLOAT_SPEC[1]}"
            document = read_document(
                frame_client or client,
                store,
                key,
                companyconcept_url(row.cik, FLOAT_SPEC[0], FLOAT_SPEC[1]),
                offline=offline,
                counter=counter,
            )
            history = float_history(document, key) if document is not None else []
            value = adjusted_float(row.value, history)
            if not 0 < value <= MAX_FLOAT_USD:
                report.rejected_implausible_float += 1
                continue
            corrected.append((value, row))
        corrected.sort(key=lambda item: (-item[0], item[1].cik))

        us: list[Issuer] = []
        for _, row in corrected:
            if len(us) >= us_needed:
                break
            if row.cik in seeded_ciks or row.cik in chosen_ciks:
                continue
            facts = read_facts(row.cik)
            ticker = _ticker_for(facts, tickers)
            if not check(facts, ticker) or ticker is None:
                continue
            if facts.country == "CA":
                report.rejected_not_canadian += 1
                continue
            us.append(_issuer(facts, ticker, "US"))
            chosen_ciks.add(row.cik)

        report.canadian_rows = len(canadian)
        report.us_rows = len(us)
        report.rows = merge_universe(seeded, canadian, us, target_total=target_total, min_us=min_us)
    finally:
        client.close()
        if frame_client is not None:
            frame_client.close()
        report.requests = counter[0]
        report.elapsed_seconds = time.monotonic() - started
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render_summary(report: BuildReport, output: Path) -> str:
    """Human-readable build summary (counts, frames, requests, seconds)."""
    lines = [
        f"wrote {output} ({len(report.rows)} rows)",
        f"countries: {report.counts_by_country()}",
        f"filer types: {report.counts_by_filer_type()}",
        f"frames: {report.frames_read} read, {report.frames_missing} empty; "
        f"periods {', '.join(report.periods)}",
        f"candidates: {report.canadian_candidates} Canadian, {report.us_candidates} US floats",
        f"rejected: no ticker {report.rejected_no_ticker}, no annual report "
        f"{report.rejected_no_annual}, fund/trust {report.rejected_fund}, "
        f"not Canadian {report.rejected_not_canadian}, implausible float "
        f"{report.rejected_implausible_float}",
        f"requests: {report.requests}; elapsed: {report.elapsed_seconds:.1f} s",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(
        description="Build config/universe_large.csv from SEC frames and submissions."
    )
    parser.add_argument(
        "--cache",
        type=Path,
        required=True,
        help="directory for the immutable raw cache (never committed)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="CSV to write (default: config/universe_large.csv)",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=date.today(),
        help="calendar date ending the frame window and the two-year annual-report "
        "window (default: today, YYYY-MM-DD)",
    )
    parser.add_argument(
        "--target-total",
        type=int,
        default=TARGET_TOTAL,
        help=f"target number of rows (default {TARGET_TOTAL})",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="read the cache instead of the network (no User-Agent needed)",
    )
    args = parser.parse_args(argv)
    try:
        report = build(
            args.cache,
            as_of=args.as_of,
            offline=args.offline,
            target_total=args.target_total,
            min_us=MIN_US,
        )
        write_universe(args.out, report.rows)
    except (ConfigError, SecClientError, UniverseError, UniverseBuildError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(render_summary(report, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
