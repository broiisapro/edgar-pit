"""Tests for the Phase 2 command-line surface (decisions A-30 to A-44).

Every command is driven through Typer's ``CliRunner``, so the flags, the printed
report and the exit code are the ones a user sees. The two fetching commands are
covered twice: once with the called function replaced (to pin the arguments,
including the client's cache root), and once end to end from the committed
fixture bytes served through respx, so the real selection, cache writes and
report printing run without a network.

The 8-K and FX fixtures lived in ``tests/fixtures/sec_text`` and
``tests/fixtures/fx`` and move into the shared ``tests/fixtures/sec`` cache when
the Phase 2 roots merge (decision A-44); the helpers below read whichever layout
the checkout has.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest
import respx
from typer.testing import CliRunner

from edgarpit.cli import app
from edgarpit.fetch import FetchReport, IssuerFetchReport
from edgarpit.fx.valet import FxFetchReport, valet_url
from edgarpit.qa import AsOfViolation, FxCoverage, QaReport, TextCoverage, UsdConversion
from edgarpit.rawcache import RawCache, document_key
from edgarpit.schemas import duckdb_ddl
from edgarpit.text.fetch import TextFetchReport

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUNNER = CliRunner()
USER_AGENT = "Test User test@example.com"

APPLE_CIK = "0000320193"
SHOP_CIK = "0001594805"
APPLE_Q4_2025 = "0000320193-25-000077"
APPLE_Q3_2026 = "0000320193-26-000018"
SHOP_Q2_2026 = "0001594805-26-000046"
APPLE_Q4_DOCUMENTS = (
    "a8-kex991q4202509272025.htm",
    "aapl-20251030.htm",
)

RY_CIK = "0001000275"
RY_ACCEPTANCE = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
RY_REPORTED = 56129000000.0
RY_USD = 77852000000.0
RY_FX_KNOWN_AT = datetime(2024, 1, 2, 22, 0, tzinfo=UTC)

_FUNDAMENTALS_DDL = """
CREATE TABLE main.fundamentals_pit (
    cik VARCHAR, ticker VARCHAR, field VARCHAR, value DOUBLE, unit VARCHAR,
    currency VARCHAR, period_start DATE, period_end DATE, period_type VARCHAR,
    accession VARCHAR, form VARCHAR, acceptance_ts TIMESTAMPTZ,
    version INTEGER, is_restatement BOOLEAN, ts_imputed BOOLEAN,
    restatement_kind VARCHAR
)
"""

_USD_DDL = """
CREATE TABLE main.fundamentals_pit_usd (
    cik VARCHAR, ticker VARCHAR, field VARCHAR, value_usd DOUBLE, unit VARCHAR,
    currency VARCHAR, period_start DATE, period_end DATE, period_type VARCHAR,
    accession VARCHAR, form VARCHAR, acceptance_ts TIMESTAMPTZ,
    version INTEGER, is_restatement BOOLEAN, ts_imputed BOOLEAN,
    restatement_kind VARCHAR, fx_rate DOUBLE, fx_rate_date DATE,
    fx_known_at TIMESTAMPTZ, asof_ts TIMESTAMPTZ, converted BOOLEAN,
    unconverted_reason VARCHAR
)
"""


# --- fixtures on disk ---------------------------------------------------------


def text_fixture_root() -> Path:
    """Return the raw-cache tree holding the 8-K index and document bytes."""
    separate = FIXTURES / "sec_text"
    return separate if separate.is_dir() else FIXTURES / "sec"


def fx_fixture_root() -> Path:
    """Return the raw-cache tree holding the ``FXUSDCAD`` fixture series."""
    separate = FIXTURES / "fx"
    if (separate / "raw" / "fx" / "_" / "FXUSDCAD").is_dir():
        return separate
    return FIXTURES / "sec"


def text_fixture_bodies() -> dict[str, bytes]:
    """Map every 8-K index/document fixture URL to its stored bytes.

    The manifest under the fixture root is the index of what was captured, so the
    respx handler answers exactly the requests the real discovery would make.
    """
    root = text_fixture_root()
    bodies: dict[str, bytes] = {}
    for line in (root / "raw" / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["endpoint"] in ("filing_index", "documents"):
            bodies[str(row["url"])] = (root / str(row["path"])).read_bytes()
    assert bodies, f"no filing_index/documents rows under {root}"
    return bodies


def fx_fixture_body() -> bytes:
    """Return the bytes of the stored ``FXUSDCAD`` Valet document."""
    paths = sorted((fx_fixture_root() / "raw" / "fx" / "_" / "FXUSDCAD").glob("*.json"))
    assert len(paths) == 1, paths
    return paths[0].read_bytes()


def fixture_handler(bodies: dict[str, bytes]) -> Callable[[httpx.Request], httpx.Response]:
    """Return a respx side effect that serves ``bodies`` by URL and 404s the rest."""

    def handle(request: httpx.Request) -> httpx.Response:
        body = bodies.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    return handle


# --- databases ----------------------------------------------------------------


def create_usd_db(path: Path, *, with_usd_table: bool = True) -> Path:
    """Create a database with one reported and one converted RY revenue row.

    The converted row's ``asof_ts`` is deliberately later than the filing's
    acceptance time, which is how a CAD amount waits for the FX rate that
    converted it; the reported row is known at acceptance.
    """
    con = duckdb.connect(str(path))
    try:
        con.execute("SET TimeZone='UTC'")
        con.execute(_FUNDAMENTALS_DDL)
        con.execute("CREATE TABLE main.events_8k (cik VARCHAR, ticker VARCHAR)")
        con.execute(
            "INSERT INTO main.fundamentals_pit VALUES "
            "(?, 'RY', 'revenue', ?, 'CAD', 'CAD', DATE '2022-11-01', DATE '2023-10-31', "
            "'FY', '0001193125-23-285639', '40-F', ?, 1, FALSE, FALSE, 'none')",
            [RY_CIK, RY_REPORTED, RY_ACCEPTANCE],
        )
        if with_usd_table:
            con.execute(_USD_DDL)
            con.execute(
                "INSERT INTO main.fundamentals_pit_usd VALUES "
                "(?, 'RY', 'revenue', ?, 'CAD', 'CAD', DATE '2022-11-01', DATE '2023-10-31', "
                "'FY', '0001193125-23-285639', '40-F', ?, 1, FALSE, FALSE, 'none', "
                "1.3871, DATE '2023-10-31', ?, ?, TRUE, NULL)",
                [RY_CIK, RY_USD, RY_ACCEPTANCE, RY_FX_KNOWN_AT, RY_FX_KNOWN_AT],
            )
    finally:
        con.close()
    return path


def create_events_db(path: Path) -> Path:
    """Create ``raw.events_8k`` with the fixture's Item 2.02 filings."""
    events = [
        (APPLE_CIK, APPLE_Q4_2025, "8-K", datetime(2025, 10, 30, 20, 30, 35, tzinfo=UTC)),
        (APPLE_CIK, APPLE_Q3_2026, "8-K", datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC)),
        (SHOP_CIK, SHOP_Q2_2026, "8-K", datetime(2026, 8, 5, 11, 1, 47, tzinfo=UTC)),
    ]
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute(duckdb_ddl("events_8k"))
        for cik, accession, form, acceptance in events:
            con.execute(
                "INSERT INTO raw.events_8k VALUES (?, ?, ?, ?, FALSE, '2.02', ?)",
                [
                    cik,
                    accession,
                    form,
                    acceptance,
                    "Results of Operations and Financial Condition",
                ],
            )
        # A second item on the same filing must not select it twice.
        con.execute(
            "INSERT INTO raw.events_8k VALUES (?, ?, '8-K', ?, FALSE, '9.01', ?)",
            [
                APPLE_CIK,
                APPLE_Q4_2025,
                datetime(2025, 10, 30, 20, 30, 35, tzinfo=UTC),
                "Financial Statements and Exhibits",
            ],
        )
    finally:
        con.close()
    return path


