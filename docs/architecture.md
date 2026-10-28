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
  filing index + 8-K documents                           │
ValetClient (same limiter) ──► Bank of Canada FX series  │
                                                         ▼
ingest/submissions.py ─► filings, events_8k  ┐
ingest/headers.py     ─► acceptance times    ├─► load.py (Pandera-validated) ─► DuckDB schema `raw`
ingest/companyfacts.py ─► facts_raw          ┘                                         │
text/ingest.py        ─► filings_text        ┐                                         │
fx/valet.py           ─► fx_daily            ┘                                         ▼
                                                     dbt (transform/): stg → int → fundamentals_pit, fundamentals_pit_usd,
                                                     ratios_pit, events_8k, filings
                                                                                       │
                        query.py (asof/history/events) ◄───────────────────────────────┘
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
| `edgarpit/schemas.py` | Pandera (Polars) schemas for `filings`, `facts_raw`, `events_8k` (staging) and `filings_text`, `fx_daily` (Phase 2 raw); the column lists are the staging contract. |
| `edgarpit/concepts.py` | Load `config/concept_map.yaml`; map `(taxonomy, concept)` → canonical field with rank. |
| `edgarpit/ingest/submissions.py` | Submissions JSON (+ pages) → `filings`, `events_8k` DataFrames. |
| `edgarpit/ingest/headers.py` | Parse `.hdr.sgml` → acceptance time (America/New_York → UTC), form, items, filing date. |
| `edgarpit/ingest/companyfacts.py` | Companyfacts JSON → `facts_raw` long DataFrame. |
| `edgarpit/text/discover.py` | Filing `-index-headers.html` → the primary 8-K/8-K/A document and every `EX-99.1` (decision A-30). |
| `edgarpit/text/clean.py` | EDGAR HTML → plain text (decision A-31). |
| `edgarpit/text/segment.py` | Plain text + the filing's item codes → item sections, or one `unsegmented` section (decision A-31). |
| `edgarpit/text/ingest.py` | Cached filing index and documents → `raw.filings_text`. |
| `edgarpit/fx/valet.py` | Bank of Canada Valet client (same limiter, retries and cache) and `parse_valet` → `raw.fx_daily` (decision A-32). |
| `edgarpit/load.py` | Validate DataFrames and load into `raw.*`; `build_from_raw(raw_dir, out_dir)` runs ingest → load → dbt. |
| `edgarpit/devdb.py` | Foundation helper: create `raw.*` tables from `tests/fixtures/staging/*.csv` (or empty) for dbt/query work before ingest exists. |
| `edgarpit/ratios.py` | Python ratio rule (same-currency/unit guard) used by tests and query. |
| `edgarpit/query.py` | `asof()`, `history()`, `events()` over DuckDB. |
| `edgarpit/qa.py` | Coverage, restatements, imputed timestamps, unmapped concepts, unit/currency anomalies, header-vs-API disagreements; non-zero exit on key violations. |
| `edgarpit/export.py` | Deterministic Parquet + `manifest.json`. |
| `edgarpit/fetch.py` | Fetch orchestration for a universe (submissions, pages, companyfacts, headers, filing indexes, 8-K documents, FX series); `incremental` skips unchanged issuers (decision A-34). |
| `edgarpit/cli.py` | Typer app `edgarpit`: `universe resolve`, `fetch`, `fetch-text`, `build`, `fx`, `asof`, `qa`, `export`. |
| `transform/` | dbt-duckdb project. |
| `api/main.py` | FastAPI over `query.py`. |
| `explorer/app.py` | Streamlit + Altair over `query.py`. |

## 4. Acceptance time (why the SGML header)

The submissions API's `acceptanceDateTime` is currently unreliable: as of October 2026 it is shifted by the New York UTC offset for many filings, in a direction that varies by issuer and day (verified: Apple's 2026-07-30 8-K, accepted 16:30:28 ET, is reported as `2026-07-31T00:30:28Z`). An early timestamp is lookahead, so the API value cannot be the source of truth.

