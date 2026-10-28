"""Convert an EDGAR HTML document to plain text (decision A-31)."""

from __future__ import annotations

import re
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from bs4.element import (
    Comment,
    Declaration,
    Doctype,
    NavigableString,
    ProcessingInstruction,
    Tag,
)

# Sentinel between table cells while rendering: a literal tab inside a cell
# must collapse to a space, while a cell boundary must survive as a tab.
_CELL_SEP = "\x1f"

# Tags whose content is never visible filing text.  ``ix:header`` carries the
# inline-XBRL contexts and hidden facts; ``head`` carries the document title.
_DROP_TAGS = frozenset({"script", "style", "head", "ix:header"})

# Block-level elements: each one ends a line.  ``br`` is handled separately
# because consecutive breaks are a real blank line, not another block boundary.
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "caption",
        "center",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "legend",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "ul",
    }
)

_ROW_TAGS = frozenset({"tr"})
_CELL_TAGS = frozenset({"th", "td"})

# A tag-like construct, used to tell an HTML document from a plain-text one.
_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][^<>]*>")
# A character entity.  Its presence means the input is HTML even when it has no
# tags, so the entity must be resolved rather than passed through literally.
_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")
# EDGAR's SGML document envelope, which the archive includes verbatim around
# some exhibit documents.  Everything outside ``<TEXT>`` is submission
# metadata (type, sequence, filename), not filing text.
_SGML_ENVELOPE_RE = re.compile(
    r"\A\s*<DOCUMENT>.*?<TEXT>(?P<body>.*?)(?:</TEXT>.*)?\Z",
    re.IGNORECASE | re.DOTALL,
)
# Runs of Unicode whitespace (including non-breaking spaces and tabs) inside
# one line or one table cell.
_SPACE_RE = re.compile(r"\s+")
_DISPLAY_NONE_RE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
# Lone surrogates cannot be encoded as UTF-8, which the lxml parser requires.
# They cannot come from a document decoded normally, but replacing them keeps
# the function total for any string input.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def html_to_text(html: str) -> str:
    """Return the readable plain text of an EDGAR HTML document.

    ``html`` is a document body exactly as served (decoded bytes, entities not
    yet resolved). Parsing uses BeautifulSoup with the lxml parser, so malformed
    markup never raises. The conversion, in order:

    - drops ``script``, ``style``, ``head`` and hidden XBRL (``ix:header`` and
      elements hidden with ``display:none``), which carry no filing text;
    - replaces block elements (``p``, ``div``, ``br``, headings, list items,
      table rows) with line breaks and table cells with a tab, so a table
      becomes one tab-separated line per row;
    - resolves HTML entities, collapses runs of spaces and tabs inside a line,
      strips trailing spaces, drops empty leading and trailing lines and
      collapses runs of blank lines to one;
    - returns ``\\n`` line endings and no trailing newline.

    The result is deterministic for the same input. Returns ``""`` for a
    document with no text (for example an empty body or one that is only hidden
    XBRL). Never returns ``None`` and never raises for malformed HTML.

    Some exhibit documents are stored in EDGAR's SGML document envelope
    (``<DOCUMENT>`` … ``<TEXT>`` … ``</TEXT>``); only the text inside ``<TEXT>``
    is converted, because the rest is submission metadata.
    """
    if not html.strip():
        return ""
    if _SURROGATE_RE.search(html):
        html = _SURROGATE_RE.sub("\ufffd", html)
    envelope = _SGML_ENVELOPE_RE.match(html)
    if envelope is not None:
        html = envelope.group("body")
    if not html.strip():
        return ""
    if not _TAG_RE.search(html) and not _ENTITY_RE.search(html):
        # A plain-text document (the fixtures store both .htm and .txt
        # documents): only its whitespace is normalised, its lines are kept.
        return _assemble(html, cell_sep="\t")
    # SEC inline-XBRL documents start with an XML declaration but are served
    # as text/html and are HTML in every way that matters; parsing them as XML
    # would break on the unclosed tags the filers rely on.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(list(_DROP_TAGS)):
        if not tag.decomposed:
            tag.decompose()
    for tag in soup.find_all(style=True):
        if tag.decomposed:
            continue
        if _DISPLAY_NONE_RE.search(str(tag.get("style", ""))):
            tag.decompose()
    body = soup.body if soup.body is not None else soup
    return _assemble(_render_contents(body, inline=False), cell_sep=_CELL_SEP)


def _render_contents(node: Tag, *, inline: bool) -> str:
    """Render the children of ``node``, joining their contributions."""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, (Comment, Declaration, Doctype, ProcessingInstruction)):
            continue
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            parts.append(_render_tag(child, inline=inline))
    return "".join(parts)


def _render_tag(tag: Tag, *, inline: bool) -> str:
    """Render one element, in block context or inside a table cell."""
    name = tag.name.lower() if tag.name else ""
    if name in _ROW_TAGS:
        return _render_row(tag, inline=inline)
    if name in _CELL_TAGS:
        # A cell outside a row (malformed markup) is still just inline text.
        return _render_contents(tag, inline=True)
    if name in ("br", "hr"):
        return " " if inline else "\n"
    text = _render_contents(tag, inline=inline)
    if name not in _BLOCK_TAGS:
        return text
    if inline:
        # Inside a table cell a block boundary is a word boundary, so a
        # multi-paragraph cell still fits on its row's single line.
        return f" {text} " if text else ""
    if not text.strip():
        return ""
    return text if text.endswith("\n") else text + "\n"


def _render_row(row: Tag, *, inline: bool) -> str:
    """Render a table row as tab-separated cells on one line (A-31)."""
    cells: list[str] = []
    for cell in row.find_all(_CELL_TAGS, recursive=False):
        value = _SPACE_RE.sub(" ", _render_contents(cell, inline=True)).strip()
        if value:
            cells.append(value)
    separator = " " if inline else _CELL_SEP
    text = separator.join(cells)
    if inline:
        return f" {text} " if text else ""
    return text + "\n" if text else ""


def _assemble(text: str, *, cell_sep: str) -> str:
    """Normalise whitespace and table cells, and drop redundant blank lines.

    ``text`` is rendered text where ``cell_sep`` separates table cells and
    ``\\n`` separates lines. Unicode whitespace inside a cell collapses to one
    space, empty cells are dropped, and cell boundaries become tabs. Empty
    leading and trailing lines are removed and runs of blank lines become one.
    """
    lines: list[str] = []
    for raw_line in text.split("\n"):
        cells = [
            cell
            for cell in (_SPACE_RE.sub(" ", part).strip() for part in raw_line.split(cell_sep))
            if cell
        ]
        line = "\t".join(cells)
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
