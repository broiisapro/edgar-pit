"""Frozen-signature checks for the Epic 00 interfaces.

Every function and dataclass here is a contract other workstreams code
against; ``inspect.signature`` catches any accidental change.
"""

from __future__ import annotations

import dataclasses
import inspect

from edgarpit import concepts, load, query, ratios
from edgarpit.ingest import companyfacts, headers, submissions

EXPECTED_SIGNATURES = {
    submissions.parse_submissions: (
        "(main: 'Mapping[str, Any]', pages: 'Sequence[Mapping[str, Any]]') -> 'pl.DataFrame'"
    ),
    submissions.explode_8k_items: (
        "(filings: 'pl.DataFrame', item_labels: 'Mapping[str, str]') -> 'pl.DataFrame'"
    ),
    headers.parse_header: "(text: 'str') -> 'HeaderInfo'",
    headers.apply_acceptance: (
        "(filings: 'pl.DataFrame', headers: 'Mapping[str, HeaderInfo]') -> 'pl.DataFrame'"
    ),
    companyfacts.parse_companyfacts: "(doc: 'Mapping[str, Any]') -> 'pl.DataFrame'",
    load.load_tables: (
        "(con: 'duckdb.DuckDBPyConnection', *, filings: 'pl.DataFrame', "
        "events_8k: 'pl.DataFrame', facts_raw: 'pl.DataFrame') -> 'None'"
    ),
    load.build_from_raw: "(raw_dir: 'Path', out_dir: 'Path') -> 'Path'",
    ratios.compute_ratio: "(numerator: 'RatioInput', denominator: 'RatioInput') -> 'RatioValue'",
    query.asof: (
        "(identifier: 'str', field: 'str', asof_ts: 'datetime | str', "
        "period: \"Literal['FY', 'Q']\" = 'FY', n: 'int' = 1, *, "
        "db: 'str | Path | duckdb.DuckDBPyConnection | None' = None) -> 'list[AsOfResult]'"
    ),
    query.history: (
        "(identifier: 'str', field: 'str', period: \"Literal['FY', 'Q'] | None\" = None, *, "
        "db: 'str | Path | duckdb.DuckDBPyConnection | None' = None) -> 'list[AsOfResult]'"
    ),
    query.events: (
        "(identifier: 'str', item: 'str | None' = None, start: 'datetime | str | None' = None, "
        "end: 'datetime | str | None' = None, *, "
        "db: 'str | Path | duckdb.DuckDBPyConnection | None' = None) -> 'list[EventResult]'"
    ),
    concepts.load_concept_map: "(path: 'Path | None' = None) -> 'ConceptMap'",
    concepts.ConceptMap.lookup: (
        "(self, taxonomy: 'str', concept: 'str') -> 'tuple[str, int] | None'"
    ),
}

EXPECTED_DATACLASS_FIELDS = {
    headers.HeaderInfo: ("accession", "form", "acceptance_ts", "filing_date", "period", "items"),
    ratios.RatioInput: ("field", "value", "unit", "currency"),
    ratios.RatioValue: ("value", "unit", "currency", "numerator", "denominator"),
    query.AsOfResult: (
        "cik",
        "ticker",
        "field",
        "value",
        "unit",
        "currency",
        "period_start",
        "period_end",
        "period_type",
        "accession",
        "form",
        "acceptance_ts",
        "version",
        "is_restatement",
        "ts_imputed",
        "restatement_kind",
    ),
    query.EventResult: (
        "cik",
        "ticker",
        "accession",
        "form",
        "acceptance_ts",
        "ts_imputed",
        "item_code",
        "item_label",
    ),
}


def test_function_signatures_are_frozen() -> None:
    for func, expected in EXPECTED_SIGNATURES.items():
        assert str(inspect.signature(func)) == expected, func.__qualname__


def test_dataclass_fields_are_frozen() -> None:
    for cls, expected in EXPECTED_DATACLASS_FIELDS.items():
        assert dataclasses.is_dataclass(cls)
        assert tuple(f.name for f in dataclasses.fields(cls)) == expected, cls.__name__
        assert cls.__dataclass_params__.frozen, cls.__name__
