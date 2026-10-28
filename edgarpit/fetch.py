"""Fetch a universe into the raw cache (FR-1.2, decisions A-1, A-4).

For every issuer :func:`fetch_universe` pulls the submissions document, each
paginated ``filings.files`` page and the companyfacts document through **one**
injected :class:`~edgarpit.sec_client.SecClient`, so the whole run shares a
single rate limiter. It then fetches the SGML headers of every accession that
matters: every ``us-gaap``/``ifrs-full`` fact accession and every 8-K/8-K/A
filing, computed with :func:`edgarpit.ingest.headers.required_accessions` over
the frozen :func:`edgarpit.ingest.submissions.parse_submissions` frame.
:func:`_fact_accessions` flattens the companyfacts document with
:func:`edgarpit.ingest.companyfacts.parse_companyfacts`, the same function the
build uses, so the fetcher and the loader agree on what matters.

Headers are immutable, so a stored header is never re-fetched. Submissions,
pages and companyfacts are re-fetched when ``refresh`` is true, each under a
new ``fetched_at`` and never overwriting an earlier copy (decision A-4); with
``refresh`` false only a missing document is fetched, which makes an
interrupted run resumable. A header 404 is recorded in the report and is not
fatal: the filing falls back to ``filing_date_eod`` (decision A-1). Any other
final HTTP error is fatal for that issuer, recorded in its report, and the run
continues with the next issuer; the caller turns a report with errors into a
non-zero exit.

Logging: one ``INFO`` line per issuer with its counts, and an ``ERROR`` line
for an issuer that failed.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from edgarpit.ingest.companyfacts import parse_companyfacts
from edgarpit.ingest.headers import required_accessions
from edgarpit.ingest.submissions import parse_submissions
from edgarpit.rawcache import RawCache
from edgarpit.sec_client import (
    SecClient,
    SecClientError,
    SecNotFound,
    companyfacts_url,
    header_url,
    submissions_page_url,
    submissions_url,
)
from edgarpit.universe import Issuer

LOGGER = logging.getLogger("edgarpit.fetch")


class FetchError(RuntimeError):
    """A fetched document cannot be parsed or a step cannot proceed."""


@dataclass
class IssuerFetchReport:
    """What one issuer's fetch did.

    Counts are per run: ``submissions``, ``pages`` and ``companyfacts`` are the
    documents fetched this run (a warm cache and ``refresh=False`` make them 0),
    ``headers_fetched`` are new immutable headers stored, ``headers_cached``
    headers already present (never re-fetched), ``headers_404`` the accessions
    whose header URL answered 404, and ``requests`` the logical GETs issued to
    SEC (retries are not counted separately). ``errors`` holds the final
    failures after retries; an issuer with errors is fatal only for that
    issuer.
    """

    cik: str
    ticker: str
    submissions: int = 0
    pages: int = 0
    companyfacts: int = 0
    headers_fetched: int = 0
    headers_cached: int = 0
    headers_404: list[str] = field(default_factory=list)
    requests: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def headers_not_found(self) -> int:
        """Number of header URLs that answered 404."""
        return len(self.headers_404)

    @property
    def documents(self) -> int:
        """Submissions and companyfacts documents fetched this run."""
        return self.submissions + self.companyfacts


@dataclass
class FetchReport:
    """The outcome of :func:`fetch_universe`, one entry per issuer in order.

    ``skipped_unchanged`` counts the issuers an incremental run skipped because
    their submissions document had not changed (decision A-34); it stays 0
    until incremental fetch lands.
    """

    issuers: list[IssuerFetchReport] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    skipped_unchanged: int = 0

    @property
    def requests(self) -> int:
        """Total logical GETs issued across every issuer."""
        return sum(issuer.requests for issuer in self.issuers)

    @property
    def issuers_ok(self) -> int:
        """Number of issuers that ended without a final error."""
        return sum(1 for issuer in self.issuers if not issuer.errors)

    @property
    def issuers_failed(self) -> int:
        """Number of issuers that ended with at least one final error."""
        return sum(1 for issuer in self.issuers if issuer.errors)

    @property
    def headers_fetched(self) -> int:
        """Headers newly stored across every issuer."""
        return sum(issuer.headers_fetched for issuer in self.issuers)

    @property
    def headers_cached(self) -> int:
        """Headers already present in the cache across every issuer."""
        return sum(issuer.headers_cached for issuer in self.issuers)

    @property
    def headers_not_found(self) -> int:
        """Header URLs that answered 404 across every issuer."""
        return sum(issuer.headers_not_found for issuer in self.issuers)

    @property
    def headers_404(self) -> list[str]:
        """Accessions whose header URLs answered 404 across every issuer."""
        return [accession for issuer in self.issuers for accession in issuer.headers_404]

    @property
    def errors(self) -> list[str]:
        """Every final per-issuer failure, prefixed with its CIK."""
        return [
            f"{issuer.cik or issuer.ticker}: {error}"
            for issuer in self.issuers
            for error in issuer.errors
        ]

    @property
    def ok(self) -> bool:
        """True when no issuer ended with a final HTTP or parse error."""
        return not self.errors


def _as_mapping(payload: Any, url: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise FetchError(f"expected a JSON object from {url}")
    return payload


def _load_json(cache: RawCache, endpoint: str, cik: str, key: str | None) -> Mapping[str, Any]:
    body = cache.latest(endpoint, cik, key)
    if body is None:  # pragma: no cover - guarded by the caller
        raise FetchError(f"no cached {endpoint} for {cik}")
    return _as_mapping(json.loads(body), f"cached {endpoint} for {cik}")


def _document(
    cache: RawCache,
    client: SecClient,
    report: IssuerFetchReport,
    endpoint: str,
    cik: str,
    key: str | None,
    url: str,
    *,
    refresh: bool,
) -> Mapping[str, Any]:
    """Return the document at ``url``, fetching it only when needed.

    With ``refresh`` false a copy already in ``cache`` is parsed instead of
    fetched. Counts and the logical request count are updated on a fetch.
    """
    if not refresh and cache.latest(endpoint, cik, key) is not None:
        return _load_json(cache, endpoint, cik, key)
    try:
        document = client.get_json(url)
    except SecClientError:
        report.requests += 1
        raise
    report.requests += 1
    if endpoint == "submissions":
        report.submissions += 1
    elif endpoint == "submissions_page":
        report.pages += 1
    elif endpoint == "companyfacts":
        report.companyfacts += 1
    return document


def _fact_accessions(cik: str, document: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every ``(cik, accession)`` referenced by a ``us-gaap``/``ifrs-full`` fact.

    ``document`` is the parsed companyfacts JSON for ``cik``. The document is
    flattened with :func:`edgarpit.ingest.companyfacts.parse_companyfacts`, the
    same function the staging load uses, then restricted to the ``us-gaap`` and
    ``ifrs-full`` taxonomies; ``dei`` facts do not drive header fetches. The
    pairs carry the submitted CIK, not the document's, because that is the CIK
    the header URL needs. Returns sorted, de-duplicated pairs. Raises
    :class:`FetchError` when the document is not a valid companyfacts object.
    """
    try:
        facts = parse_companyfacts(document)
    except ValueError as error:
        raise FetchError(f"companyfacts for CIK {cik}: {error}") from error
    accessions = (
        facts.filter(pl.col("taxonomy").is_in(("us-gaap", "ifrs-full")))
        .get_column("accession")
        .unique()
        .sort()
        .to_list()
    )
    return [(cik, accession) for accession in accessions]


