"""Rate-limited, retrying, caching HTTP client for SEC EDGAR.

One synchronous :class:`httpx.Client` (decision A-2) is used with:

- an in-house token bucket enforcing a minimum spacing of
  ``1 / settings.rate_limit_rps`` seconds between request starts (so no
  rolling one-second window ever holds more than the configured rate),
- tenacity retries (exponential backoff with jitter, capped at 60 s) on
  429/500/502/503/504 and transport errors, honouring ``Retry-After`` (both
  delta-seconds and HTTP-date forms, capped at 120 s), for at most six attempts,
- immediate failures on 403 (:class:`SecAccessDenied`, with a User-Agent hint)
  and 404 (:class:`SecNotFound`).

Online mode requires a valid ``"Name email"`` User-Agent (decision A-3) which
is validated before the first request; successful bodies are written to the
raw cache when one is given.  Offline mode serves the same URLs from a
:class:`~edgarpit.rawcache.RawCache` directory (the fixture tree uses that
layout uncompressed), never constructs a transport, needs no User-Agent and
raises :class:`OfflineMiss` for a key that is not stored.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt

from edgarpit.config import ConfigError, Settings, validate_user_agent
from edgarpit.rawcache import ENDPOINT_SUFFIX, RawCache, document_key

DATA_HOST = "https://data.sec.gov"
ARCHIVES_HOST = "https://www.sec.gov"
VALET_HOST = "https://www.bankofcanada.ca"

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 6
MAX_BACKOFF_SECONDS = 60.0
MAX_RETRY_AFTER_SECONDS = 120.0
REQUEST_TIMEOUT_SECONDS = 30.0

# A hair more than 1/rate so that no rolling one-second window can ever hold
# one request more than the configured rate, even after float rounding.
RATE_EPSILON_SECONDS = 1e-6

_SUBMISSIONS_RE = re.compile(r"/submissions/CIK(\d{10})\.json")
_SUBMISSIONS_PAGE_RE = re.compile(r"/submissions/(CIK(\d{10})-submissions-\d+)\.json")
_COMPANYFACTS_RE = re.compile(r"/api/xbrl/companyfacts/CIK(\d{10})\.json")
_FRAMES_RE = re.compile(r"/api/xbrl/frames/([^/]+)/([^/]+)/([^/]+)/([^/]+)\.json")
_COMPANYCONCEPT_RE = re.compile(r"/api/xbrl/companyconcept/CIK(\d{10})/([^/]+)/([^/]+)\.json")
_XBRL_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HEADER_RE = re.compile(r"/Archives/edgar/data/(\d+)/(\d+)/(\d{10}-\d{2}-\d{6})\.hdr\.sgml")
_FILING_INDEX_RE = re.compile(
    r"/Archives/edgar/data/(\d+)/(\d+)/(\d{10}-\d{2}-\d{6})-index-headers\.html"
)
_DOCUMENT_RE = re.compile(r"/Archives/edgar/data/(\d+)/(\d+)/([^/]+)")
_ACCESSION_DIGITS_RE = re.compile(r"(\d{10})(\d{2})(\d{6})")
_SERIES_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_VALET_RE = re.compile(r"/valet/observations/([A-Za-z0-9_.-]+)/json")


def valet_series_id(series: str) -> str:
    """Validate a Bank of Canada Valet series id and return it unchanged.

    ``series`` is a Valet identifier such as ``FXUSDCAD``: letters, digits,
    ``_``, ``.`` and ``-``, starting with a letter or digit. Dots are allowed
    inside an id (``FXUSDCAD.1``) but a ``..`` segment is rejected, so the id can
    never escape its directory when it becomes a raw-cache key. Raises
    :class:`ValueError` for an empty, malformed or dot-segment id.
    """
    if not _SERIES_RE.fullmatch(series) or ".." in series:
        raise ValueError(f"invalid Valet series id {series!r}")
    return series


class SecClientError(RuntimeError):
    """Base class for SEC client failures."""


class SecAccessDenied(SecClientError):
    """HTTP 403: SEC rejected the request; fix the User-Agent, do not retry."""


class SecNotFound(SecClientError):
    """HTTP 404: the resource does not exist; do not retry."""


class OfflineMiss(SecClientError):
    """The requested key is not present in the offline cache directory."""


class _RetryableStatus(SecClientError):
    def __init__(self, status: int, retry_after: float | None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def _cik10(cik: str) -> str:
    digits = cik.strip()
    if not digits.isdigit():
        raise ValueError(f"CIK must be digits, got {cik!r}")
    return digits.zfill(10)


def _xbrl_component(value: str, label: str) -> str:
    """Validate one XBRL path component (taxonomy, tag, unit or period).

    ``value`` is a component of a frames or companyconcept URL: a taxonomy
    (``us-gaap``, ``ifrs-full``, ``dei``), a tag (``Assets``), a unit
    (``USD``, ``shares``, ``USD-per-shares``) or a frame period (``CY2026Q2I``).
    Only letters, digits, ``_``, ``.`` and ``-`` are allowed, the first
    character must be alphanumeric, and ``__`` (the raw-cache key separator) and
    ``..`` (a path segment) are rejected, so a component can never change the
    URL's path or the cache key's shape. Raises :class:`ValueError` otherwise.
    """
    if not _XBRL_COMPONENT_RE.fullmatch(value) or "__" in value or ".." in value:
        raise ValueError(f"invalid XBRL {label} {value!r}")
    return value


def submissions_url(cik: str) -> str:
    """URL of the submissions document for a CIK."""
    return f"{DATA_HOST}/submissions/CIK{_cik10(cik)}.json"


def submissions_page_url(name: str) -> str:
    """URL of a paginated submissions page, e.g. ``CIK0000320193-submissions-001.json``."""
    return f"{DATA_HOST}/submissions/{name}"


def companyfacts_url(cik: str) -> str:
    """URL of the companyfacts document for a CIK."""
    return f"{DATA_HOST}/api/xbrl/companyfacts/CIK{_cik10(cik)}.json"


def frames_url(taxonomy: str, tag: str, unit: str, period: str) -> str:
    """URL of one XBRL frame: every issuer's value of a tag in a period.

    ``taxonomy`` is ``us-gaap``, ``ifrs-full`` or ``dei``; ``tag`` an element
    name such as ``EntityPublicFloat``; ``unit`` a unit id such as ``USD``,
    ``shares`` or ``USD-per-shares``; and ``period`` an instant or duration
    frame such as ``CY2026Q2I``. Frames are static snapshots of one calendar
    period, so they are cached globally (decision A-36). Raises
    :class:`ValueError` for a malformed component.
    """
    return (
        f"{DATA_HOST}/api/xbrl/frames/{_xbrl_component(taxonomy, 'taxonomy')}/"
        f"{_xbrl_component(tag, 'tag')}/{_xbrl_component(unit, 'unit')}/"
        f"{_xbrl_component(period, 'period')}.json"
    )


def companyconcept_url(cik: str, taxonomy: str, tag: str) -> str:
    """URL of one issuer's facts for one concept.

    ``cik`` is the issuer's CIK (digits, zero-padded), ``taxonomy`` e.g.
    ``us-gaap`` or ``ifrs-full`` and ``tag`` the element name (``Assets``).
    The document holds every unit and period the issuer ever tagged, so it is
    cached under the issuer's CIK (decision A-36). Raises :class:`ValueError`
    for a malformed CIK or component.
    """
    return (
        f"{DATA_HOST}/api/xbrl/companyconcept/CIK{_cik10(cik)}/"
        f"{_xbrl_component(taxonomy, 'taxonomy')}/{_xbrl_component(tag, 'tag')}.json"
    )


def header_url(cik: str, accession: str) -> str:
    """URL of the immutable SGML header that carries the acceptance time."""
    return (
        f"{ARCHIVES_HOST}/Archives/edgar/data/{int(_cik10(cik))}/"
        f"{accession.replace('-', '')}/{accession}.hdr.sgml"
    )


def company_tickers_url() -> str:
    """URL of SEC's ticker-to-CIK mapping."""
    return f"{ARCHIVES_HOST}/files/company_tickers.json"


