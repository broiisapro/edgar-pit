"""Point-in-time queries over the built DuckDB database (decisions A-12, A-16).

Every function decides what was known from ``acceptance_ts`` alone: a value is
known at ``asof_ts`` if and only if its filing's ``acceptance_ts <= asof_ts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import duckdb


@dataclass(frozen=True)
class AsOfResult:
    """One ``fundamentals_pit`` row as it was known at a point in time."""

    cik: str
    ticker: str | None
    field: str
    value: float
    unit: str
    currency: str | None
    period_start: date | None
    period_end: date
    period_type: str
    accession: str
    form: str
    acceptance_ts: datetime
    version: int
    is_restatement: bool
    ts_imputed: bool


@dataclass(frozen=True)
class EventResult:
    """One 8-K item with the timestamp of the filing that carried it."""

    cik: str
    ticker: str | None
    accession: str
    form: str
    acceptance_ts: datetime
    ts_imputed: bool
    item_code: str
    item_label: str


def asof(
    identifier: str,
    field: str,
    asof_ts: datetime | str,
    period: Literal["FY", "Q"] = "FY",
    n: int = 1,
    *,
    db: str | Path | duckdb.DuckDBPyConnection | None = None,
) -> list[AsOfResult]:
    """Return the ``n`` most recent periods known at ``asof_ts``.

    Candidates are ``fundamentals_pit`` rows with ``acceptance_ts <= asof_ts``
    and the requested ``period_type``; per period the row with the maximum
    ``(acceptance_ts, version)`` is kept; periods are ordered by ``period_end``
    descending and the first ``n`` are returned. ``identifier`` is a ticker or a
    zero-padded CIK; ``asof_ts`` strings must carry a timezone (``Z`` or an
    offset) and naive timestamps are rejected.
    """
    raise NotImplementedError


def history(
    identifier: str,
    field: str,
    period: Literal["FY", "Q"] | None = None,
    *,
    db: str | Path | duckdb.DuckDBPyConnection | None = None,
) -> list[AsOfResult]:
    """Return every version of ``field`` for ``identifier``.

    Versions are ordered by ``period_end`` and then by ``version``. ``period``
    filters on ``period_type`` when given. ``identifier`` is a ticker or a
    zero-padded CIK.
    """
    raise NotImplementedError


def events(
    identifier: str,
    item: str | None = None,
    start: datetime | str | None = None,
    end: datetime | str | None = None,
    *,
    db: str | Path | duckdb.DuckDBPyConnection | None = None,
) -> list[EventResult]:
    """Return 8-K items for ``identifier`` ordered by ``acceptance_ts``.

    ``item`` filters on the item code (``"2.02"``); ``start`` and ``end`` bound
    ``acceptance_ts`` (inclusive) and, like ``asof_ts``, must carry a timezone
    when given as strings.
    """
    raise NotImplementedError
