"""Build ``raw.filings_text`` from a raw cache (decision A-31)."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from typing import Any

import polars as pl

from edgarpit.rawcache import RawCache, document_key
from edgarpit.schemas import UNSEGMENTED, FilingsTextSchema
from edgarpit.text import clean, discover, segment

LOGGER = logging.getLogger("edgarpit.text.ingest")

# ``raw.filings_text`` in the staging contract's order (decision A-31). The
# ingest builds the rows with these dtypes so an empty result is still valid.
_TEXT_DTYPES: dict[str, pl.DataType | type[pl.DataType]] = {
    "cik": pl.String,
    "accession": pl.String,
    "acceptance_ts": pl.Datetime("us", "UTC"),
    "ts_imputed": pl.Boolean,
    "document": pl.String,
    "doc_type": pl.String,
    "section": pl.String,
    "section_order": pl.Int64,
    "text": pl.String,
    "sha256": pl.String,
    "char_count": pl.Int64,
}

_TEXT_COLUMNS = list(_TEXT_DTYPES)


def _frame(rows: list[tuple[object, ...]]) -> pl.DataFrame:
    """Return ``rows`` as a ``raw.filings_text`` frame in contract column order.

    An empty ``rows`` yields a frame with the contract's columns and dtypes, so
    a filing without extractable text is a valid empty table rather than an
    untyped frame.
    """
    return pl.DataFrame(rows, schema=_TEXT_DTYPES, orient="row").select(_TEXT_COLUMNS)


def _filing_row(filings: pl.DataFrame, accession: str) -> dict[str, Any]:
    """Return the one ``filings`` row of ``accession`` with a usable acceptance time.

    ``filings`` is a frame carrying the ``raw.filings`` columns. Raises
    :class:`ValueError` when the accession is absent, appears more than once
    (the contract makes it unique, and two rows could imply two acceptance
    times) or carries no ``acceptance_ts``, so a section is never labelled with
    a guessed instant.
    """
    rows = filings.filter(pl.col("accession") == accession)
    if rows.height == 0:
        raise ValueError(f"accession {accession} is not in the filings frame")
    if rows.height > 1:
        raise ValueError(
            f"filings frame has {rows.height} rows for accession {accession}; "
            "it must be unique on accession"
        )
    row: dict[str, Any] = rows.row(0, named=True)
    if row["acceptance_ts"] is None:
        raise ValueError(f"accession {accession} has no acceptance_ts in the filings frame")
    return row


def _expected_items(items: object) -> list[str]:
    """Return the comma-separated item codes of a filing, in declared order.

    ``items`` is the raw ``raw.filings.items`` value (``"2.02,9.01"`` or
    ``None``). Codes are stripped, empty entries dropped and the order kept, so
    the segmenter checks the headings against what the filing says in the order
    the filing says it.
    """
    if not isinstance(items, str):
        return []
    return [code for code in (part.strip() for part in items.split(",")) if code]


def _sections(
    ref: discover.DocumentRef, html_text: str, expected_items: Sequence[str]
) -> list[segment.Section]:
    """Split a document into its stored sections (decisions A-31, A-40).

    The primary 8-K/8-K/A document is segmented on the filing's own item codes;
    an ``EX-99.1`` exhibit is never segmented and becomes one ``unsegmented``
    section, because an exhibit carries no items of its own.
    """
    if ref.doc_type == "primary":
        return segment.segment_items(html_text, expected_items)
    return [segment.Section(section=UNSEGMENTED, order=1, text=html_text.strip())]


def ingest_text(
    raw: RawCache,
    cik: str,
    accessions: Sequence[str],
    filings: pl.DataFrame,
) -> pl.DataFrame:
    """Extract the text of ``accessions`` into a ``raw.filings_text`` frame.

    ``raw`` is the cache (or an offline fixture tree) holding the filing indexes
    and documents of ``cik``; ``cik`` is the issuer's zero-padded CIK;
    ``accessions`` the filings to extract, processed in the order given;
    ``filings`` a frame with the ``raw.filings`` columns (at least ``cik``,
    ``accession``, ``acceptance_ts``, ``ts_imputed``, ``items`` and ``form``)
    covering those accessions and unique on ``accession``.

    For each accession the latest cached filing index (``raw.filing_index``,
    key = accession) is read with
    :func:`edgarpit.text.discover.exhibit_documents`, every referenced document
    (``raw.documents``, key = ``<accession>__<filename>``) is read and converted
    with :func:`edgarpit.text.clean.html_to_text`, and the text is split with
    :func:`edgarpit.text.segment.segment_items` on the item codes of the
    accession's ``items`` column (``"2.02,9.01"``), in the order the filing
    lists them. Per decision A-40 only the primary 8-K/8-K/A document is
    segmented; every ``EX-99.1`` exhibit becomes one ``section='unsegmented'``
    row. Documents whose cleaned text is blank contribute no rows.

    The result has one row per (document, section), valid under
    :class:`edgarpit.schemas.FilingsTextSchema` and sorted by
    ``(accession, document, section_order)`` (so it does not depend on the order
    of ``accessions`` or on the order the index page lists the documents).
    ``cik`` is the ``cik`` argument; ``acceptance_ts`` and ``ts_imputed`` are
    copied from the accession's ``filings`` row, ``document`` is the EDGAR file
    name, ``doc_type`` is ``primary`` or ``EX-99.1``, ``sha256`` is the section
    text's lowercase hex SHA-256 (of its UTF-8 bytes) and ``char_count`` its
    character length. Nothing else in ``filings`` (``period_end``, ``filed``,
    ``filing_date``) and nothing inside the documents' own dates influences the
    result, so a section is known exactly when its filing was accepted.

    An accession with no cached filing index page is skipped and counted: one
    ``INFO`` line names the number skipped and the first five accessions.
    Returns an empty frame with the contract's columns when ``accessions`` is
    empty or no filing yields text. Raises :class:`ValueError` for an accession
    missing from ``filings``, carried more than once, or without an acceptance
    time, and for an index page that names another accession; raises
    :class:`LookupError` for a document listed by a cached index that is not in
    the cache, naming the missing key, so a partial cache never becomes a
    silently incomplete table.

    Document bytes are decoded as UTF-8 with replacement, so a stray byte in a
    filing can neither raise nor stop the build.
    """
    if not accessions:
        return _frame([])

    rows: list[tuple[object, ...]] = []
    skipped: list[str] = []
    for accession in accessions:
        filing = _filing_row(filings, accession)
        index_body = raw.latest("filing_index", cik, accession)
        if index_body is None:
            skipped.append(accession)
            continue
        references = discover.exhibit_documents(index_body.decode("utf-8", errors="replace"))
        expected = _expected_items(filing["items"])
        for ref in references:
            if ref.accession != accession:
                raise ValueError(
                    f"filing index for {accession} lists a document of {ref.accession}"
                )
            key = document_key(accession, ref.filename)
            body = raw.latest("documents", cik, key)
            if body is None:
                raise LookupError(f"no cached document {key} for CIK {cik}")
            document_text = clean.html_to_text(body.decode("utf-8", errors="replace"))
            if not document_text.strip():
                LOGGER.debug("[%s] no text in document %s", cik, key)
                continue
            for section in _sections(ref, document_text, expected):
                digest = hashlib.sha256(section.text.encode("utf-8")).hexdigest()
                rows.append(
                    (
                        cik,
                        accession,
                        filing["acceptance_ts"],
                        filing["ts_imputed"],
                        ref.filename,
                        ref.doc_type,
                        section.section,
                        section.order,
                        section.text,
                        digest,
                        len(section.text),
                    )
                )

    if skipped:
        LOGGER.info(
            "CIK %s: skipped %d accession(s) without a cached filing index (first five: %s)",
            cik,
            len(skipped),
            ", ".join(skipped[:5]),
        )
    frame = _frame(rows).sort(["accession", "document", "section_order"], maintain_order=True)
    FilingsTextSchema.validate(frame)
    LOGGER.info(
        "CIK %s: extracted %d text rows from %d filings", cik, frame.height, len(accessions)
    )
    return frame
