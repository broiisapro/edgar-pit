"""The ``edgarpit`` command line (PRD section 12, FR-1.14).

Commands::

    edgarpit universe resolve [--universe CSV] (--offline --fixtures DIR | --data DIR) [--write]
    edgarpit fetch --universe CSV --out DIR [--incremental]
    edgarpit fetch-text --db PATH --items ITEM [--items ITEM ...] [--limit N] [--data DIR]
    edgarpit fx --out DIR
    edgarpit build (--offline --fixtures DIR | --data DIR) --out DIR
    edgarpit asof --db PATH (--cik CIK | --ticker T) --field F --asof TS
        [--period FY|Q] [--n N] [--currency USD] [--json]
    edgarpit qa --db PATH
    edgarpit export --db PATH --out DIR

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

import duckdb
import typer

from edgarpit.config import ConfigError, Settings
from edgarpit.export import export_release
from edgarpit.fetch import FetchReport, fetch_universe
from edgarpit.fx.valet import FxFetchReport, fetch_fx
from edgarpit.load import BUILD_INFO_NAME, BUILD_INFO_TABLES, build_from_raw
from edgarpit.qa import QaReport, format_phase2, run_qa
from edgarpit.query import AsOfResult
from edgarpit.query import asof as query_asof
from edgarpit.rawcache import MANIFEST_NAME, RawCache
from edgarpit.sec_client import SecClient, SecClientError, company_tickers_url
from edgarpit.text.fetch import TextFetchReport, fetch_text_documents
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
    """The ``--json`` object for one result: the fields of expected_asof.json plus the rest.

    ``asof_ts`` is the row's known time for a ``--currency USD`` read (the later
    of the filing's acceptance time and the FX rate's known time, decision A-41)
    and ``None`` for a reported read.
    """
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
        "restatement_kind": row.restatement_kind,
        "ts_imputed": row.ts_imputed,
        "asof_ts": None if row.asof_ts is None else _iso_ts(row.asof_ts),
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
    incremental: Annotated[
        bool,
        typer.Option(
            "--incremental",
            help="probe each issuer with one submissions request and skip the unchanged ones",
        ),
    ] = False,
) -> None:
    """Fetch submissions, pages, companyfacts and SGML headers for the universe.

    One rate-limited client is used for the whole run; headers already in the
    cache are never re-fetched. A header 404 is tolerated; any other final HTTP
    error fails that issuer and the command exits 1.

    With ``--incremental`` each issuer costs one submissions request: an issuer
    whose newest accession and filing count are unchanged keeps its cached
    pages, companyfacts and headers (decision A-34). The report then prints
    ``skipped N unchanged`` and marks each skipped issuer ``unchanged``.
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
            report = fetch_universe(issuers, out, client=client, incremental=incremental)
    except ConfigError as error:
        _fail(str(error), code=2)
    _print_fetch(report, out, incremental=incremental)
    if not report.ok:
        raise typer.Exit(1)


def _print_fetch(report: FetchReport, out: Path, *, incremental: bool) -> None:
    """Print the fetch report; ``incremental`` widens the status column and adds the skip count."""
    status_width = 9 if incremental else 6
    for line in report.errors:
        typer.echo(f"error: {line}", err=True)
    typer.echo(
        f"{'issuer':<8} {'cik':<10} {'status':<{status_width}} {'requests':>8} "
        f"{'headers':>7} {'cached':>6} {'404':>4}"
    )
    for row in report.issuers:
        if row.errors:
            status = "failed"
        elif row.unchanged:
            status = "unchanged"
        else:
            status = "ok"
        typer.echo(
            f"{row.ticker:<8} {row.cik or '-':<10} "
            f"{status:<{status_width}} {row.requests:>8} "
            f"{row.headers_fetched:>7} {row.headers_cached:>6} {row.headers_not_found:>4}"
        )
    if incremental:
        typer.echo(f"skipped {report.skipped_unchanged} unchanged")
    typer.echo(
        f"summary: {report.issuers_ok} issuers ok, {report.issuers_failed} failed; "
        f"{report.requests} requests; {report.elapsed_seconds:.1f}s; "
        f"headers {report.headers_fetched} fetched, {report.headers_cached} cached, "
        f"{report.headers_not_found} not found"
    )
    typer.echo(f"raw cache: {out}")


