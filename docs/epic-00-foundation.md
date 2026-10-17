# Epic 00 — Foundation

## Goal

Put a fresh clone in a state where every Phase 1 workstream can start coding against frozen interfaces. The epic ends when:

- a fresh clone installs from a committed lockfile, and CI reports the four checks `lint`, `types`, `tests` and `dbt` on every pull request;
- the staging contract for `raw.filings`, `raw.facts_raw` and `raw.events_8k` exists as Pandera schemas plus DuckDB DDL, and the dbt output column contract is committed in `docs/architecture.md`;
- the canonical concept map, the issuer universe and the `asof()`/ingest/load/ratio signatures are fixed;
- a development DuckDB database can be built from validated staging CSVs, and `dbt build` runs against it in CI;
- real SEC fixtures for the three reference issuers make the whole pipeline testable without network.

## How it works

The pieces, in the order a build touches them:

1. **Configuration** — `edgarpit/config.py` reads `SEC_USER_AGENT`, `EDGARPIT_DB` and `EDGARPIT_RATE_LIMIT_RPS` from the environment or a `.env` file. `validate_user_agent` enforces the SEC's "Name email" form; the client calls it before the first request, so offline work never needs it.
2. **Schemas** — `edgarpit/schemas.py` defines `FilingsSchema`, `Events8kSchema` and `FactsRawSchema` (Pandera, Polars backend, strict and ordered) plus `COLUMNS`, `DUCKDB_TYPES`, `SCHEMAS` and `duckdb_ddl()`.
3. **Concept map** — `config/concept_map.yaml` names the 20 canonical fundamentals fields and their ordered US GAAP / IFRS concepts; `edgarpit/concepts.py` loads it and maps `(taxonomy, concept)` to `(field, rank)`.
4. **Raw cache and client** — `edgarpit/rawcache.py` stores every fetched body immutably under the A-4 layout (zstd, microsecond stamps, exclusive create, SHA-256 manifest) and reads the latest stamp per key, compressed or plain. `edgarpit/sec_client.py` wraps one synchronous `httpx.Client` with an in-house rate limiter (minimum spacing of `1 / rate_limit_rps` plus a safety epsilon), tenacity retries on 429/5xx/transport errors with `Retry-After` honoured, immediate failures on 403/404, and an offline mode that serves the same URLs from a `RawCache` directory (the fixture tree, uncompressed) without a transport or User-Agent.
5. **Stubs** — `edgarpit/ingest/*.py`, `edgarpit/load.py`, `edgarpit/ratios.py` and `edgarpit/query.py` carry the frozen signatures; their bodies arrive in Epic 01.
6. **Fixtures** — `tools/build_fixtures.py` produces the real, trimmed SEC tree in `tests/fixtures/sec/` for Royal Bank of Canada, Apple and Shopify (submissions plus every page, companyfacts, one header per kept accession) and, with `--staging`, the validated `tests/fixtures/staging/*.csv` plus `tests/fixtures/expected_asof.json`.
7. **Development database** — `edgarpit/devdb.py` recreates `<out>/edgarpit.duckdb` with schema `raw` and loads `tests/fixtures/staging/*.csv` when present, validating each file with its Pandera schema.
8. **dbt** — `transform/` is a dbt-duckdb project whose sources declare the `raw` tables and whose `stg_filings`, `stg_facts` and `stg_events_8k` smoke models pass rows through with `not_null` tests on their keys. `dbt build` in CI runs against the development database created in the previous step.
9. **CI** — `.github/workflows/ci.yml` runs `lint` (ruff check + format check), `types` (`mypy edgarpit`, strict), `tests` (`pytest -q`) and `dbt` (`devdb` + `dbt build`) on self-hosted macOS ARM64 runners.

## Key decisions and reasoning

