# edgar-pit

edgar-pit builds a research-grade, point-in-time fundamentals dataset from the SEC's free APIs. Every value is keyed by the moment its filing was accepted, every restatement is kept as a new version, every 8-K item is an event with an exact timestamp, and the text of every 8-K is stored section by section — so `asof('RY', 'revenue', '2024-01-15')` returns what an investor could actually have known that day, in the currency the filing reported or in US dollars at the Bank of Canada rate that was published by then. It covers US GAAP and IFRS filers, including Canadian companies cross-listed in the US.

![Royal Bank of Canada annual revenue by fiscal year, with the FY2023 restatement highlighted](docs/img/ry-revenue-restatement.svg)

The 51.464 B CAD figure was not knowable before 2024-12-04T11:26:17Z: it appears only in the 40-F that the SEC accepted at that second. A query dated one second earlier returns the 56.129 B CAD that the 2023 40-F reported, and the database keeps both versions with their accession numbers. Converted to USD, the same value waits for both its filing and the FX rate that converted it.

## Why point-in-time

- **Period end** (`2023-10-31`) says when the fiscal year ended, not when anyone could see its numbers.
- **Filing date** (`2023-11-30`) is the day EDGAR assigned the filing; anything accepted after the cutoff is dated the next day.
- **Acceptance time** (`2023-11-30T11:45:12Z`) is the second EDGAR accepted the filing — the earliest moment its numbers were public.
- A backtest that selects on period end or filing date can use a number hours or days before it existed; `edgar-pit` compares `acceptance_ts` and nothing else. A USD value compares the row's `asof_ts`, which is never earlier than either its filing or its FX rate.
- `period_end`, `filed`, `filing_date`, `fy` and `fp` never decide what was known, in the pipeline or in any query.

SEC's submissions JSON currently mis-states `acceptanceDateTime` for many filings, and the error runs in both directions, so trusting it can be lookahead. This project reads each filing's SGML header `<ACCEPTANCE-DATETIME>` instead and keeps the API value only for QA. A filing with no header gets a conservative upper bound (`filing_date` 23:59:59 New York, raised to at least the API value plus six hours) flagged `ts_imputed`.

## Quick start (offline, no SEC credentials)

Everything below runs from a fresh clone against the bundled fixtures, with no network:

```bash
uv sync --frozen
uv run edgarpit build --offline --fixtures tests/fixtures/sec --out /tmp/ep
uv run edgarpit asof --db /tmp/ep/edgarpit.duckdb --ticker RY --field revenue --asof 2024-01-15T00:00:00Z --json
```

```json
[{"cik": "0001000275", "ticker": "RY", "field": "revenue", "asof": "2024-01-15T00:00:00Z", "period": "FY", "value": 56129000000.0, "unit": "CAD", "currency": "CAD", "period_start": "2022-11-01", "period_end": "2023-10-31", "period_type": "FY", "accession": "0001193125-23-285639", "form": "40-F", "acceptance_ts": "2023-11-30T11:45:12Z", "version": 1, "is_restatement": false, "restatement_kind": "none", "ts_imputed": false}]
```

The same database answers in USD, and the bundled fixtures include the Bank of Canada series and 8-K text so the Phase 2 paths work offline too:

```bash
uv run edgarpit asof --db /tmp/ep/edgarpit.duckdb --ticker RY --field revenue --asof 2024-01-15T00:00:00Z --currency USD
```

```text
value: 40464998918.60716
currency: USD
unit: USD
period_start: 2022-11-01
period_end: 2023-10-31
accession: 0001193125-23-285639
acceptance_ts: 2023-11-30T11:45:12Z
```

