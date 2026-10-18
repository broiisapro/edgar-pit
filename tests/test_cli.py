"""Tests for the ``edgarpit`` command line (PRD section 12).

Commands whose implementation lives in another workstream (``build``, ``asof``)
are exercised with the function monkeypatched, except the one real
``asof --json`` test, which runs against a minimal hand-built database (the
column contract of ``main.fundamentals_pit`` that ``query.asof`` reads).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

from edgarpit.cli import app
from edgarpit.fetch import FetchReport, IssuerFetchReport

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "sec"
EXPECTED_ASOF = Path(__file__).parent / "fixtures" / "expected_asof.json"
UNIVERSE_PATH = REPO_ROOT / "config" / "universe.csv"
HEADER = "ticker,cik,name,country,filer_type"
RUNNER = CliRunner()
ASOF_JSON_KEYS = {
    "cik",
    "ticker",
    "field",
    "asof",
    "period",
    "value",
    "unit",
    "currency",
    "period_start",
    "period_end",
    "period_type",
    "accession",
    "form",
    "acceptance_ts",
    "version",
    "is_restatement",
    "ts_imputed",
}


def write_universe(path: Path, rows: list[tuple[str, ...]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "\n" + "".join(",".join(row) + "\n" for row in rows), encoding="utf-8")
    return path


def create_asof_db(path: Path) -> Path:
    """A minimal ``main.fundamentals_pit`` with the expected_asof.json row."""
    expected = json.loads(EXPECTED_ASOF.read_text(encoding="utf-8"))
    con = duckdb.connect(str(path))
    try:
        con.execute("SET TimeZone='UTC'")
        con.execute(
            """
            CREATE TABLE main.fundamentals_pit (
                cik VARCHAR, ticker VARCHAR, field VARCHAR, value DOUBLE, unit VARCHAR,
                currency VARCHAR, period_start DATE, period_end DATE, period_type VARCHAR,
                accession VARCHAR, form VARCHAR, acceptance_ts TIMESTAMPTZ,
                version INTEGER, is_restatement BOOLEAN, ts_imputed BOOLEAN,
                restatement_kind VARCHAR
            )
            """
        )
        # query._resolve_cik reads the issuer union, so an empty events_8k must exist.
        con.execute("CREATE TABLE main.events_8k (cik VARCHAR, ticker VARCHAR)")
        con.execute(
            "INSERT INTO main.fundamentals_pit VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                expected["cik"],
                "RY",
                expected["field"],
                float(expected["value"]),
                expected["unit"],
                expected["currency"],
                date.fromisoformat(expected["period_start"]),
                date.fromisoformat(expected["period_end"]),
                expected["period"],
                expected["accession"],
                "40-F",
                datetime.fromisoformat(expected["acceptance_ts"].replace("Z", "+00:00")),
                1,
                False,
                False,
                "none",
            ],
        )
    finally:
        con.close()
    return path


def test_help_lists_the_commands() -> None:
    result = RUNNER.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("universe", "fetch", "build", "asof"):
        assert command in result.stdout


def test_unknown_command_is_a_usage_error() -> None:
    result = RUNNER.invoke(app, ["nope"])
    assert result.exit_code == 2


def test_asof_json_matches_expected_asof(tmp_path: Path) -> None:
    db = create_asof_db(tmp_path / "edgarpit.duckdb")
    expected = json.loads(EXPECTED_ASOF.read_text(encoding="utf-8"))
    result = RUNNER.invoke(
        app,
        [
            "asof",
            "--db",
            str(db),
            "--cik",
            expected["cik"],
            "--field",
            expected["field"],
            "--asof",
            expected["asof"],
            "--period",
            expected["period"],
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and len(payload) == 1
    row = payload[0]
    assert set(row) == ASOF_JSON_KEYS
    for key, value in expected.items():
        if isinstance(value, float):
            assert row[key] == pytest.approx(value), key
        else:
            assert row[key] == value, key
    assert row["ticker"] == "RY"
    assert row["period_type"] == "FY"
    assert row["form"] == "40-F"
    assert row["version"] == 1
    assert row["is_restatement"] is False
    assert row["ts_imputed"] is False


def test_asof_text_output_prints_the_required_fields(tmp_path: Path) -> None:
    db = create_asof_db(tmp_path / "edgarpit.duckdb")
    result = RUNNER.invoke(
        app,
        [
            "asof",
            "--db",
            str(db),
            "--ticker",
            "ry",
            "--field",
            "revenue",
            "--asof",
            "2024-01-15T00:00:00Z",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "value: 56129000000" in result.stdout
    assert "currency: CAD" in result.stdout
    assert "unit: CAD" in result.stdout
    assert "period_start: 2022-11-01" in result.stdout
    assert "period_end: 2023-10-31" in result.stdout
    assert "accession: 0001193125-23-285639" in result.stdout
    assert "acceptance_ts: 2023-11-30T11:45:12Z" in result.stdout


def test_asof_without_a_known_value_exits_1(tmp_path: Path) -> None:
    db = create_asof_db(tmp_path / "edgarpit.duckdb")
    result = RUNNER.invoke(
        app,
        [
            "asof",
            "--db",
            str(db),
            "--cik",
            "0001000275",
            "--field",
            "revenue",
            "--asof",
            "2023-11-30T11:45:11Z",
        ],
    )
    assert result.exit_code == 1
    assert "no 'revenue' value known" in result.stderr
    assert len([line for line in result.stderr.splitlines() if line.strip()]) == 1


def test_asof_needs_exactly_one_identifier(tmp_path: Path) -> None:
    db = create_asof_db(tmp_path / "edgarpit.duckdb")
    base = ["asof", "--db", str(db), "--field", "revenue", "--asof", "2024-01-15T00:00:00Z"]
    assert RUNNER.invoke(app, base).exit_code == 2
    both = [*base, "--cik", "0001000275", "--ticker", "RY"]
    assert RUNNER.invoke(app, both).exit_code == 2


def test_asof_rejects_a_naive_timestamp_and_a_bad_period(tmp_path: Path) -> None:
    db = create_asof_db(tmp_path / "edgarpit.duckdb")
    base = ["asof", "--db", str(db), "--cik", "0001000275", "--field", "revenue"]
    naive = [*base, "--asof", "2024-01-15"]
    assert RUNNER.invoke(app, naive).exit_code == 2
    bad_period = [*base, "--asof", "2024-01-15T00:00:00Z", "--period", "M"]
    assert RUNNER.invoke(app, bad_period).exit_code == 2


def test_asof_with_an_unreadable_database_exits_1(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        [
            "asof",
            "--db",
            str(tmp_path / "absent.duckdb"),
            "--cik",
            "0001000275",
            "--field",
            "revenue",
            "--asof",
            "2024-01-15T00:00:00Z",
        ],
    )
    assert result.exit_code == 1
    assert result.stderr.startswith("error: ")


def test_universe_resolve_offline_reads_the_fixture_cache() -> None:
    result = RUNNER.invoke(
        app,
        ["universe", "resolve", "--offline", "--fixtures", str(FIXTURES)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "resolved 40 issuers: 0 filled, 0 disagreements, 0 unresolved" in result.stdout


def test_universe_resolve_write_keeps_a_clean_file_identical(tmp_path: Path) -> None:
    path = write_universe(
        tmp_path / "u.csv",
        [
            ("RY", "0001000275", "ROYAL BANK OF CANADA", "CA", "40-F"),
            ("AAPL", "0000320193", "Apple Inc.", "US", "10-K"),
        ],
    )
    before = path.read_bytes()
    result = RUNNER.invoke(
        app,
        [
            "universe",
            "resolve",
            "--universe",
            str(path),
            "--offline",
            "--fixtures",
            str(FIXTURES),
            "--write",
        ],
    )
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == before


def test_universe_resolve_reports_problems_and_never_writes(tmp_path: Path) -> None:
    path = write_universe(
        tmp_path / "u.csv",
        [
            ("AAPL", "0000000001", "Apple Inc.", "US", "10-K"),
            ("ZZZZ", "0000000002", "Nowhere Inc.", "US", "10-K"),
        ],
    )
    before = path.read_bytes()
    result = RUNNER.invoke(
        app,
        ["universe", "resolve", "--universe", str(path), "--offline", "--fixtures", str(FIXTURES)],
    )
    assert result.exit_code == 1
    assert "disagreement: AAPL has CIK 0000000001 but SEC maps it to 0000320193" in result.stderr
    assert "unresolved ticker: ZZZZ" in result.stderr
    assert "0 filled, 1 disagreements, 1 unresolved" in result.stdout

    written = RUNNER.invoke(
        app,
        [
            "universe",
            "resolve",
            "--universe",
            str(path),
            "--offline",
            "--fixtures",
            str(FIXTURES),
            "--write",
        ],
    )
    assert written.exit_code == 1
    assert path.read_bytes() == before


def test_universe_resolve_write_fills_a_missing_cik(tmp_path: Path) -> None:
    path = write_universe(
        tmp_path / "u.csv",
        [("SHOP", "", "Shopify Inc.", "CA", "10-K")],
    )
    result = RUNNER.invoke(
        app,
        [
            "universe",
            "resolve",
            "--universe",
            str(path),
            "--offline",
            "--fixtures",
            str(FIXTURES),
            "--write",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "filled SHOP -> 0001594805" in result.stdout
    assert "SHOP,0001594805,Shopify Inc.,CA,10-K" in path.read_text(encoding="utf-8")


def test_universe_resolve_needs_a_source() -> None:
    assert RUNNER.invoke(app, ["universe", "resolve"]).exit_code == 2
    assert RUNNER.invoke(app, ["universe", "resolve", "--offline"]).exit_code == 2
    both = ["universe", "resolve", "--offline", "--fixtures", str(FIXTURES), "--data", "x"]
    assert RUNNER.invoke(app, both).exit_code == 2


def test_universe_resolve_offline_miss_is_a_data_failure(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        [
            "universe",
            "resolve",
            "--universe",
            str(UNIVERSE_PATH),
            "--offline",
            "--fixtures",
            str(tmp_path / "empty"),
        ],
    )
    assert result.exit_code == 1
    assert result.stderr.startswith("error: ")


def test_fetch_reports_a_missing_cik_before_any_request(tmp_path: Path) -> None:
    path = write_universe(tmp_path / "u.csv", [("SHOP", "", "Shopify Inc.", "CA", "10-K")])
    result = RUNNER.invoke(app, ["fetch", "--universe", str(path), "--out", str(tmp_path / "raw")])
    assert result.exit_code == 1
    assert "issuers without a CIK: SHOP" in result.stderr


class _StubClient:
    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def stub_fetch(*args: object, **kwargs: object) -> FetchReport:
    report = FetchReport(elapsed_seconds=0.5)
    report.issuers.append(
        IssuerFetchReport(
            cik="0001000275",
            ticker="RY",
            submissions=1,
            companyfacts=1,
            headers_fetched=2,
            requests=4,
        )
    )
    report.issuers.append(
        IssuerFetchReport(
            cik="0000320193",
            ticker="AAPL",
            submissions=1,
            companyfacts=1,
            headers_fetched=2,
            headers_404=["0000320193-24-000001"],
            requests=5,
        )
    )
    return report


def test_fetch_with_a_monkeypatched_fetch_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_universe(
        tmp_path / "u.csv",
        [
            ("RY", "0001000275", "ROYAL BANK OF CANADA", "CA", "40-F"),
            ("AAPL", "0000320193", "Apple Inc.", "US", "10-K"),
        ],
    )
    monkeypatch.setattr("edgarpit.cli.SecClient", lambda *args, **kwargs: _StubClient())
    monkeypatch.setattr("edgarpit.cli.fetch_universe", stub_fetch)
    result = RUNNER.invoke(app, ["fetch", "--universe", str(path), "--out", str(tmp_path / "raw")])
    assert result.exit_code == 0, result.output
    assert "RY" in result.stdout and "0001000275" in result.stdout
    assert "AAPL" in result.stdout and "0000320193" in result.stdout
    assert (
        "summary: 2 issuers ok, 0 failed; 9 requests; 0.5s; "
        "headers 4 fetched, 0 cached, 1 not found" in result.stdout
    )
    assert f"raw cache: {tmp_path / 'raw'}" in result.stdout


def test_fetch_with_errors_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_universe(tmp_path / "u.csv", [("RY", "0001000275", "RY", "CA", "40-F")])

    def failing_fetch(*args: object, **kwargs: object) -> FetchReport:
        report = FetchReport()
        report.issuers.append(IssuerFetchReport(cik="0001000275", ticker="RY", errors=["HTTP 503"]))
        return report

    monkeypatch.setattr("edgarpit.cli.SecClient", lambda *args, **kwargs: _StubClient())
    monkeypatch.setattr("edgarpit.cli.fetch_universe", failing_fetch)
    result = RUNNER.invoke(app, ["fetch", "--universe", str(path), "--out", str(tmp_path / "raw")])
    assert result.exit_code == 1
    assert "error: 0001000275: HTTP 503" in result.stderr
    assert "summary: 0 issuers ok, 1 failed" in result.stdout
    row = next(line for line in result.stdout.splitlines() if line.startswith("RY"))
    assert "0001000275" in row and "failed" in row


def test_build_calls_build_from_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_build(raw_dir: Path, out_dir: Path) -> Path:
        calls["raw_dir"] = raw_dir
        calls["out_dir"] = out_dir
        return out_dir / "edgarpit.duckdb"

    monkeypatch.setattr("edgarpit.cli.build_from_raw", fake_build)
    fixtures = tmp_path / "fixtures"
    out = tmp_path / "out"
    result = RUNNER.invoke(
        app, ["build", "--offline", "--fixtures", str(fixtures), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert calls == {"raw_dir": fixtures, "out_dir": out}
    assert f"database: {out / 'edgarpit.duckdb'}" in result.stdout

    data_result = RUNNER.invoke(app, ["build", "--data", str(fixtures), "--out", str(out)])
    assert data_result.exit_code == 0, data_result.output


def test_build_failure_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_build(raw_dir: Path, out_dir: Path) -> Path:
        raise RuntimeError("dbt build failed")

    monkeypatch.setattr("edgarpit.cli.build_from_raw", failing_build)
    result = RUNNER.invoke(app, ["build", "--data", str(tmp_path), "--out", str(tmp_path / "out")])
    assert result.exit_code == 1
    assert "error: RuntimeError: dbt build failed" in result.stderr


def test_build_needs_a_source() -> None:
    assert RUNNER.invoke(app, ["build", "--out", "/tmp/nowhere"]).exit_code == 2
    assert RUNNER.invoke(app, ["build", "--offline", "--out", "/tmp/nowhere"]).exit_code == 2
