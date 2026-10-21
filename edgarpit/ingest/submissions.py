"""Submissions JSON to ``filings`` and ``events_8k`` DataFrames (decision A-16).

The submissions API returns one document per issuer plus paginated
``filings.files[]`` pages that share the document's columnar layout. This
module flattens those parallel arrays into the row-per-filing ``filings``
frame. ``acceptance_ts_api`` keeps the API's ``acceptanceDateTime`` parsed
literally (the trailing ``Z`` is taken at face value); the authoritative
``acceptance_ts`` is attached later from each filing's SGML header by
:func:`edgarpit.ingest.headers.apply_acceptance` (decision A-1).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from edgarpit.ingest.headers import (
    HeaderInfo,
    HeaderWithoutAcceptance,
    MissingAcceptanceTime,
    apply_acceptance,
    apply_acceptance_floors,
    parse_header,
)
from edgarpit.rawcache import RawCache
from edgarpit.schemas import Events8kSchema

logger = logging.getLogger(__name__)

DEFAULT_ITEM_LABELS_PATH = Path(__file__).resolve().parents[2] / "config" / "8k_items.yaml"

_CIK_RE = r"^\d{10}$"
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_ITEM_CODE_RE = re.compile(r"\d+(?:\.\d{2})?")
_EVENT_FORMS = frozenset({"8-K", "8-K/A"})

_EVENTS_DTYPES: dict[str, pl.DataType | type[pl.DataType]] = {
    "cik": pl.String,
    "accession": pl.String,
    "form": pl.String,
    "acceptance_ts": pl.Datetime("us", "UTC"),
    "ts_imputed": pl.Boolean,
    "item_code": pl.String,
    "item_label": pl.String,
}

_FILINGS_DTYPES: dict[str, pl.DataType | type[pl.DataType]] = {
    "cik": pl.String,
    "accession": pl.String,
    "form": pl.String,
    "filing_date": pl.Date,
    "report_date": pl.Date,
    "acceptance_ts": pl.Datetime("us", "UTC"),
    "acceptance_ts_source": pl.String,
    "acceptance_ts_api": pl.Datetime("us", "UTC"),
    "ts_imputed": pl.Boolean,
    "primary_document": pl.String,
    "items": pl.String,
    "is_amendment": pl.Boolean,
    "is_xbrl": pl.Boolean,
    "source": pl.String,
}


def _arrays_of(document: Mapping[str, Any], source: str) -> Mapping[str, Any]:
    """Return the parallel arrays of ``filings.recent`` or of a bare page."""
    filings = document.get("filings")
    arrays: Any = (
        filings["recent"] if isinstance(filings, Mapping) and "recent" in filings else document
    )
    if not isinstance(arrays, Mapping):
        raise ValueError(f"submissions source {source!r} has no filing arrays")
    return arrays


def _optional_str(value: Any) -> str | None:
    """Return a non-empty string or ``None`` (the API uses ``""`` for absent)."""
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _parse_date(value: Any, *, field: str, accession: str, source: str) -> date:
    """Parse an API ``YYYY-MM-DD`` value or raise ``ValueError`` with context."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{source}: filing {accession} has no {field}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{source}: filing {accession} has malformed {field} {value!r}") from exc


def _parse_optional_date(value: Any, *, field: str, accession: str, source: str) -> date | None:
    """Parse an optional API date (``""`` and missing mean null)."""
    text = _optional_str(value)
    if text is None:
        return None
    return _parse_date(text, field=field, accession=accession, source=source)


