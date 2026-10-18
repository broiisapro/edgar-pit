# Epic 01 — Core pipeline

## Goal

Turn the frozen Epic 00 interfaces into the working point-in-time pipeline this project is named for. The epic ends when:

- `edgarpit fetch` fills a raw cache for the 40-issuer universe through the rate-limited SEC client, including one SGML header per relevant filing;
- `edgarpit build` ingests a cached raw tree into `<out>/edgarpit.duckdb` and runs dbt to produce `fundamentals_pit`, `ratios_pit`, `filings` and `events_8k` exactly per the column contract, with the model tests as the gate;
- `asof()`, `history()` and `events()` (Python), the FastAPI service and the Streamlit explorer answer "what was known" from the merged tables, and `tests/fixtures/expected_asof.json` is reproduced from the fixture cache;
- `edgarpit qa` reports coverage, restatements, imputed timestamps, header-vs-API disagreements, unmapped concepts, unit anomalies and within-accession conflicts, and exits non-zero on a key violation;
- `edgarpit export` writes a deterministic Parquet release with a manifest;
- CI runs `lint`, `types`, `tests` and `dbt` on every pull request.

Everything below describes the merged code, not the issue text. Exact column lists stay in `docs/architecture.md` sections 5–7; this document is the map of how the pieces fit and the reasoning behind them.

> Correction (Epic 02): the fallback acceptance time described below was made conservative by decision A-23, and `fundamentals_pit` gained `restatement_kind` by decision A-25; see `docs/epic-02-integration.md`.

## How it works

The path a build takes: **fetch → ingest → load → dbt → query/API/explorer → QA → export**.

### Fetch (`edgarpit/fetch.py`)

`fetch_universe(issuers, out_dir, *, client, refresh=True)` drives one injected `SecClient` (one rate limiter for the whole run) per issuer:

- the submissions document and every page in `filings.files[]` (cache key = page stem),
- the companyfacts document,
- SGML headers for `ingest.headers.required_accessions(filings, fact_accessions)` — enough to time every `us-gaap`/`ifrs-full` fact and every 8-K/8-K/A (see the header rule below).

Headers are immutable, so a cached header is never re-fetched; other documents are re-fetched only when `refresh=True`, each under a new microsecond stamp (never overwritten, decision A-4). A 404 header is counted and tolerated; any other error is recorded on `IssuerFetchReport.errors` and stops only that issuer, so one bad issuer never sinks the run. `FetchReport.ok` is false when any issuer failed. The CLI refuses to fetch for an issuer without a resolved CIK.

### Ingest (`edgarpit/ingest/`)

- `submissions.parse_submissions(main, pages)` flattens `filings.recent` plus every page into the `raw.filings` columns, leaving `acceptance_ts`, `acceptance_ts_source` and `ts_imputed` null; `acceptance_ts_api` is the submissions value, kept for diagnostics only.
- `submissions.load_item_labels()` and `submissions.explode_8k_items(filings, item_labels)` produce `raw.events_8k`, one row per distinct 8-K item code; codes without a label become `"unknown"`; null/empty item strings yield no rows (counted in a debug line).
- `headers.parse_header(text)` reads `<ACCEPTANCE-DATETIME>`, `<ACCESSION-NUMBER>`, `<TYPE>`, `<FILING-DATE>`, optional `<PERIOD>` and `<ITEMS>` into a frozen `HeaderInfo`.
- `headers.apply_acceptance(filings, headers)` attaches `acceptance_ts` (header UTC) or falls back to `filing_date` 23:59:59 America/New_York with `acceptance_ts_source='filing_date_eod'` and `ts_imputed=true`. The fallback is an upper bound, so it is never lookahead.
- `headers.required_accessions(filings, fact_accessions)` returns the sorted, de-duplicated union of the given `(cik, accession)` fact pairs (including accessions missing from submissions) and every 8-K/8-K/A row — this is the header-selection rule the fetcher and its tests share.
- `companyfacts.parse_companyfacts(doc)` flattens one companyfacts document into `raw.facts_raw` for the `us-gaap`, `ifrs-full` and `dei` taxonomies (others are skipped and counted in a debug line), deduplicating only rows equal in every column; entries that differ only in `value` stay (versions or conflicts, decision A-7). `companyfacts.ingest_facts(raw, cik)` does the same for the latest cached document of an issuer.
- `companyfacts.conflicting_values(facts_raw)` returns the rows of a `(cik, taxonomy, concept, unit, period_start, period_end, accession)` group that disagree on `value`, for QA.
- `submissions.ingest_filings(raw, cik, item_labels)` composes the submissions, header and event steps over a cache; `ingest_facts` is its companyfacts counterpart.

