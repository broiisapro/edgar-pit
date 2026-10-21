# edgar-pit — architecture


## 1. Purpose and the one rule

edgar-pit builds a point-in-time (PIT) fundamentals and corporate-events dataset from SEC EDGAR. The single correctness rule is:

> A value is known at time `t` if and only if the filing that carried it has `acceptance_ts <= t`.

`period_end`, `filed`, `filing_date`, `fy` and `fp` never decide what was known. Every query, model and test is built around that rule.

## 2. Data flow

```
config/universe.csv
      │
      ▼
SecClient (rate-limited, retrying, User-Agent)  ──►  raw cache (immutable, zstd, SHA-256 manifest)
  submissions + paginated pages                          │
  companyfacts                                           │
  per-filing SGML headers (acceptance time)              │
                                                         ▼
ingest/submissions.py ─► filings, events_8k  ┐
ingest/headers.py     ─► acceptance times    ├─► load.py (Pandera-validated) ─► DuckDB schema `raw`
ingest/companyfacts.py ─► facts_raw          ┘                                         │
                                                                                        ▼
                                                     dbt (transform/): stg → int → fundamentals_pit, ratios_pit, events_8k, filings
                                                                                        │
                        query.py (asof/history/events) ◄────────────────────────────────┘
                          │            │             │
                     Python API    FastAPI       Streamlit explorer
                                                                                        │
                                                         export.py ─► Parquet + manifest.json (GitHub Release)
```

## 3. Modules

| Module | Responsibility |
|---|---|
| `edgarpit/config.py` | Settings from env (`SEC_USER_AGENT`, rate limit, paths, `EDGARPIT_DB`); pydantic-settings; `.env` support. |
| `edgarpit/sec_client.py` | `SecClient`: `get_json(url)`, `get_text(url)`; token bucket ≤5 req/s; tenacity retries; raw cache writer + manifest; offline mode reading a cache/fixtures dir. |
| `edgarpit/rawcache.py` | Raw cache layout, exclusive-create writes, manifest append, "latest per key" reader (compressed or plain). |
| `edgarpit/universe.py` | Load/validate `config/universe.csv`; `resolve` against `company_tickers.json`. |
| `edgarpit/schemas.py` | Pandera (Polars) schemas for `filings`, `facts_raw`, `events_8k`; the column lists are the staging contract. |
| `edgarpit/concepts.py` | Load `config/concept_map.yaml`; map `(taxonomy, concept)` → canonical field with rank. |
| `edgarpit/ingest/submissions.py` | Submissions JSON (+ pages) → `filings`, `events_8k` DataFrames. |
| `edgarpit/ingest/headers.py` | Parse `.hdr.sgml` → acceptance time (America/New_York → UTC), form, items, filing date. |
| `edgarpit/ingest/companyfacts.py` | Companyfacts JSON → `facts_raw` long DataFrame. |
| `edgarpit/load.py` | Validate DataFrames and load into `raw.*`; `build_from_raw(raw_dir, out_dir)` runs ingest → load → dbt. |
| `edgarpit/devdb.py` | Foundation helper: create `raw.*` tables from `tests/fixtures/staging/*.csv` (or empty) for dbt/query work before ingest exists. |
| `edgarpit/ratios.py` | Python ratio rule (same-currency/unit guard) used by tests and query. |
| `edgarpit/query.py` | `asof()`, `history()`, `events()` over DuckDB. |
| `edgarpit/qa.py` | Coverage, restatements, imputed timestamps, unmapped concepts, unit/currency anomalies, header-vs-API disagreements; non-zero exit on key violations. |
| `edgarpit/export.py` | Deterministic Parquet + `manifest.json`. |
| `edgarpit/fetch.py` | Fetch orchestration for a universe (submissions, pages, companyfacts, headers). |
| `edgarpit/cli.py` | Typer app `edgarpit`: `universe resolve`, `fetch`, `build`, `asof`, `qa`, `export`. |
| `transform/` | dbt-duckdb project. |
| `api/main.py` | FastAPI over `query.py`. |
| `explorer/app.py` | Streamlit + Altair over `query.py`. |

## 4. Acceptance time (why the SGML header)

The submissions API's `acceptanceDateTime` is currently unreliable: as of October 2026 it is shifted by the New York UTC offset for many filings, in a direction that varies by issuer and day (verified: Apple's 2026-07-30 8-K, accepted 16:30:28 ET, is reported as `2026-07-31T00:30:28Z`). An early timestamp is lookahead, so the API value cannot be the source of truth.

- `acceptance_ts` comes from `<ACCEPTANCE-DATETIME>YYYYMMDDHHMMSS` in `https://www.sec.gov/Archives/edgar/data/<cik-int>/<accession-no-dashes>/<accession>.hdr.sgml`, interpreted in `America/New_York` (DST-aware via `zoneinfo`) and converted to UTC.
- Headers are fetched for every accession referenced by a `us-gaap`/`ifrs-full` fact and every 8-K/8-K/A, under the same rate limiter and User-Agent; they are immutable, so a cached header is never re-fetched.
- `acceptance_ts_api` keeps the API value for QA comparison only.
- Fallback when no header exists: `filing_date` 23:59:59 America/New_York → UTC, `acceptance_ts_source='filing_date_eod'`, `ts_imputed=true` (an upper bound on the true time, so never lookahead).

