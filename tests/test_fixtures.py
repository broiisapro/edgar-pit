"""Fixture-tree checks for the real SEC responses under tests/fixtures/sec.

These tests never touch the network.  They verify the tree produced by
``tools/build_fixtures.py``, ``tools/build_fixtures.py --text`` and
``tools/build_fx_fixture.py`` on one merged cache (decision A-44): layout, size
budget, manifest hashes, the required issuer cases, that every kept accession
has a parsed header, and that the staging CSVs and ``expected_asof.json`` are
consistent with the fixture tree and the frozen staging schemas.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from edgarpit.concepts import load_concept_map
from edgarpit.schemas import COLUMNS, DUCKDB_TYPES, SCHEMAS

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SEC_ROOT = FIXTURE_ROOT / "sec"
RAW_DIR = SEC_ROOT / "raw"
MANIFEST_PATH = RAW_DIR / "manifest.jsonl"
STAGING_DIR = FIXTURE_ROOT / "staging"
EXPECTED_ASOF_PATH = FIXTURE_ROOT / "expected_asof.json"

CIKS = ("0001000275", "0000320193", "0001594805")
APPLE_CIK = "0000320193"
RY_CIK = "0001000275"

RY_FY2023_40F = "0001193125-23-285639"
RY_FY2023_RESTATED_40F = "0001193125-24-270294"
APPLE_10K_AMENDMENT = "0001193125-10-012091"

ANNUAL_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "40-F",
        "40-F/A",
        "20-F",
        "20-F/A",
        "6-K",
        "6-K/A",
    }
)
EVENT_FORMS = frozenset({"8-K", "8-K/A"})
ANNUAL_CUTOFF = "2018-01-01"
EVENT_CUTOFF = "2022-01-01"
FACT_CUTOFF = "2018-01-01"

STAMP_RE = re.compile(r"\d{8}T\d{6}\d{6}Z")
ACCEPTANCE_RE = re.compile(r"<ACCEPTANCE-DATETIME>(\d{14})")
ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")

SIZE_BUDGET_BYTES = 20 * 1024 * 1024

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_fixture_builder() -> ModuleType:
    """Import ``tools/build_fixtures.py`` by path (it is not a package)."""
    path = REPO_ROOT / "tools" / "build_fixtures.py"
    spec = importlib.util.spec_from_file_location("fixture_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_kept_concepts_cover_every_concept_in_the_map() -> None:
    keep = load_fixture_builder().keep_concepts()
    for spec in load_concept_map().specs():
        assert set(spec.us_gaap) <= keep["us-gaap"], spec.field
        assert set(spec.ifrs_full) <= keep["ifrs-full"], spec.field
    assert "RevenuesNetOfInterestExpense" in keep["us-gaap"]


def test_fixture_companyfacts_keep_only_mapped_or_extra_concepts() -> None:
    keep = load_fixture_builder().keep_concepts()
    concept_map = load_concept_map()
    checked = 0
    for cik in CIKS:
        for taxonomy, concepts in load_companyfacts(cik)["facts"].items():
            if taxonomy == "dei":
                continue
            assert taxonomy in keep, f"{cik}: unexpected taxonomy {taxonomy}"
            for concept in concepts:
                assert (
                    concept_map.lookup(taxonomy, concept) is not None or concept in keep[taxonomy]
                ), f"{cik}: {taxonomy}:{concept} is neither mapped nor a kept extra"
                checked += 1
    assert checked > 50


def load_manifest() -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows, "manifest.jsonl is empty"
    return rows


def iter_submissions_rows(cik: str) -> Iterator[dict[str, Any]]:
    """Yield every kept row of a submissions document or page for ``cik``."""
    paths = sorted((RAW_DIR / "submissions" / cik).glob("*.json"))
    paths += sorted((RAW_DIR / "submissions_page" / cik).glob("*/*.json"))
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        filings = document.get("filings")
        arrays = (
            filings["recent"] if isinstance(filings, dict) and "recent" in filings else document
        )
        for index in range(len(arrays["accessionNumber"])):
            yield {key: arrays[key][index] for key in arrays if isinstance(arrays[key], list)}


def load_companyfacts(cik: str) -> dict[str, Any]:
    paths = sorted((RAW_DIR / "companyfacts" / cik).glob("*.json"))
    assert len(paths) == 1, f"expected one companyfacts file for {cik}, got {paths}"
    return json.loads(paths[0].read_text(encoding="utf-8"))


def header_paths(cik: str, accession: str | None = None) -> list[Path]:
    base = RAW_DIR / "headers" / cik
    if accession is not None:
        return sorted((base / accession).glob("*.hdr.sgml"))
    return sorted(base.glob("*/*.hdr.sgml"))


def header_acceptance(cik: str, accession: str) -> datetime:
    paths = header_paths(cik, accession)
    assert paths, f"no header for {accession}"
    text = paths[0].read_text(encoding="utf-8", errors="replace")
    match = ACCEPTANCE_RE.search(text)
    assert match, f"header for {accession} has no ACCEPTANCE-DATETIME"
    parsed = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    return parsed.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


def fact_rows(
    cik: str, taxonomies: tuple[str, ...] = ("us-gaap", "ifrs-full")
) -> Iterator[dict[str, Any]]:
    document = load_companyfacts(cik)
    for taxonomy in taxonomies:
        for concept, entry in document["facts"].get(taxonomy, {}).items():
            for unit, rows in entry["units"].items():
                for row in rows:
                    yield {
                        "taxonomy": taxonomy,
                        "concept": concept,
                        "unit": unit,
                        **row,
                    }


def test_fixture_tree_layout() -> None:
    rows = load_manifest()
    manifest_paths = {row["path"] for row in rows}
    stored = {
        path.relative_to(SEC_ROOT).as_posix() for path in RAW_DIR.rglob("*") if path.is_file()
    }
    stored.discard("raw/manifest.jsonl")
    assert stored == manifest_paths, "manifest and stored files disagree"
    for row in rows:
        assert row["path"].startswith("raw/")
        parts = row["path"].split("/")
        assert len(parts[1]) > 0
        stamp = parts[-1].split(".")[0]
        assert STAMP_RE.fullmatch(stamp), f"bad fetched_at stamp in {row['path']}"
        assert row["fetched_at"] == stamp
        assert row["endpoint"] in {
            "submissions",
            "submissions_page",
            "companyfacts",
            "headers",
            "company_tickers",
            "filing_index",
            "documents",
            "fx",
        }
        # SEC bytes are trimmed to keep the fixtures reviewable; the 8-K text
        # and the Bank of Canada series are stored exactly as served.
        assert row["trimmed"] is (row["endpoint"] not in {"filing_index", "documents", "fx"})
        if row["endpoint"] == "headers":
            assert parts[0:3] == ["raw", "headers", row["cik"]]
            assert ACCESSION_RE.fullmatch(parts[3])
        elif row["endpoint"] == "company_tickers":
            assert parts[0:3] == ["raw", "company_tickers", "_"]
            assert row["cik"] == ""
        elif row["endpoint"] == "filing_index":
            assert parts[0:3] == ["raw", "filing_index", row["cik"]]
            assert ACCESSION_RE.fullmatch(parts[3])
            assert parts[-1].endswith(".html")
        elif row["endpoint"] == "documents":
            assert parts[0:3] == ["raw", "documents", row["cik"]]
            accession, _, filename = parts[3].partition("__")
            assert ACCESSION_RE.fullmatch(accession)
            assert filename and parts[-1].endswith(Path(filename).suffix)
        elif row["endpoint"] == "fx":
            assert parts[0:3] == ["raw", "fx", "_"]
            assert parts[3] == "FXUSDCAD", "the SEC cache holds only the ingested daily series"
            assert row["cik"] == ""
        else:
            assert parts[2] == row["cik"]


def test_the_text_and_fx_fixtures_live_in_the_merged_cache() -> None:
    """Decision A-44: one SEC cache feeds the offline build, text and FX included.

    The separate text root is gone, the 8-K pages and documents are listed in
    the SEC manifest, and the ingested FX series sits in the same tree; the
    retired noon series never does, because nothing may ingest it.
    """
    assert not (FIXTURE_ROOT / "sec_text").exists(), "tests/fixtures/sec_text was merged"
    endpoints = {row["endpoint"] for row in load_manifest()}
    assert {"filing_index", "documents", "fx"} <= endpoints
    assert list((RAW_DIR / "filing_index").glob("*/*/*.html"))
    assert list((RAW_DIR / "documents").glob("*/*/*.htm"))
    assert not (FIXTURE_ROOT / "fx" / "raw" / "fx" / "_" / "FXUSDCAD").exists()


def test_manifest_discard_removes_only_the_named_endpoints(tmp_path: Path) -> None:
    """A mode that refreshes part of the cache keeps the other endpoints' rows."""
    builder = load_fixture_builder()
    manifest = builder.Manifest(root=tmp_path)
    stored: dict[str, Path] = {}
    for endpoint in ("submissions", "filing_index"):
        path = tmp_path / "raw" / endpoint / "0000320193" / "20260101T000000000000Z.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"{}")
        stored[endpoint] = path
        manifest.rows.append({"path": path.relative_to(tmp_path).as_posix(), "endpoint": endpoint})

    manifest.discard(("filing_index",))

    assert [row["endpoint"] for row in manifest.rows] == ["submissions"]
    assert not stored["filing_index"].exists()
    assert stored["submissions"].exists()


