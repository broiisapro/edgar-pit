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

:func:`fetch_fx` is the fetch half: it reads the whole series once per call and
stores the body in the ``fx`` raw-cache slot only when it differs from the
latest cached body, because the series gains an observation every business day
(decision A-37).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as wall_time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from edgarpit.config import Settings
from edgarpit.rawcache import RawCache
from edgarpit.schemas import FxDailySchema
from edgarpit.sec_client import VALET_HOST, SecClient, valet_series_id

logger = logging.getLogger(__name__)

# A rate for ``d`` is known at ``d`` 17:00 America/Toronto (decision A-32).  The
# hour is a wall-clock time in the series' own zone, so it is 21:00 or 22:00 UTC
# depending on daylight saving; 17:00 is never in a DST gap or overlap.
TORONTO = ZoneInfo("America/Toronto")
KNOWN_AT_TIME = wall_time(17, 0)
FX_SOURCE = "valet"
DEFAULT_SERIES = "FXUSDCAD"

_FX_DTYPES: dict[str, Any] = {
    "series": pl.String,
    "rate_date": pl.Date,
    "value": pl.Float64,
    "known_at": pl.Datetime("us", "UTC"),
    "source": pl.String,
}


def valet_url(series: str, start: date | None = None, end: date | None = None) -> str:
    """URL of a Bank of Canada Valet daily observations document.

    ``series`` is the Valet series id (``FXUSDCAD``: Canadian dollars per US
    dollar), ``start`` and ``end`` inclusive observation-date bounds (``DATE``,
    rendered as ``YYYY-MM-DD``); without them Valet returns its whole history.
    The URL carries no timezone: Valet dates are calendar dates in
    ``America/Toronto``. Raises :class:`ValueError` for an empty or malformed
    series id or for ``start`` after ``end``.
    """
    series = valet_series_id(series)
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

    def get_text(self, url: str) -> str:
        """GET ``url`` and return its body as text, caching it under its series.

        The body is decoded with the response's charset (UTF-8 for Valet's JSON)
        and is exactly the text :meth:`get_json` parses; a caller that has to
        compare bodies byte for byte, as :func:`fetch_fx` does, encodes it back
        to UTF-8.
        """
        return self._client.get_text(url)

    def fetch(
        self,
        series: str = "FXUSDCAD",
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        """Return the Valet observations document for ``series`` and the date bounds.

        ``start`` and ``end`` are inclusive observation dates; without them the
        whole history is returned. The response is cached in the ``fx`` slot, so
        a client built with ``offline_dir`` can read it without the network.
        """
        return self.get_json(valet_url(series, start, end))

    def close(self) -> None:
        """Close the underlying HTTP client, when one was created."""
        self._client.close()

    def __enter__(self) -> ValetClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@dataclass(frozen=True)
class FxFetchReport:
    """The outcome of :func:`fetch_fx` for one Valet series.

    ``series`` is the Valet series id, ``observations`` the number of
    observations the fetched document yielded (one row each), ``first_date`` and
    ``last_date`` the earliest and latest observation dates (``None`` for an
    empty series) and ``stored`` whether a new raw-cache file was written;
    ``stored`` is ``False`` when the fetched body is identical to the latest
    cached body, which is what a second run on the same day sees.
    """

    series: str
    observations: int
    first_date: date | None
    last_date: date | None
    stored: bool


def fetch_fx(
    data_dir: Path,
    *,
    series: str = DEFAULT_SERIES,
    client: ValetClient | None = None,
) -> FxFetchReport:
    """Fetch a whole Valet series and store it when it changed.

    ``data_dir`` is the root of the raw cache (the data directory of a build),
    ``series`` a Valet series id (``FXUSDCAD``: Canadian dollars per US dollar)
    and ``client`` an optional :class:`ValetClient`; without one a client is
    built from :class:`~edgarpit.config.Settings`, which then needs a valid
    ``SEC_USER_AGENT``. Exactly one request is made per call - the series grows
    by an observation every business day, so unlike an immutable filing document
    it is fetched every time (decision A-37).

    The response is parsed before anything is written. Its body is stored under
    ``raw/fx/_/<series>/`` (decision A-30) only when its SHA-256 differs from
    the latest cached body for the series, so a repeat run on an unchanged
    series stores nothing and the cache keeps one file per change. Returns
    :class:`FxFetchReport` with the counts and the date range of the parsed
    series. Raises :class:`ValueError` for a malformed series id or a document
    :func:`parse_valet` rejects, and the
    :class:`~edgarpit.sec_client.SecClientError` family for HTTP failures.
    """
    series = valet_series_id(series)
    url = valet_url(series)
    if client is None:
        with ValetClient(Settings()) as owned:
            body = owned.get_text(url).encode("utf-8")
    else:
        body = client.get_text(url).encode("utf-8")

    frame = parse_valet(json.loads(body), series)
    cache = RawCache(data_dir)
    cached = cache.latest("fx", "", series)
    stored = cached is None or hashlib.sha256(cached).digest() != hashlib.sha256(body).digest()
    if stored:
        cache.write("fx", "", series, body, url=url, http_status=200)

    observations = frame.height
    first_date = frame["rate_date"][0] if observations else None
    last_date = frame["rate_date"][-1] if observations else None
    logger.info(
        "fx %s: %d observations %s..%s; %s",
        series,
        observations,
        first_date,
        last_date,
        "stored" if stored else "unchanged, nothing stored",
    )
    return FxFetchReport(
        series=series,
        observations=observations,
        first_date=first_date,
        last_date=last_date,
        stored=stored,
    )


def _known_at(rate_date: date) -> datetime:
    """Return the instant a rate observed on ``rate_date`` is treated as known.

    ``rate_date`` is a calendar date in ``America/Toronto``; the instant is that
    date's 17:00 local time, converted to a timezone-aware UTC datetime
    (decision A-32). It is 21:00 UTC under EDT and 22:00 UTC under EST.
    """
    return datetime.combine(rate_date, KNOWN_AT_TIME, tzinfo=TORONTO).astimezone(UTC)


def _observation_date(entry: Any, position: int) -> date:
    """Return an observation's ``d`` date, or raise ``ValueError`` naming ``position``."""
    raw = entry.get("d") if isinstance(entry, Mapping) else None
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"Valet observation {position} has no 'd' date: {raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"Valet observation {position} has an invalid date {raw!r}") from None


def _observation_value(entry: Mapping[str, Any], series: str, rate_date: date) -> float | None:
    """Return the observation's rate, or ``None`` when it carries no value.

    The value is the ``v`` field of the observation's ``series`` object. A
    missing object, a missing ``v`` and an empty string are Valet's way of
    saying "no observation today" (holidays), so they return ``None``; anything
    else must be a finite positive decimal, else ``ValueError`` names
    ``rate_date``.
    """
    raw = entry.get(series)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"Valet {series} value for {rate_date} is not an object: {raw!r}")
    text = raw.get("v")
    if text is None:
        return None
    if isinstance(text, str):
        text = text.strip()
        if not text:
            return None
    if isinstance(text, bool) or not isinstance(text, (str, int, float)):
        raise ValueError(f"Valet {series} value for {rate_date} is not a number: {text!r}")
    try:
        number = Decimal(text if isinstance(text, str) else str(text))
    except InvalidOperation:
        raise ValueError(
            f"Valet {series} value for {rate_date} is not a number: {text!r}"
        ) from None
    if not number.is_finite() or number <= 0:
        raise ValueError(
            f"Valet {series} value for {rate_date} must be a finite positive number: {text!r}"
        )
    return float(number)


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
    ``rate_date``; the skipped count goes to a debug log line. Raises
    :class:`ValueError` when ``doc`` is not a JSON object, when it has no
    ``seriesDetail`` entry for ``series`` (so another series' document can never
    parse as an empty frame), when it has no ``observations`` list, when an
    observation has no ``d`` date, when a value is present but not a finite
    positive number, or when two observations share a date.
    """
    if not isinstance(doc, Mapping):
        raise ValueError(f"Valet document is not a JSON object: {type(doc).__name__}")
    detail = doc.get("seriesDetail")
    if not isinstance(detail, Mapping) or series not in detail:
        raise ValueError(f"Valet document does not describe series {series!r}")
    observations = doc.get("observations")
    if not isinstance(observations, list):
        raise ValueError(f"Valet document for {series} has no 'observations' list")

    values: dict[date, float] = {}
    skipped = 0
    for position, entry in enumerate(observations):
        rate_date = _observation_date(entry, position)
        value = _observation_value(entry, series, rate_date)
        if value is None:
            skipped += 1
            continue
        if rate_date in values:
            raise ValueError(f"Valet document for {series} repeats the date {rate_date}")
        values[rate_date] = value
    if skipped:
        logger.debug("skipped %d %s observations without a value", skipped, series)

    rows = sorted(values.items())
    frame = pl.DataFrame(
        {
            "series": [series] * len(rows),
            "rate_date": [rate_date for rate_date, _ in rows],
            "value": [value for _, value in rows],
            "known_at": [_known_at(rate_date) for rate_date, _ in rows],
            "source": [FX_SOURCE] * len(rows),
        },
        schema=_FX_DTYPES,
    )
    FxDailySchema.validate(frame)
    return frame