def create_empty_db(path: Path) -> Path:
    """Create an empty DuckDB file, for a command whose data layer is replaced."""
    con = duckdb.connect(str(path))
    con.close()
    return path


def write_universe(path: Path) -> Path:
    """Write a two-issuer universe CSV with resolved CIKs."""
    path.write_text(
        "ticker,cik,name,country,filer_type\n"
        "RY,0001000275,ROYAL BANK OF CANADA,CA,40-F\n"
        "AAPL,0000320193,Apple Inc.,US,10-K\n",
        encoding="utf-8",
    )
    return path


class StubClient:
    """A stand-in for :class:`edgarpit.sec_client.SecClient` used as a context manager."""

    def __enter__(self) -> StubClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


# --- fetch --incremental ------------------------------------------------------


def fetch_report(*, incremental: bool) -> FetchReport:
    """A two-issuer report; with ``incremental`` the second issuer is skipped."""
    report = FetchReport(
        elapsed_seconds=1.25,
        skipped_unchanged=1 if incremental else 0,
    )
    report.issuers.append(
        IssuerFetchReport(
            cik=APPLE_CIK,
            ticker="AAPL",
            submissions=1,
            companyfacts=1,
            headers_fetched=2,
            requests=4,
        )
    )
    report.issuers.append(
        IssuerFetchReport(
            cik=RY_CIK,
            ticker="RY",
            requests=1,
            unchanged=incremental,
        )
    )
    return report