def filing_index_url(cik: str, accession: str) -> str:
    """URL of the ``-index-headers.html`` document that lists a filing's files.

    ``cik`` is the issuer's CIK (digits, zero-padded) and ``accession`` the
    filing's ``NNNNNNNNNN-YY-NNNNNN`` number. The page carries each document's
    SGML ``<TYPE>`` and ``<FILENAME>``, which is how 8-K documents are found
    (decision A-30); it is immutable, so it is cached forever.
    """
    return (
        f"{ARCHIVES_HOST}/Archives/edgar/data/{int(_cik10(cik))}/"
        f"{accession.replace('-', '')}/{accession}-index-headers.html"
    )


def document_url(cik: str, accession: str, filename: str) -> str:
    """URL of one document inside a filing directory.

    ``cik`` is the issuer's CIK (digits, zero-padded), ``accession`` the filing's
    ``NNNNNNNNNN-YY-NNNNNN`` number and ``filename`` the EDGAR file name from
    the filing index (``aapl-20260730.htm``). The file name is percent-encoded,
    so a space or a non-ASCII character cannot break the URL. Raises
    :class:`ValueError` for an empty file name or one with a path separator.
    """
    if not filename or "/" in filename or "\\" in filename:
        raise ValueError(f"invalid document file name {filename!r}")
    quoted = quote(filename, safe="._-~")
    return (
        f"{ARCHIVES_HOST}/Archives/edgar/data/{int(_cik10(cik))}/"
        f"{accession.replace('-', '')}/{quoted}"
    )


