"""Fixture-tree checks for the real SEC 8-K documents under tests/fixtures/sec.

These tests never touch the network.  They verify the tree produced by
``tools/build_fixtures.py --text`` on the merged SEC fixture cache (decisions
A-30 and A-44): the decision A-30 layout of the ``filing_index`` and
``documents`` endpoints, the manifest hashes and URLs, the required Item 2.02 /
non-2.02 / amendment selection from the submissions fixtures, that every
``-index-headers.html`` page accounts for exactly the documents stored for its
filing, the size budget, and that ``tests/fixtures/text_expected.json`` is
consistent with the stored bytes.

The extractor at the bottom is a reference used only to confirm that the
hand-checked headings and sentences are visible text rather than markup; the
production HTML-to-text implementation is not part of this fixture issue.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from edgarpit.rawcache import RawCache

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SEC_ROOT = FIXTURE_ROOT / "sec"
TEXT_RAW_DIR = SEC_ROOT / "raw"
MANIFEST_PATH = TEXT_RAW_DIR / "manifest.jsonl"
EXPECTED_PATH = FIXTURE_ROOT / "text_expected.json"

REPO_ROOT = Path(__file__).resolve().parents[1]

APPLE_CIK = "0000320193"
SHOPIFY_CIK = "0001594805"
RY_CIK = "0001000275"

EVENT_FORMS = frozenset({"8-K", "8-K/A"})
SIZE_BUDGET_BYTES = 20 * 1024 * 1024
TEXT_ENDPOINTS = frozenset({"filing_index", "documents"})

STAMP_RE = re.compile(r"\d{8}T\d{6}\d{6}Z")
ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")
FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def load_fixture_builder() -> ModuleType:
    """Import ``tools/build_fixtures.py`` by path (it is not a package)."""
    path = REPO_ROOT / "tools" / "build_fixtures.py"
    spec = importlib.util.spec_from_file_location("text_fixture_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_manifest() -> list[dict[str, Any]]:
    """Return the 8-K text rows of the shared SEC fixture manifest (decision A-15)."""
    rows = [
        json.loads(line)
        for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    text_rows = [row for row in rows if row["endpoint"] in TEXT_ENDPOINTS]
    assert text_rows, "tests/fixtures/sec/raw/manifest.jsonl lists no 8-K text rows"
    return text_rows


def accession_of(row: dict[str, Any]) -> str:
    """Return the accession encoded in a text manifest row's path."""
    parts = row["path"].split("/")
    if row["endpoint"] == "filing_index":
        return parts[3]
    return parts[3].split("__", 1)[0]


def filename_of(row: dict[str, Any]) -> str:
    """Return the document filename encoded in a text manifest row's path."""
    assert row["endpoint"] == "documents", row
    return row["path"].split("/")[3].split("__", 1)[1]


def stored_path(row: dict[str, Any]) -> Path:
    """Return the on-disk path of a text manifest row."""
    return SEC_ROOT / row["path"]


def selected_filings() -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group the text manifest rows by ``(cik, accession)``."""
    filings: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in load_manifest():
        filings.setdefault((row["cik"], accession_of(row)), []).append(row)
    return filings


def submissions_rows(cik: str) -> dict[str, dict[str, Any]]:
    """Return the kept submissions-fixture rows of ``cik`` keyed by accession."""
    base = SEC_ROOT / "raw"
    paths = sorted((base / "submissions" / cik).glob("*.json"))
    paths += sorted((base / "submissions_page" / cik).glob("*/*.json"))
    rows: dict[str, dict[str, Any]] = {}
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        filings = document.get("filings")
        arrays = (
            filings["recent"] if isinstance(filings, dict) and "recent" in filings else document
        )
        for index in range(len(arrays["accessionNumber"])):
            rows[arrays["accessionNumber"][index]] = {
                key: arrays[key][index] for key in arrays if isinstance(arrays[key], list)
            }
    return rows


def items_of(row: dict[str, Any]) -> set[str]:
    """Return the comma-separated 8-K item codes of a submissions row."""
    return {item.strip() for item in (row.get("items") or "").split(",") if item.strip()}


def index_documents(row: dict[str, Any]) -> list[Any]:
    """Parse a stored ``-index-headers.html`` page's document list."""
    assert row["endpoint"] == "filing_index", row
    text = stored_path(row).read_text(encoding="utf-8", errors="replace")
    return load_fixture_builder().parse_index_documents(text)


