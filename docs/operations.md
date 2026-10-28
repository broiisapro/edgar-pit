# Operations: scheduled refresh and data releases

Two GitHub Actions workflows keep the dataset current without a server:

| Workflow | Trigger | What it produces |
|---|---|---|
| `.github/workflows/refresh.yml` | nightly `43 10 * * *` UTC (02:43 or 03:43 US Pacific) and manual dispatch | a refreshed raw cache in the Actions cache, `build-info.json` and the QA report as artifacts |
| `.github/workflows/release.yml` | weekly Sunday `17 12 * * *` UTC (04:17 or 05:17 Pacific) and manual dispatch | a full (non-incremental) SEC fetch into the restored cache, then a GitHub Release `data-YYYY-MM-DD` with the DuckDB database, five Parquet files, `manifest.json`, `build-info.json` and `SHA256SUMS` |

Both are inert until the owner switches them on. Nothing in this repository
fetches SEC data on a schedule until the two settings in the next section
exist, so merging a change to either file cannot call the SEC or publish
anything.

## Enabling and disabling

Two repository settings, both under **Settings → Secrets and variables →
Actions**:

1. **Variable** `REFRESH_ENABLED` = `true`. Every job either carries
   `if: vars.REFRESH_ENABLED == 'true'` or needs a job that does, so with the
   variable unset or any other value the run is skipped and reports green
   without doing anything.
2. **Secret** `SEC_USER_AGENT` = a declared contact of the form
   `Your Name you@example.com`. SEC's fair-access policy requires it on every
   request, and the client refuses to send one without it. An early step of
   both workflows fails with a clear `::error::` when the secret is empty,
   before any request is made.

With `gh` authenticated as a repository admin:

```bash
gh variable set REFRESH_ENABLED --body true
gh secret set SEC_USER_AGENT   # paste the value at the prompt
```

To pause the schedules, set the variable to `false` (or delete it). The
workflows, their history and the existing cache stay in place.

The release workflow uses the same `SEC_USER_AGENT`, because it runs a full
fetch of its own before building; it has no separate secret. It publishes with
the workflow's `GITHUB_TOKEN`, which the `publish` job is granted
`contents: write` for; every other job is read-only.

## Running one by hand

```bash
gh workflow run refresh.yml              # nightly refresh, now
gh workflow run release.yml              # weekly release, now
gh run list --workflow=refresh.yml --limit 5
gh run watch <run-id>                    # follow the steps live
```

From the browser: **Actions → Refresh data** (or **Data release**) → **Run
workflow**. A manual run obeys the same `REFRESH_ENABLED` switch as a
scheduled one, and it takes no inputs, so there is no way to bypass the gate.

## What the nightly refresh does

One job, `refresh`, on `ubuntu-24.04` with a 360-minute timeout. In order:

1. **Check `SEC_USER_AGENT`.** Missing or empty: fail here with a message
   naming the secret, before touching the network.
2. **Restore the raw cache** from `data/raw` with a rolling key
   (`edgarpit-raw-<run id>-<attempt>`) and `restore-keys: edgarpit-raw-`, so
   the newest previous run is the fallback. A cache miss is not an error; the
   first run simply starts empty.
3. `edgarpit fetch --universe config/universe_large.csv --out data
   --incremental` — one submissions request per issuer; issuers whose newest
   accession and filing count are unchanged are skipped (`skipped N
   unchanged`), and only new filings' pages, company facts and SGML headers are
   fetched.
4. `edgarpit build --data data --out build` — ingest the cache, load DuckDB,
   run dbt. This build exists so the next step can read the list of 8-K
   filings that carry Item 2.02.
5. `edgarpit fetch-text --db build/edgarpit.duckdb --items 2.02 --data data` —
   fetch the filing index and documents of every 8-K with Item 2.02 that is
   not cached yet. It only fetches: the text sections are built by the next
   step, so the database stays a pure function of the cache.
6. `edgarpit fx --out data` — request the Bank of Canada `FXUSDCAD` series
   once; the body is stored in the cache only when it differs from the latest
   copy, so an unchanged series adds no file.
7. `edgarpit build --data data --out build` — rebuild, now with the new text
   and FX rows in the cache. The database is a pure function of the cache.
8. `edgarpit qa --db build/edgarpit.duckdb | tee build/qa.txt` — the report is
   stored as an artifact, and `set -o pipefail` makes a duplicated key or a
   late `asof_ts` fail the job.
9. **Save the raw cache** (always, even when an earlier step failed — the
   files already fetched are immutable and save the next run the repeat).
   If the run fetched nothing at all, the step is skipped rather than failing
   on a missing directory, so a missing secret shows one clear error.