- `acceptance_ts` comes from `<ACCEPTANCE-DATETIME>YYYYMMDDHHMMSS` in `https://www.sec.gov/Archives/edgar/data/<cik-int>/<accession-no-dashes>/<accession>.hdr.sgml`, interpreted in `America/New_York` (DST-aware via `zoneinfo`) and converted to UTC.
- Headers are fetched for every accession referenced by a `us-gaap`/`ifrs-full` fact and every 8-K/8-K/A, under the same rate limiter and User-Agent; they are immutable, so a cached header is never re-fetched.
- `acceptance_ts_api` keeps the API value for QA comparison only.
- Fallback when no header yields an acceptance time: `greatest(filing_date 23:59:59 America/New_York, acceptance_ts_api + 6 h)`, the API term only when present, with `acceptance_ts_source='filing_date_eod'` and `ts_imputed=true` (decision A-23). The six-hour margin covers the submissions API's observed four- to five-hour shift in either direction, so paper filings (which EDGAR may date a week before acceptance) are never timed before their true acceptance and cannot leak a value early.
- Pre-2001 headers carry no `<ACCEPTANCE-DATETIME>` (decision A-26): `parse_header` raises `MissingAcceptanceTime` only for that case, ingest uses the fallback above raised to at least `<DATE-OF-FILING-DATE-CHANGE>` 23:59:59 America/New_York when the header has one, and the header's `<ITEMS>` still feed the 8-K events. Any other malformed header fails the build.
- A cached header whose accession has no submissions row becomes a `raw.filings` row built from the header (`source='header'`, `acceptance_ts_source='sgml_header'`, decision A-24).

## 5. Raw cache

```
<dir>/raw/
  submissions/<cik10>/<fetched_at>.json.zst
  submissions_page/<cik10>/<page-stem>/<fetched_at>.json.zst
  companyfacts/<cik10>/<fetched_at>.json.zst
  headers/<cik10>/<accession>/<fetched_at>.hdr.sgml.zst
  company_tickers/_/<fetched_at>.json.zst
  filing_index/<cik10>/<accession>/<fetched_at>.html.zst
  documents/<cik10>/<accession>__<filename>/<fetched_at>.<ext>.zst
  fx/_/<series>/<fetched_at>.json.zst
  manifest.jsonl
```

- `<fetched_at>` = `YYYYMMDDTHHMMSSffffffZ` (UTC, microseconds). Exclusive create; never overwrite; write to a temp file then rename.
- `manifest.jsonl` rows: `path`, `url`, `endpoint`, `cik`, `sha256` (uncompressed body), `bytes`, `http_status`, `fetched_at`.
- Readers take the latest `<fetched_at>` per key and accept `.zst` or plain files. Fixtures use the same layout uncompressed.
- `filing_index` keys are accessions: the page is `https://www.sec.gov/Archives/edgar/data/<cik-int>/<acc-no-dashes>/<accession>-index-headers.html`, which lists each document's SGML `<TYPE>` and `<FILENAME>` (`index.json` has no types).
- `documents` keys are `<accession>__<filename>` and the stored file keeps the document's own extension (`.htm`, `.html`, `.txt`), so a key names exactly one file and a reader never mistakes one document for another. A filing's documents are immutable, so they are fetched once and never re-fetched.
- `fx` keys are Bank of Canada Valet series ids (`FXUSDCAD`) in the global `_` slot, with `""` in the manifest's `cik`, like `company_tickers`. A series is stored whole, so the key does not depend on the date range requested.
- All endpoints share the same rate limiter, User-Agent and retry policy (decisions A-2, A-3); the Bank of Canada requests go through the same client.

### Incremental fetch (decision A-34)

`data.sec.gov` sends no `ETag` or `Last-Modified`, so the only change signal is the document itself. An issuer is unchanged when the newly fetched submissions document's newest accession and filing count equal the cached one (`fetch.submissions_unchanged`); then its pages, companyfacts, headers, filing indexes and documents are skipped and the run reports `skipped N unchanged`. One submissions request per issuer per run. A submissions document with a new accession, a different count or no cached predecessor is fetched in full, so the run is always safe to repeat.

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
| source | VARCHAR | `recent`, a page file name, or `header` (a row built from a cached header, decision A-24) |

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

### `raw.filings_text` — one row per (document, section) of an 8-K (decision A-31)