def _parse_acceptance_api(value: Any, *, accession: str, source: str) -> datetime | None:
    """Parse ``acceptanceDateTime`` literally as an aware UTC datetime.

    The API writes e.g. ``2026-07-31T00:30:28.000Z``; the instant the string
    claims is kept exactly as given (decision A-1) even when it disagrees with
    the filing's SGML header.
    """
    text = _optional_str(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise ValueError(
            f"{source}: filing {accession} has malformed acceptanceDateTime {text!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"{source}: filing {accession} acceptanceDateTime {text!r} has no timezone"
        )
    return parsed.astimezone(UTC)


def _rows_of(arrays: Mapping[str, Any], cik: str, source: str) -> list[dict[str, Any]]:
    """Flatten one document's parallel arrays into filings row dicts."""
    accessions = arrays.get("accessionNumber")
    if not isinstance(accessions, list):
        raise ValueError(f"submissions source {source!r} has no accessionNumber array")
    lengths = {len(value) for value in arrays.values() if isinstance(value, list)}
    if len(lengths) > 1:
        raise ValueError(f"submissions source {source!r} has arrays of unequal length {lengths}")

    def at(key: str, index: int) -> Any:
        values = arrays.get(key)
        if isinstance(values, list):
            return values[index]
        return None

    rows: list[dict[str, Any]] = []
    for index, accession_value in enumerate(accessions):
        accession = str(accession_value)
        if not _ACCESSION_RE.fullmatch(accession):
            raise ValueError(f"{source}: malformed accession {accession!r}")
        form = _optional_str(at("form", index))
        if form is None:
            raise ValueError(f"{source}: filing {accession} has no form")
        rows.append(
            {
                "cik": cik,
                "accession": accession,
                "form": form,
                "filing_date": _parse_date(
                    at("filingDate", index), field="filingDate", accession=accession, source=source
                ),
                "report_date": _parse_optional_date(
                    at("reportDate", index), field="reportDate", accession=accession, source=source
                ),
                "acceptance_ts": None,
                "acceptance_ts_source": None,
                "acceptance_ts_api": _parse_acceptance_api(
                    at("acceptanceDateTime", index), accession=accession, source=source
                ),
                "ts_imputed": None,
                "primary_document": _optional_str(at("primaryDocument", index)),
                "items": _optional_str(at("items", index)),
                "is_amendment": form.endswith("/A"),
                "is_xbrl": bool(at("isXBRL", index)),
                "source": source,
            }
        )
    return rows


def parse_submissions(main: Mapping[str, Any], pages: Sequence[Mapping[str, Any]]) -> pl.DataFrame:
    """Flatten a submissions document and its paginated ``filings.files`` pages.

    ``main`` is the payload of ``CIK##########.json`` and ``pages`` are the
    payloads of its ``filings.files[]`` pages in the order listed. Returns one
    row per accession with the columns of
    :class:`edgarpit.schemas.FilingsSchema`; when an accession appears in more
    than one document the ``recent`` row wins, otherwise the first page wins.
    ``source`` is ``recent`` or the page's file name (e.g.
    ``CIK0001000275-submissions-001.json``). ``cik`` is zero-padded to ten
    digits from the document; dates are parsed (``""`` means null for
    ``report_date``, ``items`` and ``primary_document``); ``acceptance_ts_api``
    is parsed exactly as given; ``is_amendment`` is ``form.endswith('/A')`` and
    ``is_xbrl`` is the document's ``isXBRL`` flag. ``acceptance_ts``,
    ``acceptance_ts_source`` and ``ts_imputed`` stay null because they are
    filled from the filing's SGML header by
    :func:`edgarpit.ingest.headers.apply_acceptance` (decision A-1).

    Raises ``ValueError`` when the document has no ``cik``, when
    ``filings.files[]`` does not match ``pages`` one-to-one, when a document's
    original arrays have unequal lengths, or when a required field
    (``accessionNumber``, ``form``, ``filingDate``) is missing or malformed.
    """
    cik_value = main.get("cik")
    cik = str(cik_value).zfill(10) if cik_value not in (None, "") else ""
    if not re.fullmatch(_CIK_RE, cik):
        raise ValueError(f"submissions document has no valid cik: {cik_value!r}")

    filings_block = main.get("filings")
    file_entries = filings_block.get("files", []) if isinstance(filings_block, Mapping) else []
    if not isinstance(file_entries, list):
        raise ValueError(f"submissions document for {cik} has a non-list filings.files")
    names = [str(entry["name"]) for entry in file_entries]
    if len(names) != len(pages):
        raise ValueError(
            f"submissions document for {cik} lists {len(names)} page files "
            f"but {len(pages)} pages were given"
        )

    rows: dict[str, dict[str, Any]] = {}
    for row in _rows_of(_arrays_of(main, "recent"), cik, "recent"):
        rows.setdefault(row["accession"], row)
    for name, page in zip(names, pages, strict=True):
        for row in _rows_of(_arrays_of(page, name), cik, name):
            rows.setdefault(row["accession"], row)
    columns = {name: [row[name] for row in rows.values()] for name in _FILINGS_DTYPES}
    return pl.DataFrame(columns, schema=_FILINGS_DTYPES)


def load_item_labels(path: Path | None = None) -> dict[str, str]:
    """Load the Form 8-K item-code labels from ``path``.

    The YAML file has an ``items`` mapping of current item code (``"2.02"``) to
    SEC item title and an optional ``legacy`` mapping of pre-August-2004
    single-number codes. Returns the merged ``{item_code: label}`` dict; the
    default path is ``config/8k_items.yaml``. Raises ``ValueError`` when the
    file cannot be read, has no ``items`` mapping, defines a code twice or
    outside the code format, or gives a code an empty label.
    """
    source = DEFAULT_ITEM_LABELS_PATH if path is None else Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read item labels {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in item labels {source}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), dict):
        raise ValueError(f"item labels {source} must contain an 'items' mapping")
    labels: dict[str, str] = {}
    for section in ("items", "legacy"):
        entries = raw.get(section, {})
        if not isinstance(entries, dict):
            raise ValueError(f"item labels {source}: section {section!r} must be a mapping")
        for code, label in entries.items():
            key = str(code)
            if not _ITEM_CODE_RE.fullmatch(key):
                raise ValueError(f"item labels {source}: code {key!r} is not an item code")
            if key in labels:
                raise ValueError(f"item labels {source}: code {key!r} is defined more than once")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"item labels {source}: code {key!r} has no label")
            labels[key] = label.strip()
    return labels


