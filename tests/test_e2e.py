"""End-to-end tests of the Phase 1 offline path (issue #63).

Every test drives the installed CLI through :mod:`subprocess` against
``tests/fixtures/sec``, exactly as the Phase 1 definition of done does, and
asserts on the Royal Bank reference values and invariants only: other fixture
values may change without touching this module.

Two independent builds are used deliberately. ``built`` runs
``edgarpit build --offline``; ``loaded`` runs ``python -m edgarpit.load``, the
same :func:`edgarpit.load.build_from_raw` path through its module entry point,
which also prints the per-table row counts. The pair feeds the export
determinism check and keeps each build out of the other's way.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from api.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "sec"
EXPECTED_ASOF = Path(__file__).parent / "fixtures" / "expected_asof.json"
EXPLORER = REPO_ROOT / "explorer" / "app.py"

RY_CIK = "0001000275"
RY_ORIGINAL_ACCESSION = "0001193125-23-285639"
RY_RESTATED_ACCESSION = "0001193125-24-270294"
RY_FY2023_REVENUE = 56_129_000_000
RY_FY2023_RESTATED_REVENUE = 51_464_000_000

pytestmark = pytest.mark.e2e


@dataclass(frozen=True)
class Build:
    """One built database and the stdout of the command that built it."""

    out_dir: Path
    stdout: str

    @property
    def db(self) -> Path:
        return self.out_dir / "edgarpit.duckdb"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``python -m edgarpit.cli ARGS`` from the repository root.

    The subprocess uses the interpreter running the tests, so it always sees
    the project environment; the offline commands need no environment
    variables.
    """
    return subprocess.run(
        [sys.executable, "-m", "edgarpit.cli", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Build:
    """The CLI build: ``edgarpit build --offline --fixtures tests/fixtures/sec``."""
    out_dir = tmp_path_factory.mktemp("cli-build")
    result = run_cli("build", "--offline", "--fixtures", str(FIXTURES), "--out", str(out_dir))
    assert result.returncode == 0, f"build failed:\n{result.stdout}\n{result.stderr}"
    return Build(out_dir=out_dir, stdout=result.stdout)


@pytest.fixture(scope="module")
def loaded(tmp_path_factory: pytest.TempPathFactory) -> Build:
    """The module build: ``python -m edgarpit.load``, the second independent build."""
    out_dir = tmp_path_factory.mktemp("module-build")
    result = subprocess.run(
        [sys.executable, "-m", "edgarpit.load", "--raw", str(FIXTURES), "--out", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"build failed:\n{result.stdout}\n{result.stderr}"
    return Build(out_dir=out_dir, stdout=result.stdout)


def query_rows(db: Path, sql: str, parameters: list[object] | None = None) -> list[tuple[Any, ...]]:
    """Run one read-only, UTC-session query against ``db`` and return all rows."""
    con = duckdb.connect(str(db), read_only=True)
    try:
        con.execute("SET TimeZone='UTC'")
        return con.execute(sql, parameters or []).fetchall()
    finally:
        con.close()


def iso_z(value: datetime) -> str:
    """Format an aware UTC datetime as ISO-8601 with a ``Z`` suffix."""
    return value.isoformat().replace("+00:00", "Z")


def asof_cli(
    db: Path, asof_ts: str, *, cik: str = RY_CIK, field: str = "revenue", n: int = 1
) -> list[dict[str, object]]:
    """Run ``asof --json`` and return the result objects, newest period first."""
    result = run_cli(
        "asof",
        "--db",
        str(db),
        "--cik",
        cik,
        "--field",
        field,
        "--asof",
        asof_ts,
        "--period",
        "FY",
        "--n",
        str(n),
        "--json",
    )
    assert result.returncode == 0, f"asof failed:\n{result.stdout}\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and len(payload) == n
    rows: list[dict[str, object]] = payload
    return rows


def test_build_exits_zero_and_reports_the_database(built: Build) -> None:
    assert built.db.is_file()
    assert "database: " in built.stdout


def test_module_build_prints_the_table_counts(loaded: Build) -> None:
    """``python -m edgarpit.load`` is the build entry point that prints row counts.

    The ``edgarpit build`` command prints the database path and the dbt summary;
    the per-table counts are printed by this module entry point over the same
    ``build_from_raw`` path.
    """
    for table in ("raw.filings", "raw.events_8k", "raw.facts_raw", "raw.concept_map"):
        match = re.search(rf"^{re.escape(table)}: (\d+) rows$", loaded.stdout, re.MULTILINE)
        assert match is not None, f"no row-count line for {table}:\n{loaded.stdout}"
        assert int(match.group(1)) > 0, table
    assert "dbt build: PASS=" in loaded.stdout
    assert "ERROR=0" in loaded.stdout
    assert f"database: {loaded.db}" in loaded.stdout


def test_asof_json_reproduces_expected_asof(built: Build) -> None:
    expected = json.loads(EXPECTED_ASOF.read_text(encoding="utf-8"))
    row = asof_cli(built.db, expected["asof"], cik=expected["cik"], field=expected["field"])[0]
    for key, value in expected.items():
        assert row[key] == value, key


def test_restatement_boundary_is_exact(built: Build) -> None:
    """The restated FY2023 value becomes known exactly at the restating 40-F's acceptance.

    The restating filing carries two annual periods at once (FY2023 restated and
    the new FY2024), and ``asof`` returns periods newest first, so the test asks
    for two periods and picks the FY2023 row. Both boundary instants are derived
    from the built database: the restating row's ``acceptance_ts`` and one second
    before it.
    """
    expected = json.loads(EXPECTED_ASOF.read_text(encoding="utf-8"))
    rows = query_rows(
        built.db,
        """
        select acceptance_ts, period_end
        from main.fundamentals_pit
        where cik = ? and field = 'revenue' and accession = ?
        order by period_end desc
        """,
        [RY_CIK, RY_RESTATED_ACCESSION],
    )
    assert len(rows) >= 2
    fy2023_rows = [row for row in rows if row[1].isoformat() == expected["period_end"]]
    assert len(fy2023_rows) == 1
    at = fy2023_rows[0][0]
    assert isinstance(at, datetime)
    before = at - timedelta(seconds=1)
    new_fiscal_year_end = max(row[1] for row in rows)
    assert new_fiscal_year_end > fy2023_rows[0][1]

    before_rows = asof_cli(built.db, iso_z(before), n=2)
    at_rows = asof_cli(built.db, iso_z(at), n=2)

    before_fy2023 = next(row for row in before_rows if row["period_end"] == expected["period_end"])
    at_fy2023 = next(row for row in at_rows if row["period_end"] == expected["period_end"])
    assert before_fy2023["value"] == RY_FY2023_REVENUE
    assert before_fy2023["accession"] == RY_ORIGINAL_ACCESSION
    assert at_fy2023["value"] == RY_FY2023_RESTATED_REVENUE
    assert at_fy2023["accession"] == RY_RESTATED_ACCESSION

    # The same filing starts the next fiscal year; that must not be known a
    # second earlier, and it must appear exactly at the acceptance instant.
    new_period = new_fiscal_year_end.isoformat()
    assert all(row["period_end"] != new_period for row in before_rows)
    assert at_rows[0]["period_end"] == new_period
    assert at_rows[0]["accession"] == RY_RESTATED_ACCESSION


def test_qa_passes_and_flags_a_duplicated_key(built: Build, tmp_path: Path) -> None:
    good = run_cli("qa", "--db", str(built.db))
    assert good.returncode == 0, f"qa failed:\n{good.stdout}\n{good.stderr}"
    assert "Key uniqueness: OK" in good.stdout

    # The copy keeps the file name: dbt's views name the catalog explicitly, so
    # a differently named copy would not resolve them. fundamentals_pit is a
    # dbt view, hence the rename before the duplicate row is inserted.
    copy = tmp_path / "edgarpit.duckdb"
    shutil.copyfile(built.db, copy)
    con = duckdb.connect(str(copy))
    try:
        con.execute(
            "CREATE TABLE main.fundamentals_pit_copy AS SELECT * FROM main.fundamentals_pit"
        )
        con.execute("ALTER VIEW main.fundamentals_pit RENAME TO fundamentals_pit_original")
        con.execute(
            "CREATE TABLE main.fundamentals_pit AS SELECT * FROM main.fundamentals_pit_copy"
        )
        con.execute(
            "INSERT INTO main.fundamentals_pit SELECT * FROM main.fundamentals_pit_copy LIMIT 1"
        )
    finally:
        con.close()

    bad = run_cli("qa", "--db", str(copy))
    assert bad.returncode == 1
    assert "Key uniqueness: violated" in bad.stdout
    assert "key uniqueness violated" in bad.stderr


def test_export_from_independent_builds_is_byte_identical(
    built: Build, loaded: Build, tmp_path: Path
) -> None:
    first = tmp_path / "release-cli"
    second = tmp_path / "release-module"
    one = run_cli("export", "--db", str(built.db), "--out", str(first))
    two = run_cli("export", "--db", str(loaded.db), "--out", str(second))
    assert one.returncode == 0, f"export failed:\n{one.stdout}\n{one.stderr}"
    assert two.returncode == 0, f"export failed:\n{two.stdout}\n{two.stderr}"

    first_files = sorted(path.name for path in first.glob("*.parquet"))
    second_files = sorted(path.name for path in second.glob("*.parquet"))
    assert first_files and first_files == second_files
    for name in first_files:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name

    manifest_a = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    manifest_b = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_a.pop("build_timestamp") is not None
    assert manifest_b.pop("build_timestamp") is not None
    assert manifest_a == manifest_b


def test_universe_resolve_offline_exits_zero() -> None:
    result = run_cli("universe", "resolve", "--offline", "--fixtures", str(FIXTURES))
    assert result.returncode == 0, f"resolve failed:\n{result.stdout}\n{result.stderr}"
    assert "resolved 40 issuers: 0 filled, 0 disagreements, 0 unresolved" in result.stdout


def test_api_asof_returns_the_ry_accession(built: Build, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = json.loads(EXPECTED_ASOF.read_text(encoding="utf-8"))
    monkeypatch.setenv("EDGARPIT_DB", str(built.db))
    with TestClient(app) as client:
        response = client.get(
            "/asof",
            params={
                "cik": RY_CIK,
                "field": "revenue",
                "asof": expected["asof"],
                "period": "FY",
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["accession"] == RY_ORIGINAL_ACCESSION


def test_explorer_runs_against_the_built_database(
    built: Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EDGARPIT_DB", str(built.db))
    app_test = AppTest.from_file(str(EXPLORER), default_timeout=60)
    app_test.run()
    assert not app_test.exception

    issuer_box = app_test.sidebar.selectbox(key="issuer")
    ry_option = next(option for option in issuer_box.options if option.startswith("RY"))
    issuer_box.set_value(ry_option)
    app_test.run()
    assert not app_test.exception
