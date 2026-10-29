#!/usr/bin/env python3
"""Build the offline Bank of Canada exchange-rate fixtures.

The fixtures are the Valet API responses for the two USD/CAD daily series the
project needs (decision A-32), stored byte for byte: nothing is trimmed,
re-serialized or edited.

- ``FXUSDCAD``: daily average exchange rate, Canadian dollars per US dollar,
  published by 16:30 ET on every Bank of Canada business day, from 2017-01-03.
- ``IEXE0101``: retired noon rate, Canadian dollars per US dollar, 2007-05-01
  to 2017-04-28.  The Bank stopped updating it on 2017-04-28 when the daily
  average methodology replaced the noon and closing rates.

``FXUSDCAD`` feeds the build, so it lives in the merged SEC fixture cache
(decision A-44) and its row joins that cache's manifest; the retired noon
series is research evidence only and stays in its own root, from which nothing
is ever ingested.  Layout (decisions A-4 and A-30, uncompressed so PR diffs are
reviewable)::

    tests/fixtures/sec/raw/fx/_/FXUSDCAD/<fetched_at>.json
    tests/fixtures/sec/raw/manifest.jsonl
    tests/fixtures/fx/raw/fx/_/IEXE0101/<fetched_at>.json
    tests/fixtures/fx/raw/manifest.jsonl

``fetched_at`` is ``YYYYMMDDTHHMMSSffffffZ`` (UTC, microseconds).  Each
manifest carries one row per stored file with the SHA-256 of the stored bytes,
``"trimmed": false`` and the Valet URL (decision A-15).

One GET per series on a normal run, at most two attempts per series, one
request per second.  A response is validated before anything is written: the
series must appear in ``seriesDetail``, observation dates must be ISO dates in
strictly increasing order, and every value must be a positive decimal.  A
re-run replaces the stored file and the manifest row of each series and leaves
every other row of the target manifest untouched, so the tree always holds
exactly one file per series.

Run it with ``uv run python tools/build_fx_fixture.py``.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SEC_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "sec"
EVIDENCE_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "fx"

VALET_HOST = "https://www.bankofcanada.ca"
SERIES: tuple[str, ...] = ("FXUSDCAD", "IEXE0101")

# Where each series is stored.  ``FXUSDCAD`` is ingested by the build, so it
# belongs to the merged SEC cache (decision A-44); ``IEXE0101`` is evidence of
# the pre-2017 methodology and is never ingested, so it stays in its own root
# (decision A-39).
SERIES_ROOT: dict[str, Path] = {
    "FXUSDCAD": SEC_FIXTURE_ROOT,
    "IEXE0101": EVIDENCE_FIXTURE_ROOT,
}

USER_AGENT = "edgar-pit fixtures (+https://github.com/broiisapro/edgar-pit)"
RATE_LIMIT_RPS = 1.0
MAX_ATTEMPTS = 2
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class FixtureError(RuntimeError):
    """Raised when the FX fixtures cannot be built."""


def fixture_root(series: str) -> Path:
    """Return the fixture root ``series`` is stored under.

    ``series`` is a Bank of Canada Valet series id from :data:`SERIES`; the
    result is :data:`SEC_FIXTURE_ROOT` for the ingested daily series and
    :data:`EVIDENCE_FIXTURE_ROOT` for the evidence-only noon series. Raises
    :class:`FixtureError` for an unknown series.
    """
    try:
        return SERIES_ROOT[series]
    except KeyError:
        known = ", ".join(SERIES)
        raise FixtureError(f"unknown FX series {series!r}; expected one of {known}") from None


def manifest_path(root: Path) -> Path:
    """Return the manifest path of the raw cache under ``root``."""
    return root / "raw" / "manifest.jsonl"


def observation_url(series: str) -> str:
    """Return the Valet URL of the full observation history of ``series``."""
    return f"{VALET_HOST}/valet/observations/{series}/json"


class RateLimiter:
    """Minimum spacing between request starts; guarantees <= 1 req/s."""

    def __init__(self, rate: float) -> None:
        self._spacing = 1.0 / rate
        self._next_at = 0.0

    def wait(self) -> None:
        """Sleep until the next request may start."""
        now = time.monotonic()
        if now < self._next_at:
            time.sleep(self._next_at - now)
            now = time.monotonic()
        self._next_at = now + self._spacing


def fetch(url: str, *, limiter: RateLimiter) -> tuple[int, bytes]:
    """GET ``url``; return ``(status, body)`` or raise :class:`FixtureError`.

    At most ``MAX_ATTEMPTS`` requests are made, so a normal run plus one retry
    stays within the two requests per series allowed for this fixture.
    """
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        limiter.wait()
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return response.status, body
        except urllib.error.HTTPError as error:
            if error.code not in RETRY_STATUSES:
                raise FixtureError(f"unexpected HTTP {error.code} for {url}") from error
            last_error = f"HTTP {error.code}"
            retry_after = error.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.0
            if attempt < MAX_ATTEMPTS:
                time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = f"{type(error).__name__}: {error}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(1.0)
    raise FixtureError(f"giving up on {url} after {MAX_ATTEMPTS} attempts ({last_error})")


def validate_document(document: Any, series: str) -> None:
    """Raise :class:`FixtureError` unless ``document`` is a usable Valet response.

    ``document`` is the decoded JSON body.  The series must be described in
    ``seriesDetail`` with ``dateFormat`` ``daily``; ``observations`` must be a
    non-empty list of ``{"d": "YYYY-MM-DD", series: {"v": "<positive decimal>"}}``
    entries whose dates are strictly increasing.
    """
    if not isinstance(document, dict):
        raise FixtureError(f"{series}: response is not a JSON object")
    details = document.get("seriesDetail")
    if not isinstance(details, dict) or series not in details:
        raise FixtureError(f"{series}: seriesDetail does not describe the series")
    detail = details[series]
    if not isinstance(detail, dict) or detail.get("dateFormat") != "daily":
        raise FixtureError(f"{series}: expected dateFormat 'daily'")
    observations = document.get("observations")
    if not isinstance(observations, list) or not observations:
        raise FixtureError(f"{series}: response has no observations")

    previous: date | None = None
    for position, observation in enumerate(observations):
        if not isinstance(observation, dict):
            raise FixtureError(f"{series}: observation {position} is not an object")
        raw_date = observation.get("d")
        if not isinstance(raw_date, str):
            raise FixtureError(f"{series}: observation {position} has no date")
        try:
            rate_date = date.fromisoformat(raw_date)
        except ValueError as error:
            raise FixtureError(f"{series}: observation {position} has date {raw_date!r}") from error
        if previous is not None and rate_date <= previous:
            raise FixtureError(f"{series}: dates are not strictly increasing at {raw_date}")
        previous = rate_date

        entry = observation.get(series)
        if not isinstance(entry, dict) or "v" not in entry:
            raise FixtureError(f"{series}: observation {raw_date} has no value")
        try:
            value = Decimal(str(entry["v"]))
        except InvalidOperation as error:
            raise FixtureError(
                f"{series}: observation {raw_date} has value {entry['v']!r}"
            ) from error
        if not value.is_finite() or value <= 0:
            raise FixtureError(f"{series}: observation {raw_date} has non-positive value {value}")


def now_stamp() -> str:
    """Return the UTC cache stamp used in fixture file names."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def load_manifest_rows(root: Path) -> list[dict[str, Any]]:
    """Read the manifest under ``root``, or return an empty list when absent."""
    path = manifest_path(root)
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def write_manifest(root: Path, rows: list[dict[str, Any]]) -> None:
    """Write ``rows`` sorted by path, one JSON object per line."""
    path = manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in sorted(rows, key=lambda row: row["path"])
    )
    path.write_text(lines, encoding="utf-8")