def test_size_budget() -> None:
    total = sum(path.stat().st_size for path in FIXTURE_ROOT.rglob("*") if path.is_file())
    assert total < SIZE_BUDGET_BYTES, f"fixtures are {total / 1e6:.1f} MB, budget is 20 MB"


def test_manifest_hashes_match_files() -> None:
    for row in load_manifest():
        body = (SEC_ROOT / row["path"]).read_bytes()
        assert len(body) == row["bytes"], row["path"]
        assert hashlib.sha256(body).hexdigest() == row["sha256"], row["path"]
        assert row["http_status"] == 200, row["path"]


def test_manifest_urls_are_real_sec_urls_for_the_cik() -> None:
    for row in load_manifest():
        cik = row["cik"]
        url = row["url"]
        parts = row["path"].split("/")
        if row["endpoint"] == "company_tickers":
            assert cik == ""
            assert url == "https://www.sec.gov/files/company_tickers.json"
            continue
        if row["endpoint"] == "fx":
            assert cik == ""
            assert url == "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
            continue
        assert re.fullmatch(r"\d{10}", cik)
        if row["endpoint"] == "submissions":
            assert url == f"https://data.sec.gov/submissions/CIK{cik}.json"
        elif row["endpoint"] == "submissions_page":
            page = parts[3]
            assert url == f"https://data.sec.gov/submissions/{page}.json"
            assert page.startswith(f"CIK{cik}-submissions-")
        elif row["endpoint"] == "companyfacts":
            assert url == f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        elif row["endpoint"] in {"filing_index", "documents"}:
            accession = parts[3].partition("__")[0]
            assert ACCESSION_RE.fullmatch(accession)
            directory = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
            )
            if row["endpoint"] == "filing_index":
                assert url == f"{directory}/{accession}-index-headers.html"
            else:
                assert url == f"{directory}/{parts[3].partition('__')[2]}"
        else:
            accession = parts[3]
            assert ACCESSION_RE.fullmatch(accession)
            expected = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{accession.replace('-', '')}/{accession}.hdr.sgml"
            )
            assert url == expected


