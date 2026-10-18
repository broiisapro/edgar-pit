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
    assert us == ("revenue", 1)
    assert ifrs == ("revenue", 1)


def test_revenues_outranks_contract_revenue() -> None:
    """Banks and energy issuers tag totals that contract revenue misses.

    JPM and BAC report only ``Revenues``; JPM's FY2023 accession carries no
    contract revenue at all, and ENB's contract revenue (29.3B CAD FY2025)
    is a component of its ``Revenues`` total (65.2B CAD).  The per-accession
    "lowest rank wins" rule (docs/architecture.md section 7) must therefore
    pick ``Revenues`` whenever both are reported.
    """
    concept_map = load_concept_map()
    candidates = [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
    ]
    picked = min(candidates, key=lambda concept: concept_map.lookup("us-gaap", concept)[1])
    assert picked == "Revenues"


def test_bank_revenue_total_is_mapped() -> None:
    """Wells Fargo reports no Revenues; its total is RevenuesNetOfInterestExpense.

    WFC files FY2023-FY2025 10-Ks that tag ``RevenuesNetOfInterestExpense``
    (83,699,000,000 USD FY2025) and no ``Revenues`` or contract revenue, so
    the bank total needs its own rank between ``Revenues`` and contract
    revenue.
    """
    concept_map = load_concept_map()
    assert concept_map.lookup("us-gaap", "RevenuesNetOfInterestExpense") == ("revenue", 2)
    assert concept_map.lookup("us-gaap", "Revenues") == ("revenue", 1)
    assert concept_map.lookup("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax") == (
        "revenue",
        3,
    )


def test_ifrs_bank_revenue_concept_is_pinned() -> None:
    """The Canadian banks (RY, TD, BNS, BMO, CM) report ifrs-full:Revenue."""
    concept_map = load_concept_map()
    assert concept_map.lookup("ifrs-full", "Revenue") == ("revenue", 1)
    assert concept_map.lookup("ifrs-full", "RevenueFromContractsWithCustomers") == ("revenue", 2)


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


def test_specs_returns_every_field_in_file_order() -> None:
    concept_map = load_concept_map()
    specs = concept_map.specs()

    assert [spec.field for spec in specs] == concept_map.fields
    assert len(specs) >= 20
    assert all(isinstance(spec, ConceptSpec) for spec in specs)
    revenue = specs[0]
    assert revenue.field == "revenue"
    assert revenue.kind == "duration"
    assert revenue.unit_family == "currency"
    assert revenue.us_gaap[0] == "Revenues"
    assert revenue.us_gaap.index("RevenuesNetOfInterestExpense") == 1
    assert revenue.ifrs_full[0] == "Revenue"


def test_specs_order_matches_a_tiny_map(tmp_path: Path) -> None:
    path = write_map(
        tmp_path,
        """
fields:
  a:
    kind: instant
    unit_family: shares
    us-gaap: [CommonStockSharesOutstanding]
    ifrs-full: [NumberOfSharesOutstanding]
  b:
    kind: duration
    unit_family: currency
    us-gaap: [Revenues]
    ifrs-full: [Revenue]
""",
    )
    concept_map = load_concept_map(path)
    assert [spec.field for spec in concept_map.specs()] == ["a", "b"]
    assert concept_map.specs()[0].unit_family == "shares"


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
