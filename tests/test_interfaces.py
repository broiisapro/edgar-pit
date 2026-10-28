"""Frozen-signature checks for the Epic 00 and Phase 2 interfaces.

Every function and dataclass here is a contract other workstreams code
against; ``inspect.signature`` catches any accidental change.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from edgarpit import concepts, fetch, load, query, ratios, rawcache, sec_client
from edgarpit.config import Settings
from edgarpit.fx import valet
from edgarpit.ingest import companyfacts, headers, submissions
from edgarpit.text import clean, discover, ingest, segment

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
        "db: 'str | Path | duckdb.DuckDBPyConnection | None' = None, "
        "currency: \"Literal['USD'] | None\" = None) -> 'list[AsOfResult]'"
    ),
    query.history: (
        "(identifier: 'str', field: 'str', period: \"Literal['FY', 'Q'] | None\" = None, *, "
        "db: 'str | Path | duckdb.DuckDBPyConnection | None' = None, "
        "currency: \"Literal['USD'] | None\" = None) -> 'list[AsOfResult]'"
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
    # Phase 2 (decisions A-30, A-31, A-32, A-34).
    fetch.fetch_universe: (
        "(issuers: 'Sequence[Issuer]', out_dir: 'Path | str', *, client: 'SecClient', "
        "refresh: 'bool' = True, incremental: 'bool' = False) -> 'FetchReport'"
    ),
    fetch.submissions_unchanged: (
        "(cached: 'Mapping[str, Any] | None', fresh: 'Mapping[str, Any]') -> 'bool'"
    ),
    rawcache.document_key: "(accession: 'str', filename: 'str') -> 'str'",
    sec_client.filing_index_url: "(cik: 'str', accession: 'str') -> 'str'",
    sec_client.document_url: "(cik: 'str', accession: 'str', filename: 'str') -> 'str'",
    valet.valet_url: (
        "(series: 'str', start: 'date | None' = None, end: 'date | None' = None) -> 'str'"
    ),
    valet.parse_valet: ("(doc: 'Mapping[str, Any]', series: 'str' = 'FXUSDCAD') -> 'pl.DataFrame'"),
    discover.exhibit_documents: "(index_headers_html: 'str') -> 'list[DocumentRef]'",
    clean.html_to_text: "(html: 'str') -> 'str'",
    segment.segment_items: ("(text: 'str', expected_items: 'Sequence[str]') -> 'list[Section]'"),
    ingest.ingest_text: (
        "(raw: 'RawCache', cik: 'str', accessions: 'Sequence[str]', "
        "filings: 'pl.DataFrame') -> 'pl.DataFrame'"
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
    discover.DocumentRef: ("accession", "filename", "doc_type"),
    segment.Section: ("section", "order", "text"),
}


def test_function_signatures_are_frozen() -> None:
    for func, expected in EXPECTED_SIGNATURES.items():
        assert str(inspect.signature(func)) == expected, func.__qualname__


def test_dataclass_fields_are_frozen() -> None:
    for cls, expected in EXPECTED_DATACLASS_FIELDS.items():
        assert dataclasses.is_dataclass(cls)
        assert tuple(f.name for f in dataclasses.fields(cls)) == expected, cls.__name__
        assert cls.__dataclass_params__.frozen, cls.__name__


def test_fetch_report_carries_the_incremental_counter() -> None:
    names = tuple(f.name for f in dataclasses.fields(fetch.FetchReport))
    assert names == ("issuers", "elapsed_seconds", "skipped_unchanged")
    assert fetch.FetchReport().skipped_unchanged == 0


def test_valet_client_has_the_sec_client_injection_points() -> None:
    signature = inspect.signature(valet.ValetClient)
    assert list(signature.parameters) == [
        "settings",
        "cache",
        "offline_dir",
        "clock",
        "sleep",
        "transport",
    ]
    assert signature.parameters["cache"].kind is inspect.Parameter.KEYWORD_ONLY


def test_phase2_stubs_raise_not_implemented(tmp_path: Path) -> None:
    filings = pl.DataFrame(
        schema={
            "cik": pl.String,
            "accession": pl.String,
            "acceptance_ts": pl.Datetime("us", "UTC"),
            "ts_imputed": pl.Boolean,
            "items": pl.String,
        }
    )
    calls: list[Any] = [
        lambda: clean.html_to_text("<html></html>"),
        lambda: segment.segment_items("text", ["2.02"]),
        lambda: ingest.ingest_text(rawcache.RawCache(tmp_path), "0000320193", [], filings),
        lambda: valet.parse_valet({}),
        lambda: fetch.submissions_unchanged(None, {}),
    ]
    for call in calls:
        with pytest.raises(NotImplementedError):
            call()


def test_incremental_fetch_is_refused_rather_than_silently_full(tmp_path: Path) -> None:
    client = sec_client.SecClient(Settings(sec_user_agent="Test User test@example.com"))
    with pytest.raises(NotImplementedError, match="incremental"):
        fetch.fetch_universe([], tmp_path, client=client, incremental=True)


def test_currency_is_refused_until_the_usd_model_exists() -> None:
    with pytest.raises(NotImplementedError, match="currency"):
        query.asof("AAPL", "revenue", "2024-01-01T00:00:00Z", currency="USD")
    with pytest.raises(NotImplementedError, match="currency"):
        query.history("AAPL", "revenue", currency="USD")
