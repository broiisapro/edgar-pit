# edgar-pit v1.1.0 — 8-K text, USD values and a living dataset

This release closes Phase 2. The dataset still follows one rule — a value is known at time `t` only if the filing that carried it was accepted at or before `t` — and now answers two more questions under it: **what did the 8-K say?** and **what is this value in US dollars?**

The full live-run numbers for the 250-issuer universe are recorded in [`docs/epic-06-universe-incremental.md`](epic-06-universe-incremental.md); this note describes what the release contains and how to use it.

## What this release adds

### 8-K text

Every 8-K/8-K/A selected by an item can now be read as clean, item-labelled text:

- the **primary** 8-K document and every **`EX-99.1`** exhibit are fetched from the filing's `-index-headers.html` page;
- HTML is converted to plain text — scripts, styles, hidden XBRL and page furniture removed, tables flattened to one tab-separated line per row, entities resolved, whitespace normalised;
- the primary document is split on its own `Item N.NN` headings, but only when those headings match the filing's declared items exactly once and in order; otherwise the whole document is stored as one `unsegmented` section instead of a guess;
- an `EX-99.1` exhibit is never segmented: it becomes one `unsegmented` row;
- every row is known at its filing's `acceptance_ts`, never at a date printed inside the document.

Item 2.02 text is the join of the text table to the event table on the accession, so a researcher gets the whole filing — primary document and press-release exhibit — for every results announcement.

### USD values

`main.fundamentals_pit_usd` converts every reported value to US dollars with the Bank of Canada `FXUSDCAD` daily rate:

- `USD` values are used as is at a rate of `1.0`;
- `CAD` values use the latest observation dated on or before the period end;
- a rate for date `d` is treated as known at `d` 17:00 America/Toronto, so a converted value cannot appear before the rate that produced it was published;
- a converted row's `asof_ts` is `max(acceptance_ts, fx_known_at)`, and that exact instant travels on the Python result, the REST response and the explorer; a backtest compares it with its clock instead of approximating it from the period end;
- every unconverted row keeps its reported value and a reason: `no_rate` for a CAD period before 2017-01-03, `currency_out_of_scope` for any other currency. USD reads omit those rows rather than returning null.

### A larger, incremental universe and a schedule

- `config/universe_large.csv` holds about 250 issuers — roughly 125 US large caps and 125 Canadian SEC filers — built from SEC data and kept alongside the original 40-issuer `config/universe.csv`.
- `edgarpit fetch --incremental` makes one submissions request per issuer and skips the ones whose newest accession and filing count are unchanged, reporting `skipped N unchanged`.
- Two GitHub Actions workflows keep the data current: a nightly incremental refresh and a weekly release that re-reads the whole universe and publishes an immutable `data-YYYY-MM-DD` GitHub Release. Both are inert until the repository owner sets `REFRESH_ENABLED=true` and the `SEC_USER_AGENT` secret; see [`docs/operations.md`](operations.md).

## New tables and files

**Raw staging (`raw` schema):**

| table | one row per | key |
|---|---|---|
| `raw.filings_text` | 8-K document section | `(accession, document, section, section_order)` |
| `raw.fx_daily` | FX series observation | `(series, rate_date)` |

**Queryable models (`main` schema):**

| model | contents |
|---|---|
| `main.fundamentals_pit_usd` | every `fundamentals_pit` row plus `value_usd`, `fx_rate`, `fx_rate_date`, `fx_known_at`, `converted`, `asof_ts`, `unconverted_reason` |
| `main.filings_text` | `raw.filings_text` plus `ticker` |

**Release files.** A v1.1.0 database exports **eight** Parquet files plus the manifest:

```
fundamentals_pit.parquet      fundamentals_pit_usd.parquet
ratios_pit.parquet            filings_text.parquet
events_8k.parquet             fx_daily.parquet
filings.parquet
facts_raw.parquet
```

The manifest keeps `build_timestamp`, `database` and `sec_fetch_range`, and every entry now also carries `bytes`; the `filings_text` entry carries `text_bytes`, the uncompressed UTF-8 size of the section text. The release also attaches `edgarpit.duckdb` (the complete database the explorer reads), `build-info.json` and `SHA256SUMS`.

**Read surface additions:** `query.filing_text(...)`, `currency="USD"` on `query.asof` and `query.history`, `AsOfResult.asof_ts`, `GET /text`, `currency=USD` on `/asof` and `/history`, `asof_ts` on `/asof` and `/history` results, a "Reported / USD" switch and an 8-K text panel in the explorer, and the CLI commands `fetch-text`, `fx` and `asof --currency USD`.

## Compatibility

- **v1.0.0 databases still read.** Reported `asof`, `history`, `events`, `issuers` and `fields` behave exactly as before. A USD read or a text read against a database that predates the Phase 2 models raises `query.MissingTableError` naming the missing table; the REST service answers **501** with that message, and the explorer renders the v1.0.0 page unchanged. A v1.0.0 database exports the same five Parquet files as before.
- **Phase 1 outputs are unchanged.** The five v1.0.0 Parquet files are byte-identical with and without the Phase 2 tables in the same database, and the Phase 1 test pins their SHA-256. A release consumer that pinned those files sees no change.
- **Additive interfaces.** `AsOfResult` gains `asof_ts` as its last field with a default of `None`; a reported read leaves it null. `export` adds `bytes` to every manifest entry. The new CLI flags and REST parameters are optional. Existing commands and signatures keep their behaviour.
- **Additive raw cache.** The new `filing_index`, `documents` and `fx` endpoints sit beside the existing ones. A Phase 1 raw cache still builds: the text and FX tables are created empty when the cache holds no such endpoint, and the Phase 1 models are unchanged.
- **Python and dbt versions.** The code targets Python 3.12 with the committed `uv.lock`; dbt-core 1.12 and dbt-duckdb 1.11 remain pinned. No new service or credential is required for an offline build.

