# Epic 04 — 8-K text

## Goal

Epic 02 made the fundamentals point-in-time and Epic 03 shipped them. This epic adds the other half of the corporate record: the words of an 8-K. It ends when every 8-K/8-K/A selected by an item can be read as clean, item-labelled text whose knowledge time is the filing's acceptance time, and when that text is a first-class table in the offline build and the release.

The epic ends when:

- the staging contract freezes `raw.filings_text`, the `filing_index` and `documents` raw-cache endpoints and the four text function signatures, so discovery, cleaning, segmentation and ingest can be built in parallel (#99);
- real 8-K documents and exhibits are fixtures, stored byte-for-byte with hand-checked expectations (#100);
- the filing's `-index-headers.html` page yields exactly its primary 8-K/8-K/A document and every `EX-99.1` exhibit, after an exhibit-type survey fixes the normalisation rule (#107);
- HTML becomes plain text with scripts, styles and hidden XBRL removed and each table row one tab-separated line (#108);
- the primary document is split on the filing's own item headings, and left whole when that split cannot be made with certainty (#109);
- `edgarpit fetch-text` caches filing indexes and documents, and `edgarpit build` ingests them into `raw.filings_text` (#110);
- the text fixtures merge into the one SEC fixture cache, so the offline build produces every Phase 2 table on real bytes (#132).

Everything below describes the merged code. The commands were run against the fixture cache at the commit that added this document; the counts are from that build, not from a live fetch (the 250-issuer live run is recorded in [`docs/epic-06-universe-incremental.md`](epic-06-universe-incremental.md)).

## How it works

### Finding the documents (decision A-30, issue #107, PR #126)

A filing's `index.json` has no document types, so it cannot name the primary document or an exhibit. The text pipeline starts one level up, at

```
https://www.sec.gov/Archives/edgar/data/<cik-int>/<acc-no-dashes>/<accession>-index-headers.html
```

which lists one `<DOCUMENT>` block per file with an SGML `<SEQUENCE>`, `<TYPE>` and `<FILENAME>`. `edgarpit.text.discover.exhibit_documents(index_headers_html)` unescapes the page, checks that the `<DOCUMENT>` tags balance and that every block has a usable sequence, type and file name, and returns:

1. the single document whose `<TYPE>` is `8-K` or `8-K/A`, as `DocumentRef(doc_type="primary")`;
2. every document whose `<TYPE>` names exhibit 99.1, in `<SEQUENCE>` order, as `DocumentRef(doc_type="EX-99.1")`.

Everything else is ignored: the index page itself, the `.hdr.sgml` header, the XBRL instance, schema, linkbase and rendering files, `R*.htm` viewer pages, graphics, and every other exhibit type. A page with no primary 8-K document, two of them, conflicting accession numbers or an unusable block raises `ValueError` naming the accession, because guessing which document is the filing is worse than stopping the build.

**The exhibit-type survey.** Before settling the rule, the pages were read: the 15 fixture pages, 49 live `-index-headers.html` pages of 8-K/8-K/A filings from 2015 to 2025, and 44 legacy `-index.htm` pages from 2003 to 2013. The issuers were Apple, Shopify, Berkshire Hathaway, Microsoft, Tesla, JPMorgan, Pfizer, Coca-Cola and Walmart; the documents came from in-house filers and from DFIN, RR Donnelley, Toppan Merrill and Broadridge. The exhibit types seen were `EX-99.1` (62), `EX-99.2` (9) and `EX-99` (1); no other spelling occurred.

The rule therefore normalises rather than matching one string: after case, surrounding whitespace, the separator after `99` (`.`, `-`, `_` or a space), an optional pair of parentheses and zero padding are removed, the type must be exactly exhibit 99.1. `EX-99.01`, `EX-99.(1)`, `EX-99-1` and `EX-99_1` all become `EX-99.1`; bare `EX-99` is ambiguous with the whole exhibit-99 series and `EX-99.2` is a different exhibit, so both are ignored, as is `EX-99.1A`. `doc_type` is always the normalised value, so downstream code never sees a filer's spelling.

### Cleaning HTML (decision A-31, issue #108, PR #127)

`edgarpit.text.clean.html_to_text(html)` parses with BeautifulSoup's lxml parser, so malformed markup never raises. It:

- unwraps EDGAR's SGML document envelope (`<DOCUMENT>` … `<TEXT>` … `</TEXT>`) first; most exhibits are served that way and the envelope's `<TYPE>`/`<SEQUENCE>`/`<FILENAME>` lines are submission metadata, not filing prose;
- drops `script`, `style`, `head`, `ix:header` and every element whose inline style contains `display:none`; hidden inline-XBRL facts and Workiva's hidden blocks never reach the text;
- turns block elements (`p`, `div`, headings, list items, table rows, `<br>`) into line breaks and table cells into tabs, so each table row is one tab-separated line; a nested table renders inline inside its outer cell and cannot split the outer row;
- resolves entities, collapses Unicode whitespace inside a line or cell, drops empty cells and blank leading/trailing lines, and collapses a run of blank lines to one;
- treats an input with no tags and no entities as plain text, keeping its line structure (tested with a synthetic `.txt` case; all 27 fixture documents are `.htm`);
- returns `\n` line endings, no trailing newline, and `""` for a document with no visible text.

The conversion is deterministic: the same bytes always produce the same text. The trade-off is explicit — empty table cells are dropped, which can shift a column when a legitimate value is blank; the contract says to drop them and the fixture rows are consistent with that.

### Segmenting (decision A-31, decision A-40, issue #109, PR #124)

`edgarpit.text.segment.segment_items(text, expected_items)` splits the cleaned **primary** document. A heading is the word `Item` (any case), optionally indented, then whitespace, a dotted code (`2.02`), an optional period and a word boundary, **at the start of a line**. A reference in running prose ("as described in Item 2.02 above") is not a heading. Only dotted codes can be headings, so a filing that declares the legacy pre-2004 numeric items (`5`, `7`, `12`) never segments.

The split is confident only when the ordered list of codes found in the document **equals** the filing's declared item list: every item once, in the declared order, no duplicate, no undeclared heading, no missing item. That one equality is the confidence rule; anything else returns a single `Section(section='unsegmented', order=1, text=text.strip())`. A wrong split would attribute prose to the wrong event, so the fallback is the whole document, never a partial guess.

The sections' text runs from one heading to the next; the preamble before the first heading (form title, registrant block) stays at the start of the first section, so no text is lost. The reconstruction rule is exact at two levels: the concatenated whitespace-separated tokens always equal `text.split()`, and for canonical text (no leading/trailing spaces, no blank lines) `"\n".join(section.text)` equals `text.strip()` character for character.

An `EX-99.1` exhibit is never segmented (decision A-40). It carries no items of its own, and its headings often describe the press release's sections rather than the 8-K's; it becomes one `unsegmented` row. `section` therefore names either an EDGAR item code (`2.02`, `9.01`) or `unsegmented`, which is exactly what `FilingsTextSchema` allows.

### Fetching and ingesting (decisions A-30, A-37, A-40; issue #110, PR #125)

The work is split in two so the database stays a pure function of the raw cache (decision A-37).

**`edgarpit fetch-text --db PATH --items 2.02 [--items ...] [--limit N] [--data DIR]`** only fetches. It opens the built database read-only and selects `(cik, accession)` from `raw.events_8k` where the form is `8-K`/`8-K/A` and any item matches, ordered by `acceptance_ts` then `accession`; a filing is selected once even when several of its items match, and `--limit` keeps the first N. For each selection it stores the `-index-headers.html` page under `raw/filing_index/<cik10>/<accession>/` and every document `exhibit_documents` names under `raw/documents/<cik10>/<accession>__<filename>/`. A cached key is read from the cache and never requested again, so a second run over the same cache makes no HTTP request at all. A 404 on one index page or document is recorded in `TextFetchReport.failed` and the run continues; every other `SecClientError` propagates, so a broken SEC response stops the run instead of leaving a silently partial cache. The command prints the five counts of `TextFetchReport` and one `not found:` line per 404. A database without `raw.events_8k` exits 1; a missing or malformed `SEC_USER_AGENT` exits 2 before any request, because the client is built before the database is read.

**`edgarpit build`** ingests. `edgarpit.text.ingest.ingest_text(raw, cik, accessions, filings)` reads the latest cached index for each accession, re-runs discovery, reads every referenced document, cleans it, and segments the primary document on the accession's own `raw.filings.items` (`"2.02,9.01"`, in declared order). Every `EX-99.1` becomes one `unsegmented` row. The result has one row per `(document, section)`, sorted by `(accession, document, section_order)`, with `acceptance_ts` and `ts_imputed` copied from `raw.filings`, `sha256` of the stored text's UTF-8 bytes and `char_count` of its characters. Nothing inside the document — its dates, its period, its dateline — influences the knowledge time. An accession with no cached index page is skipped and counted in one `INFO` line; a document the cached index lists but the cache lacks raises `LookupError` naming the key, so a partial cache never becomes a silently incomplete table. When the cache holds no `filing_index` entry at all, `build` creates `raw.filings_text` empty, so a Phase 1 cache still builds.

### Reading the text (issue #113, PR #128)

`main.filings_text` is `raw.filings_text` plus `ticker` from `raw.universe`. `query.filing_text(identifier, *, item=None, start=None, end=None, db=None)` returns sections ordered by `acceptance_ts`, `accession`, `document`, `section_order`; `item="2.02"` keeps **every section and exhibit of each filing whose `main.events_8k` rows carry that item**, so Item 2.02 text is the join of the two tables on accession, not a filter on `section`. The REST service exposes the same rows at `GET /text`, and the explorer lists each Item 2.02 filing with its sections and exhibits. A database without `main.filings_text` raises `query.MissingTableError`, which REST answers as 501.

### Fixture evidence (issue #100, PR #104; issue #132, PR #136)

`tests/fixtures/sec/raw` holds 15 filing-index pages and 27 documents — 15 primaries and 12 `EX-99.1` exhibits — stored exactly as SEC served them (2.95 MB). The selection spans Apple (11 filings, including the only 8-K/A and two other non-2.02 filings that carry an item without an exhibit) and Shopify (4 filings, including a three-item report). Royal Bank of Canada files 6-Ks and has no 8-K or 8-K/A row in the submissions fixture, so it contributes no text fixture; `tests/test_fixtures_text.py` asserts that absence rather than assuming it. `tests/fixtures/text_expected.json` records the item headings and three hand-checked sentences per document for three primaries, and the section each sentence must land in.

A fixture build is the end-to-end proof:

```bash
uv run edgarpit build --offline --fixtures tests/fixtures/sec --out ./build
```

```
raw.filings_text: 42 rows
main.filings_text: 42 rows
dbt build: PASS=97 WARN=0 ERROR=0 SKIP=0 TOTAL=97
```

(Abridged: these three lines are quoted exactly from the build summary.)

The 42 rows are the 15 primary documents' 30 item sections plus the 12 exhibits' single `unsegmented` rows each. The primary sections are `2.02` ×12, `5.02` ×2, `5.03` ×1, `7.01` ×2 and `9.01` ×13. All 15 primaries segmented confidently: `edgarpit qa` reports `primary documents: 15, unsegmented: 0 (0.0%)`, `zero-length sections: 0` and `duplicate sha256 across accessions: 0`. The same QA section reports 26 Item 2.02 filings in the fixture build and 14 without text (11 Apple, 3 Shopify), which is expected: the fixture cache holds text for 12 of the 26, and the live run fetches the rest.

`tests/test_e2e_phase2.py` pins the evidence on that build: every indexed filing appears in `raw.filings_text`; each primary document's sections are exactly the filing's declared item codes in order; every other cached document of the filing is an `EX-99.1` with one `unsegmented` row; `acceptance_ts` and `ts_imputed` equal the `raw.filings` row for every section; and the hand-checked sentences land in their semantically correct sections. The same module also proves two exports of the release are byte-identical.

## Key decisions and reasoning

- **The index page, not `index.json`.** `index.json` lists file names without types, so it cannot distinguish the primary 8-K from an exhibit. The `-index-headers.html` page carries the SGML `<TYPE>` the filer declared, which is the only reliable evidence (decision A-30).
- **Normalise exhibit types instead of matching `EX-99.1` exactly.** The survey saw only `EX-99.1`, `EX-99.2` and bare `EX-99`, and the rule also accepts the separator, parenthesis and zero-padding variants an agent could produce; an exact match would silently drop an exhibit written `EX-99.01` (issue #107).
- **Segment only when the headings are exactly the declared items.** A split that loses a heading or invents one would attribute prose to the wrong event. The equality check makes a wrong split impossible by construction, at the cost of leaving a document that cannot be split confidently whole (issue #109).
- **Exhibits are never segmented.** An exhibit has no items of its own; its headings are the press release's, not the 8-K's (decision A-40, issue #110).
- **Fetching and building are separate commands.** `fetch-text` writes only the cache and `build` writes only the database, so the database remains a pure function of the cache and a failed build never leaves text fetched into one database but not another (decision A-37, issue #131).
- **Fetched documents are immutable, so they are never re-fetched.** A warm cache makes no request; only a missing key costs a request (decision A-30).
- **The text fixtures were a separate root first, then merged.** Keeping the Phase 1 fixture manifest unchanged while the text work landed avoided a three-way conflict; decision A-44 merged the roots once ingest existed, and the manifest now holds 499 rows (issue #132).
- **A section's of-record time is its filing's acceptance time, never a date in the text.** A press release is public when the 8-K carrying it is accepted; its dateline is not evidence (decision A-31).
- **Bare `EX-99` is ignored.** It could be any exhibit in the 99 series, and the survey could not establish that it means 99.1; storing the wrong document as the press release would be worse than storing none (issue #107).

## Interfaces for future epics

- **`edgarpit.text.discover.DocumentRef`** — frozen dataclass `(accession, filename, doc_type)`; `doc_type` is `primary` or `EX-99.1`.
- **`edgarpit.text.discover.exhibit_documents(index_headers_html) -> list[DocumentRef]`** — primary first, exhibits in sequence order; raises `ValueError` for a page it cannot trust.
- **`edgarpit.text.clean.html_to_text(html) -> str`** — deterministic plain text; never raises for malformed HTML.
- **`edgarpit.text.segment.Section`** and **`segment_items(text, expected_items) -> list[Section]`** — sections named by item code or `unsegmented`, 1-based order.
- **`edgarpit.text.ingest.ingest_text(raw, cik, accessions, filings) -> pl.DataFrame`** — a `FilingsTextSchema` frame, or the empty contract frame.
- **`edgarpit.text.fetch.select_text_accessions(db, items, *, limit=None)`**, **`fetch_text_documents(db, data_dir, items, *, limit=None, client=None)`** and **`TextFetchReport(filings, indexes_fetched, documents_fetched, already_cached, failed)`**.
- **Raw cache** — `filing_index` keyed by accession (`.html`), `documents` keyed `<accession>__<filename>` with the document's own extension; both immutable.
- **Tables** — `raw.filings_text` (the staging contract) and `main.filings_text` (+ `ticker`); `query.filing_text`, `GET /text`.
- **CLI** — `edgarpit fetch-text --db PATH --items ITEM [--items ...] [--limit N] [--data DIR]`.
- **QA** — `TextCoverage` reports Item 2.02 filings without text, the top issuers by missing count, the unsegmented share of primary documents, zero-length sections and duplicate hashes across accessions.

## Deviations from the plan

- **The text fixtures started in `tests/fixtures/sec_text` and moved later.** The issue that created them asked for a separate root so the Phase 1 manifest stayed untouched; decision A-44 merged them into `tests/fixtures/sec/raw` once ingest existed, and the manifest rows moved unchanged because they are root-relative.
- **The text fixture manifest says `"trimmed": false`.** The rows keep the A-15 shape, but these bodies are stored exactly as SEC served them, so calling them trimmed would be false. The reader ignores the flag.
- **The cleaner unwraps EDGAR's SGML envelope and passes plain text through.** The served bytes of every exhibit include the envelope, and a plain-text input must not be parsed as HTML; both readings are needed for the output to be filing prose rather than metadata.
- **Empty table cells are dropped.** This is what the contract says, and it can shift columns on a table with a legitimately blank cell. The stored event text is still complete at the token level.
- **Only three primary documents are hand-checked sentence by sentence.** The other twelve are pinned structurally — item headings, section counts, exhibit file names — because hand-writing expectations for every document would be a second transcription of the fixtures rather than an independent check.
- **The Phase 2 staging tables were first held in `PHASE2_SCHEMAS` and later promoted.** Issue #99 kept them out of the Phase 1 contract to avoid changing it mid-wave; issue #112 moved `filings_text` and `fx_daily` into `SCHEMAS`/`COLUMNS`/`DUCKDB_TYPES` when their loader landed (decision A-38).
- **The Phase 2 stub check was retired.** Once every stub had an implementation, `test_phase2_stubs_raise_not_implemented` could only assert the opposite of the truth; it was deleted while every signature and dataclass pin stayed (decision A-43).

## Known limitations and deferred items

- **8-K text starts in 2014.** EDGAR's `-index-headers.html` endpoint returns 200 for 2014 filings but 404 for 2012 and 2013 (checked on Apple, Pfizer, Walmart and Microsoft). Older filings have items in `events_8k` but no text. The legacy `-index.htm` page exists for 2003–2013 and was read only as survey evidence; it is not a production input.
- **Only the primary document and `EX-99.1` are stored.** `EX-99.2`, bare `EX-99`, other exhibits, the full submission and the XBRL instance are not. A filing whose press release is tagged `EX-99.2` therefore has no exhibit text, though its primary document is still stored.
- **Segmentation is heading-based and conservative.** A document whose headings do not match its declared items stays one `unsegmented` row. There is no fallback that recognises an item by prose, and no table extraction into fields.
- **The text is plain, not structured.** A financial table is tab-separated lines; the pipeline does not infer column headers or units. That is Phase 3 extraction work, not this table.
- **The fixture cache is not the universe.** 26 Item 2.02 filings exist in the fixture build and 12 have text; the gap is closed by `fetch-text` on live data, not by the fixtures.
- **No full-text index.** `text` is a `VARCHAR`; a release reader scans or filters on the other columns. DuckDB's full-text search extension is not a dependency.
- **A document the cached index names but the cache does not hold stops the build.** `ingest_text` raises `LookupError` naming the missing key rather than skipping it; a partial cache is a build failure, never a silently short table.
- **Text coverage is reported, not enforced.** QA counts Item 2.02 filings without text and exits non-zero only on a key violation or an `asof_ts` leak, so an incomplete text fetch is visible but not fatal.
