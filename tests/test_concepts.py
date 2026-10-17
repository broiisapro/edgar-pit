"""Tests for :mod:`edgarpit.concepts` and ``config/concept_map.yaml``."""

from __future__ import annotations

from pathlib import Path

import pytest

from edgarpit.concepts import (
    DEFAULT_CONCEPT_MAP_PATH,
    ConceptMap,
    ConceptMapError,
    ConceptSpec,
    load_concept_map,
)

REQUIRED_FIELDS = {
    "revenue",
    "net_income",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "operating_cash_flow",
    "capex",
    "cash",
    "shares_outstanding",
    "eps_diluted",
    "eps_basic",
    "operating_income",
    "gross_profit",
    "current_assets",
    "current_liabilities",
    "long_term_debt",
}


def write_map(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "concept_map.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_default_map_loads() -> None:
    concept_map = load_concept_map()
    assert isinstance(concept_map, ConceptMap)
    assert DEFAULT_CONCEPT_MAP_PATH.exists()
    assert len(concept_map.fields) >= 20


def test_required_fields_are_defined() -> None:
    assert set(load_concept_map().fields) >= REQUIRED_FIELDS


def test_ifrs_and_usgaap_revenue_map_to_the_same_field() -> None:
    concept_map = load_concept_map()
    us = concept_map.lookup("us-gaap", "Revenues")
    ifrs = concept_map.lookup("ifrs-full", "Revenue")
    contract = concept_map.lookup("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax")
    assert us == ("revenue", 2)
    assert ifrs == ("revenue", 1)
    assert contract == ("revenue", 1)


def test_rank_is_position_in_the_ordered_list() -> None:
    concept_map = load_concept_map()
    assert concept_map.lookup("us-gaap", "GrossProfit") == ("gross_profit", 1)
    assert concept_map.lookup("ifrs-full", "ProfitLoss") == ("net_income", 1)
    assert concept_map.lookup("ifrs-full", "ProfitLossAttributableToOwnersOfParent") == (
        "net_income",
        2,
    )


def test_unknown_taxonomy_or_concept_returns_none() -> None:
    concept_map = load_concept_map()
    assert concept_map.lookup("dei", "EntityCommonStockSharesOutstanding") is None
    assert concept_map.lookup("srt", "Revenues") is None
    assert concept_map.lookup("us-gaap", "NotAConcept") is None


def test_fields_keep_file_order() -> None:
    assert load_concept_map().fields[:3] == ["revenue", "gross_profit", "operating_income"]


def test_duplicate_concept_across_fields_is_rejected(tmp_path: Path) -> None:
    path = write_map(
        tmp_path,
        """
fields:
  revenue:
    kind: duration
    unit_family: currency
    us-gaap: [Revenues]
    ifrs-full: [Revenue]
  sales:
    kind: duration
    unit_family: currency
    us-gaap: [Revenues]
    ifrs-full: [Sales]
""",
    )
    with pytest.raises(ConceptMapError):
        load_concept_map(path)


def test_duplicate_concept_within_one_field_is_rejected(tmp_path: Path) -> None:
    path = write_map(
        tmp_path,
        """
fields:
  revenue:
    kind: duration
    unit_family: currency
    us-gaap: [Revenues, Revenues]
    ifrs-full: [Revenue]
""",
    )
    with pytest.raises(ConceptMapError):
        load_concept_map(path)


def test_invalid_kind_is_rejected(tmp_path: Path) -> None:
    path = write_map(
        tmp_path,
        """
fields:
  revenue:
    kind: flow
    unit_family: currency
    us-gaap: [Revenues]
    ifrs-full: [Revenue]
""",
    )
    with pytest.raises(ConceptMapError):
        load_concept_map(path)


def test_unknown_attribute_is_rejected(tmp_path: Path) -> None:
    path = write_map(
        tmp_path,
        """
fields:
  revenue:
    kind: duration
    unit_family: currency
    us-gaap: [Revenues]
    ifrs-full: [Revenue]
    typo: true
""",
    )
    with pytest.raises(ConceptMapError):
        load_concept_map(path)


def test_missing_fields_mapping_is_rejected(tmp_path: Path) -> None:
    path = write_map(tmp_path, "version: 0\n")
    with pytest.raises(ConceptMapError):
        load_concept_map(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConceptMapError):
        load_concept_map(tmp_path / "absent.yaml")


def test_concept_map_rejects_duplicate_field_names() -> None:
    spec = ConceptSpec.model_validate(
        {
            "field": "revenue",
            "kind": "duration",
            "unit_family": "currency",
            "us-gaap": ["Revenues"],
            "ifrs-full": ["Revenue"],
        }
    )
    with pytest.raises(ConceptMapError):
        ConceptMap([spec, spec])
