"""Bank of Canada Valet daily FX series (decision A-32).

Valet serves one JSON document per series and date range from
``https://www.bankofcanada.ca/valet/observations/<series>/json``. The series in
scope is ``FXUSDCAD`` (Canadian dollars per US dollar, business daily, from
2017-01-03); ``ValetClient`` shares the SEC client's rate limiter, retries and
raw cache, so a Valet run obeys the same fair-access rules and lands in the
``raw/fx/_/<series>/`` slot of the cache (decision A-30).

The publication time is not in the document. A rate for date ``d`` is therefore
treated as known at ``d`` 17:00 ``America/Toronto`` (the Bank publishes around
16:30 ET), which is never earlier than publication and so never leaks a value.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from edgarpit.config import Settings
from edgarpit.rawcache import RawCache
from edgarpit.sec_client import VALET_HOST, SecClient

_SERIES_RE = re.compile(r"[A-Za-z0-9_.-]+")


def valet_url(series: str, start: date | None = None, end: date | None = None) -> str:
    """URL of a Bank of Canada Valet daily observations document.

    ``series`` is the Valet series id (``FXUSDCAD``: Canadian dollars per US
    dollar), ``start`` and ``end`` inclusive observation-date bounds (``DATE``,
    rendered as ``YYYY-MM-DD``); without them Valet returns its whole history.
    The URL carries no timezone: Valet dates are calendar dates in
    ``America/Toronto``. Raises :class:`ValueError` for an empty or malformed
    series id or for ``start`` after ``end``.
    """
    if not _SERIES_RE.fullmatch(series):
        raise ValueError(f"invalid Valet series id {series!r}")
    if start is not None and end is not None and start > end:
        raise ValueError(f"start {start} is after end {end}")
    query: list[str] = []
    if start is not None:
        query.append(f"start_date={start.isoformat()}")
    if end is not None:
        query.append(f"end_date={end.isoformat()}")
    url = f"{VALET_HOST}/valet/observations/{series}/json"
    return f"{url}?{'&'.join(query)}" if query else url


class ValetClient:
    """Bank of Canada Valet client with the SEC client's limiter, retries and cache.

    ``settings`` supplies the rate limit; the client sends the same declared
    contact ``SEC_USER_AGENT`` to the Bank of Canada, which asks automated
    clients to identify themselves. ``cache`` stores every response in the
    ``fx`` endpoint (key = series id), ``offline_dir`` serves responses from a
    cache or fixture tree instead of the network and needs no User-Agent, and
    ``clock``, ``sleep`` and ``transport`` are the injection points used by the
    SEC client's tests. A 403 raises
    :class:`~edgarpit.sec_client.SecAccessDenied`, a 404
    :class:`~edgarpit.sec_client.SecNotFound`, and a final 429/5xx or transport
    error :class:`~edgarpit.sec_client.SecClientError`, after the same six
    attempts as an SEC request.
    """

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
        self._client = SecClient(
            settings,
            cache=cache,
            offline_dir=offline_dir,
            clock=clock,
            sleep=sleep,
            transport=transport,
        )

    def get_json(self, url: str) -> dict[str, Any]:
        """GET ``url`` and return its JSON object, caching it under its series."""
        return self._client.get_json(url)

    def fetch(
        self,
        series: str = "FXUSDCAD",
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        """Return the Valet observations document for ``series`` and the date bounds.

        ``start`` and ``end`` are inclusive observation dates; without them the
        whole history is returned. The response is cached in the ``fx`` slot, so
        a later ``fetch`` of the same series reads the cache in offline mode.
        """
        return self.get_json(valet_url(series, start, end))

    def close(self) -> None:
        """Close the underlying HTTP client, when one was created."""
        self._client.close()

    def __enter__(self) -> ValetClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def parse_valet(doc: Mapping[str, Any], series: str = "FXUSDCAD") -> pl.DataFrame:
    """Turn a Valet observations document into ``raw.fx_daily`` rows.

    ``doc`` is the parsed JSON of
    ``valet_url(series)``: ``{"observations": [{"d": "2023-10-31",
    "FXUSDCAD": {"v": "1.3659"}}, ...]}``. ``series`` is the series id whose
    value each observation carries. Returns a frame valid under
    :class:`edgarpit.schemas.FxDailySchema`: one row per observation date with
    ``series``, ``rate_date`` (``DATE``, the observation's ``d``),
    ``value`` (``DOUBLE``, the rate in the series' quotation: Canadian dollars
    per US dollar for ``FXUSDCAD``), ``known_at`` (timezone-aware UTC:
    ``rate_date`` 17:00 ``America/Toronto``, decision A-32) and
    ``source='valet'``. Observations without a value for ``series`` (Valet emits
    an empty string on holidays) are dropped, and rows are ordered by
    ``rate_date``. Raises :class:`ValueError` when ``doc`` has no
    ``observations`` list, when an observation has no ``d`` date, when a value is
    present but not a finite positive number, or when two observations share a
    date.
    """
    raise NotImplementedError("the Valet parser lands with the FX ingest issue")
