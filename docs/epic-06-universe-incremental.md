# Epic 06 — Universe expansion and incremental refresh

## Goal

Phase 2 built 8-K text, FX and USD values on fixtures and a 40-issuer universe. Epic 06 is the definition of done on real data: the 250-issuer universe fetched into the shared raw cache, built into one database, enriched with Item 2.02 text and Bank of Canada rates, rebuilt, checked and exported, with the nightly incremental path proved on the same cache.

The epic ends when:

- `config/universe_large.csv` (250 issuers) fetches, builds, enriches, rebuilds, passes `edgarpit qa` and exports, and the numbers are recorded here (#138);
- whatever the real filings break is fixed at its root with a test built from a minimal real excerpt, and every fix is disclosed (#138);
- `edgarpit fetch --incremental` skips the unchanged issuers on real data and reports the count and the request count (#138);
- the finished raw cache and the build (database, export, `build-info.json`, QA report) sit outside the tracked tree for the release that follows (#138).

This document records a single live run on 2026-10-29 against the shared cache in `./data`. The cache already held the 40-issuer Phase 1 fetch; everything here only added to it.

## How it works

### The 250-issuer universe (decisions A-33, A-36)

`config/universe_large.csv` is built by the committed tool from SEC sources: the top ~125 US issuers by latest `dei:EntityPublicFloat`, plus the top ~125 Canadian issuers by latest total assets in USD, where "Canadian" is the EDGAR business or mailing address state code (A0–A9, B0, Z4), not the frame `loc` and not the filer's exchange. Every issuer of `config/universe.csv` stays in, and fund/SIC exclusions and a two-year annual-report requirement apply. The file is frozen for this epic: an issuer that cannot be processed would be reported, not removed.

The fetched file resolves to 250 issuers, 125 in each country:

| Country | Filer type | Issuers | Mean fields per issuer | With FY revenue |
| --- | --- | ---: | ---: | ---: |
| US | 10-K | 125 | 17.5 | 125 |
| CA | 40-F | 89 | 16.1 | 86 |
| CA | 10-K | 31 | 18.2 | 30 |
| CA | 20-F | 5 | 17.6 | 5 |

The latest annual revenue is reported in USD for 194 issuers and in CAD for 50 (the rest are pre-revenue or report revenue only in older periods). All 250 issuers have at least 10 of the 20 canonical fields and FY rows; the minimum is 10 (Royal Bank, CIBC and BNS, banks with no inventory, gross profit or capex), the median 18, the maximum 20.

### `raw.universe` is the issuers the cache holds

`build_from_raw` used to load `config/universe.csv` wholesale. On a cache spanning the large universe that listed 250 issuers but tagged only the 40 the small file knows, leaving the others' tickers null and listing issuers with no rows. `load.reference_universe(cached_ciks)` now unions the project's universe files, keeps exactly the CIKs with a cached submissions document, refuses files that disagree about any of a CIK's four metadata columns, and `build_from_raw` loads that frame. Every row a build produces joins to a ticker, and `raw.universe` never lists an issuer the cache lacks.

### Incremental fetch (decisions A-34, A-42)

`data.sec.gov` sends no `ETag` or `Last-Modified`, so an issuer counts as unchanged when its freshly fetched submissions document has the same newest accession and the same filing count as the cached one. `fetch --incremental` then skips its pages, companyfacts and headers and costs one submissions request. The nightly refresh uses this; the weekly release deliberately runs a full fetch first so companyfacts-only corrections are picked up at least weekly.

### Item 2.02 text at 250 issuers (decisions A-30, A-31, A-37, A-40)

`fetch-text --items 2.02` selects the 8-K/8-K/A filings carrying Item 2.02 from the built database, then stores each filing's `-index-headers.html` page and every document it names in scope (the primary 8-K and each `EX-99.1`) under `raw/filing_index` and `raw/documents`. Nothing is written to the database: the next build ingests the cache into `raw.filings_text`, so the database stays a pure function of the cache. Only the primary document is segmented; each exhibit is one `unsegmented` row.

Three real-data cases shaped the text path:

- **The pre-2014 limit.** The `-index-headers.html` endpoint does not exist for older filings. On this universe 4,958 of the 12,302 Item 2.02 filings answer 404: 4,687 of the 4,688 before 2014 (one October 2012 filing does have a page) and the 271 filed in 2014 before June. Coverage is complete from June 2014 on — 276 of the 547 filings in 2014 and every one of the 7,067 from 2015 to 2026 have text. The fetch records those accessions as not found and continues; the build is unaffected. The 404 is not cached — the raw cache stores response bodies, not absences — so each run asks again for the accessions that have no page (see the known limits).
- **Two primary documents in one index.** McDonald's 8-K `0000063908-14-000056` lists `a8-kcoverexhibitlistingand.htm` as `FORM 8-K` and `form8k.pdf` as `PDF OF ENTIRE FORM 8-K`, both with `<TYPE> 8-K`. The strict one-primary rule stopped the whole fetch at that filing. When exactly one candidate has an HTML extension it is now the primary — the PDF is a rendering of the same filing and carries no extractable text — and two HTML candidates still raise instead of guessing.
- **PDF exhibits.** Seven of the 13,883 cached documents are PDFs, all `EX-99.1`. Converting their bytes as HTML produced garbage and, for three of them, a `RecursionError` that stopped the build. The file name now decides which documents reach the converter: `.htm`, `.html` and `.txt` only. A skipped document stays in the raw cache as fetched evidence and contributes no text row.

### FX and USD values (decisions A-32, A-38, A-39)

`edgarpit fx --out data` fetches the Bank of Canada `FXUSDCAD` series in one request and stores it under `raw/fx/_/FXUSDCAD/`. The build converts CAD fundamentals at the latest observation on or before the period end, treats a rate as known at `rate_date` 17:00 America/Toronto, and sets `asof_ts = greatest(acceptance_ts, fx_known_at)`, so a converted value is never visible before its rate was published. USD rows use rate 1.0; every other currency stays unconverted with `unconverted_reason`.

### The live run (2026-10-29)

Commands, in the order they ran, against `./data` and `./build-live`:

| Step | Result |
| --- | --- |
| `edgarpit fetch --universe config/universe_large.csv --out data` | 250 issuers ok, 0 failed; 49,537 requests; 11,087.8 s; 48,540 headers fetched, 11,647 cached from the Phase 1 cache, 1 header 404 (CRM `0001108524-21-000014`) |
| the same fetch again, warm cache | 250 ok, 0 failed; 1,015 requests; 228.0 s; 18 new headers, 60,187 cached, the same 404 |
| `edgarpit build --data data --out build-live` | 39.8 s; dbt `PASS=97 WARN=0 ERROR=0 SKIP=0`; peak resident memory 3.94 GB; database 123 MiB |
| `edgarpit fetch-text --db … --items 2.02 --data data` | 12,302 filings selected; 7,296 index pages and 13,802 documents fetched; 129 already cached; 4,958 indexes not found (no document 404); 26,056 requests; 1 h 44 min (14:16–16:00 UTC) |
| `edgarpit fx --out data` | `FXUSDCAD` 2,450 observations 2017-01-03..2026-10-28; stored |
| `edgarpit build --data data --out build-live` | 551.3 s (9 min 11 s, of which the text conversion is about 8.5 min); peak resident memory 5.65 GB; database 502 MiB |
| `edgarpit qa --db build-live/edgarpit.duckdb` | exit 0 in 5.0 s; report saved as `build-live/qa.txt` |
| `edgarpit export --db … --out build-live/release` | 8 Parquet files and `manifest.json` in 3.4 s |
| `edgarpit fetch --universe … --out data --incremental` | 250 ok, 0 failed; skipped 235 unchanged; 549 requests; 125.0 s; 3 new headers, 8,015 cached |

The raw cache after the run holds 84,510 manifest rows and 721 MB: `headers` 60,208, `documents` 13,883, `filing_index` 7,344, `submissions_page` 1,649, `submissions` 830, `companyfacts` 595, `fx` 1. Its fetched-at range is `2026-10-18T09:10:54Z` to `2026-10-29T16:28:47Z`. `python -m edgarpit.rawcache verify data` reports `manifest OK`.

Row counts of the built database:

| Table | Rows |
| --- | ---: |
| `raw.filings` | 1,123,949 |
| `raw.events_8k` | 94,860 |
| `raw.facts_raw` | 4,351,276 |
| `raw.filings_text` | 21,677 |
| `raw.fx_daily` | 2,450 |
| `raw.concept_map` | 61 |
| `raw.universe` | 250 |
| `main.fundamentals_pit` | 507,326 |
| `main.fundamentals_pit_usd` | 507,326 |
| `main.ratios_pit` | 105,150 |
| `main.events_8k` | 94,860 |
| `main.filings` | 1,123,949 |
| `main.filings_text` | 21,677 |

The export is 81 MB: `filings_text` 35 MB, `facts_raw` 18 MB, `filings` 17 MB, `fundamentals_pit_usd` 4.8 MB, `fundamentals_pit` 3.4 MB, `ratios_pit` 1.4 MB, `events_8k` 621 KB, `fx_daily` 27 KB. The manifest's `sec_fetch_range` is `20261018T091054968139Z` … `20261029T160147952422Z`.

### Coverage

- **The 20 canonical fields.** Every issuer has at least 10; the median is 18. Four issuers have no revenue: NexGen and Lithium Americas are pre-revenue developers, Skeena reports only interest income, and B2Gold tags revenue by commodity (`RevenueFromSaleOfGold`, `RevenueFromSaleOfSilver`, `RevenueFromInterest`) with no total. Gross profit reaches 111 issuers, R&D 95 (issuers that report it), inventory 172, accounts receivable 170.
- **History.** Median FY coverage is 14 years of revenue and 16–17 years of net income, total assets, equity and operating cash flow; the longest history is 74 annual periods. `main.fundamentals_pit` holds 507,326 versions over 218,653 distinct `(cik, field, period_start, period_end)` keys.
- **USD conversion.** 490,091 rows are converted (465,011 already USD, 25,080 CAD at a rate on or before the period end). 17,235 stay unconverted: 4,862 CAD with `no_rate` (all period ends before 2017-01-03, the first observation); and with `currency_out_of_scope`, 11,861 rows whose unit carries no currency (11,795 share counts and 66 EPS rows tagged `pure`, `shares/USD` or `segment`), 502 EUR and 10 AUD. No-rate rows after 2017-01-03: 0, and 0 `asof_ts` violations.
- **8-K text.** 7,344 of the 12,302 Item 2.02 filings have text, across 156 issuers: 15,145 primary-section rows and 6,532 exhibit rows, 254.8 M characters, mean 11,754 characters per section. 681 primary documents (9.3 %) stayed whole (`unsegmented`); the rest matched their filing's own item list. No zero-length section. 29 text hashes appear under more than one accession, which is expected for boilerplate press releases.

### QA

`edgarpit qa` exits 0. The report's notable items, and why each is acceptable:

- **Imputed acceptance times.** 1,066,370 filings take the A-23 fallback (`filing_date_eod`); 57,579 are header-sourced. Headers are fetched only for filings that carry a `us-gaap`/`ifrs-full` fact or are 8-K/8-K/A (decision A-1), so most ownership and prospectus filings are deliberately imputed. 2,626 header-required filings took the fallback because their cached header carries no `<ACCEPTANCE-DATETIME>` (pre-2001 filings) or, for CRM's 2021 filing, no header answered at all.
- **Header-vs-API disagreements: 0.** This is a finding, not a silent pass. In the Epic 02 run (2026-10-21) 8,410 of 11,647 header-sourced filings disagreed with the submissions API by more than 60 s, the reason decision A-1 distrusts the API. The cache fetched on 2026-10-28/29 shows none: Apple's 2026-07-30 8-K, the example A-1 records as four hours late in the API (`2026-07-31T00:30:28Z`), now reads `2026-07-30T20:30:28Z`, exactly its SGML header. `acceptance_ts` still comes from the header and the PIT rule is unchanged; the diagnostic simply has nothing to report on this fetch.
- **Restatements.** 24,821 restated versions of 507,326 (23,758 `revision`, 1,063 `split_adjustment`). A filing that repeats a changed value is a version whose value still differs from the first, so later comparatives carry `is_restatement = true`; that is decision A-7 applied literally.
- **Unmapped concepts.** 8,193 distinct `us-gaap`/`ifrs-full` concepts are outside the 20 canonical fields (income tax expense, weighted-average shares, comprehensive income and the like), which is expected: the map covers the fields in the contract, not the whole taxonomy.
- **Unit anomalies.** 131 (111 issuer-field pairs reporting more than one currency, mostly Canadian issuers that switched reporting currency, and 20 EPS pairs tagged with unit `pure` instead of `USD/shares`). The `unit` column keeps what the filing said; the value is unchanged.
- **Ratio gaps.** 10,450 `OTHER period` (6/9-month YTD and other durations outside the FY/Q windows), 6,670 `missing input`, 46 `zero denominator`, 0 `mixed currency`.
- **Conflicting values within one accession: 0.** Key uniqueness: OK.
- **Legacy 8-K events.** 7,346 events were accepted before 2004-08-23 and carry pre-2004 item codes, which are unreliable metadata; they are counted per issuer, not used to select text.
- **Cross-issuer duplicates.** 1,039 filing rows were listed under more than one issuer (ownership schedules and Forms 3/4); the filer's own row won (decision A-27), and 0 event rows needed resolving.
- **The one inaccessible fact carrier.** CRM `0001108524-21-000014` is in companyfacts but absent from submissions, and its header answers 404. Its 555 facts keep `filed` 23:59:59 New York and `ts_imputed = true` (decision A-23, last sentence); 52 of them map to canonical fields.

## Key decisions and reasoning

- **The acceptance time still comes from the SGML header.** The API agreeing with the headers on this fetch is welcome but not a reason to depend on it: A-1 found the shift goes in both directions by CIK and day, and an early timestamp is lookahead that S-5 forbids. The header is immutable and the cache never re-fetches it.
- **`raw.universe` is a function of the cache, not of the CSV.** A build must describe exactly what it contains. Unioning the two universe files keeps the 40-issuer file sufficient for the fixture build and the large file sufficient for the live one, and a disagreement between them is an error rather than a silent precedence rule.
- **The HTML/PDF tie-break is narrow.** Only "one HTML candidate among several 8-K-typed documents" is resolved; every other multiplicity raises. That covers EDGAR's PDF-of-the-whole-filing convention without ever choosing between two different documents.
- **Non-HTML documents are skipped at ingest, not at fetch.** Decision A-40 says every `EX-99.1` of a selected filing is fetched, so the PDFs stay in the archive; the text table only promises text, and a PDF needs a parser the project does not carry.
- **`build-live/` is ignored.** The issue asks for the build outside the tracked tree next to `data/`; `.gitignore` now covers it.
- **Performance is linear in issuers and documents.** The 40-issuer Epic 02 build took 20.3 s; 250 issuers without text take 39.8 s. The second build's 9.2 min is the one-off HTML conversion of 13,876 documents (about 36 ms each), not a super-linear term. Peak resident memory is 3.9 GB before text and 5.7 GB with it; nothing in the pipeline is quadratic in universe size.

## Deviations from the plan

- **The text fetch ran twice.** The first run stopped on the McDonald's index at the boundary between the pre-2014 404s and the first 2014 filing; the fix above let the second run finish and resume from the cache without re-fetching anything it held.
- **The re-run of `fetch` before the build was a full (non-incremental) fetch.** Its purpose was to prove that a warm cache re-fetch completes and fetches no headers it already has; the incremental path is proved separately at the end of the run.
- **The cache is ahead of the build by 15 issuers.** The final incremental probe found new filings for 15 issuers, which the next release run (a full fetch, then a build) absorbs. The database and export described above are a pure function of the cache as of the second build.

## Known limitations and deferred items

- **Text before June 2014 is not available from this endpoint.** 4,958 Item 2.02 filings (40 % of the set) predate it. The legacy `-index.htm` pages list documents without reliable SGML types; supporting them would need its own survey and is not part of this epic.
- **An absent index page is asked for again on every run.** The raw cache stores response bodies, not absences, so each nightly `fetch-text` re-requests the 4,958 pre-2014 indexes (about 17 min at 5 req/s). Caching the absence needs a raw-cache contract change (a manifest row without a body, or a marker the verifier accepts) and is recorded as a fix request for the cache owner rather than smuggled in here.
- **PDF-only exhibits are archived but not readable.** Seven documents; a PDF parser is a new dependency, which the wave forbids.
- **Share counts are `currency_out_of_scope` in `fundamentals_pit_usd`.** A share count has no currency, so decision A-38's three reasons put it in the out-of-scope bucket; the value is unchanged and `converted = false`. Reading share counts from `fundamentals_pit` avoids the flag.
- **B2Gold has no `revenue`.** Its filings tag gold, silver and interest revenue separately with no total, so mapping any one of them would understate revenue by the others; the gap is reported in coverage instead.
- **Source tagging quirks survive into the data.** ICE reports diluted EPS of 120,000,000 for some 2015–2016 quarters (unit `USD/shares`), Lululemon tagged negative quarterly revenue in 2011–2012, and 60 EPS rows use unit `pure`. The pipeline reproduces what the filing said, flags unit anomalies, and does not edit values.
- **The incremental probe is the only live check of A-34.** CI runs the fixture cache; the nightly refresh exercises this path on real data.
- **The FX series starts in 2017.** 4,862 CAD versions with earlier period ends stay unconverted (`no_rate`), by decision A-39.