def test_company_tickers_fixture_covers_the_universe() -> None:
    universe_path = Path(__file__).resolve().parents[1] / "config" / "universe.csv"
    with universe_path.open(encoding="utf-8", newline="") as handle:
        universe = {row["ticker"]: row["cik"] for row in csv.DictReader(handle)}
    paths = sorted((RAW_DIR / "company_tickers" / "_").glob("*.json"))
    assert len(paths) == 1
    document = json.loads(paths[0].read_text(encoding="utf-8"))
    by_ticker = {entry["ticker"]: f"{entry['cik_str']:010d}" for entry in document.values()}
    assert len(document) == len(by_ticker), "duplicate tickers in the fixture"
    assert len(document) == len(universe) + 5, "fixture is not the universe plus five tickers"
    for ticker, cik in universe.items():
        assert by_ticker.get(ticker) == cik, ticker


def test_every_page_file_from_submissions_is_kept() -> None:
    for cik in CIKS:
        main = sorted((RAW_DIR / "submissions" / cik).glob("*.json"))
        assert len(main) == 1
        document = json.loads(main[0].read_text(encoding="utf-8"))
        page_names = [entry["name"] for entry in document["filings"].get("files", [])]
        stored = sorted((RAW_DIR / "submissions_page" / cik).glob("*/*.json"))
        assert len(stored) == len(page_names)
        assert {path.parent.name + ".json" for path in stored} == set(page_names)


def test_kept_submissions_rows_respect_the_filter() -> None:
    for cik in CIKS:
        for row in iter_submissions_rows(cik):
            form = row["form"]
            filing_date = row["filingDate"]
            assert form in ANNUAL_FORMS | EVENT_FORMS, row
            if form in EVENT_FORMS:
                assert filing_date >= EVENT_CUTOFF, row
            elif (cik, row["accessionNumber"]) != (APPLE_CIK, APPLE_10K_AMENDMENT):
                assert filing_date >= ANNUAL_CUTOFF, row