@app.command("fetch-text")
def fetch_text(
    db: Annotated[Path, typer.Option("--db", help="built database holding raw.events_8k")],
    items: Annotated[
        list[str],
        typer.Option("--items", help="8-K item code whose filings to fetch, repeatable"),
    ],
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="stop after this many filings (default: all)"),
    ] = None,
    data: Annotated[
        Path,
        typer.Option("--data", help="raw-cache directory to fetch into (default ./data)"),
    ] = Path("data"),
) -> None:
    """Fetch filing indexes and 8-K documents for the filings carrying ``--items``.

    ``--db`` is a built database whose ``raw.events_8k`` selects the filings;
    ``--items`` is repeatable (``--items 2.02 --items 7.01``); ``--limit`` caps
    the number of filings. The filing index page and every in-scope document
    (the primary 8-K document and each ``EX-99.1`` exhibit) go into the raw
    cache under ``--data``; an already cached one is never requested again
    (decisions A-30, A-37, A-40). Nothing is written to the database: the next
    ``edgarpit build`` ingests the cached text.

    The client is built before the database is read, so a missing or malformed
    ``SEC_USER_AGENT`` exits 2 before any request, exactly like ``fetch``. A
    database without ``raw.events_8k`` exits 1 with the selection's message; a
    404 on one index or document is printed as ``not found`` and the run
    continues (exit 0), because the item is permanently absent rather than
    transiently unavailable.
    """
    if limit is not None and limit < 0:
        raise typer.BadParameter("--limit must be zero or positive")
    try:
        client = SecClient(Settings(), cache=RawCache(data))
    except ConfigError as error:
        _fail(str(error), code=2)
    try:
        with client:
            report = fetch_text_documents(db, data, items, limit=limit, client=client)
    except (SecClientError, ValueError) as error:
        _fail(str(error))
    except OSError as error:
        _fail(f"{type(error).__name__}: {error}")
    _print_text_fetch(report, data)


def _print_text_fetch(report: TextFetchReport, data: Path) -> None:
    """Print the five counts of a text fetch and every key that answered 404."""
    typer.echo(f"filings selected: {report.filings}")
    typer.echo(f"index pages fetched: {report.indexes_fetched}")
    typer.echo(f"documents fetched: {report.documents_fetched}")
    typer.echo(f"already cached: {report.already_cached}")
    for key in report.failed:
        typer.echo(f"  not found: {key}", err=True)
    typer.echo(f"failed: {len(report.failed)}")
    typer.echo(f"raw cache: {data}")


