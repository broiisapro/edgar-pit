"""Offline checks for ``config/universe_large.csv`` and its builder (issue #102).

The CSV is committed data, so these tests keep it well-formed without touching
the network: columns, unique 10-digit CIKs, country and filer-type domains,
sort order, size, and that it is a superset of ``config/universe.csv``. The
builder's selection rules are exercised on small in-memory fixtures, including
an end-to-end ``build(..., offline=True)`` over a temporary raw cache.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from edgarpit.universe import Issuer, UniverseError, load_universe, write_universe

REPO_ROOT = Path(__file__).resolve().parents[1]
LARGE_PATH = REPO_ROOT / "config" / "universe_large.csv"
SMALL_PATH = REPO_ROOT / "config" / "universe.csv"

COLUMNS = ["ticker", "cik", "name", "country", "filer_type"]
CIK_RE = re.compile(r"^\d{10}$")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]*$")
ANNUAL_FORMS = {"10-K", "40-F", "20-F"}
COUNTRIES = {"CA", "US"}
STAMP = "20260101T000000000000Z"


def load_builder() -> ModuleType:
    """Import ``tools/build_universe_large.py`` by path (it is not a package)."""
    path = REPO_ROOT / "tools" / "build_universe_large.py"
    spec = importlib.util.spec_from_file_location("universe_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------
# The committed CSV
# --------------------------------------------------------------------------


def test_header_size_and_country_mix() -> None:
    rows = read_csv(LARGE_PATH)
    assert rows, "universe_large.csv is empty"
    assert list(rows[0]) == COLUMNS
    assert 230 <= len(rows) <= 300, len(rows)
    canada = [row for row in rows if row["country"] == "CA"]
    united_states = [row for row in rows if row["country"] == "US"]
    assert len(canada) >= 40
    assert len(united_states) >= 25


def test_sorted_by_country_then_ticker() -> None:
    keys = [(row["country"], row["ticker"]) for row in read_csv(LARGE_PATH)]
    assert keys == sorted(keys)


def test_ciks_are_unique_ten_digits() -> None:
    ciks = [row["cik"] for row in read_csv(LARGE_PATH)]
    assert len(set(ciks)) == len(ciks)
    for cik in ciks:
        assert CIK_RE.fullmatch(cik), cik


def test_tickers_are_unique_and_well_formed() -> None:
    tickers = [row["ticker"] for row in read_csv(LARGE_PATH)]
    assert len(set(tickers)) == len(tickers)
    for ticker in tickers:
        assert TICKER_RE.fullmatch(ticker), ticker


def test_names_are_non_empty() -> None:
    for row in read_csv(LARGE_PATH):
        assert row["name"].strip(), row


def test_countries_and_filer_types_are_known_values() -> None:
    for row in read_csv(LARGE_PATH):
        assert row["country"] in COUNTRIES, row
        assert row["filer_type"] in ANNUAL_FORMS, row


def test_superset_of_the_phase_one_universe() -> None:
    large = {row["cik"]: row for row in read_csv(LARGE_PATH)}
    for row in read_csv(SMALL_PATH):
        assert row["cik"] in large, f"{row['ticker']} ({row['cik']}) is missing"
        assert large[row["cik"]] == row


def test_file_loads_with_the_shared_universe_loader() -> None:
    issuers = load_universe(LARGE_PATH)
    assert len(issuers) == len(read_csv(LARGE_PATH))
    assert all(issuer.cik is not None for issuer in issuers)


# --------------------------------------------------------------------------
# Frame and submissions helpers
# --------------------------------------------------------------------------


def test_frame_periods_are_calendar_quarters_newest_first() -> None:
    builder = load_builder()
    assert builder.frame_periods(date(2026, 10, 27), 3) == [
        "CY2026Q4I",
        "CY2026Q3I",
        "CY2026Q2I",
    ]
    assert builder.frame_periods(date(2026, 1, 1), 2) == ["CY2026Q1I", "CY2025Q4I"]
    assert builder.frame_periods(date(2025, 12, 31), 1) == ["CY2025Q4I"]


def test_parse_frame_keeps_values_and_rejects_malformed_rows() -> None:
    builder = load_builder()
    document = {
        "data": [
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "loc": "US-CA",
                "end": "2025-06-27",
                "val": 3253431000000,
                "accn": "0000320193-25-000001",
            }
        ]
    }
    rows = builder.parse_frame(document, "CY2025Q2I")
    assert rows == [
        builder.FrameRow(
            cik="0000320193",
            name="Apple Inc.",
            loc="US-CA",
            end="2025-06-27",
            value=3253431000000.0,
            accession="0000320193-25-000001",
        )
    ]
    with pytest.raises(builder.UniverseBuildError):
        builder.parse_frame({"data": [{"cik": "x", "end": "2025-01-01", "val": 1}]}, "CY2025Q1I")
    with pytest.raises(builder.UniverseBuildError):
        builder.parse_frame({"data": [{"cik": 1, "end": "", "val": 1}]}, "CY2025Q1I")
    with pytest.raises(builder.UniverseBuildError):
        builder.parse_frame({"data": [{"cik": 1, "end": "2025-01-01", "val": None}]}, "CY2025Q1I")
    with pytest.raises(builder.UniverseBuildError):
        builder.parse_frame({"pts": 0}, "CY2025Q1I")


def test_latest_by_cik_uses_newest_period_then_accession() -> None:
    builder = load_builder()
    rows = [
        builder.FrameRow("0000000001", "A", "US-NY", "2025-06-30", 10.0, "acc-2"),
        builder.FrameRow("0000000001", "A", "US-NY", "2026-06-30", 20.0, "acc-1"),
        builder.FrameRow("0000000001", "A", "US-NY", "2026-06-30", 30.0, "acc-3"),
        builder.FrameRow("0000000002", "B", "CA-ON", "2025-06-30", 5.0, "acc-4"),
    ]
    latest = builder.latest_by_cik(rows)
    assert latest["0000000001"].value == 30.0
    assert latest["0000000002"].value == 5.0


def test_address_country_reads_canadian_provinces_and_us_states() -> None:
    builder = load_builder()
    canada = {"business": {"stateOrCountry": "A6", "isForeignLocation": 0}}
    assert builder.address_country(canada) == "CA"
    quebec = {"mailing": {"stateOrCountryDescription": "Quebec, Canada"}}
    assert builder.address_country(quebec) == "CA"
    assert builder.address_country({"business": {"stateOrCountry": "NY"}}) == "US"
    assert (
        builder.address_country(
            {"business": {"stateOrCountry": None, "countryCode": "A6", "isForeignLocation": 1}}
        )
        == "CA"
    )
    assert (
        builder.address_country(
            {"business": {"countryCode": "E9", "country": "Cayman Islands", "isForeignLocation": 1}}
        )
        is None
    )
    assert builder.address_country(None) is None


def test_annual_filer_type_takes_latest_annual_form_after_cutoff() -> None:
    builder = load_builder()
    cutoff = date(2024, 10, 28)
    filings = [
        ("10-Q", date(2026, 8, 1)),
        ("40-F", date(2026, 3, 1)),
        ("10-K", date(2024, 2, 1)),
        ("10-K/A", date(2026, 6, 1)),
    ]
    assert builder.annual_filer_type(filings, cutoff) == "40-F"
    assert builder.annual_filer_type([("10-K", date(2024, 2, 1))], cutoff) is None
    tie = [("20-F", date(2025, 3, 1)), ("10-K", date(2025, 3, 1))]
    assert builder.annual_filer_type(tie, cutoff) == "10-K"
    assert builder.annual_filer_type([], cutoff) is None


def test_primary_ticker_prefers_the_sec_order_then_a_plain_symbol() -> None:
    builder = load_builder()
    assert builder.primary_ticker(("RY", "RYLBF"), ("RY", "RYLBF")) == "RY"
    assert builder.primary_ticker((), ("ABC-PD", "ABC-WT", "ABC")) == "ABC"
    assert builder.primary_ticker((), ("ZED", "AB")) == "AB"
    assert builder.primary_ticker((), ("AAA-WT",)) is None
    assert builder.primary_ticker((), ()) is None


def test_sic_excluded_covers_funds_trusts_and_blank_checks() -> None:
    builder = load_builder()
    assert builder.sic_excluded("6221")
    assert builder.sic_excluded(6770)
    assert not builder.sic_excluded("6029")
    assert not builder.sic_excluded(None)


def test_adjusted_float_undoes_power_of_ten_scale_errors() -> None:
    builder = load_builder()
    # Universal Display: the 2022 filing jumped from 9.59e9 to 4.38e12.
    history = [
        (date(2021, 6, 30), 9_594_897_936.0),
        (date(2022, 6, 30), 4_384_120_131_000.0),
        (date(2025, 6, 30), 6_792_425_456_000.0),
    ]
    assert builder.adjusted_float(6_792_425_456_000.0, history) == pytest.approx(6_792_425_456.0)
    # Cabot: a million-fold jump between 2024 and 2025.
    history = [(date(2024, 3, 31), 5_052_810_968.0), (date(2025, 3, 31), 4_429_047_299_000_000.0)]
    assert builder.adjusted_float(4_429_047_299_000_000.0, history) == pytest.approx(
        4_429_047_299.0
    )
    # HIVE: the error appears in the only pair of observations on file.
    history = [(date(2025, 9, 30), 950_740_231.0), (date(2026, 3, 31), 950_740_231_000.0)]
    assert builder.adjusted_float(950_740_231_000.0, history) == pytest.approx(950_740_231.0)
    # Real growth below the jump threshold is never rescaled.
    history = [(date(2021, 6, 30), 500_000_000_000.0), (date(2025, 6, 30), 4_000_000_000_000.0)]
    assert builder.adjusted_float(4_000_000_000_000.0, history) == 4_000_000_000_000.0
    assert builder.adjusted_float(5.0, []) == 5.0


def test_float_history_reads_usd_observations() -> None:
    builder = load_builder()
    document = {
        "units": {
            "USD": [
                {"end": "2024-06-30", "val": 100},
                {"end": "2025-06-30", "val": 200},
            ]
        }
    }
    assert builder.float_history(document, "CIK0000000001") == [
        (date(2024, 6, 30), 100.0),
        (date(2025, 6, 30), 200.0),
    ]
    assert builder.float_history({"units": {}}, "CIK0000000001") == []
    with pytest.raises(builder.UniverseBuildError):
        builder.float_history({"units": {"USD": [{"val": 1}]}}, "CIK0000000001")


# --------------------------------------------------------------------------
# Selection and the end-to-end offline build
# --------------------------------------------------------------------------


def issuer(ticker: str, cik: str, country: str = "CA", form: str = "40-F") -> Issuer:
    return Issuer(ticker=ticker, cik=cik, name=f"{ticker} Inc.", country=country, filer_type=form)


def test_merge_universe_keeps_seeded_rows_and_adds_all_canadian_rows() -> None:
    builder = load_builder()
    seeded = [issuer("AAA", "0000000001", "CA"), issuer("BBB", "0000000002", "US", "10-K")]
    canadian = [
        issuer("DDD", "0000000004"),
        issuer("CCC", "0000000003"),
        issuer("CCC", "0000000003"),
    ]
    us = [issuer("EEE", "0000000005", "US", "10-K"), issuer("FFF", "0000000006", "US", "10-K")]
    rows = builder.merge_universe(seeded, canadian, us, target_total=5, min_us=1)
    assert [row.ticker for row in rows] == ["AAA", "CCC", "DDD", "BBB", "EEE"]
    assert rows[0] == seeded[0]


def test_merge_universe_keeps_a_us_floor_when_canada_reaches_the_target() -> None:
    builder = load_builder()
    seeded = [issuer("AAA", "0000000001")]
    canadian = [issuer("CCC", "0000000003"), issuer("DDD", "0000000004")]
    us = [issuer("EEE", "0000000005", "US", "10-K"), issuer("FFF", "0000000006", "US", "10-K")]
    rows = builder.merge_universe(seeded, canadian, us, target_total=2, min_us=2)
    assert {"EEE", "FFF"} <= {row.ticker for row in rows}


def test_merge_universe_rejects_a_duplicate_ticker() -> None:
    builder = load_builder()
    with pytest.raises(UniverseError):
        builder.merge_universe(
            [issuer("AAA", "0000000001")],
            [issuer("AAA", "0000000009")],
            [],
            target_total=10,
            min_us=1,
        )


def build_cache(tmp_path: Path) -> Path:
    """Write a tiny offline raw cache: tickers, two frames, four submissions."""
    builder = load_builder()
    cache = tmp_path / "cache"
    write_json(
        cache / "raw" / "company_tickers" / "_" / f"{STAMP}.json",
        {
            "0": {"cik_str": 1, "ticker": "ZZA", "title": "US Big"},
            "1": {"cik_str": 2, "ticker": "ZZB", "title": "US Small"},
            "2": {"cik_str": 3, "ticker": "ZZC", "title": "Canadian"},
            "3": {"cik_str": 4, "ticker": "ZZD", "title": "Canadian Trust"},
        },
    )
    store = builder.DocumentStore(cache)
    periods = builder.frame_periods(date(2026, 10, 27))
    float_period = periods[2]
    store.write(
        builder.frame_key(*builder.FLOAT_SPEC, float_period),
        json.dumps(
            {
                "taxonomy": "dei",
                "tag": "EntityPublicFloat",
                "uom": "USD",
                "data": [
                    {
                        "cik": 1,
                        "entityName": "US Big",
                        "loc": "US-NY",
                        "end": "2026-06-30",
                        "val": 100_000_000_000,
                        "accn": "acc-1",
                    },
                    {
                        "cik": 2,
                        "entityName": "US Small",
                        "loc": "US-CA",
                        "end": "2026-06-30",
                        "val": 1_000_000_000,
                        "accn": "acc-2",
                    },
                    {
                        "cik": 3,
                        "entityName": "Canadian",
                        "loc": "CA-ON",
                        "end": "2026-06-30",
                        "val": 5_000_000_000,
                        "accn": "acc-3",
                    },
                ],
            }
        ).encode(),
        url="https://data.sec.gov/api/xbrl/frames/test",
        http_status=200,
    )
    store.write(
        builder.frame_key(*builder.COVER_SPEC, float_period),
        json.dumps(
            {
                "taxonomy": "dei",
                "tag": "EntityCommonStockSharesOutstanding",
                "uom": "shares",
                "data": [
                    {
                        "cik": 4,
                        "entityName": "Canadian Trust",
                        "loc": "CA-ON",
                        "end": "2026-06-30",
                        "val": 25_000_000,
                        "accn": "acc-4",
                    },
                    {
                        "cik": 5,
                        "entityName": "Unlisted Canadian",
                        "loc": "CA-BC",
                        "end": "2026-06-30",
                        "val": 15_000_000,
                        "accn": "acc-5",
                    },
                ],
            }
        ).encode(),
        url="https://data.sec.gov/api/xbrl/frames/test",
        http_status=200,
    )
    submissions = {
        1: {"name": "US Big", "ticker": "ZZA", "state": "NY", "sic": "7372", "form": "10-K"},
        2: {"name": "US Small", "ticker": "ZZB", "state": "CA", "sic": "7372", "form": "10-K"},
        3: {"name": "Canadian", "ticker": "ZZC", "state": "A6", "sic": "6029", "form": "40-F"},
        4: {"name": "Trust", "ticker": "ZZD", "state": "A6", "sic": "6221", "form": "40-F"},
        5: {"name": "Unlisted", "ticker": "ZZE", "state": "A6", "sic": "6029", "form": "40-F"},
    }
    for cik, spec in submissions.items():
        address = {"business": {"stateOrCountry": spec["state"], "isForeignLocation": 0}}
        write_json(
            cache / "raw" / "submissions" / f"{cik:010d}" / f"{STAMP}.json",
            {
                "cik": str(cik),
                "name": spec["name"],
                "tickers": [spec["ticker"]],
                "addresses": address,
                "sic": spec["sic"],
                "filings": {
                    "recent": {
                        "form": ["8-K", spec["form"]],
                        "filingDate": ["2026-09-01", "2026-02-01"],
                    }
                },
            },
        )
    return cache


def test_offline_build_applies_every_selection_rule(tmp_path: Path) -> None:
    builder = load_builder()
    cache = build_cache(tmp_path)
    report = builder.build(
        cache,
        as_of=date(2026, 10, 27),
        offline=True,
        target_total=45,
        min_us=2,
    )
    rows = {issuer.cik: issuer for issuer in report.rows}
    assert "0000000003" in rows
    assert rows["0000000003"].country == "CA"
    assert rows["0000000003"].filer_type == "40-F"
    assert "0000000001" in rows
    assert rows["0000000001"].country == "US"
    assert "0000000004" not in rows, "a commodity trust must be excluded"
    assert "0000000005" not in rows, "an issuer without a mapped ticker must be excluded"
    seeded = {issuer.cik for issuer in load_universe(SMALL_PATH)}
    assert seeded <= set(rows), "every seeded issuer must survive selection"
    assert report.canadian_rows == 1
    assert report.us_rows == 2
    assert report.rejected_no_ticker == 1
    assert report.rejected_fund == 1
    assert report.requests == 0
    keys = [(row.country, row.ticker) for row in report.rows]
    assert keys == sorted(keys)


def test_offline_build_writes_the_same_bytes_twice(tmp_path: Path) -> None:
    builder = load_builder()
    cache = build_cache(tmp_path)
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    for path in (first, second):
        report = builder.build(
            cache,
            as_of=date(2026, 10, 27),
            offline=True,
            target_total=45,
            min_us=2,
        )
        write_universe(path, report.rows)
    assert first.read_bytes() == second.read_bytes()
