"""Tests for the issuer universe: load, validate, resolve (FR-1.1).

Everything runs offline: SEC's ticker mapping is served from the committed
``tests/fixtures/sec`` cache through :class:`~edgarpit.sec_client.SecClient`.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from edgarpit.config import Settings
from edgarpit.sec_client import SecClient, company_tickers_url
from edgarpit.universe import (
    CikDisagreement,
    Issuer,
    UniverseError,
    load_universe,
    resolve,
    ticker_map,
    write_universe,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = REPO_ROOT / "config" / "universe.csv"
FIXTURES = Path(__file__).parent / "fixtures" / "sec"
HEADER = "ticker,cik,name,country,filer_type\n"
APPLE = ("AAPL", "0000320193", "Apple Inc.", "US", "10-K")
RY = ("RY", "0001000275", "ROYAL BANK OF CANADA", "CA", "40-F")


def company_tickers_doc() -> dict[str, Any]:
    paths = sorted((FIXTURES / "raw" / "company_tickers" / "_").glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def offline_client() -> SecClient:
    return SecClient(Settings(sec_user_agent=None), offline_dir=FIXTURES)


def write_rows(path: Path, rows: list[tuple[str, ...]], header: str = HEADER) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "".join(",".join(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_load_committed_universe() -> None:
    issuers = load_universe(UNIVERSE_PATH)
    assert len(issuers) == 40
    assert [issuer.ticker for issuer in issuers[:2]] == ["RY", "TD"]
    assert issuers[0] == Issuer(
        ticker="RY",
        cik="0001000275",
        name="ROYAL BANK OF CANADA",
        country="CA",
        filer_type="40-F",
    )
    assert all(issuer.cik is not None for issuer in issuers)
    assert {issuer.country for issuer in issuers} == {"CA", "US"}
    assert {issuer.filer_type for issuer in issuers} <= {"10-K", "40-F", "20-F"}


def test_load_preserves_row_order_and_allows_a_missing_cik(tmp_path: Path) -> None:
    path = write_rows(tmp_path / "u.csv", [APPLE, ("SHOP", "", "Shopify Inc.", "CA", "10-K"), RY])
    issuers = load_universe(path)
    assert [issuer.ticker for issuer in issuers] == ["AAPL", "SHOP", "RY"]
    assert issuers[1].cik is None
    assert issuers[2].cik == "0001000275"


def test_load_requires_the_frozen_header(tmp_path: Path) -> None:
    path = write_rows(
        tmp_path / "u.csv",
        [APPLE],
        header="ticker,cik,name,country,filer\n",
    )
    with pytest.raises(UniverseError, match="header must be ticker,cik,name,country,filer_type"):
        load_universe(path)


def test_load_rejects_an_empty_universe(tmp_path: Path) -> None:
    path = tmp_path / "u.csv"
    path.write_text(HEADER, encoding="utf-8")
    with pytest.raises(UniverseError, match="has no issuers"):
        load_universe(path)


def test_load_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(UniverseError, match="cannot read universe"):
        load_universe(tmp_path / "absent.csv")


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (("AAPL", "0000320193", "", "US", "10-K"), "name is empty"),
        (("AAPL", "320193", "Apple Inc.", "US", "10-K"), "must be 10 digits"),
        (("AAPL", "000032019a", "Apple Inc.", "US", "10-K"), "must be 10 digits"),
        (("AAPL", "0000320193", "Apple Inc.", "GB", "10-K"), "country 'GB'"),
        (("AAPL", "0000320193", "Apple Inc.", "US", "8-K"), "filer_type '8-K'"),
        (("", "0000320193", "Apple Inc.", "US", "10-K"), "ticker is empty"),
    ],
)
def test_load_rejects_bad_rows(tmp_path: Path, row: tuple[str, ...], message: str) -> None:
    path = write_rows(tmp_path / "u.csv", [row])
    with pytest.raises(UniverseError, match=message):
        load_universe(path)


def test_load_rejects_duplicate_tickers(tmp_path: Path) -> None:
    path = write_rows(tmp_path / "u.csv", [RY, RY])
    with pytest.raises(UniverseError, match="ticker RY already on line 2"):
        load_universe(path)


def test_load_rejects_duplicate_ciks(tmp_path: Path) -> None:
    path = write_rows(tmp_path / "u.csv", [RY, ("TOO", RY[1], "Two Inc.", "CA", "40-F")])
    with pytest.raises(UniverseError, match="CIK 0001000275 already used by ticker RY"):
        load_universe(path)


def test_ticker_map_reads_the_sorted_fixture_entries() -> None:
    document = company_tickers_doc()
    mapping = ticker_map(document)
    assert mapping["AAPL"] == "0000320193"
    assert mapping["RY"] == "0001000275"
    assert mapping["SHOP"] == "0001594805"
    assert len(mapping) == 45


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({}, "no entries"),
        ({"0": "not an object"}, "not an object"),
        ({"0": {"cik_str": 1}}, "has no ticker"),
        ({"0": {"ticker": "", "cik_str": 1}}, "has no ticker"),
        ({"0": {"ticker": "AAPL", "cik_str": "320193"}}, "invalid cik_str"),
        ({"0": {"ticker": "AAPL", "cik_str": 0}}, "invalid cik_str"),
        ({"0": {"ticker": "AAPL", "cik_str": True}}, "invalid cik_str"),
        (
            {"0": {"ticker": "AAPL", "cik_str": 320193}, "1": {"ticker": "AAPL", "cik_str": 1}},
            "maps ticker AAPL to both",
        ),
    ],
)
def test_ticker_map_rejects_malformed_documents(document: dict[str, Any], message: str) -> None:
    with pytest.raises(UniverseError, match=message):
        ticker_map(document)


def test_resolve_committed_universe_offline_is_clean() -> None:
    issuers = load_universe(UNIVERSE_PATH)
    with offline_client() as client:
        document = client.get_json(company_tickers_url())
    report = resolve(issuers, document)
    assert report.ok
    assert report.issuers == issuers
    assert report.filled == []
    assert report.disagreements == []
    assert report.unresolved == []


def test_resolve_fills_a_missing_cik() -> None:
    issuers = [Issuer("AAPL", None, "Apple Inc.", "US", "10-K")]
    report = resolve(issuers, company_tickers_doc())
    assert report.ok
    assert report.filled == [("AAPL", "0000320193")]
    assert report.issuers[0].cik == "0000320193"
    assert issuers[0].cik is None  # the input is never mutated


def test_resolve_flags_a_wrong_cik_without_rewriting_it() -> None:
    issuers = [Issuer("AAPL", "0000000001", "Apple Inc.", "US", "10-K")]
    report = resolve(issuers, company_tickers_doc())
    assert not report.ok
    assert report.disagreements == [CikDisagreement("AAPL", "0000000001", "0000320193")]
    assert report.filled == []
    assert report.unresolved == []
    assert report.issuers[0].cik == "0000000001"


def test_resolve_lists_unknown_tickers() -> None:
    issuers = [
        Issuer("ZZZZ", "0000000001", "Nowhere Inc.", "US", "10-K"),
        Issuer("SHOP", "0001594805", "Shopify Inc.", "CA", "10-K"),
    ]
    report = resolve(issuers, company_tickers_doc())
    assert not report.ok
    assert report.unresolved == ["ZZZZ"]
    assert report.disagreements == []
    assert report.issuers == issuers


def test_write_universe_preserves_columns_and_order(tmp_path: Path) -> None:
    issuers = load_universe(UNIVERSE_PATH)
    target = write_universe(tmp_path / "out" / "universe.csv", issuers)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "ticker,cik,name,country,filer_type"
    assert len(lines) == len(issuers) + 1
    assert load_universe(target) == issuers
    with target.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[1] == ["RY", "0001000275", "ROYAL BANK OF CANADA", "CA", "40-F"]


def test_write_universe_writes_a_missing_cik_as_empty(tmp_path: Path) -> None:
    issuer = Issuer("SHOP", None, "Shopify Inc.", "CA", "10-K")
    target = write_universe(tmp_path / "u.csv", [issuer])
    assert target.read_text(encoding="utf-8") == (
        "ticker,cik,name,country,filer_type\nSHOP,,Shopify Inc.,CA,10-K\n"
    )
    assert load_universe(target)[0].cik is None