def test_headers_parse_and_convert_to_utc() -> None:
    total = 0
    for cik in CIKS:
        for path in header_paths(cik):
            text = path.read_text(encoding="utf-8", errors="replace")
            assert ACCEPTANCE_RE.search(text), path
            total += 1
    assert total > 0
    # 2023-11-30 is EST (UTC-5): 06:45:12 local -> 11:45:12Z.
    assert header_acceptance(RY_CIK, RY_FY2023_40F) == datetime(
        2023, 11, 30, 11, 45, 12, tzinfo=UTC
    )


def test_every_kept_fact_accession_has_a_header() -> None:
    for cik in CIKS:
        accessions = {row["accn"] for row in fact_rows(cik)}
        assert accessions
        missing = sorted(accession for accession in accessions if not header_paths(cik, accession))
        assert missing == [], f"{cik} fact accessions without headers: {missing}"


def test_royal_bank_revenue_restatement_versions() -> None:
    versions = [
        row
        for row in fact_rows(RY_CIK, ("ifrs-full",))
        if row["concept"] == "Revenue"
        and row["unit"] == "CAD"
        and row["end"] == "2023-10-31"
        and row["start"] == "2022-11-01"
    ]
    values = {row["val"]: row["accn"] for row in versions}
    assert values.get(56129000000) == RY_FY2023_40F
    assert values.get(51464000000) == RY_FY2023_RESTATED_40F
    rows_by_accession = {row["accessionNumber"]: row for row in iter_submissions_rows(RY_CIK)}
    assert rows_by_accession[RY_FY2023_40F]["filingDate"] == "2023-11-30"
    assert rows_by_accession[RY_FY2023_RESTATED_40F]["filingDate"] == "2024-12-04"
    assert header_paths(RY_CIK, RY_FY2023_40F)
    assert header_paths(RY_CIK, RY_FY2023_RESTATED_40F)


def test_apple_amendment_is_kept_with_a_header() -> None:
    rows_by_accession = {row["accessionNumber"]: row for row in iter_submissions_rows(APPLE_CIK)}
    amendment = rows_by_accession[APPLE_10K_AMENDMENT]
    assert amendment["form"] == "10-K/A"
    assert amendment["filingDate"] == "2010-01-25"
    assert header_paths(APPLE_CIK, APPLE_10K_AMENDMENT)


def test_apple_has_many_8ks_with_headers() -> None:
    eight_ks = [
        row
        for row in iter_submissions_rows(APPLE_CIK)
        if row["form"] in EVENT_FORMS and row["filingDate"] >= EVENT_CUTOFF
    ]
    assert len(eight_ks) >= 20, f"only {len(eight_ks)} Apple 8-Ks"
    for row in eight_ks:
        assert header_paths(APPLE_CIK, row["accessionNumber"]), row["accessionNumber"]


def test_annual_filing_with_period_end_before_asof_but_accepted_after() -> None:
    """A period end can precede an as-of time while the filing that carried
    the value was accepted after it; the date rule alone must not decide."""
    asof = datetime(2024, 1, 15, tzinfo=UTC)
    cases = []
    for cik in CIKS:
        for row in fact_rows(cik):
            start, end = row.get("start"), row.get("end")
            if not isinstance(start, str) or not isinstance(end, str):
                continue
            if not 350 <= (date.fromisoformat(end) - date.fromisoformat(start)).days <= 380:
                continue
            if not end < asof.date().isoformat():
                continue
            acceptance = header_acceptance(cik, row["accn"])
            if acceptance > asof:
                cases.append((cik, row["concept"], end, row["accn"], acceptance))
    assert cases, "no annual filing with period end before the as-of but accepted after it"
    # The RY FY2023 restatement published on 2024-12-04 is one such case.
    assert any(case[0] == RY_CIK and case[3] == RY_FY2023_RESTATED_40F for case in cases)


def test_fact_rows_are_recent_enough() -> None:
    for cik in CIKS:
        for row in fact_rows(cik):
            assert row["end"] >= FACT_CUTOFF, row


# --- staging CSVs (issue #7) -------------------------------------------------

_POLARS_TYPES: dict[str, Any] = {
    "VARCHAR(10)": pl.String,
    "VARCHAR": pl.String,
    "DATE": pl.Date,
    "TIMESTAMPTZ": pl.Datetime("us", "UTC"),
    "BOOLEAN": pl.Boolean,
    "DOUBLE": pl.Float64,
    "INTEGER": pl.Int64,
}