def classify(url: str) -> tuple[str, str, str]:
    """Split a known SEC or Bank of Canada URL into ``(endpoint, cik10, key)``.

    Keys are the page stem for ``submissions_page``, the accession for
    ``headers`` and ``filing_index``, ``<accession>__<filename>`` for
    ``documents`` (decision A-30), the series id for ``fx`` (Valet),
    ``<taxonomy>__<tag>__<unit>__<period>`` for a global ``frames`` document and
    ``<taxonomy>__<tag>`` for an issuer's ``companyconcept`` document (decision
    A-36); the main documents and ``company_tickers`` use an empty key. The CIK
    of a ``documents`` URL is the directory's, so a filing an agent submitted for
    an issuer still caches under the issuer's CIK; the accession is rebuilt from
    the directory's 18 digits. Raises :class:`ValueError` for URLs outside the
    documented endpoints or with a malformed XBRL path component.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path
    if host == "data.sec.gov":
        match = _SUBMISSIONS_RE.fullmatch(path)
        if match:
            return ("submissions", match.group(1), "")
        match = _SUBMISSIONS_PAGE_RE.fullmatch(path)
        if match:
            return ("submissions_page", match.group(2), match.group(1))
        match = _COMPANYFACTS_RE.fullmatch(path)
        if match:
            return ("companyfacts", match.group(1), "")
        match = _FRAMES_RE.fullmatch(path)
        if match:
            taxonomy, tag, unit, period = match.groups()
            key = "__".join(
                _xbrl_component(part, label)
                for part, label in zip(
                    (taxonomy, tag, unit, period),
                    ("taxonomy", "tag", "unit", "period"),
                    strict=True,
                )
            )
            return ("frames", "", key)
        match = _COMPANYCONCEPT_RE.fullmatch(path)
        if match:
            cik, taxonomy, tag = match.groups()
            key = "__".join(
                _xbrl_component(part, label)
                for part, label in zip((taxonomy, tag), ("taxonomy", "tag"), strict=True)
            )
            return ("companyconcept", cik, key)
    if host == "www.sec.gov":
        if path == "/files/company_tickers.json":
            return ("company_tickers", "", "")
        match = _HEADER_RE.fullmatch(path)
        if match:
            cik_int, no_dashes, accession = match.groups()
            if no_dashes != accession.replace("-", ""):
                raise ValueError(f"header URL accession does not match its directory: {url}")
            return ("headers", f"{int(cik_int):010d}", accession)
        match = _FILING_INDEX_RE.fullmatch(path)
        if match:
            cik_int, no_dashes, accession = match.groups()
            if no_dashes != accession.replace("-", ""):
                raise ValueError(f"filing index URL accession does not match its directory: {url}")
            return ("filing_index", f"{int(cik_int):010d}", accession)
        match = _DOCUMENT_RE.fullmatch(path)
        if match:
            cik_int, no_dashes, filename = match.groups()
            accession_match = _ACCESSION_DIGITS_RE.fullmatch(no_dashes)
            if accession_match is None:
                raise ValueError(f"document URL has no accession directory: {url}")
            accession = "-".join(accession_match.groups())
            return ("documents", f"{int(cik_int):010d}", document_key(accession, unquote(filename)))
    if host == "www.bankofcanada.ca":
        match = _VALET_RE.fullmatch(path)
        if match:
            return ("fx", "", valet_series_id(match.group(1)))
    raise ValueError(f"cannot classify URL: {url}")


def _parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header (RFC 9110) into seconds to wait.

    Accepts both forms: delta-seconds (a non-negative integer) and an
    HTTP-date (IMF-fixdate or the obsolete RFC 850 / asctime formats, via
    :func:`email.utils.parsedate_to_datetime`). A date at or before ``now``
    (default: the current UTC time) yields ``0``. Returns ``None`` for a
    missing or unparseable value so the caller falls back to backoff.
    """
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        # The obsolete asctime form carries no zone and is defined as GMT.
        when = when.replace(tzinfo=UTC)
    moment = now if now is not None else datetime.now(UTC)
    return max(0.0, (when - moment).total_seconds())