- **Python 3.12 and uv only.** `.python-version` pins 3.12; `uv.lock` is committed and every job runs `uv sync --frozen`, so the same 105-package environment is reproduced everywhere. Runtime dependencies are declared as compatible ranges (verified on PyPI at authoring time) and pinned exactly in the lockfile.
- **Layout of the staging tables is frozen in code, not only in prose.** `docs/architecture.md` section 6 is the contract; `edgarpit/schemas.py` is its executable form, including tz-aware `Datetime("us", "UTC")` timestamps, zero-padded ten-digit CIKs, `NNNNNNNNNN-YY-NNNNNN` accessions, strict column order and exact dtypes. `devdb` creates the DuckDB tables from the same `duckdb_ddl()` the loader will use, so the schema cannot drift between paths.
- **The `acceptance_ts` rule reaches the schema layer.** `acceptance_ts` is non-nullable and accompanied by `acceptance_ts_source` (`sgml_header` or `filing_date_eod`) and `ts_imputed`; `acceptance_ts_api` exists for diagnostics only. The dbt sources and smoke models preserve all of it.
- **One concept map.** Canonical field names exist only in `config/concept_map.yaml`. Every field declares its `kind` (duration/instant), `unit_family` and ordered concept list per taxonomy; duplicate concepts, unknown attributes and invalid kinds fail at load time. Every concept name was verified against the taxonomies present in SEC companyfacts (including all three fixture issuers).
- **Universe verified against EDGAR.** `config/universe.csv` holds 40 issuers (20 Canadian, 20 US) with CIKs checked against `company_tickers.json` and `filer_type` derived from each issuer's most recent annual report in its submissions document. Tests check shape offline; the live check was done when the file was created.
- **The development database is a first-class artifact.** Before ingest exists, dbt and query development run against `create_dev_db()`, which validates CSVs with the same schemas the loader will enforce and sets the session timezone to UTC (decision A-5). CI builds it under `$RUNNER_TEMP` so parallel runners never share a file.
- **CI is the gate, not a formality.** Four jobs with the exact names the ruleset will require later; per-ref concurrency with cancel-in-progress; no job touches the network beyond package installation.
- **The cache is the unit of raw truth.** Cache files are never overwritten (exclusive create after a temp write), each manifest row can be re-verified (`python -m edgarpit.rawcache verify DIR`), and readers accept both compressed cache files and the uncompressed fixture tree, which makes fixtures and live caches interchangeable. `company_tickers.json` is classified but not cached, because the A-4 layout does not include it.
- **Rate limiting is spacing, not a counter.** The client enforces a minimum interval between request starts (`1 / rate_limit_rps` plus a 1 µs epsilon) so no rolling one-second window can exceed the configured rate even after float rounding; the limiter, retry waits and the HTTP transport are all injectable, so the behaviour is tested deterministically without sleeping or network.

## Interfaces for future epics

**Staging schemas** — `edgarpit/schemas.py`: `FilingsSchema`, `Events8kSchema`, `FactsRawSchema`, `COLUMNS`, `DUCKDB_TYPES`, `SCHEMAS`, `duckdb_ddl(table, schema="raw")`. Frozen after Epic 00; changes need a dedicated issue.

**Column contract** — `docs/architecture.md` section 7 (`fundamentals_pit`, `ratios_pit`, `events_8k`, `filings`). The query layer depends only on these columns.

**Fixtures and staging CSVs** — `tests/fixtures/sec/` is a real, trimmed SEC cache tree (uncompressed, 455 stored responses plus `raw/manifest.jsonl`) for Royal Bank of Canada (`0001000275`), Apple (`0000320193`) and Shopify (`0001594805`), produced by the committed `tools/build_fixtures.py`. `tests/fixtures/staging/*.csv` holds 187 derived rows (30 filings, 141 facts, 16 8-K items) that `create_dev_db` loads when present, and `tests/fixtures/expected_asof.json` pins the definition-of-done answer: `RY revenue` as of `2024-01-15T00:00:00Z` is `56,129,000,000 CAD` for FY2023 (`2022-11-01` to `2023-10-31`) from accession `0001193125-23-285639` accepted `2023-11-30T11:45:12Z`; the December 2024 restatement to 51,464,000,000 must not be returned.

**Concept map** — `edgarpit/concepts.py`: `load_concept_map(path=None) -> ConceptMap`; `ConceptMap.lookup("ifrs-full", "Revenue") -> ("revenue", 1)`; `ConceptMap.fields`; `ConceptMapError`.

**SEC client** — `edgarpit/sec_client.py`: `SecClient(settings, *, cache=None, offline_dir=None, clock=..., sleep=..., transport=None)`; `get_json(url) -> dict`, `get_text(url) -> str`, context manager; exceptions `SecClientError`, `SecAccessDenied`, `SecNotFound`, `OfflineMiss`. URL builders `submissions_url`, `submissions_page_url`, `companyfacts_url`, `header_url`, `company_tickers_url` and `classify(url) -> (endpoint, cik, key)` are the supported entry points. Online mode validates `SEC_USER_AGENT` in the constructor; offline mode (`offline_dir`) needs neither transport nor User-Agent.

**Raw cache** — `edgarpit/rawcache.py`: `RawCache(root)` with `write(endpoint, cik, key, body, *, url, http_status)`, `latest(endpoint, cik, key=None) -> bytes | None`, `keys(endpoint, cik)`, `verify() -> VerifyReport` and `manifest_rows()`; `python -m edgarpit.rawcache verify DIR` prints an inventory and exits non-zero on any mismatch. Layout, stamps and manifest fields are defined in `docs/architecture.md` section 5.