### Load (`edgarpit/load.py`)

- `load_tables(con, *, filings, events_8k, facts_raw)` validates every frame with its Pandera schema first, sets the session timezone to UTC, then drops, recreates and fills the three `raw` tables in one transaction: an invalid frame leaves the previous tables untouched.
- `load_reference_tables(con, *, concept_map=None, universe_csv=None)` replaces `raw.concept_map` (flattened from `config/concept_map.yaml`, `us-gaap` before `ifrs-full`, rank 1 first) and `raw.universe` (from `config/universe.csv`, CIK read as a string).
- `build_from_raw(raw_dir, out_dir)` is the whole build: every CIK with a cached submissions document, ascending, is ingested with `ingest_filings` and `ingest_facts`; the frames are concatenated, validated and loaded; then dbt runs `build --target build` in-process through `dbtRunner` with `EDGARPIT_DB` pointed at `<out_dir>/edgarpit.duckdb` and `--target-path <out_dir>/dbt-target`. A dbt failure raises `RuntimeError`, so callers never receive a database whose tests did not pass. No network is used. `python -m edgarpit.load --raw DIR --out DIR` wraps it and prints the row counts and the dbt node summary.

### Transform (`transform/`)

A plain dbt-duckdb project (no packages, views by default, anonymous usage stats off), profile targets `fixtures` and `build`, database path from `EDGARPIT_DB`:

- `stg_filings`, `stg_facts` (`select distinct *`, so exact duplicates collapse) and `stg_events_8k` read the `raw` sources.
- `int_facts_mapped` joins `stg_facts` to `raw.concept_map` on `(taxonomy, concept)` — only mapped concepts pass — and left-joins `stg_filings` for `acceptance_ts`, falling back to `timezone('America/New_York', filed + interval '23:59:59')` with `ts_imputed = true` when the accession has no filing row.
- `fundamentals_pit` classifies each period by duration (350–380 days → `FY`; 80–100 days → `Q`; other → `OTHER`; instants are `FY` only when their `period_end` is a known fiscal-year end, else `Q`), keeps the lowest `concept_rank` per `(cik, field, period_start, period_end, accession)`, numbers versions by `(acceptance_ts, accession)`, and computes `first_value`, `is_restatement` (`abs(value - first_value) > 0.005 * abs(first_value)`, decision A-7) and `pct_change_vs_first`.
- `ratios_pit` computes `roe`, `leverage` and `net_margin` from the latest numerator and denominator accepted at or before each candidate acceptance; inputs must share a non-null currency and the denominator must be non-zero; `asof_ts = greatest(numerator_acceptance_ts, denominator_acceptance_ts)`.
- `filings` and `events_8k` add `ticker` to the raw staging tables through `raw.universe`.

Schema tests (not-null and accepted-values on every contract column, uniqueness of `(cik, field, period_start, period_end, accession)` and `(cik, ratio, period_start, period_end, asof_ts)`) plus singular tests: `assert_first_version_not_a_restatement`, `assert_versions_gapless`, `assert_ratio_inputs_not_from_future` and `assert_ratio_inputs_same_currency`. The future-input test is proven able to fail through a test-only `inject_future_ratio` variable (exercised by `tests/test_dbt_models.py`).

### Query, API, explorer

- `edgarpit/query.py` implements the read side. `asof(identifier, field, asof_ts, period="FY", n=1, *, db=None)` takes the rows with `acceptance_ts <= asof_ts` and the requested `period_type`, keeps the max `(acceptance_ts, version)` per period, orders by `period_end` desc and returns the first `n`. `history` returns every version; `events` filters `events_8k` by inclusive `acceptance_ts` bounds; `issuers()` and `fields(identifier)` drive the explorer. `identifier` is a ticker (case-insensitive) or a CIK (digits, zero-padded); naive timestamps raise `ValueError`; a path is opened read-only and a passed connection is used as-is.
- `api/main.py` exposes `GET /health`, `GET /asof`, `GET /history` and `GET /events` with the same parameters (`ticker` or `cik`, `field`, `asof`, `period`, `n`, `item`, `from`/`to`), response models mirroring the query dataclasses, HTTP 404 for unknown issuers and 422 for bad parameters.
- `explorer/app.py` is a Streamlit page: issuer/field/period selectors and an as-of date, an Altair revenue/value timeline that marks restatements and later versions, a version table with `known_at_asof`, and the 8-K event list.

### QA and export

