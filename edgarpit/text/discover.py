"""Find the documents of an 8-K filing in its ``-index-headers.html`` page (A-30, A-31).

The ``index.json`` of a filing has no document types, so the text work starts
from the SGML index page at
``https://www.sec.gov/Archives/edgar/data/<cik-int>/<accession-no-dashes>/<accession>-index-headers.html``:
each document row carries a ``<TYPE>`` and a ``<FILENAME>``. Only the primary
8-K/8-K/A document and the ``EX-99.1`` exhibits are in scope. When the page
lists more than one 8-K/8-K/A document, a single HTML candidate is the primary:
EDGAR sometimes lists the HTML primary together with a PDF of the entire filing
(McDonald's ``0000063908-14-000056`` lists ``a8-kcoverexhibitlistingand.htm``
as ``FORM 8-K`` and ``form8k.pdf`` as ``PDF OF ENTIRE FORM 8-K``), and only the
HTML converts to text. Any other multiplicity stays an error rather than a
guess.

Survey of exhibit ``<TYPE>`` spellings (2026-10-28). Besides the 15 fixture pages
under ``tests/fixtures/sec_text``, 93 live pages were read from SEC EDGAR: 49
``-index-headers.html`` pages of 8-K/8-K/A filings from 2015 to 2025, and 44
legacy ``-index.htm`` pages from 2003 to 2013 for the same issuers (the
``-index-headers.html`` endpoint does not exist for filings before 2014, checked
on Apple, Pfizer, Walmart and Microsoft). The issuers were Apple, Shopify,
Berkshire Hathaway, Microsoft, Tesla, JPMorgan, Pfizer, Coca-Cola and Walmart;
the documents came from in-house filers and from DFIN
(``d*``, ``ef*``), RR Donnelley (``rrd*``), Toppan Merrill (``a*``, ``tm*``)
and Broadridge (``e*``). The exhibit types seen were ``EX-99.1`` (62), ``EX-99.2``
(9) and ``EX-99`` (1); no other spelling occurred in the sample. The rule is
therefore: a type counts as ``EX-99.1`` when it names exhibit 99.1 exactly, after
normalising case, surrounding whitespace, the separator after ``99`` (``.``,
``-``, ``_`` or a space), an optional pair of parentheses and zero padding
of the number. That accepts ``EX-99.01``, ``EX-99.(1)``, ``EX-99-1`` and
``EX-99_1`` without accepting a different exhibit; bare ``EX-99`` is ambiguous
with the whole exhibit-99 series and ``EX-99.2`` is a separate exhibit, so both
are ignored. ``doc_type`` is always the normalised ``EX-99.1``.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_ACCESSION = r"\d{10}-\d{2}-\d{6}"
_ACCESSION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"<ACCESSION-NUMBER>\s*({_ACCESSION})"),
    re.compile(rf"ACCESSION NUMBER:\s*({_ACCESSION})"),
)
_DOCUMENT_RE = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.IGNORECASE | re.DOTALL)
_DOCUMENT_OPEN_RE = re.compile(r"<DOCUMENT>", re.IGNORECASE)
_DOCUMENT_CLOSE_RE = re.compile(r"</DOCUMENT>", re.IGNORECASE)
_PRIMARY_TYPES = frozenset({"8-K", "8-K/A"})
_EXHIBIT_TYPE_RE = re.compile(r"EX\s*-?\s*99\s*[._\- ]\s*\(?0*1\)?", re.IGNORECASE)
# EDGAR sometimes lists an HTML primary document and a PDF rendering of the
# whole filing under the same 8-K type (McDonald's 0000063908-14-000056 lists
# "FORM 8-K" a8-kcoverexhibitlistingand.htm and "PDF OF ENTIRE FORM 8-K"
# form8k.pdf). Only HTML carries extractable text, so one HTML candidate
# disambiguates the pair; two HTML candidates remain an error.
_HTML_SUFFIXES = (".htm", ".html")


@dataclass(frozen=True)
class DocumentRef:
    """One document of a filing, as the filing index names it.

    ``accession`` is the filing's ``NNNNNNNNNN-YY-NNNNNN`` number, ``filename``
    the EDGAR file name (``aapl-20260730.htm``) and ``doc_type`` either
    ``primary`` (the 8-K/8-K/A document) or ``EX-99.1`` (an exhibit). The pair
    ``(accession, filename)`` is what the raw cache's ``documents`` endpoint
    keys on.
    """

    accession: str
    filename: str
    doc_type: str


def _accession(body: str) -> str:
    """Return the page's accession number, or raise for a missing/conflicting one."""
    found = {match.group(1) for pattern in _ACCESSION_PATTERNS for match in pattern.finditer(body)}
    if not found:
        raise ValueError("filing index page has no accession number")
    if len(found) > 1:
        listed = ", ".join(sorted(found))
        raise ValueError(f"filing index page has conflicting accession numbers: {listed}")
    return found.pop()