def explode_8k_items(filings: pl.DataFrame, item_labels: Mapping[str, str]) -> pl.DataFrame:
    """Explode every 8-K/8-K/A filing's ``items`` into one row per item code.

    ``filings`` is a frame with ``cik``, ``accession``, ``form``,
    ``acceptance_ts`` (aware UTC), ``ts_imputed`` and the raw comma-separated
    ``items`` column; ``item_labels`` maps an item code (``"2.02"``) to its
    label. Each 8-K/8-K/A row yields one row per distinct, stripped item code,
    carrying the filing's ``acceptance_ts`` and ``ts_imputed``; codes missing
    from the mapping get the label ``"unknown"``. Filings whose ``items`` is
    null, empty or only separators produce no rows; the count of such filings
    is logged. Returns a frame valid under
    :class:`edgarpit.schemas.Events8kSchema` (unique on
    ``(accession, item_code)``).
    """
    eight_ks = filings.filter(pl.col("form").is_in(sorted(_EVENT_FORMS)))
    events = (
        eight_ks.select("cik", "accession", "form", "acceptance_ts", "ts_imputed", "items")
        .with_columns(pl.col("items").str.split(","))
        .explode("items", empty_as_null=True)
        .with_columns(pl.col("items").str.strip_chars().alias("item_code"))
        .filter(pl.col("item_code").is_not_null() & (pl.col("item_code") != ""))
        .unique(subset=["accession", "item_code"], keep="first", maintain_order=True)
        .with_columns(
            pl.col("item_code").replace_strict(item_labels, default="unknown").alias("item_label")
        )
        .select(*_EVENTS_DTYPES)
    )
    filers = events["accession"].n_unique()
    logger.info(
        "exploded %d 8-K item rows from %d filings; %d filings carry no items",
        events.height,
        filers,
        eight_ks.height - filers,
    )
    Events8kSchema.validate(events)
    return events


