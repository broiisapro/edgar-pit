# Epic 07 — Scheduled refresh and weekly releases

## Goal

Up to this epic the dataset was built when someone ran the commands. This epic makes it a living dataset: a nightly job fetches what changed, rebuilds, adds 8-K text and FX rates, rebuilds again and records the QA report; a weekly job re-reads the whole universe, builds, proves the data and publishes an immutable GitHub Release.

The epic ends when:

- `.github/workflows/refresh.yml` and `.github/workflows/release.yml` exist, are inert until the owner enables them, and carry the security and cache choices in this document (#118);
- the CLI surface they call exists and the workflows use it exactly (#131);
- the release runs a full, non-incremental fetch before building, and published releases are immutable (decision A-42);
- the cold-backfill path, the cache's limits and every common failure mode are written down for an operator ([`docs/operations.md`](operations.md)).

Everything below describes the merged workflows at the commit that added this document. Neither workflow has run on GitHub yet: they are disabled by default, and the first enabled run is the owner's cold backfill (see "What the owner runs after merge"). The commands they call are the ones the CLI tests exercise; the workflow YAML itself is pinned by `tests/test_workflows.py`.

## How it works

### The nightly refresh (`refresh.yml`, issue #118, PR #120)

One job, `refresh`, on `ubuntu-24.04` with a 360-minute timeout. It runs on a schedule at `43 10 * * *` UTC — 02:43 or 03:43 US Pacific, outside the 06:00–09:00 window when the owner's machine and the network are busy — and on manual dispatch. It has no `push` or `pull_request` trigger, so a fork cannot run it and cannot read its secret.

The job carries `if: vars.REFRESH_ENABLED == 'true'`. With the variable unset or any other value the job is skipped and the run reports green without doing anything; a manual dispatch obeys the same switch, so there is no way to bypass it. The workflow's token is read-only (`permissions: contents: read`), and its concurrency group `edgarpit-scheduled`, shared with the weekly release, with `cancel-in-progress: false` makes a slow night hold the slot instead of overlapping the next one, and makes a release that enters the lane wait for the running refresh rather than race it.

The steps, in order:

1. **Check out and install** — `actions/checkout`, `astral-sh/setup-uv` with its package cache keyed on `uv.lock`, then `uv sync --frozen`.
2. **Check `SEC_USER_AGENT`** — an empty secret fails here with an `::error::` naming it, before the first request.
3. **Restore the raw cache** from `data/raw` with a fresh key (`edgarpit-raw-<run id>-<attempt>`) and `restore-keys: edgarpit-raw-`; a cache miss is not an error, so the first run starts empty.
4. `edgarpit fetch --universe config/universe_large.csv --out data --incremental` — one submissions request per issuer; an issuer whose newest accession and filing count are unchanged is skipped and counted (`skipped N unchanged`), and only new filings' pages, company facts and SGML headers are fetched (decision A-34).
5. `edgarpit build --data data --out build` — ingest the cache, load DuckDB, run dbt. This build exists so the next step can read the 8-K filings that carry Item 2.02.
6. `edgarpit fetch-text --db build/edgarpit.duckdb --items 2.02 --data data` — fetch the filing index and documents of every Item 2.02 8-K not cached yet. It only fetches; the text sections are built by the next build, so the database stays a pure function of the cache (decision A-37).
7. `edgarpit fx --out data` — request the Bank of Canada `FXUSDCAD` series once; the body is stored only when it differs from the latest copy, so an unchanged series adds no file.
8. `edgarpit build --data data --out build` — rebuild, now with the new text and FX rows in the cache.
9. `edgarpit qa --db build/edgarpit.duckdb | tee build/qa.txt` — `set -o pipefail` keeps QA's exit code through `tee`, so a duplicated key or a late `asof_ts` fails the job.
10. **Save the raw cache** (see "Cache strategy and cold backfill").
11. **Upload** `build/build-info.json` and `build/qa.txt` as the `refresh-report` artifact, kept 14 days.

The database itself is not uploaded: it can be rebuilt from the cache at any time, and the release is the place a database is published.

### The weekly release (`release.yml`, issue #118/PR #120; full fetch in issue #131/PR #135)

Two jobs. The first, `build`, has a read-only token and runs on `ubuntu-24.04` with a 180-minute timeout. It runs on Sunday at `17 12 * * 0` UTC — 04:17 or 05:17 Pacific, after the nightly refresh — and on manual dispatch, under the same `REFRESH_ENABLED` gate and the `edgarpit-scheduled` concurrency group it shares with the nightly refresh.

1. **Restore the raw cache read-only** with the same key scheme and `fail-on-cache-miss: true`: publishing a database built from an empty cache would be worse than not publishing. The release never saves a cache.
2. **Check `SEC_USER_AGENT`** before the first request.
3. `edgarpit fetch --universe config/universe_large.csv --out data` — the **full** fetch (decision A-42), with no `--incremental`. Once a week every issuer's submissions pages and companyfacts document are re-read, so a late XBRL correction that added no filing cannot stay hidden behind the nightly probe for more than a week. Headers and 8-K documents are immutable and cached, so this costs about one submissions and one companyfacts request per issuer, not a cold backfill.
4. `edgarpit build --data data --out build`.
5. `edgarpit qa --db build/edgarpit.duckdb | tee build/qa.txt` — a key violation exits 1 and stops the job before anything is exported.
6. `edgarpit export --db build/edgarpit.duckdb --out release` — the Parquet files and `manifest.json`.
7. **Assemble the assets** in `assets/`: `edgarpit.duckdb`, `build-info.json`, every `release/*.parquet`, `manifest.json`, then `SHA256SUMS` over all of them, verified immediately with `sha256sum -c SHA256SUMS`. The same step writes `meta/release-tag.txt` (`data-YYYY-MM-DD`) and `meta/release-notes.md`, the stranger-facing release text.
8. **Upload** `assets/` and `meta/` as the `release-assets` artifact (7 days), plus `build/build-info.json` and `build/qa.txt` as `release-report` (14 days, uploaded even when the job failed).

The second job, `publish`, is the only job with `contents: write` and a 30-minute timeout. It downloads the artifact and runs `gh release create` with the tag, the notes file and every asset, targeting the commit the run was built from so the tag records which code produced the data. If a release with that tag already exists it fails with a message instead of overwriting: published data releases are immutable, so a same-day re-run waits for the next weekly run. Release tags are append-only, like the `epic-NN-complete` tags.

Both workflows sit in the single concurrency group `edgarpit-scheduled` with `cancel-in-progress: false`, so whichever starts first keeps the lane: a release that arrives while the nightly refresh is running waits for it, a refresh that fires while a release is running waits for the release, and neither cancels the other. Each run holds its own 5 requests/s SEC limiter, so an overlap would send up to 10 requests/s against the fair-access limit; sharing the lane means the two never fetch at the same time, and the release's Sunday slot after the night is a convenience rather than the thing that keeps them apart.

### Security choices

- **Least privilege.** Both workflows start at `permissions: contents: read`; only `publish` raises to `contents: write`, and it runs no repository code beyond `gh release create`.
- **No untrusted trigger.** Neither workflow runs on `pull_request`, `pull_request_target` or `push`. A fork cannot start one, cannot see the secret and cannot publish.
- **Pinned actions.** Every third-party action is pinned to a full commit SHA with its version in a comment (`actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.3.3, `actions/cache/restore` and `actions/cache/save` v6.1.0, `actions/upload-artifact` v7.0.2, `actions/download-artifact` v8.0.2). A moved tag cannot change what runs.
- **Secret scoping.** `SEC_USER_AGENT` is passed only to the two guards and the fetching steps — the nightly incremental fetch, `fetch-text`, `fx` and the weekly full fetch — and is never set at job or workflow level, never echoed and never interpolated into a shell script. No `${{ }}` expression appears inside any `run` block, so no repository variable can reach a command line.
- **Fail closed.** No step uses `continue-on-error`, `|| true`, `|| echo` or `set +e`; a failing command fails the run. The only `if: always()` steps are the cache-presence check, the cache save and the report uploads, which exist to preserve partial progress and the explanation of a failure.
- **Bounded.** Every job has a timeout (refresh 360 minutes, release build 180, publish 30), so a stuck fetch cannot hold a runner indefinitely.
- **Opt-in.** The `REFRESH_ENABLED` variable is the one switch; merging a workflow change cannot call the SEC or publish anything while it is unset.

### Cache strategy and cold backfill

The cache holds one directory, `data/raw` — the immutable response bodies and `raw/manifest.jsonl`, the same layout as a local `--data` directory. Every run writes a fresh key (`edgarpit-raw-<run id>-<attempt>`) and restores the newest previous entry through the `edgarpit-raw-` prefix. Cache keys are immutable, so a run never edits an existing entry, and a failed run cannot corrupt the last good cache.

The save step runs **always**, but only when `data/raw/manifest.jsonl` exists. Fetched bodies are immutable, so a run that times out or fails halfway still leaves every completed request in the next cache; the guard means a failed `SEC_USER_AGENT` check does not add a second, misleading red step.

GitHub limits a repository to **10 GB of cache** and evicts entries not accessed for **7 days**. A nightly run touches the newest entry every night, so it stays warm; older entries age out as newer runs write new ones. If the cache outgrows 10 GB or is evicted, the next refresh is a full fetch: 250 issuers mean tens of thousands of SGML headers plus pages and company facts, tens of thousands of requests at the 5 requests/s fair-access ceiling, i.e. several hours — more than one 360-minute run usually gets. The documented recovery is to dispatch the refresh again and again: each run restores what the last one saved and continues, so the raw cache is its own progress record. The weekly release cannot finish a cold backfill, because it does not save the cache it fills and refuses to start without one.

### Weekly full fetch and immutable releases (decision A-42)

`data.sec.gov` sends no `ETag` or `Last-Modified`, so the incremental probe compares the submissions document's newest accession and filing count. A companyfacts correction that changes a value without adding a filing is invisible to that probe. The weekly full fetch is the safety net: once a week every issuer's submissions pages and companyfacts are re-read, while headers and 8-K documents stay cached because they are immutable. Releases are append-only: a tag is `data-YYYY-MM-DD`, and a re-run on the same UTC date fails rather than replacing a published dataset.

### Tests

`tests/test_workflows.py` (37 collected cases) parses both files as YAML and text and pins what a reviewer would otherwise have to trust: the triggers are schedule plus dispatch only; the nightly minute is outside the Pacific morning window in both offsets; the release is weekly; every job is gated directly or through `needs`; the token starts read-only and only `publish` may write; actions are 40-character SHAs with a version comment; every job has a timeout; one run at a time with no job-level override; no expression is interpolated into a `run` script; the exact command sequence; the cache restore/save order and the save guard; which steps receive the secret; the fast-fail guards; the release never writes the cache, fetches the whole universe before building and assembles every documented asset; publishing is a separate least-privilege job; and no step silences a failure.

The workflow tests were shown to fail by mutation: removing the release's full fetch fails three release-workflow tests. The CLI mutations those tests protect against — dropping `--incremental`, building the text client over the wrong cache, removing `format_phase2` — are in the Phase 2 CLI test module. `actionlint` reports no error on either workflow file.

## Key decisions and reasoning

- **The gate is job-level, not step-level.** A disabled repository shows a skipped job and does not consume a runner; manual dispatch cannot bypass it.
- **The nightly minute avoids the Pacific morning.** 10:43 UTC is 02:43 or 03:43 Pacific under the two offsets and is not on the hour, so the fetch does not compete with the owner's working day and does not share a minute with another scheduled job.
- **The refresh builds twice.** `fetch-text` needs a database to choose accessions, and text and FX are raw inputs, so the second build regenerates the database from a cache that now includes them. Building into the same directory is safe because the build replaces its tables and re-runs dbt.
- **The release fetches in full.** A nightly probe cannot see a companyfacts-only correction; the weekly job is the backstop, and it costs about two requests per issuer rather than a cold backfill (decision A-42).
- **The release never writes the cache.** Publishing a database built from an empty or partial cache would be worse than a failed run, hence `fail-on-cache-miss: true` and no save step.
- **Publishing is a separate job.** The build/export/checksum job is read-only; only `publish` gets `contents: write`, and it runs no repository code beyond `gh release create`.
- **Releases are immutable.** A same-day re-run fails with a clear message; the next weekly run publishes the next date. Overwriting a published dataset would break every consumer that pinned a release.
- **The cache is saved even on failure.** Fetched bytes are immutable and expensive; keeping them turns a timeout into progress instead of lost work.
- **History is visible in the artifacts.** `build-info.json` and `qa.txt` are uploaded even from a failed run, so a red job leaves the report that explains it.

## Interfaces for future epics

- **Workflows** — `.github/workflows/refresh.yml` (nightly refresh) and `.github/workflows/release.yml` (weekly release); triggers, gate, permissions and command sequence as above.
- **Repository settings** — variable `REFRESH_ENABLED` (`true` enables), secret `SEC_USER_AGENT`. Nothing else is required.
- **Release tag** — `data-YYYY-MM-DD` (UTC date of the run), never rewritten.
- **Release assets** — `edgarpit.duckdb`, every `release/*.parquet` (five on a Phase 1 database, eight on a Phase 2 database), `manifest.json`, `build-info.json`, `SHA256SUMS`.
- **Artifacts** — `refresh-report` (`build-info.json`, `qa.txt`; 14 days), `release-assets` (`assets/`, `meta/`; 7 days), `release-report` (`build-info.json`, `qa.txt`; 14 days).
- **Cache** — `data/raw`, rolling key `edgarpit-raw-<run id>-<attempt>`, restore prefix `edgarpit-raw-`.
- **CLI commands the workflows depend on** — `fetch --incremental`, `build --data`, `fetch-text --items`, `fx --out`, `qa --db`, `export --db --out`.
- **Operator guide** — [`docs/operations.md`](operations.md).

## Deviations from the plan

- **The weekly release first restored an existing cache and built; the full fetch was added in the CLI issue.** Issue #131 put the full, non-incremental fetch before the build and the `SEC_USER_AGENT` guard ahead of it, because the release is the place where a companyfacts-only correction has to be caught (decision A-42).
- **The release notes are written by the workflow, not committed.** The text ships with the data it describes and cannot drift from the run; the repository's own release note is [`docs/release-notes-v1.1.0.md`](release-notes-v1.1.0.md), and the workflow's template is the one attached to each `data-YYYY-MM-DD` tag.
- **The nightly refresh keeps a 360-minute timeout.** It is the GitHub-hosted ceiling and is sized for a cold run; a warm night finishes in minutes.
- **The release build has a 180-minute timeout instead of the refresh's 360.** It restores a warm cache and refuses to start without one, so it never carries a cold backfill.

## Known limitations and deferred items

- **Neither workflow has run on GitHub yet.** They are disabled by default and the repository's Actions history has no scheduled run; the first enabled dispatch is the owner's cold backfill.
- **The first run is expensive.** A cold 250-issuer backfill is tens of thousands of requests at 5 requests/s and usually needs several dispatches; the cache is saved each time, so the runs resume rather than restart.
- **A cache eviction repeats the cost.** GitHub's 10 GB/7-day cache can evict the tree; the next refresh is then a full fetch again.
- **The workflow's inline release-notes template still lists the five Phase 1 Parquet files.** The assemble step copies every `release/*.parquet`, so a Phase 2 database ships all eight; only the attached notes text is stale. [`docs/release-notes-v1.1.0.md`](release-notes-v1.1.0.md) is the corrected stranger-facing text.
- **There is no alerting.** A failed schedule is visible in the Actions tab and in the `refresh-report` artifact, not in an inbox or a pager.
- **The cron times are UTC.** The local Pacific time shifts by an hour across daylight saving; the nightly window avoids the working morning in both offsets, but the release's 04:17/05:17 Pacific is not a fixed wall-clock time.
- **A hosted runner is ephemeral.** Every run re-installs dependencies and restores the cache; there is no persistent workspace and no warm DuckDB file.
- **The workflows publish data, not code.** No tag in this epic is a package release; the v1.1.0 release note and the data releases are separate artifacts.

## What the owner runs after merge

The workflows are already merged and inert. These steps turn them on and prove the first run:

1. **Set the two repository settings** under **Settings → Secrets and variables → Actions**: the variable `REFRESH_ENABLED=true` and the secret `SEC_USER_AGENT` set to a declared contact of the form `Name email`. With `gh` authenticated as an administrator:

   ```bash
   gh variable set REFRESH_ENABLED --body true
   gh secret set SEC_USER_AGENT   # paste the value at the prompt
   ```

2. **Start the refresh by hand and watch it.** The first run is a cold backfill and may hit the 360-minute cap: `gh workflow run refresh.yml`, then `gh run watch <run-id>`. If it stops at the timeout, run it again; the cache is saved even on a failed run, so each dispatch continues where the last one stopped. Repeat until a run finishes and the next nightly schedule completes in minutes.
3. **Let the weekly release run, or dispatch it by hand once the backfill is complete.** It restores the cache with `fail-on-cache-miss: true` and publishes `data-YYYY-MM-DD`; a `data-` tag is never rewritten.
4. **Tag the epics.** Annotated tags `epic-04-complete` through `epic-07-complete` after verification (decision G-4), then cut `v1.1.0` from the verified commit.

The full operator guide, including how to read each class of failed run, is [`docs/operations.md`](operations.md).