- `edgarpit/qa.py` opens no writes: `run_qa(con)` returns a `QaReport` with per-issuer coverage, issuers without rows, restatement counts and top restatements, imputed timestamps by source, header-vs-API disagreements (over 60 s, worst ten), unmapped `us-gaap`/`ifrs-full` concepts by frequency, multi-currency fields, units outside their field's declared families, within-accession conflicting values, and key violations. `report.ok` is false only for key violations; `edgarpit qa` exits 1 for those or for an unreadable database.
- `edgarpit/export.py` writes one Parquet file per released table (`fundamentals_pit`, `ratios_pit`, `events_8k`, `filings`, `facts_raw`) with `COPY ... ORDER BY` over each table's key columns and fixed writer settings (ZSTD, 100,000-row groups, overwrite). Data files carry no build timestamps, so re-exporting the same database is byte-identical (`tests/test_export.py` proves it). `manifest.json` carries the build timestamp, the database file name, the SEC fetch range (min/max `fetched_at` from a raw `manifest.jsonl` when `--data` is given) and each file's row count, SHA-256 and schema.

### CLI (`edgarpit/cli.py`)

| Command | What it does |
|---|---|
| `edgarpit universe resolve [--universe PATH] (--offline --fixtures DIR \| --data DIR) [--write]` | Validates tickers against `company_tickers.json`; reports filled/disagreeing/unresolved CIKs; exit 1 unless everything is clean; writes only with `--write`. |
| `edgarpit fetch --universe PATH --out DIR` | Fetches submissions, pages, companyfacts and headers into the raw cache; refuses issuers without a resolved CIK; exit 1 if any issuer failed. |
| `edgarpit build (--offline --fixtures DIR \| --data DIR) --out DIR` | Runs `build_from_raw` and prints `database: <path>`. |
| `edgarpit asof --db PATH --field FIELD --asof TS (--cik ID \| --ticker SYM) [--period FY] [--n 1] [--json]` | Prints the as-of answer; exit 1 when no known value or unknown issuer; exit 2 on malformed/naive timestamps. |
| `edgarpit qa --db PATH` | Prints the QA report; exit 1 on a key violation. |
| `edgarpit export --db PATH --out DIR [--data RAW]` | Writes the Parquet release and `manifest.json`. |

Exit codes are `0` success, `1` data/fetch/build failure, `2` usage error; every failure prints one `error: …` line to stderr. `python -m edgarpit.devdb --out DIR`, `python -m edgarpit.load --raw DIR --out DIR` and `python -m edgarpit.rawcache verify DIR` remain available for the development database, the build and cache verification.

### CI

`.github/workflows/ci.yml`, jobs named exactly `lint` (ruff check + format check), `types` (`mypy edgarpit`, strict), `tests` (`pytest -q`, network tests deselected) and `dbt` (`devdb` on staging, then `dbt build --target fixtures`), on self-hosted macOS ARM64 runners with per-ref concurrency. All four are required on `main`.

## Key decisions and reasoning