def _header_only_filings(
    cik: str,
    headers: Mapping[str, HeaderInfo],
    missing: Mapping[str, HeaderWithoutAcceptance],
    known: set[str],
) -> pl.DataFrame:
    """Build filings rows from cached headers whose accession has no API row.

    ``cik`` is the issuer's ten-character CIK, ``headers`` maps an accession
    with a usable SGML header to its parsed :class:`HeaderInfo`, ``missing``
    maps an accession whose header carries no acceptance time to its
    :class:`HeaderWithoutAcceptance` and ``known`` the accessions already
    carried by the submissions frame. Returns one ``FilingsSchema`` row per
    header accession outside ``known``, ordered by accession, with ``form``,
    ``filing_date``, ``report_date`` from ``<PERIOD>`` (null when absent),
    ``items`` from ``<ITEMS>``, ``is_amendment`` from the form,
    ``is_xbrl=false``, ``primary_document`` null and ``source='header'``
    (decision A-24). The acceptance columns stay null: the caller fills them
    with :func:`edgarpit.ingest.headers.apply_acceptance`, so a usable header
    supplies ``acceptance_ts_source='sgml_header'`` while a header without an
    acceptance time takes the A-23 fallback (decision A-26).
    """
    rows: list[dict[str, object]] = []
    for accession, info in sorted(headers.items()):
        if accession in known:
            continue
        rows.append(
            {
                "cik": cik,
                "accession": accession,
                "form": info.form,
                "filing_date": info.filing_date,
                "report_date": info.period,
                "acceptance_ts": None,
                "acceptance_ts_source": None,
                "acceptance_ts_api": None,
                "ts_imputed": None,
                "primary_document": None,
                "items": info.items,
                "is_amendment": info.form.endswith("/A"),
                "is_xbrl": False,
                "source": "header",
            }
        )
    for accession, missing_info in sorted(missing.items()):
        if accession in known:
            continue
        rows.append(
            {
                "cik": cik,
                "accession": accession,
                "form": missing_info.form,
                "filing_date": missing_info.filing_date,
                "report_date": missing_info.period,
                "acceptance_ts": None,
                "acceptance_ts_source": None,
                "acceptance_ts_api": None,
                "ts_imputed": None,
                "primary_document": None,
                "items": missing_info.items,
                "is_amendment": missing_info.form.endswith("/A"),
                "is_xbrl": False,
                "source": "header",
            }
        )
    return pl.DataFrame(rows, schema=_FILINGS_DTYPES)


def _cached_headers(
    raw: RawCache, cik: str
) -> tuple[dict[str, HeaderInfo], dict[str, HeaderWithoutAcceptance]]:
    """Parse every cached header for one issuer, separating unusable ones.

    Returns ``(headers, missing)`` keyed by accession: ``headers`` holds the
    headers that yield an acceptance time, ``missing`` the pre-2001 headers
    that carry none (decision A-26). Any other ``ValueError`` from
    :func:`edgarpit.ingest.headers.parse_header` still propagates: a corrupt
    cache fails the build rather than being quietly timed by the fallback.
    """
    headers: dict[str, HeaderInfo] = {}
    missing: dict[str, HeaderWithoutAcceptance] = {}
    for accession in raw.keys("headers", cik):
        body = raw.latest("headers", cik, accession)
        if body is None:  # pragma: no cover - keys() lists existing directories only
            continue
        try:
            headers[accession] = parse_header(body.decode("utf-8"))
        except MissingAcceptanceTime as error:
            missing[accession] = error.header
    return headers, missing