def store_series(
    rows: list[dict[str, Any]],
    *,
    root: Path,
    series: str,
    url: str,
    body: bytes,
    http_status: int,
) -> Path:
    """Store ``body`` for ``series`` under ``root`` and return the written path.

    Existing files and manifest rows of the same series are removed first so a
    re-run leaves one file per series; rows of any other endpoint or series in
    ``rows`` are kept.  ``body`` is written exactly as received.
    """
    kept: list[dict[str, Any]] = []
    for row in rows:
        if row["path"].startswith(f"raw/fx/_/{series}/"):
            (root / row["path"]).unlink(missing_ok=True)
        else:
            kept.append(row)
    rows[:] = kept

    stamp = now_stamp()
    relative = Path("raw") / "fx" / "_" / series / f"{stamp}.json"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():  # pragma: no cover - microsecond names make this absurd
        raise FixtureError(f"refusing to overwrite {path}")
    path.write_bytes(body)
    rows.append(
        {
            "path": relative.as_posix(),
            "url": url,
            "endpoint": "fx",
            "cik": "",
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "fetched_at": stamp,
            "http_status": http_status,
            "trimmed": False,
        }
    )
    return path


def build() -> dict[str, dict[str, Any]]:
    """Fetch, validate and store every series; return a per-series summary.

    All responses are fetched and validated before the first file is written,
    so a failed run leaves the stored tree untouched.  Each series lands in its
    :func:`fixture_root` and only that series' manifest row is replaced, so the
    daily series joins the merged SEC fixture cache without disturbing the
    submissions, header or text rows already listed there.
    """
    limiter = RateLimiter(RATE_LIMIT_RPS)
    pending: list[tuple[str, str, int, bytes, dict[str, Any]]] = []

    for series in SERIES:
        url = observation_url(series)
        print(f"[{series}] {url}", flush=True)
        status, body = fetch(url, limiter=limiter)
        try:
            document = json.loads(body)
        except json.JSONDecodeError as error:
            raise FixtureError(f"{series}: response is not JSON ({error})") from error
        validate_document(document, series)
        pending.append((series, url, status, body, document))

    roots: list[Path] = []
    for series in SERIES:
        root = fixture_root(series)
        if root not in roots:
            roots.append(root)
    manifests = {root: load_manifest_rows(root) for root in roots}

    summary: dict[str, dict[str, Any]] = {}
    for series, url, status, body, document in pending:
        root = fixture_root(series)
        path = store_series(
            manifests[root], root=root, series=series, url=url, body=body, http_status=status
        )
        observations = document["observations"]
        summary[series] = {
            "path": path.relative_to(root).as_posix(),
            "observations": len(observations),
            "first": observations[0]["d"],
            "last": observations[-1]["d"],
            "bytes": len(body),
        }

    for root in roots:
        write_manifest(root, manifests[root])
    return summary


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    summary = build()
    for series, entry in sorted(summary.items()):
        print(
            f"{series}: {entry['observations']} observations "
            f"({entry['first']}..{entry['last']}) -> {entry['path']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
