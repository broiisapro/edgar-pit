"""Offline shape checks for ``config/universe.csv``.

The CIKs and ``filer_type`` values were verified live against SEC's
``company_tickers.json`` and each issuer's submissions document when the file
was created; these tests keep the file well-formed without touching the
network.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

UNIVERSE_PATH = Path(__file__).resolve().parents[1] / "config" / "universe.csv"

CIK_RE = re.compile(r"^\d{10}$")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]*$")
ANNUAL_FORMS = {"10-K", "40-F", "20-F"}
COUNTRIES = {"CA", "US"}


def read_universe() -> list[dict[str, str]]:
    with UNIVERSE_PATH.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_header_and_size() -> None:
    rows = read_universe()
    assert rows, "universe is empty"
    assert list(rows[0]) == ["ticker", "cik", "name", "country", "filer_type"]
    assert 35 <= len(rows) <= 45
    canada = [row for row in rows if row["country"] == "CA"]
    united_states = [row for row in rows if row["country"] == "US"]
    assert len(canada) >= 18
    assert len(united_states) >= 18


def test_ciks_are_zero_padded_ten_digits() -> None:
    for row in read_universe():
        assert CIK_RE.fullmatch(row["cik"]), row


def test_tickers_are_well_formed_and_unique() -> None:
    tickers = [row["ticker"] for row in read_universe()]
    assert len(set(tickers)) == len(tickers)
    for ticker in tickers:
        assert TICKER_RE.fullmatch(ticker), ticker


def test_ciks_are_unique() -> None:
    ciks = [row["cik"] for row in read_universe()]
    assert len(set(ciks)) == len(ciks)


def test_names_are_non_empty() -> None:
    for row in read_universe():
        assert row["name"].strip(), row


def test_countries_and_filer_types_are_known_values() -> None:
    for row in read_universe():
        assert row["country"] in COUNTRIES, row
        assert row["filer_type"] in ANNUAL_FORMS, row


def test_required_issuers_are_present() -> None:
    rows = {row["ticker"]: row for row in read_universe()}
    assert {"RY", "AAPL", "SHOP"} <= set(rows)
    assert rows["RY"]["cik"] == "0001000275"
    assert rows["AAPL"]["cik"] == "0000320193"
    assert rows["SHOP"]["cik"] == "0001594805"
    assert rows["RY"]["filer_type"] == "40-F"
    assert rows["SHOP"]["filer_type"] == "10-K"


def test_canadian_mix_of_forty_f_and_ten_k() -> None:
    forms = {row["filer_type"] for row in read_universe() if row["country"] == "CA"}
    assert "40-F" in forms
    assert "10-K" in forms