def _fill_header_items(
    filings: pl.DataFrame, missing: Mapping[str, HeaderWithoutAcceptance]
) -> pl.DataFrame:
    """Fill a filing's null ``items`` from a header without an acceptance time.

    ``filings`` is a ``FilingsSchema`` frame already carrying acceptance
    times; ``missing`` maps an accession to its header. Only rows whose
    submissions record has no ``items`` are changed, so the API value wins
    whenever it exists; the header's codes are still used for the 8-K event
    explosion (decision A-26).
    """
    items = {accession: header.items for accession, header in missing.items() if header.items}
    if not items:
        return filings
    return filings.with_columns(
        pl.when(pl.col("items").is_null())
        .then(pl.col("accession").replace_strict(items, default=None, return_dtype=pl.String))
        .otherwise(pl.col("items"))
        .alias("items")
    )


def ingest_filings(
    raw: RawCache, cik: str, item_labels: Mapping[str, str]
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Ingest one issuer's cached filings and 8-K events from a raw cache.

    ``raw`` is the cache root (a fetched-data directory or the uncompressed
    fixture tree), ``cik`` the issuer's ten-character CIK and ``item_labels``
    the Form 8-K code labels from
    :func:`edgarpit.ingest.submissions.load_item_labels`. Reads the latest
    submissions document, every page file it lists (in listed order) and every
    cached SGML header for the issuer, then attaches the authoritative
    acceptance times from the headers (decision A-1). A header without an
    ``<ACCEPTANCE-DATETIME>`` (pre-2001 filings, decision A-26) is not fatal:
    its filing takes the A-23 fallback, raised to at least the header's
    ``<DATE-OF-FILING-DATE-CHANGE>`` when present, and the header's ``<ITEMS>``
    fill a submissions row that has none; one warning per issuer names the
    count and the first five accessions. A cached header whose accession is
    absent from the submissions document still becomes a filings row built
    from the header, so every fetched header is used (decision A-24); such a
    row and its facts are never imputed from ``filed``. Returns
    ``(filings, events_8k)``, valid under
    :class:`edgarpit.schemas.FilingsSchema` and
    :class:`edgarpit.schemas.Events8kSchema`. Raises ``ValueError`` when the
    issuer has no cached submissions document, when a page listed by the
    document is not in the cache, or when a cached header is malformed in any
    way other than a missing acceptance time.
    """
    main_body = raw.latest("submissions", cik)
    if main_body is None:
        raise ValueError(f"no cached submissions document for CIK {cik}")
    main = json.loads(main_body)
    filings_block = main.get("filings")
    file_entries = filings_block.get("files", []) if isinstance(filings_block, Mapping) else []
    if not isinstance(file_entries, list):
        raise ValueError(f"submissions document for {cik} has a non-list filings.files")
    pages: list[Mapping[str, Any]] = []
    for entry in file_entries:
        name = str(entry["name"])
        body = raw.latest("submissions_page", cik, name.removesuffix(".json"))
        if body is None:
            raise ValueError(f"missing cached submissions page {name} for CIK {cik}")
        pages.append(json.loads(body))

    headers, missing = _cached_headers(raw, cik)
    if missing:
        logger.warning(
            "CIK %s: %d cached headers have no <ACCEPTANCE-DATETIME>; using the "
            "filing-date fallback for them (first five: %s)",
            cik,
            len(missing),
            ", ".join(sorted(missing)[:5]),
        )
    floors = {
        accession: header.date_of_filing_date_change
        for accession, header in missing.items()
        if header.date_of_filing_date_change is not None
    }

    submissions = parse_submissions(main, pages)
    filings = apply_acceptance_floors(apply_acceptance(submissions, headers), floors)
    filings = _fill_header_items(filings, missing)
    known = set(submissions["accession"].to_list())
    header_only = _header_only_filings(cik, headers, missing, known)
    if header_only.height:
        filings = pl.concat(
            [filings, apply_acceptance_floors(apply_acceptance(header_only, headers), floors)]
        )
    events = explode_8k_items(filings, item_labels)
    return filings, events