![Terminal recording: edgarpit asof for RY revenue one second before and at the restating 40-F's acceptance time](docs/img/asof-demo.gif)

## 8-K text

Every 8-K/8-K/A selected by an item has its documents in the database, split into
readable sections:

- the **primary** 8-K document and every **`EX-99.1`** exhibit are fetched from the
  filing's `-index-headers.html` page;
- HTML is converted to plain text with BeautifulSoup and lxml — scripts, styles and
  hidden XBRL are removed, tables become one tab-separated line per row, entities
  are resolved and whitespace is normalised;
- the primary document is split on `Item N.NN` headings, but only when the headings
  match the filing's own item list exactly once and in order; otherwise the whole
  document is stored as a single `unsegmented` row instead of a guess;
- an `EX-99.1` exhibit is never segmented: it is one `unsegmented` row.

Each row carries `document`, `doc_type` (`primary` or `EX-99.1`), `section`
(`2.02`, `9.01`, … or `unsegmented`), `section_order`, the cleaned `text`,
`sha256` of that text and `char_count`. The row's knowledge time is the filing's
`acceptance_ts`, never a date inside the text.

Item 2.02 text is the join of the text to the event on the accession. In Python:

```python
from edgarpit.query import filing_text

rows = filing_text("AAPL", item="2.02")
for row in rows[:3]:
    print(row.acceptance_ts, row.document, row.section, row.char_count)
```

```text
2022-01-27 21:30:36+00:00 a8-kex991q1202212252021.htm unsegmented 13071
2022-01-27 21:30:36+00:00 aapl-20220127.htm 2.02 3144
2022-01-27 21:30:36+00:00 aapl-20220127.htm 9.01 534
```

or in SQL, over the same database:

```bash
duckdb /tmp/ep/edgarpit.duckdb -c "SELECT f.accession, f.document, f.section, f.section_order, f.char_count FROM main.filings_text f WHERE f.ticker = 'AAPL' AND f.accession = '0000320193-22-000006' ORDER BY f.document, f.section_order;"
```

```text
┌──────────────────────┬─────────────────────────────┬─────────────┬───────────────┬────────────┐
│      accession       │          document           │   section   │ section_order │ char_count │
│       varchar        │           varchar           │   varchar   │     int32     │   int32    │
├──────────────────────┼─────────────────────────────┼─────────────┼───────────────┼────────────┤
│ 0000320193-22-000006 │ a8-kex991q1202212252021.htm │ unsegmented │             1 │      13071 │
│ 0000320193-22-000006 │ aapl-20220127.htm           │ 2.02        │             1 │       3144 │
│ 0000320193-22-000006 │ aapl-20220127.htm           │ 9.01        │             2 │        534 │
└──────────────────────┴─────────────────────────────┴─────────────┴───────────────┴────────────┘
```

The REST service exposes the same rows at `GET /text?ticker=AAPL&item=2.02`, and
the explorer lists each Item 2.02 filing with its sections and exhibits.

## USD values

`main.fundamentals_pit_usd` converts every reported value to US dollars when the
currency is `USD` or `CAD`; other currencies are excluded and counted, never
silently guessed:

- `USD` values are used as is at a rate of `1.0`; they are known at their
  filing's `acceptance_ts`.
- `CAD` values use the latest **Bank of Canada** `FXUSDCAD` observation (Canadian
  dollars per US dollar) on or before the period end. The Bank publishes the
  daily rate around 16:30 America/Toronto; the dataset conservatively treats a
  rate for date `d` as known at `d` 17:00 America/Toronto.
- A converted row's `asof_ts` is `max(acceptance_ts, fx_known_at)`: the value is
  returned only when both its filing and the rate that produced the USD amount
  exist. The result carries that exact `asof_ts`, so a backtest compares it with
  its clock instead of approximating it from the period end.
- `unconverted_reason` is `no_rate` when the period ends before the first
  observation (2017-01-03) and `currency_out_of_scope` for any other currency.
  Such rows are absent from a USD answer rather than returned as null.
- Ratios always use the reported values of one currency; mixing currencies is
  rejected (QA counts the gaps).

The REST service takes `currency=USD` on `/asof` and `/history`, and every
`AsOfResponse` carries `asof_ts` (null for a reported read). The explorer has a
"Reported / USD" switch that greys out a USD value until its own `asof_ts`.

## Live data

Set a declared User-Agent first; SEC requires one of the form "Name email" and the client refuses to make a request without it. Copy `.env.example` to `.env` and set `SEC_USER_AGENT="Your Name you@example.com"`, then:

```bash
uv run edgarpit fetch --universe config/universe.csv --out ./data
uv run edgarpit build --data ./data --out ./build
uv run edgarpit fetch-text --db ./build/edgarpit.duckdb --items 2.02 --data ./data
uv run edgarpit fx --out ./data
uv run edgarpit build --data ./data --out ./build
uv run edgarpit qa --db ./build/edgarpit.duckdb
uv run edgarpit export --db ./build/edgarpit.duckdb --out ./release
```

`fetch-text` only fetches: it reads the 8-Ks carrying the requested items from
`--db` only to choose accessions, caches their index pages and documents, and
stops before the build. `fx` fetches the Bank of Canada series into the cache
and writes nothing when the series is unchanged. `build` then ingests both from
the cache, so the database is a pure function of the cache.

Measured on the full Phase 1 universe (October 2026, one Apple silicon laptop): `fetch` downloaded 40 submissions documents, 188 paginated pages, 40 companyfacts documents and 11,647 SGML headers in **11,915 requests over 2,566.9 s (≈43 minutes)** at an average **4.64 requests/s**; a second `fetch` over the same cache made 268 requests in 85.6 s and downloaded **zero** headers, because headers are immutable. `build` took 20.3 s and produced a **30.5 MiB** DuckDB file with 429,703 filings, 17,056 8-K item rows and 821,605 raw facts, which dbt turned into **86,782 `fundamentals_pit` rows** and 18,057 `ratios_pit` rows. `qa` exited 0 (`Key uniqueness: OK`), and `export` wrote the five Parquet files plus `manifest.json`. 581 of the 11,647 headers predate XBRL and carry no acceptance time; those filings use the conservative fallback above, and their 8-K items are still read.

The client respects SEC fair access: one request at a time through a token bucket capped at 5 requests/s, exponential backoff with jitter on 429/5xx, and a User-Agent on every request. Raw responses are cached immutably under `data/raw/` with a SHA-256 manifest, so a re-fetch adds files instead of overwriting them and an interrupted run resumes warm.

### Incremental fetch and the schedules

`fetch --incremental` asks each issuer for one submissions document and compares
its newest accession and filing count with the cached copy. An unchanged issuer
costs that one request; its pages, company facts and headers are skipped and the
run prints `skipped N unchanged`. A changed issuer is fetched in full, so the
run is always safe to repeat (decision A-34).

Two GitHub Actions workflows use the commands above; both are documented in
[`docs/operations.md`](docs/operations.md):

- **Nightly refresh** (`refresh.yml`): incremental fetch, build, fetch-text for
  Item 2.02, FX refresh, rebuild, QA; it always saves the raw cache, so a cold
  backfill resumes where a timeout stopped.
- **Weekly release** (`release.yml`): a full, non-incremental fetch first (so
  companyfacts-only corrections are picked up at least weekly), then a build,
  QA, export and a `data-YYYY-MM-DD` GitHub Release. Releases are immutable: a
  same-day re-run fails rather than replacing one.

## The large universe

`config/universe_large.csv` holds the Phase 2 universe: about **250 issuers**,
roughly **125 US** and **125 Canadian**. It is built only from SEC and Bank of
Canada data by `tools/build_universe_large.py`:

```bash
uv run python tools/build_universe_large.py --cache ./data --out config/universe_large.csv
```

- US issuers are ranked by the latest `dei:EntityPublicFloat` in the XBRL frames
  API, with the power-of-ten scale errors some filers introduced in 2022
  corrected.
- Canadian issuers are those whose submissions business or mailing address
  carries a Canadian province code (`A0`–`A9`, `B0`–`B2`, `Z4`) or a Canadian
  place name, ranked by their latest annual total assets in USD from
  `us-gaap:Assets` or `ifrs-full:Assets` and the `FXUSDCAD` rate. A frame `loc`
  never decides nationality.
- Every issuer of the 40-name Phase 1 universe stays in. Funds and blank-check
  companies are excluded, a second share class of the same CIK adds no row, and
  each issuer needs a 10-K, 40-F or 20-F within two years.

The file is a static list for a fetch run — frames carry no acceptance time, so
nothing in it decides what was known when.

## Interfaces

### Python

```python
from edgarpit.query import asof

r = asof("RY", "revenue", "2024-01-15T00:00:00Z")[0]
print(r.value, r.unit, r.period_end, r.accession, r.acceptance_ts)
```

```text
56129000000.0 CAD 2023-10-31 0001193125-23-285639 2023-11-30 11:45:12+00:00
```

`asof(identifier, field, asof_ts, period="FY", n=1, *, db=None, currency=None)` takes a ticker or a CIK and returns the latest version of each of the `n` most recent periods accepted at or before `asof_ts`; every result carries the accession, form and acceptance time it came from. With `currency="USD"` it reads the converted model, `value` is in USD and the result's `asof_ts` is the instant the converted value became known. `history(identifier, field, period=None, *, db=None, currency=None)` returns every version of every period, `events(identifier, item=None, start=None, end=None)` returns 8-K items, `filing_text(identifier, *, item=None, start=None, end=None)` returns 8-K sections, and `issuers()`/`fields(identifier)` list what a database holds. Timestamps must carry a timezone (`Z` or an offset); naive input is rejected.

### DuckDB SQL

```bash
duckdb /tmp/ep/edgarpit.duckdb -c "SET TimeZone='UTC'; SELECT period_end, version, value, accession, acceptance_ts, restatement_kind FROM main.fundamentals_pit WHERE ticker = 'RY' AND field = 'revenue' AND period_type = 'FY' ORDER BY period_end DESC, version LIMIT 5;"
```

```text
┌────────────┬─────────┬───────────────┬──────────────────────┬──────────────────────────┬──────────────────┐
│ period_end │ version │     value     │      accession       │      acceptance_ts       │ restatement_kind │
│    date    │  int64  │    double     │       varchar        │ timestamp with time zone │     varchar      │
├────────────┼─────────┼───────────────┼──────────────────────┼──────────────────────────┼──────────────────┤
│ 2025-10-31 │       1 │ 66605000000.0 │ 0001193125-25-305927 │ 2025-12-03 11:28:41+00   │ none             │
│ 2024-10-31 │       1 │ 57344000000.0 │ 0001193125-24-270294 │ 2024-12-04 11:26:17+00   │ none             │
│ 2024-10-31 │       2 │ 57344000000.0 │ 0001193125-25-305927 │ 2025-12-03 11:28:41+00   │ none             │
│ 2023-10-31 │       1 │ 56129000000.0 │ 0001193125-23-285639 │ 2023-11-30 11:45:12+00   │ none             │
│ 2023-10-31 │       2 │ 51464000000.0 │ 0001193125-24-270294 │ 2024-12-04 11:26:17+00   │ revision         │
└────────────┴─────────┴───────────────┴──────────────────────┴──────────────────────────┴──────────────────┘
```

The USD model has the same rows plus `value_usd`, `fx_rate`, `fx_rate_date`, `fx_known_at`, `converted`, `asof_ts` and `unconverted_reason`:

```bash
duckdb /tmp/ep/edgarpit.duckdb -c "SET TimeZone='UTC'; SELECT period_end, version, round(value, 2) AS value, round(value_usd, 2) AS value_usd, fx_rate, asof_ts, unconverted_reason FROM main.fundamentals_pit_usd WHERE ticker = 'RY' AND field = 'revenue' AND period_type = 'FY' ORDER BY period_end DESC, version LIMIT 3;"
```

```text
┌────────────┬─────────┬───────────────┬────────────────┬─────────┬──────────────────────────┬────────────────────┐
│ period_end │ version │     value     │   value_usd    │ fx_rate │         asof_ts          │ unconverted_reason │
│    date    │  int64  │    double     │     double     │ double  │ timestamp with time zone │      varchar       │
├────────────┼─────────┼───────────────┼────────────────┼─────────┼──────────────────────────┼────────────────────┤
│ 2025-10-31 │       1 │ 66605000000.0 │ 47513910686.26 │  1.4018 │ 2025-12-03 11:28:41+00   │ NULL               │
│ 2024-10-31 │       1 │ 57344000000.0 │ 41207243460.76 │  1.3916 │ 2024-12-04 11:26:17+00   │ NULL               │
│ 2024-10-31 │       2 │ 57344000000.0 │ 41207243460.76 │  1.3916 │ 2025-12-03 11:28:41+00   │ NULL               │
└────────────┴─────────┴───────────────┴────────────────┴─────────┴──────────────────────────┴────────────────────┘
```

Use `asof_ts <= t` (not `acceptance_ts`) whenever you read `value_usd`: a CAD
value whose rate was published later is not known earlier than that rate.

### REST

```bash
EDGARPIT_DB=/tmp/ep/edgarpit.duckdb uv run uvicorn api.main:app --port 8000
curl -fsS "localhost:8000/asof?ticker=RY&field=revenue&asof=2024-01-15T00:00:00Z&period=FY"
```

```json
[{"cik":"0001000275","ticker":"RY","field":"revenue","value":56129000000.0,"unit":"CAD","currency":"CAD","period_start":"2022-11-01","period_end":"2023-10-31","period_type":"FY","accession":"0001193125-23-285639","form":"40-F","acceptance_ts":"2023-11-30T11:45:12Z","version":1,"is_restatement":false,"ts_imputed":false,"restatement_kind":"none","asof_ts":null}]
```

With `currency=USD` the same endpoint returns the converted value, `"currency":"USD"`, and its exact `asof_ts`:

```bash
curl -fsS "localhost:8000/asof?ticker=RY&field=revenue&asof=2024-01-15T00:00:00Z&currency=USD"
```

```json
[{"cik":"0001000275","ticker":"RY","field":"revenue","value":40464998918.60716,"unit":"USD","currency":"USD","period_start":"2022-11-01","period_end":"2023-10-31","period_type":"FY","accession":"0001193125-23-285639","form":"40-F","acceptance_ts":"2023-11-30T11:45:12Z","version":1,"is_restatement":false,"ts_imputed":false,"restatement_kind":"none","asof_ts":"2023-11-30T11:45:12Z"}]
```

`GET /asof`, `GET /history`, `GET /events` and `GET /text` mirror the Python functions, and the OpenAPI schema is at `http://localhost:8000/docs`.

### Explorer

```bash
EDGARPIT_DB=/tmp/ep/edgarpit.duckdb uv run streamlit run explorer/app.py
```

Pick an issuer and a field to see every version on a timeline, move the as-of slider and watch later versions grey out, switch between reported and USD values, browse 8-K items with links to the filing index pages, and read the sections of each Item 2.02 filing. A hosted copy of the same app runs at [edgar-pit.streamlit.app](https://edgar-pit.streamlit.app).

### Parquet release

```bash
uv run edgarpit export --db /tmp/ep/edgarpit.duckdb --out /tmp/ep/release
```

A Phase 2 database exports eight Parquet files plus `manifest.json`:

| Asset | One row per | Key |
|---|---|---|
| `fundamentals_pit.parquet` | (issuer, field, period, filing) | `cik, field, period_start, period_end, accession` |
| `fundamentals_pit_usd.parquet` | the same, converted | the same |
| `ratios_pit.parquet` | (issuer, ratio, period, `asof_ts`) | `cik, ratio, period_start, period_end, asof_ts` |
| `events_8k.parquet` | (filing, 8-K item) | `cik, accession, item_code` |
| `filings.parquet` | filing | `cik, accession` |
| `filings_text.parquet` | (document, section) of an 8-K | `accession, document, section_order, section` |
| `fx_daily.parquet` | (series, rate date) | `series, rate_date` |
| `facts_raw.parquet` | companyfacts entry | `cik, taxonomy, concept, unit, period_start, period_end, accession` |

`manifest.json` records each file's row count, **`bytes`** (the Parquet file
size) and SHA-256, plus the SEC fetch range and build time. The `filings_text`
entry also records **`text_bytes`**, the uncompressed UTF-8 size of the section
text, because text rows are the only ones whose size a reader cannot estimate
from the row count. A 1.0.0 database has no Phase 2 models and exports exactly
the five Phase 1 files. The Phase 1 release is attached at
[v1.0.0](https://github.com/broiisapro/edgar-pit/releases/tag/v1.0.0). The same
release carries `edgarpit.duckdb`, the full database: point the explorer at it
with `EDGARPIT_DB=/path/to/edgarpit.duckdb`, or pass `db="/path/to/edgarpit.duckdb"`
to `asof`.

## Data model

| Table | One row per | Key | Notes |
|---|---|---|---|
| `filings` | filing | `accession` | form, filing date, report date, `acceptance_ts`, `acceptance_ts_source`, `ts_imputed`, 8-K items, amendment flag |
| `events_8k` | (filing, 8-K item) | `(accession, item_code)` | item code and label, acceptance time |
| `facts_raw` | companyfacts entry | `(cik, taxonomy, concept, unit, period_start, period_end, accession)` | as reported, before concept mapping |
| `fundamentals_pit` | (issuer, field, period, filing) | `(cik, field, period_start, period_end, accession)` | canonical field, value, unit, currency, source concept, `version`, `is_restatement`, `restatement_kind` |
| `fundamentals_pit_usd` | the same row converted | the same | `value_usd`, `fx_rate`, `fx_rate_date`, `fx_known_at`, `converted`, `asof_ts`, `unconverted_reason` |
| `ratios_pit` | (issuer, ratio, period, `asof_ts`) | `(cik, ratio, period_start, period_end, asof_ts)` | ROE, leverage, net margin; inputs share currency and unit and were all accepted at or before `asof_ts` |
| `filings_text` | (8-K document, section) | `(accession, document, section, section_order)` | cleaned text, `sha256`, `char_count`, item section or `unsegmented` |
| `fx_daily` | (series, rate date) | `(series, rate_date)` | Bank of Canada observation and the instant it is known |

Full column contracts, the staging schema and the query semantics are in [`docs/architecture.md`](docs/architecture.md).

## Coverage and limitations

- **Universe:** the Phase 2 `config/universe_large.csv` has 250 issuers, about 125 US and 125 Canadian, a mix of 40-F, 20-F and 10-K filers resolved from SEC submissions data rather than hard-coded.
- **Fields:** 20 canonical fundamentals fields (revenue, gross profit, operating income, net income, income before tax, basic and diluted EPS, R&D, operating cash flow, capex, total assets, current assets, cash, inventory, accounts receivable, total liabilities, current liabilities, long-term debt, total equity, shares outstanding) mapped to `us-gaap` and `ifrs-full` concepts in [`config/concept_map.yaml`](config/concept_map.yaml), with per-taxonomy rank lists.
- **USD conversion:** only `USD` and `CAD` are in scope; other currencies keep `value_usd` null with `unconverted_reason='currency_out_of_scope'`. `FXUSDCAD` starts on 2017-01-03, so earlier periods are `no_rate`. Nothing is chained from the older noon series. There is no derived Q4: XBRL reports annual and year-to-date periods, and YTD periods are kept as `OTHER` and excluded from `asof`.
- **Restatements:** `is_restatement` is a change greater than 0.5 % from the first version. `restatement_kind` separates `split_adjustment` (a share-based value re-reported for a near-integer split factor within 0.5 %) from `revision`; the tolerance is a heuristic, so a split with an unusual factor or drifting per-share values is reported as a revision (Apple's FY2019 diluted EPS, 2.46 → 0.61, is a 4.03× change and classifies as a revision).
- **8-K text** starts with the 2014 `-index-headers.html` pages: an older 8-K has items but no text. Only the primary 8-K document and `EX-99.1` exhibits are stored; other exhibits and the full submission are not.
- **Universe:** a company listed only on the TSX files nothing with the SEC and is outside the dataset, however large it is.
- **8-K items before 2004-08-23** use an older item-numbering scheme; QA counts those events per issuer and their labels are unreliable.
- **Coverage gaps are reported, not hidden.** In the live build BNS has 10 of the 20 fields (banks tag no gross profit, operating income or inventory), and JPM's `long_term_debt` stops at FY2013 while its `cash` starts at FY2015 because the concepts it uses changed. `edgarpit qa` prints per-issuer coverage (fields × fiscal years), the largest unmapped concepts (4,901 in the live build), 7 unit/currency anomalies, and ratio gaps by reason (1,882 YTD periods excluded by design, 1,089 periods with a missing ratio input, 1 zero denominator). The Phase 2 section adds text coverage per issuer and the count of converted and unconverted USD rows by reason.

## How it is tested

- Classic point-in-time bugs have named tests that fail when the rule breaks: `tests/test_query_asof.py::test_restatement_accepted_after_asof_is_not_returned`, `tests/test_query_asof.py::test_period_end_before_asof_but_filed_after_is_excluded`, `tests/test_query_asof.py::test_property_no_row_after_asof` (Hypothesis, random as-of timestamps), `tests/test_query_usd.py::test_usd_asof_ts_is_never_earlier_than_the_acceptance_time`, `tests/test_ratios.py::test_mixed_currency_ratio_is_rejected`, `tests/test_companyfacts.py::test_ifrs_and_usgaap_map_to_same_canonical_field`, and the dbt test `transform/tests/assert_ratio_inputs_not_from_future.sql`.
- The end-to-end module drives the installed CLI through `subprocess` over the fixture cache and pins the RY restatement boundary, the `qa` exit codes, the API, the explorer, the 8-K sections and the USD values.
- CI runs `lint`, `types`, `tests` and `dbt` on every pull request. Tests never touch the network: HTTP is mocked with `respx`, and live checks are marked `network` and excluded. The suite is 890 tests; the two `network` tests are deselected by default.
- Two independent builds of the same fixtures produce byte-identical Parquet files and manifests that are equal after removing `build_timestamp`.

## Licence and data

MIT — see [LICENSE](LICENSE). Fundamentals and filing text come from SEC EDGAR, which publishes them as public records; FX rates come from the Bank of Canada Valet service. This project is not affiliated with or endorsed by either institution. Every request declares a User-Agent and stays at or below 5 requests per second.
