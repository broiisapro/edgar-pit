"""Offline checks for ``config/universe_large.csv`` and its builder (issue #115).

The CSV is committed data, so these tests keep it well-formed without touching
the network: columns, unique 10-digit CIKs, country and filer-type domains,
sort order, size, and that it is a superset of ``config/universe.csv``. The
builder's rules are exercised on small in-memory fixtures, including an
end-to-end ``build(..., offline=True)`` over a temporary raw cache: Canadian
issuers come from the submissions address, US issuers from the public-float
frame, and every seeded row survives.
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
from typing import Any

import pytest

from edgarpit.rawcache import RawCache
from edgarpit.sec_client import frames_url
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
AS_OF = date(2026, 10, 27)
PER_COUNTRY = 25


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


def write_doc(cache: Path, parts: list[str], payload: Any) -> None:
    """Write one uncompressed cache document under ``raw/<parts>/<stamp>.json``."""
    directory = cache.joinpath("raw", *parts)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{STAMP}.json").write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------
# The committed CSV
# --------------------------------------------------------------------------


def test_header_and_country_balance() -> None:
    rows = read_csv(LARGE_PATH)
    assert rows, "universe_large.csv is empty"
    assert list(rows[0]) == COLUMNS
    assert 240 <= len(rows) <= 260, len(rows)
    canada = [row for row in rows if row["country"] == "CA"]
    united_states = [row for row in rows if row["country"] == "US"]
    assert len(canada) >= 115, len(canada)
    assert len(united_states) >= 115, len(united_states)
    assert len(canada) + len(united_states) == len(rows)


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
# Frames and companyconcept helpers
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


def test_annual_assets_prefers_the_newest_annual_report() -> None:
    builder = load_builder()
    document = {
        "units": {
            "CAD": [
                {"end": "2024-10-31", "val": 100, "accn": "a-1", "form": "40-F"},
                {"end": "2025-01-31", "val": 120, "accn": "b-1", "form": "6-K"},
                {"end": "2025-10-31", "val": 200, "accn": "c-1", "form": "40-F"},
                {"end": "2025-10-31", "val": 210, "accn": "d-1", "form": "6-K"},
            ]
        }
    }
    fact = builder.annual_assets(document, "CIK0000000003")
    assert fact == builder.AssetFact(
        end=date(2025, 10, 31),
        value=200.0,
        unit="CAD",
        accession="c-1",
        form="40-F",
    )


def test_annual_assets_falls_back_to_an_interim_value() -> None:
    builder = load_builder()
    document = {
        "units": {
            "USD": [
                {"end": "2026-03-31", "val": 90, "accn": "a-1", "form": "10-Q"},
                {"end": "2026-06-30", "val": 95, "accn": "b-1", "form": "10-Q"},
            ]
        }
    }
    fact = builder.annual_assets(document, "CIK0000000004")
    assert fact is not None
    assert (fact.end, fact.value, fact.unit) == (date(2026, 6, 30), 95.0, "USD")


def test_annual_assets_reads_the_newest_row_across_units() -> None:
    builder = load_builder()
    document = {
        "units": {
            "CAD": [{"end": "2025-10-31", "val": 200, "accn": "cad-1", "form": "40-F"}],
            "USD": [{"end": "2024-10-31", "val": 150, "accn": "usd-1", "form": "40-F"}],
        }
    }
    fact = builder.annual_assets(document, "CIK0000000005")
    assert fact is not None
    assert (fact.end, fact.unit) == (date(2025, 10, 31), "CAD")


def test_annual_assets_rejects_malformed_documents() -> None:
    builder = load_builder()
    assert builder.annual_assets({"units": {}}, "CIK") is None
    with pytest.raises(builder.UniverseBuildError):
        builder.annual_assets({"units": {"CAD": [{"val": 1, "form": "40-F"}]}}, "CIK")
    with pytest.raises(builder.UniverseBuildError):
        builder.annual_assets({"units": {"CAD": [{"end": "2025-10-31", "val": None}]}}, "CIK")
    with pytest.raises(builder.UniverseBuildError):
        builder.annual_assets({"units": {"CAD": [{"end": "31/10/2025", "val": 1}]}}, "CIK")
    with pytest.raises(builder.UniverseBuildError):
        builder.annual_assets({"no_units": True}, "CIK")


def test_assets_in_usd_converts_only_cad_and_usd() -> None:
    builder = load_builder()
    usd = builder.AssetFact(date(2025, 10, 31), 100.0, "USD", "a-1", "10-K")
    cad = builder.AssetFact(date(2025, 10, 31), 140.0, "CAD", "a-1", "40-F")
    eur = builder.AssetFact(date(2025, 10, 31), 100.0, "EUR", "a-1", "20-F")
    assert builder.assets_in_usd(usd, 1.4) == 100.0
    assert builder.assets_in_usd(cad, 1.4) == pytest.approx(100.0)
    assert builder.assets_in_usd(usd, None) == 100.0
    assert builder.assets_in_usd(cad, None) is None
    assert builder.assets_in_usd(eur, 1.4) is None


def test_latest_fx_rate_takes_the_newest_observation() -> None:
    builder = load_builder()
    document = {
        "observations": [
            {"d": "2026-10-23", "FXUSDCAD": {"v": "1.3993"}},
            {"d": "2026-10-26"},
            {"d": "2026-10-27", "FXUSDCAD": {"v": "1.4038"}},
        ]
    }
    assert builder.latest_fx_rate(document) == (date(2026, 10, 27), 1.4038)
    assert builder.latest_fx_rate({"observations": []}) is None
    assert builder.latest_fx_rate({"observations": [{"d": "2026-10-27"}]}) is None
    with pytest.raises(builder.UniverseBuildError):
        builder.latest_fx_rate({"no_observations": True})
    with pytest.raises(builder.UniverseBuildError):
        builder.latest_fx_rate({"observations": [{"d": "2026-13-01", "FXUSDCAD": {"v": "1.4"}}]})
    with pytest.raises(builder.UniverseBuildError):
        builder.latest_fx_rate({"observations": [{"d": "2026-10-27", "FXUSDCAD": {"v": "zero"}}]})
    with pytest.raises(builder.UniverseBuildError):
        builder.latest_fx_rate({"observations": [{"d": "2026-10-27", "FXUSDCAD": {"v": "0"}}]})


# --------------------------------------------------------------------------
# Submissions helpers
# --------------------------------------------------------------------------


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


def test_address_country_lets_a_canadian_mailing_address_win() -> None:
    builder = load_builder()
    both = {
        "business": {"stateOrCountry": "NY", "isForeignLocation": 0},
        "mailing": {"stateOrCountry": "A8", "isForeignLocation": 1},
    }
    assert builder.address_country(both) == "CA"
    foreign = {
        "business": {"countryCode": "E9", "country": "Cayman Islands", "isForeignLocation": 1},
        "mailing": {"stateOrCountry": "CA", "isForeignLocation": 0},
    }
    assert builder.address_country(foreign) == "US"


def test_every_canadian_address_code_is_recognised() -> None:
    builder = load_builder()
    for code in ("A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "B0", "Z4"):
        assert builder.address_country({"business": {"stateOrCountry": code}}) == "CA", code


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


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def issuer(ticker: str, cik: str, country: str = "CA", form: str = "40-F") -> Issuer:
    return Issuer(ticker=ticker, cik=cik, name=f"{ticker} Inc.", country=country, filer_type=form)


def test_merge_universe_caps_each_country_and_keeps_seeded_rows() -> None:
    builder = load_builder()
    seeded = [
        issuer("AAA", "0000000001", "CA"),
        issuer("BBB", "0000000002", "US", "10-K"),
        issuer("CCC", "0000000003", "CA"),
    ]
    canadian = [issuer("EEE", "0000000005", "CA"), issuer("FFF", "0000000006", "CA")]
    us = [issuer("GGG", "0000000007", "US", "10-K")]
    rows = builder.merge_universe(seeded, canadian, us, per_country=3)
    # Canada keeps both seeded rows and one ranked row; a duplicate CIK or a
    # ticker clash never reaches the file twice.
    assert [row.ticker for row in rows] == ["AAA", "CCC", "EEE", "BBB", "GGG"]


def test_merge_universe_never_drops_a_seeded_row_at_the_cap() -> None:
    builder = load_builder()
    seeded = [issuer("AAA", "0000000001"), issuer("BBB", "0000000002")]
    rows = builder.merge_universe(seeded, [], [], per_country=1)
    assert [row.ticker for row in rows] == ["AAA", "BBB"]
    assert rows == seeded


def test_merge_universe_skips_a_duplicate_cik_and_rejects_a_duplicate_ticker() -> None:
    builder = load_builder()
    rows = builder.merge_universe(
        [issuer("AAA", "0000000001")],
        [issuer("AAA", "0000000001"), issuer("CCC", "0000000003")],
        [],
        per_country=10,
    )
    assert [row.ticker for row in rows] == ["AAA", "CCC"]
    with pytest.raises(UniverseError):
        builder.merge_universe(
            [issuer("AAA", "0000000001")],
            [issuer("AAA", "0000000009")],
            [],
            per_country=10,
        )


def test_document_reader_serves_the_cache_and_reports_a_miss(tmp_path: Path) -> None:
    builder = load_builder()
    cache = RawCache(tmp_path)
    url = frames_url("dei", "EntityPublicFloat", "USD", "CY2026Q3I")
    cache.write(
        "frames",
        "",
        "dei__EntityPublicFloat__USD__CY2026Q3I",
        b'{"data": []}',
        url=url,
        http_status=200,
    )
    reader = builder.DocumentReader(cache, None, [0])
    assert reader.json(url) == {"data": []}
    assert reader.json(frames_url("dei", "EntityPublicFloat", "USD", "CY2020Q1I")) is None


# --------------------------------------------------------------------------
# The end-to-end offline build
# --------------------------------------------------------------------------


# Canadian issuers by submissions address, with their assets in the reporting
# currency: CIK -> (ticker, name, state code, SIC, form, total assets CAD|USD).
CANADIAN = {
    "0000000003": ("ZZC", "Canadian Bank", "A6", "6029", "40-F", 500_000_000_000),
    "0000000012": ("ZZM", "Canadian Mid", "A8", "6029", "40-F", 300_000_000_000),
    "0000000013": ("ZZO", "Canadian Large", "A1", "6029", "40-F", 400_000_000_000),
    "0000000014": ("ZZP", "Canadian Small", "A2", "6029", "40-F", 50_000_000_000),
    "0000000011": ("ZZN", "Canadian Tiny", "A3", "6029", "40-F", 1_000_000_000),
}
US = {
    "0000000001": ("ZZA", "US Big", "NY", 100_000_000_000),
    "0000000002": ("ZZB", "US Small", "CA", 1_000_000_000),
    "0000000005": ("ZZG", "US No Annual", "TX", 2_000_000_000),
    "0000000009": ("ZZK", "US Float Only", "WA", 3_000_000_000),
}
# The float frame also lists a Canadian issuer with no ticker (rejected) and one
# whose frame location says Canada while its submissions address says New York
# (it must not become a Canadian row).
FLOAT_EXTRAS = {
    "0000000020": ("Unlisted Canadian", "CA-BC", 4_000_000_000),
    "0000000010": ("Frame Canadian US Address", "CA-ON", 5_000_000_000),
}
# Issuers that fail one Canadian selection rule each: a fund, a euro reporter
# and a filer that tags no assets.
CANADIAN_EXTRAS = {
    "0000000004": ("ZZD", "Canadian Trust", "A6", "6221", "Canadian Trust"),
    "0000000006": ("ZZH", "Canadian Euro", "A8", "6029", "Canadian Euro"),
    "0000000007": ("ZZI", "Canadian No Assets", "A6", "6029", "Canadian No Assets"),
}


def build_cache(tmp_path: Path) -> Path:
    """Write a tiny offline raw cache exercising every selection rule."""
    cache = tmp_path / "cache"
    tickers = [(cik, spec[0], spec[1]) for cik, spec in CANADIAN.items()] + [
        (cik, spec[0], spec[1]) for cik, spec in US.items()
    ]
    tickers += [(cik, spec[0], spec[1]) for cik, spec in CANADIAN_EXTRAS.items()]
    tickers += [
        ("0000000008", "ZZJ", "Canadian Interim Only"),
        ("0000000010", "ZZL", "Frame Canadian US Address"),
    ]
    write_doc(
        cache,
        ["company_tickers", "_"],
        {
            str(index): {"cik_str": int(cik), "ticker": ticker, "title": name}
            for index, (cik, ticker, name) in enumerate(tickers)
        },
    )

    float_data = [
        {
            "cik": int(cik),
            "entityName": spec[1],
            "loc": f"US-{spec[2]}",
            "end": "2026-06-30",
            "val": spec[3],
            "accn": f"acc-{cik}",
        }
        for cik, spec in US.items()
    ] + [
        {
            "cik": int(cik),
            "entityName": spec[0],
            "loc": spec[1],
            "end": "2026-06-30",
            "val": spec[2],
            "accn": f"acc-{cik}",
        }
        for cik, spec in FLOAT_EXTRAS.items()
    ]
    write_doc(
        cache,
        ["frames", "_", "dei__EntityPublicFloat__USD__CY2026Q3I"],
        {"taxonomy": "dei", "tag": "EntityPublicFloat", "uom": "USD", "data": float_data},
    )

    def submission(
        name: str, ticker: str, addresses: dict[str, dict[str, Any]], sic: str, forms: list[str]
    ) -> dict[str, Any]:
        filed = ["2026-09-01"] if len(forms) == 1 else ["2026-09-01", "2026-02-01"]
        return {
            "name": name,
            "tickers": [ticker],
            "addresses": addresses,
            "sic": sic,
            "filings": {"recent": {"form": forms, "filingDate": filed}},
        }

    for cik, (ticker, name, state, sic, form, _) in CANADIAN.items():
        write_doc(
            cache,
            ["submissions", cik],
            submission(
                name,
                ticker,
                {"business": {"stateOrCountry": state, "isForeignLocation": 1}},
                sic,
                ["8-K", form],
            ),
        )
    for cik, (ticker, name, state, _assets) in US.items():
        forms = ["8-K"] if cik == "0000000005" else ["8-K", "10-K"]
        write_doc(
            cache,
            ["submissions", cik],
            submission(
                name,
                ticker,
                {"business": {"stateOrCountry": state, "isForeignLocation": 0}},
                "7372",
                forms,
            ),
        )
    for cik, (ticker, name, state, sic, _label) in CANADIAN_EXTRAS.items():
        write_doc(
            cache,
            ["submissions", cik],
            submission(
                name,
                ticker,
                {"business": {"stateOrCountry": state, "isForeignLocation": 1}},
                sic,
                ["40-F"],
            ),
        )
    write_doc(
        cache,
        ["submissions", "0000000008"],
        submission(
            "Canadian Interim Only",
            "ZZJ",
            {"mailing": {"stateOrCountryDescription": "Quebec, Canada"}},
            "6029",
            ["40-F"],
        ),
    )
    write_doc(
        cache,
        ["submissions", "0000000010"],
        submission(
            "Frame Canadian US Address",
            "ZZL",
            {"business": {"stateOrCountry": "NY", "isForeignLocation": 0}},
            "7372",
            ["10-K"],
        ),
    )
    write_doc(
        cache,
        ["submissions", "0000000020"],
        submission(
            "Unlisted Canadian",
            "ZZU",
            {"business": {"stateOrCountry": "A1", "isForeignLocation": 1}},
            "6029",
            ["40-F"],
        ),
    )

    def assets(unit: str, value: float, form: str = "40-F") -> dict[str, Any]:
        return {
            "units": {
                unit: [{"end": "2025-10-31", "val": value, "accn": f"asset-{value}", "form": form}]
            }
        }

    for cik, (_, _, _, _, _, value) in CANADIAN.items():
        write_doc(cache, ["companyconcept", cik, "ifrs-full__Assets"], assets("CAD", float(value)))
    write_doc(
        cache,
        ["companyconcept", "0000000008", "us-gaap__Assets"],
        assets("USD", 100_000_000_000.0, form="10-Q"),
    )
    write_doc(
        cache,
        ["companyconcept", "0000000006", "us-gaap__Assets"],
        assets("EUR", 90_000_000_000.0),
    )
    write_doc(
        cache,
        ["fx", "_", "FXUSDCAD"],
        {"observations": [{"d": "2026-10-27", "FXUSDCAD": {"v": "1.4"}}]},
    )
    return cache


def test_offline_build_applies_every_selection_rule(tmp_path: Path) -> None:
    builder = load_builder()
    cache = build_cache(tmp_path)
    report = builder.build(cache, as_of=AS_OF, offline=True, per_country=PER_COUNTRY)
    rows = {issuer.cik: issuer for issuer in report.rows}

    # The five largest Canadian candidates by assets in USD fill the slots; the
    # smallest (ZZN, 1e9 CAD) is left out by the cap.
    assert rows["0000000003"].country == "CA"
    assert rows["0000000003"].filer_type == "40-F"
    assert {"0000000003", "0000000012", "0000000013", "0000000014", "0000000008"} <= set(rows)
    assert "0000000011" not in rows, "the sixth-largest Canadian candidate misses the cap"
    # The euro reporter, the trust, the asset-less filer and the ticker-less
    # issuer never enter the file; the frame-located Canadian with a US address
    # is not Canadian either.
    assert "0000000006" not in rows
    assert "0000000004" not in rows
    assert "0000000007" not in rows
    assert "0000000020" not in rows
    assert "0000000010" not in rows

    assert rows["0000000001"].country == "US"
    assert rows["0000000001"].filer_type == "10-K"
    assert "0000000005" not in rows, "an issuer without an annual report is excluded"
    assert "0000000009" in rows

    seeded = {issuer.cik for issuer in load_universe(SMALL_PATH)}
    assert seeded <= set(rows), "every seeded issuer must survive selection"
    assert report.canadian_candidates == 10
    assert report.canadian_rows == 5
    assert report.us_rows == 3
    assert report.rejected_fund == 1
    assert report.rejected_currency == 1
    assert report.rejected_no_assets == 1
    assert report.rejected_no_ticker == 1
    assert report.rejected_no_annual == 1
    assert report.requests == 0
    assert report.fx_rate == pytest.approx(1.4)
    assert [(row.country, row.ticker) for row in report.rows] == sorted(
        (row.country, row.ticker) for row in report.rows
    )


def test_offline_build_ranks_canadian_rows_by_total_assets(tmp_path: Path) -> None:
    builder = load_builder()
    cache = build_cache(tmp_path)
    report = builder.build(cache, as_of=AS_OF, offline=True, per_country=PER_COUNTRY)
    largest = [ticker for ticker, _, _ in report.largest_canadian(5)]
    ranked = [ticker for ticker in largest if ticker in {"ZZC", "ZZM", "ZZO", "ZZP", "ZZJ"}]
    assert ranked == ["ZZC", "ZZO", "ZZM", "ZZJ", "ZZP"]


def test_offline_build_keeps_only_issuers_with_canadian_addresses(tmp_path: Path) -> None:
    # The acceptance criterion for the committed CSV, checked against the
    # evidence the offline build reads: every non-seeded Canadian row's cached
    # submissions document carries a Canadian province.
    builder = load_builder()
    cache = build_cache(tmp_path)
    report = builder.build(cache, as_of=AS_OF, offline=True, per_country=PER_COUNTRY)
    seeded = {issuer.cik for issuer in load_universe(SMALL_PATH)}
    canadian = [issuer for issuer in report.rows if issuer.country == "CA" and issuer.cik]
    assert canadian
    for issuer in canadian:
        if issuer.cik in seeded:
            continue
        document = json.loads(
            (cache / "raw" / "submissions" / str(issuer.cik) / f"{STAMP}.json").read_text()
        )
        assert builder.address_country(document["addresses"]) == "CA"
        code = document["addresses"].get("business", {}).get("stateOrCountry")
        if code is not None:
            assert code in builder.CANADIAN_CODES


def test_offline_build_is_deterministic(tmp_path: Path) -> None:
    builder = load_builder()
    cache = build_cache(tmp_path)
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    for path in (first, second):
        report = builder.build(cache, as_of=AS_OF, offline=True, per_country=PER_COUNTRY)
        write_universe(path, report.rows)
    assert first.read_bytes() == second.read_bytes()


def test_offline_build_reports_a_cache_without_company_tickers(tmp_path: Path) -> None:
    builder = load_builder()
    with pytest.raises(builder.UniverseBuildError, match="company_tickers"):
        builder.build(tmp_path / "empty", as_of=AS_OF, offline=True, per_country=PER_COUNTRY)