def test_text_manifest_and_stored_files_agree() -> None:
    rows = load_manifest()
    listed = {row["path"] for row in rows}
    stored = {
        path.relative_to(SEC_ROOT).as_posix()
        for endpoint in TEXT_ENDPOINTS
        for path in (TEXT_RAW_DIR / endpoint).rglob("*")
        if path.is_file()
    }
    assert stored == listed, "text manifest and stored files disagree"
    for row in rows:
        parts = row["path"].split("/")
        assert parts[0] == "raw"
        assert row["endpoint"] in {"filing_index", "documents"}
        assert row["trimmed"] is False, "SEC bytes must be stored unmodified"
        assert STAMP_RE.fullmatch(parts[-1].split(".")[0]), row["path"]
        assert row["fetched_at"] == parts[-1].split(".")[0]
        assert re.fullmatch(r"\d{10}", row["cik"])
        assert ACCESSION_RE.fullmatch(accession_of(row)), row["path"]
        if row["endpoint"] == "filing_index":
            assert parts[1:4] == ["filing_index", row["cik"], accession_of(row)]
            assert parts[-1].endswith(".html")
        else:
            assert parts[1:3] == ["documents", row["cik"]]
            assert FILENAME_RE.fullmatch(filename_of(row)), row["path"]


def test_text_manifest_hashes_match_files() -> None:
    for row in load_manifest():
        body = stored_path(row).read_bytes()
        assert len(body) == row["bytes"], row["path"]
        assert hashlib.sha256(body).hexdigest() == row["sha256"], row["path"]
        assert row["http_status"] == 200, row["path"]


def test_text_manifest_urls_are_real_sec_archive_urls() -> None:
    for row in load_manifest():
        accession = accession_of(row)
        directory = (
            f"https://www.sec.gov/Archives/edgar/data/{int(row['cik'])}/"
            f"{accession.replace('-', '')}"
        )
        if row["endpoint"] == "filing_index":
            assert row["url"] == f"{directory}/{accession}-index-headers.html"
        else:
            assert row["url"] == f"{directory}/{filename_of(row)}"


def test_required_selection_covers_the_acceptance_criteria() -> None:
    submissions = {cik: submissions_rows(cik) for cik in (APPLE_CIK, SHOPIFY_CIK, RY_CIK)}
    assert not [row for row in submissions[RY_CIK].values() if row["form"] in EVENT_FORMS], (
        "Royal Bank of Canada files 6-Ks, so it has no 8-K text fixture"
    )

    counts = {"apple_2_02": 0, "shopify_2_02": 0, "other_8k": 0, "amendment": 0}
    for cik, accession in selected_filings():
        row = submissions[cik].get(accession)
        assert row is not None, f"{accession} is not in the submissions fixtures"
        items = items_of(row)
        if row["form"] == "8-K/A":
            assert "2.02" not in items
            counts["amendment"] += 1
        elif "2.02" in items:
            assert row["form"] == "8-K"
            counts["apple_2_02" if cik == APPLE_CIK else "shopify_2_02"] += 1
        else:
            assert row["form"] == "8-K"
            counts["other_8k"] += 1

    assert counts["apple_2_02"] >= 8, counts
    assert counts["shopify_2_02"] >= 4, counts
    assert counts["other_8k"] >= 2, counts
    assert counts["amendment"] >= 1, counts


def test_every_selected_filing_has_its_index_primary_and_exhibits() -> None:
    submissions = {cik: submissions_rows(cik) for cik in (APPLE_CIK, SHOPIFY_CIK)}
    for (cik, accession), rows in selected_filings().items():
        index_rows = [row for row in rows if row["endpoint"] == "filing_index"]
        assert len(index_rows) == 1, f"{accession} has {len(index_rows)} index pages"
        submission = submissions[cik][accession]
        documents = index_documents(index_rows[0])

        expected = {submission["primaryDocument"]}
        expected |= {
            document.filename for document in documents if document.document_type == "EX-99.1"
        }
        assert {filename_of(row) for row in rows if row["endpoint"] == "documents"} == expected

        primary = [
            document for document in documents if document.document_type == submission["form"]
        ]
        assert [document.filename for document in primary] == [submission["primaryDocument"]]
        if "2.02" in items_of(submission):
            assert any(document.document_type == "EX-99.1" for document in documents), (
                f"{accession} has Item 2.02 but no EX-99.1"
            )


