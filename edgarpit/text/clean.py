"""Convert an EDGAR HTML document to plain text (decision A-31)."""

from __future__ import annotations


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
    """
    raise NotImplementedError("HTML-to-text lands with the filing-text ingest")
