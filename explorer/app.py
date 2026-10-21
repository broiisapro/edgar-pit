"""Streamlit explorer over the point-in-time query layer (issues #31, #85).

Reads a DuckDB database through :mod:`edgarpit.query` only (decision A-21). A
value is known at time ``t`` if and only if its filing's ``acceptance_ts <= t``;
the "as of" control uses that rule to mark and grey out versions.

Two database modes:

- **Local.** ``EDGARPIT_DB`` (or the settings default) names a built database.
  Provenance is read from ``build-info.json`` or ``manifest.json`` beside it
  when one is present.
- **Released.** When that file is missing and ``EDGARPIT_DB_URL`` is set, the
  released database is downloaded once, streamed to a cache file (at most
  500 MB) and verified against ``EDGARPIT_DB_SHA256`` when that is set. The
  result is cached across reruns and sessions with ``st.cache_resource`` and
  opened read-only, so the page works on Streamlit Community Cloud without a
  local build. The sibling ``manifest.json`` supplies the build provenance.

The release publishes these assets:

- ``edgarpit.duckdb`` — the build database this page reads;
- ``manifest.json`` — build timestamp, SEC fetch range and the SHA-256 and
  schema of every released file;
- ``fundamentals_pit.parquet``, ``ratios_pit.parquet``, ``events_8k.parquet``,
  ``filings.parquet`` and ``facts_raw.parquet`` — the five released tables.

Run locally with ``streamlit run explorer/app.py``; the tests drive it
headlessly with ``streamlit.testing.v1.AppTest``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import altair as alt
import duckdb
import httpx
import polars as pl
import streamlit as st

from edgarpit import query
from edgarpit.config import Settings

STATUS_ORDER = [
    "version 1",
    "restatement",
    "split adjustment",
    "later version",
    "not yet accepted",
]
STATUS_COLORS = ["#1f77b4", "#d62728", "#9467bd", "#ff7f0e", "#9e9e9e"]
STATUS_SHAPES = ["circle", "diamond", "triangle-up", "square", "cross"]

VERSION_COLUMNS = [
    "period_end",
    "version",
    "value",
    "unit",
    "accepted",
    "known_at_asof",
    "status",
    "restatement_kind",
    "accession",
]

MAX_DATABASE_BYTES = 500 * 1024 * 1024
MAX_METADATA_BYTES = 5 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 300.0
METADATA_TIMEOUT_SECONDS = 30.0
CACHE_DIRNAME = "edgarpit-explorer"

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_FETCH_STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


class ReleaseDownloadError(RuntimeError):
    """The released database is missing, too large, corrupt or fails its digest."""


@dataclass(frozen=True)
class ReleaseInfo:
    """Build provenance shown under the page title.

    ``fetch_range`` is the oldest and newest SEC ``fetched_at`` instant of the
    raw cache that fed the build, timezone-aware UTC, or ``None`` when no build
    metadata was available. ``build_timestamp`` is the release export time in
    UTC when the manifest carries one.
    """

    fetch_range: tuple[datetime, datetime] | None = None
    build_timestamp: datetime | None = None


@dataclass(frozen=True)
class DatabaseSource:
    """One database the explorer can read and where it came from.

    ``path`` is the DuckDB file, ``info`` its build provenance (possibly empty)
    and ``downloaded`` says whether this process fetched it from a release.
    """

    path: Path
    info: ReleaseInfo
    downloaded: bool


def filing_url(cik: str, accession: str) -> str:
    """Return the EDGAR filing-index URL for ``accession`` of issuer ``cik``.

    ``cik`` is a 10-character CIK (converted to its integer form) and
    ``accession`` is the dashed accession number.
    """
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}/{accession}-index.htm"
    )


def _status(version: int, restatement_kind: str, known: bool) -> str:
    """Return the display status of one version.

    Only a ``revision`` is called a restatement; a ``split_adjustment`` gets
    its own status so stock splits are not shown as misleading restatements
    (decision A-25). Versions accepted after the as-of time are ``not yet
    accepted``.
    """
    if not known:
        return "not yet accepted"
    if restatement_kind == "split_adjustment":
        return "split adjustment"
    if restatement_kind == "revision":
        return "restatement"
    if version == 1:
        return "version 1"
    return "later version"


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime | None:
    """Parse one UTC timestamp from a build document; ``None`` when unrecognised.

    Accepts the raw-cache ``fetched_at`` form (``YYYYMMDDTHHMMSSffffffZ``) and
    ISO-8601 with ``Z`` or an offset. Naive ISO-8601 timestamps are rejected,
    because a build document has no local timezone to apply to them.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value) == 26 and value.endswith("Z"):
            return datetime.strptime(value, _FETCH_STAMP_FORMAT).replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def parse_release_info(document: Mapping[str, Any]) -> ReleaseInfo:
    """Extract provenance from a release ``manifest.json`` or ``build-info.json``.

    ``document`` is the parsed JSON object: ``sec_fetch_range`` is an object
    with ``min`` and ``max`` SEC fetch stamps, and ``build_timestamp`` is the
    export time. Missing or malformed fields yield ``None`` for that field
    rather than an error, because the page reads the database without them.
    """
    fetch_range: tuple[datetime, datetime] | None = None
    raw_range = document.get("sec_fetch_range")
    if isinstance(raw_range, Mapping):
        oldest = _parse_utc(raw_range.get("min"))
        newest = _parse_utc(raw_range.get("max"))
        if oldest is not None and newest is not None:
            fetch_range = (oldest, newest)
    return ReleaseInfo(
        fetch_range=fetch_range,
        build_timestamp=_parse_utc(document.get("build_timestamp")),
    )


