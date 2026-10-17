"""FastAPI service over :mod:`edgarpit.query` (issue #30).

Every endpoint answers point-in-time questions: a value is known at time ``t``
if and only if the filing that carried it has ``acceptance_ts <= t``. The
database is ``EDGARPIT_DB`` (:class:`edgarpit.config.Settings.edgarpit_db`);
each request opens one read-only connection to it and closes it again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from typing import Annotated, Literal

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, PlainSerializer

from edgarpit import query
from edgarpit.config import Settings


def _iso_z(value: datetime) -> str:
    """Serialise a timestamp as ISO-8601 UTC with a ``Z`` suffix."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


UtcTimestamp = Annotated[datetime, PlainSerializer(_iso_z, return_type=str)]


class AsOfResponse(BaseModel):
    """A ``fundamentals_pit`` value as it was known at a point in time.

    ``acceptance_ts`` is ISO-8601 UTC with a ``Z`` suffix; ``value`` is in
    ``unit`` (an ISO currency code, ``shares`` or ``pure``).
    """

    model_config = ConfigDict(from_attributes=True)

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
    acceptance_ts: UtcTimestamp
    version: int
    is_restatement: bool
    ts_imputed: bool


class EventResponse(BaseModel):
    """One 8-K item with the timestamp of the filing that carried it.

    ``acceptance_ts`` is ISO-8601 UTC with a ``Z`` suffix.
    """

    model_config = ConfigDict(from_attributes=True)

    cik: str
    ticker: str | None
    accession: str
    form: str
    acceptance_ts: UtcTimestamp
    ts_imputed: bool
    item_code: str
    item_label: str


class HealthResponse(BaseModel):
    """Service liveness."""

    status: str


app = FastAPI(
    title="edgar-pit",
    summary="Point-in-time SEC fundamentals and corporate events.",
    description=(
        "A value is known at time t if and only if its filing's acceptance timestamp "
        "is at or before t. Timestamps are ISO-8601 UTC with a 'Z' suffix; values are "
        "in the unit reported by the filing."
    ),
    version="0.1.0",
)


def database() -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield one read-only connection to ``EDGARPIT_DB`` per request.

    The session timezone is UTC so ``TIMESTAMPTZ`` values come back as UTC;
    the connection is closed when the request is done.
    """
    con = duckdb.connect(str(Settings().edgarpit_db), read_only=True)
    try:
        con.execute("SET TimeZone='UTC'")
        yield con
    finally:
        con.close()


def _identifier(ticker: str | None, cik: str | None) -> str:
    """Return the identifier to query, enforcing exactly one of ticker or CIK.

    Raises :class:`fastapi.HTTPException` with status 422 when both or neither
    are given, or when ``cik`` is not a string of 1-10 digits.
    """
    if (ticker is None) == (cik is None):
        raise HTTPException(status_code=422, detail="provide exactly one of 'ticker' or 'cik'")
    if cik is not None:
        text = cik.strip()
        if not text.isdigit() or len(text) > 10:
            raise HTTPException(status_code=422, detail="'cik' must be 1-10 digits")
        return text
    assert ticker is not None
    return ticker


def _execute[ResultT](operation: Callable[[], list[ResultT]]) -> list[ResultT]:
    """Run a query, mapping lookup failures to 404 and bad input to 422."""
    try:
        return operation()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["service"],
    summary="Liveness check",
)
def health() -> HealthResponse:
    """Return ``{"status": "ok"}``; does not touch the database."""
    return HealthResponse(status="ok")


@app.get(
    "/asof",
    response_model=list[AsOfResponse],
    tags=["point-in-time"],
    summary="Values known at a point in time",
)
def asof(
    field: Annotated[str, Query(description="Canonical field, e.g. 'revenue'.")],
    asof: Annotated[
        datetime,
        Query(
            description=(
                "As-of instant, ISO-8601 with 'Z' or an offset "
                "(e.g. '2024-01-15T00:00:00Z'). Naive timestamps are rejected."
            )
        ),
    ],
    db: Annotated[duckdb.DuckDBPyConnection, Depends(database)],
    ticker: Annotated[
        str | None, Query(description="Issuer ticker, case-insensitive (e.g. 'RY').")
    ] = None,
    cik: Annotated[
        str | None, Query(description="Issuer CIK, 1-10 digits; zero-padded internally.")
    ] = None,
    period: Annotated[Literal["FY", "Q"], Query(description="Period type: 'FY' or 'Q'.")] = "FY",
    n: Annotated[int, Query(description="Number of most recent periods to return.", ge=1)] = 1,
) -> list[AsOfResponse]:
    """Return the ``n`` most recent periods of ``field`` known at ``asof``.

    Answer is empty when the issuer has no such value; unknown issuers give 404
    and naive or malformed timestamps give 422.
    """
    identifier = _identifier(ticker, cik)
    rows = _execute(lambda: query.asof(identifier, field, asof, period, n, db=db))
    return [AsOfResponse.model_validate(row) for row in rows]


@app.get(
    "/history",
    response_model=list[AsOfResponse],
    tags=["point-in-time"],
    summary="Every version of a field",
)
def history(
    field: Annotated[str, Query(description="Canonical field, e.g. 'revenue'.")],
    db: Annotated[duckdb.DuckDBPyConnection, Depends(database)],
    ticker: Annotated[
        str | None, Query(description="Issuer ticker, case-insensitive (e.g. 'RY').")
    ] = None,
    cik: Annotated[
        str | None, Query(description="Issuer CIK, 1-10 digits; zero-padded internally.")
    ] = None,
    period: Annotated[
        Literal["FY", "Q"] | None,
        Query(description="Restrict to 'FY' or 'Q'; all period types when omitted."),
    ] = None,
) -> list[AsOfResponse]:
    """Return every version of ``field`` ordered by period and version.

    The result is empty when the issuer has no such value; unknown issuers give
    404.
    """
    identifier = _identifier(ticker, cik)
    rows = _execute(lambda: query.history(identifier, field, period, db=db))
    return [AsOfResponse.model_validate(row) for row in rows]


@app.get(
    "/events",
    response_model=list[EventResponse],
    tags=["point-in-time"],
    summary="8-K items by acceptance time",
)
def events(
    db: Annotated[duckdb.DuckDBPyConnection, Depends(database)],
    ticker: Annotated[
        str | None, Query(description="Issuer ticker, case-insensitive (e.g. 'AAPL').")
    ] = None,
    cik: Annotated[
        str | None, Query(description="Issuer CIK, 1-10 digits; zero-padded internally.")
    ] = None,
    item: Annotated[str | None, Query(description="Item code to keep, e.g. '2.02'.")] = None,
    start: Annotated[
        datetime | None,
        Query(
            alias="from",
            description=(
                "Lower bound on acceptance_ts, inclusive, ISO-8601 with 'Z' or an offset."
            ),
        ),
    ] = None,
    end: Annotated[
        datetime | None,
        Query(
            alias="to",
            description=(
                "Upper bound on acceptance_ts, inclusive, ISO-8601 with 'Z' or an offset."
            ),
        ),
    ] = None,
) -> list[EventResponse]:
    """Return 8-K items ordered by acceptance time.

    ``from``/``to`` bound ``acceptance_ts`` inclusively; naive timestamps give
    422 and unknown issuers give 404.
    """
    identifier = _identifier(ticker, cik)
    rows = _execute(lambda: query.events(identifier, item, start, end, db=db))
    return [EventResponse.model_validate(row) for row in rows]