**Dev database** — `edgarpit/devdb.py`: `create_dev_db(out_dir, staging_dir=None) -> Path`; `python -m edgarpit.devdb --out DIR [--staging DIR]`.

**Ingest / load / query / ratios** — signatures frozen in `edgarpit/ingest/submissions.py`, `edgarpit/ingest/headers.py`, `edgarpit/ingest/companyfacts.py`, `edgarpit/load.py`, `edgarpit/ratios.py`, `edgarpit/query.py`; `tests/test_interfaces.py` asserts each with `inspect.signature`:

```
parse_submissions(main, pages) -> pl.DataFrame
explode_8k_items(filings, item_labels) -> pl.DataFrame
parse_header(text) -> HeaderInfo
apply_acceptance(filings, headers) -> pl.DataFrame
parse_companyfacts(doc) -> pl.DataFrame
load_tables(con, *, filings, events_8k, facts_raw) -> None
build_from_raw(raw_dir, out_dir) -> Path
compute_ratio(numerator, denominator) -> RatioValue
asof(identifier, field, asof_ts, period="FY", n=1, *, db=None) -> list[AsOfResult]
history(identifier, field, period=None, *, db=None) -> list[AsOfResult]
events(identifier, item=None, start=None, end=None, *, db=None) -> list[EventResult]
```

## Deviations from the plan

- **One universe substitution: XOM was replaced by ORCL.** Every ticker in the suggested list resolved against SEC's `company_tickers.json` except that XOM now maps to CIK `0002115436` (ExxonMobil Holdings Corp), a recently formed holding company whose submissions contain no annual report, so no `filer_type` could be derived. ORCL (Oracle, CIK `0001341439`, 10-K) was verified live and used instead; the file still has 20 Canadian and 20 US issuers.
- **The SEC fixture tree was regenerated once, in #7**, to extend the builder's concept list to every concept in `config/concept_map.yaml` (the follow-up flagged when the fixtures first landed). Values are still byte-for-byte live rows; the trim rule is unchanged.
- **`history()` and `events()` carry a keyword-only `db` parameter** like `asof()` does. Decision A-12 freezes only `asof()`; the two related functions follow its pattern so callers pass the database the same way.
- **`# type: ignore[valid-type]` on the tz-aware Pandera annotations.** Pandera's Polars backend needs `pl.Datetime("us", "UTC")` written as an annotation instance, which mypy's expression checker rejects; the ignores are annotation-only and have no runtime effect.
- **No `edgarpit` CLI in this epic.** `devdb` and `rawcache` expose `python -m` entry points instead; the Typer CLI (`universe resolve`, `fetch`, `build`, `asof`, `qa`, `export`) is Epic 02, as the architecture plans.

## Known limitations and deferred items

- **The pipeline bodies are stubs.** `edgarpit/ingest/*.py`, `edgarpit/load.py`, `edgarpit/ratios.py` and `edgarpit/query.py` raise `NotImplementedError`; the signatures, schemas and tests that pin them are in place, but Epic 01 implements them. Calling `asof()` today is expected to fail.
- **dbt builds smoke models only.** `stg_filings`, `stg_facts` and `stg_events_8k` pass rows through; the real `int_*` and `fundamentals_pit` / `ratios_pit` / `events_8k` / `filings` models and the `assert_ratio_inputs_not_from_future` test are Epic 01. Section 7 of `docs/architecture.md` is the frozen target contract, not yet a built model.
- **No fetch orchestration, QA or export.** `fetch.py`, `qa.py`, `export.py`, `api/` and `explorer/` are not in this epic; the full 40-issuer backfill (including one SGML header per relevant filing) has not been run.
- **8-K item labels are `unknown` in the staging fixtures** until `config/8k_items.yaml` lands in Epic 01.
- **Shopify has no `us-gaap:Liabilities` facts** in companyfacts, so `total_liabilities` has no Shopify rows in the staging CSVs. This is a real coverage gap for `qa.py` to report, not a selection error.
- **`company_tickers.json` is classified but not stored** in the raw cache, because the A-4 layout has no place for it; universe resolution in Epic 02 will need either a cache slot or a fresh fetch.
- **Live SEC behaviour is not covered by CI.** Tests marked `network` are deselected by default; the one live smoke test is run manually.
- **The ruleset does not yet require the four status checks.** Decision U-1 defers that until the workflow has reported on at least one pull request; until then the checks are enforced by merge discipline.
- **Out of scope by phase:** derived Q4 values, FX conversion, 8-K document text, LLM extraction, a hosted API and a larger universe.