def _fetch_headers(
    cache: RawCache,
    client: SecClient,
    report: IssuerFetchReport,
    pairs: Sequence[tuple[str, str]],
) -> None:
    """Fetch every required header that is not already in the cache.

    A 404 is appended to ``report.headers_404`` and skipped; any other final
    :class:`SecClientError` stops this issuer (its remaining headers stay for
    the next run) and is recorded in ``report.errors``.
    """
    for cik, accession in pairs:
        if cache.latest("headers", cik, accession) is not None:
            report.headers_cached += 1
            continue
        try:
            client.get_text(header_url(cik, accession))
        except SecNotFound:
            report.headers_404.append(accession)
            report.requests += 1
            LOGGER.warning("[%s] header not found: %s", cik, accession)
            continue
        except SecClientError as error:
            report.requests += 1
            report.errors.append(f"header {accession}: {error}")
            return
        report.headers_fetched += 1
        report.requests += 1


def _fetch_issuer(
    issuer: Issuer, cache: RawCache, client: SecClient, *, refresh: bool
) -> IssuerFetchReport:
    """Fetch one issuer; never raises, records failures in the returned report."""
    report = IssuerFetchReport(cik=issuer.cik or "", ticker=issuer.ticker)
    if issuer.cik is None:
        report.errors.append("issuer has no CIK; resolve the universe first")
        return report
    cik = issuer.cik
    try:
        main = _document(
            cache, client, report, "submissions", cik, None, submissions_url(cik), refresh=refresh
        )
        filings = main.get("filings")
        page_specs = filings.get("files", []) if isinstance(filings, Mapping) else []
        if not isinstance(page_specs, list):
            raise FetchError(f"submissions for CIK {cik} has a malformed filings.files list")
        pages: list[Mapping[str, Any]] = []
        for spec in page_specs:
            if not isinstance(spec, Mapping) or not isinstance(spec.get("name"), str):
                raise FetchError(f"submissions for CIK {cik} has a malformed page entry")
            name = spec["name"]
            pages.append(
                _document(
                    cache,
                    client,
                    report,
                    "submissions_page",
                    cik,
                    name.removesuffix(".json"),
                    submissions_page_url(name),
                    refresh=refresh,
                )
            )
        companyfacts = _document(
            cache,
            client,
            report,
            "companyfacts",
            cik,
            None,
            companyfacts_url(cik),
            refresh=refresh,
        )
        try:
            frame = parse_submissions(main, pages)
        except ValueError as error:
            raise FetchError(f"submissions for CIK {cik}: {error}") from error
        pairs = required_accessions(frame, _fact_accessions(cik, companyfacts))
        _fetch_headers(cache, client, report, pairs)
    except (SecClientError, FetchError) as error:
        report.errors.append(str(error))
    return report