- **The acceptance rule is upstream of everything.** `acceptance_ts` comes from the filing's immutable SGML header (`<ACCEPTANCE-DATETIME>`, America/New_York → UTC), because the submissions API value is currently shifted in both directions (decision A-1); `acceptance_ts_api` is stored for QA only. The fallback for a filing without a header is `filing_date` 23:59:59 New York with `ts_imputed=true`, chosen because it can only be later than the truth, never earlier. An ambiguous fall-back hour resolves to the later UTC instant; a time in the skipped spring-forward hour is an error (decision A-22). No query, model or test ever consults `period_end`, `filed`, `filing_date`, `fy` or `fp` to decide what was known.
- **Concept ranks are evidence-based.** `config/concept_map.yaml` is the only place a canonical field name exists (decision S-8). Ranks were revised after collecting the annual facts of all 40 universe issuers: `us-gaap:Revenues` now outranks contract revenue (which is a fee-income or revenue-subset line for banks, and a component for ENB, TRP, CVX, CP and WMT — decision A-18), and `RevenuesNetOfInterestExpense` covers WFC, which tags no `Revenues`. Components are never mapped to total fields; issuers with no candidate stay unmapped and show up in QA coverage.
- **`raw.facts_raw` is pure companyfacts.** It carries no timestamps of its own; dbt attaches `acceptance_ts` by joining `raw.filings` on `accession`, so PIT semantics live in one place. Exact duplicate rows collapse; same-key values that differ are kept, because only the filing's acceptance order can tell a version from a conflict (decision A-7).
- **Versions and restatements are first-value relative.** Versions are ordered by `(acceptance_ts, accession)` within `(cik, field, period_start, period_end)`; a restatement is a >0.5% move from the first value (and 0 → non-zero). Comparatives repeated in later filings are versions with `is_restatement=false`; no value is ever deleted.
- **One concept per `(field, period, accession)`, lowest rank wins.** Where an accession tags several mapped concepts of the same field, the lowest rank is used, which is why rank curation matters. Q4 values are not derived and `OTHER` durations (YTD) are excluded from `asof` (decision A-6).
- **Ratios are same-currency and never future-dated.** A ratio exists only when numerator and denominator share a currency and an as-of instant exists at which both are known: `asof_ts` is the later of the two acceptance times, and the dbt test `assert_ratio_inputs_not_from_future` enforces it. `edgarpit/ratios.py` mirrors the unit/currency rule in Python (`UnitMismatchError`) so callers and tests share one definition.
- **Reference tables, not hard-coded SQL.** dbt maps concepts and attaches tickers only through `raw.concept_map` and `raw.universe` (decision A-20), which are rebuilt from `config/` on every load, so changing the map never means editing SQL.
- **The raw cache is immutable and headers are forever.** Every body is written zstd-compressed under a microsecond stamp with SHA-256 manifest rows; an existing path is never overwritten. Headers never change, so a cached header is never re-fetched; other documents can be refreshed into new stamps and readers take the latest (decision A-4).
- **Determinism is a feature.** `build --offline` on the same cache produces the same database, and `export_release` writes Parquet files byte-identically across runs: fixed row order, no timestamps inside data files, fixed writer settings, build time only in `manifest.json` (section 9 of `docs/architecture.md`).
- **Failures are transactional and legible.** Frames are validated before a single statement runs; the loader replaces tables in one transaction; the CLI turns any failure into exit 1 with one `error: …` line; `build_from_raw` raises when dbt fails rather than returning a half-tested database.

## Interfaces for future epics

**Ingest** — implemented in `edgarpit/ingest/`:

```
parse_submissions(main, pages) -> pl.DataFrame
parse_header(text) -> HeaderInfo                     # HeaderInfo.period: date | None
apply_acceptance(filings, headers) -> pl.DataFrame
required_accessions(filings, fact_accessions) -> list[tuple[str, str]]
explode_8k_items(filings, item_labels) -> pl.DataFrame
ingest_filings(raw, cik, item_labels) -> tuple[pl.DataFrame, pl.DataFrame]
parse_companyfacts(doc) -> pl.DataFrame
ingest_facts(raw, cik) -> pl.DataFrame
conflicting_values(facts_raw) -> pl.DataFrame
```

**Load, ratios, query, export**:

```
load_tables(con, *, filings, events_8k, facts_raw) -> None
load_reference_tables(con, *, concept_map=None, universe_csv=None) -> None
build_from_raw(raw_dir, out_dir) -> Path             # ingest -> load -> dbt
compute_ratio(numerator, denominator) -> RatioValue  # raises UnitMismatchError
asof(identifier, field, asof_ts, period="FY", n=1, *, db=None) -> list[AsOfResult]
history(identifier, field, period=None, *, db=None) -> list[AsOfResult]
events(identifier, item=None, start=None, end=None, *, db=None) -> list[EventResult]
issuers(*, db=None) -> list[tuple[str, str | None]]
fields(identifier, *, db=None) -> list[str]
fetch_universe(issuers, out_dir, *, client, refresh=True) -> FetchReport
export_release(con, out_dir, *, raw_manifest=None) -> Manifest
```

`tests/test_interfaces.py` pins the original frozen signatures with `inspect.signature`; the Epic 01 additions (`required_accessions`, `ingest_filings`, `ingest_facts`, `load_reference_tables`, `issuers`, `fields`) are frozen once merged (decision A-21).

**Tables and columns.** `raw.filings` (accession unique), `raw.events_8k` (`(accession, item_code)` unique), `raw.facts_raw`, `raw.concept_map` (`field, taxonomy, concept, concept_rank, kind, unit_family`), `raw.universe` (`ticker, cik, name, country, filer_type`); query-visible `main.fundamentals_pit` (20 columns, key `(cik, field, period_start, period_end, accession)`), `main.ratios_pit` (14 columns, key `(cik, ratio, period_start, period_end, asof_ts)`), `main.filings` and `main.events_8k` (raw columns plus `ticker`). The exact column lists, nullability and types are the frozen contract in `docs/architecture.md` sections 6–7; `edgarpit/schemas.py` is its executable form.

