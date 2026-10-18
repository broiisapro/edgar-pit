"""Build a DuckDB with ``main.fundamentals_pit`` and ``main.events_8k`` from row lists.

The two tables follow the frozen column contract of ``docs/architecture.md``
section 7 so that query, API and explorer tests never depend on the dbt models.
Callers pass :class:`FundamentalRow` and :class:`EventRow` values (small Python
row lists) to :func:`build_pit_db`, which returns an open connection to an
in-memory database or, when ``path`` is given, to a database file.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb

FUNDAMENTALS_PIT_DDL = """
CREATE TABLE main.fundamentals_pit (
    cik VARCHAR(10),
    ticker VARCHAR,
    field VARCHAR,
    period_start DATE,
    period_end DATE,
    period_type VARCHAR,
    accession VARCHAR,
    form VARCHAR,
    acceptance_ts TIMESTAMPTZ,
    ts_imputed BOOLEAN,
    value DOUBLE,
    unit VARCHAR,
    currency VARCHAR,
    source_taxonomy VARCHAR,
    source_concept VARCHAR,
    concept_rank INTEGER,
    version INTEGER,
    first_value DOUBLE,
    is_restatement BOOLEAN,
    pct_change_vs_first DOUBLE,
    restatement_kind VARCHAR
)
"""

EVENTS_8K_DDL = """
CREATE TABLE main.events_8k (
    cik VARCHAR(10),
    accession VARCHAR,
    form VARCHAR,
    acceptance_ts TIMESTAMPTZ,
    ts_imputed BOOLEAN,
    item_code VARCHAR,
    item_label VARCHAR,
    ticker VARCHAR
)
"""

_FUNDAMENTAL_INSERT = "INSERT INTO main.fundamentals_pit VALUES (" + ", ".join(["?"] * 21) + ")"
_EVENT_INSERT = "INSERT INTO main.events_8k VALUES (" + ", ".join(["?"] * 8) + ")"


@dataclass(frozen=True)
class FundamentalRow:
    """One ``main.fundamentals_pit`` row; dates and timestamps are UTC.

    ``acceptance_ts`` is the filing's acceptance time (UTC), ``version`` the
    1-based version of the period ordered by ``(acceptance_ts, accession)`` and
    ``first_value`` the value of version 1 (``None`` when not set by a test).
    ``ticker`` and ``currency`` may be ``None`` for shares/pure units;
    ``restatement_kind`` is the contract's last column (``none``,
    ``split_adjustment`` or ``revision``) and is ``none`` unless a test sets it.
    """

    cik: str
    field: str
    value: float
    period_end: date
    accession: str
    acceptance_ts: datetime
    period_start: date | None = None
    period_type: str = "FY"
    form: str = "10-K"
    ts_imputed: bool = False
    unit: str = "USD"
    currency: str | None = "USD"
    source_taxonomy: str = "us-gaap"
    source_concept: str = "Revenues"
    concept_rank: int = 1
    ticker: str | None = None
    version: int = 1
    first_value: float | None = None
    is_restatement: bool = False
    pct_change_vs_first: float | None = None
    restatement_kind: str = "none"


@dataclass(frozen=True)
class EventRow:
    """One ``main.events_8k`` row; ``acceptance_ts`` is the filing's time (UTC)."""

    cik: str
    accession: str
    acceptance_ts: datetime
    item_code: str
    item_label: str = "unknown"
    form: str = "8-K"
    ts_imputed: bool = False
    ticker: str | None = None


def build_pit_db(
    fundamentals: Iterable[FundamentalRow] = (),
    events: Iterable[EventRow] = (),
    *,
    path: str | Path | None = None,
) -> duckdb.DuckDBPyConnection:
    """Create ``main.fundamentals_pit`` and ``main.events_8k`` filled with the rows.

    Rows are inserted in the order given. ``path`` selects the database file;
    without it the database is in memory. The session timezone is UTC (decision
    A-5) and the returned connection is open: the caller closes it, or moves the
    database file into place before opening it read-only.
    """
    con = duckdb.connect(":memory:" if path is None else str(path))
    con.execute("SET TimeZone='UTC'")
    con.execute("CREATE SCHEMA IF NOT EXISTS main")
    con.execute(FUNDAMENTALS_PIT_DDL)
    con.execute(EVENTS_8K_DDL)
    fundamental_rows = [
        (
            row.cik,
            row.ticker,
            row.field,
            row.period_start,
            row.period_end,
            row.period_type,
            row.accession,
            row.form,
            row.acceptance_ts,
            row.ts_imputed,
            row.value,
            row.unit,
            row.currency,
            row.source_taxonomy,
            row.source_concept,
            row.concept_rank,
            row.version,
            row.first_value,
            row.is_restatement,
            row.pct_change_vs_first,
            row.restatement_kind,
        )
        for row in fundamentals
    ]
    if fundamental_rows:
        con.executemany(_FUNDAMENTAL_INSERT, fundamental_rows)
    event_rows = [
        (
            row.cik,
            row.accession,
            row.form,
            row.acceptance_ts,
            row.ts_imputed,
            row.item_code,
            row.item_label,
            row.ticker,
        )
        for row in events
    ]
    if event_rows:
        con.executemany(_EVENT_INSERT, event_rows)
    return con