@pytest.fixture
def fetch_call(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the fetch and its client, recording the ``incremental`` argument."""
    seen: dict[str, Any] = {}

    def fake_fetch(*args: object, **kwargs: Any) -> FetchReport:
        incremental = bool(kwargs.get("incremental"))
        seen["incremental"] = incremental
        seen["args"] = args
        return fetch_report(incremental=incremental)

    monkeypatch.setattr("edgarpit.cli.SecClient", lambda *args, **kwargs: StubClient())
    monkeypatch.setattr("edgarpit.cli.fetch_universe", fake_fetch)
    return seen


def test_incremental_fetch_passes_the_flag_and_reports_the_skips(
    tmp_path: Path, fetch_call: dict[str, Any]
) -> None:
    universe = write_universe(tmp_path / "universe.csv")
    result = RUNNER.invoke(
        app,
        ["fetch", "--universe", str(universe), "--out", str(tmp_path / "raw"), "--incremental"],
    )
    assert result.exit_code == 0, result.output
    assert fetch_call["incremental"] is True
    assert "skipped 1 unchanged" in result.stdout
    ry = next(line for line in result.stdout.splitlines() if line.startswith("RY"))
    assert "unchanged" in ry
    assert "summary: 2 issuers ok, 0 failed; 5 requests; 1.2s" in result.stdout


def test_a_full_fetch_keeps_the_previous_output(tmp_path: Path, fetch_call: dict[str, Any]) -> None:
    universe = write_universe(tmp_path / "universe.csv")
    result = RUNNER.invoke(
        app, ["fetch", "--universe", str(universe), "--out", str(tmp_path / "raw")]
    )
    assert result.exit_code == 0, result.output
    assert fetch_call["incremental"] is False
    assert "skipped" not in result.stdout
    ry = next(line for line in result.stdout.splitlines() if line.startswith("RY"))
    assert "ok" in ry and "unchanged" not in ry
    # The status column keeps its Phase 1 width, so the table is byte-identical.
    header = next(line for line in result.stdout.splitlines() if line.startswith("issuer"))
    assert header == (
        f"{'issuer':<8} {'cik':<10} {'status':<6} {'requests':>8} "
        f"{'headers':>7} {'cached':>6} {'404':>4}"
    )


# --- fetch-text ---------------------------------------------------------------


@pytest.fixture
def text_call(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``fetch_text_documents`` and set a valid User-Agent."""
    seen: dict[str, Any] = {}

    def fake_fetch_text(
        db: Path,
        data_dir: Path,
        items: list[str],
        *,
        limit: int | None = None,
        client: Any = None,
    ) -> TextFetchReport:
        seen["db"] = Path(db)
        seen["data"] = Path(data_dir)
        seen["items"] = list(items)
        seen["limit"] = limit
        seen["cache_root"] = None if client is None else Path(client.cache.root)
        return TextFetchReport(
            filings=2,
            indexes_fetched=1,
            documents_fetched=3,
            already_cached=4,
            failed=("0000320193-25-000077__missing.htm",),
        )

    monkeypatch.setenv("SEC_USER_AGENT", USER_AGENT)
    monkeypatch.setattr("edgarpit.cli.fetch_text_documents", fake_fetch_text)
    return seen


def test_fetch_text_passes_the_flags_and_a_client_cached_at_data(
    tmp_path: Path, text_call: dict[str, Any]
) -> None:
    db = create_empty_db(tmp_path / "edgarpit.duckdb")
    data = tmp_path / "cache"
    result = RUNNER.invoke(
        app,
        [
            "fetch-text",
            "--db",
            str(db),
            "--items",
            "2.02",
            "--items",
            "7.01",
            "--limit",
            "2",
            "--data",
            str(data),
        ],
    )
    assert result.exit_code == 0, result.output
    assert text_call == {
        "db": db,
        "data": data,
        "items": ["2.02", "7.01"],
        "limit": 2,
        "cache_root": data,
    }
    assert "filings selected: 2" in result.stdout
    assert "index pages fetched: 1" in result.stdout
    assert "documents fetched: 3" in result.stdout
    assert "already cached: 4" in result.stdout
    assert "failed: 1" in result.stdout
    assert "  not found: 0000320193-25-000077__missing.htm" in result.stderr
    assert f"raw cache: {data}" in result.stdout


def test_fetch_text_defaults_to_the_data_directory(
    tmp_path: Path, text_call: dict[str, Any]
) -> None:
    db = create_empty_db(tmp_path / "edgarpit.duckdb")
    result = RUNNER.invoke(app, ["fetch-text", "--db", str(db), "--items", "2.02"])
    assert result.exit_code == 0, result.output
    assert text_call["data"] == Path("data")
    assert text_call["cache_root"] == Path("data")
    assert text_call["limit"] is None


def test_fetch_text_without_the_events_table_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The selection's own message is the one a user needs to fix it."""
    db = create_empty_db(tmp_path / "edgarpit.duckdb")
    monkeypatch.setenv("SEC_USER_AGENT", USER_AGENT)
    result = RUNNER.invoke(app, ["fetch-text", "--db", str(db), "--items", "2.02"])
    assert result.exit_code == 1
    assert "raw.events_8k" in result.stderr
    assert "run `edgarpit build` first" in result.stderr


def test_fetch_text_usage_errors_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = create_empty_db(tmp_path / "edgarpit.duckdb")
    monkeypatch.setenv("SEC_USER_AGENT", USER_AGENT)
    missing_items = RUNNER.invoke(app, ["fetch-text", "--db", str(db)])
    negative_limit = RUNNER.invoke(
        app, ["fetch-text", "--db", str(db), "--items", "2.02", "--limit", "-1"]
    )
    missing_db = RUNNER.invoke(app, ["fetch-text", "--items", "2.02"])
    assert missing_items.exit_code == 2
    assert negative_limit.exit_code == 2
    assert "must be zero or positive" in negative_limit.stderr
    assert missing_db.exit_code == 2


def test_fetch_text_without_a_user_agent_exits_2(tmp_path: Path) -> None:
    db = create_empty_db(tmp_path / "edgarpit.duckdb")
    result = RUNNER.invoke(
        app, ["fetch-text", "--db", str(db), "--items", "2.02", "--data", str(tmp_path / "d")]
    )
    assert result.exit_code == 2
    assert "SEC_USER_AGENT" in result.stderr


def test_fetch_text_end_to_end_writes_the_cached_index_and_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real fetch path over the committed 8-K bytes, served through respx."""
    monkeypatch.setenv("SEC_USER_AGENT", USER_AGENT)
    db = create_events_db(tmp_path / "edgarpit.duckdb")
    data = tmp_path / "cache"
    bodies = text_fixture_bodies()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url__regex=r"https://www\.sec\.gov/Archives/edgar/data/.*").mock(
            side_effect=fixture_handler(bodies)
        )
        result = RUNNER.invoke(
            app,
            [
                "fetch-text",
                "--db",
                str(db),
                "--items",
                "2.02",
                "--limit",
                "1",
                "--data",
                str(data),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "filings selected: 1" in result.stdout
    assert "index pages fetched: 1" in result.stdout
    assert "documents fetched: 2" in result.stdout
    assert "already cached: 0" in result.stdout
    assert "failed: 0" in result.stdout

    cache = RawCache(data)
    assert cache.latest("filing_index", APPLE_CIK, APPLE_Q4_2025) is not None
    for filename in APPLE_Q4_DOCUMENTS:
        assert cache.latest("documents", APPLE_CIK, document_key(APPLE_Q4_2025, filename))
    requested = {str(call.request.url) for call in route.calls}
    assert len(requested) == 3
    assert all(url in bodies for url in requested)
    # Limit 1 selects the oldest Item 2.02 filing; nothing later is requested.
    assert not any(APPLE_Q3_2026 in url or SHOP_Q2_2026 in url for url in requested)


def test_fetch_text_twice_makes_no_second_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", USER_AGENT)
    db = create_events_db(tmp_path / "edgarpit.duckdb")
    data = tmp_path / "cache"
    bodies = text_fixture_bodies()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url__regex=r"https://www\.sec\.gov/Archives/edgar/data/.*").mock(
            side_effect=fixture_handler(bodies)
        )
        first = RUNNER.invoke(
            app,
            ["fetch-text", "--db", str(db), "--items", "2.02", "--limit", "1", "--data", str(data)],
        )
        calls = route.call_count
        second = RUNNER.invoke(
            app,
            ["fetch-text", "--db", str(db), "--items", "2.02", "--limit", "1", "--data", str(data)],
        )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "already cached: 3" in second.stdout
    assert "index pages fetched: 0" in second.stdout
    assert "documents fetched: 0" in second.stdout
    assert route.call_count == calls == 3


# --- fx -----------------------------------------------------------------------


def test_fx_prints_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_fetch_fx(data_dir: Path) -> FxFetchReport:
        seen["out"] = Path(data_dir)
        return FxFetchReport(
            series="FXUSDCAD",
            observations=2449,
            first_date=date(2017, 1, 3),
            last_date=date(2026, 10, 27),
            stored=False,
        )

    monkeypatch.setattr("edgarpit.cli.fetch_fx", fake_fetch_fx)
    out = tmp_path / "cache"
    result = RUNNER.invoke(app, ["fx", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert seen == {"out": out}
    assert "FXUSDCAD: 2449 observations 2017-01-03..2026-10-27" in result.stdout
    assert "stored: no (unchanged)" in result.stdout
    assert f"raw cache: {out}" in result.stdout


def test_fx_without_a_user_agent_exits_2(tmp_path: Path) -> None:
    result = RUNNER.invoke(app, ["fx", "--out", str(tmp_path / "cache")])
    assert result.exit_code == 2
    assert "SEC_USER_AGENT" in result.stderr


def test_fx_failure_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_fetch_fx(data_dir: Path) -> FxFetchReport:
        raise ValueError("Valet document does not describe series 'FXUSDCAD'")

    monkeypatch.setattr("edgarpit.cli.fetch_fx", failing_fetch_fx)
    result = RUNNER.invoke(app, ["fx", "--out", str(tmp_path / "cache")])
    assert result.exit_code == 1
    assert "error: Valet document does not describe series 'FXUSDCAD'" in result.stderr


def test_fx_end_to_end_stores_the_valet_series_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real Valet parse and cache write over the committed series, through respx."""
    monkeypatch.setenv("SEC_USER_AGENT", USER_AGENT)
    body = fx_fixture_body()
    document = json.loads(body)
    observations = document["observations"]
    first, last = observations[0]["d"], observations[-1]["d"]
    out = tmp_path / "cache"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url=valet_url("FXUSDCAD")).mock(
            return_value=httpx.Response(200, content=body)
        )
        first_run = RUNNER.invoke(app, ["fx", "--out", str(out)])

    assert first_run.exit_code == 0, first_run.output
    assert f"FXUSDCAD: {len(observations)} observations {first}..{last}" in first_run.stdout
    assert "stored: yes" in first_run.stdout
    cache = RawCache(out)
    assert cache.latest("fx", "", "FXUSDCAD") == body

    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url=valet_url("FXUSDCAD")).mock(
            return_value=httpx.Response(200, content=body)
        )
        second_run = RUNNER.invoke(app, ["fx", "--out", str(out)])
        assert route.call_count == 1

    assert second_run.exit_code == 0, second_run.output
    assert "stored: no (unchanged)" in second_run.stdout
    # Exactly one file per change: the repeat run added nothing.
    stored = list((out / "raw" / "fx" / "_" / "FXUSDCAD").glob("*"))
    assert len(stored) == 1


