"""Tests for the reference universe a build loads (decision A-20).

``build_from_raw`` loads ``raw.universe`` with :func:`edgarpit.load.reference_universe`,
which unions the project's universe files and keeps the issuers the raw cache
holds. The build-level assertions live in ``test_load.py`` next to the other
build tests; this module pins the selection rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from edgarpit.load import (
    DEFAULT_UNIVERSE_CSV,
    DEFAULT_UNIVERSE_LARGE_CSV,
    reference_universe,
)

RY_CIK = "0001000275"
APPLE_CIK = "0000320193"
# In config/universe_large.csv only: the 40-issuer file predates the large one.
AAUC_CIK = "0001993344"
UNKNOWN_CIK = "0000000001"

HEADER = "ticker,cik,name,country,filer_type\n"


def write_universe(path: Path, rows: list[str]) -> Path:
    path.write_text(HEADER + "".join(row + "\n" for row in rows), encoding="utf-8")
    return path


def test_the_two_project_files_describe_the_universe() -> None:
    assert DEFAULT_UNIVERSE_CSV.name == "universe.csv"
    assert DEFAULT_UNIVERSE_LARGE_CSV.name == "universe_large.csv"
    assert DEFAULT_UNIVERSE_CSV.is_file()
    assert DEFAULT_UNIVERSE_LARGE_CSV.is_file()


def test_reference_universe_keeps_only_the_issuers_the_cache_holds() -> None:
    frame = reference_universe({RY_CIK, AAUC_CIK, UNKNOWN_CIK})

    assert frame.columns == ["ticker", "cik", "name", "country", "filer_type"]
    assert frame.get_column("cik").to_list() == sorted([RY_CIK, AAUC_CIK])
    assert frame.get_column("ticker").to_list() == ["RY", "AAUC"]
    assert frame.height == frame.get_column("cik").n_unique()


def test_reference_universe_takes_an_issuer_listed_in_both_files_once() -> None:
    frame = reference_universe({RY_CIK})

    assert frame.height == 1
    assert frame.row(0) == ("RY", RY_CIK, "ROYAL BANK OF CANADA", "CA", "40-F")


def test_reference_universe_returns_nothing_for_an_empty_cache() -> None:
    frame = reference_universe(set())

    assert frame.height == 0
    assert frame.columns == ["ticker", "cik", "name", "country", "filer_type"]


def test_reference_universe_rejects_files_that_disagree_about_an_issuer(
    tmp_path: Path,
) -> None:
    first = write_universe(tmp_path / "first.csv", ["RY,0001000275,ROYAL BANK OF CANADA,CA,40-F"])
    second = write_universe(tmp_path / "second.csv", ["RY,0001000275,ROYAL BANK,CA,10-K"])

    with pytest.raises(ValueError, match=r"disagree about CIK\(s\) \['0001000275'\]"):
        reference_universe({RY_CIK}, paths=(first, second))


def test_reference_universe_accepts_the_same_row_in_two_files(tmp_path: Path) -> None:
    row = "AAPL,0000320193,Apple Inc.,US,10-K"
    first = write_universe(tmp_path / "first.csv", [row])
    second = write_universe(tmp_path / "second.csv", [row])

    frame = reference_universe({APPLE_CIK}, paths=(first, second))

    assert frame.height == 1
    assert frame.row(0) == ("AAPL", APPLE_CIK, "Apple Inc.", "US", "10-K")


def test_reference_universe_names_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="universe CSV not found"):
        reference_universe({RY_CIK}, paths=(tmp_path / "absent.csv",))


def test_reference_universe_rejects_a_malformed_cik(tmp_path: Path) -> None:
    path = write_universe(tmp_path / "bad.csv", ["RY,1000275,ROYAL BANK OF CANADA,CA,40-F"])

    with pytest.raises(ValueError, match="not 10 digits"):
        reference_universe({RY_CIK}, paths=(path,))


def test_reference_universe_requires_at_least_one_file() -> None:
    with pytest.raises(ValueError, match="no universe CSV"):
        reference_universe({RY_CIK}, paths=())
