"""Live smoke test over two real issuers (#70).

Fetches a 40-F issuer and a 10-K issuer from SEC into a temporary raw cache
with the configured ``SEC_USER_AGENT``, then drives the installed CLI in
subprocesses -- ``edgarpit build --data`` and ``edgarpit qa`` -- the way the
Phase 1 definition of done does, and checks that QA exits 0 (every contract
key is unique). Excluded from CI: it calls live SEC endpoints under the
project's fair-access rate limit (tests never hit the network unless run with
``-m network``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from edgarpit.config import Settings
from edgarpit.fetch import fetch_universe
from edgarpit.rawcache import RawCache
from edgarpit.sec_client import SecClient
from edgarpit.universe import load_universe

pytestmark = pytest.mark.network

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = REPO_ROOT / "config" / "universe.csv"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``python -m edgarpit.cli ARGS`` from the repository root."""
    return subprocess.run(
        [sys.executable, "-m", "edgarpit.cli", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_live_fetch_and_build_two_issuers(tmp_path: Path) -> None:
    by_ticker = {issuer.ticker: issuer for issuer in load_universe(UNIVERSE)}
    issuers = [by_ticker["RY"], by_ticker["SHOP"]]  # a 40-F and a 10-K filer

    with SecClient(Settings(), cache=RawCache(tmp_path)) as client:
        report = fetch_universe(issuers, tmp_path, client=client)
    assert report.ok, report.errors
    assert report.headers_fetched > 0

    out_dir = tmp_path / "build"
    build = run_cli("build", "--data", str(tmp_path), "--out", str(out_dir))
    assert build.returncode == 0, f"{build.stdout}\n{build.stderr}"

    qa = run_cli("qa", "--db", str(out_dir / "edgarpit.duckdb"))
    assert qa.returncode == 0, f"{qa.stdout}\n{qa.stderr}"
    assert "Key uniqueness: OK" in qa.stdout
