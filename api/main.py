"""FastAPI service over :mod:`edgarpit.query` (issue #30, issue #113).

Every endpoint answers point-in-time questions: a value is known at time ``t``
if and only if the filing that carried it has ``acceptance_ts <= t``. A value
converted to USD additionally waits for the FX rate that converted it, so USD
reads apply the rule to the model's ``asof_ts`` (decision A-32). The database is
``EDGARPIT_DB`` (:class:`edgarpit.config.Settings.edgarpit_db`); each request
opens one read-only connection to it and closes it again.

A database built before the Phase 2 models (a 1.0.0 release) has neither
``main.fundamentals_pit_usd`` nor ``main.filings_text``; asking for USD values
or 8-K text from such a database answers 501 with the missing table named,
instead of a generic server error.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from typing import Annotated, Literal

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, PlainSerializer

from edgarpit import __version__, query
from edgarpit.config import Settings


def _iso_z(value: datetime) -> str:
    """Serialise a timestamp as ISO-8601 UTC with a ``Z`` suffix."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


UtcTimestamp = Annotated[datetime, PlainSerializer(_iso_z, return_type=str)]

CurrencyQuery = Annotated[
    Literal["USD"] | None,
    Query(
        description=(
            "Read values converted to USD (only 'USD' is supported). The point-in-time "
            "rule then applies to the FX conversion time (max of the filing's acceptance "
            "and the rate's publication), so a CAD value appears only once the rate that "
            "converted it was published. Requires a database with the FX model."
        )
    ),
]


class AsOfResponse(BaseModel):
    """A ``fundamentals_pit`` value as it was known at a point in time.

    ``acceptance_ts`` is ISO-8601 UTC with a ``Z`` suffix; ``value`` is in
    ``unit`` (an ISO currency code, ``shares`` or ``pure``) and
    ``restatement_kind`` is ``none``, ``split_adjustment`` or ``revision``. With
    ``currency=USD`` the value is converted, ``currency`` is ``USD`` and ``unit``
    carries the reported unit with its currency replaced (``CAD`` → ``USD``,
    ``CAD/shares`` → ``USD/shares``); ``acceptance_ts`` stays the filing's time.
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
    restatement_kind: str


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


class TextResponse(BaseModel):
    """One section of one stored 8-K document.

    ``acceptance_ts`` is ISO-8601 UTC with a ``Z`` suffix and is the time the
    filing that carried the text was accepted. ``document`` is the EDGAR file
    name, ``doc_type`` is ``primary`` or ``EX-99.1``, ``section`` an EDGAR item
    code (``2.02``) or ``unsegmented`` and ``section_order`` its 1-based
    position in the document. ``text`` is the cleaned plain text, ``sha256`` its
    lowercase hex digest and ``char_count`` its length in characters.
    """

    model_config = ConfigDict(from_attributes=True)

    cik: str
    ticker: str | None
    accession: str
    acceptance_ts: UtcTimestamp
    ts_imputed: bool
    document: str
    doc_type: str
    section: str
    section_order: int
    text: str
    sha256: str
    char_count: int


class HealthResponse(BaseModel):
    """Service liveness."""

    status: str


app = FastAPI(
    title="edgar-pit",
    summary="Point-in-time SEC fundamentals and corporate events.",
    description=(
        "A value is known at time t if and only if its filing's acceptance timestamp "
        "is at or before t. Timestamps are ISO-8601 UTC with a 'Z' suffix; values are "
        "in the unit reported by the filing, or in USD when currency=USD is requested. "
        "8-K text is available per document section at /text."
    ),
    version=__version__,
)


@app.exception_handler(query.MissingTableError)
def missing_table(_: Request, exc: query.MissingTableError) -> JSONResponse:
    """Answer 501 when the database predates the model the request needs.

    A 1.0.0 release has no ``main.fundamentals_pit_usd`` or
    ``main.filings_text``; that is a property of the deployment, not of the
    request, so it is neither 404 nor 422 and the body names the missing table.
    """
    return JSONResponse(status_code=501, content={"detail": str(exc)})


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
    currency: CurrencyQuery = None,
) -> list[AsOfResponse]:
    """Return the ``n`` most recent periods of ``field`` known at ``asof``.

    Answer is empty when the issuer has no such value; unknown issuers give 404,
    naive or malformed timestamps give 422 and a database without the FX model
    gives 501 for ``currency=USD``.
    """
    identifier = _identifier(ticker, cik)
    rows = _execute(
        lambda: query.asof(identifier, field, asof, period, n, db=db, currency=currency)
    )
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
    currency: CurrencyQuery = None,
) -> list[AsOfResponse]:
    """Return every version of ``field`` ordered by period and version.

    The result is empty when the issuer has no such value; unknown issuers give
    404 and a database without the FX model gives 501 for ``currency=USD``.
    Rows that could not be converted are absent from a USD answer.
    """
    identifier = _identifier(ticker, cik)
    rows = _execute(lambda: query.history(identifier, field, period, db=db, currency=currency))
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


@app.get(
    "/text",
    response_model=list[TextResponse],
    tags=["point-in-time"],
    summary="Stored 8-K text by acceptance time",
)
def text(
    db: Annotated[duckdb.DuckDBPyConnection, Depends(database)],
    ticker: Annotated[
        str | None, Query(description="Issuer ticker, case-insensitive (e.g. 'AAPL').")
    ] = None,
    cik: Annotated[
        str | None, Query(description="Issuer CIK, 1-10 digits; zero-padded internally.")
    ] = None,
    item: Annotated[
        str | None,
        Query(
            description=(
                "Keep only filings whose 8-K items contain this code (e.g. '2.02'); "
                "every section and exhibit of those filings is returned."
            )
        ),
    ] = None,
    start: Annotated[
        datetime | None,
        Query(
            description="Lower bound on acceptance_ts, inclusive, ISO-8601 with 'Z' or an offset."
        ),
    ] = None,
    end: Annotated[
        datetime | None,
        Query(
            description="Upper bound on acceptance_ts, inclusive, ISO-8601 with 'Z' or an offset."
        ),
    ] = None,
) -> list[TextResponse]:
    """Return the stored 8-K text of the issuer ordered by acceptance time.

    Rows are the sections of the primary 8-K/8-K/A document plus one
    ``unsegmented`` row per ``EX-99.1`` exhibit. ``item`` selects the filings
    through their 8-K items; ``start``/``end`` bound ``acceptance_ts``
    inclusively. Naive timestamps give 422, unknown issuers 404 and a database
    without the text model 501.
    """
    identifier = _identifier(ticker, cik)
    rows = _execute(lambda: query.filing_text(identifier, item=item, start=start, end=end, db=db))
    return [TextResponse.model_validate(row) for row in rows]
