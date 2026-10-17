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

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

_CIK_RE = r"^\d{10}$"
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_EVENT_FORMS = frozenset({"8-K", "8-K/A"})

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


def explode_8k_items(filings: pl.DataFrame, item_labels: Mapping[str, str]) -> pl.DataFrame:
    """Explode every 8-K/8-K/A filing's ``items`` into one row per item code.

    ``item_labels`` maps an item code (``"2.02"``) to its label; codes that are
    missing from the mapping get the label ``"unknown"``. Returns a frame valid
    under :class:`edgarpit.schemas.Events8kSchema`.
    """
    raise NotImplementedError
