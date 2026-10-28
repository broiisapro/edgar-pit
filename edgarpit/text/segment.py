"""Split cleaned 8-K text on the filing's own item headings (decision A-31).

The split is deliberately conservative: a document is segmented only when its
headings are exactly the filing's declared items, once each and in the declared
order. Anything else — a repeated heading, an item the filing does not declare,
a missing item, an out-of-order heading — leaves the whole document in one
``section='unsegmented'`` row, because a wrong split attributes text to the
wrong event.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from edgarpit.schemas import UNSEGMENTED

# An item heading is the word ``Item`` (any case) at the start of a line, after
# optional indentation, whitespace, a dotted EDGAR item code (the shape the
# staging contract allows: one or two digits, a dot, two digits), an optional
# period and a word boundary. Anchoring at the line start is what keeps a
# reference such as "as described in Item 2.02 above" in running prose from
# counting as a heading.
_HEADING = re.compile(
    r"^[ \t\u00a0]*(?i:item)[ \t\u00a0]+(?P<code>\d{1,2}\.\d{2})\.?(?![0-9A-Za-z_.])",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Section:
    """One section of a document's text.

    ``section`` is the EDGAR item code the section belongs to (``2.02``) or
    ``unsegmented``; ``order`` is the section's 1-based position in the
    document, and ``text`` the section's stripped plain text.
    """

    section: str
    order: int
    text: str


def segment_items(text: str, expected_items: Sequence[str]) -> list[Section]:
    """Split ``text`` into one :class:`Section` per item heading.

    ``text`` is cleaned plain text (see :func:`edgarpit.text.clean.html_to_text`)
    and ``expected_items`` the filing's own item codes from ``raw.filings.items``
    (for example ``["2.02", "9.01"]``), in the order the filing declares them.
    A heading is a line that starts — after optional indentation — with ``Item``
    (any case) followed by the item code and a word boundary, optionally
    followed by a period, so ``Item 2.02``, ``ITEM 2.02.`` and
    ``Item 2.02 Results of Operations`` are headings while a reference to
    "Item 2.02" inside a sentence is not. Only dotted codes can be headings, so
    a filing that declares a legacy pre-2004 numeric item (``5``, ``7``, ``12``)
    never segments and is returned unsegmented.

    Segmentation is confident when every expected item appears as a heading
    exactly once, in the order given, and no other ``Item N.NN`` heading
    appears. Then the result holds one section per heading in document order:
    ``section`` is the item code, ``order`` counts from 1 and ``text`` runs from
    the heading to the next heading, stripped; text before the first heading is
    kept at the start of the first section, so no text is lost. When
    segmentation is not confident the result is a single
    ``Section(section='unsegmented', order=1, text=text.strip())``.

    Reconstruction rule: the sections' whitespace-separated tokens, in section
    order, are exactly ``text.split()``; for input whose lines carry no leading
    or trailing whitespace and that has no blank lines — the shape of a filing
    body with one paragraph per line — ``"\\n".join(section.text for section in sections)``
    equals ``text.strip()`` character for character. Blank lines and indentation
    between a section's last line and the next heading are layout and are
    collapsed by the strip; no non-whitespace character is dropped.

    A filing that declares no items cannot be segmented; non-blank ``text`` is
    returned as one unsegmented section. Returns an empty list for blank
    ``text``. Never raises for text that does not segment; the caller decides
    what an unsegmented document means.
    """
    if not text.strip():
        return []

    headings = list(_HEADING.finditer(text))
    codes = [match.group("code") for match in headings]
    if not expected_items or codes != list(expected_items):
        return [Section(UNSEGMENTED, 1, text.strip())]

    # The first section starts at the beginning of the document so it keeps the
    # preamble (form title, registrant block) that precedes the first heading.
    cuts = [0, *(match.start() for match in headings[1:]), len(text)]
    return [
        Section(code, position + 1, text[cuts[position] : cuts[position + 1]].strip())
        for position, code in enumerate(codes)
    ]