def _retry_wait(state: RetryCallState) -> float:
    """Wait before the next attempt: ``Retry-After`` or jittered backoff.

    A ``Retry-After`` wait is capped at :data:`MAX_RETRY_AFTER_SECONDS`; the
    exponential backoff (base ``0.5 * 2 ** (attempt - 1)``, jitter 0.5 to
    1.5 times the base) is capped at :data:`MAX_BACKOFF_SECONDS`.
    """
    outcome = state.outcome
    if outcome is not None:
        error = outcome.exception()
        if isinstance(error, _RetryableStatus) and error.retry_after is not None:
            return min(MAX_RETRY_AFTER_SECONDS, max(0.0, error.retry_after))
    base: float = 0.5 * 2 ** (int(state.attempt_number) - 1)
    return min(MAX_BACKOFF_SECONDS, base * random.uniform(0.5, 1.5))


class SecClient:
    """Synchronous SEC client with rate limiting, retries and optional caching."""

    def __init__(
        self,
        settings: Settings,
        *,
        cache: RawCache | None = None,
        offline_dir: Path | str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.clock = clock
        self.sleep = sleep
        self.transport = transport
        self._next_start = 0.0
        self._client: httpx.Client | None = None
        self._user_agent: str | None = None
        self._offline: RawCache | None = None
        if offline_dir is not None:
            self._offline = RawCache(offline_dir)
        else:
            try:
                self._user_agent = validate_user_agent(settings.sec_user_agent or "")
            except ConfigError as error:
                raise ConfigError(
                    f"online SEC access needs a valid SEC_USER_AGENT before any request: {error}"
                ) from error

    @property
    def offline(self) -> bool:
        return self._offline is not None

    def _client_for_requests(self) -> httpx.Client:
        if self._client is None:
            headers = {"User-Agent": self._user_agent or "", "Accept-Encoding": "gzip, deflate"}
            self._client = httpx.Client(
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                transport=self.transport,
                follow_redirects=True,
            )
        return self._client

    def _wait_for_slot(self) -> None:
        now = self.clock()
        if now < self._next_start:
            self.sleep(self._next_start - now)
            now = self.clock()
        spacing = 1.0 / self.settings.rate_limit_rps + RATE_EPSILON_SECONDS
        self._next_start = max(now, self._next_start) + spacing

    def _send(self, url: str) -> httpx.Response:
        self._wait_for_slot()
        response = self._client_for_requests().get(url)
        if response.status_code == 200:
            return response
        if response.status_code == 403:
            raise SecAccessDenied(
                f"SEC returned 403 for {url}; set SEC_USER_AGENT to a real "
                '"Name email" with your contact address and do not retry.'
            )
        if response.status_code == 404:
            raise SecNotFound(f"SEC returned 404 for {url}")
        if response.status_code in RETRY_STATUSES:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise _RetryableStatus(response.status_code, retry_after)
        raise SecClientError(f"unexpected HTTP {response.status_code} for {url}")

    def _request(self, url: str) -> httpx.Response:
        retrying = Retrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=_retry_wait,
            sleep=self.sleep,
            retry=retry_if_exception_type((_RetryableStatus, httpx.TransportError)),
            reraise=True,
        )
        response: httpx.Response | None = None
        for attempt in retrying:
            with attempt:
                response = self._send(url)
        if response is None:  # pragma: no cover - Retrying always runs at least once
            raise SecClientError(f"no attempt was made for {url}")
        return response

    def _write_cache(self, url: str, response: httpx.Response) -> None:
        if self.cache is None:
            return
        endpoint, cik, key = classify(url)
        if endpoint not in ENDPOINT_SUFFIX:
            return
        self.cache.write(
            endpoint,
            cik,
            key,
            response.content,
            url=url,
            http_status=response.status_code,
        )

    def _offline_body(self, url: str) -> bytes:
        if self._offline is None:  # pragma: no cover - guarded by the caller
            raise OfflineMiss(url)
        endpoint, cik, key = classify(url)
        body = self._offline.latest(endpoint, cik, key)
        if body is None:
            raise OfflineMiss(f"no cached response for {url} in {self._offline.root}")
        return body

    def get_text(self, url: str) -> str:
        """GET ``url`` and return the body as text."""
        if self._offline is not None:
            return self._offline_body(url).decode("utf-8")
        response = self._request(url)
        self._write_cache(url, response)
        return response.text

    def get_json(self, url: str) -> dict[str, Any]:
        """GET ``url`` and return the parsed JSON object."""
        if self._offline is not None:
            payload = json.loads(self._offline_body(url))
        else:
            payload = json.loads(self.get_text(url))
        if not isinstance(payload, dict):
            raise SecClientError(f"expected a JSON object from {url}")
        return payload

    def close(self) -> None:
        """Close the underlying HTTP client, when one was created."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> SecClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
