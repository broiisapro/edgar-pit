"""Checks for the Bank of Canada Valet FX fixtures under ``tests/fixtures/fx``.

These tests never touch the network.  They verify the tree produced by
``tools/build_fx_fixture.py``: layout, manifest hashes, the Valet response
shape, observation ordering and positivity, the coverage of each series, and
the hand-checked conversion cases in ``tests/fixtures/fx_expected.json``.

The six expected cases encode decision A-32: a conversion uses the latest
observation published on or before the period end, so a weekend or a Bank of
Canada holiday falls back to the previous business day, and a period end
before the first observation has no rate at all.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import itertools
import json
import re
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
FX_ROOT = FIXTURE_ROOT / "fx"
RAW_DIR = FX_ROOT / "raw"
MANIFEST_PATH = RAW_DIR / "manifest.jsonl"
EXPECTED_PATH = FIXTURE_ROOT / "fx_expected.json"
REPO_ROOT = Path(__file__).resolve().parents[1]

DAILY_SERIES = "FXUSDCAD"
NOON_SERIES = "IEXE0101"
STAMP_RE = re.compile(r"\d{8}T\d{6}\d{6}Z")

# Hand-checked against the Bank of Canada's published USD/CAD observations for
# these dates (Valet FXUSDCAD).  Rate dates are business days; 2016-12-30 is
# before the series starts, so no rate exists.  Keep in step with
# tests/fixtures/fx_expected.json.
HAND_CHECKED_CASES: tuple[tuple[str, str | None, str | None], ...] = (
    ("2016-12-30", None, None),
    ("2017-01-03", "2017-01-03", "1.3435"),
    ("2023-10-31", "2023-10-31", "1.3871"),
    ("2024-03-24", "2024-03-22", "1.3593"),
    ("2024-03-29", "2024-03-28", "1.3550"),
    ("2024-09-02", "2024-08-30", "1.3491"),
)


def load_builder() -> ModuleType:
    """Import ``tools/build_fx_fixture.py`` by path (it is not a package)."""
    path = REPO_ROOT / "tools" / "build_fx_fixture.py"
    spec = importlib.util.spec_from_file_location("build_fx_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_manifest() -> list[dict[str, Any]]:
    """Return the fixture manifest rows."""
    rows = [
        json.loads(line)
        for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows, "manifest.jsonl is empty"
    return rows


def load_series(series: str) -> dict[str, Any]:
    """Return the single stored Valet response for ``series``."""
    paths = sorted((RAW_DIR / "fx" / "_" / series).glob("*.json"))
    assert len(paths) == 1, f"expected one fixture file for {series}, got {paths}"
    return json.loads(paths[0].read_text(encoding="utf-8"))


def observations(series: str) -> list[tuple[date, Decimal]]:
    """Return ``(rate_date, value)`` pairs of ``series`` in file order."""
    document = load_series(series)
    return [
        (date.fromisoformat(observation["d"]), Decimal(observation[series]["v"]))
        for observation in document["observations"]
    ]


def latest_on_or_before(
    rows: Sequence[tuple[date, Decimal]], period_end: date
) -> tuple[date, Decimal] | None:
    """Return the latest observation dated on or before ``period_end``.

    This is the lookup rule of decision A-32; it is repeated here so the
    fixture cases can be checked without the conversion module.
    """
    chosen: tuple[date, Decimal] | None = None
    for rate_date, value in rows:
        if rate_date > period_end:
            break
        chosen = (rate_date, value)
    return chosen


def load_expected_cases() -> list[dict[str, Any]]:
    """Return the hand-checked conversion cases."""
    document = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, list), "fx_expected.json must be a JSON array"
    return document


def test_fixture_tree_layout() -> None:
    rows = load_manifest()
    stored = {path.relative_to(FX_ROOT).as_posix() for path in RAW_DIR.rglob("*") if path.is_file()}
    stored.discard("raw/manifest.jsonl")
    assert stored == {row["path"] for row in rows}, "manifest and stored files disagree"

    for row in rows:
        parts = row["path"].split("/")
        assert len(parts) == 5, row["path"]
        assert parts[0:3] == ["raw", "fx", "_"], row["path"]
        series = parts[3]
        assert series in {DAILY_SERIES, NOON_SERIES}, row["path"]
        stamp = parts[-1].removesuffix(".json")
        assert STAMP_RE.fullmatch(stamp), f"bad fetched_at stamp in {row['path']}"
        assert row["fetched_at"] == stamp
        assert row["endpoint"] == "fx"
        assert row["cik"] == "", "the fx endpoint has no issuer (cik '_' in the path)"
        assert row["trimmed"] is False
        assert row["http_status"] == 200
        assert row["url"] == (f"https://www.bankofcanada.ca/valet/observations/{series}/json")


def test_manifest_hashes_match_files() -> None:
    for row in load_manifest():
        body = (FX_ROOT / row["path"]).read_bytes()
        assert len(body) == row["bytes"], row["path"]
        assert hashlib.sha256(body).hexdigest() == row["sha256"], row["path"]


def test_fixture_parses_as_valet_observations() -> None:
    for series in (DAILY_SERIES, NOON_SERIES):
        document = load_series(series)
        details = document["seriesDetail"][series]
        assert details["dateFormat"] == "daily"
        assert details["dimension"] == {"key": "d", "name": "Date"}
        assert document["observations"], series
        for observation in document["observations"]:
            assert isinstance(observation["d"], str)
            assert isinstance(observation[series]["v"], str)

    daily = load_series(DAILY_SERIES)["seriesDetail"][DAILY_SERIES]
    assert daily["label"] == "USD/CAD"
    assert daily["metadata"] == {
        "unit": "Canadian dollars per US dollar",
        "frequency": "Business daily",
    }


def test_observation_dates_strictly_increase() -> None:
    for series in (DAILY_SERIES, NOON_SERIES):
        dates = [rate_date for rate_date, _ in observations(series)]
        assert dates, series
        assert all(earlier < later for earlier, later in itertools.pairwise(dates)), series


def test_every_value_is_positive() -> None:
    for series in (DAILY_SERIES, NOON_SERIES):
        for rate_date, value in observations(series):
            assert value > 0, f"{series} {rate_date}"


def test_series_cover_their_published_windows() -> None:
    daily = observations(DAILY_SERIES)
    assert daily[0][0] == date(2017, 1, 3), "FXUSDCAD starts on its first published date"
    assert daily[-1][0] >= date(2026, 1, 1), "FXUSDCAD fixture is stale"
    assert all(
        (later - earlier).days <= 7 for (earlier, _), (later, _) in itertools.pairwise(daily)
    )

    noon = observations(NOON_SERIES)
    assert noon[0][0] == date(2007, 5, 1)
    assert noon[-1][0] == date(2017, 4, 28), "the retired noon series is frozen"
    assert all((later - earlier).days <= 7 for (earlier, _), (later, _) in itertools.pairwise(noon))


def test_expected_file_matches_the_hand_checked_cases() -> None:
    cases = load_expected_cases()
    assert len(cases) == 6
    for case in cases:
        assert set(case) == {"period_end", "expected_rate_date", "expected_rate"}
    assert [
        (
            case["period_end"],
            case["expected_rate_date"],
            None if case["expected_rate"] is None else Decimal(str(case["expected_rate"])),
        )
        for case in cases
    ] == [
        (
            period_end,
            rate_date,
            None if rate is None else Decimal(rate),
        )
        for period_end, rate_date, rate in HAND_CHECKED_CASES
    ]


def test_expected_cases_cover_the_required_scenarios() -> None:
    cases = {case["period_end"]: case for case in load_expected_cases()}
    assert "2016-12-30" in cases, "pre-series period end"
    assert "2023-10-31" in cases, "Royal Bank FY2023 year end"

    weekend = [
        case for case in cases.values() if date.fromisoformat(case["period_end"]).weekday() >= 5
    ]
    assert weekend, "no period end on a weekend"

    daily_dates = {rate_date for rate_date, _ in observations(DAILY_SERIES)}
    holidays = [
        case
        for case in cases.values()
        if date.fromisoformat(case["period_end"]) not in daily_dates
        and date.fromisoformat(case["period_end"]).weekday() < 5
    ]
    assert holidays, "no period end on a Bank of Canada holiday"


def test_expected_cases_are_consistent_with_the_fixture() -> None:
    rows = observations(DAILY_SERIES)
    for case in load_expected_cases():
        period_end = date.fromisoformat(case["period_end"])
        found = latest_on_or_before(rows, period_end)
        if case["expected_rate"] is None:
            assert found is None, case
            assert case["expected_rate_date"] is None
            continue
        assert found is not None, case
        rate_date, value = found
        assert value == Decimal(str(case["expected_rate"])), case
        assert rate_date == date.fromisoformat(case["expected_rate_date"]), case
        assert rate_date <= period_end, "a rate can never come from after the period end"


def test_pre_2017_noon_series_overlaps_the_daily_series() -> None:
    daily = {rate_date for rate_date, _ in observations(DAILY_SERIES)}
    noon = {rate_date for rate_date, _ in observations(NOON_SERIES)}
    overlap = sorted(daily & noon)
    assert len(overlap) >= 80, "the two methodologies overlap for about four months"
    assert overlap[0] == date(2017, 1, 3)
    assert overlap[-1] == date(2017, 4, 28)


def test_builder_uses_only_the_standard_library() -> None:
    tree = ast.parse((REPO_ROOT / "tools" / "build_fx_fixture.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= sys.stdlib_module_names, imported - sys.stdlib_module_names


def test_builder_stays_within_the_request_budget() -> None:
    builder = load_builder()
    assert builder.SERIES == (DAILY_SERIES, NOON_SERIES)
    assert builder.MAX_ATTEMPTS <= 2, "at most two requests per series"
    assert builder.observation_url(DAILY_SERIES) == (
        "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
    )


def test_builder_validates_a_valet_response() -> None:
    builder = load_builder()

    def document(observations: Any) -> dict[str, Any]:
        return {
            "seriesDetail": {DAILY_SERIES: {"dateFormat": "daily"}},
            "observations": observations,
        }

    valid = document(
        [
            {"d": "2017-01-03", DAILY_SERIES: {"v": "1.3435"}},
            {"d": "2017-01-04", DAILY_SERIES: {"v": "1.3315"}},
        ]
    )
    builder.validate_document(valid, DAILY_SERIES)

    broken = {
        "missing series": {"seriesDetail": {}, "observations": valid["observations"]},
        "monthly": {
            "seriesDetail": {DAILY_SERIES: {"dateFormat": "monthly"}},
            "observations": valid["observations"],
        },
        "empty": document([]),
        "unsorted": document(list(reversed(valid["observations"]))),
        "duplicate date": document([valid["observations"][0], valid["observations"][0]]),
        "bad date": document([{"d": "03/01/2017", DAILY_SERIES: {"v": "1.3435"}}]),
        "no value": document([{"d": "2017-01-03", DAILY_SERIES: {}}]),
        "zero": document([{"d": "2017-01-03", DAILY_SERIES: {"v": "0"}}]),
        "negative": document([{"d": "2017-01-03", DAILY_SERIES: {"v": "-1.3435"}}]),
        "not a number": document([{"d": "2017-01-03", DAILY_SERIES: {"v": "n/a"}}]),
    }
    for _reason, candidate in broken.items():
        with pytest.raises(builder.FixtureError):
            builder.validate_document(candidate, DAILY_SERIES)


def test_builder_stores_bytes_unchanged_and_replaces_older_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = load_builder()
    monkeypatch.setattr(builder, "FIXTURE_ROOT", tmp_path)
    body = b'{"observations": [{"d": "2017-01-03", "FXUSDCAD": {"v": "1.3435"}}]}\n'
    rows: list[dict[str, Any]] = []
    first = builder.store_series(
        rows,
        series=DAILY_SERIES,
        url=builder.observation_url(DAILY_SERIES),
        body=body,
        http_status=200,
    )
    assert first.read_bytes() == body
    assert len(rows) == 1
    assert rows[0]["bytes"] == len(body)
    assert rows[0]["sha256"] == hashlib.sha256(body).hexdigest()
    assert rows[0]["trimmed"] is False

    monkeypatch.setattr(builder, "now_stamp", lambda: "20260101T000000000000Z")
    second = builder.store_series(
        rows,
        series=DAILY_SERIES,
        url=builder.observation_url(DAILY_SERIES),
        body=body,
        http_status=200,
    )
    assert second.name == "20260101T000000000000Z.json"
    assert not first.exists(), "the old file of a refreshed series is removed"
    assert len(rows) == 1, "the manifest keeps one row per series"