## How to get the data

**Offline, from the bundled fixtures** — no network, no SEC account:

```bash
git clone https://github.com/broiisapro/edgar-pit.git
cd edgar-pit
uv sync --frozen
uv run edgarpit build --offline --fixtures tests/fixtures/sec --out ./build
uv run edgarpit qa --db ./build/edgarpit.duckdb
uv run edgarpit asof --db ./build/edgarpit.duckdb --ticker RY --field revenue \
    --asof 2024-01-15T00:00:00Z --currency USD
uv run edgarpit export --db ./build/edgarpit.duckdb --out ./release
```

The fixture cache includes 8-K text and the Bank of Canada series, so the offline build exercises both Phase 2 paths.

**Live, from the SEC and the Bank of Canada.** Set a declared contact first; the SEC requires one of the form `Name email` and the client refuses to send a request without it:

```bash
cp .env.example .env    # set SEC_USER_AGENT="Your Name you@example.com"
uv run edgarpit fetch --universe config/universe_large.csv --out ./data
uv run edgarpit build --data ./data --out ./build
uv run edgarpit fetch-text --db ./build/edgarpit.duckdb --items 2.02 --data ./data
uv run edgarpit fx --out ./data
uv run edgarpit build --data ./data --out ./build
uv run edgarpit qa --db ./build/edgarpit.duckdb
uv run edgarpit export --db ./build/edgarpit.duckdb --out ./release
```

The first live fetch is a one-time backfill; later fetches can use `--incremental`.

**From a published release.** Each weekly `data-YYYY-MM-DD` release carries `edgarpit.duckdb`, the eight Parquet files, `manifest.json`, `build-info.json` and `SHA256SUMS`. Verify a download with `sha256sum -c SHA256SUMS` in the directory holding the files, then query a Parquet file with any DuckDB client:

```sql
SELECT period_end, value_usd, fx_rate, fx_rate_date, asof_ts
FROM read_parquet('fundamentals_pit_usd.parquet')
WHERE ticker = 'RY' AND field = 'revenue'
ORDER BY period_end DESC
LIMIT 5;
```

The hosted explorer at [edgar-pit.streamlit.app](https://edgar-pit.streamlit.app) reads the same database; it can also be pointed at a local file with `EDGARPIT_DB`.

## Known limits

- **8-K text starts in 2014.** EDGAR's `-index-headers.html` pages do not exist for older filings; an older 8-K has items but no text.
- **Only the primary document and `EX-99.1` are stored.** Other exhibits (`EX-99.2`, bare `EX-99`), the full submission and the XBRL instance are out of scope.
- **Segmentation is conservative.** A document whose headings do not match its declared items is one `unsegmented` section; the text is complete, the item attribution is absent.
- **FX is USD/CAD only and starts 2017-01-03.** Other currencies keep `unconverted_reason='currency_out_of_scope'`; an earlier CAD period keeps `no_rate`. The older noon series is not chained.
- **One rate per period end.** A fiscal year is converted at the observation for its last day, not at a period average.
- **The universe is SEC-only.** A company listed only on the TSX files nothing with the SEC and is outside the dataset, however large it is.
- **No derived Q4.** XBRL reports annual and year-to-date periods; YTD periods are kept as `OTHER` and excluded from `asof`.
- **No LLM extraction.** Forward guidance and other narrative fields are Phase 3; this release ships the text, not structured extractions from it.
- **The API and explorer remain single-process development services.** The API has no authentication, pagination or rate limiting; run it locally or keep it behind your own gateway.
- **Scheduled refresh is opt-in.** The two workflows publish nothing until the owner sets `REFRESH_ENABLED` and `SEC_USER_AGENT`; the first enabled run is a cold backfill that may need several dispatches.

## Data, licence and credit

Fundamentals and events are public records from SEC EDGAR, redistributed under the SEC's fair-access terms. Exchange rates are published by the Bank of Canada. The code in this repository is MIT licensed. Neither the SEC nor the Bank of Canada endorses this dataset.

## Read more

- [`docs/epic-04-8k-text.md`](epic-04-8k-text.md) — how the documents are found, cleaned, segmented and ingested.
- [`docs/epic-05-fx-normalization.md`](epic-05-fx-normalization.md) — the Valet research, the conversion rule and the USD read surface.
- [`docs/epic-06-universe-incremental.md`](epic-06-universe-incremental.md) — the live 250-issuer run and its measured numbers.
- [`docs/epic-07-scheduled-refresh.md`](epic-07-scheduled-refresh.md) — the nightly refresh and weekly release.
- [`docs/architecture.md`](architecture.md) — the data model, column contracts and query semantics.
- [`docs/operations.md`](operations.md) — enabling, running and debugging the scheduled workflows.
