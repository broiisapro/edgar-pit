"""Companyfacts JSON to the ``facts_raw`` DataFrame (decision A-16).

:func:`parse_companyfacts` flattens one companyfacts document into the long
``raw.facts_raw`` table: one row per entry found under
``facts -> taxonomy -> concept -> units -> [entries]`` for the ``us-gaap``,
``ifrs-full`` and ``dei`` taxonomies.  Other taxonomies (``ffd``, ``invest``,
``srt``, ...) are skipped and counted in a debug log line.  The frame carries
no timestamps: ``period_start``/``period_end``/``filed`` are dates, as the
companyfacts document states them, and ``acceptance_ts`` is attached later by
joining ``raw.filings`` on ``accession`` (section 6 of
``docs/architecture.md``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import polars as pl

from edgarpit.rawcache import RawCache

logger = logging.getLogger(__name__)

KEPT_TAXONOMIES: tuple[str, ...] = ("us-gaap", "ifrs-full", "dei")

_COLUMN_TYPES: dict[str, Any] = {
    "cik": pl.String,
    "taxonomy": pl.String,
    "concept": pl.String,
    "unit": pl.String,
    "value": pl.Float64,
    "period_start": pl.String,
    "period_end": pl.String,
    "fy": pl.Int64,
    "fp": pl.String,
    "form": pl.String,
    "accession": pl.String,
    "filed": pl.String,
    "frame": pl.String,
}

_DATE_COLUMNS = ("period_start", "period_end", "filed")
_DATE_FORMAT = "%Y-%m-%d"


def _cik10(value: Any) -> str:
    """Zero-pad the document's CIK to ten digits, or raise ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"companyfacts document has no numeric 'cik': {value!r}")
    digits = str(value).strip()
    if not digits.isdigit():
        raise ValueError(f"companyfacts document has a non-numeric 'cik': {value!r}")
    if len(digits) > 10:
        raise ValueError(f"companyfacts document has a 'cik' longer than ten digits: {value!r}")
    return digits.zfill(10)


def _as_text(value: Any) -> str | None:
    """Return a string value, or ``None`` for anything else (nullable column)."""
    return value if isinstance(value, str) else None


def _as_fy(value: Any) -> int | None:
    """Return an integral fiscal year, or ``None`` when it is absent or invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _count_entries(concepts: Any) -> int:
    """Count fact entries under a taxonomy block, for the skipped-taxonomy log."""
    total = 0
    if not isinstance(concepts, Mapping):
        return 0
    for entry in concepts.values():
        units = entry.get("units") if isinstance(entry, Mapping) else None
        if not isinstance(units, Mapping):
            continue
        for rows in units.values():
            if isinstance(rows, list):
                total += len(rows)
    return total


def _valid_entry(row: Mapping[str, Any]) -> bool:
    """True when a fact entry has every field the staging contract requires."""
    value = row.get("val")
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and isinstance(row.get("end"), str)
        and isinstance(row.get("accn"), str)
        and isinstance(row.get("form"), str)
        and isinstance(row.get("filed"), str)
    )


def parse_companyfacts(doc: Mapping[str, Any]) -> pl.DataFrame:
    """Flatten a companyfacts document into the long ``facts_raw`` table.

    ``doc`` is a parsed ``data.sec.gov/api/xbrl/companyfacts`` JSON object.
    Returns one row per entry in ``facts -> taxonomy -> concept -> units ->
    [entries]`` for the :data:`KEPT_TAXONOMIES`; ``cik`` is zero-padded to ten
    digits; ``unit`` is exactly the ``units`` key; ``value`` comes from
    ``val``; ``period_start`` comes from ``start`` and is null for instants;
    ``period_end`` comes from ``end``; ``fy``, ``fp``, ``form``, ``accession``
    (from ``accn``), ``filed`` and ``frame`` (null when absent) are copied
    unchanged.  Rows that are equal in every column are kept once; entries
    with the same key and a different value are different rows (versions or
    conflicts, decision A-7).  Input order is preserved.  Dates are calendar
    dates exactly as SEC states them; the frame carries no times or
    timezones.  Raises ``ValueError`` when ``doc`` has no ``facts`` object or
    no numeric ``cik``.
    """
    facts = doc.get("facts")
    if not isinstance(facts, Mapping):
        raise ValueError("companyfacts document has no 'facts' object")
    cik = _cik10(doc.get("cik"))
    columns: dict[str, list[Any]] = {name: [] for name in _COLUMN_TYPES}
    malformed = 0
    for taxonomy, concepts in facts.items():
        if taxonomy not in KEPT_TAXONOMIES:
            logger.debug("skipping taxonomy %s with %d entries", taxonomy, _count_entries(concepts))
            continue
        if not isinstance(concepts, Mapping):
            continue
        for concept, entry in concepts.items():
            units = entry.get("units") if isinstance(entry, Mapping) else None
            if not isinstance(units, Mapping):
                continue
            for unit, rows in units.items():
                if not isinstance(unit, str) or not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, Mapping) or not _valid_entry(row):
                        malformed += 1
                        continue
                    columns["cik"].append(cik)
                    columns["taxonomy"].append(taxonomy)
                    columns["concept"].append(concept)
                    columns["unit"].append(unit)
                    columns["value"].append(float(row["val"]))
                    columns["period_start"].append(_as_text(row.get("start")))
                    columns["period_end"].append(row["end"])
                    columns["fy"].append(_as_fy(row.get("fy")))
                    columns["fp"].append(_as_text(row.get("fp")))
                    columns["form"].append(row["form"])
                    columns["accession"].append(row["accn"])
                    columns["filed"].append(row["filed"])
                    columns["frame"].append(_as_text(row.get("frame")))
    if malformed:
        logger.debug("skipped %d malformed companyfacts entries", malformed)
    frame = pl.DataFrame(columns, schema=_COLUMN_TYPES)
    frame = frame.with_columns(
        pl.col(column).str.to_date(format=_DATE_FORMAT) for column in _DATE_COLUMNS
    )
    return frame.unique(maintain_order=True).select(list(_COLUMN_TYPES))


def ingest_facts(raw: RawCache, cik: str) -> pl.DataFrame:
    """Flatten the latest cached companyfacts document for an issuer.

    ``raw`` is the :class:`edgarpit.rawcache.RawCache` written by the fetcher;
    ``cik`` is any digit string and is zero-padded to ten digits.  Reads the
    newest body under ``raw/companyfacts/<cik10>/`` and returns what
    :func:`parse_companyfacts` returns for it.  Raises ``ValueError`` when
    ``cik`` is not numeric or the body is not a JSON object, and
    ``FileNotFoundError`` when no companyfacts document is cached for the
    issuer.
    """
    digits = cik.strip()
    if not digits.isdigit():
        raise ValueError(f"CIK must be digits, got {cik!r}")
    cik10 = digits.zfill(10)
    body = raw.latest("companyfacts", cik10)
    if body is None:
        raise FileNotFoundError(f"no companyfacts document cached for CIK {cik10} under {raw.root}")
    document = json.loads(body)
    if not isinstance(document, Mapping):
        raise ValueError(f"cached companyfacts for CIK {cik10} is not a JSON object")
    return parse_companyfacts(document)
