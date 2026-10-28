"""Fetch the 8-K documents of selected filings into the raw cache (decisions A-30, A-37).

``fetch-text`` reads the 8-K filings whose items a user asks for from the built
database, then stores each filing's ``-index-headers.html`` page and every
document the page names for the text table — the primary 8-K/8-K/A document and
each ``EX-99.1`` exhibit (decision A-40) — in the raw cache under ``--data``.
Filing indexes and documents are immutable, so a key that is already cached is
never requested again (decision A-30); a second run over the same cache makes no
HTTP request.

The fetch only fetches: ``raw.filings_text`` is built later by
:func:`edgarpit.text.ingest.ingest_text` from the cache (decision A-37), which
keeps the database a pure function of the raw cache.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from edgarpit.config import Settings
from edgarpit.rawcache import RawCache, document_key
from edgarpit.sec_client import SecClient, SecNotFound, document_url, filing_index_url
from edgarpit.text import discover

LOGGER = logging.getLogger("edgarpit.text.fetch")

_EVENT_FORMS = ("8-K", "8-K/A")


@dataclass(frozen=True)
class TextFetchReport:
    """What one ``fetch-text`` run did.

    ``filings`` is the number of filings selected from the database (before any
    failure); ``indexes_fetched`` and ``documents_fetched`` are the filing index
    pages and documents stored this run; ``already_cached`` counts the index
    pages and documents that were not requested because the cache already held
    them; ``failed`` lists, in encounter order, the accession of a filing whose
    index page answered 404 and the ``<accession>__<filename>`` key of a
    document that did. Neither a failed index nor a failed document stops the
    run.
    """

    filings: int
    indexes_fetched: int
    documents_fetched: int
    already_cached: int
    failed: tuple[str, ...]


def select_text_accessions(
    db: Path, items: Sequence[str], *, limit: int | None = None
) -> list[tuple[str, str]]:
    """Return the ``(cik, accession)`` of 8-K/8-K/A filings carrying ``items``.

    ``db`` is a built database with ``raw.events_8k``; ``items`` the item codes
    to select (``["2.02"]``); a filing is selected when any of its item codes is
    in ``items``. The result is ordered by the filing's ``acceptance_ts`` then
    ``accession``, so the same database always yields the same order, and holds
    each filing once even when several of its items match. ``limit`` keeps only
    the first ``limit`` filings (``0`` selects none, ``None`` keeps all).

    Returns an empty list for empty ``items``. Raises :class:`ValueError` for a
    negative ``limit`` or a database without ``raw.events_8k``, and
    :class:`FileNotFoundError` when there is no database at ``db``. The
    database is opened read-only, so the selection cannot change it.
    """
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be zero or positive, got {limit}")
    wanted = sorted({item.strip() for item in items if item.strip()})
    if not wanted or limit == 0:
        return []
    path = Path(db)
    if not path.is_file():
        raise FileNotFoundError(f"no database at {path}")
    con = duckdb.connect(str(path), read_only=True)
    try:
        forms = ", ".join("?" for _ in _EVENT_FORMS)
        item_slots = ", ".join("?" for _ in wanted)
        statement = (
            "SELECT DISTINCT cik, accession, acceptance_ts FROM raw.events_8k "
            f"WHERE form IN ({forms}) AND item_code IN ({item_slots}) "
            "ORDER BY acceptance_ts, accession"
        )
        try:
            rows = con.execute(statement, [*_EVENT_FORMS, *wanted]).fetchall()
        except duckdb.CatalogException as error:
            raise ValueError(
                f"database {path} has no raw.events_8k table; run `edgarpit build` first"
            ) from error
    finally:
        con.close()
    selected: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for cik, accession, _acceptance_ts in rows:
        pair = (str(cik), str(accession))
        if pair not in seen:
            seen.add(pair)
            selected.append(pair)
    return selected if limit is None else selected[:limit]


def fetch_text_documents(
    db: Path,
    data_dir: Path,
    items: Sequence[str],
    *,
    limit: int | None = None,
    client: SecClient | None = None,
) -> TextFetchReport:
    """Fetch the filing index and text documents of the selected 8-Ks.

    ``db`` is a built database with ``raw.events_8k``; ``data_dir`` the raw
    cache root to write into when no ``client`` is given; ``items`` the 8-K item
    codes whose filings should be fetched (see
    :func:`select_text_accessions`); ``limit`` caps the number of filings.

    For each selected filing the ``-index-headers.html`` page is stored (unless
    already cached) and then every document
    :func:`edgarpit.text.discover.exhibit_documents` names from it — the primary
    8-K/8-K/A document and each ``EX-99.1`` exhibit — through the same
    :class:`~edgarpit.sec_client.SecClient`, so the run shares one rate limiter
    and the cache's immutability rules (decision A-30). A cached index page is
    read from the cache and its documents checked from there, so a warm cache
    makes no request at all; neither an index page nor a document is ever
    re-fetched.

    ``client`` must be constructed with ``cache=RawCache(data_dir)`` (the bytes
    are written by the client); when it is ``None`` one is built from
    :class:`edgarpit.config.Settings` over ``data_dir`` and closed before
    returning, which needs ``SEC_USER_AGENT`` only when a request is actually
    made. Returns a :class:`TextFetchReport`. A 404 on a filing index or a
    document is recorded in ``failed`` and the run continues; every other
    :class:`~edgarpit.sec_client.SecClientError` propagates, so a broken SEC
    response stops the run instead of leaving a silently partial cache. Raises
    :class:`ValueError` for a client without a cache and the errors of
    :func:`select_text_accessions` for the database.
    """
    selected = select_text_accessions(db, items, limit=limit)
    if not selected:
        return TextFetchReport(0, 0, 0, 0, ())
    owned_client = client is None
    if client is None:
        client = SecClient(Settings(), cache=RawCache(data_dir))
    try:
        cache = client.cache
        if cache is None:
            raise ValueError(
                "the SecClient passed to fetch_text_documents has no raw cache; "
                "construct it with cache=RawCache(data_dir)"
            )
        indexes_fetched = 0
        documents_fetched = 0
        already_cached = 0
        failed: list[str] = []
        for cik, accession in selected:
            cached_index = cache.latest("filing_index", cik, accession)
            if cached_index is None:
                try:
                    index_text = client.get_text(filing_index_url(cik, accession))
                except SecNotFound:
                    failed.append(accession)
                    LOGGER.warning("[%s] filing index not found: %s", cik, accession)
                    continue
                indexes_fetched += 1
            else:
                index_text = cached_index.decode("utf-8", errors="replace")
                already_cached += 1
            for ref in discover.exhibit_documents(index_text):
                key = document_key(accession, ref.filename)
                if cache.latest("documents", cik, key) is not None:
                    already_cached += 1
                    continue
                try:
                    client.get_text(document_url(cik, accession, ref.filename))
                except SecNotFound:
                    failed.append(key)
                    LOGGER.warning("[%s] document not found: %s", cik, key)
                    continue
                documents_fetched += 1
        report = TextFetchReport(
            filings=len(selected),
            indexes_fetched=indexes_fetched,
            documents_fetched=documents_fetched,
            already_cached=already_cached,
            failed=tuple(failed),
        )
        LOGGER.info(
            "fetch-text: %d filings, %d indexes and %d documents fetched, "
            "%d already cached, %d failed",
            report.filings,
            report.indexes_fetched,
            report.documents_fetched,
            report.already_cached,
            len(report.failed),
        )
        return report
    finally:
        if owned_client:
            client.close()
