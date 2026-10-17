"""The ``edgarpit`` command line (PRD section 12, FR-1.14).

Commands::

    edgarpit universe resolve [--universe CSV] (--offline --fixtures DIR | --data DIR) [--write]
    edgarpit fetch --universe CSV --out DIR
    edgarpit build (--offline --fixtures DIR | --data DIR) --out DIR
    edgarpit asof --db PATH (--cik CIK | --ticker T) --field F --asof TS
        [--period FY|Q] [--n N] [--json]

Exit codes: ``0`` success, ``1`` a data or fetch failure (missing value,
unresolved issuer, HTTP errors after retries, build failure), ``2`` a usage
error (bad flags, malformed timestamps, missing or malformed
``SEC_USER_AGENT``). Every failure prints a one-line cause to stderr.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn, cast

import typer

from edgarpit.config import ConfigError, Settings
from edgarpit.fetch import FetchReport, fetch_universe
from edgarpit.load import build_from_raw
from edgarpit.query import AsOfResult
from edgarpit.query import asof as query_asof
from edgarpit.rawcache import RawCache
from edgarpit.sec_client import SecClient, SecClientError, company_tickers_url
from edgarpit.universe import ResolveReport, UniverseError, load_universe, resolve, write_universe

DEFAULT_UNIVERSE = Path("config/universe.csv")

app = typer.Typer(help="Point-in-time fundamentals and corporate events from SEC EDGAR.")
universe_app = typer.Typer(help="Issuer universe commands.")
app.add_typer(universe_app, name="universe")


def _fail(message: str, *, code: int = 1) -> NoReturn:
    """Print a one-line cause to stderr and exit with ``code``."""
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def _iso_date(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _iso_ts(value: datetime) -> str:
    """Format an aware UTC timestamp as ISO-8601 with a ``Z`` suffix."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _format_value(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


def _asof_payload(row: AsOfResult, *, asof_ts: str, period: str) -> dict[str, Any]:
    """The ``--json`` object for one result: the fields of expected_asof.json plus the rest."""
    return {
        "cik": row.cik,
        "ticker": row.ticker,
        "field": row.field,
        "asof": asof_ts,
        "period": period,
        "value": row.value,
        "unit": row.unit,
        "currency": row.currency,
        "period_start": _iso_date(row.period_start),
        "period_end": _iso_date(row.period_end),
        "period_type": row.period_type,
        "accession": row.accession,
        "form": row.form,
        "acceptance_ts": _iso_ts(row.acceptance_ts),
        "version": row.version,
        "is_restatement": row.is_restatement,
        "ts_imputed": row.ts_imputed,
    }


@universe_app.command("resolve")
def universe_resolve(
    universe: Annotated[
        Path, typer.Option("--universe", help="universe CSV to resolve")
    ] = DEFAULT_UNIVERSE,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="read company_tickers.json from --fixtures with no network"),
    ] = False,
    fixtures: Annotated[
        Path | None,
        typer.Option("--fixtures", help="raw-cache directory to read offline (with --offline)"),
    ] = None,
    data: Annotated[
        Path | None,
        typer.Option("--data", help="raw-cache directory to fetch company_tickers.json into"),
    ] = None,
    write: Annotated[
        bool,
        typer.Option(
            "--write", help="write filled CIKs back to --universe when resolution is clean"
        ),
    ] = False,
) -> None:
    """Validate every ticker against SEC's company_tickers.json and fill missing CIKs.

    With ``--offline`` the mapping is read from the ``--fixtures`` cache with no
    network; with ``--data`` it is fetched and cached first. Any unresolved
    ticker or CIK disagreement is reported to stderr and exits 1; nothing is
    written and no CIK is ever rewritten silently.
    """
    if offline and data is not None:
        raise typer.BadParameter("--offline and --data are mutually exclusive")
    if offline:
        if fixtures is None:
            raise typer.BadParameter("--offline needs --fixtures DIR")
        client = SecClient(Settings(sec_user_agent=None), offline_dir=fixtures)
    elif data is not None:
        try:
            client = SecClient(Settings(), cache=RawCache(data))
        except ConfigError as error:
            _fail(str(error), code=2)
    else:
        raise typer.BadParameter("specify --offline --fixtures DIR or --data DIR")

    try:
        issuers = load_universe(universe)
        with client:
            document = client.get_json(company_tickers_url())
    except (UniverseError, SecClientError, OSError, ValueError) as error:
        _fail(str(error))
    report = resolve(issuers, document)
    _print_resolve(report)
    if not report.ok:
        raise typer.Exit(1)
    if write:
        write_universe(universe, report.issuers)
        typer.echo(f"wrote {len(report.issuers)} issuers to {universe}")


def _print_resolve(report: ResolveReport) -> None:
    for ticker, cik in report.filled:
        typer.echo(f"filled {ticker} -> {cik}")
    for disagreement in report.disagreements:
        typer.echo(
            f"disagreement: {disagreement.ticker} has CIK {disagreement.universe_cik} "
            f"but SEC maps it to {disagreement.sec_cik}",
            err=True,
        )
    for ticker in report.unresolved:
        typer.echo(f"unresolved ticker: {ticker}", err=True)
    typer.echo(
        f"resolved {len(report.issuers)} issuers: {len(report.filled)} filled, "
        f"{len(report.disagreements)} disagreements, {len(report.unresolved)} unresolved"
    )


