# Epic 02 — End-to-end integration

## Goal

Epic 01 produced the whole Phase 1 pipeline and proved it on fixtures. Epic 02 makes it run the way the project will actually be used: CI builds the full offline database on the one-core container runner, end-to-end tests drive the real CLI over the fixture cache, the acceptance rules for filings without headers and for header-only filings are conservative in both directions, stock splits no longer masquerade as accounting restatements, every build records its provenance for the release manifest, QA reports the questions an audit asks, and the pipeline is exercised against live SEC data for the 40-issuer universe.

The epic ends when:

- CI runs on `edgar-pit-docker-1` (the CPU-capped Linux container) and its `dbt` job builds `edgarpit.duckdb` from the fixture raw cache, runs the definition-of-done `dbt build --target fixtures`, and runs `edgarpit qa` to exit 0 (issues #61, #62);
- `tests/test_e2e.py` drives the real CLI through `subprocess` and reproduces the Phase 1 definition of done, including the RY restatement boundary and byte-identical exports from two independent builds (#63);
- the fallback acceptance time is never earlier than the truth for paper filings, and every fetched SGML header is used, even when its accession is absent from submissions (#65);
- `fundamentals_pit.restatement_kind` separates split adjustments from accounting revisions, and query, API and explorer carry it (#66);
- fixtures keep every mapped concept automatically and the staging data is rebuilt from them (#67);
- every build writes `build-info.json` and the export manifest carries the SEC fetch range by default (#68);
- QA reports split adjustments, legacy 8-K items, filing sources and ratio gaps (#69);
- the live 40-issuer fetch/build/qa/export run completes with QA exiting 0 and its numbers recorded here (#70 — **open**, see the live run below).

## How it works

### CI runner and the offline build in CI

While the repository is private, CI runs on exactly one self-hosted runner, `edgar-pit-docker-1`: a `linux/arm64` Docker container (Ubuntu 24.04, `ghcr.io/actions/actions-runner`) limited to 1 CPU core and 4 GB, with the labels `self-hosted, Linux, ARM64, edgar-pit` (decision C-4). `.github/workflows/ci.yml` keeps the four required jobs `lint`, `types`, `tests`, `dbt`, the per-ref concurrency group with `cancel-in-progress: true`, and the `pull_request` / `push: main` triggers. Each job has a `timeout-minutes` cap (lint 10, types 10, tests 25, dbt 15); the jobs share the one core, so they run one at a time and queue. `astral-sh/setup-uv` installs uv, uv installs Python from `.python-version`, and uv's cache and managed Python installs live in the runner's home volume, so later runs start warm.

The `dbt` job is the full offline Phase 1 path, not the old staging shortcut:

```
uv run edgarpit build --offline --fixtures tests/fixtures/sec --out "$RUNNER_TEMP/ep"
EDGARPIT_DB="$RUNNER_TEMP/ep/edgarpit.duckdb" uv run dbt build --project-dir transform --profiles-dir transform --target fixtures
uv run edgarpit qa --db "$RUNNER_TEMP/ep/edgarpit.duckdb"
```

`edgarpit build` runs ingest → load → dbt (`--target build`) on the fixture raw cache; the explicit `dbt build --target fixtures` then runs the definition-of-done pair against the same database, and `qa` must exit 0. `edgarpit devdb` remains for local tooling but is no longer used by CI.

### End-to-end tests (`tests/test_e2e.py`, marker `e2e`)

The module drives the installed CLI through `subprocess` (`sys.executable -m edgarpit.cli …`) from the repository root against `tests/fixtures/sec`, into pytest temp directories, so it tests the same entry point a user runs. Two module-scoped builds are deliberate: `built` runs `edgarpit build --offline`, and `loaded` runs `python -m edgarpit.load`, the same `build_from_raw` path through its module entry point — the second independent build the export determinism check needs, and the entry point that prints the per-table row counts. On top of those databases the module asserts:

- `asof --json` at `2024-01-15T00:00:00Z` reproduces every key of `tests/fixtures/expected_asof.json`;
- the restatement boundary: one second before the restating 40-F's acceptance the FY2023 revenue period still answers 56,129,000,000 (accession `0001193125-23-285639`), and exactly at the acceptance instant it answers 51,464,000,000 (accession `0001193125-24-270294`). The instants are derived from the built database, and because that filing also starts FY2024, the test asks for two periods and selects FY2023 by its reference `period_end`, additionally pinning FY2024 as absent one second earlier and present exactly at the instant;
- `qa` exits 0, and a copy of the database with a duplicated `fundamentals_pit` key exits 1 with `Key uniqueness: violated` (the copy keeps the file name because dbt names the catalog explicitly, and the dbt view is renamed before a physical table with the duplicate row replaces it);
- `export` from the two independent builds produces byte-identical Parquet files and manifests equal after removing `build_timestamp`;
- `universe resolve --offline --fixtures …` exits 0;
- FastAPI `TestClient` `/asof` returns the RY accession, and the explorer runs under `streamlit.testing.v1.AppTest` against the built database without exceptions.

The marker is registered in `pyproject.toml` and is not deselected, so the module runs in CI's `tests` job; the whole suite, this module included, passed in about a minute and a half on the one-core runner.

### Conservative fallback and header-only filings (decisions A-23, A-24)

A filing with no usable SGML header falls back to `acceptance_ts = greatest(filing_date 23:59:59 America/New_York, acceptance_ts_api + 6 hours)` (the API term only when present). The old filing-date end of day alone was not an upper bound: RY `9999999997-18-002767` is dated 2018-03-08 but was accepted 2018-03-15, seven days later, so facts from paper filings could leak early. The six-hour margin covers the submissions API's observed four- to five-hour shift in either direction. The source stays `filing_date_eod`, `ts_imputed` stays true, and an ambiguous fallback hour resolves to the later instant (decision A-22).

An accession with a cached header but no submissions row becomes a `raw.filings` row built from the header (`source='header'`, `acceptance_ts_source='sgml_header'`, `ts_imputed=false`, `acceptance_ts_api` null, `report_date` from `<PERIOD>`, `items` from `<ITEMS>`, `is_xbrl=false`, `primary_document=null`), so every fetched header is used and its facts are never imputed from `filed`. Header-only 8-Ks flow through the same item explosion.

### Restatement kinds (decision A-25)

`fundamentals_pit.restatement_kind` is `none` | `split_adjustment` | `revision`, added as the model's last column; `is_restatement` is unchanged. A restatement is a `split_adjustment` when the field's unit family is `shares` or `currency_per_share` and the ratio between the version's value and the first value, or its inverse (reverse splits), is within 0.5 % of an integer factor in `{2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50, 100}`; every other restatement is a `revision`; `none` holds exactly when `is_restatement` is false. dbt tests pin the accepted values, the iff, and that splits never appear on currency fields. `query.AsOfResult` carries the field last, the API mirrors it, and the explorer marks only `revision` as a restatement and gives `split_adjustment` its own legend entry.

### Fixtures derive from the concept map (issue #67)

`tools/build_fixtures.py` keeps every concept of every field in `config/concept_map.yaml` (through the new public `ConceptMap.specs()`), plus a small extras-only set for deliberately unmapped concepts, so `us-gaap:RevenuesNetOfInterestExpense` and future map additions are kept automatically. The fixtures were re-fetched live at ≤ 4 req/s (3 submissions + 18 pages + 3 companyfacts + 431 headers + `company_tickers.json`, 1.78 MB, 0 headers missing) and the staging CSVs and `expected_asof.json` rebuilt; the RY reference values are byte-identical. The one pin that changed is Shopify FY2022 revenue, which now selects `Revenues` (5,600,000,000, accession `0001594805-25-000012`, one version) because the refreshed companyfacts expose the preferred total (decision A-18).

### Build info and export (issue #68)

`build_from_raw` writes `<out_dir>/build-info.json` after a successful dbt run: the package version, the raw cache path, per-CIK document counts by endpoint, the SEC fetch range (min/max `fetched_at` from the raw manifest), row counts for every raw and main contract table, and the dbt node summary. No wall-clock time goes into data tables. `edgarpit export` reads `build-info.json` next to `--db` for the fetch range when `--data` is not given, and `--data` still overrides. A missing `build-info.json` is treated as no range (hand-built databases keep working); a malformed one is a hard error. `edgarpit fetch` prints a per-issuer summary table, and `IssuerFetchReport.headers_404` lists the missing header accessions.

### QA additions (issue #69)

`edgarpit qa` now also reports: restatement counts split by `restatement_kind`, with the top-restatements audit list restricted to `revision` rows; 8-K events accepted before 2004-08-23 counted per issuer (the cutoff uses New York wall time, because it is a US effective date) with a note that pre-2004 item codes are unreliable; filings by `source` (`recent`, page file names, `header`) and by `acceptance_ts_source`; and ratio gaps — periods with an input but no `ratios_pit` row, classified `OTHER period`, `missing input`, `mixed currency` or `zero denominator`, with the first ten examples. `load.load_reference_tables` flattens the concept map through `ConceptMap.specs()`.

### The live run (issue #70 — open)

The live run is **partially complete**; the numbers below are from PR #79, which is open because the issue's headline acceptance (the full build → qa → export) is not met:

- `edgarpit fetch --universe config/universe.csv --out ./data`: **40 issuers ok, 0 failed; 11,915 requests; 2,566.9 s; 11,647 headers fetched, 0 cached, 0 not found**; 0 fetch errors after retries; average **4.64 requests/s** (the client caps at 5).
- `edgarpit build --data ./data --out ./build` stops with `ValueError: SGML header is missing <ACCEPTANCE-DATETIME>`: **581 of the 11,647** cached headers are real EDGAR headers for pre-XBRL filings (e.g. JPM `0000019617-00-000005`) that carry no acceptance tag, and the header parser is fatal on them. These filings are in the set decision A-1 requires a header for, and a header that cannot yield an acceptance time is exactly the "no header available" case A-23 already defines.
- A diagnostic build over a copy of the cache with those 581 header files removed (the effect of the suggested fix) completed the whole pipeline: dbt `PASS=68 WARN=0 ERROR=0`; database **30.8 MiB**; row counts `raw.filings` 429,703, `raw.events_8k` 17,056, `raw.facts_raw` 821,605, `raw.concept_map` 61, `raw.universe` 40, `main.fundamentals_pit` 86,782, `main.ratios_pit` 18,057, `main.events_8k` 17,056, `main.filings` 429,703; `edgarpit qa` **exit 0**, `Key uniqueness: OK`.
- QA summary on the diagnostic build: imputed `filing_date_eod` 418,637; header-vs-API disagreements over 60 s **8,410** (header-sourced rows only); restated versions 3,825 (3,532 `revision`, 293 `split_adjustment`); legacy 8-K events JPM 223, BAC 359, WFC 140, KO 50, PEP 122, PG 124, CVX 72; ratio gaps `OTHER period` 1,882, `missing input` 1,089, `zero denominator` 1. Every issuer has FY coverage for revenue, net income, total assets and equity.
- Boundary checks on two `revision` restatements, one Canadian: TECK FY2023 revenue 15,011,000,000 before `2025-02-20T20:46:54Z` and 6,476,000,000 at it (accession `0000886986-25-000004`); AAPL FY2009 operating income 7,658,000,000 before `2010-01-25T21:25:58Z` and 11,740,000,000 at it (accession `0001193125-10-012091`).
- The required re-fetch issued **268 requests in 85.6 s and fetched 0 headers** (11,647 cached), as headers are immutable.
- `edgarpit export` on the diagnostic build wrote 5 Parquet files plus a manifest whose `sec_fetch_range` came from the `build-info.json` next to the database, with no `--data`.
- `tests/test_live_smoke.py` (marked `network`, excluded from CI) fetches RY (40-F) and SHOP (10-K) into `tmp_path` and drives `edgarpit build --data` and `edgarpit qa` through subprocesses: 1 passed in 51.8 s.

The run exposed two bugs in workstream files, fixed in PR #79: the submissions API lists 81 accessions under several issuers, which the unique-accession contract rejected, so `build_from_raw` now keeps the filer's own row when the accession prefix names an issuer and otherwise the smallest CIK (the same rule keyed by `(accession, item_code)` for events); and QA's header-vs-API disagreement section now compares only header-sourced rows, because an A-23 imputed bound differs from the API by construction (the live count fell from 427,047 to 8,410).

## Key decisions and reasoning

- **One CPU-capped container runner, queueing accepted.** The macOS runners were replaced by a single `linux/arm64` Docker container with 1 CPU and 4 GB (decision C-4). Jobs queue behind each other; that is the accepted cost of a simpler, reproducible environment, and per-job `timeout-minutes` keep a stuck job from blocking the queue.
- **CI proves the full offline path, not a shortcut.** The `dbt` job builds the database the same way a user does (`edgarpit build --offline`), then re-runs the definition-of-done `dbt build --target fixtures` and `qa`. The old `devdb` staging step is gone from CI (the module itself stays for local tooling).
- **End-to-end means the CLI through `subprocess`, not an imported function.** The module runs `python -m edgarpit.cli` with the interpreter running the tests, so the entry point, exit codes and stdout are all exercised. Independent builds are used only where independence is the point (export determinism); everything else shares the two module-scoped databases, which keeps the module well inside its four-minute budget on the one-core runner (the whole CI `tests` job passed in about 90 seconds).
- **The restatement boundary is tested through `--n 2`.** The restating 40-F reports the restated FY2023 and the new FY2024 at the same instant, so a bare `asof` answers with FY2024; the test selects the FY2023 row and pins both the flip and the new period's appearance at exactly the acceptance instant.
- **The fallback has two terms because one source is not enough.** The filing-date end of day is a sound upper bound for electronic filings, but paper filings can be accepted days later; the API timestamp is the only other per-filing evidence, and the +6 h margin makes it safe even when the API is early.
- **Header-only filings are first-class rows.** Every fetched header is used, even without a submissions row, so the PIT chain never falls back to `filed` for facts whose acceptance time the project already paid to fetch. Header-only rows are appended in accession order so builds stay deterministic, and unknown header fields stay null/false rather than guessed.
- **Splits are reported, not hidden.** A split changes shares and per-share values by a near-exact factor; calling that a restatement misleads. `is_restatement` stays as the value-change flag (backwards compatible) and `restatement_kind` adds the interpretation; QA's audit list keeps only `revision` rows.
- **`build-info.json` is the build's provenance record.** It carries the same `sec_fetch_range` key as the release manifest so `export` reads one shape, and it keeps CI-built and live-built databases equally explainable. Document counts are stored document versions per `(CIK, endpoint)`, so a refresh that adds a second copy counts 2.
- **Cross-issuer duplicate accessions resolve at load, not in ingest.** Only `build_from_raw` concatenates issuers, so the filer's own row (or the smallest CIK) wins there; ingest is per issuer and cannot see the duplicate (PR #79, open).

## Interfaces for future epics

- **CLI.** The commands are unchanged; `edgarpit qa` reports the new sections, `edgarpit fetch` prints a per-issuer summary table, `edgarpit build` prints the database path and the dbt summary, and `python -m edgarpit.load` prints the table row counts.
- **`build-info.json`** next to the database: `package_version`, `raw_cache`, `documents` (per CIK and endpoint), `sec_fetch_range` (`min`/`max`), row counts for every raw and main table, dbt summary. Consumed by `export` and by the epic's live review.
- **`fundamentals_pit.restatement_kind`** (`none` | `split_adjustment` | `revision`) and `AsOfResult.restatement_kind` as its last field; the API response models include it.
- **`ConceptMap.specs()`** returns the ordered `ConceptSpec` list (field, kind, unit family, both taxonomy concept lists); the loader and the fixture builder use it instead of the private mapping.
- **`FetchReport` / `IssuerFetchReport`**: run totals plus `headers_fetched`, `headers_cached`, `headers_404` (accessions), per-endpoint document counts and issuer status.
- **QA report**: restatement counts by kind; top restatements (`revision` only); legacy 8-K events; filings by `source` and `acceptance_ts_source`; ratio gaps by reason with examples.
- **`export_release(con, out_dir, *, raw_manifest=None, build_info=None)`**: `raw_manifest` wins, then `build_info`, then no range.
- **`tests/test_live_smoke.py`** (network-marked) is the template for any future live check: fetch two issuers into `tmp_path`, build, QA.

## Deviations from the plan

- **`edgarpit build` does not print the per-table row counts.** The counts are printed by `python -m edgarpit.load --raw … --out …`, the same `build_from_raw` path; the e2e module runs both and asserts each. The issue text assumed the CLI printed them; changing `cli.py` was outside this workstream.
- **The end-to-end module runs two builds.** The issue allows the second where independence is the point; export determinism across independent builds is that point, and `edgarpit build` versus `python -m edgarpit.load` covers both entry points at once.
- **Shopify FY2022 revenue is now one `Revenues` version** where the old staging data had two contract-revenue versions. That is the intended effect of keeping every mapped concept and re-fetching (decision A-18 rank preference), disclosed with the fixture refresh in PR #77.
- **`tests/test_cli.py` was touched by the restatement-kind work** (its hand-built `main.fundamentals_pit` needed the new column so `query.asof` could read it) — one file from another workstream, disclosed in PR #76.
- **The live run is not complete at the time of writing.** See Known limitations.

## Known limitations and deferred items

- **Issue #70's full-universe run is blocked.** The fetched 40-issuer cache is complete and re-fetched, but `build` stops on 581 pre-XBRL headers without `<ACCEPTANCE-DATETIME>` (of 11,647). The fix — treat a header whose `parse_header` raises as "no header available" and let the A-23 fallback apply, with a regression test on the cached `0000019617-00-000005` header — is described in PR #79 with the affected accessions per issuer (BAC 120, JPM 100, PG 73, WFC 67, CVX 46, PEP 38, AMZN 38, UNH 28, WMT 18, AAPL 17, JNJ 13, KO 9, NVDA 7, HD 3, MSFT 3, OTEX 1). The database size, row counts, QA summary and coverage above come from the diagnostic mirror build; `./build` and `./release` from the unmodified cache do not exist yet.
- **Two live-run fixes live in PR #79, which is open**: cross-issuer duplicate-accession resolution in `load.build_from_raw` and the header-sourced filter in QA's disagreement section.
- **`docs/architecture.md` §4 still documents the pre-A-23 fallback** and §6's `raw.filings.source` note does not list `header`; both are outside this epic's file scope and belong to a later docs pass.
- **A-25's split tolerance is a heuristic.** A genuine split whose factor is not a near-integer in the supported set, or whose per-share values make the ratio drift outside 0.5 %, is reported as a `revision` (Apple FY2019 diluted EPS 2.46 → 0.61 is 4.03×, so it classifies as a revision). `is_restatement` is unchanged either way, and QA shows both kinds separately.
- **Q4 is not derived** and `OTHER` durations (6/9-month YTD) are excluded from `asof`; ratio gaps report the `OTHER period` reason instead.
- **No FX conversion.** A ratio whose inputs are in different currencies is dropped and counted as a `mixed currency` ratio gap.
- **Fallback timestamps are upper bounds.** A filing whose header is missing or unusable is timed at `greatest(filing_date end of day, API + 6 h)`; safe for PIT, conservative by construction.
- **Live SEC behaviour is not covered by CI.** Tests marked `network` are deselected; the run above and the smoke test are manual.
- **One runner is a bottleneck.** CI jobs queue; the four required checks can take longer than the work itself when several PRs are open (accepted, decision C-4).
- **The explorer and API remain single-process, unauthenticated development services**; the API has no pagination or rate limiting.
- **Out of scope by phase:** a larger universe, 8-K document text, LLM extraction, a hosted multi-user service and derived analytics beyond the three ratios.
