# Epic 05 — FX normalization and USD values

## Goal

A Canadian issuer reports in Canadian dollars, and a reader comparing it with a US issuer needs one currency. This epic adds a point-in-time USD view of the fundamentals without relaxing the project's one rule: a value is usable at time `t` only if both the filing that carried it and the exchange rate that converted it were public by `t`.

The epic ends when:

- the Bank of Canada `FXUSDCAD` series is captured as a fixture, and its publication time and pre-2017 history are researched rather than assumed (#101);
- `parse_valet` turns a Valet response into `raw.fx_daily` rows and `fetch_fx` stores the series in the raw cache only when it changed (#111);
- `build` loads `raw.fx_daily` and dbt builds `main.fundamentals_pit_usd`, with an explicit reason for every unconverted row (#112);
- USD values and 8-K text reach the Python API, the REST service and the explorer (#113);
- QA reports FX coverage and USD conversion, and treats a known time earlier than its inputs as a failure (#116);
- the release export includes `fundamentals_pit_usd.parquet` and `fx_daily.parquet` when the database holds them (#117);
- every USD result carries the exact `asof_ts` at which it became known through Python, REST and the explorer, and the CLI reads the same model (#133, #131).

Everything below describes the merged code. The fixture numbers come from an offline build of `tests/fixtures/sec` at the commit that added this document; the 250-issuer live run is recorded in [`docs/epic-06-universe-incremental.md`](epic-06-universe-incremental.md) and is not repeated here.

## How it works

### The Valet research (issue #101, PR #103)

`GET https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json` returns one JSON object:

- `terms`: a link to the Bank's terms;
- `seriesDetail.FXUSDCAD`: label `USD/CAD`, description "Daily average exchange rate of the US dollar in Canadian dollars", `dateFormat: "daily"`, unit "Canadian dollars per US dollar", frequency "Business daily";
- `observations`: `[{"d": "YYYY-MM-DD", "FXUSDCAD": {"v": "1.3435"}}, …]`, ascending by date, one entry per business day, with string values carrying four decimals.

There is no pagination: one request returns the whole history. The captured series had **2,449 observations from 2017-01-03 to 2026-10-27** (299,351 bytes). The first date is the first observation Valet serves, not a truncation of the request.

**Publication time.** The Bank's publication-details page says daily average rates are published "by 16:30 ET" on days when its Ottawa Head Office is open, and that a rate not published by 17:00 ET is shown as "Not available" for that day. The Valet response carries no timestamp, so a rate for date `d` is treated as known at `d` **17:00 America/Toronto** (decision A-32): 21:00 UTC under EDT, 22:00 UTC under EST. 17:00 is the Bank's own backstop, so the dataset is never earlier than publication; for 16:30–17:00 it is up to half an hour conservative.

**Pre-2017.** Valet carries the retired noon rates under `IEXE0101` ("U.S. dollar (noon)", 2007-05-01 to 2017-04-28, 2,504 observations, CAD per USD) and `IEXE0101_RECIPROCAL` (1950-10-02 to 2017-04-28, 16,743 observations, USD per CAD). The two daily series overlap for 82 business days, with a mean relative difference of about 0.01 % and a largest single-day difference of about 0.6 %, so a splice would not show an obvious level break. But a noon quote and a trimmed mean of quotes collected through the day measure different things, and the Bank explicitly does not restate the old series. **Decision A-39: do not chain them.** A CAD period ending before 2017-01-03 is not converted and is flagged `no_rate`. `IEXE0101` stays in `tests/fixtures/fx/raw` as research evidence and is never ingested (decision A-44); `parse_valet` rejects a document that does not describe the requested series, so the legacy file cannot parse as an empty `FXUSDCAD` frame.

The fixture root `tests/fixtures/sec/raw/fx/_/FXUSDCAD/` holds the response byte for byte, and `tests/fixtures/fx_expected.json` records six hand-checked conversion cases: a period end before the series starts (no rate), the first observation, the 2023 Royal Bank period end, a Sunday period end priced at the preceding Friday, Good Friday priced at the preceding Thursday, and Labour Day priced at the preceding Friday. `tests/test_fixtures_fx.py` checks the hashes, the response shape, strict date ordering, positive values, the published windows and the expected cases.

### `raw.fx_daily` (decision A-32, decision A-38; issue #111, PR #119)

`edgarpit.fx.valet.parse_valet(doc, series="FXUSDCAD")` returns one row per observation:

| column | meaning |
|---|---|
| `series` | Valet series id (`FXUSDCAD`) |
| `rate_date` | observation date (`DATE`) |
| `value` | rate in the series' quotation (Canadian dollars per US dollar), strictly positive |
| `known_at` | `rate_date` 17:00 `America/Toronto` converted to UTC |
| `source` | `valet` |

Observations without a value for the series — Valet emits an empty string on holidays and days the Bank is closed — are dropped and the count is logged. A missing `d`, a repeated date, a value that is not a finite positive number, a document without `seriesDetail[series]` and a document without an `observations` list raise `ValueError`, because a wrong-series document parsing as an empty frame would silently turn every CAD period into `no_rate`. The frame is validated against `FxDailySchema` before it is returned; `(series, rate_date)` is unique.

`fetch_fx(data_dir, *, series="FXUSDCAD", client=None)` makes exactly one request per call, parses before writing, and stores the body under `raw/fx/_/<series>/` only when its SHA-256 differs from the latest cached body. The series gains an observation every business day, so unlike a filing document it is fetched every time; an unchanged series adds no file. `ValetClient` wraps the same `SecClient` used for EDGAR — same token bucket, retries, User-Agent and raw cache — so the Bank's traffic obeys the same fair-access rules.

### `main.fundamentals_pit_usd` (decision A-32, decision A-38; issue #112, PR #129)

`main.fundamentals_pit_usd` keeps every column of `main.fundamentals_pit` unchanged and in the same order, then adds:

| column | meaning |
|---|---|
| `value_usd` | the value in USD, null when unconverted |
| `fx_rate` | the `FXUSDCAD` observation used; `1.0` for a USD value |
| `fx_rate_date` | that observation's date; null for USD and unconverted rows |
| `fx_known_at` | that date's 17:00 Toronto instant in UTC; null like `fx_rate_date` |
| `converted` | true when `value_usd` is populated |
| `asof_ts` | when the row is known: `greatest(acceptance_ts, fx_known_at)` for a converted CAD row, `acceptance_ts` otherwise |
| `unconverted_reason` | null when converted; `no_rate` for a CAD period before the first observation; `currency_out_of_scope` for any other currency, including rows with no currency |

The conversion rule is arithmetic, not statistical:

- a `USD` row needs no observation: `value_usd = value`, `fx_rate = 1.0`, `fx_rate_date` and `fx_known_at` null, `asof_ts = acceptance_ts`;
- a `CAD` row uses the latest `FXUSDCAD` observation whose `rate_date` is **on or before the period end**: `value_usd = value / fx_rate` and `asof_ts = greatest(acceptance_ts, fx_known_at)`;
- every other row keeps `value_usd` null, sets `converted = false`, records the reason, and keeps `asof_ts = acceptance_ts`.

The key is the same `(cik, field, period_start, period_end, accession)` as `fundamentals_pit`, so a converted row keeps the version identity of the reported row it came from. Consumers must filter on `asof_ts <= t`, never on `acceptance_ts`; the model's docstring, the query layer and the REST response all say so.

The rate choice is deliberately independent of publication order: it is the latest observation dated on or before the period end, even if a later-dated observation was published earlier. A rate published after the filing moves `asof_ts` forward; a rate published before it does not.

### The point-in-time rule and its tests

The rule has two halves — the rate exists, and the rate is public — and both are tested at the SQL and Python layers.

**dbt singular tests** (`transform/tests/`):

- `assert_asof_not_before_filing_or_rate.sql` — no row's `asof_ts` is earlier than its `acceptance_ts` or its `fx_known_at`, and a converted CAD row's `asof_ts` is exactly `greatest(acceptance_ts, fx_known_at)`;
- `assert_converted_matches_value_usd.sql` — `converted` holds exactly when `value_usd` is populated and `unconverted_reason` is null; a converted row records a rate, and a USD row converts at exactly 1.0;
- `assert_converted_value_matches_rate.sql` — a converted CAD row's `value_usd` is `value / fx_rate` within floating-point tolerance of the rate it records;
- `assert_fx_observation_not_after_period_end.sql` — no row uses an observation dated after its period end.

The model takes a test-only `inject_future_fx` variable that appends an observation dated 2099-12-31 with a rate of 999. A build with it must pass the same tests and produce the same conversion columns, which proves the lateral join can never pick a future-dated rate.

**Python tests.** `tests/test_fundamentals_usd.py` builds a development database from the staging CSVs and hand-written facts rows and pins the arithmetic: a CAD value on a Friday period end uses that Friday's rate; a Sunday period end uses the preceding Friday's rate; a CAD value before 2017-01-03 is `no_rate`; a EUR value is `currency_out_of_scope`; a USD value is at 1.0; a CAD row whose rate was published after the filing is known at the rate. A Hypothesis property over generated observations and period ends asserts the chosen date is always the latest on or before the period end, and that a period end before every observation is never converted.

`tests/test_query_usd.py` carries the classic-bug tests: `test_a_cad_value_waits_for_the_rate_that_converts_it` (a USD read one second before the rate's known time returns nothing, while the reported read at the same instant succeeds) and `test_usd_asof_ts_is_never_earlier_than_the_acceptance_time` (the result's `asof_ts` is the later of the two inputs). The end-to-end module adds `test_no_usd_row_is_known_before_its_rate` over the whole table.

### USD reads: query, REST, explorer and CLI (decision A-41; issues #113, #133, #131)

- **Python.** `query.asof(identifier, field, asof_ts, period="FY", n=1, *, db=None, currency="USD")` and `query.history(..., currency="USD")` read `main.fundamentals_pit_usd`. Candidates are `converted` rows with `asof_ts <= asof_ts`; the per-period winner is chosen by `(asof_ts, version)`, so the reported ordering is unchanged. `AsOfResult.value` is `value_usd`, `currency` is `"USD"`, `unit` keeps the reported unit family with the currency replaced (`CAD` → `USD`, `CAD/shares` → `USD/shares`), and `acceptance_ts` stays the filing's own time so "when was this accepted" and "when could this be used" remain distinguishable. `AsOfResult.asof_ts` — the last field, `datetime | None` — carries the exact known time; a reported read leaves it `None`. `history(currency="USD")` returns converted rows only, so an unconverted period is absent rather than null. A v1.0.0 database raises `query.MissingTableError` naming the model.
- **REST.** `/asof` and `/history` accept `currency=USD` as a one-value `Literal`, so any other value is a 422 before the database is touched. Every `AsOfResponse` carries `asof_ts` as ISO-8601 UTC with `Z`, null for a reported read. `MissingTableError` answers 501, because the absent model is a property of the deployment, not the request. `/text` and the rest of the Phase 2 surface are described in [Epic 04](epic-04-8k-text.md).
- **Explorer.** A "Reported / USD" switch exists only when `main.fundamentals_pit_usd` is present. In USD mode the versions table and chart read `query.history(..., currency="USD")`, and a row is greyed out until its own `asof_ts`; a USD row without an `asof_ts` is treated as not known. A v1.0.0 database renders exactly as before.
- **CLI.** `edgarpit asof ... --currency USD` passes the currency through, prints `currency: USD` and `unit: USD`, and exits 1 with the query's message when the model is missing; any other currency is a usage error (exit 2). It reads `query.asof`, whose result carries the exact `asof_ts`; the REST response exposes the same field.

A fixture build answers the reference case:

```bash
uv run edgarpit asof --db ./build/edgarpit.duckdb --ticker RY --field revenue \
    --asof 2024-01-15T00:00:00Z --currency USD
```

```
value: 40464998918.60716
currency: USD
unit: USD
period_start: 2022-11-01
period_end: 2023-10-31
accession: 0001193125-23-285639
acceptance_ts: 2023-11-30T11:45:12Z
```

The row is 56,129,000,000 CAD divided by the 2023-10-31 observation of 1.3871; that rate was public at `2023-10-31T21:00:00Z`, well before the 40-F, so the converted value is known at the filing's acceptance time. The restating 40-F (`0001193125-24-270294`, accepted `2024-12-04T11:26:17Z`) converts 51,464,000,000 CAD to 37,101,867,204.95999 USD.

### QA (issue #116, PR #122)

`edgarpit qa` prints a Phase 2 section after the existing ones:

- **FX coverage** (`raw.fx_daily`): per series, first and last observation, row count, the largest gap in calendar and Monday–Friday business days, the newest `raw.filings.acceptance_ts` as the staleness reference, the business days since the last observation, and a stale flag. A series is stale when its last observation is more than five business days before that reference; an existing but empty table is stale when filings exist.
- **USD conversion** (`main.fundamentals_pit_usd`): row counts grouped by `(converted, unconverted_reason, currency)`, the issuers whose `no_rate` rows end on or after 2017-01-03, and every row where `asof_ts < acceptance_ts` or `asof_ts < fx_known_at` — a point-in-time key violation, with the first ten examples.
- **8-K text** (`raw.filings_text`): coverage, segmentation and duplicate hashes, described in [Epic 04](epic-04-8k-text.md).

The unique keys of `raw.filings_text` and `raw.fx_daily` join the existing key checks. A key violation or an `asof_ts` leak makes `QaReport.ok` false, so the CLI exits 1; an incomplete text fetch is reported but not fatal. `run_qa` reads `unconverted_reason` defensively, so a database whose model predates decision A-38 reports the status as `unknown` instead of failing.

On the fixture build:

```
FX daily (raw.fx_daily)
  FXUSDCAD: 2449 rows, 2017-01-03..2026-10-27
  largest gap: 5 days calendar, 3 days business
  stale: no
USD conversion (main.fundamentals_pit_usd)
  converted CAD: 902
  converted USD: 2293
  unconverted currency_out_of_scope -: 65
No-rate rows after 2017-01-03 (must be zero while FX is current): 0
Asof_ts violations (asof_ts earlier than acceptance_ts or fx_known_at): 0
```

The 65 unconverted rows are unit `shares`, which carries no currency. The fixture cache has no CAD period before 2017-01-03, so the build has no `no_rate` row; the rule is exercised by the staging-CSV build in `tests/test_fundamentals_usd.py`.

### Export (issue #117, PR #121)

`edgarpit export` writes `fundamentals_pit_usd.parquet` from `main.fundamentals_pit_usd` and `fx_daily.parquet` from `raw.fx_daily` when those tables exist, with fixed row orders (`(cik, field, period_start, period_end, accession)` and `(series, rate_date)`); a v1.0.0 database still exports exactly its five Phase 1 files. Every manifest entry carries `bytes`, and `filings_text` also carries `text_bytes`. The fixture build exports **eight files**:

| file | rows |
|---|---|
| `fundamentals_pit.parquet` | 3,260 |
| `ratios_pit.parquet` | 791 |
| `events_8k.parquet` | 106 |
| `filings.parquet` | 431 |
| `facts_raw.parquet` | 4,877 |
| `fundamentals_pit_usd.parquet` | 3,260 |
| `filings_text.parquet` | 42 |
| `fx_daily.parquet` | 2,449 |

`test_phase1_files_are_byte_identical_with_and_without_phase2_tables` proves the five v1.0.0 files keep their exact bytes when the Phase 2 tables are added; that is the compatibility promise a consumer relies on.

## Key decisions and reasoning

- **17:00 Toronto, not 16:30.** The Bank says 16:30 with a 17:00 backstop for a rate that is late. 17:00 is never earlier than publication and needs no exception for a late day (decision A-32).
- **The rate is chosen by period end, not by publication order.** A value for a period should be priced at a rate that was current for that period; taking the newest observation in the series would price an old period with a later rate (decision A-32).
- **A converted row's known time is the later of the two inputs.** The amount cannot be used before its filing is public, and the USD number cannot be computed before the rate is public; `greatest` is the only safe join (decision A-32).
- **Unconverted rows are kept, not dropped.** Deleting a EUR row or a pre-2017 CAD row would hide a coverage gap; the row keeps its reported value, `converted = false` and a reason, and USD reads omit it (decision A-38).
- **No chaining of the pre-2017 noon series.** The series measure different things and the Bank does not restate history; a `no_rate` flag is more honest than a spliced number (decision A-39).
- **The exact known time travels on the result.** Deriving it from `period_end` in each consumer is an approximation that can be early or late; `AsOfResult.asof_ts` and the REST field make the exact instant part of the contract (decision A-41).
- **USD values do not change the ratios.** `ratios_pit` stays on reported values of one currency; a ratio built from differently converted inputs would mix rates and units for no gain. QA counts the gaps.
- **The series is fetched every run and stored only when it changes.** A documented endpoint with one whole-history response has no cheap change probe; comparing the body's digest is exact and keeps the cache free of duplicate files (decision A-37).

## Interfaces for future epics

- **`raw.fx_daily`** — `(series, rate_date, value, known_at, source)`, unique on `(series, rate_date)`; `known_at` is always 17:00 `America/Toronto` in UTC.
- **`main.fundamentals_pit_usd`** — every `fundamentals_pit` column in order plus `value_usd`, `fx_rate`, `fx_rate_date`, `fx_known_at`, `converted`, `asof_ts`, `unconverted_reason`; same key as `fundamentals_pit`.
- **`AsOfResult.asof_ts`** — last field, `datetime | None`; set for USD reads, `None` for reported reads.
- **REST** — `currency=USD` on `/asof` and `/history`; `asof_ts` on every `AsOfResponse`; 501 for a missing Phase 2 model.
- **CLI** — `asof --currency USD`; `fx --out DIR`.
- **`edgarpit.fx.valet`** — `valet_url`, `ValetClient`, `parse_valet`, `fetch_fx`, `FxFetchReport`.
- **Raw cache** — `fx` endpoint, global `_` slot, key = series id, empty `cik` in the manifest.
- **QA** — `FxCoverage`/`FxSeriesCoverage`, `UsdConversion` (including `asof_violation_count` and the no-rate issuer list).
- **Export** — `fundamentals_pit_usd.parquet`, `fx_daily.parquet`.

## Deviations from the plan

- **The pre-2017 noon series is stored but not used.** The fixture carries `IEXE0101` so a later decision about pre-2017 coverage does not need another live fetch; the pipeline ingests only `FXUSDCAD` (decision A-44).
- **The FX fixture started in `tests/fixtures/fx` and moved to the SEC cache.** Decision A-44 merged it once ingest existed, so the offline build exercises USD conversion; the evidence root keeps `IEXE0101`.
- **The first daily observation is 2017-01-03.** The Bank's publication page says daily rates are "not available from 1 January to 28 February 2017", but Valet carries observations from 2017-01-03 and their monthly arithmetic means reproduce the published January and February averages to four decimals. The dataset uses the series as served (decision A-39).
- **`unconverted_reason` was added to the column contract when the loader landed.** Issue #99 froze the model without it; issue #112 added the additive column, and the export and QA read it without assuming it exists (decision A-38).
- **The explorer first approximated the USD known time from the period end, then moved to the exact `asof_ts`.** The approximation was conservative but could grey a value out slightly too long; decision A-41 replaced it with the row's own instant (issue #133).
- **The CLI gained `--currency USD` in the Phase 2 CLI issue, not in the storage issue.** The model and query existed first; the command surface followed once the fixture merge made an end-to-end test possible (issue #131).

## Known limitations and deferred items

- **Only `USD` and `CAD` are converted.** Every other currency, and every unit with no currency (shares, pure), is `currency_out_of_scope` and absent from USD reads. The fixture build has 65 such rows.
- **FX before 2017-01-03 is not converted.** A CAD period ending earlier keeps `value_usd` null and `unconverted_reason='no_rate'`; the older noon series is not chained (decision A-39).
- **One rate per period end, not an average.** A fiscal year is converted at the observation for its last day; the model does not average daily rates over the period.
- **17:00 is conservative by up to half an hour.** A converted value can be known slightly later than the Bank's actual 16:30 publication; it is never earlier.
- **A missing observation falls back to the previous business day.** Weekends and Bank holidays use the latest earlier observation, which is the documented daily-average convention but can be several days old around a long closure.
- **Ratios are not converted.** `ratios_pit` remains a reported-currency table; there is no USD ratio model.
- **The FX series is one source.** There is no cross-check against another provider and no correction for a Bank revision; a re-fetched series adds a new cache version and the next build uses the latest body.
- **No intraday or real-time conversion.** The dataset is daily and point-in-time, not a market-data feed.
- **Live coverage depends on the live run.** The fixture build proves the rules; the number of converted and unconverted rows for 250 issuers is reported in [`docs/epic-06-universe-incremental.md`](epic-06-universe-incremental.md).