def test_index_headers_lists_the_stored_document_types() -> None:
    submissions = {cik: submissions_rows(cik) for cik in (APPLE_CIK, SHOPIFY_CIK)}
    for (cik, accession), rows in selected_filings().items():
        index_row = next(row for row in rows if row["endpoint"] == "filing_index")
        listed = {document.filename: document for document in index_documents(index_row)}
        form = submissions[cik][accession]["form"]
        for row in rows:
            if row["endpoint"] != "documents":
                continue
            filename = filename_of(row)
            assert filename in listed, f"{accession}: {filename} is not in the index"
            assert listed[filename].document_type in {
                form,
                "EX-99.1",
            }, f"{accession}: {filename} has type {listed[filename].document_type}"


def stored_document_types() -> dict[tuple[str, str, str], str]:
    """Map ``(cik, accession, filename)`` to the ``<TYPE>`` its index lists."""
    types: dict[tuple[str, str, str], str] = {}
    for (cik, accession), rows in selected_filings().items():
        index_row = next(row for row in rows if row["endpoint"] == "filing_index")
        for document in index_documents(index_row):
            types[(cik, accession, document.filename)] = document.document_type
    return types


def test_text_expected_matches_the_stored_filings() -> None:
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    checked = expected["checked"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked["checked_on"])
    assert len(checked["method"]) >= 3
    assert all(isinstance(line, str) and line for line in checked["method"])

    selected = selected_filings()
    document_types = stored_document_types()
    documents = expected["documents"]
    assert len(documents) == 3, "three documents carry hand-checked expectations"

    for entry in documents:
        key = (entry["cik"], entry["accession"])
        assert key in selected, key
        assert entry["kind"] in {"primary", "exhibit_99_1"}
        assert FILENAME_RE.fullmatch(entry["filename"])
        assert (entry["cik"], entry["accession"], entry["filename"]) in document_types
        row = next(
            row
            for row in selected[key]
            if row["endpoint"] == "documents" and filename_of(row) == entry["filename"]
        )
        text = " ".join(
            visible_text(stored_path(row).read_text(encoding="utf-8", errors="replace")).split()
        )
        assert entry["item_headings"], f"{entry['filename']} has no expected item headings"
        for heading in entry["item_headings"]:
            assert heading.startswith("Item "), heading
            assert heading in text, (entry["filename"], heading)
        assert len(entry["sentences"]) >= 3
        for sentence in entry["sentences"]:
            assert sentence == " ".join(sentence.split()), "expected sentences are normalised"
            assert len(sentence.split()) >= 6, sentence
            assert sentence in text, (entry["filename"], sentence)

    assert len({(entry["cik"], entry["accession"]) for entry in documents}) == 3
    assert {entry["cik"] for entry in documents} == {APPLE_CIK, SHOPIFY_CIK}
    assert all(entry["kind"] == "primary" for entry in documents)

    exhibits = expected["exhibit_99_1"]
    assert set(exhibits) == {accession for _cik, accession in selected}
    for (cik, accession), rows in selected.items():
        stored_exhibits = sorted(
            filename_of(row)
            for row in rows
            if row["endpoint"] == "documents"
            and document_types[(cik, accession, filename_of(row))] == "EX-99.1"
        )
        assert exhibits[accession] == stored_exhibits, accession


def test_stored_selection_matches_the_builder() -> None:
    expected = {(cik, accession) for cik, accession in load_fixture_builder().text_selection()}
    stored = {
        (row["cik"], accession_of(row))
        for row in load_manifest()
        if row["endpoint"] == "filing_index"
    }
    assert stored == expected