**Header-selection rule** — `required_accessions(filings, fact_accessions)` fetches a header for every accession referenced by a `us-gaap`/`ifrs-full` fact (even one missing from submissions) and for every 8-K/8-K/A row; headers are cached forever. This is the exact set the fetcher iterates.

**Export manifest** — `<out>/fundamentals_pit.parquet`, `ratios_pit.parquet`, `events_8k.parquet`, `filings.parquet`, `facts_raw.parquet` and `manifest.json` with `build_timestamp`, `database`, `sec_fetch_range` (`min`/`max`, null without a raw manifest) and, per file, `table`, `file`, `rows`, `sha256` and `schema`.

**CLI and module entry points** — the Typer app `edgarpit` (`universe resolve`, `fetch`, `build`, `asof`, `qa`, `export`), plus `python -m edgarpit.devdb`, `python -m edgarpit.load` and `python -m edgarpit.rawcache verify`.

## Deviations from the plan

- **`history()` and `events()` take a keyword-only `db`** like `asof()`, even though only `asof()` was frozen in Epic 00; the three read the same database the same way.
- **Interfaces added during Epic 01.** `required_accessions`, `ingest_filings`, `ingest_facts`, `load_reference_tables`, `query.issuers`, `query.fields` and the `raw.concept_map` / `raw.universe` reference tables were added after the Epic 00 plan (decisions A-20, A-21) to keep the loader, dbt, query layer and explorer from re-spelling each other.
- **`fetch.py` reads fact accessions with a small private helper** that walks the companyfacts `accn` keys directly, rather than calling `ingest_facts`; the helper is documented as a stand-in and is the one place where the fetch path duplicates ingest logic.
- **`build_from_raw` runs dbt in-process** (`dbtRunner`, target `build`, artifacts under `<out>/dbt-target`) and exports `EDGARPIT_DB` for the duration of the run, instead of shelling out; a failed node raises `RuntimeError`.
- **Concept ranks changed after the Epic 00 map** (revenue totals over contract revenue, the WFC bank total, and comments for the non-obvious fallbacks) once the 40-issuer evidence existed; `config/concept_map.yaml` grew from 60 to 61 mapped pairs.
- **The release includes `raw.facts_raw`** alongside the four query tables, so a consumer can rebuild fundamentals without re-fetching.
- **The `company_tickers.json` cache location** (`raw/company_tickers/_/`) exists only because universe resolution needed it (decision A-17); it was not in the original A-4 layout.
- **Explorer and API are read-only clients of `query.py`** with no write path and no authentication, as planned for Phase 1, but they are single-process development services, not deployment-hardened.

## Known limitations and deferred items

- **The full live backfill has not been run here.** The fetch path is exercised through fixtures and the rate-limited client's own tests; `tests/fixtures/sec` is a trimmed three-issuer tree. A real build fetches submissions, companyfacts and one header per relevant filing for all 40 issuers (the one-time cost estimated in decision A-1).
- **Q4 is not derived** and `OTHER` durations (6/9-month YTD) are excluded from `asof`, so a quarter's cumulative-to-date value is not restated into a standalone quarter (decision A-6).
- **No FX conversion.** A ratio whose inputs are in different currencies is dropped (and reported by QA); there is no converted fallback.
- **Coverage gaps are reported, not patched.** Insurers tag no single IFRS 17 total-revenue element (MFC's `Revenue` is fee/other revenue), most IFRS issuers report shares outstanding only as `dei:EntityCommonStockSharesOutstanding` (which the concept map cannot carry), and issuers without `Liabilities` or `LongTermDebt` simply have `none` for those fields; QA's coverage and unmapped-concept sections are the honest answer.
- **`fetch.py` duplicates the companyfacts accession walk** (see deviations) — a follow-up should route it through `ingest_facts` once the fetch path stops being latency-sensitive.
- **Restatement detection is first-value relative and value-only**: a currency change without a value change, or a reclassification inside the 0.5% band, is not flagged as a restatement; QA's unit/currency anomalies are the safety net.
- **`ts_imputed` rows are upper bounds.** A filing whose header is missing (or whose accession is absent from submissions) is timed at `filing_date` 23:59:59 New York, which is safe for PIT but conservative.
- **Live SEC behaviour is not covered by CI.** Tests marked `network` are deselected; the one live smoke test (`test_sec_client.py`) runs manually.
- **The explorer is single-page and unauthenticated**, the API has no pagination or rate limiting, and neither is packaged for deployment.
- **Out of scope by phase:** a larger universe, 8-K document text, LLM extraction, a hosted multi-user service and derived analytics beyond the three ratios.
