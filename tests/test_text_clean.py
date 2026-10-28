"""Tests for the EDGAR HTML-to-text cleaner (decision A-31, issue #108).

The 27 real 8-K documents under ``tests/fixtures/sec/raw/documents`` (decisions
A-30, A-44) are the primary test surface: every one must convert, the three
hand-checked documents in ``tests/fixtures/text_expected.json`` must contain
every expected sentence and item heading, and no hidden inline-XBRL fact,
script or submission metadata may leak.  Small inputs pin each conversion rule,
and the Hypothesis properties check that output never contains markup, never
has a run of blank lines and is stable when re-cleaned.

The fixture tree holds 27 ``.htm`` documents and no ``.txt`` document; the
plain-text path is exercised by the synthetic ``.txt`` test below.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from edgarpit.text.clean import html_to_text

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SEC_ROOT = FIXTURE_ROOT / "sec"
DOCUMENTS_ROOT = SEC_ROOT / "raw" / "documents"
EXPECTED_PATH = FIXTURE_ROOT / "text_expected.json"

PROPERTY_SETTINGS = settings(deadline=None, max_examples=50)
# A real SEC document, used for the leak and table-row assertions below.
APPLE_Q1_2022 = ("0000320193", "0000320193-22-000006", "a8-kex991q1202212252021.htm")


def document_paths() -> list[Path]:
    """Return every stored fixture document, sorted by path."""
    return sorted(path for path in DOCUMENTS_ROOT.rglob("*") if path.is_file())


def document_body(path: Path) -> str:
    """Return a fixture document's bytes, decoded the way EDGAR serves it."""
    return path.read_text(encoding="utf-8", errors="replace")


def fixture_document(cik: str, accession: str, filename: str) -> Path:
    """Return the one fixture document for ``(cik, accession, filename)``."""
    matches = sorted(DOCUMENTS_ROOT.glob(f"{cik}/{accession}__{filename}/*"))
    assert len(matches) == 1, f"{accession}/{filename}: {len(matches)} stored documents"
    return matches[0]


def expected_documents() -> list[dict[str, Any]]:
    """Return the hand-checked expectations from ``text_expected.json``."""
    return json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))["documents"]


@pytest.fixture(scope="module")
def converted() -> dict[Path, str]:
    """Clean every fixture document once, keyed by path."""
    return {path: html_to_text(document_body(path)) for path in document_paths()}


def test_fixture_tree_has_only_htm_documents() -> None:
    paths = document_paths()
    assert len(paths) == 27, "the 8-K text fixture set is 27 documents"
    assert {path.suffix for path in paths} == {".htm"}
    assert not list(DOCUMENTS_ROOT.rglob("*.txt"))


def test_every_document_converts_to_well_formed_text(converted: dict[Path, str]) -> None:
    for path, text in converted.items():
        assert text, f"{path} converted to nothing"
        assert "\r" not in text, f"{path} kept a carriage return"
        assert not text.endswith("\n"), f"{path} has a trailing newline"
        assert "\n\n\n" not in text, f"{path} has a run of blank lines"
        assert text == html_to_text(document_body(path)), f"{path} is not deterministic"


@pytest.mark.parametrize(
    "entry",
    expected_documents(),
    ids=lambda entry: entry["filename"],
)
def test_hand_checked_documents_contain_expected_text(entry: dict[str, Any]) -> None:
    path = fixture_document(entry["cik"], entry["accession"], entry["filename"])
    text = html_to_text(document_body(path))
    for heading in entry["item_headings"]:
        assert heading in text, f"{entry['filename']} lost heading {heading!r}"
    for sentence in entry["sentences"]:
        assert sentence in text, f"{entry['filename']} lost sentence {sentence[:40]!r}"


def test_hidden_inline_xbrl_never_leaks(converted: dict[Path, str]) -> None:
    forbidden = (
        "us-gaap:",
        "dei:",
        "xbrli:",
        "xbrldi:",
        # The CIKs appear only inside the hidden inline-XBRL header.
        "0000320193",
        "0001594805",
    )
    for path, text in converted.items():
        for token in forbidden:
            assert token not in text, f"{path} leaked hidden XBRL token {token!r}"


def test_script_comments_and_doctype_text_never_leak(converted: dict[Path, str]) -> None:
    # Every fixture document loads this one external script; the comments and
    # the HTML 4.01 doctype live inside the SGML envelope of some exhibits.
    forbidden = (
        "/A18y7y/wfl/p5a/",
        "Document created using",
        "Copyright 2022 Workiva",
        "-//W3C//DTD HTML 4.01 Transitional//EN",
        "<TYPE>",
        "<SEQUENCE>",
    )
    for path, text in converted.items():
        for token in forbidden:
            assert token not in text, f"{path} leaked non-text token {token!r}"


def test_exhibit_sgml_envelope_is_unwrapped(converted: dict[Path, str]) -> None:
    path = fixture_document(*APPLE_Q1_2022)
    text = converted[path]
    assert "a8-kex991q1202212252021.htm" not in text
    assert text.startswith("Exhibit 99.1\n")
    assert "Apple Reports First Quarter Results" in text


def test_press_release_table_row_is_one_tab_separated_line(converted: dict[Path, str]) -> None:
    path = fixture_document(*APPLE_Q1_2022)
    lines = converted[path].split("\n")
    # Apple's Q1 2022 condensed income statement, cells kept in document order
    # with the empty spacer cells between them dropped.
    assert "Products\t$\t104,429\t$\t95,678" in lines
    row = lines.index("Products\t$\t104,429\t$\t95,678")
    assert lines[row + 1] == "Services\t19,516\t15,761"


