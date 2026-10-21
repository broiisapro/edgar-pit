# Epic 03 — Ship v1.0.0

## Goal

Epic 02 made the pipeline correct and ran it end to end; Epic 03 makes the project shippable. The epic ends when:

- `README.md` opens with the chart and the as-of recording, states the point-in-time rule in five lines, and shows a quick start a stranger can run offline in under two minutes (issues #81, #82);
- the code is MIT-licensed, the package metadata is complete and the repository carries no local paths, secrets or debug leftovers (#84);
- CI runs on GitHub-hosted `ubuntu-24.04` runners with the four required jobs, before the repository goes public (#86);
- the explorer can load the released database from a URL and deploy on Streamlit Community Cloud without a local build (#85);
- the Epic 02 live run is finished and recorded, including the pre-2001 SGML headers that blocked the first build (#87, #70, #88);
- the release contents, the deployment steps and the remaining limitations are written down in this document (#83).

Everything below describes the merged code. The README links the `v1.0.0` release page and the hosted explorer at `https://edgar-pit.streamlit.app`; the links resolve once the owner makes the repository public, publishes the release and deploys the explorer, which are the steps listed in "What the owner runs after merge" and not claimed as done here.

## How it works

### README assets (`tools/readme_assets.py`, issue #81)

The two images are produced by one tool that runs locally, never in CI. It builds `edgarpit.duckdb` from `tests/fixtures/sec` through the installed CLI (`edgarpit build --offline --fixtures … --out <work>`), queries `main.fundamentals_pit`, and writes:

- `docs/img/ry-revenue-restatement.svg` — Royal Bank of Canada annual revenue by fiscal year. Every one of the 15 filing versions is a dot: filled for the first filing of a period, open for later versions of the same period, offset a fraction of a year so a repeated version stays visible. The FY2023 pair is red, joined by a dashed connector and labelled `56.129 B as filed / 2023-11-30T11:45:12Z` and `51.464 B as restated / 2024-12-04T11:26:17Z`. The subtitle's `−8.3 %` is computed from the two versions. The SVG carries an explicit white background, so it reads the same in GitHub's light and dark themes.
- `docs/img/asof-demo.gif` — a terminal recording of `edgarpit asof --ticker RY --field revenue --asof <one second before the acceptance>|--asof <the acceptance> --n 2`. The tool runs both commands, writes an asciinema v2 cast from their real output with fixed timings, and renders it with `agg` (`brew install agg`, 44 KB). `--n 2` is required because the restating 40-F also introduces FY2024, so the FY2023 row is only visible alongside it; the recording shows 56,129,000,000 one second before the acceptance and 51,464,000,000 at it.

The tool refuses to draw a chart whose data no longer contains exactly one downward CAD revision, and refuses to write the recording if the restated value appears one second before its acceptance. Rendering needs Altair's `vl-convert-python`, added to the `dev` dependency group only. Re-running the tool produces a byte-identical SVG and GIF; the only inputs are the fixture cache and the tool versions.

### Licence and metadata (issue #84)

- `LICENSE` is MIT, `Copyright (c) 2026 Moksh Siruvani` (decision A-28: code under MIT, SEC data credited in the README).
- `pyproject.toml` carries `version = "1.0.0"`, the GitHub description, `license = "MIT"` (PEP 639) with `license-files`, the author, the ten keywords/topics, classifiers, and `[project.urls]` for Homepage/Repository/Issues on `https://github.com/broiisapro/edgar-pit`. `edgarpit.__version__` matches.
- `.env.example` lists every variable the code reads, each with a one-line comment: `SEC_USER_AGENT`, `EDGARPIT_DB`, `EDGARPIT_RATE_LIMIT_RPS`, and the commented-out `EDGARPIT_DB_URL` / `EDGARPIT_DB_SHA256` pair. `.gitignore` covers `data/`, `build/`, `release/`, `.env`, `.orchestration/` and `.worktrees/`.
- The repository scan (in PR #90) found no debug prints, no `breakpoint()`, no TODO without an issue number, no absolute local paths, no file over 5 MB outside `tests/fixtures`, and `uv lock --check` passing.

### Hosted CI (issue #86, decision C-5)

`.github/workflows/ci.yml` keeps the four required jobs `lint`, `types`, `tests`, `dbt`, the per-ref concurrency group with `cancel-in-progress: true`, and the `pull_request` / `push: main` triggers, and now runs every job on `ubuntu-24.04`. Each job installs with `uv sync --frozen` after `astral-sh/setup-uv` with `enable-cache: true` keyed on `uv.lock`; actions are pinned by full SHA; the workflow token is read-only (`permissions: contents: read`); no secrets are used and there is no `pull_request_target`. Timeouts are lint 10, types 10, tests 25, dbt 15 minutes. The `dbt` job still proves the full offline path: `edgarpit build --offline --fixtures tests/fixtures/sec --out "$RUNNER_TEMP/ep"`, then `dbt build --target fixtures`, then `edgarpit qa` must exit 0. The self-hosted `edgar-pit-docker-1` container runner (decision C-4) is retired; `docs/architecture.md` §11 records why (on a public repository anyone's pull request can execute code, and hosted runners keep that code off the owner's machine).

### Explorer deployment path and database download (issue #85)

`explorer/app.py` resolves its database in order:

1. an existing `EDGARPIT_DB` file (or the settings default) — local behaviour unchanged, plus a provenance caption read from `build-info.json` or, failing that, `manifest.json` beside it;
2. otherwise `EDGARPIT_DB_URL`: the released `edgarpit.duckdb` is streamed in 1 MiB chunks to `<tmp>/edgarpit-explorer/<url+digest hash>/edgarpit.duckdb` through a `.part` file, capped at 500 MB (checked from `Content-Length` and again while streaming), verified against `EDGARPIT_DB_SHA256` when it is set (64 hex characters, validated before any request), opened read-only once to prove DuckDB can read it, and cached across reruns and sessions with `st.cache_resource` keyed on URL plus digest; the sibling `manifest.json` supplies the SEC fetch range;
3. neither — a clear error page naming both variables.

A malformed digest, a non-`http(s)` URL, an oversized body and a body that is not a database each show an error page instead of raising. `.streamlit/config.toml` pins a light theme with usage stats off. There is no `requirements.txt`: Streamlit Community Cloud prefers `uv.lock`, so the app installs from the committed lockfile. The eight new tests in `tests/test_explorer.py` drive the download path against a local HTTP server fixture (download and render, cache reuse, checksum mismatch, size cap, non-database body, local-wins precedence) with no external network.

### Release contents (issue #70)

`edgarpit export --db ./build/edgarpit.duckdb --out ./release` writes the release directory:

- `fundamentals_pit.parquet`, `ratios_pit.parquet`, `events_8k.parquet`, `filings.parquet`, `facts_raw.parquet` — fixed row order, fixed Parquet writer settings, no build timestamps inside the data;
- `manifest.json` — `build_timestamp`, `database`, `sec_fetch_range` (`min`/`max` of the raw cache's `fetched_at`), and one entry per file with `table`, `file`, `rows`, `sha256` and the column `schema`. The fetch range comes from `build-info.json` next to the database unless `--data` names the raw cache.

For the hosted explorer the release also carries the database itself (`edgarpit.duckdb`, 30.5 MiB in the live build) as an asset. Two independent builds of the same fixtures produce byte-identical Parquet files and manifests that are equal after removing `build_timestamp`.

### The Epic 02 finish that shipped with this epic

- **Legacy headers (#87, decision A-26).** 581 of the live 11,647 headers predate 2001 and have no `<ACCEPTANCE-DATETIME>`. `parse_header` now raises `MissingAcceptanceTime` for exactly that case; ingest treats such a header as missing for acceptance time (the A-23 fallback, raised to `<DATE-OF-FILING-DATE-CHANGE>` 23:59:59 New York when present) while still using its `<ITEMS>` for 8-K events. Any other malformation still fails the build.
- **The live run (#70, PR #79).** The 40-issuer cache was built, `build`/`qa`/`export` ran to completion and the numbers are recorded in `docs/epic-02-integration.md` and the README: 11,915 requests in 2,566.9 s at 4.64 req/s, 581 legacy headers, a 20.3 s build producing a 30.5 MiB database (429,703 filings, 17,056 8-K rows, 821,605 raw facts, 86,782 `fundamentals_pit` rows, 18,057 `ratios_pit` rows), `qa` exit 0, and the five Parquet files plus manifest.
- **Epic 02 document (#88).** `docs/epic-02-integration.md` carries the final live-run numbers and the three defects the run closed (legacy headers, cross-issuer duplicate accessions, the QA disagreement filter).

### README (issue #82)

`README.md` follows the order a quant researcher, a data-team hiring manager and a finance professor need: the PRD §15 opening paragraph, the chart with a two-sentence caption, five lines on acceptance time vs period end vs filing date plus the submissions-JSON caveat, the offline quick start with pasted output and the recording, live data with the measured numbers and fair-access behaviour, the five interfaces (Python, DuckDB SQL, REST, explorer, Parquet release) each with a command that was run, the data-model table, coverage and limitations including two real QA-reported gaps, how it is tested (classic-bug tests by name, property test, end-to-end module, CI, export determinism), and the MIT licence with the SEC credit. The release link points at the `v1.0.0` tag and the explorer link at `https://edgar-pit.streamlit.app`; both are set, so no placeholder is left for the owner to fill.

## Key decisions and reasoning

- **Hosted CI before going public, not after.** Required checks that run a stranger's pull-request code on the owner's machine are not acceptable once the repository is public, and the hosted jobs run in parallel instead of queueing behind one CPU-capped container (decision C-5, `docs/architecture.md` §11).
- **MIT for the code, credit for the data.** SEC filings are public records; the repository licenses its own code and credits the source in the README without implying endorsement (decision A-28).
- **The explorer downloads the database, not the Parquet files.** It queries DuckDB, so the five Parquet files stay release consumers' format; the database is streamed once, size-capped and cached across reruns, which keeps the app inside Streamlit Community Cloud's ephemeral disk and memory budget.
- **The digest is optional but recommended.** Without `EDGARPIT_DB_SHA256` the download is still size-capped and read-only-validated; with it, a corrupted or truncated asset is rejected and the cached file is never used. The deployment steps set it.
- **No `requirements.txt`.** Cloud's dependency resolution prefers `uv.lock`; a second dependency file would drift from the lockfile for no benefit.
- **README images are built from fixtures and committed.** Anyone can regenerate them with one local command; CI never renders images, so a rendering dependency stays out of the required checks and the SVG/GIF are reviewable in pull requests.
- **The recording is generated, not recorded interactively.** The tool writes the cast from the real command output with fixed timings, so the GIF is reproducible and the pasted terminal text cannot drift from the pipeline's actual output.
- **The live numbers are the measurement from PR #79, quoted with their date.** Re-running the full fetch takes ~43 minutes of SEC traffic; the project stands behind the recorded run rather than a fresh measurement per document.

## Interfaces for future epics

- **Release assets.** `edgarpit.duckdb` (the database the explorer reads), `manifest.json`, and the five Parquet files `fundamentals_pit`, `ratios_pit`, `events_8k`, `filings`, `facts_raw`. The names are frozen by the explorer's docstring and the export code.
- **`manifest.json` fields.** `build_timestamp`, `database`, `sec_fetch_range` (`min`/`max`, UTC ISO-8601 with `Z`), and `files[]` with `table`, `file`, `rows`, `sha256` and `schema`.
- **Explorer environment.** `EDGARPIT_DB` (local file; wins when present), `EDGARPIT_DB_URL` (HTTPS release asset), `EDGARPIT_DB_SHA256` (optional 64-hex digest). Documented in `.env.example`.
- **`tools/readme_assets.py`.** The one way to regenerate `docs/img/ry-revenue-restatement.svg` and `docs/img/asof-demo.gif`; it needs `agg` on `PATH` and the `dev` group installed.
- **Version and CI.** `edgarpit.__version__ == "1.0.0"`; the required checks are still `lint`, `types`, `tests`, `dbt`, now on hosted runners.
- **Epic 02 documents.** `docs/epic-02-integration.md` holds the final live-run numbers that this epic's README quotes.

## What the owner runs after merge

The README already links the release and the explorer, so no placeholder needs filling; these steps make the links resolve.

1. **Make the repository public.** Confirm the self-hosted runner is stopped and deregistered first; hosted CI is already in place.
2. **Publish the `v1.0.0` release.** Build the live database, run `edgarpit export --db ./build/edgarpit.duckdb --out ./release`, then attach `edgarpit.duckdb`, `manifest.json` and the five Parquet files to a GitHub release tagged `v1.0.0`.
3. **Deploy the explorer.** Compute `shasum -a 256 edgarpit.duckdb`, create the app on `share.streamlit.io` for repository `broiisapro/edgar-pit`, branch `main`, main file `explorer/app.py`, Python 3.12, and set the secrets `EDGARPIT_DB_URL` (the release asset URL) and `EDGARPIT_DB_SHA256` (the digest), then deploy it at `https://edgar-pit.streamlit.app`. Cloud installs from `uv.lock`.
4. **Tag the epic.** Annotated tag `epic-03-complete` after verification (decision G-4).

## Deviations from the plan

- **The README's live numbers are quoted from PR #79, not re-measured while writing it.** The 40-issuer fetch takes ~43 minutes and was already run and merged; the document says when it was measured.
- **The recording is rendered by `agg`, which is installed locally, not a project dependency.** The tool documents the `brew install agg` step and fails with a clear message when the binary is missing.
- **The README's DuckDB SQL example uses the DuckDB CLI.** The project does not install the CLI; the example names the command a reader runs after installing it.
- **The explorer is not deployed and the release does not exist at merge time.** Both are owner steps above; the README links `https://github.com/broiisapro/edgar-pit/releases/tag/v1.0.0` and `https://edgar-pit.streamlit.app`, which start resolving when the owner publishes the release and deploys the app.
- **The repository is still private when this epic merges.** Everything is written for a public audience, but the visibility change is an owner action after the checks above.

## Known limitations and deferred items

- **The hosted explorer has not served a real visitor yet.** The download path is tested against a local HTTP fixture and the app was run headless against a served release directory, but the first Cloud deployment is the remaining end-to-end check.
- **CI minutes are consumed while the repository is private.** The hosted jobs use the plan's included minutes; fork pull requests do not run workflows (no `pull_request_target`).
- **The images are not verified in CI.** Regenerating them is a manual step; a fixture change that alters the chart is caught by the tool's own checks when someone runs it, not by the required checks.
- **The explorer's first page load downloads 30.5 MiB.** Later reruns in the same container reuse the cached file; a new release or digest starts a new cache entry.
- **Epic 02's live-run numbers are frozen at 2026-10-18.** The dataset is not refreshed on a schedule in Phase 1; a re-fetch adds immutable cache versions and the next build picks them up, but no workflow does that yet (Phase 2).
- **The earlier epic documents describe the retired self-hosted runner.** `docs/architecture.md` §11 is the current CI description; `docs/epic-02-integration.md` and the Epic 02 PRs keep their historical account of `edgar-pit-docker-1`.
