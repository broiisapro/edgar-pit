"""Issuer universe: load, validate and resolve (FR-1.1, decision A-20).

``config/universe.csv`` carries the frozen columns ``ticker,cik,name,country,
filer_type`` (decision A-20). ``cik`` is the 10-character zero-padded string
(decision A-5); an empty field means the CIK is not known yet and
:func:`resolve` fills it from SEC's ``company_tickers.json``. A CIK that
disagrees with SEC's mapping is reported and kept as-is: the mapping is never
silently rewritten, and the caller fails loudly instead.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

UNIVERSE_COLUMNS = ["ticker", "cik", "name", "country", "filer_type"]
COUNTRIES = frozenset({"CA", "US"})
FILER_TYPES = frozenset({"10-K", "40-F", "20-F"})

_CIK_RE = re.compile(r"\d{10}")


class UniverseError(ValueError):
    """The universe file or SEC's ticker mapping is missing or malformed."""


@dataclass(frozen=True)
class Issuer:
    """One universe row.

    Attributes:
        ticker: US ticker symbol, upper case.
        cik: 10-character zero-padded CIK, or ``None`` when not yet known.
        name: issuer name as in the universe file.
        country: ``CA`` or ``US``.
        filer_type: annual form, one of ``10-K``, ``40-F``, ``20-F``.
    """

    ticker: str
    cik: str | None
    name: str
    country: str
    filer_type: str


@dataclass(frozen=True)
class CikDisagreement:
    """A universe CIK that disagrees with SEC's ticker mapping.

    ``universe_cik`` is the value kept from the universe file; ``sec_cik`` is
    what ``company_tickers.json`` says. The universe value is never overwritten.
    """

    ticker: str
    universe_cik: str
    sec_cik: str


@dataclass(frozen=True)
class ResolveReport:
    """Outcome of :func:`resolve`: resolved issuers plus every problem found.

    ``filled`` holds the ``(ticker, cik)`` pairs filled from SEC's mapping,
    ``disagreements`` the CIKs that disagree with it (kept unchanged in
    ``issuers``), and ``unresolved`` the tickers absent from it. ``ok`` is true
    only when there is no disagreement and no unresolved ticker.
    """

    issuers: list[Issuer]
    filled: list[tuple[str, str]]
    disagreements: list[CikDisagreement]
    unresolved: list[str]

    @property
    def ok(self) -> bool:
        return not self.disagreements and not self.unresolved


def load_universe(path: Path | str) -> list[Issuer]:
    """Load and validate a universe CSV, preserving row order.

    The header must be exactly ``ticker,cik,name,country,filer_type``. Every
    row needs a non-empty ticker and name, a unique ticker, ``country`` in
    ``{CA, US}``, ``filer_type`` in ``{10-K, 40-F, 20-F}`` and either an empty
    CIK or a 10-digit CIK unique across the file. Raises :class:`UniverseError`
    on the first violation, with the file and line number in the message.
    """
    source = Path(path)
    try:
        with source.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            if header != UNIVERSE_COLUMNS:
                raise UniverseError(
                    f"universe {source} header must be {','.join(UNIVERSE_COLUMNS)}; "
                    f"got {','.join(header) or '<empty>'}"
                )
            rows = list(reader)
    except OSError as exc:
        raise UniverseError(f"cannot read universe {source}: {exc}") from exc
    if not rows:
        raise UniverseError(f"universe {source} has no issuers")

    issuers: list[Issuer] = []
    tickers: dict[str, int] = {}
    ciks: dict[str, str] = {}
    for number, row in enumerate(rows, start=2):
        ticker = (row["ticker"] or "").strip()
        raw_cik = (row["cik"] or "").strip()
        name = (row["name"] or "").strip()
        country = (row["country"] or "").strip()
        filer_type = (row["filer_type"] or "").strip()
        if not ticker:
            raise UniverseError(f"{source} line {number}: ticker is empty")
        if ticker in tickers:
            raise UniverseError(
                f"{source} line {number}: ticker {ticker} already on line {tickers[ticker]}"
            )
        cik: str | None = None
        if raw_cik:
            if not _CIK_RE.fullmatch(raw_cik):
                raise UniverseError(f"{source} line {number}: CIK {raw_cik!r} must be 10 digits")
            if raw_cik in ciks:
                raise UniverseError(
                    f"{source} line {number}: CIK {raw_cik} already used by ticker {ciks[raw_cik]}"
                )
            cik = raw_cik
            ciks[cik] = ticker
        if not name:
            raise UniverseError(f"{source} line {number}: name is empty")
        if country not in COUNTRIES:
            raise UniverseError(
                f"{source} line {number}: country {country!r} must be one of "
                f"{', '.join(sorted(COUNTRIES))}"
            )
        if filer_type not in FILER_TYPES:
            raise UniverseError(
                f"{source} line {number}: filer_type {filer_type!r} must be one of "
                f"{', '.join(sorted(FILER_TYPES))}"
            )
        tickers[ticker] = number
        issuers.append(Issuer(ticker, cik, name, country, filer_type))
    return issuers