@app.command()
def fx(
    out: Annotated[Path, typer.Option("--out", help="raw-cache directory to write into")],
) -> None:
    """Fetch the Bank of Canada ``FXUSDCAD`` series into the raw cache.

    One request fetches the whole series (Canadian dollars per US dollar,
    business daily). The body is stored under ``raw/fx/_/FXUSDCAD/`` only when
    it differs from the latest cached copy, so a repeat run on an unchanged
    series adds no file. Prints the series id, the observation count, the first
    and last observation dates and whether a file was stored (decisions A-32,
    A-37). Exits 2 without a valid ``SEC_USER_AGENT`` (the Bank asks automated
    clients to identify themselves, and the client sends the same contact) and
    1 on an HTTP or parse failure.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        report = fetch_fx(out)
    except ConfigError as error:
        _fail(str(error), code=2)
    except (SecClientError, ValueError) as error:
        _fail(str(error))
    except OSError as error:
        _fail(f"{type(error).__name__}: {error}")
    _print_fx(report, out)


def _print_fx(report: FxFetchReport, out: Path) -> None:
    """Print one Valet fetch: series, counts, date range and whether it was stored."""
    first = _iso_date(report.first_date) or "-"
    last = _iso_date(report.last_date) or "-"
    typer.echo(f"{report.series}: {report.observations} observations {first}..{last}")
    typer.echo("stored: yes" if report.stored else "stored: no (unchanged)")
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
    run the same local pipeline (no network is used either way). After a
    successful build the row count of every contract table and the dbt node
    summary are printed from the ``build-info.json`` the build wrote, then the
    database path.
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
        _print_build_summary(db)
    except Exception as error:  # the CLI boundary turns any build failure into exit 1
        _fail(f"{type(error).__name__}: {error}")
    typer.echo(f"database: {db}")


def _print_build_summary(db: Path) -> None:
    """Print the table row counts and dbt summary recorded next to ``db``.

    ``db`` is the database path returned by
    :func:`edgarpit.load.build_from_raw`; its sibling ``build-info.json`` holds
    the counts of :data:`edgarpit.load.BUILD_INFO_TABLES` and the dbt node
    summary. Raises ``RuntimeError`` when that file is missing or malformed,
    because a finished build must carry its provenance record.
    """
    info_path = Path(db).parent / BUILD_INFO_NAME
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read build info {info_path}: {error}") from error
    tables = info.get("tables")
    if not isinstance(tables, dict):
        raise RuntimeError(f"build info {info_path} has no table counts")
    for table in BUILD_INFO_TABLES:
        typer.echo(f"{table}: {int(tables[table])} rows")
    dbt = info.get("dbt")
    if isinstance(dbt, str):
        typer.echo(f"dbt build: {dbt}")


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
    currency: Annotated[
        str | None,
        typer.Option("--currency", help="read the USD view: USD"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="print JSON instead of text")] = False,
) -> None:
    """Print the value of ``field`` as it was known at ``--asof``.

    One of ``--cik`` or ``--ticker`` identifies the issuer. Text output prints
    value, currency, unit, period_start, period_end, accession and
    acceptance_ts, plus ``asof_ts`` for a ``--currency USD`` read; ``--json``
    prints an array with those keys plus the rest of the result (ticker,
    period_type, form, version, is_restatement, restatement_kind, ts_imputed)
    and the echoed ``asof``/``period`` arguments.

    ``--currency USD`` reads ``main.fundamentals_pit_usd`` and applies the
    point-in-time rule to each row's ``asof_ts``, the later of the filing's
    acceptance time and the FX rate's known time (decisions A-32, A-41), so a
    CAD amount is not returned before the rate that converted it was published.
    Each USD row carries that known time as ``asof_ts`` (ISO UTC in ``--json``,
    an ``asof_ts:`` line in text output); a reported read prints nothing there
    because its known time is its filing's ``acceptance_ts``.
    A database without that model exits 1 with the query's message; any other
    currency is a usage error (exit 2).
    """
    if (cik is None) == (ticker is None):
        raise typer.BadParameter("exactly one of --cik or --ticker is required")
    identifier = cik if cik is not None else ticker
    if identifier is None:  # pragma: no cover - guarded by the check above
        raise typer.BadParameter("exactly one of --cik or --ticker is required")
    if period not in ("FY", "Q"):
        raise typer.BadParameter("--period must be FY or Q")
    if currency not in (None, "USD"):
        raise typer.BadParameter("--currency must be USD")
    try:
        results = query_asof(
            identifier,
            field,
            asof_ts,
            cast(Literal["FY", "Q"], period),
            n,
            db=db,
            currency=cast(Literal["USD"] | None, currency),
        )
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
        if row.asof_ts is not None:
            typer.echo(f"asof_ts: {_iso_ts(row.asof_ts)}")


def _year_list(years: list[int]) -> str:
    return ", ".join(str(year) for year in years) if years else "none"


def _period(period_start: date | None, period_end: date) -> str:
    start = _iso_date(period_start) or "instant"
    return f"{start}..{period_end.isoformat()}"


def _print_qa(report: QaReport) -> None:
    typer.echo(
        f"Coverage ({report.canonical_fields} canonical fields, "
        f"{report.universe_issuers} universe issuers)"
    )
    for issuer_coverage in report.coverage:
        name = f" {issuer_coverage.name}" if issuer_coverage.name else ""
        typer.echo(
            f"  {issuer_coverage.cik} {issuer_coverage.ticker or '-'}{name}: "
            f"{len(issuer_coverage.by_field)} fields, "
            f"FY {_year_list(issuer_coverage.fy_years)}, {issuer_coverage.rows} rows"
        )
        for field_name, field_years in issuer_coverage.by_field.items():
            typer.echo(f"    {field_name}: {_year_list(field_years)}")
    for missing in report.issuers_without_rows:
        typer.echo(f"  no rows: {missing.cik} {missing.ticker} {missing.name}")

    typer.echo("Restatements")
    for restatement_count in report.restatement_counts:
        typer.echo(
            f"  {restatement_count.cik} {restatement_count.field} "
            f"[{restatement_count.kind}]: {restatement_count.count}"
        )
    for restatement in report.top_restatements:
        change = (
            ""
            if restatement.pct_change_vs_first is None
            else f" ({restatement.pct_change_vs_first:+.1%})"
        )
        typer.echo(
            f"  {restatement.cik} {restatement.field} "
            f"{_period(restatement.period_start, restatement.period_end)}: "
            f"{_format_value(restatement.first_value)} "
            f"@ {_iso_ts(restatement.first_acceptance_ts)} -> "
            f"{_format_value(restatement.value)} @ {_iso_ts(restatement.acceptance_ts)}{change}"
        )

    typer.echo("Imputed timestamps")
    for source, imputed_count in report.imputed_by_source.items():
        typer.echo(f"  {source}: {imputed_count}")
    typer.echo(f"Header-vs-API disagreements over 60 s: {report.disagreement_count}")
    for disagreement in report.disagreements:
        typer.echo(
            f"  {disagreement.cik} {disagreement.accession}: "
            f"{_iso_ts(disagreement.acceptance_ts)} vs "
            f"{_iso_ts(disagreement.acceptance_ts_api)} ({disagreement.seconds:.0f} s)"
        )

    typer.echo("Headers without acceptance time")
    for headerless in report.headers_without_acceptance:
        typer.echo(f"  {headerless.cik} {headerless.ticker or '-'}: {headerless.count}")
    typer.echo(
        f"  total: {sum(headerless.count for headerless in report.headers_without_acceptance)}"
    )

    typer.echo("Legacy 8-K events (accepted before 2004-08-23; item codes are unreliable)")
    for legacy in report.legacy_events:
        typer.echo(f"  {legacy.cik} {legacy.ticker or '-'}: {legacy.count}")

    typer.echo("Filings by source")
    for counted in report.filings_by_source:
        typer.echo(f"  {counted.source}: {counted.count}")
    typer.echo("Filings by acceptance_ts_source")
    for counted in report.filings_by_acceptance_source:
        typer.echo(f"  {counted.source}: {counted.count}")

    typer.echo("Unmapped concepts")
    for unmapped in report.unmapped_concepts:
        typer.echo(f"  {unmapped.taxonomy}:{unmapped.concept}: {unmapped.count}")

    typer.echo("Unit anomalies")
    for multi_currency in report.multi_currency:
        typer.echo(
            f"  {multi_currency.cik} {multi_currency.field}: "
            f"multiple currencies {', '.join(multi_currency.currencies)}"
        )
    for unexpected_unit in report.unexpected_units:
        families = ", ".join(unexpected_unit.expected_families) or "no declared family"
        typer.echo(
            f"  {unexpected_unit.cik} {unexpected_unit.field} unit {unexpected_unit.unit}: "
            f"expected {families}"
        )

    typer.echo("Ratio gaps (input present, no ratios_pit row)")
    for reason, count in report.ratio_gap_counts.items():
        typer.echo(f"  {reason}: {count}")
    for gap in report.ratio_gaps:
        typer.echo(
            f"  {gap.cik} {gap.ratio} {_period(gap.period_start, gap.period_end)} "
            f"{gap.period_type}: {gap.reason}"
        )

    typer.echo("Conflicting values within one accession")
    for conflict in report.conflicting_values:
        values = ", ".join(_format_value(value) for value in conflict.values)
        typer.echo(
            f"  {conflict.cik} {conflict.accession} {conflict.taxonomy}:{conflict.concept} "
            f"{conflict.unit} {_period(conflict.period_start, conflict.period_end)}: {values}"
        )

    if report.ok:
        typer.echo("Key uniqueness: OK")
    else:
        typer.echo("Key uniqueness: violated")
        for violation in report.key_violations:
            typer.echo(
                f"  {violation.table} ({violation.key}): {violation.duplicates} duplicate keys"
            )
    for line in format_phase2(report):
        typer.echo(line)


@app.command()
def qa(db: Annotated[Path, typer.Option("--db", help="DuckDB file to report on")]) -> None:
    """Print the data-quality report; exit 1 on a duplicate key or an ``asof_ts`` leak.

    The Phase 2 sections (FX coverage, USD conversion, 8-K text) follow the
    Phase 1 sections and render ``not built`` for a database that predates them.
    A non-zero exit names every failing check: duplicated contract keys and
    ``main.fundamentals_pit_usd`` rows whose ``asof_ts`` precedes their
    acceptance time or FX known time (decisions A-32, A-41).
    """
    try:
        con = duckdb.connect(str(db), read_only=True)
    except Exception as error:  # the CLI boundary turns any connect failure into exit 1
        _fail(f"{type(error).__name__}: {error}")
    try:
        report = run_qa(con)
    except Exception as error:  # a missing table or failed query is a data failure
        _fail(f"{type(error).__name__}: {error}")
    finally:
        con.close()
    _print_qa(report)
    if not report.ok:
        reasons: list[str] = []
        if report.key_violations:
            tables = ", ".join(violation.table for violation in report.key_violations)
            reasons.append(f"key uniqueness violated in {tables}")
        if report.usd_conversion.asof_violation_count:
            reasons.append(
                f"{report.usd_conversion.asof_violation_count} asof_ts violations in "
                "main.fundamentals_pit_usd (asof_ts before acceptance_ts or fx_known_at)"
            )
        _fail("; ".join(reasons))


@app.command()
def export(
    db: Annotated[Path, typer.Option("--db", help="DuckDB file to export")],
    out: Annotated[Path, typer.Option("--out", help="output directory for the Parquet release")],
    data: Annotated[
        Path | None,
        typer.Option(
            "--data",
            help="raw-cache directory whose raw/manifest.jsonl supplies the SEC fetch range",
        ),
    ] = None,
) -> None:
    """Write one Parquet file per released table plus ``manifest.json``.

    The release manifest records the SEC fetch date range from ``--data``
    (min/max ``fetched_at`` of ``<data>/raw/manifest.jsonl``) when given;
    otherwise from the ``build-info.json`` next to the database, when one
    exists. Without either the range stays null.
    """
    raw_manifest: Path | None = None
    build_info: Path | None = None
    if data is not None:
        candidate = Path(data) / "raw" / MANIFEST_NAME
        if not candidate.is_file():
            _fail(f"no raw manifest under {data}; expected {candidate}")
        raw_manifest = candidate
    else:
        candidate = Path(db).parent / BUILD_INFO_NAME
        build_info = candidate if candidate.is_file() else None
    try:
        con = duckdb.connect(str(db), read_only=True)
    except Exception as error:  # the CLI boundary turns a connect failure into exit 1
        _fail(f"{type(error).__name__}: {error}")
    try:
        manifest = export_release(con, out, raw_manifest=raw_manifest, build_info=build_info)
    except Exception as error:  # a missing table or failed write is a data failure
        _fail(f"{type(error).__name__}: {error}")
    finally:
        con.close()
    for entry in manifest.files:
        typer.echo(f"{entry.file}: {entry.rows} rows, sha256 {entry.sha256}")
    typer.echo(f"manifest: {Path(out) / 'manifest.json'}")


if __name__ == "__main__":  # pragma: no cover - console-script entry is `edgarpit.cli:app`
    app()