## 5. Raw cache

```
<dir>/raw/
  submissions/<cik10>/<fetched_at>.json.zst
  submissions_page/<cik10>/<page-stem>/<fetched_at>.json.zst
  companyfacts/<cik10>/<fetched_at>.json.zst
  headers/<cik10>/<accession>/<fetched_at>.hdr.sgml.zst
  manifest.jsonl
```

- `<fetched_at>` = `YYYYMMDDTHHMMSSffffffZ` (UTC, microseconds). Exclusive create; never overwrite; write to a temp file then rename.
- `manifest.jsonl` rows: `path`, `url`, `endpoint`, `cik`, `sha256` (uncompressed body), `bytes`, `http_status`, `fetched_at`.
- Readers take the latest `<fetched_at>` per key and accept `.zst` or plain files. Fixtures use the same layout uncompressed.

## 6. Staging contract (`raw` schema, Pandera in `edgarpit/schemas.py`)

### `raw.filings` — one row per filing
| column | type | notes |
|---|---|---|
| cik | VARCHAR(10) | zero-padded |
| accession | VARCHAR | `NNNNNNNNNN-YY-NNNNNN`, unique |
| form | VARCHAR | e.g. `10-K`, `40-F`, `8-K/A` |
| filing_date | DATE | |
| report_date | DATE NULL | |
| acceptance_ts | TIMESTAMPTZ | authoritative, UTC |
| acceptance_ts_source | VARCHAR | `sgml_header` \| `filing_date_eod` |
| acceptance_ts_api | TIMESTAMPTZ NULL | diagnostic only |
| ts_imputed | BOOLEAN | `acceptance_ts_source <> 'sgml_header'` |
| primary_document | VARCHAR NULL | |
| items | VARCHAR NULL | raw comma-separated 8-K items |
| is_amendment | BOOLEAN | form ends with `/A` |
| is_xbrl | BOOLEAN | |
| source | VARCHAR | `recent` or the page file name |

### `raw.events_8k` — one row per (8-K or 8-K/A, item)
| column | type |
|---|---|
| cik | VARCHAR(10) |
| accession | VARCHAR |
| form | VARCHAR |
| acceptance_ts | TIMESTAMPTZ |
| ts_imputed | BOOLEAN |
| item_code | VARCHAR (e.g. `2.02`) |
| item_label | VARCHAR (from `config/8k_items.yaml`; `unknown` if unmapped) |

Unique key: `(accession, item_code)`.

### `raw.facts_raw` — one row per companyfacts entry
| column | type | notes |
|---|---|---|
| cik | VARCHAR(10) | |
| taxonomy | VARCHAR | `us-gaap` \| `ifrs-full` \| `dei` |
| concept | VARCHAR | |
| unit | VARCHAR | as in companyfacts (`USD`, `CAD`, `shares`, `USD/shares`, `pure`) |
| value | DOUBLE | |
| period_start | DATE NULL | null for instants |
| period_end | DATE | |
| fy | INTEGER NULL | filing's fiscal year — never period identity |
| fp | VARCHAR NULL | filing's fiscal period — never period identity |
| form | VARCHAR | |
| accession | VARCHAR | |
| filed | DATE | |
| frame | VARCHAR NULL | |

`facts_raw` is pure companyfacts; `acceptance_ts` is attached in dbt by joining `raw.filings` on `accession`.

### `raw.concept_map` — one row per (field, taxonomy, concept) (decision A-20)

| column | type | notes |
|---|---|---|
| field | VARCHAR | canonical field from `config/concept_map.yaml` |
| taxonomy | VARCHAR | `us-gaap` \| `ifrs-full` |
| concept | VARCHAR | |
| concept_rank | INTEGER | 1-based position in the field's ordered list for that taxonomy |
| kind | VARCHAR | `duration` \| `instant` |
| unit_family | VARCHAR | `currency` \| `shares` \| `currency_per_share` |

Loaded by `edgarpit.load.load_reference_tables`; dbt maps concepts only through this table.

### `raw.universe` — one row per issuer (decision A-20)

| column | type | notes |
|---|---|---|
| ticker | VARCHAR | |
| cik | VARCHAR(10) | zero-padded |
| name | VARCHAR | |
| country | VARCHAR | |
| filer_type | VARCHAR | |

Loaded by `edgarpit.load.load_reference_tables` from `config/universe.csv`; dbt attaches tickers only through this table.

## 7. Column contract — dbt output models (schema `main`)

Frozen in Epic 0. The query layer depends only on these columns.

