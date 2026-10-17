"""Tests for :mod:`edgarpit.ingest.companyfacts` (issue #20).

The fixture issuers under ``tests/fixtures/sec`` are real, trimmed SEC
responses, so every assertion here is checked against data the SEC published.
The independent expected-row helper below re-flattens the raw document with
plain dictionaries, which keeps the Polars implementation honest.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from edgarpit.concepts import load_concept_map
from edgarpit.ingest.companyfacts import (
    KEPT_TAXONOMIES,
    ingest_facts,
    parse_companyfacts,
)
from edgarpit.rawcache import RawCache
from edgarpit.schemas import FactsRawSchema

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sec"
RAW = RawCache(FIXTURE_ROOT)

RY_CIK = "0001000275"
APPLE_CIK = "0000320193"
SHOP_CIK = "0001594805"
CIKS = (RY_CIK, APPLE_CIK, SHOP_CIK)

RY_FY2023_40F = "0001193125-23-285639"
RY_FY2023_RESTATED_40F = "0001193125-24-270294"

LOGGER = "edgarpit.ingest.companyfacts"


def load_document(cik: str) -> dict[str, Any]:
    paths = sorted((FIXTURE_ROOT / "raw" / "companyfacts" / cik).glob("*.json"))
    assert len(paths) == 1, f"expected one companyfacts file for {cik}"
    return json.loads(paths[0].read_text(encoding="utf-8"))


def expected_rows(document: dict[str, Any]) -> set[tuple[Any, ...]]:
    """Independently flatten a document into the rows ``facts_raw`` promises."""
    cik = f"{int(document['cik']):010d}"
    rows: set[tuple[Any, ...]] = set()
    for taxonomy, concepts in document["facts"].items():
        if taxonomy not in KEPT_TAXONOMIES:
            continue
        for concept, entry in concepts.items():
            for unit, entries in entry["units"].items():
                for row in entries:
                    rows.add(
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


def frame_rows(frame: pl.DataFrame) -> set[tuple[Any, ...]]:
    """The parsed frame as the same tuples the expected helper produces."""
    rows: set[tuple[Any, ...]] = set()
    for row in frame.iter_rows(named=True):
        start = row["period_start"]
        rows.add(
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


def entry(**overrides: Any) -> dict[str, Any]:
    """A well-formed companyfacts entry, overridable per field."""
    row: dict[str, Any] = {
        "val": 1.5,
        "start": "2023-01-01",
        "end": "2023-12-31",
        "fy": 2023,
        "fp": "FY",
        "form": "10-K",
        "accn": "0000320193-23-000106",
        "filed": "2023-11-03",
    }
    row.update(overrides)
    return row


def document(cik: int, taxonomy: str, concept: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"cik": cik, "facts": {taxonomy: {concept: {"units": {"USD": rows}}}}}


@pytest.mark.parametrize("cik", CIKS)
def test_fixture_rows_match_the_kept_entries_without_duplicates(cik: str) -> None:
    """Every kept entry becomes exactly one row; exact duplicates collapse."""
    frame = ingest_facts(RAW, cik)
    expected = expected_rows(load_document(cik))
    assert frame_rows(frame) == expected
    assert frame.height == len(expected)
    assert frame.height == frame.unique().height


@pytest.mark.parametrize("cik", CIKS)
def test_output_validates_under_facts_raw_schema(cik: str) -> None:
    FactsRawSchema.validate(ingest_facts(RAW, cik))


@pytest.mark.parametrize("cik", CIKS)
def test_cik_is_zero_padded(cik: str) -> None:
    frame = ingest_facts(RAW, cik)
    assert frame["cik"].unique().to_list() == [cik]


def test_royal_bank_revenue_has_both_versions() -> None:
    """The FY2023 40-F and the FY2024 restatement are two rows, not one."""
    frame = ingest_facts(RAW, RY_CIK)
    revenue = frame.filter(
        (pl.col("taxonomy") == "ifrs-full")
        & (pl.col("concept") == "Revenue")
        & (pl.col("period_start") == date(2022, 11, 1))
        & (pl.col("period_end") == date(2023, 10, 31))
    )
    assert revenue.height == 2
    assert set(revenue["value"].to_list()) == {56129000000.0, 51464000000.0}
    assert set(revenue["accession"].to_list()) == {RY_FY2023_40F, RY_FY2023_RESTATED_40F}


def test_instants_have_a_null_period_start_and_durations_do_not() -> None:
    frame = ingest_facts(RAW, RY_CIK)
    assets = frame.filter(pl.col("concept") == "Assets")
    assert assets.height > 0
    assert assets["period_start"].null_count() == assets.height
    revenue = frame.filter((pl.col("taxonomy") == "ifrs-full") & (pl.col("concept") == "Revenue"))
    assert revenue.height > 0
    assert revenue["period_start"].null_count() == 0


def test_entry_fields_are_copied_unchanged() -> None:
    doc = document(
        1000275,
        "ifrs-full",
        "Revenue",
        [
            entry(
                val=56129000000,
                start="2022-11-01",
                end="2023-10-31",
                fy=2023,
                fp="FY",
                form="40-F",
                accn=RY_FY2023_40F,
                filed="2023-11-30",
                frame="CY2023",
            )
        ],
    )
    record = parse_companyfacts(doc).to_dicts()[0]
    assert record == {
        "cik": RY_CIK,
        "taxonomy": "ifrs-full",
        "concept": "Revenue",
        "unit": "USD",
        "value": 56129000000.0,
        "period_start": date(2022, 11, 1),
        "period_end": date(2023, 10, 31),
        "fy": 2023,
        "fp": "FY",
        "form": "40-F",
        "accession": RY_FY2023_40F,
        "filed": date(2023, 11, 30),
        "frame": "CY2023",
    }


def test_absent_start_fy_fp_and_frame_are_null() -> None:
    row = entry()
    for key in ("start", "fy", "fp"):
        del row[key]
    record = parse_companyfacts(document(320193, "us-gaap", "Assets", [row])).to_dicts()[0]
    assert record["period_start"] is None
    assert record["fy"] is None
    assert record["fp"] is None
    assert record["frame"] is None


def test_exact_duplicates_collapse_but_conflicting_values_stay() -> None:
    base = entry()
    doc = document(
        320193,
        "us-gaap",
        "Revenues",
        [dict(base), dict(base), {**base, "val": 2.0}],
    )
    frame = parse_companyfacts(doc)
    assert frame.height == 2
    assert sorted(frame["value"].to_list()) == [1.5, 2.0]


def test_skipped_taxonomies_are_counted_in_a_debug_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    doc = {
        "cik": 320193,
        "facts": {
            "us-gaap": {"GrossProfit": {"units": {"USD": [entry()]}}},
            "srt": {"SegmentMember": {"units": {"USD": [entry(), entry()]}}},
            "ffd": {"SomeFund": {"units": {"USD": [entry()]}}},
        },
    }
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        frame = parse_companyfacts(doc)
    assert frame.height == 1
    assert "skipping taxonomy srt with 2 entries" in caplog.text
    assert "skipping taxonomy ffd with 1 entries" in caplog.text


def test_malformed_entries_are_skipped_and_counted(caplog: pytest.LogCaptureFixture) -> None:
    good = entry()
    missing_accn = {key: value for key, value in good.items() if key != "accn"}
    missing_value = {key: value for key, value in good.items() if key != "val"}
    doc = document(320193, "us-gaap", "Revenues", [good, missing_accn, missing_value])
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        frame = parse_companyfacts(doc)
    assert frame.height == 1
    assert "skipped 2 malformed companyfacts entries" in caplog.text


def test_a_document_with_no_kept_facts_validates_as_an_empty_frame() -> None:
    frame = parse_companyfacts({"cik": 320193, "facts": {"srt": {}}})
    assert frame.height == 0
    assert list(frame.columns) == list(FactsRawSchema.to_schema().columns)
    FactsRawSchema.validate(frame)


def test_missing_facts_object_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_companyfacts({"cik": 320193})


def test_missing_or_non_numeric_cik_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_companyfacts({"facts": {}})
    with pytest.raises(ValueError):
        parse_companyfacts({"cik": "Apple", "facts": {}})


def test_ifrs_and_usgaap_map_to_same_canonical_field() -> None:
    """RY's IFRS revenue rows and Apple's US GAAP revenue rows are all ``revenue``."""
    concept_map = load_concept_map()
    ry = ingest_facts(RAW, RY_CIK)
    apple = ingest_facts(RAW, APPLE_CIK)
    ry_revenue = ry.filter(pl.col("concept") == "Revenue")
    assert ry_revenue.height > 0
    assert ry_revenue["taxonomy"].unique().to_list() == ["ifrs-full"]
    apple_revenue = apple.filter(
        pl.col("concept").is_in(
            [
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "SalesRevenueNet",
            ]
        )
    )
    assert apple_revenue.height > 0
    pairs = pl.concat([ry_revenue, apple_revenue]).select("taxonomy", "concept").unique()
    assert pairs.height == 4  # ifrs-full:Revenue plus Apple's three US GAAP rankings
    for taxonomy, concept in pairs.iter_rows():
        mapped = concept_map.lookup(taxonomy, concept)
        assert mapped is not None
        assert mapped[0] == "revenue"


def test_ingest_facts_reads_the_latest_cached_document(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    first = document(320193, "us-gaap", "Revenues", [entry()])
    second = document(320193, "us-gaap", "Revenues", [entry(), entry(val=2.5)])
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    cache.write("companyfacts", APPLE_CIK, "", json.dumps(first).encode(), url=url, http_status=200)
    cache.write(
        "companyfacts", APPLE_CIK, "", json.dumps(second).encode(), url=url, http_status=200
    )
    frame = ingest_facts(cache, "320193")
    assert frame.height == 2
    assert sorted(frame["value"].to_list()) == [1.5, 2.5]


def test_ingest_facts_reports_a_missing_document(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ingest_facts(RawCache(tmp_path), RY_CIK)


def test_ingest_facts_rejects_a_non_numeric_cik(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ingest_facts(RawCache(tmp_path), "RY")


def iter_fixture_units() -> Iterator[tuple[str, str, str]]:
    """Every (taxonomy, concept, unit) present in the fixture documents."""
    for cik in CIKS:
        for taxonomy, concepts in load_document(cik)["facts"].items():
            for concept, entry_data in concepts.items():
                for unit in entry_data["units"]:
                    yield taxonomy, concept, unit


def test_units_are_copied_exactly_from_the_units_keys() -> None:
    wanted = set(iter_fixture_units())
    assert wanted, "fixtures carry no units"
    for cik in CIKS:
        frame = ingest_facts(RAW, cik)
        present = {
            (row[0], row[1], row[2])
            for row in frame.select("taxonomy", "concept", "unit").unique().iter_rows()
        }
        assert present <= wanted
