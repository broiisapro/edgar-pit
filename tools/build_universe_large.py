#!/usr/bin/env python3
"""Build ``config/universe_large.csv`` from SEC sources (decisions A-33, A-36).

The file holds the large Phase 2 universe: about 125 US and 125 Canadian
issuers, ranked by size from SEC data only. It is produced from SEC and Bank of
Canada data only:

- ``https://www.sec.gov/files/company_tickers.json`` resolves a CIK to its
  tickers (the same document ``edgarpit universe resolve`` uses).
- ``https://data.sec.gov/api/xbrl/frames/dei/EntityPublicFloat/USD/<period>.json``
  ranks US issuers by public float in US dollars.
- ``https://data.sec.gov/submissions/CIK<10 digits>.json`` supplies the entity
  name, the business and mailing addresses (which decide whether an issuer is
  Canadian), the filing history that proves the issuer filed a 10-K, 40-F or
  20-F in the last two years, and its SIC code.
- ``https://data.sec.gov/api/xbrl/companyconcept/CIK<10 digits>/<taxonomy>/Assets.json``
  supplies each Canadian candidate's total-assets history; the newest annual
  value ranks the Canadian list. ``us-gaap:Assets`` is read first and
  ``ifrs-full:Assets`` when the issuer tags no US-GAAP assets.
- ``https://data.sec.gov/api/xbrl/companyconcept/CIK<10 digits>/dei/EntityPublicFloat.json``
  supplies each top US candidate's float history, which is how the builder
  detects and undoes the power-of-ten scale errors some filers started
  reporting in 2022 (see :func:`adjusted_float`).
- ``https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json`` supplies the
  CAD-per-USD rate that converts a Canadian issuer's assets to US dollars
  (decision A-32 series, read through :class:`edgarpit.fx.valet.ValetClient`).
  Assets reported in any currency other than CAD or USD cannot be ranked and
  the issuer is dropped, counted in the report.

Frames are calendar-quarter snapshots. A single quarter is a partial list (a
float fact is tagged once a year), so the builder reads the
:data:`FRAME_QUARTERS` quarters ending with the quarter that contains
``--as-of`` and keeps each issuer's newest value. Frames carry no acceptance
time, so nothing here decides what was known when: this is a static issuer list
for a fetch run, not a point-in-time table.

Selection rules, in order (decision A-36):

1. Seed: every row of ``config/universe.csv`` is kept unchanged and counts
   toward its country's slots, so the large universe is always a superset of
   the Phase 1 universe.
2. Canadian: an issuer is Canadian when its submissions business or mailing
   address carries a Canadian province code (``A0``-``A9``, ``B0``-``B2``,
   ``Z4``) or a Canadian place name; a frame ``loc`` never decides it. The
   candidate set is every CIK of ``company_tickers.json`` plus every CIK seen in
   the float frames, so a sweep of the submissions documents finds Canadian
   issuers that tag no ``dei`` cover fact. Candidates are filtered by ticker,
   annual report, SIC code and a usable total-assets value, then ranked by the
   newest annual total assets in USD (ties broken by CIK) and the top
   ``--per-country`` minus the seeded Canadian rows are kept.
3. US: issuers with ``loc`` ``US-*`` in the float frames, ranked by the newest
   float value after the scale-error correction (ties broken by CIK), filtered
   by the same ticker, annual-report and SIC rules and by a US submissions
   address, and added until the file holds ``--per-country`` US rows including
   the seeded ones.
4. Excluded everywhere: funds, commodity trusts and blank-check companies
   (:data:`EXCLUDED_SIC`), issuers without a plain ticker in
   ``company_tickers.json``, issuers without an annual report in the window and
   one row per CIK (a second share class of the same CIK never adds a row).

The output is deterministic for a given cache and ``--as-of``: every list is
sorted before it is truncated.

Responses are cached immutably under ``--cache`` (never committed) in the
layout of :class:`edgarpit.rawcache.RawCache`, at <= 5 requests per second
through :class:`edgarpit.sec_client.SecClient`. A document already in the cache
is read before any request is made, so a re-run - including the long
submissions sweep - makes no requests and writes the same file; ``--offline``
additionally guarantees that no request can happen at all and needs no
User-Agent.

Usage::

    export SEC_USER_AGENT='Name email@example.com'
    uv run python tools/build_universe_large.py --cache /tmp/ep-i/universe
    uv run python tools/build_universe_large.py --cache /tmp/ep-i/universe --offline

Exit codes: ``0`` on success, ``2`` on a configuration, cache or SEC data
error (the message names the issuer or URL).
"""