def test_scripts_styles_and_head_content_are_removed() -> None:
    html = (
        "<html><head><title>Document</title><style>p { color: red; }</style></head>"
        "<body><script>var secret = 1;</script><p>Visible</p></body></html>"
    )
    assert html_to_text(html) == "Visible"


def test_hidden_elements_are_removed() -> None:
    html = (
        '<div style="display: none">hidden block</div>'
        '<div style="display:none"><ix:header><ix:hidden>fact</ix:hidden></ix:header></div>'
        '<p style="display: block">visible</p>'
    )
    assert html_to_text(html) == "visible"


def test_block_elements_become_line_breaks() -> None:
    html = "<div>one</div><div>two</div><h1>Three</h1><ul><li>a</li><li>b</li></ul>"
    assert html_to_text(html) == "one\ntwo\nThree\na\nb"
    assert html_to_text("<div>a<hr>b</div>") == "a\nb"


def test_break_tags_are_line_breaks_and_runs_become_one_blank_line() -> None:
    assert html_to_text("<div>a<br>b</div>") == "a\nb"
    assert html_to_text("<div>a<br><br><br>b</div>") == "a\n\nb"


def test_table_rows_become_tab_separated_lines() -> None:
    html = (
        "<table><tr><td>Net sales</td><td>$</td><td>10</td></tr>"
        "<tr><td>Cost</td><td> </td><td>4</td></tr></table>"
    )
    assert html_to_text(html) == "Net sales\t$\t10\nCost\t4"


def test_empty_table_cells_and_rows_are_dropped() -> None:
    html = "<table><tr><td></td><td> </td></tr><tr><td>x</td><td></td><td>y</td></tr></table>"
    assert html_to_text(html) == "x\ty"


def test_nested_tables_stay_on_their_outer_row() -> None:
    html = (
        "<table><tr><td>outer"
        "<table><tr><td>inner one</td><td>inner two</td></tr></table>"
        "end</td><td>last</td></tr></table>"
    )
    assert html_to_text(html) == "outer inner one inner two end\tlast"


def test_entities_and_unicode_whitespace_are_resolved() -> None:
    html = "<p>AT&amp;T &#8220;quoted&#8221;\u00a0\u00a0next\u2009line</p>"
    assert html_to_text(html) == "AT&T “quoted” next line"


def test_entities_are_resolved_without_any_tags() -> None:
    assert html_to_text("AT&amp;T &nbsp; Inc.") == "AT&T Inc."


def test_blank_lines_are_collapsed_and_edges_stripped() -> None:
    assert html_to_text("<div>a<br><br><br>b</div>") == "a\n\nb"
    assert html_to_text("<br><br>a<br><br>") == "a"


def test_empty_and_hidden_only_documents_return_empty_string() -> None:
    assert html_to_text("") == ""
    assert html_to_text(" \n\t ") == ""
    assert html_to_text('<div style="display:none"><ix:header>false</ix:header></div>') == ""
    assert html_to_text("<html><body></body></html>") == ""


def test_plain_text_document_passes_through_with_only_whitespace_normalisation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release.txt"
    path.write_text("Heading\n\n\nBody   text\twith tab  \n", encoding="utf-8")
    assert html_to_text(path.read_text(encoding="utf-8")) == "Heading\n\nBody text\twith tab"


def test_malformed_markup_never_raises() -> None:
    for html in (
        "<div><p>unclosed",
        "</table></div>stray",
        "<p>a < b and c > d</p>",
        "<table><tr><td>cell",
        "<script>never closed",
        "<p>&#xZZ; &unknown;</p>",
    ):
        assert isinstance(html_to_text(html), str)


TAG_NAMES = (
    "div",
    "p",
    "span",
    "b",
    "h1",
    "li",
    "table",
    "tr",
    "td",
    "br",
    "script",
    "style",
    "head",
    "ix:header",
    "ix:hidden",
)
TEXT = st.text(
    alphabet=st.characters(blacklist_characters="<>&\x1f"),
    max_size=20,
)
STYLES = st.sampled_from(("", ' style="display:none"', ' style="display: none"'))
ELEMENTS = st.recursive(
    TEXT,
    lambda children: st.tuples(
        st.sampled_from(TAG_NAMES),
        st.lists(children, max_size=3),
        STYLES,
    ).map(lambda parts: f"<{parts[0]}{parts[2]}>" + "".join(parts[1]) + f"</{parts[0]}>"),
    max_leaves=8,
)
DOCUMENTS = st.lists(ELEMENTS, max_size=5).map(
    lambda elements: "<html><body>" + "".join(elements) + "</body></html>"
)
TAG_IN_MARKUP = re.compile(r"<\s*/?\s*([A-Za-z][A-Za-z0-9:._-]*)")


@given(DOCUMENTS)
@PROPERTY_SETTINGS
def test_output_never_contains_a_tag_from_the_input(html: str) -> None:
    text = html_to_text(html)
    for name in TAG_IN_MARKUP.findall(html):
        assert f"<{name.lower()}" not in text.lower()


@given(DOCUMENTS)
@PROPERTY_SETTINGS
def test_output_never_has_two_consecutive_blank_lines(html: str) -> None:
    assert "\n\n\n" not in html_to_text(html)


@given(DOCUMENTS)
@PROPERTY_SETTINGS
def test_recleaning_the_plain_output_is_stable(html: str) -> None:
    text = html_to_text(html)
    assert html_to_text(text) == text