| column | type | notes |
|---|---|---|
| cik | VARCHAR(10) | |
| accession | VARCHAR | |
| acceptance_ts | TIMESTAMPTZ | copied from `raw.filings`; the only thing that decides when the text is known |
| ts_imputed | BOOLEAN | copied from `raw.filings` |
| document | VARCHAR | EDGAR file name, e.g. `aapl-20260730.htm` |
| doc_type | VARCHAR | `primary` (the 8-K/8-K/A document) \| `EX-99.1` |
| section | VARCHAR | EDGAR item code (`2.02`) \| `unsegmented` |
| section_order | INTEGER | 1-based position in the document |
| text | VARCHAR | cleaned plain text |
| sha256 | VARCHAR | lowercase hex SHA-256 of `text` |
| char_count | INTEGER | character length of `text` |

Unique key: `(accession, document, section, section_order)`.

Text rule (decision A-31): the documents of a filing come from its
`-index-headers.html` page — the primary 8-K/8-K/A document and every `EX-99.1`
exhibit. Each is converted from HTML to text with BeautifulSoup and the lxml
parser: scripts, styles, `head` and hidden XBRL are removed, tables are flattened
to one tab-separated line per row, entities are resolved and whitespace is
normalised. The text is split on `Item N.NN` headings that match the filing's own
item list; when the headings do not cover the expected items exactly once and in
order, the whole document is a single `section='unsegmented'` row instead of a
guess. `sha256` and `char_count` describe the stored `text`, so a consumer can
deduplicate and verify a section without re-reading the document. Nothing in the
document (its dates, its period) decides what was known: the row carries the
filing's `acceptance_ts`.

### `raw.fx_daily` — one row per (series, rate date) (decision A-32)

| column | type | notes |
|---|---|---|
| series | VARCHAR | Valet series id; `FXUSDCAD` = Canadian dollars per US dollar |
| rate_date | DATE | observation date (a Bank of Canada business day) |
| value | DOUBLE | rate in the series' quotation; strictly positive |
| known_at | TIMESTAMPTZ | `rate_date` 17:00 `America/Toronto` in UTC |
| source | VARCHAR | `valet` |

Unique key: `(series, rate_date)`.

FX rule (decision A-32): the Bank of Canada Valet `FXUSDCAD` series is business
daily from 2017-01-03. The publication time is not in the response, so a rate for
date `d` is treated as known at `d` 17:00 `America/Toronto` (the Bank publishes
around 16:30 ET): never earlier than publication, so never lookahead.
Conversion uses the latest observation on or before the period end, and a
converted value's `asof_ts` is `greatest(acceptance_ts, fx_known_at)`. Only `CAD`
and `USD` currency units are converted; other currencies are excluded and counted
by QA. Period ends before the first observation are not converted and are counted
by QA too.

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

### `fundamentals_pit_usd` — `fundamentals_pit` converted to USD (decision A-32)

Every column of `fundamentals_pit`, unchanged and in the same order, plus:

| column | type | notes |
|---|---|---|
| value_usd | DOUBLE NULL | `value` in USD; null when `converted` is false |
| fx_rate | DOUBLE NULL | `FXUSDCAD` observation used (Canadian dollars per US dollar); `1.0` for a USD value; null when unconverted |
| fx_rate_date | DATE NULL | observation date of `fx_rate`; null for a USD value and for an unconverted row |
| fx_known_at | TIMESTAMPTZ NULL | `fx_rate_date` 17:00 `America/Toronto` in UTC; null like `fx_rate_date` |
| converted | BOOLEAN | true when `value_usd` is populated |
| asof_ts | TIMESTAMPTZ | `greatest(acceptance_ts, fx_known_at)`: when the converted row is known |

Key: the same `(cik, field, period_start, period_end, accession)` as
`fundamentals_pit`, so a converted row keeps the version identity of the row it
came from.

Conversion rule: a `USD` row needs no observation — `value_usd = value`,
`fx_rate = 1.0`, `fx_rate_date` and `fx_known_at` null, `asof_ts = acceptance_ts`.
A `CAD` row uses the latest `FXUSDCAD` observation on or before `period_end`:
`value_usd = value / fx_rate` and `asof_ts = greatest(acceptance_ts, fx_known_at)`.
A row whose currency is neither `USD` nor `CAD`, or whose `period_end` precedes
the first observation, keeps `value_usd`, `fx_rate`, `fx_rate_date` and
`fx_known_at` null, sets `converted = false`, keeps `asof_ts = acceptance_ts`, and
is counted by QA. Consumers must filter on `asof_ts <= t` (not `acceptance_ts`)
when they use `value_usd`, so a CAD value cannot appear before its rate was
published.

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