# --- asof --currency USD ------------------------------------------------------


def asof_args(db: Path, asof_ts: str, *extra: str) -> list[str]:
    """Return the argv of an ``asof`` query for RY revenue."""
    return [
        "asof",
        "--db",
        str(db),
        "--cik",
        RY_CIK,
        "--field",
        "revenue",
        "--asof",
        asof_ts,
        *extra,
    ]


def test_asof_usd_reads_the_converted_row(tmp_path: Path) -> None:
    db = create_usd_db(tmp_path / "edgarpit.duckdb")
    result = RUNNER.invoke(app, asof_args(db, "2024-01-03T00:00:00Z", "--currency", "USD"))
    assert result.exit_code == 0, result.output
    assert f"value: {RY_USD:.0f}" in result.stdout
    assert "currency: USD" in result.stdout
    assert "unit: USD" in result.stdout
    assert f"acceptance_ts: {RY_ACCEPTANCE.isoformat().replace('+00:00', 'Z')}" in result.stdout


def test_asof_usd_json_shows_the_converted_currency(tmp_path: Path) -> None:
    db = create_usd_db(tmp_path / "edgarpit.duckdb")
    result = RUNNER.invoke(
        app, asof_args(db, "2024-01-03T00:00:00Z", "--currency", "USD", "--json")
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    assert payload[0]["currency"] == "USD"
    assert payload[0]["unit"] == "USD"
    assert payload[0]["value"] == pytest.approx(RY_USD)


def test_asof_usd_waits_for_the_fx_known_time(tmp_path: Path) -> None:
    """A CAD value converted by a rate published later is not known before that rate."""
    db = create_usd_db(tmp_path / "edgarpit.duckdb")
    # The filing is known at RY_ACCEPTANCE, so the reported read succeeds...
    reported = RUNNER.invoke(app, asof_args(db, "2023-12-01T00:00:00Z"))
    assert reported.exit_code == 0, reported.output
    assert f"value: {RY_REPORTED:.0f}" in reported.stdout
    assert "currency: CAD" in reported.stdout
    # ...but the USD read is not known until the rate's known time.
    converted = RUNNER.invoke(app, asof_args(db, "2023-12-01T00:00:00Z", "--currency", "USD"))
    assert converted.exit_code == 1
    assert "no 'revenue' value known" in converted.stderr


def test_asof_usd_without_the_conversion_model_exits_1(tmp_path: Path) -> None:
    db = create_usd_db(tmp_path / "edgarpit.duckdb", with_usd_table=False)
    result = RUNNER.invoke(app, asof_args(db, "2024-01-03T00:00:00Z", "--currency", "USD"))
    assert result.exit_code == 1
    assert "main.fundamentals_pit_usd is missing" in result.stderr
    assert "Phase 2 FX model" in result.stderr


def test_asof_rejects_another_currency(tmp_path: Path) -> None:
    db = create_usd_db(tmp_path / "edgarpit.duckdb")
    result = RUNNER.invoke(app, asof_args(db, "2024-01-03T00:00:00Z", "--currency", "EUR"))
    assert result.exit_code == 2
    assert "--currency must be USD" in result.stderr


# --- qa -----------------------------------------------------------------------


def qa_report(*, asof_violations: int = 0) -> QaReport:
    """A report with every Phase 2 section built and the given as-of leak count."""
    violations = [
        AsOfViolation(
            cik=RY_CIK,
            field="revenue",
            period_start=date(2022, 11, 1),
            period_end=date(2023, 10, 31),
            accession="0001193125-23-285639",
            acceptance_ts=RY_ACCEPTANCE,
            fx_known_at=RY_FX_KNOWN_AT,
            asof_ts=RY_ACCEPTANCE,
        )
        for _ in range(asof_violations)
    ]
    return QaReport(
        canonical_fields=8,
        universe_issuers=2,
        fx_coverage=FxCoverage(built=True),
        usd_conversion=UsdConversion(
            built=True,
            asof_violation_count=asof_violations,
            asof_violations=violations,
        ),
        text_coverage=TextCoverage(built=True),
    )


def test_qa_prints_the_phase2_sections_after_the_phase1_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = create_empty_db(tmp_path / "edgarpit.duckdb")
    monkeypatch.setattr("edgarpit.cli.run_qa", lambda con: qa_report())
    result = RUNNER.invoke(app, ["qa", "--db", str(db)])
    assert result.exit_code == 0, result.output
    for section in (
        "Coverage",
        "Key uniqueness: OK",
        "FX daily (raw.fx_daily)",
        "USD conversion (main.fundamentals_pit_usd)",
        "8-K text (raw.filings_text)",
    ):
        assert section in result.stdout, section
    assert result.stdout.index("Key uniqueness: OK") < result.stdout.index(
        "FX daily (raw.fx_daily)"
    )
    assert "Asof_ts violations (asof_ts earlier than acceptance_ts or fx_known_at): 0" in (
        result.stdout
    )


def test_qa_asof_violation_exits_non_zero_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = create_empty_db(tmp_path / "edgarpit.duckdb")
    monkeypatch.setattr("edgarpit.cli.run_qa", lambda con: qa_report(asof_violations=1))
    result = RUNNER.invoke(app, ["qa", "--db", str(db)])
    assert result.exit_code == 1
    assert "Asof_ts violations (asof_ts earlier than acceptance_ts or fx_known_at): 1" in (
        result.stdout
    )
    assert f"{RY_CIK} revenue 2022-11-01..2023-10-31 0001193125-23-285639" in result.stdout
    assert "1 asof_ts violations in main.fundamentals_pit_usd" in result.stderr
    assert "asof_ts before acceptance_ts or fx_known_at" in result.stderr
