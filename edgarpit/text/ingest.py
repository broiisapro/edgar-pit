"""Build ``raw.filings_text`` from a raw cache (decision A-31)."""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl

from edgarpit.rawcache import RawCache


def ingest_text(
    raw: RawCache,
    cik: str,
    accessions: Sequence[str],
    filings: pl.DataFrame,
) -> pl.DataFrame:
    """Extract the text of ``accessions`` into a ``raw.filings_text`` frame.

    ``raw`` is the cache (or an offline fixture tree) holding the filing indexes
    and documents of ``cik``; ``cik`` is the issuer's zero-padded CIK;
    ``accessions`` the filings to extract; ``filings`` a frame with the
    ``raw.filings`` columns (at least ``cik``, ``accession``, ``acceptance_ts``,
    ``ts_imputed``, ``items`` and ``form``) covering those accessions.

    For each accession, in the order given, the filing index
    (``raw.filing_index``, key = accession) is read with
    :func:`edgarpit.text.discover.exhibit_documents`, every referenced document
    (``raw.documents``, key = ``<accession>__<filename>``) is read and converted
    with :func:`edgarpit.text.clean.html_to_text`, and the text is split with
    :func:`edgarpit.text.segment.segment_items` on the item codes of the
    accession's ``items`` column (``"2.02,9.01"``), in the order the filing
    lists them. The result has one row per (document, section, section_order),
    in document order and then section order, and is valid under
    :class:`edgarpit.schemas.FilingsTextSchema`: ``cik``, ``acceptance_ts`` and
    ``ts_imputed`` are copied from the accession's ``filings`` row, ``document``
    is the file name, ``doc_type`` is ``primary`` or ``EX-99.1``, ``sha256`` is
    the section text's lowercase hex SHA-256 and ``char_count`` its character
    length. Nothing else in ``filings`` (``period_end``, ``filed``,
    ``filing_date``) influences the result, so a section is known exactly when
    its filing was accepted.

    Returns an empty frame with the contract's columns when ``accessions`` is
    empty or a filing has no extractable text. Raises :class:`ValueError` for an
    accession missing from ``filings`` or without an acceptance time, and
    :class:`LookupError` for a filing index or document that is not in the
    cache, naming the missing key.
    """
    raise NotImplementedError("filing-text ingest lands with the 8-K text issues")