def local_release_info(db_path: Path) -> ReleaseInfo:
    """Read build provenance from ``build-info.json`` or ``manifest.json`` beside a database.

    Returns the first readable document's :class:`ReleaseInfo`, and an empty
    one when neither file exists or the document is not a JSON object. A local
    database stays readable without any metadata.
    """
    for name in ("build-info.json", "manifest.json"):
        candidate = db_path.parent / name
        if not candidate.is_file():
            continue
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ReleaseInfo()
        if not isinstance(document, Mapping):
            return ReleaseInfo()
        return parse_release_info(document)
    return ReleaseInfo()


def manifest_url(db_url: str) -> str:
    """Return the URL of the ``manifest.json`` published beside a release database.

    Only the final path segment changes, so a signed query string on
    ``db_url`` is preserved.
    """
    parts = urlsplit(db_url)
    directory = parts.path.rsplit("/", 1)[0]
    return urlunsplit(
        (parts.scheme, parts.netloc, f"{directory}/manifest.json", parts.query, parts.fragment)
    )


def fetch_release_info(db_url: str) -> ReleaseInfo:
    """Read the release ``manifest.json`` beside ``db_url``.

    Returns an empty :class:`ReleaseInfo` when the manifest is absent,
    unreachable, larger than 5 MB or not a JSON object: provenance is optional
    and must never stop the explorer from reading the database. HTTP failures
    are not raised.
    """
    try:
        with httpx.stream(
            "GET",
            manifest_url(db_url),
            follow_redirects=True,
            timeout=METADATA_TIMEOUT_SECONDS,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                body.extend(chunk)
                if len(body) > MAX_METADATA_BYTES:
                    return ReleaseInfo()
    except httpx.HTTPError:
        return ReleaseInfo()
    try:
        document = json.loads(body)
    except json.JSONDecodeError:
        return ReleaseInfo()
    if not isinstance(document, Mapping):
        return ReleaseInfo()
    return parse_release_info(document)


def cache_path(url: str, sha256: str | None) -> Path:
    """Return the cache file for one release URL and optional expected digest.

    The directory name hashes ``url`` and the digest, so a rerun in the same
    container reuses the file and a different release or digest never reads a
    stale database. The path is under the system temp directory.
    """
    key = hashlib.sha256(f"{url}\n{sha256 or ''}".encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / CACHE_DIRNAME / key / "edgarpit.duckdb"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def download_database(url: str, *, sha256: str | None = None, target: Path | None = None) -> Path:
    """Stream the released database from ``url`` to ``target`` and return the path.

    ``target`` defaults to :func:`cache_path` for ``url`` and ``sha256``. The
    body is written to a ``.part`` file in 1 MiB chunks and renamed only after
    it is complete; at most 500 MB is accepted, checked against
    ``Content-Length`` and again while streaming. When ``sha256`` is given the
    downloaded digest must match it (case-insensitive) or the partial file is
    removed and :class:`ReleaseDownloadError` is raised. A cached file whose
    digest matches is reused without a request. Network and HTTP failures
    propagate as :class:`httpx.HTTPError`; a body that is too large, or fails
    its digest, raises :class:`ReleaseDownloadError`.
    """
    destination = target if target is not None else cache_path(url, sha256)
    if destination.is_file() and (sha256 is None or _file_sha256(destination) == sha256.lower()):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.part")
    digest = hashlib.sha256()
    complete = False
    try:
        with httpx.stream(
            "GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_bytes = int(declared)
                except ValueError:
                    declared_bytes = 0
                if declared_bytes > MAX_DATABASE_BYTES:
                    raise ReleaseDownloadError(
                        f"the database at {url} is larger than the 500 MB limit"
                    )
            written = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                    written += len(chunk)
                    if written > MAX_DATABASE_BYTES:
                        raise ReleaseDownloadError(
                            f"the database at {url} is larger than the 500 MB limit"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
        if sha256 is not None and digest.hexdigest() != sha256.lower():
            raise ReleaseDownloadError(
                "the downloaded database does not match EDGARPIT_DB_SHA256 "
                f"(expected {sha256.lower()}, got {digest.hexdigest()})"
            )
        partial.replace(destination)
        complete = True
    finally:
        if not complete:
            partial.unlink(missing_ok=True)
    return destination


def check_database(path: Path) -> None:
    """Open ``path`` read-only and close it, proving DuckDB can read it.

    Raises :class:`ReleaseDownloadError` when the file is not a database (for
    example an HTML error page saved by a proxy, or a truncated download).
    """
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error as exc:
        raise ReleaseDownloadError(f"{path.name} is not a readable DuckDB database: {exc}") from exc
    con.close()


@st.cache_resource(show_spinner="Downloading the released database…")
def cached_database(url: str, sha256: str | None) -> DatabaseSource:
    """Download, verify and cache the released database for this process.

    Streamlit keeps the returned :class:`DatabaseSource` across reruns and
    sessions, so the transfer and the read-only check happen once per
    container. A file that DuckDB cannot read is deleted so a later run can
    retry. Raises :class:`ReleaseDownloadError` or :class:`httpx.HTTPError`.
    """
    path = download_database(url, sha256=sha256)
    try:
        check_database(path)
    except ReleaseDownloadError:
        path.unlink(missing_ok=True)
        raise
    return DatabaseSource(path=path, info=fetch_release_info(url), downloaded=True)


def resolve_database() -> tuple[DatabaseSource | None, str | None]:
    """Return the database to read, or ``(None, message)`` when none is available.

    Local mode wins: ``EDGARPIT_DB`` (else the settings default) when that file
    exists, with provenance from ``build-info.json`` or ``manifest.json``
    beside it. Otherwise ``EDGARPIT_DB_URL`` selects the released database,
    which is downloaded and checksum-verified when ``EDGARPIT_DB_SHA256`` is
    set. The returned message is ready to show on the page.
    """
    configured = os.environ.get("EDGARPIT_DB")
    db_path = Path(configured) if configured else Settings().edgarpit_db
    if db_path.is_file():
        return (
            DatabaseSource(path=db_path, info=local_release_info(db_path), downloaded=False),
            None,
        )
    url = os.environ.get("EDGARPIT_DB_URL", "").strip()
    if not url:
        return None, (
            f"No database at {db_path}. Set EDGARPIT_DB to a built database, set "
            "EDGARPIT_DB_URL to the released edgarpit.duckdb asset, or create one with "
            "`uv run python -m edgarpit.devdb --out <dir>` and the dbt models."
        )
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None, f"EDGARPIT_DB_URL must be an http(s) URL; got {url!r}."
    sha256 = os.environ.get("EDGARPIT_DB_SHA256", "").strip() or None
    if sha256 is not None and not _SHA256_RE.fullmatch(sha256):
        return None, (
            f"EDGARPIT_DB_SHA256 must be a 64-character hexadecimal SHA-256 digest; got {sha256!r}."
        )
    try:
        source = cached_database(url, sha256)
    except httpx.HTTPError as exc:
        return None, f"Could not download the database from {url}: {exc}"
    except ReleaseDownloadError as exc:
        return None, str(exc)
    return source, None


def _utc_label(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def provenance_caption(info: ReleaseInfo) -> str:
    """Return the provenance line shown under the page title.

    ``info`` comes from a release manifest or a build-info document; the text
    gives the SEC fetch range in UTC, adds the release time when known, and
    says the range is unavailable when the build carried no metadata.
    """
    if info.fetch_range is None:
        return "SEC fetch range: not available for this build."
    oldest, newest = info.fetch_range
    text = f"SEC fetch range: {_utc_label(oldest)} to {_utc_label(newest)}"
    if info.build_timestamp is not None:
        text += f" · released {_utc_label(info.build_timestamp)}"
    return text


def versions_frame(rows: list[query.AsOfResult], asof: datetime) -> pl.DataFrame:
    """Build the versions table for ``rows`` as a Polars frame.

    ``asof`` is the UTC instant of the "as of" control; each row gets a
    ``known_at_asof`` flag (``acceptance_ts <= asof``) and a ``status`` used by
    the chart and the table. Only ``revision`` versions are marked as
    restatements; ``split_adjustment`` versions carry their own status (decision
    A-25). One row per filing version, in query order.
    """
    return pl.DataFrame(
        {
            "period_start": [row.period_start for row in rows],
            "period_end": [row.period_end for row in rows],
            "version": [row.version for row in rows],
            "value": [row.value for row in rows],
            "unit": [row.unit for row in rows],
            "accession": [row.accession for row in rows],
            "form": [row.form for row in rows],
            "accepted": [_iso_z(row.acceptance_ts) for row in rows],
            "known_at_asof": [row.acceptance_ts <= asof for row in rows],
            "is_restatement": [row.is_restatement for row in rows],
            "restatement_kind": [row.restatement_kind for row in rows],
            "status": [
                _status(row.version, row.restatement_kind, row.acceptance_ts <= asof)
                for row in rows
            ],
        }
    )


def events_frame(rows: list[query.EventResult]) -> pl.DataFrame:
    """Build the 8-K events table for ``rows`` as a Polars frame.

    ``filing`` holds the EDGAR index URL; the table shows ``accepted`` (UTC,
    ``Z`` suffix), ``item_code``, ``item_label`` and ``form``.
    """
    return pl.DataFrame(
        {
            "accepted": [_iso_z(row.acceptance_ts) for row in rows],
            "item_code": [row.item_code for row in rows],
            "item_label": [row.item_label for row in rows],
            "form": [row.form for row in rows],
            "filing": [filing_url(row.cik, row.accession) for row in rows],
        }
    )


def timeline_chart(frame: pl.DataFrame) -> alt.Chart:
    """Return the Altair timeline: one point per version at its ``period_end``.

    Colour and shape encode ``status`` (version 1, restatement, split
    adjustment, later version, not yet accepted), so revisions, stock splits
    and versions not yet accepted at the "as of" time are visually distinct
    with a legend; the tooltip carries the value, unit, accession and
    acceptance time.
    """
    units = frame["unit"].unique().to_list()
    y_title = f"value ({units[0]})" if len(units) == 1 else "value"
    chart: alt.Chart = (
        alt.Chart(frame)
        .mark_point(filled=True, size=120)
        .encode(
            x=alt.X("period_end:T", title="period end"),
            y=alt.Y("value:Q", title=y_title),
            color=alt.Color(
                "status:N",
                title="version status",
                scale=alt.Scale(domain=STATUS_ORDER, range=STATUS_COLORS),
            ),
            shape=alt.Shape(
                "status:N",
                title="version status",
                scale=alt.Scale(domain=STATUS_ORDER, range=STATUS_SHAPES),
            ),
            tooltip=[
                alt.Tooltip("period_end:T", title="period end"),
                alt.Tooltip("value:Q", title="value", format=",.2f"),
                alt.Tooltip("unit:N", title="unit"),
                alt.Tooltip("version:Q", title="version"),
                alt.Tooltip("accession:N", title="accession"),
                alt.Tooltip("accepted:N", title="accepted (UTC)"),
                alt.Tooltip("known_at_asof:N", title="known at as-of"),
            ],
        )
    )
    return chart


def main() -> None:
    """Render the explorer page for the configured or released database."""
    st.set_page_config(page_title="edgar-pit explorer", layout="wide")
    st.title("edgar-pit explorer")
    st.caption(
        "Point-in-time SEC fundamentals and corporate events. A value is known at time t "
        "if and only if the filing that carried it was accepted at or before t."
    )

    source, error = resolve_database()
    if source is None:
        st.error(error or "No database is available.")
        return
    st.caption(provenance_caption(source.info))
    if source.downloaded:
        st.caption(f"Reading the released database at {source.path}.")
    db_path = source.path

    try:
        issuers = query.issuers(db=db_path)
    except duckdb.Error as exc:
        st.error(f"Could not read {db_path}: {exc}")
        return
    if not issuers:
        st.warning("The database has no issuers with fundamentals or events yet.")
        return

    labels = {f"{ticker or 'no ticker'} ({cik})": cik for cik, ticker in issuers}
    issuer = st.sidebar.selectbox("Issuer (ticker and CIK)", options=list(labels), key="issuer")
    cik = labels[issuer]

    available_fields = query.fields(cik, db=db_path)
    field = (
        st.sidebar.selectbox("Field", options=available_fields, key="field")
        if available_fields
        else None
    )
    period_choice = st.sidebar.radio("Period", options=["FY", "Q"], horizontal=True, key="period")
    quarterly = period_choice == "Q"
    period: Literal["FY", "Q"] = "Q" if quarterly else "FY"
    asof_date = st.sidebar.date_input("As of (UTC midnight)", value=date.today(), key="asof")
    asof = datetime(asof_date.year, asof_date.month, asof_date.day, tzinfo=UTC)
    st.sidebar.caption("Versions accepted after this instant are greyed out and marked not known.")

    st.subheader(f"{issuer} — {field if field is not None else 'no fields'}")
    if field is None:
        st.info("This issuer has no fundamental values in the database.")
    else:
        rows = query.history(cik, field, period=period, db=db_path)
        if not rows:
            st.info(f"No {period} versions of {field} for this issuer.")
        else:
            frame = versions_frame(rows, asof)
            known = int(frame["known_at_asof"].sum())
            chart_column, table_column = st.columns([3, 2])
            with chart_column:
                st.altair_chart(timeline_chart(frame), width="stretch")
            with table_column:
                st.metric("versions known at the as-of", f"{known} of {len(frame)}")
                st.dataframe(frame[VERSION_COLUMNS], width="stretch", hide_index=True)

    st.subheader("8-K events")
    event_rows = query.events(cik, db=db_path)
    if not event_rows:
        st.info("This issuer has no 8-K events in the database.")
    else:
        st.dataframe(
            events_frame(event_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "filing": st.column_config.LinkColumn(
                    "filing", display_text=r"(\d{10}-\d{2}-\d{6})-index\.htm$"
                )
            },
        )


if __name__ == "__main__":
    main()
