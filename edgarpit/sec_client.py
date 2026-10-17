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
from urllib.parse import urlparse

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt

from edgarpit.config import ConfigError, Settings, validate_user_agent
from edgarpit.rawcache import ENDPOINT_SUFFIX, RawCache

DATA_HOST = "https://data.sec.gov"
ARCHIVES_HOST = "https://www.sec.gov"

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
_HEADER_RE = re.compile(r"/Archives/edgar/data/(\d+)/(\d+)/(\d{10}-\d{2}-\d{6})\.hdr\.sgml")


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


def submissions_url(cik: str) -> str:
    """URL of the submissions document for a CIK."""
    return f"{DATA_HOST}/submissions/CIK{_cik10(cik)}.json"


def submissions_page_url(name: str) -> str:
    """URL of a paginated submissions page, e.g. ``CIK0000320193-submissions-001.json``."""
    return f"{DATA_HOST}/submissions/{name}"


def companyfacts_url(cik: str) -> str:
    """URL of the companyfacts document for a CIK."""
    return f"{DATA_HOST}/api/xbrl/companyfacts/CIK{_cik10(cik)}.json"


def header_url(cik: str, accession: str) -> str:
    """URL of the immutable SGML header that carries the acceptance time."""
    return (
        f"{ARCHIVES_HOST}/Archives/edgar/data/{int(_cik10(cik))}/"
        f"{accession.replace('-', '')}/{accession}.hdr.sgml"
    )


def company_tickers_url() -> str:
    """URL of SEC's ticker-to-CIK mapping."""
    return f"{ARCHIVES_HOST}/files/company_tickers.json"


def classify(url: str) -> tuple[str, str, str]:
    """Split a known SEC URL into ``(endpoint, cik10, key)``.

    Keys are the page stem for ``submissions_page`` and the accession for
    ``headers``; the main documents and ``company_tickers`` use an empty key.
    Raises :class:`ValueError` for URLs outside the documented endpoints.
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
    if host == "www.sec.gov":
        if path == "/files/company_tickers.json":
            return ("company_tickers", "", "")
        match = _HEADER_RE.fullmatch(path)
        if match:
            cik_int, no_dashes, accession = match.groups()
            if no_dashes != accession.replace("-", ""):
                raise ValueError(f"header URL accession does not match its directory: {url}")
            return ("headers", f"{int(cik_int):010d}", accession)
    raise ValueError(f"cannot classify SEC URL: {url}")


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