def _field(block: str, tag: str) -> str:
    """Return the text of one SGML ``<tag>`` field of a document block, or ``""``."""
    match = re.search(rf"<{tag}>([^\r\n<]*)", block, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _blocks(body: str, accession: str) -> list[tuple[int, int, str, str]]:
    """Return ``(sequence, page_position, doc_type, filename)`` for every document.

    ``page_position`` is the zero-based position of the block on the page, used
    only as a stable tie-breaker. Raises :class:`ValueError` naming the
    ``accession`` when the page has no document block, an unbalanced
    ``<DOCUMENT>`` tag, or a block without a usable ``<SEQUENCE>``, ``<TYPE>`` or
    ``<FILENAME>``.
    """
    starts = len(_DOCUMENT_OPEN_RE.findall(body))
    ends = len(_DOCUMENT_CLOSE_RE.findall(body))
    if starts != ends:
        raise ValueError(
            f"filing {accession} index page has unbalanced <DOCUMENT> tags: "
            f"{starts} opening, {ends} closing"
        )
    matches = list(_DOCUMENT_RE.finditer(body))
    if not matches:
        raise ValueError(f"filing {accession} index page has no <DOCUMENT> block")
    if len(matches) != starts:
        raise ValueError(f"filing {accession} index page has an unclosed <DOCUMENT> block")

    blocks: list[tuple[int, int, str, str]] = []
    for position, match in enumerate(matches):
        block = match.group(1)
        sequence_text = _field(block, "SEQUENCE")
        if not sequence_text:
            raise ValueError(f"filing {accession} document block {position + 1} has no <SEQUENCE>")
        if not sequence_text.isdigit():
            raise ValueError(
                f"filing {accession} document block {position + 1} has a non-numeric "
                f"<SEQUENCE> {sequence_text!r}"
            )
        doc_type = _field(block, "TYPE")
        if not doc_type:
            raise ValueError(f"filing {accession} document block {position + 1} has no <TYPE>")
        filename = _field(block, "FILENAME")
        if not filename:
            raise ValueError(
                f"filing {accession} document block {position + 1} (<TYPE> {doc_type}) "
                "has no <FILENAME>"
            )
        blocks.append((int(sequence_text), position, doc_type, filename))
    return blocks


def _is_exhibit_99_1(doc_type: str) -> bool:
    """True when ``doc_type`` names exhibit 99.1 in one of its accepted spellings."""
    return _EXHIBIT_TYPE_RE.fullmatch(doc_type.strip()) is not None


def _is_html(filename: str) -> bool:
    """True when ``filename`` has an HTML extension (case-insensitive)."""
    return filename.strip().lower().endswith(_HTML_SUFFIXES)


def _primary_document(accession: str, blocks: list[tuple[int, int, str, str]]) -> str:
    """Return the filename of the filing's one primary 8-K/8-K/A document.

    ``blocks`` are the page's ``(sequence, position, doc_type, filename)`` rows.
    Raises :class:`ValueError` naming the ``accession`` when the page has no
    primary document or when more than one remains after the HTML tie-break
    described in the module docstring.
    """
    primary = [block for block in blocks if block[2].upper() in _PRIMARY_TYPES]
    if not primary:
        raise ValueError(f"filing {accession} index page lists no primary 8-K/8-K/A document")
    if len(primary) > 1:
        html_primary = [block for block in primary if _is_html(block[3])]
        if len(html_primary) == 1:
            primary = html_primary
    if len(primary) > 1:
        listed = ", ".join(f"{block[3]} (<TYPE> {block[2]})" for block in primary)
        raise ValueError(
            f"filing {accession} index page lists {len(primary)} primary 8-K/8-K/A "
            f"documents: {listed}"
        )
    return primary[0][3]


def exhibit_documents(index_headers_html: str) -> list[DocumentRef]:
    """Return the primary document and the ``EX-99.1`` exhibits of a filing.

    ``index_headers_html`` is the text of the filing's
    ``-index-headers.html`` page (escaped SGML inside its ``<PRE>`` block; the
    function unescapes it first). The result lists the one document whose
    ``<TYPE>`` is ``8-K`` or ``8-K/A`` (``doc_type='primary'``) first, then every
    document whose ``<TYPE>`` names exhibit 99.1 (``doc_type='EX-99.1'``), in
    ``<SEQUENCE>`` order; EDGAR numbers the primary document first, so the two
    orders coincide. The ``accession`` of every reference is the page's
    accession number. Documents outside those types are ignored: the index page
    itself, the ``.hdr.sgml`` header, the XBRL instance, schema, linkbase and
    rendering files, ``R*.htm`` viewer pages, graphics and any other exhibit
    type.

    Exhibit types are normalised as described in the module docstring: case,
    the separator after ``99``, optional parentheses and zero padding do not
    matter, so ``EX-99.01``, ``EX-99.(1)``, ``EX-99-1`` and ``EX-99_1`` all
    become ``EX-99.1``. ``EX-99`` and ``EX-99.2`` are not exhibit 99.1 and are
    ignored.

    Raises :class:`ValueError` naming the accession when the page carries no
    accession number (or conflicting ones), when it has no ``<DOCUMENT>`` block,
    when its ``<DOCUMENT>`` tags are unbalanced, when a block has no usable
    ``<SEQUENCE>``, ``<TYPE>`` or ``<FILENAME>``, or when it does not list one
    primary 8-K/8-K/A document. Several candidates are resolved only by the
    HTML rule of the module docstring: exactly one HTML filename wins, and two
    HTML candidates or no HTML among them still raise.
    """
    body = html.unescape(index_headers_html)
    accession = _accession(body)
    blocks = _blocks(body, accession)

    primary = _primary_document(accession, blocks)

    exhibits = sorted(
        (block for block in blocks if _is_exhibit_99_1(block[2])),
        key=lambda block: (block[0], block[1]),
    )
    return [
        DocumentRef(accession=accession, filename=primary, doc_type="primary"),
        *(
            DocumentRef(accession=accession, filename=block[3], doc_type="EX-99.1")
            for block in exhibits
        ),
    ]
