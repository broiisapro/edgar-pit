"""Find the documents of an 8-K filing in its ``-index-headers.html`` page (A-30, A-31).

The ``index.json`` of a filing has no document types, so the text work starts
from the SGML index page at
``https://www.sec.gov/Archives/edgar/data/<cik-int>/<accession-no-dashes>/<accession>-index-headers.html``:
each document row carries a ``<TYPE>`` and a ``<FILENAME>``. Only the primary
8-K/8-K/A document and the ``EX-99.1`` exhibits are in scope.
"""

from __future__ import annotations

from dataclasses import dataclass


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


def exhibit_documents(index_headers_html: str) -> list[DocumentRef]:
    """Return the primary document and the ``EX-99.1`` exhibits of a filing.

    ``index_headers_html`` is the text of the filing's
    ``-index-headers.html`` page. The result lists, in page order, the one
    document whose ``<TYPE>`` is ``8-K`` or ``8-K/A`` (``doc_type='primary'``)
    followed by every document whose ``<TYPE>`` is ``EX-99.1``
    (``doc_type='EX-99.1'``); the ``accession`` of every reference is the page's
    ``<ACCESSION NUMBER>``. Documents outside those types are ignored: the index
    page itself, the ``.hdr.sgml`` header, the XBRL instance, schema, linkbase
    and rendering files, ``R*.htm`` viewer pages, graphics and any other exhibit
    type.

    Raises :class:`ValueError` when the page carries no ``<ACCESSION NUMBER>``,
    when it lists no primary 8-K/8-K/A document, or when a primary or exhibit
    row has no ``<FILENAME>``.
    """
    raise NotImplementedError("8-K document discovery lands with the filing-text ingest")