def test_build_text_writes_into_the_merged_cache_and_keeps_other_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``--text`` run reproduces the committed text rows in the merged cache.

    The builder runs on a temporary root that holds only the submissions, page
    and ``company_tickers`` fixtures, with HTTP served from the committed bytes.
    It must add the ``filing_index`` and ``documents`` endpoints to that root's
    manifest, reproduce every committed text row byte for byte, keep the other
    endpoints, and replace its own rows on a second run instead of duplicating
    them (decision A-44).
    """
    builder = load_fixture_builder()
    committed = builder.load_manifest_rows()
    text_rows = [row for row in committed if row["endpoint"] in TEXT_ENDPOINTS]
    bodies = {row["url"]: (SEC_ROOT / row["path"]).read_bytes() for row in text_rows}
    kept_endpoints = ("submissions", "submissions_page", "company_tickers")

    root = tmp_path / "sec"
    kept_rows = []
    for row in committed:
        if row["endpoint"] not in kept_endpoints:
            continue
        target = root / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SEC_ROOT / row["path"]).read_bytes())
        kept_rows.append(row)
    (root / "raw" / "manifest.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in kept_rows), encoding="utf-8"
    )
    monkeypatch.setattr(builder, "now_stamp", lambda: "20261028T000000000000Z")
    monkeypatch.setattr(builder, "_fetch", lambda url, *, agent, limiter: (200, bodies[url]))

    summary = builder.build_text(
        agent="Test User test@example.com", limiter=builder.RateLimiter(1.0), root=root
    )

    assert summary["filings"] == 15
    assert summary["documents"] == 27
    assert summary["exhibits"] == 12
    written = builder.load_manifest_rows(root)
    assert {row["endpoint"] for row in written} == set(kept_endpoints) | TEXT_ENDPOINTS
    assert [row for row in written if row["endpoint"] == "company_tickers"] == [
        row for row in kept_rows if row["endpoint"] == "company_tickers"
    ]
    new_text_rows = [row for row in written if row["endpoint"] in TEXT_ENDPOINTS]
    assert len(new_text_rows) == len(text_rows)
    committed_by_url = {row["url"]: row for row in text_rows}
    assert {row["url"] for row in new_text_rows} == set(committed_by_url)
    for row in new_text_rows:
        original = committed_by_url[row["url"]]
        assert row["sha256"] == original["sha256"], row["url"]
        assert row["bytes"] == original["bytes"], row["url"]
        assert row["trimmed"] is False
        assert row["path"].split("/")[1:-1] == original["path"].split("/")[1:-1]
        assert (root / row["path"]).read_bytes() == (SEC_ROOT / original["path"]).read_bytes()

    report = RawCache(root).verify()
    assert not report.missing and not report.mismatched and not report.unlisted

    second = builder.build_text(
        agent="Test User test@example.com", limiter=builder.RateLimiter(1.0), root=root
    )
    assert second["documents"] == 27
    assert len(builder.load_manifest_rows(root)) == len(written)


def test_fixture_documents_cover_the_formats_the_parser_must_survive() -> None:
    bodies = [
        stored_path(row).read_text(encoding="utf-8", errors="replace")
        for row in load_manifest()
        if row["endpoint"] == "documents"
    ]
    # Apple's Workiva-filed 8-Ks hide their inline XBRL in display:none blocks.
    assert any("ix:header" in body and "display:none" in body for body in bodies)
    # The press releases carry wide financial tables.
    assert any(body.count("<tr") >= 50 for body in bodies)
    # One amendment and at least one primary document from each issuer layout.
    assert any("FORM 8-K/A" in visible_text(body) for body in bodies)
    assert sum(1 for body in bodies if "FORM 8-K" in visible_text(body)) >= 5


def test_total_fixture_size_budget() -> None:
    total = sum(path.stat().st_size for path in FIXTURE_ROOT.rglob("*") if path.is_file())
    assert total < SIZE_BUDGET_BYTES, f"fixtures are {total / 1e6:.1f} MB, budget is 20 MB"


class _VisibleText(HTMLParser):
    """Collect character data outside hidden elements.

    ``convert_charrefs`` decodes the HTML entities, so the collected text is
    what a reader sees: ``&amp;`` becomes ``&`` and ``&#8220;`` a curly quote.
    Tags whose style hides them (inline XBRL headers, ``display:none``) and
    ``script``/``style``/``head`` content are skipped.  Block-level tags add a
    line break and table cells a tab, so joining the result approximates the
    flattened text the production parser must produce.
    """

    _SKIP_TAGS = frozenset(
        {
            "script",
            "style",
            "head",
            "ix:header",
            "ix:hidden",
            "ix:references",
            "ix:resources",
            "ix:exclude",
        }
    )
    _BLOCKS = frozenset(
        {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "ul", "ol", "hr"}
    )
    _CELLS = frozenset({"td", "th"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, bool]] = []
        self._skipping = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        style = (dict(attrs).get("style") or "").replace(" ", "").lower()
        hidden = tag in self._SKIP_TAGS or "display:none" in style or "visibility:hidden" in style
        if hidden:
            self._skipping += 1
        self._stack.append((tag, hidden))
        if tag in self._BLOCKS:
            self.parts.append("\n")
        if tag in self._CELLS:
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if self._stack:
            _tag, hidden = self._stack.pop()
            if hidden and self._skipping:
                self._skipping -= 1
        if tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def visible_text(html: str) -> str:
    """Return the visible text of ``html``, one block per line, cells tabbed."""
    parser = _VisibleText()
    parser.feed(html)
    parser.close()
    lines = []
    for line in "".join(parser.parts).replace("\xa0", " ").splitlines():
        line = " ".join(line.split())
        if line:
            lines.append(line)
    return "\n".join(lines)