### `fundamentals_pit` — one row per (cik, field, period_start, period_end, accession)
| column | type | notes |
|---|---|---|
| cik | VARCHAR(10) | |
| ticker | VARCHAR NULL | from universe |
| field | VARCHAR | canonical field from `config/concept_map.yaml` |
| period_start | DATE NULL | null for instant fields |
| period_end | DATE | |
| period_type | VARCHAR | `FY` \| `Q` \| `OTHER` (decision A-6) |
| accession | VARCHAR | |
| form | VARCHAR | |
| acceptance_ts | TIMESTAMPTZ | |
| ts_imputed | BOOLEAN | |
| value | DOUBLE | |
| unit | VARCHAR | |
| currency | VARCHAR NULL | ISO code parsed from unit (`USD`, `CAD`, …); null for `shares`/`pure` |
| source_taxonomy | VARCHAR | |
| source_concept | VARCHAR | |
| concept_rank | INTEGER | position in the field's ordered concept list (1 = preferred) |
| version | INTEGER | 1..n by (acceptance_ts, accession) within (cik, field, period_start, period_end) |
| first_value | DOUBLE | value of version 1 |
| is_restatement | BOOLEAN | decision A-7 |
| pct_change_vs_first | DOUBLE NULL | |
| restatement_kind | VARCHAR | `none` \| `split_adjustment` \| `revision` (decision A-25); `split_adjustment` only for share-based unit families whose change is a near-integer split factor |

Concept selection: for each (cik, field, period, accession) keep the lowest `concept_rank` present in that accession.

### `ratios_pit` — one row per (cik, ratio, period_start, period_end, asof_ts)
| column | type |
|---|---|
| cik | VARCHAR(10) |
| ratio | VARCHAR (`roe`, `leverage`, `net_margin`) |
| period_start | DATE NULL |
| period_end | DATE |
| period_type | VARCHAR |
| asof_ts | TIMESTAMPTZ (max acceptance_ts of inputs used) |
| value | DOUBLE |
| currency | VARCHAR |
| numerator_field, numerator_accession | VARCHAR |
| numerator_acceptance_ts | TIMESTAMPTZ |
| denominator_field, denominator_accession | VARCHAR |
| denominator_acceptance_ts | TIMESTAMPTZ |

Invariant (dbt test `assert_ratio_inputs_not_from_future`): `numerator_acceptance_ts <= asof_ts AND denominator_acceptance_ts <= asof_ts`.

### `events_8k` — as `raw.events_8k`, plus `ticker`. `filings` — as `raw.filings`, plus `ticker`.

## 8. Query semantics

- `asof(identifier, field, asof_ts, period="FY", n=1, *, db=None) -> list[AsOfResult]`: candidates are rows with `acceptance_ts <= asof_ts` and `period_type = period`; per period keep the max `(acceptance_ts, version)`; order by `period_end` desc; first `n`. `AsOfResult` fields: cik, ticker, field, value, unit, currency, period_start, period_end, period_type, accession, form, acceptance_ts, version, is_restatement, ts_imputed, restatement_kind (decision A-25; the explorer marks only `revision` as a restatement and gives `split_adjustment` its own status).
- `history(identifier, field, period=None)` → every version, ordered by period_end, version.
- `events(identifier, item=None, start=None, end=None)` → rows of `events_8k` by acceptance_ts.
- `asof_ts` strings must carry a timezone (`Z` or offset); naive inputs are rejected.

REST (FastAPI): `GET /asof`, `GET /history`, `GET /events` with the same parameters; `ticker` or `cik`; OpenAPI at `/docs`.

## 9. Determinism and export

`build --offline` must produce identical Parquet hashes across runs: fixed `ORDER BY` on every exported table, no build timestamps inside data files, fixed Parquet writer settings (compression, row group size). Build time and fetch range go only into `manifest.json`.

## 10. Testing

- Unit + property tests (Hypothesis) with no network; HTTP via respx; data via fixtures.
- Classic-bug tests (must exist, must be able to fail): restatement accepted after as-of not returned; period end before as-of but filed after excluded; property "no row after as-of"; dbt future-input test; IFRS and US GAAP concepts map to the same field; mixed-currency ratio rejected.
- `@pytest.mark.network` tests are excluded from CI.

## 11. CI

GitHub Actions, workflow `ci.yml`, jobs `lint`, `types`, `tests`, `dbt` (required checks on `main`), on GitHub-hosted `ubuntu-24.04` runners (decision C-5). Each job installs with `uv sync --frozen`; `astral-sh/setup-uv` caches uv's package cache keyed on the committed `uv.lock`, and uv installs the interpreter pinned by `.python-version` (3.12). The four jobs run in parallel, each under a `timeout-minutes` cap, and only `pull_request` and pushes to `main` trigger the workflow; the workflow token is read-only (`permissions: contents: read`) and no secrets are used. Temp paths are under `$RUNNER_TEMP`, never a fixed shared path (decision A-14). Tests marked `network` are deselected. The self-hosted `edgar-pit-docker-1` container runner (decision C-4) was retired before the repository became public: on a public repository anyone's pull request can execute code, and hosted runners keep that code off the owner's machine (PLAYBOOK §7). Hosted minutes are included in the GitHub plan; the repo's own workflows do not run for forks' pushes.