@app.command()
def fetch(
    universe: Annotated[Path, typer.Option("--universe", help="universe CSV with resolved CIKs")],
    out: Annotated[Path, typer.Option("--out", help="raw-cache directory to fetch into")],
) -> None:
    """Fetch submissions, pages, companyfacts and SGML headers for the universe.

    One rate-limited client is used for the whole run; headers already in the
    cache are never re-fetched. A header 404 is tolerated; any other final HTTP
    error fails that issuer and the command exits 1.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        issuers = load_universe(universe)
    except UniverseError as error:
        _fail(str(error))
    missing = [issuer.ticker for issuer in issuers if issuer.cik is None]
    if missing:
        _fail(
            f"issuers without a CIK: {', '.join(missing)}; "
            "run 'edgarpit universe resolve --write' first"
        )
    try:
        with SecClient(Settings(), cache=RawCache(out)) as client:
            report = fetch_universe(issuers, out, client=client)
    except ConfigError as error:
        _fail(str(error), code=2)
    _print_fetch(report, out)
    if not report.ok:
        raise typer.Exit(1)


def _print_fetch(report: FetchReport, out: Path) -> None:
    for line in report.errors:
        typer.echo(f"error: {line}", err=True)
    typer.echo(
        f"fetched {len(report.issuers)} issuers in {report.elapsed_seconds:.1f}s: "
        f"{report.requests} requests, "
        f"{sum(row.headers_fetched for row in report.issuers)} headers fetched, "
        f"{sum(row.headers_cached for row in report.issuers)} already cached, "
        f"{sum(row.headers_not_found for row in report.issuers)} not found"
    )
    typer.echo(f"raw cache: {out}")


@app.command()
def build(
    out: Annotated[Path, typer.Option("--out", help="output directory for the DuckDB database")],
    offline: Annotated[
        bool, typer.Option("--offline", help="build from the --fixtures raw cache with no network")
    ] = False,
    fixtures: Annotated[
        Path | None, typer.Option("--fixtures", help="fixture raw cache (with --offline)")
    ] = None,
    data: Annotated[Path | None, typer.Option("--data", help="raw cache to build from")] = None,
) -> None:
    """Build ``<out>/edgarpit.duckdb`` from a raw cache: ingest, load, then dbt.

    ``--offline --fixtures DIR`` and ``--data DIR`` identify the raw cache; both
    run the same local pipeline (no network is used either way).
    """
    if offline and data is not None:
        raise typer.BadParameter("--offline and --data are mutually exclusive")
    if offline:
        if fixtures is None:
            raise typer.BadParameter("--offline needs --fixtures DIR")
        raw_dir = fixtures
    elif data is not None:
        raw_dir = data
    else:
        raise typer.BadParameter("specify --offline --fixtures DIR or --data DIR")
    try:
        db = build_from_raw(raw_dir, out)
    except Exception as error:  # the CLI boundary turns any build failure into exit 1
        _fail(f"{type(error).__name__}: {error}")
    typer.echo(f"database: {db}")


@app.command()
def asof(
    db: Annotated[Path, typer.Option("--db", help="DuckDB file to query")],
    field: Annotated[str, typer.Option("--field", help="canonical field, e.g. revenue")],
    asof_ts: Annotated[str, typer.Option("--asof", help="ISO-8601 timestamp with a timezone")],
    cik: Annotated[
        str | None, typer.Option("--cik", help="issuer CIK (digits, zero-padded)")
    ] = None,
    ticker: Annotated[str | None, typer.Option("--ticker", help="issuer ticker")] = None,
    period: Annotated[str, typer.Option("--period", help="period type: FY or Q")] = "FY",
    n: Annotated[
        int, typer.Option("--n", help="how many of the most recent periods to return")
    ] = 1,
    json_output: Annotated[bool, typer.Option("--json", help="print JSON instead of text")] = False,
) -> None:
    """Print the value of ``field`` as it was known at ``--asof``.

    One of ``--cik`` or ``--ticker`` identifies the issuer. Text output prints
    value, currency, unit, period_start, period_end, accession and
    acceptance_ts; ``--json`` prints an array with those keys plus the rest of
    the result (ticker, period_type, form, version, is_restatement,
    ts_imputed) and the echoed ``asof``/``period`` arguments.
    """
    if (cik is None) == (ticker is None):
        raise typer.BadParameter("exactly one of --cik or --ticker is required")
    identifier = cik if cik is not None else ticker
    if identifier is None:  # pragma: no cover - guarded by the check above
        raise typer.BadParameter("exactly one of --cik or --ticker is required")
    if period not in ("FY", "Q"):
        raise typer.BadParameter("--period must be FY or Q")
    try:
        results = query_asof(identifier, field, asof_ts, cast(Literal["FY", "Q"], period), n, db=db)
    except ValueError as error:
        _fail(str(error), code=2)
    except LookupError as error:
        _fail(str(error))
    except Exception as error:  # the CLI boundary turns an unreadable database into exit 1
        _fail(f"{type(error).__name__}: {error}")
    if not results:
        _fail(f"no {field!r} value known for {identifier} at {asof_ts}")
    if json_output:
        payload = [_asof_payload(row, asof_ts=asof_ts, period=period) for row in results]
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    for index, row in enumerate(results):
        if index:
            typer.echo("")
        typer.echo(f"value: {_format_value(row.value)}")
        typer.echo(f"currency: {row.currency or ''}")
        typer.echo(f"unit: {row.unit}")
        typer.echo(f"period_start: {_iso_date(row.period_start) or ''}")
        typer.echo(f"period_end: {_iso_date(row.period_end)}")
        typer.echo(f"accession: {row.accession}")
        typer.echo(f"acceptance_ts: {_iso_ts(row.acceptance_ts)}")


if __name__ == "__main__":  # pragma: no cover - console-script entry is `edgarpit.cli:app`
    app()
