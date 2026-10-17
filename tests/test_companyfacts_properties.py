"""Property tests for companyfacts flattening and duplicate handling (#21).

The strategies generate companyfacts-shaped documents directly, so the
deliberate cases exercise the flattening rather than the JSON parser: several
taxonomies, concepts and units; entries with and without ``start`` and
``frame``; exact duplicates; and same-key entries that differ only in value
(versions or conflicts, never merged, decision A-7).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.strategies import DrawFn

from edgarpit.ingest.companyfacts import (
    KEPT_TAXONOMIES,
    conflicting_values,
    ingest_facts,
    parse_companyfacts,
)
from edgarpit.rawcache import RawCache
from edgarpit.schemas import FactsRawSchema

# 50 examples keep the property suite near 25 s on the shared build machine;
# the deadline is off because a single Polars/Pandera pass can exceed the
# 200 ms default under load without any behavioural difference.
PROPERTY_SETTINGS = settings(deadline=None, max_examples=50)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sec"
CIKS = ("0001000275", "0000320193", "0001594805")

SKIPPED_TAXONOMIES = ("srt", "ffd", "invest", "cyol")

TAXONOMIES = st.sampled_from(list(KEPT_TAXONOMIES))
TEXT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=6)
UNITS = st.sampled_from(["USD", "CAD", "EUR", "shares", "USD/shares"])
FORMS = st.sampled_from(["10-K", "40-F", "10-Q", "8-K"])
DATES = st.dates(min_value=date(1990, 1, 1), max_value=date(2035, 12, 31)).map(date.isoformat)
ACCESSIONS = st.from_regex(r"\d{10}-\d{2}-\d{6}", fullmatch=True)
VALUES = st.one_of(
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
)


@st.composite
def entries(draw: DrawFn) -> dict[str, Any]:
    """One well-formed companyfacts entry, with optional start and frame."""
    row: dict[str, Any] = {
        "val": draw(VALUES),
        "end": draw(DATES),
        "fy": draw(st.one_of(st.none(), st.integers(min_value=1990, max_value=2035))),
        "fp": draw(st.one_of(st.none(), st.sampled_from(["FY", "Q1", "Q2", "Q3", "Q4"]))),
        "form": draw(FORMS),
        "accn": draw(ACCESSIONS),
        "filed": draw(DATES),
    }
    if draw(st.booleans()):
        row["start"] = draw(DATES)
    if draw(st.booleans()):
        row["frame"] = draw(st.sampled_from(["CY2023", "CY2023Q1", "CY2024"]))
    return row


@st.composite
def entry_rows(draw: DrawFn) -> list[dict[str, Any]]:
    """Rows for one (concept, unit): a row, an exact copy and a value change."""
    base = draw(entries())
    return [base, dict(base), {**base, "val": base["val"] + 1}]


@st.composite
def companyfacts_documents(draw: DrawFn) -> dict[str, Any]:
    """A companyfacts document with kept and skipped taxonomies."""
    groups = draw(
        st.lists(
            st.tuples(TAXONOMIES, TEXT, UNITS, entry_rows()),
            min_size=1,
            max_size=6,
        )
    )
    skipped = draw(
        st.lists(
            st.tuples(st.sampled_from(SKIPPED_TAXONOMIES), TEXT, UNITS, entry_rows()),
            min_size=1,
            max_size=2,
        )
    )
    facts: dict[str, dict[str, Any]] = {}
    for taxonomy, concept, unit, rows in groups + skipped:
        entry = facts.setdefault(taxonomy, {}).setdefault(concept, {"units": {}})
        entry["units"].setdefault(unit, []).extend(rows)
    return {"cik": draw(st.integers(min_value=1, max_value=9_999_999_999)), "facts": facts}


def expected_rows(doc: dict[str, Any]) -> list[tuple[Any, ...]]:
    """The kept entries of ``doc`` as staging rows, in dictionary order."""
    cik = f"{int(doc['cik']):010d}"
    rows: list[tuple[Any, ...]] = []
    for taxonomy, concepts in doc["facts"].items():
        if taxonomy not in KEPT_TAXONOMIES:
            continue
        for concept, entry in concepts.items():
            for unit, entries_ in entry["units"].items():
                for row in entries_:
                    rows.append(
                        (
                            cik,
                            taxonomy,
                            concept,
                            unit,
                            float(row["val"]),
                            row.get("start"),
                            row["end"],
                            row.get("fy"),
                            row.get("fp"),
                            row["form"],
                            row["accn"],
                            row["filed"],
                            row.get("frame"),
                        )
                    )
    return rows


def keys_of(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """The conflict key of a staging tuple (its value is at index 4)."""
    return (
        row[0],
        row[1],
        row[2],
        row[3],
        row[5],
        row[6],
        row[10],
    )


def values_by_key(rows: Iterable[tuple[Any, ...]]) -> dict[tuple[Any, ...], set[Any]]:
    grouped: dict[tuple[Any, ...], set[Any]] = {}
    for row in rows:
        grouped.setdefault(keys_of(row), set()).add(row[4])
    return grouped


def frame_rows(frame: pl.DataFrame) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for row in frame.iter_rows(named=True):
        start = row["period_start"]
        rows.append(
            (
                row["cik"],
                row["taxonomy"],
                row["concept"],
                row["unit"],
                row["value"],
                start.isoformat() if start is not None else None,
                row["period_end"].isoformat(),
                row["fy"],
                row["fp"],
                row["form"],
                row["accession"],
                row["filed"].isoformat(),
                row["frame"],
            )
        )
    return rows


def expected_conflict_rows(doc: dict[str, Any]) -> set[tuple[Any, ...]]:
    """Rows of key groups that hold more than one distinct value."""
    grouped = values_by_key(expected_rows(doc))
    conflicting_keys = {key for key, values in grouped.items() if len(values) > 1}
    return {row for row in expected_rows(doc) if keys_of(row) in conflicting_keys}


@PROPERTY_SETTINGS
@given(companyfacts_documents())
def test_every_kept_entry_appears_once_per_distinct_row(doc: dict[str, Any]) -> None:
    frame = parse_companyfacts(doc)
    expected = set(expected_rows(doc))
    assert set(frame_rows(frame)) == expected
    assert frame.height == len(expected)
    assert frame.height == frame.unique().height


@PROPERTY_SETTINGS
@given(companyfacts_documents())
def test_entries_that_differ_only_in_value_are_never_merged(doc: dict[str, Any]) -> None:
    frame = parse_companyfacts(doc)
    assert values_by_key(frame_rows(frame)) == values_by_key(expected_rows(doc))


@PROPERTY_SETTINGS
@given(companyfacts_documents())
def test_skipped_taxonomies_never_appear(doc: dict[str, Any]) -> None:
    frame = parse_companyfacts(doc)
    present = set(frame["taxonomy"].unique().to_list())
    assert present <= set(KEPT_TAXONOMIES)
    for taxonomy in SKIPPED_TAXONOMIES:
        if taxonomy in doc["facts"]:
            assert taxonomy not in present


@PROPERTY_SETTINGS
@given(companyfacts_documents())
def test_output_always_validates_under_facts_raw_schema(doc: dict[str, Any]) -> None:
    FactsRawSchema.validate(parse_companyfacts(doc))


@PROPERTY_SETTINGS
@given(companyfacts_documents())
def test_conflicting_values_matches_an_independent_grouping(doc: dict[str, Any]) -> None:
    frame = parse_companyfacts(doc)
    conflicts = conflicting_values(frame)
    assert set(frame_rows(conflicts)) == expected_conflict_rows(doc)
    assert conflicts.columns == frame.columns
    assert conflicts.schema == frame.schema


def test_same_key_different_value_rows_are_both_kept() -> None:
    """The focused A-7 case: one key, two values, both rows out."""
    row = {
        "val": 100,
        "start": "2023-01-01",
        "end": "2023-12-31",
        "fy": 2023,
        "fp": "FY",
        "form": "10-K",
        "accn": "0000320193-23-000106",
        "filed": "2023-11-03",
    }
    doc = {
        "cik": 320193,
        "facts": {"us-gaap": {"Revenues": {"units": {"USD": [row, {**row, "val": 250}]}}}},
    }
    frame = parse_companyfacts(doc)
    assert frame.height == 2
    assert sorted(frame["value"].to_list()) == [100.0, 250.0]
    assert conflicting_values(frame).height == 2


def test_fixture_documents_have_no_conflicting_values() -> None:
    """The tracked fixture count: zero conflicting rows in all three issuers."""
    raw = RawCache(FIXTURE_ROOT)
    total = 0
    for cik in CIKS:
        frame = ingest_facts(raw, cik)
        conflicts = conflicting_values(frame)
        assert conflicts.height == 0, f"{cik} has {conflicts.height} conflicting rows"
        assert conflicts.columns == frame.columns
        assert conflicts.schema == frame.schema
        total += conflicts.height
    assert total == 0


def test_conflicting_values_requires_the_key_columns() -> None:
    frame = pl.DataFrame({"cik": ["0000000001"], "value": [1.0]})
    with pytest.raises(pl.exceptions.ColumnNotFoundError):
        conflicting_values(frame)


def test_conflicting_values_without_a_conflict_is_empty() -> None:
    row = {
        "val": 1.0,
        "start": "2023-01-01",
        "end": "2023-12-31",
        "fy": 2023,
        "fp": "FY",
        "form": "10-K",
        "accn": "0000320193-23-000106",
        "filed": "2023-11-03",
    }
    other = {**row, "accn": "0000320193-24-000123"}
    doc = {"cik": 320193, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [row, other]}}}}}
    frame = parse_companyfacts(doc)
    assert frame.height == 2
    assert conflicting_values(frame).height == 0
    assert conflicting_values(frame).schema == frame.schema
