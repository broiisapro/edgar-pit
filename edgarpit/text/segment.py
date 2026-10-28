"""Split cleaned 8-K text on the filing's own item headings (decision A-31)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


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
    A heading is a line that starts with ``Item`` (any case) followed by the item
    code and a word boundary, optionally followed by a period, so ``Item 2.02``,
    ``ITEM 2.02.`` and ``Item 2.02 Results of Operations`` are headings while a
    reference to "Item 2.02" inside a sentence is not.

    Segmentation is confident when every expected item appears as a heading
    exactly once, in the order given, and no other ``Item N.NN`` heading
    appears. Then the result holds one section per heading in document order:
    ``section`` is the item code, ``order`` counts from 1 and ``text`` runs from
    the heading to the next heading, stripped; text before the first heading is
    kept at the start of the first section, so no text is lost. When
    segmentation is not confident the result is a single
    ``Section(section='unsegmented', order=1, text=text.strip())``.

    Returns an empty list for blank ``text``. Never raises for text that does
    not segment; the caller decides what an unsegmented document means.
    """
    raise NotImplementedError("item segmentation lands with the filing-text ingest")