def ticker_map(document: Mapping[str, Any]) -> dict[str, str]:
    """Map ticker to 10-digit CIK from a ``company_tickers.json`` document.

    The document is SEC's object of ``{"0": {"cik_str": int, "ticker": str,
    "title": str}, ...}`` entries. Raises :class:`UniverseError` when an entry
    is not an object, has no usable ticker or integer ``cik_str``, or maps one
    ticker to two different CIKs, and when the document is empty.
    """
    mapping: dict[str, str] = {}
    for key, entry in document.items():
        if not isinstance(entry, Mapping):
            raise UniverseError(f"company_tickers entry {key!r} is not an object")
        ticker = entry.get("ticker")
        cik = entry.get("cik_str")
        if not isinstance(ticker, str) or not ticker:
            raise UniverseError(f"company_tickers entry {key!r} has no ticker")
        if isinstance(cik, bool) or not isinstance(cik, int) or not 0 < cik <= 9_999_999_999:
            raise UniverseError(f"company_tickers entry {key!r} has an invalid cik_str {cik!r}")
        cik10 = f"{cik:010d}"
        existing = mapping.get(ticker)
        if existing is not None and existing != cik10:
            raise UniverseError(
                f"company_tickers maps ticker {ticker} to both {existing} and {cik10}"
            )
        mapping[ticker] = cik10
    if not mapping:
        raise UniverseError("company_tickers.json has no entries")
    return mapping


def resolve(issuers: Sequence[Issuer], tickers_doc: Mapping[str, Any]) -> ResolveReport:
    """Check each issuer's ticker against SEC's mapping and fill missing CIKs.

    A missing CIK is filled from ``company_tickers.json``. A CIK that disagrees
    with SEC's mapping is reported and kept unchanged in ``issuers``. A ticker
    absent from the document is listed in ``unresolved`` (its row is kept
    unchanged too, whether or not it carries a CIK). ``ok`` is true only when
    every ticker is mapped and every CIK agrees.
    """
    sec = ticker_map(tickers_doc)
    resolved: list[Issuer] = []
    filled: list[tuple[str, str]] = []
    disagreements: list[CikDisagreement] = []
    unresolved: list[str] = []
    for issuer in issuers:
        sec_cik = sec.get(issuer.ticker)
        if sec_cik is None:
            unresolved.append(issuer.ticker)
            resolved.append(issuer)
        elif issuer.cik is None:
            filled.append((issuer.ticker, sec_cik))
            resolved.append(replace(issuer, cik=sec_cik))
        elif issuer.cik != sec_cik:
            disagreements.append(CikDisagreement(issuer.ticker, issuer.cik, sec_cik))
            resolved.append(issuer)
        else:
            resolved.append(issuer)
    return ResolveReport(resolved, filled, disagreements, unresolved)


def write_universe(path: Path | str, issuers: Sequence[Issuer]) -> Path:
    """Write a universe CSV, preserving column order and row order.

    The header is exactly ``ticker,cik,name,country,filer_type`` (decision
    A-20); a missing CIK is written as an empty field. Writes with ``\\n`` line
    endings and returns the path written.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(UNIVERSE_COLUMNS)
        for issuer in issuers:
            writer.writerow(
                [
                    issuer.ticker,
                    issuer.cik or "",
                    issuer.name,
                    issuer.country,
                    issuer.filer_type,
                ]
            )
    return target