10. **Upload `build/build-info.json` and `build/qa.txt`** as the
    `refresh-report` artifact (always, kept 14 days).

The database itself is not uploaded: it can be rebuilt from the cache at any
time, and the release is the place a database is published.

## What the weekly release does

Two jobs. The first, `build`, has a read-only token and runs on `ubuntu-24.04`
with a 180-minute timeout:

1. **Restore the raw cache read-only.** The same key scheme as the refresh,
   plus `fail-on-cache-miss: true`: publishing a database built from an empty
   cache would be worse than not publishing. The release never saves a cache.
2. **Check `SEC_USER_AGENT`.** The release fetches, so it needs the same secret
   and fails here, before the first request, when it is empty.
3. `edgarpit fetch --universe config/universe_large.csv --out data` — the
   **full** fetch (decision A-42), with no `--incremental`: once a week every
   issuer's submissions pages and companyfacts document are re-read, so a
   late XBRL correction that added no filing cannot stay hidden behind the
   nightly probe for more than a week. Headers and 8-K documents are
   immutable and cached, so this costs about one submissions and one
   companyfacts request per issuer, not a cold backfill.
4. `edgarpit build --data data --out build`.
5. `edgarpit qa --db build/edgarpit.duckdb | tee build/qa.txt`. A key violation
   exits 1 and stops the job here, so nothing is exported or published.
6. `edgarpit export --db build/edgarpit.duckdb --out release` — the five
   Parquet files and `manifest.json`, with a fixed row order and fixed writer
   settings, so the same cache exports byte-identically.
7. **Assemble the assets** in `assets/`: `edgarpit.duckdb`,
   `build-info.json`, the five `.parquet` files, `manifest.json`, then
   `SHA256SUMS` over all of them, verified immediately with
   `sha256sum -c SHA256SUMS`. The same step writes `meta/release-tag.txt`
   (`data-$(date -u +%F)`) and `meta/release-notes.md`, the stranger-facing
   release text.
8. **Upload** `assets/` and `meta/` as the `release-assets` artifact (7 days),
   plus `build/build-info.json` and `build/qa.txt` as `release-report`
   (14 days, uploaded even when the job failed).

The second job, `publish`, is the only job with `contents: write` and a
30-minute timeout. It downloads the artifact and runs `gh release create` with
the tag, the notes file and every asset, targeting the commit the run was built
from so the tag records which code produced the data. If a release with that tag
already exists it fails with a message instead of overwriting: published data
releases are immutable, so a re-run on the same day waits for the next weekly
run. That also keeps release tags append-only, like the `epic-NN-complete` tags.

## Reading a failed run

Open **Actions → the run → the failed job**; each step's log is one click
away. The CLI prints one line per failure (`error: ...` on stderr) and uses
exit code `1` for a data or fetch failure and `2` for a usage error. The
common ones:

- **`Check SEC_USER_AGENT` failed** — the secret is missing or empty. Set it
  and re-run; nothing was fetched.
- **`Fetch (incremental)` failed** — one or more issuers failed (a final HTTP
  error after retries, malformed JSON, a schema violation). The step prints a
  per-issuer table first and exits 1. The cache still holds every response
  that succeeded, and the `Save raw cache` step runs anyway, so the next run
  picks up where this one stopped.
- **`Fetch (full)` failed** (release only) — the same failure on the weekly
  full fetch. The release stops before QA and publishes nothing; the nightly
  refresh keeps the cache current in the meantime.
- **`Restore raw cache` failed** (release only) — there is no cache to publish
  from: the refresh has not run since the cache was last evicted, or never
  ran, or `REFRESH_ENABLED` has been `false` throughout. Run the refresh first.
- **`QA` failed** — the report prints `Key uniqueness: violated` and names the
  tables with duplicate keys, or an `Asof_ts violations` line names
  `main.fundamentals_pit_usd` rows whose known time is later than their
  `asof_ts`. The refresh still saves its cache and uploads the report; the
  release stops before exporting. This is a data bug, not an infrastructure
  one: fix the pipeline, do not bypass QA.
- **The job hit its timeout** — a cold backfill is the long case (see the next
  section). Completed steps are not lost: the next run restores the cache and
  continues. `refresh-report` and `release-report` are uploaded even when a job
  fails, so the QA text of a failed run is always available.

Both workflows have `concurrency` with `cancel-in-progress: false`: a run that
starts while another is going waits for it instead of racing it for the cache
key or the SEC's rate limit. To abandon a stuck run, cancel it from the
Actions page; the save step then uploads whatever was fetched.