- `asof(identifier, field, asof_ts, period="FY", n=1, *, db=None, currency=None) -> list[AsOfResult]`: candidates are rows with `acceptance_ts <= asof_ts` and `period_type = period`; per period keep the max `(acceptance_ts, version)`; order by `period_end` desc; first `n`. `AsOfResult` fields: cik, ticker, field, value, unit, currency, period_start, period_end, period_type, accession, form, acceptance_ts, version, is_restatement, ts_imputed, restatement_kind (decision A-25; the explorer marks only `revision` as a restatement and gives `split_adjustment` its own status).
- `history(identifier, field, period=None, *, db=None, currency=None)` → every version, ordered by period_end, version.
- `events(identifier, item=None, start=None, end=None)` → rows of `events_8k` by acceptance_ts.
- `asof_ts` strings must carry a timezone (`Z` or offset); naive inputs are rejected.
- `currency="USD"` reads `main.fundamentals_pit_usd` and applies the point-in-time rule to `asof_ts` instead of `acceptance_ts`, returning `value_usd` with the same `AsOfResult` fields (decision A-32). Until that model is built the keyword raises `NotImplementedError`; `None` keeps the row's own currency.

REST (FastAPI): `GET /asof`, `GET /history`, `GET /events` with the same parameters; `ticker` or `cik`; OpenAPI at `/docs`.

## 9. Determinism and export

`build --offline` must produce identical Parquet hashes across runs: fixed `ORDER BY` on every exported table, no build timestamps inside data files, fixed Parquet writer settings (compression, row group size). Build time and fetch range go only into `manifest.json`.

## 10. Testing

- Unit + property tests (Hypothesis) with no network; HTTP via respx; data via fixtures.
- Classic-bug tests (must exist, must be able to fail): restatement accepted after as-of not returned; period end before as-of but filed after excluded; property "no row after as-of"; dbt future-input test; IFRS and US GAAP concepts map to the same field; mixed-currency ratio rejected.
- `@pytest.mark.network` tests are excluded from CI.

## 11. CI

GitHub Actions, workflow `ci.yml`, jobs `lint`, `types`, `tests`, `dbt` (required checks on `main`), on GitHub-hosted `ubuntu-24.04` runners (decision C-5). Each job installs with `uv sync --frozen`; `astral-sh/setup-uv` caches uv's package cache keyed on the committed `uv.lock`, and uv installs the interpreter pinned by `.python-version` (3.12). The four jobs run in parallel, each under a `timeout-minutes` cap, and only `pull_request` and pushes to `main` trigger the workflow; the workflow token is read-only (`permissions: contents: read`) and no secrets are used. Temp paths are under `$RUNNER_TEMP`, never a fixed shared path (decision A-14). Tests marked `network` are deselected. The self-hosted `edgar-pit-docker-1` container runner (decision C-4) was retired before the repository became public: on a public repository anyone's pull request can execute code, and hosted runners keep that code off the owner's machine (PLAYBOOK §7). Hosted minutes are included in the GitHub plan; the repo's own workflows do not run for forks' pushes.

## 12. Command line (Phase 2 surface)

The existing commands are unchanged; Phase 2 adds these signatures. Each lands
with the issue that implements it, and the flags below are the agreed surface.

```
edgarpit fetch --universe CSV --out DIR [--incremental]
edgarpit fetch-text --db PATH --items 2.02 [--limit N] [--data ./data]
edgarpit fx --out DIR
edgarpit asof --db PATH (--cik CIK | --ticker T) --field F --asof TS [--currency USD]
```

- `fetch --incremental` fetches one submissions document per issuer and skips the
  issuers whose document is unchanged, reporting `skipped N unchanged`
  (decision A-34).
- `fetch-text` reads the 8-K filings whose `items` contain one of `--items` from
  the database at `--db`, extracts their cached filing index and documents under
  `--data` (default `./data`) into `raw.filings_text`, and stops after `--limit`
  filings when given. It never fetches a document that is already cached
  (decision A-30).
- `fx` fetches the `FXUSDCAD` history into the raw cache at `--out` and prints
  the row count; a cached series is reused, so a repeat run makes no request.
- `asof --currency USD` reads `main.fundamentals_pit_usd` and applies the
  point-in-time rule to `asof_ts` (decision A-32).
