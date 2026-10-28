"""8-K document discovery from the filing's ``-index-headers.html`` page.

The fixture tree is the real SEC responses under ``tests/fixtures/sec_text``
(decision A-30); the expected exhibit file names are the hand-checked ones in
``tests/fixtures/text_expected.json``. No test here touches the network.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from edgarpit.text.discover import DocumentRef, exhibit_documents

FIXTURES = Path(__file__).parent / "fixtures"
TEXT_RAW = FIXTURES / "sec_text" / "raw"
INDEX_ROOT = TEXT_RAW / "filing_index"
DOCUMENTS_ROOT = TEXT_RAW / "documents"
EXPECTED = json.loads((FIXTURES / "text_expected.json").read_text(encoding="utf-8"))
EXPECTED_EXHIBITS: dict[str, list[str]] = EXPECTED["exhibit_99_1"]
EXPECTED_PRIMARY: dict[str, str] = {
    entry["accession"]: entry["filename"] for entry in EXPECTED["documents"]
}

ACCESSION = "0000320193-22-000006"
AMENDMENT = "0001140361-26-035325"
EXHIBIT_LESS = ("0001193125-22-225365", "0001140361-24-040659")

_DOCUMENT_BLOCK_RE = re.compile(r"<DOCUMENT>.*?</DOCUMENT>", re.DOTALL)


def index_pages() -> dict[str, Path]:
    """Return the fixture index page of every accession, keyed by accession."""
    pages: dict[str, Path] = {}
    for path in sorted(INDEX_ROOT.rglob("*.html")):
        accession = path.parent.name
        assert accession not in pages, f"two index pages for {accession}"
        pages[accession] = path
    return pages


def stored_documents() -> dict[str, set[str]]:
    """Return the document file names stored under ``raw/documents`` per accession."""
    stored: dict[str, set[str]] = {}
    for directory in sorted(DOCUMENTS_ROOT.glob("*/*")):
        accession, _, filename = directory.name.partition("__")
        stored.setdefault(accession, set()).add(filename)
    return stored


def document(
    sequence: str | None = "1",
    doc_type: str | None = "8-K",
    filename: str | None = "primary.htm",
) -> str:
    """Return one escaped-SGML ``<DOCUMENT>`` block with optional missing fields."""
    lines = ["<DOCUMENT>"]
    if sequence is not None:
        lines.append(f"<SEQUENCE>{sequence}")
    if doc_type is not None:
        lines.append(f"<TYPE>{doc_type}")
    if filename is not None:
        lines.append(f"<FILENAME>{filename}")
    lines.extend(["<TEXT>", '<a href="primary.htm">Document 1</a><br>', "</DOCUMENT>"])
    return "\n".join(lines)


def index_page(
    blocks: list[str],
    *,
    form: str = "8-K",
    comment_accession: str | None = ACCESSION,
    tag_accession: str | None = ACCESSION,
    label_accession: str | None = ACCESSION,
) -> str:
    """Return a synthetic ``-index-headers.html`` page holding ``blocks``."""
    pre = ["<SEC-DOCUMENT>index.html : 20220127", "<SEC-HEADER>header.hdr.sgml : 20220127"]
    if tag_accession is not None:
        pre.append(f"<ACCESSION-NUMBER>{tag_accession}")
    if label_accession is not None:
        pre.append(f"ACCESSION NUMBER:\t\t{label_accession}")
    pre.append(f"CONFORMED SUBMISSION TYPE:\t{form}")
    pre.extend(blocks)
    pre.append("</SEC-DOCUMENT>")
    comment = ""
    if comment_accession is not None:
        comment = (
            "<!--\n<SEC-HEADER>header.hdr.sgml : 20220127\n"
            f"<ACCESSION-NUMBER>{comment_accession}\n<TYPE>{form}\n</SEC-HEADER>\n-->\n"
        )
    body = html.escape("\n".join(pre))
    return (
        f"<HTML><HEAD><TITLE>submission</TITLE>\n{comment}</HEAD>"
        f"<BODY>\n<PRE>{body}</PRE>\n</BODY></HTML>"
    )


# --------------------------------------------------------------------------- fixtures


def test_every_fixture_page_matches_the_recorded_documents() -> None:
    pages = index_pages()
    stored = stored_documents()
    assert set(pages) == set(EXPECTED_EXHIBITS), "fixture pages and text_expected.json disagree"

    for accession, path in sorted(pages.items()):
        refs = exhibit_documents(path.read_text(encoding="utf-8"))
        exhibits = [ref.filename for ref in refs if ref.doc_type == "EX-99.1"]

        assert all(ref.accession == accession for ref in refs), accession
        assert refs[0].doc_type == "primary", accession
        assert exhibits == EXPECTED_EXHIBITS[accession], accession
        assert {ref.filename for ref in refs} == stored.get(accession, set()), accession
        assert len({ref.filename for ref in refs}) == len(refs), accession

        if accession in EXPECTED_PRIMARY:
            assert refs[0].filename == EXPECTED_PRIMARY[accession], accession


def test_the_amendment_returns_its_8_ka_primary() -> None:
    page = index_pages()[AMENDMENT].read_text(encoding="utf-8")
    refs = exhibit_documents(page)
    assert refs == [
        DocumentRef(accession=AMENDMENT, filename="ef20081427_8ka.htm", doc_type="primary")
    ]


def test_filings_without_an_exhibit_return_the_primary_only() -> None:
    pages = index_pages()
    for accession in EXHIBIT_LESS:
        refs = exhibit_documents(pages[accession].read_text(encoding="utf-8"))
        assert len(refs) == 1, accession
        assert refs[0].doc_type == "primary", accession
        assert refs[0].filename in stored_documents()[accession], accession


def test_escaped_and_unescaped_pages_discover_the_same_documents() -> None:
    page = index_pages()[ACCESSION].read_text(encoding="utf-8")
    assert exhibit_documents(html.unescape(page)) == exhibit_documents(page)


# ------------------------------------------------------------------------ selection


def test_primary_comes_first_and_exhibits_follow_in_sequence_order() -> None:
    page = index_page(
        [
            document("9", "EX-99.1", "second-exhibit.htm"),
            document("4", "GRAPHIC", "logo.jpg"),
            document("7", "EX-99.1", "first-exhibit.htm"),
            document("1", "8-K", "form8k.htm"),
            document("11", "EX-101.SCH", "aapl-20220127.xsd"),
        ]
    )
    assert exhibit_documents(page) == [
        DocumentRef(accession=ACCESSION, filename="form8k.htm", doc_type="primary"),
        DocumentRef(accession=ACCESSION, filename="first-exhibit.htm", doc_type="EX-99.1"),
        DocumentRef(accession=ACCESSION, filename="second-exhibit.htm", doc_type="EX-99.1"),
    ]


def test_documents_outside_scope_are_ignored() -> None:
    page = index_page(
        [
            document("1", "8-K", "form8k.htm"),
            document("2", "EX-99.2", "presentation.htm"),
            document("3", "EX-99", "general-exhibit.htm"),
            document("4", "EX-3.1", "bylaws.htm"),
            document("5", "XML", "R1.htm"),
            document("6", "GRAPHIC", "logo.jpg"),
            document("7", "EX-99.1", "exhibit991.htm"),
        ]
    )
    assert [ref.filename for ref in exhibit_documents(page)] == ["form8k.htm", "exhibit991.htm"]


@pytest.mark.parametrize(
    "doc_type",
    [
        "EX-99.1",
        "ex-99.1",
        "Ex-99.1",
        "EX-99.01",
        "EX-99.(1)",
        "EX-99-1",
        "EX-99_1",
        "EX-99.1 ",
    ],
)
def test_every_accepted_exhibit_spelling_normalises_to_ex_99_1(doc_type: str) -> None:
    page = index_page([document("1", "8-K", "form8k.htm"), document("2", doc_type, "exhibit.htm")])
    refs = exhibit_documents(page)
    assert [(ref.filename, ref.doc_type) for ref in refs] == [
        ("form8k.htm", "primary"),
        ("exhibit.htm", "EX-99.1"),
    ]


@pytest.mark.parametrize("doc_type", ["EX-99.2", "EX-99", "EX-99.1A", "EX-99.10", "EX-100.INS"])
def test_other_exhibit_types_are_not_ex_99_1(doc_type: str) -> None:
    page = index_page([document("1", "8-K", "form8k.htm"), document("2", doc_type, "other.htm")])
    assert [ref.filename for ref in exhibit_documents(page)] == ["form8k.htm"]


def test_an_8_ka_primary_is_found_in_a_synthetic_page() -> None:
    page = index_page([document("1", "8-K/A", "form8ka.htm")], form="8-K/A")
    assert exhibit_documents(page) == [
        DocumentRef(accession=ACCESSION, filename="form8ka.htm", doc_type="primary")
    ]


# ------------------------------------------------------------------------- malformed


@pytest.mark.parametrize(
    ("block", "reason"),
    [
        (document(sequence=None), "no <SEQUENCE>"),
        (document(doc_type=None), "no <TYPE>"),
        (document(filename=None), "no <FILENAME>"),
        (document(sequence="one"), "non-numeric"),
    ],
)
def test_a_block_without_a_usable_field_raises(block: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason) as excinfo:
        exhibit_documents(index_page([document("1", "8-K", "form8k.htm"), block]))
    assert ACCESSION in str(excinfo.value)


def test_a_page_without_a_document_block_raises() -> None:
    with pytest.raises(ValueError, match="no <DOCUMENT> block") as excinfo:
        exhibit_documents(index_page([]))
    assert ACCESSION in str(excinfo.value)


def test_a_page_without_a_primary_document_raises() -> None:
    page = index_page([document("1", "EX-99.1", "exhibit.htm")])
    with pytest.raises(ValueError, match="no primary 8-K/8-K/A") as excinfo:
        exhibit_documents(page)
    assert ACCESSION in str(excinfo.value)


def test_two_primary_documents_raise_instead_of_picking_one() -> None:
    page = index_page([document("1", "8-K", "form8k.htm"), document("2", "8-K/A", "form8ka.htm")])
    with pytest.raises(ValueError, match="2 primary 8-K/8-K/A") as excinfo:
        exhibit_documents(page)
    assert ACCESSION in str(excinfo.value)


def test_a_page_without_an_accession_raises() -> None:
    page = index_page(
        [document()], comment_accession=None, tag_accession=None, label_accession=None
    )
    with pytest.raises(ValueError, match="no accession number"):
        exhibit_documents(page)


def test_conflicting_accession_numbers_raise() -> None:
    page = index_page([document()], label_accession="0000320193-22-000007")
    with pytest.raises(ValueError, match="conflicting accession numbers") as excinfo:
        exhibit_documents(page)
    assert "0000320193-22-000006" in str(excinfo.value)
    assert "0000320193-22-000007" in str(excinfo.value)


def test_an_unbalanced_document_tag_raises() -> None:
    page = index_page(["<DOCUMENT>\n<SEQUENCE>1\n<TYPE>8-K\n<FILENAME>form8k.htm"])
    with pytest.raises(ValueError, match="unbalanced <DOCUMENT> tags") as excinfo:
        exhibit_documents(page)
    assert ACCESSION in str(excinfo.value)


# ------------------------------------------------------------------------- property


def split_document_blocks(body: str) -> tuple[str, list[str], str]:
    """Split a page into the text before, between and after its document blocks."""
    blocks = _DOCUMENT_BLOCK_RE.findall(body)
    assert len(blocks) > 1
    first = body.index(blocks[0])
    last = body.rindex(blocks[-1]) + len(blocks[-1])
    return body[:first], blocks, body[last:]


# Three exhibits around two ignored documents, so a shuffle can reorder the exhibits.
PROPERTY_PAGE = html.unescape(
    index_page(
        [
            document("1", "8-K", "form8k.htm"),
            document("2", "EX-99.1", "exhibit-a.htm"),
            document("3", "GRAPHIC", "logo.jpg"),
            document("4", "EX-99.1", "exhibit-b.htm"),
            document("5", "EX-101.SCH", "aapl-20220127.xsd"),
            document("6", "EX-99.1", "exhibit-c.htm"),
        ]
    )
)
PAGE_HEAD, PAGE_BLOCKS, PAGE_TAIL = split_document_blocks(PROPERTY_PAGE)
PAGE_RESULT = exhibit_documents(PROPERTY_PAGE)


def test_the_property_page_has_several_exhibits() -> None:
    assert [ref.filename for ref in PAGE_RESULT] == [
        "form8k.htm",
        "exhibit-a.htm",
        "exhibit-b.htm",
        "exhibit-c.htm",
    ]


@given(data=st.data())
@settings(max_examples=50, deadline=None)
def test_shuffling_document_blocks_never_changes_the_result(data: st.DataObject) -> None:
    order = data.draw(st.permutations(range(len(PAGE_BLOCKS))), label="block order")
    shuffled = PAGE_HEAD + "".join(PAGE_BLOCKS[index] for index in order) + PAGE_TAIL
    assert exhibit_documents(shuffled) == PAGE_RESULT