## The first run: a cold backfill

The cache starts empty, and one cold fetch of 250 issuers needs tens of
thousands of requests at the 5 requests/s fair-access ceiling: several hours,
which is more than a single run usually gets. The design assumes that
(decision A-42):

1. Set the two repository settings, then start the refresh **by hand** with
   `gh workflow run refresh.yml` and watch it (`gh run watch <run-id>`).
2. If it hits the time cap, **run it again**. Every response already fetched
   is immutable and the cache is saved even when the job times out, so each
   re-dispatch starts from where the last one stopped instead of from zero.
3. Repeat until a run reports success and the next night's schedule runs in
   minutes. That run is the backfill: nothing extra has to be marked done, and
   the raw cache is its own progress record.

Do not start the weekly release until the backfill has finished at least
once: the release restores the cache with `fail-on-cache-miss: true` and
refuses to publish a database built from an empty one. A cold backfill also
cannot be finished by the weekly release, because the release does not save
the cache it fills.

## The Actions cache

The cache holds one directory, `data/raw` — the immutable response bodies and
`raw/manifest.jsonl`, the same layout as a local `--data` directory. Every run
writes a fresh key (`edgarpit-raw-<run id>-<attempt>`) and restores the newest
previous entry through the `edgarpit-raw-` prefix; keys are immutable, so a
run never edits an existing entry. The release workflow only reads.

GitHub limits each repository to **10 GB of cache** and evicts entries that
have not been accessed for **7 days**. A nightly run accesses the newest entry
every night, so it stays warm; older entries age out as newer runs write new
ones, and the 10 GB ceiling evicts the least recently used first. If the raw
cache outgrows 10 GB, the next run after an eviction is a **full fetch**: 250
issuers means tens of thousands of SGML headers plus pages and company facts,
tens of thousands of requests at the 5 requests/s fair-access ceiling, i.e.
several hours — which is why the refresh timeout is 6 hours and why the
release refuses to run without a cache rather than publishing an empty
dataset. A warm incremental night is one request per issuer and finishes in
minutes.

The cache upload at the end of a refresh moves the whole `data/raw` tree, so a
cold run also spends time compressing and uploading it. That cost is paid once
per eviction, not nightly. The weekly release restores the same cache but never
saves it: its full fetch (decision A-42) reads fresh submissions and
companyfacts documents into its own workspace for that week's build, and the
next nightly refresh fetches what it needs under its own new key. The release
stays read-only on the cache so a failed release can never replace a good one
with a partial tree.

## Where the outputs live

- Actions cache: `data/raw` (raw SEC and Bank of Canada responses plus the
  manifest).
- `refresh-report` artifact: `build-info.json` (package version, table row
  counts, raw-cache document counts, SEC fetch range) and `qa.txt` (coverage,
  restatements, imputed timestamps, anomalies, key uniqueness). Kept 14 days.
- GitHub Release `data-YYYY-MM-DD`: `edgarpit.duckdb`, `fundamentals_pit.parquet`,
  `ratios_pit.parquet`, `events_8k.parquet`, `filings.parquet`,
  `facts_raw.parquet`, `manifest.json`, `build-info.json` and `SHA256SUMS`.
  Verify a download with `sha256sum -c SHA256SUMS` in the directory holding the
  files. The release page also carries the query snippet and the
  point-in-time rule, so a stranger who lands there knows what the numbers are.

## Security posture

- `permissions: contents: read` at the top of both workflows; only the
  `publish` job raises to `contents: write`, and it runs no repository code
  beyond `gh release create`.
- Neither workflow triggers on `pull_request`, `pull_request_target` or
  `push`, so a fork cannot run it and cannot read the secret.
- Every third-party action is pinned to a full commit SHA with the version in
  a comment; `SEC_USER_AGENT` is passed only to the six steps that fetch (the
  two guards, the nightly incremental fetch, fetch-text, FX and the weekly full
  fetch), never echoed, and never interpolated into a shell script — no
  `${{ }}` expression appears inside a `run` block, so no repository variable
  can reach a command line.
- No step uses `continue-on-error`, `|| true` or `set +e`: a failing command
  fails the run.

## Current state

The workflows use the Phase 2 command surface documented in
`docs/architecture.md` §12: `fetch --incremental`, `fetch-text`, `fx`,
`asof --currency USD` and the Phase 2 sections of `qa`. The commands exist on
`main`; the workflows stay disabled until `REFRESH_ENABLED` is set. The first
enabled refresh is a cold backfill (see above) and should be run by hand and
watched; after it, the nightly schedule only fetches what changed and the
weekly release re-reads everything once.