def read_staging_csv(table: str) -> pl.DataFrame:
    overrides = {column: _POLARS_TYPES[dtype] for column, dtype in DUCKDB_TYPES[table].items()}
    return pl.read_csv(
        STAGING_DIR / f"{table}.csv",
        schema_overrides=overrides,
    ).select(COLUMNS[table])


def read_staging_rows(table: str) -> list[dict[str, str]]:
    with (STAGING_DIR / f"{table}.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


_FACT_ENTRY_CACHE: dict[str, dict[str, set[tuple[Any, ...]]]] = {}


def companyfacts_entries(cik: str) -> dict[str, set[tuple[Any, ...]]]:
    """Fixture companyfacts tuples keyed by accession, for tracing."""
    if cik not in _FACT_ENTRY_CACHE:
        document = load_companyfacts(cik)
        by_accession: dict[str, set[tuple[Any, ...]]] = {}
        for taxonomy, concepts in document["facts"].items():
            for concept, entry in concepts.items():
                for unit, rows in entry["units"].items():
                    for row in rows:
                        by_accession.setdefault(row["accn"], set()).add(
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
        _FACT_ENTRY_CACHE[cik] = by_accession
    return _FACT_ENTRY_CACHE[cik]


def test_staging_csv_headers_match_the_frozen_columns() -> None:
    for table, columns in COLUMNS.items():
        with (STAGING_DIR / f"{table}.csv").open(encoding="utf-8") as handle:
            assert handle.readline().strip().split(",") == columns


def test_staging_csvs_validate_against_the_pandera_schemas() -> None:
    for table, schema in SCHEMAS.items():
        frame = read_staging_csv(table)
        schema.validate(frame)


def test_staging_selection_size_is_within_the_issue_budget() -> None:
    total = sum(len(read_staging_rows(table)) for table in COLUMNS)
    assert 150 <= total <= 300, f"staging has {total} rows"


def test_staging_facts_trace_to_fixture_companyfacts() -> None:
    seen = 0
    for row in read_staging_rows("facts_raw"):
        entries = companyfacts_entries(row["cik"]).get(row["accession"], set())
        match = (
            row["cik"],
            row["taxonomy"],
            row["concept"],
            row["unit"],
            float(row["value"]),
            row["period_start"] or None,
            row["period_end"],
            int(row["fy"]) if row["fy"] else None,
            row["fp"] or None,
            row["form"],
            row["accession"],
            row["filed"],
            row["frame"] or None,
        )
        assert match in entries, row
        seen += 1
    assert seen > 100


def test_staging_facts_cover_the_ry_restatement() -> None:
    revenue = [
        row
        for row in read_staging_rows("facts_raw")
        if row["cik"] == RY_CIK
        and row["concept"] == "Revenue"
        and row["period_start"] == "2022-11-01"
        and row["period_end"] == "2023-10-31"
    ]
    values = {float(row["value"]) for row in revenue}
    assert {56129000000.0, 51464000000.0} <= values
    accessions = {row["accession"] for row in revenue}
    assert {RY_FY2023_40F, RY_FY2023_RESTATED_40F} <= accessions


def test_staging_filings_acceptance_ts_equals_their_headers() -> None:
    rows = read_staging_rows("filings")
    assert rows
    for row in rows:
        expected = header_acceptance(row["cik"], row["accession"])
        expected_iso = expected.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert row["acceptance_ts"] == expected_iso, row
        assert row["acceptance_ts_source"] == "sgml_header"
        assert row["ts_imputed"] == "false"
        assert row["acceptance_ts_api"] != ""


def test_staging_events_explode_at_least_ten_apple_8ks() -> None:
    events = read_staging_rows("events_8k")
    assert events
    accessions = {row["accession"] for row in events}
    assert len(accessions) >= 10
    assert all(row["cik"] == APPLE_CIK for row in events)
    assert all(row["form"] in EVENT_FORMS for row in events)
    assert all(row["item_label"] == "unknown" for row in events)
    keys = [(row["accession"], row["item_code"]) for row in events]
    assert len(keys) == len(set(keys))
    for row in events:
        assert row["acceptance_ts"] == read_filing_acceptance(row["accession"])


def test_staging_filings_text_names_real_8k_filings() -> None:
    """Every staged section belongs to a staged 8-K and carries its acceptance time."""
    filings = {row["accession"]: row for row in read_staging_rows("filings")}
    rows = read_staging_rows("filings_text")
    assert rows
    for row in rows:
        filing = filings[row["accession"]]
        assert filing["form"] in EVENT_FORMS, row
        assert row["cik"] == filing["cik"]
        assert row["acceptance_ts"] == filing["acceptance_ts"]
        assert row["ts_imputed"] == filing["ts_imputed"]
        assert row["doc_type"] in {"primary", "EX-99.1"}
        assert hashlib.sha256(row["text"].encode("utf-8")).hexdigest() == row["sha256"], row
        assert len(row["text"]) == int(row["char_count"]), row
    keys = [
        (row["accession"], row["document"], row["section"], row["section_order"]) for row in rows
    ]
    assert len(keys) == len(set(keys))


FX_FIXTURE_DIR = RAW_DIR / "fx" / "_" / "FXUSDCAD"


def bank_of_canada_rates() -> dict[str, float]:
    """The fixture ``FXUSDCAD`` observations as ``{rate date: value}``."""
    paths = sorted(FX_FIXTURE_DIR.glob("*.json"))
    assert len(paths) == 1
    document = json.loads(paths[0].read_text(encoding="utf-8"))
    return {
        observation["d"]: float(observation["FXUSDCAD"]["v"])
        for observation in document["observations"]
        if observation["FXUSDCAD"]["v"]
    }


def test_staging_fx_daily_is_the_bank_of_canada_series() -> None:
    """The hand-written staging series carries real Valet values and known times.

    Values are checked against the captured ``FXUSDCAD`` response, and
    ``known_at`` must be 17:00 ``America/Toronto`` in UTC (decision A-32), which
    is 22:00Z in winter and 21:00Z in summer. The series starts on the Bank's
    first business-daily observation (2017-01-03, decision A-39), so a period
    end before it has no rate.
    """
    toronto = ZoneInfo("America/Toronto")
    rates = bank_of_canada_rates()
    rows = read_staging_rows("fx_daily")
    assert rows
    dates = [row["rate_date"] for row in rows]
    assert dates == sorted(set(dates)), "staging FX dates must be unique and ascending"
    assert dates[0] == "2017-01-03"
    for row in rows:
        assert row["series"] == "FXUSDCAD"
        assert row["source"] == "valet"
        assert float(row["value"]) == rates[row["rate_date"]], row
        expected = datetime.combine(
            date.fromisoformat(row["rate_date"]), time(17, 0), tzinfo=toronto
        ).astimezone(UTC)
        assert row["known_at"] == expected.strftime("%Y-%m-%dT%H:%M:%SZ"), row


def read_filing_acceptance(accession: str) -> str:
    rows = [row for row in read_staging_rows("filings") if row["accession"] == accession]
    assert len(rows) == 1, accession
    return rows[0]["acceptance_ts"]


def test_expected_asof_is_consistent_with_the_staging_rows() -> None:
    expected = json.loads(EXPECTED_ASOF_PATH.read_text(encoding="utf-8"))
    assert expected["cik"] == RY_CIK
    filings = {row["accession"]: row for row in read_staging_rows("filings")}
    filing = filings[expected["accession"]]
    assert expected["acceptance_ts"] == filing["acceptance_ts"]
    candidates = [
        row
        for row in read_staging_rows("facts_raw")
        if row["cik"] == expected["cik"]
        and row["period_start"] == expected["period_start"]
        and row["period_end"] == expected["period_end"]
        and float(row["value"]) == float(expected["value"])
        and row["accession"] == expected["accession"]
        and row["unit"] == expected["unit"]
    ]
    assert len(candidates) == 1
    concept_map = load_concept_map()
    asof = datetime.fromisoformat(expected["asof"].replace("Z", "+00:00"))
    known = [
        row
        for row in read_staging_rows("facts_raw")
        if row["cik"] == expected["cik"]
        and (concept_map.lookup(row["taxonomy"], row["concept"]) or ("",))[0] == "revenue"
        and row["period_start"] == expected["period_start"]
        and row["period_end"] == expected["period_end"]
        and datetime.fromisoformat(
            filings[row["accession"]]["acceptance_ts"].replace("Z", "+00:00")
        )
        <= asof
    ]
    assert known, "expected_asof has no version known at the as-of time"
    latest = max(
        known,
        key=lambda row: datetime.fromisoformat(
            filings[row["accession"]]["acceptance_ts"].replace("Z", "+00:00")
        ),
    )
    assert latest["accession"] == expected["accession"]
    assert latest["concept"] == "Revenue"
    assert expected["currency"] == "CAD" == expected["unit"]