from __future__ import annotations

import argparse
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
from edgarpit.fx.valet import ValetClient, valet_url
from edgarpit.rawcache import RawCache
from edgarpit.sec_client import (
    SecClient,
    SecClientError,
    SecNotFound,
    classify,
    company_tickers_url,
    companyconcept_url,
    frames_url,
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

TARGET_PER_COUNTRY = 125
FRAME_QUARTERS = 8
FLOAT_SPEC = ("dei", "EntityPublicFloat", "USD")
# A-36 ranks Canadian issuers by total assets in USD. US-GAAP filers tag
# `us-gaap:Assets`; MJDS 40-F filers tag `ifrs-full:Assets`. Other taxonomies
# (e.g. `srt`) never carry an issuer's balance sheet.
ASSET_SPECS = (("us-gaap", "Assets"), ("ifrs-full", "Assets"))
FX_SERIES = "FXUSDCAD"

ANNUAL_FORMS = ("10-K", "40-F", "20-F")
ANNUAL_WINDOW_DAYS = 730

# Some filers report dei:EntityPublicFloat scaled by a power of ten (a factor
# of 1,000 or 1,000,000 shows up in the frames API from 2022 on). A jump of
# this size between two consecutive observations marks the scale error; the
# newest value is divided by that power of ten. A value still above
# MAX_FLOAT_USD after the correction is treated as unusable.
SCALE_JUMP_RATIO = 100.0
MAX_FLOAT_USD = 5_000_000_000_000.0
US_PROBE_FLOOR = 300

# EDGAR address codes for the Canadian provinces and territories plus the
# federal code: Alberta to Saskatchewan are A0-A9, Yukon B0, the Northwest
# Territories B1, Nunavut B2 and "Canada" Z4. The address text ("Ontario,
# Canada") is checked as well, because older submissions omit the code.
CANADIAN_CODES = frozenset(
    {"A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "B0", "B1", "B2", "Z4"}
)

# Blank-check companies and investment/commodity trusts, by SIC code: they
# file annual reports but hold no operating business.
EXCLUDED_SIC = frozenset({"6221", "6722", "6726", "6733", "6770"})

TICKER_RE = re.compile(r"[A-Z][A-Z0-9.]*")
STATE_RE = re.compile(r"[A-Z]{2}")


class UniverseBuildError(RuntimeError):
    """SEC data or the cache cannot produce a universe row."""


# --------------------------------------------------------------------------
# Document cache
# --------------------------------------------------------------------------


class DocumentReader:
    """Read raw-cache documents, fetching a miss through the SEC client.

    ``cache`` is the immutable raw cache, ``client`` the online client whose
    ``get_json`` writes new bodies (``None`` in offline mode) and ``counter`` a
    one-element list counting network requests. A stored body is always used
    before any request, so a re-run against a warm cache issues no requests and
    ``--offline`` never touches the network. A document the SEC does not have
    (HTTP 404) is absent from the cache too, so both modes return ``None``.

    Raises :class:`UniverseBuildError` for a stored or fetched body that is not
    a JSON object, and :class:`edgarpit.sec_client.SecClientError` for a request
    failure other than 404.
    """

    def __init__(self, cache: RawCache, client: SecClient | None, counter: list[int]) -> None:
        self._cache = cache
        self._client = client
        self._counter = counter

    def json(self, url: str) -> dict[str, Any] | None:
        """Return the JSON object at ``url``, or ``None`` when the SEC has none."""
        endpoint, cik, key = classify(url)
        body = self._cache.latest(endpoint, cik, key)
        if body is not None:
            return _json_object(body, url)
        if self._client is None:
            return None
        self._counter[0] += 1
        try:
            return self._client.get_json(url)
        except SecNotFound:
            return None


def _json_object(body: bytes, source: str) -> dict[str, Any]:
    """Parse ``body`` as a JSON object, naming ``source`` on failure."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UniverseBuildError(f"cached document {source} is not JSON: {error}") from error
    if not isinstance(payload, dict):
        raise UniverseBuildError(f"document {source} is not a JSON object")
    return payload


# --------------------------------------------------------------------------
# Frames and companyconcept documents
# --------------------------------------------------------------------------


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


def read_frame(reader: DocumentReader, spec: tuple[str, str, str], period: str) -> list[FrameRow]:
    """Read one frame from the cache or SEC; ``[]`` when the frame is absent.

    ``spec`` is ``(taxonomy, tag, unit)`` and ``period`` an instant frame such
    as ``CY2026Q2I``. A quarter the SEC has not published returns ``[]``; a
    malformed frame raises :class:`UniverseBuildError`.
    """
    payload = reader.json(frames_url(*spec, period))
    if payload is None:
        return []
    return parse_frame(payload, period)


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


@dataclass(frozen=True)
class AssetFact:
    """One total-assets observation of an issuer.

    Attributes:
        end: balance-sheet date (``DATE``).
        value: total assets in ``unit``.
        unit: reporting currency, ``CAD`` or ``USD`` (others are parsed but
            never ranked).
        accession: accession of the filing that carried the fact.
        form: the filing's form as tagged in the fact.
    """

    end: date
    value: float
    unit: str
    accession: str
    form: str


def annual_assets(document: Mapping[str, Any], source: str) -> AssetFact | None:
    """Newest total-assets value of a companyconcept document, annual first.

    ``document`` is the parsed ``companyconcept`` JSON of one ``Assets`` tag and
    ``source`` labels it in error messages. Every unit is considered; the
    newest row whose ``form`` is an annual report (:data:`ANNUAL_FORMS`) is
    returned, and when the issuer tagged assets in no annual report at all the
    newest row of any form is returned instead. A tie on ``end`` is broken by
    the larger accession, so the choice never depends on row order. Returns
    ``None`` when the document holds no usable fact. Raises
    :class:`UniverseBuildError` on a malformed row rather than silently ranking
    an issuer on a partial history.
    """
    units = document.get("units")
    if not isinstance(units, Mapping):
        raise UniverseBuildError(f"{source} has no units object")
    annual: list[AssetFact] = []
    other: list[AssetFact] = []
    for unit in sorted(units):
        rows = units[unit]
        if not isinstance(unit, str) or not isinstance(rows, list):
            raise UniverseBuildError(f"{source} has a malformed unit {unit!r}")
        for row in rows:
            if not isinstance(row, Mapping):
                raise UniverseBuildError(f"{source} has a non-object fact")
            end = row.get("end")
            value = row.get("val")
            accession = row.get("accn")
            form = row.get("form")
            if not isinstance(end, str) or not end:
                raise UniverseBuildError(f"{source} has a fact without an end date")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise UniverseBuildError(f"{source} has a fact without a numeric val")
            try:
                when = date.fromisoformat(end)
            except ValueError as error:
                raise UniverseBuildError(f"{source} has a malformed end date {end!r}") from error
            fact = AssetFact(
                end=when,
                value=float(value),
                unit=unit,
                accession=accession if isinstance(accession, str) else "",
                form=form if isinstance(form, str) else "",
            )
            (annual if fact.form in ANNUAL_FORMS else other).append(fact)
    pool = annual or other
    if not pool:
        return None
    return max(pool, key=lambda fact: (fact.end, fact.accession))


def read_assets(reader: DocumentReader, cik: str) -> AssetFact | None:
    """Read an issuer's newest total assets from the cache or SEC.

    Every tag of :data:`ASSET_SPECS` is tried in order and the first one with a
    usable value wins: a US-GAAP filer is ranked by ``us-gaap:Assets`` and a
    MJDS filer, which tags no US-GAAP assets, by ``ifrs-full:Assets``. Returns
    ``None`` when neither document exists or holds a fact.
    """
    for taxonomy, tag in ASSET_SPECS:
        document = reader.json(companyconcept_url(cik, taxonomy, tag))
        if document is None:
            continue
        fact = annual_assets(document, f"CIK{cik} {taxonomy}:{tag}")
        if fact is not None:
            return fact
    return None


def assets_in_usd(fact: AssetFact, cad_per_usd: float | None) -> float | None:
    """Convert a total-assets fact to US dollars.

    ``fact`` carries the reporting currency; ``cad_per_usd`` is the latest
    ``FXUSDCAD`` observation (Canadian dollars per US dollar). A USD fact is
    returned unchanged, a CAD fact is divided by the rate, and any other
    currency returns ``None`` because it is out of scope (decision A-32) and
    must not be guessed. A CAD fact without a rate also returns ``None``.
    """
    if fact.unit == "USD":
        return fact.value
    if fact.unit == "CAD" and cad_per_usd is not None and cad_per_usd > 0:
        return fact.value / cad_per_usd
    return None


# --------------------------------------------------------------------------
# FX
# --------------------------------------------------------------------------


def latest_fx_rate(
    document: Mapping[str, Any], series: str = FX_SERIES
) -> tuple[date, float] | None:
    """Latest observation of a Valet series document as ``(date, rate)``.

    ``document`` is the parsed JSON of ``valet_url(series)``; ``series`` is the
    Valet id (``FXUSDCAD``: Canadian dollars per US dollar). Observations
    without a value for the series (Valet's empty string on a holiday) are
    skipped; the newest date wins regardless of document order. Returns
    ``None`` when no observation carries a value. Raises
    :class:`UniverseBuildError` for a malformed observation or a non-positive
    rate, because a wrong rate would silently mis-rank every Canadian issuer.
    """
    observations = document.get("observations")
    if not isinstance(observations, list):
        raise UniverseBuildError("FX document has no observations array")
    best: tuple[date, float] | None = None
    for entry in observations:
        if not isinstance(entry, Mapping):
            raise UniverseBuildError("FX document has a non-object observation")
        day = entry.get("d")
        if not isinstance(day, str) or not day:
            raise UniverseBuildError("FX observation has no date")
        try:
            when = date.fromisoformat(day)
        except ValueError as error:
            raise UniverseBuildError(f"FX observation has a malformed date {day!r}") from error
        value = entry.get(series)
        if not isinstance(value, Mapping):
            continue
        raw = value.get("v")
        if raw is None or raw == "":
            continue
        try:
            rate = float(raw)
        except (TypeError, ValueError) as error:
            raise UniverseBuildError(f"FX observation {day} has a non-numeric value") from error
        if not rate > 0 or not math.isfinite(rate):
            raise UniverseBuildError(f"FX observation {day} has a non-positive value {rate!r}")
        if best is None or when > best[0]:
            best = (when, rate)
    return best


def read_fx_rate(
    cache: RawCache,
    *,
    offline: bool,
    counter: list[int],
    settings: Settings | None,
) -> tuple[date, float] | None:
    """Latest ``FXUSDCAD`` observation from the cache or the Bank of Canada.

    The series is read from the raw cache first; only a cache miss in online
    mode fetches it through :class:`edgarpit.fx.valet.ValetClient` (counted in
    ``counter``) and stores it in the ``fx`` slot. Returns ``(date, CAD per
    USD)`` or ``None`` when the series is neither cached nor fetchable. Raises
    :class:`UniverseBuildError` for a malformed cached or fetched document.
    """
    body = cache.latest("fx", "", FX_SERIES)
    if body is not None:
        return latest_fx_rate(_json_object(body, valet_url(FX_SERIES)))
    if offline or settings is None:
        return None
    counter[0] += 1
    with ValetClient(settings, cache=cache) as valet:
        document = valet.get_json(valet_url(FX_SERIES))
    return latest_fx_rate(document)


# --------------------------------------------------------------------------
# Submissions
# --------------------------------------------------------------------------


def _section_country(block: Any) -> str | None:
    """Classify one submissions address block as ``CA``, ``US`` or ``None``.

    A description or country naming Canada, or a Canadian province code, is
    ``CA``; a non-foreign two-letter state code is ``US``; anything else
    (including a missing block) is ``None``.
    """
    if not isinstance(block, Mapping):
        return None
    code = block.get("stateOrCountry") or block.get("countryCode")
    description = " ".join(
        str(block.get(key) or "") for key in ("stateOrCountryDescription", "country")
    ).lower()
    if "canada" in description:
        return "CA"
    if isinstance(code, str) and code in CANADIAN_CODES:
        return "CA"
    if isinstance(code, str) and STATE_RE.fullmatch(code) and block.get("isForeignLocation") != 1:
        return "US"
    return None


def address_country(addresses: Mapping[str, Any] | None) -> str | None:
    """Classify a submissions ``addresses`` object as ``CA``, ``US`` or ``None``.

    ``addresses`` is the document's ``addresses`` value. A Canadian business
    *or* mailing address makes the issuer Canadian (decision A-36); otherwise a
    US state code in either address makes it US; otherwise the address is
    foreign or missing and the result is ``None``.
    """
    if not isinstance(addresses, Mapping):
        return None
    countries = [_section_country(addresses.get(section)) for section in ("business", "mailing")]
    if "CA" in countries:
        return "CA"
    if "US" in countries:
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
    reader: DocumentReader,
    document: Mapping[str, Any],
    cik: str,
    cutoff: date,
) -> list[tuple[str, date]]:
    """Every filing of one issuer down to ``cutoff``, from recent plus pages.

    The ``filings.recent`` block holds at least the last 1,000 filings and is
    enough for two years for almost every issuer. When its oldest row is newer
    than ``cutoff`` and the document lists pages, the pages are read in order
    until one reaches ``cutoff`` (or they run out), because a missing annual
    report would silently drop the issuer. A page the SEC does not serve ends
    the walk.
    """
    pairs = _filing_rows(document, f"CIK{cik}")
    if pairs and min(filed for _, filed in pairs) <= cutoff:
        return pairs
    known = {(form, filed) for form, filed in pairs}
    for url in _page_urls(document):
        page = reader.json(url)
        if page is None:
            break
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
    reader: DocumentReader,
    document: Mapping[str, Any],
    cik: str,
    cutoff: date,
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
    pairs = read_filings(reader, document, cik, cutoff)
    return SubmissionFacts(
        cik=cik,
        name=name.strip(),
        tickers=tickers,
        country=address_country(document.get("addresses")),
        filer_type=annual_filer_type(pairs, cutoff),
        sic=str(sic).strip() if sic is not None else None,
    )


def sweep_submissions(
    reader: DocumentReader, ciks: Iterable[str], cutoff: date
) -> tuple[dict[str, SubmissionFacts], int]:
    """Read every candidate's submissions document; return facts and misses.

    ``ciks`` is the candidate CIK set (every ticker CIK plus every float-frame
    CIK) and ``cutoff`` the start of the annual-report window. An issuer whose
    document the SEC does not serve is counted as a miss and skipped. The
    result maps 10-character CIK to facts; its order never matters because
    every selection sorts explicitly.
    """
    facts: dict[str, SubmissionFacts] = {}
    missing = 0
    for cik in sorted(ciks):
        document = reader.json(submissions_url(cik))
        if document is None:
            missing += 1
            continue
        facts[cik] = submission_facts(reader, document, cik, cutoff)
    return facts, missing


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def merge_universe(
    seeded: Sequence[Issuer],
    canadian: Sequence[Issuer],
    us: Sequence[Issuer],
    *,
    per_country: int,
) -> list[Issuer]:
    """Combine seeded, Canadian and US rows into the sorted file.

    Seeded rows always win, keep their fields verbatim and are never dropped;
    every ranked row is taken in the order given until its country holds
    ``per_country`` rows including the seeded ones. A duplicate CIK is skipped
    (the first row wins) and a duplicate ticker raises :class:`UniverseError`.
    The result is sorted by ``(country, ticker)``, so the file order never
    depends on input order.
    """
    rows: list[Issuer] = []
    by_cik: set[str] = set()
    by_ticker: dict[str, str] = {}
    counts: dict[str, int] = defaultdict(int)

    def add(issuer: Issuer, *, capped: bool) -> bool:
        if issuer.cik is None:
            raise UniverseError(f"issuer {issuer.ticker} has no CIK")
        if issuer.cik in by_cik:
            return False
        owner = by_ticker.get(issuer.ticker)
        if owner is not None:
            raise UniverseError(f"ticker {issuer.ticker} is used by both {owner} and {issuer.cik}")
        if capped and counts[issuer.country] >= per_country:
            return False
        rows.append(issuer)
        by_cik.add(issuer.cik)
        by_ticker[issuer.ticker] = issuer.cik
        counts[issuer.country] += 1
        return True

    for issuer in seeded:
        add(issuer, capped=False)
    for issuer in canadian:
        add(issuer, capped=True)
    for issuer in us:
        add(issuer, capped=True)
    return sorted(rows, key=lambda issuer: (issuer.country, issuer.ticker))


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


@dataclass
class BuildReport:
    """Counts and timings the CLI prints and the PR body quotes."""

    periods: list[str] = field(default_factory=list)
    per_country: int = TARGET_PER_COUNTRY
    requests: int = 0
    submissions_read: int = 0
    submissions_missing: int = 0
    sweep_candidates: int = 0
    frames_read: int = 0
    frames_missing: int = 0
    seeded: int = 0
    canadian_candidates: int = 0
    us_candidates: int = 0
    canadian_rows: int = 0
    us_rows: int = 0
    rejected_no_ticker: int = 0
    rejected_no_annual: int = 0
    rejected_fund: int = 0
    rejected_no_assets: int = 0
    rejected_currency: int = 0
    rejected_no_facts: int = 0
    rejected_not_us: int = 0
    rejected_implausible_float: int = 0
    fx_date: date | None = None
    fx_rate: float | None = None
    assets_usd: dict[str, float] = field(default_factory=dict)
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

    def largest_canadian(self, count: int) -> list[tuple[str, str, float | None]]:
        """The ``count`` largest Canadian rows as ``(ticker, name, USD assets)``.

        Rows without a ranked asset value (the seeded Phase 1 issuers when their
        assets could not be read) come last, ordered by ticker. Sorting is
        deterministic because the asset map is keyed by CIK.
        """
        canadian = [issuer for issuer in self.rows if issuer.country == "CA"]
        canadian.sort(
            key=lambda issuer: (
                -(self.assets_usd.get(issuer.cik or "", -1.0)),
                issuer.ticker,
            )
        )
        return [
            (issuer.ticker, issuer.name, self.assets_usd.get(issuer.cik or ""))
            for issuer in canadian[:count]
        ]


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


def _tickers_by_cik(tickers: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """Invert ``ticker -> CIK`` into ``CIK -> sorted tickers``."""
    by_cik: dict[str, list[str]] = defaultdict(list)
    for ticker, cik in tickers.items():
        by_cik[cik].append(ticker)
    return {cik: tuple(sorted(entries)) for cik, entries in by_cik.items()}


def _ticker_for(
    facts: SubmissionFacts,
    tickers: Mapping[str, str],
    tickers_by_cik: Mapping[str, tuple[str, ...]],
) -> str | None:
    """Primary ticker that ``company_tickers.json`` maps to this CIK."""
    valid = [ticker for ticker in facts.tickers if tickers.get(ticker) == facts.cik]
    return primary_ticker(valid, tickers_by_cik.get(facts.cik, ()))


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


def _reject(report: BuildReport, reason: str) -> None:
    """Increment the rejection counter named by ``reason``."""
    counters = {
        "no_ticker": "rejected_no_ticker",
        "no_annual": "rejected_no_annual",
        "fund": "rejected_fund",
        "no_assets": "rejected_no_assets",
        "currency": "rejected_currency",
        "no_facts": "rejected_no_facts",
        "not_us": "rejected_not_us",
        "float": "rejected_implausible_float",
    }
    attribute = counters.get(reason)
    if attribute is None:  # pragma: no cover - internal invariant
        raise UniverseBuildError(f"unknown rejection reason {reason!r}")
    setattr(report, attribute, getattr(report, attribute) + 1)


def build(
    cache_dir: Path,
    *,
    as_of: date,
    offline: bool,
    per_country: int,
) -> BuildReport:
    """Build the universe from the cache or SEC.

    ``cache_dir`` holds the immutable raw cache (frames, companyconcept,
    submissions, ``company_tickers`` and the FX series). ``as_of`` is the
    calendar date whose quarter ends the frame window and whose two-year
    annual-report window is applied; it is a date, not a timestamp.
    ``per_country`` caps each country's rows. Raises :class:`ConfigError` for a
    missing User-Agent in online mode, :class:`UniverseBuildError` for
    malformed SEC data and :class:`edgarpit.sec_client.SecClientError` for HTTP
    failures.
    """
    started = time.monotonic()
    report = BuildReport(periods=frame_periods(as_of), per_country=per_country)
    counter = [0]
    cache = RawCache(cache_dir)
    settings: Settings | None = None
    client: SecClient | None = None
    try:
        if not offline:
            settings = Settings(sec_user_agent=find_user_agent())
            client = SecClient(settings, cache=cache)
        reader = DocumentReader(cache, client, counter)
        cutoff = as_of - timedelta(days=ANNUAL_WINDOW_DAYS)

        tickers_doc = reader.json(company_tickers_url())
        if tickers_doc is None:
            raise UniverseBuildError(
                f"no company_tickers.json in the cache and none fetched for {cache_dir}"
            )
        tickers = ticker_map(tickers_doc)
        tickers_by_cik = _tickers_by_cik(tickers)

        seeded = sorted(
            (issuer for issuer in load_universe(SMALL_UNIVERSE) if issuer.cik is not None),
            key=lambda issuer: (issuer.country, issuer.ticker),
        )
        report.seeded = len(seeded)
        seeded_ciks = {issuer.cik for issuer in seeded}
        seeded_ca = [issuer for issuer in seeded if issuer.country == "CA"]

        float_rows: list[FrameRow] = []
        for period in report.periods:
            rows = read_frame(reader, FLOAT_SPEC, period)
            report.frames_read += 1
            report.frames_missing += int(not rows)
            float_rows.extend(rows)

        sweep_ciks = set(tickers_by_cik) | {row.cik for row in float_rows}
        report.sweep_candidates = len(sweep_ciks)
        facts_by_cik, report.submissions_missing = sweep_submissions(reader, sweep_ciks, cutoff)
        report.submissions_read = len(facts_by_cik)

        report.fx_date, report.fx_rate = read_fx_rate(
            cache, offline=offline, counter=counter, settings=settings
        ) or (None, None)
        assets: dict[str, AssetFact | None] = {}

        def asset_of(cik: str) -> AssetFact | None:
            """Read (once) the newest total assets of one issuer."""
            if cik not in assets:
                assets[cik] = read_assets(reader, cik)
            return assets[cik]

        def ranked_assets(cik: str) -> float | None:
            """The issuer's total assets in USD, or ``None`` when unrankable."""
            fact = asset_of(cik)
            if fact is None:
                return None
            value = assets_in_usd(fact, report.fx_rate)
            if value is None or not math.isfinite(value) or value <= 0:
                return None
            return value

        canadian_pool: list[tuple[float, SubmissionFacts, str]] = []
        for cik, facts in sorted(facts_by_cik.items()):
            if cik in seeded_ciks or facts.country != "CA":
                continue
            report.canadian_candidates += 1
            ticker = _ticker_for(facts, tickers, tickers_by_cik)
            if ticker is None:
                _reject(report, "no_ticker")
                continue
            if facts.filer_type is None:
                _reject(report, "no_annual")
                continue
            if sic_excluded(facts.sic):
                _reject(report, "fund")
                continue
            fact = asset_of(cik)
            if fact is None:
                _reject(report, "no_assets")
                continue
            value = ranked_assets(cik)
            if value is None:
                _reject(report, "currency")
                continue
            canadian_pool.append((value, facts, ticker))
        canadian_pool.sort(key=lambda item: (-item[0], item[1].cik))
        canadian = [_issuer(facts, ticker, "CA") for _, facts, ticker in canadian_pool]

        # The seeded issuers keep their rows verbatim, but their assets are
        # read too so the report and the PR can quote the largest Canadian
        # names without a second pass.
        for issuer in seeded_ca:
            if issuer.cik in facts_by_cik:
                asset_of(issuer.cik)

        latest_float = latest_by_cik(float_rows)
        us_pool = sorted(
            (row for row in latest_float.values() if row.loc.startswith("US-") and row.value > 0),
            key=lambda row: (-row.value, row.cik),
        )
        report.us_candidates = len(us_pool)
        us_slots = max(per_country - sum(1 for i in seeded if i.country == "US"), 0)
        # The raw float frame contains power-of-ten scale errors, so the top
        # candidates by raw value are validated against their own history
        # before ranking.
        corrected: list[tuple[float, FrameRow]] = []
        probed = 0
        us: list[Issuer] = []
        us_chosen: set[str] = set()
        # Rank in passes: probe a batch of candidates, rank the corrected
        # values, and probe further only when the batch cannot fill the slots,
        # so the run never reads a float history it does not need.
        while probed < len(us_pool) and len(us) < us_slots:
            limit = min(len(us_pool), max(probed + US_PROBE_FLOOR, 2 * us_slots))
            for row in us_pool[probed:limit]:
                probed += 1
                if row.cik in seeded_ciks:
                    continue
                document = reader.json(companyconcept_url(row.cik, FLOAT_SPEC[0], FLOAT_SPEC[1]))
                history = (
                    float_history(document, f"CIK{row.cik} {FLOAT_SPEC[0]}:{FLOAT_SPEC[1]}")
                    if document is not None
                    else []
                )
                value = adjusted_float(row.value, history)
                if not 0 < value <= MAX_FLOAT_USD:
                    _reject(report, "float")
                    continue
                corrected.append((value, row))
            corrected.sort(key=lambda item: (-item[0], item[1].cik))
            for _, row in corrected:
                if len(us) >= us_slots:
                    break
                if row.cik in seeded_ciks or row.cik in us_chosen:
                    continue
                us_facts = facts_by_cik.get(row.cik)
                if us_facts is None:
                    # The float frame lists a CIK the ticker file does not map
                    # to an issuer with a submissions document.
                    _reject(report, "no_facts")
                    continue
                ticker = _ticker_for(us_facts, tickers, tickers_by_cik)
                if ticker is None:
                    _reject(report, "no_ticker")
                    continue
                if us_facts.filer_type is None:
                    _reject(report, "no_annual")
                    continue
                if sic_excluded(us_facts.sic):
                    _reject(report, "fund")
                    continue
                if us_facts.country != "US":
                    # The float frame's `loc` can disagree with the address;
                    # the submissions address decides, and a Canadian issuer is
                    # either in the Canadian list or not in the universe at all.
                    _reject(report, "not_us")
                    continue
                us.append(_issuer(us_facts, ticker, "US"))
                us_chosen.add(row.cik)

        report.rows = merge_universe(seeded, canadian, us, per_country=per_country)
        report.canadian_rows = sum(
            1 for issuer in report.rows if issuer.country == "CA" and issuer.cik not in seeded_ciks
        )
        report.us_rows = sum(
            1 for issuer in report.rows if issuer.country == "US" and issuer.cik not in seeded_ciks
        )
        report.assets_usd = {}
        for issuer in report.rows:
            if issuer.cik is None:  # pragma: no cover - merge rejects a missing CIK
                continue
            value = ranked_assets(issuer.cik)
            if value is not None:
                report.assets_usd[issuer.cik] = value
    finally:
        if client is not None:
            client.close()
        report.requests = counter[0]
        report.elapsed_seconds = time.monotonic() - started
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render_summary(report: BuildReport, output: Path) -> str:
    """Human-readable build summary: counts, assets, requests and seconds."""
    fx = "none"
    if report.fx_date is not None and report.fx_rate is not None:
        fx = f"{report.fx_rate:.4f} CAD/USD on {report.fx_date.isoformat()}"
    lines = [
        f"wrote {output} ({len(report.rows)} rows)",
        f"countries: {report.counts_by_country()}",
        f"filer types: {report.counts_by_filer_type()}",
        f"frames: {report.frames_read} read, {report.frames_missing} empty; "
        f"periods {', '.join(report.periods)}",
        f"submissions: {report.submissions_read} read, {report.submissions_missing} missing "
        f"of {report.sweep_candidates} candidates",
        f"selected: {report.canadian_rows} Canadian and {report.us_rows} US ranked rows "
        f"on top of {report.seeded} seeded",
        f"candidates: {report.canadian_candidates} Canadian addresses, "
        f"{report.us_candidates} US floats",
        f"rejected: no ticker {report.rejected_no_ticker}, no annual report "
        f"{report.rejected_no_annual}, fund/trust {report.rejected_fund}, no assets "
        f"{report.rejected_no_assets}, currency {report.rejected_currency}, no facts "
        f"{report.rejected_no_facts}, not US {report.rejected_not_us}, implausible float "
        f"{report.rejected_implausible_float}",
        f"fx: {fx}",
    ]
    lines.extend(
        f"largest Canadian: {ticker} {name} {assets:,.0f} USD"
        if assets is not None
        else f"largest Canadian: {ticker} {name} (seeded, no ranked assets)"
        for ticker, name, assets in report.largest_canadian(20)
    )
    lines.append(f"requests: {report.requests}; elapsed: {report.elapsed_seconds:.1f} s")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(
        description="Build config/universe_large.csv from SEC frames, companyconcept "
        "documents and submissions."
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
        "--per-country",
        type=int,
        default=TARGET_PER_COUNTRY,
        help=f"maximum rows per country (default {TARGET_PER_COUNTRY})",
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
            per_country=args.per_country,
        )
        write_universe(args.out, report.rows)
    except (ConfigError, SecClientError, UniverseError, UniverseBuildError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(render_summary(report, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