def submissions_unchanged(cached: Mapping[str, Any] | None, fresh: Mapping[str, Any]) -> bool:
    """Whether a freshly fetched submissions document shows no new filings.

    ``cached`` is the previously stored submissions document (``None`` when
    there is none) and ``fresh`` the document just fetched, both parsed JSON
    objects. An issuer is unchanged when the newest accession and the filing
    count of the two documents agree (decision A-34): ``data.sec.gov`` sends no
    ``ETag`` or ``Last-Modified``, so the document itself is the only signal, and
    a new filing always changes the newest accession or the count. A ``None``
    cache is never unchanged. Raises :class:`ValueError` when either document
    has no ``filings`` object with a ``recent`` list.

    Returns ``True`` when the issuer can be skipped for this run.
    """
    raise NotImplementedError("incremental fetch (decision A-34) lands with its own issue")


def fetch_universe(
    issuers: Sequence[Issuer],
    out_dir: Path | str,
    *,
    client: SecClient,
    refresh: bool = True,
    incremental: bool = False,
) -> FetchReport:
    """Fetch ``issuers`` into the raw cache rooted at ``out_dir``.

    ``client`` must be configured to write into ``out_dir`` (its
    :class:`~edgarpit.rawcache.RawCache` root); it is used for every request so
    the whole run shares one rate limiter. With ``refresh`` true submissions,
    pages and companyfacts get a new copy per run; headers are always fetched
    only when missing. Documents with ``refresh`` false come from the cache
    when present, so a warm cache is untouched. Returns a :class:`FetchReport`
    whose ``ok`` is false when any issuer failed with a non-404 error; a header
    404 is counted but not fatal. Never raises for an issuer-level failure.

    With ``incremental`` true an issuer whose submissions document is unchanged
    would skip its pages, companyfacts and headers and be counted in
    ``FetchReport.skipped_unchanged`` (decision A-34). That mode is not
    implemented yet: passing ``incremental=True`` raises
    :class:`NotImplementedError` rather than silently refetching everything.
    """
    if incremental:
        raise NotImplementedError(
            "incremental fetch (decision A-34) lands with its own issue; "
            "call fetch_universe with incremental=False"
        )
    start = time.monotonic()
    cache = RawCache(out_dir)
    report = FetchReport()
    for issuer in issuers:
        issuer_report = _fetch_issuer(issuer, cache, client, refresh=refresh)
        report.issuers.append(issuer_report)
        if issuer_report.errors:
            LOGGER.error(
                "[%s] %s: %s",
                issuer_report.cik or "-",
                issuer.ticker,
                "; ".join(issuer_report.errors),
            )
        else:
            LOGGER.info(
                "[%s] %s: %d documents, %d pages, %d headers fetched, %d cached, "
                "%d not found, %d requests",
                issuer_report.cik or "-",
                issuer.ticker,
                issuer_report.documents,
                issuer_report.pages,
                issuer_report.headers_fetched,
                issuer_report.headers_cached,
                issuer_report.headers_not_found,
                issuer_report.requests,
            )
    report.elapsed_seconds = time.monotonic() - start
    return report
