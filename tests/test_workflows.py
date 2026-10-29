"""Checks for the scheduled refresh and data-release workflows (issue #118).

These two workflows are the only code here that runs unattended with a real
credential, on a public repository, against SEC endpoints. The tests pin what a
reviewer would otherwise have to trust by reading YAML: which events start a
run, the enable switch, least-privilege permissions, action pins, timeouts, the
single concurrency lane both files share, the recorded command sequence, where
the SEC User-Agent is passed, and the full asset list a release publishes.

Nothing is executed and nothing touches the network: the files are parsed as
YAML (for structure) and as text (for pins and comments).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from edgarpit import __version__
from edgarpit.export import EXPORTS, PHASE2_EXPORTS

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
REFRESH_PATH = WORKFLOW_DIR / "refresh.yml"
RELEASE_PATH = WORKFLOW_DIR / "release.yml"
ROOT = WORKFLOW_DIR.parents[1]
PATHS = pytest.mark.parametrize("path", [REFRESH_PATH, RELEASE_PATH], ids=["refresh", "release"])

# The repository variable that turns the scheduled workflows on. Both workflows
# are inert until its value is exactly this string.
GATE = "vars.REFRESH_ENABLED == 'true'"

# 06:00-09:00 US Pacific in minutes after local midnight: the window the nightly
# refresh must not land in, whatever the daylight-saving offset is.
PACIFIC_MORNING = range(6 * 60, 9 * 60)

SECRET = "secrets.SEC_USER_AGENT"

# Every command line the refresh workflow must run, in this order. ``uv run`` is
# stripped before matching; the QA line may also store its report.
REFRESH_COMMANDS: tuple[str, ...] = (
    r"^edgarpit fetch --universe config/universe_large\.csv --out data --incremental$",
    r"^edgarpit build --data data --out build$",
    r"^edgarpit fetch-text --db build/edgarpit\.duckdb --items 2\.02 --data data$",
    r"^edgarpit fx --out data$",
    r"^edgarpit build --data data --out build$",
    r"^edgarpit qa --db build/edgarpit\.duckdb( \| tee build/qa\.txt)?$",
)

RELEASE_COMMANDS: tuple[str, ...] = (
    r"^edgarpit fetch --universe config/universe_large\.csv --out data$",
    r"^edgarpit build --data data --out build$",
    r"^edgarpit qa --db build/edgarpit\.duckdb( \| tee build/qa\.txt)?$",
    r"^edgarpit export --db build/edgarpit\.duckdb --out release$",
)

# Every step that receives the SEC User-Agent, by step name.
SECRET_RECEIVERS = frozenset(
    {"Check SEC_USER_AGENT", "Fetch (incremental)", "Fetch filing text", "Fetch FX rates"}
)
RELEASE_SECRET_RECEIVERS = frozenset({"Check SEC_USER_AGENT", "Fetch (full)"})


def load(path: Path) -> dict[str, Any]:
    """Parse one workflow file as YAML and return the top-level mapping."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} is not a mapping"
    return document


def events(path: Path) -> dict[str, Any]:
    """Return the ``on`` block; PyYAML reads that key as ``True`` under YAML 1.1."""
    document = load(path)
    event = document.get("on", document.get(True))
    assert isinstance(event, dict), f"{path.name} has no event mapping"
    return event


def jobs(path: Path) -> dict[str, Any]:
    """Return the ``jobs`` mapping of one workflow."""
    value = load(path)["jobs"]
    assert isinstance(value, dict), f"{path.name} has no jobs mapping"
    return value


def steps(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(job name, step)`` for every step, in file order."""
    for job_name, job in jobs(path).items():
        for step in job.get("steps", []):
            yield job_name, step


def run_blocks(path: Path) -> Iterator[tuple[str, dict[str, Any], str]]:
    """Yield ``(job name, step, run script)`` for every step that runs a command."""
    for job_name, step in steps(path):
        run = step.get("run")
        if isinstance(run, str):
            yield job_name, step, run


def gated(document: dict[str, Any], name: str) -> bool:
    """Whether a job, or every job it needs, carries the enable switch."""
    job = document["jobs"][name]
    if GATE in str(job.get("if", "")):
        return True
    needs = job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    return bool(needs) and all(gated(document, need) for need in needs)


def cron_fields(path: Path) -> list[list[str]]:
    """Return the five fields of each schedule entry in one workflow."""
    entries = events(path)["schedule"]
    assert isinstance(entries, list) and entries, f"{path.name} has no schedule"
    return [entry["cron"].split() for entry in entries]


def cron_time(fields: list[str]) -> tuple[int, int]:
    """Return ``(hour, minute)`` UTC of a fixed-time cron entry."""
    minute, hour = fields[0], fields[1]
    assert minute.isdigit() and hour.isdigit(), " ".join(fields)
    return int(hour), int(minute)


def command_lines(path: Path) -> list[str]:
    """Every ``edgarpit`` command line in file order, with ``uv run`` stripped."""
    lines: list[str] = []
    for _, _, run in run_blocks(path):
        for raw_line in run.splitlines():
            line = raw_line.strip()
            if line.startswith("uv run "):
                line = line[len("uv run ") :]
            if line.startswith("edgarpit "):
                lines.append(line)
    return lines


def assert_ordered(lines: list[str], patterns: tuple[str, ...]) -> None:
    """Assert ``patterns`` match successive lines, in order, without gaps."""
    position = 0
    for pattern in patterns:
        for index in range(position, len(lines)):
            if re.match(pattern, lines[index]):
                position = index + 1
                break
        else:
            raise AssertionError(f"{pattern!r} missing or out of order in {lines}")


def step_named(path: Path, name: str) -> dict[str, Any]:
    """Return the single step called ``name``."""
    matches = [step for _, step in steps(path) if step.get("name") == name]
    assert len(matches) == 1, f"{path.name} has {len(matches)} steps named {name!r}"
    return matches[0]


# --- triggers -----------------------------------------------------------------


@PATHS
def test_triggers_are_schedule_and_manual_dispatch_only(path: Path) -> None:
    event = events(path)
    assert set(event) == {"schedule", "workflow_dispatch"}
    # Neither workflow may run on a pull request or a push: a stranger's branch
    # must never reach the SEC or the release token.
    for forbidden in ("pull_request", "push", "workflow_run"):
        assert forbidden not in event
    # A manual run starts the same job under the same switch; it takes no input
    # that could bypass it.
    assert event["workflow_dispatch"] is None


def test_refresh_is_nightly_and_outside_the_pacific_morning_window() -> None:
    entries = cron_fields(REFRESH_PATH)
    assert len(entries) == 1
    assert entries[0][2:] == ["*", "*", "*"], "the refresh is a daily schedule"
    hour, minute = cron_time(entries[0])
    assert minute != 0, "the schedule's minute is jittered, not on the hour"
    for offset in (-7, -8):  # PDT and PST
        local = (hour * 60 + minute + offset * 60) % (24 * 60)
        assert local not in PACIFIC_MORNING, f"runs at {local // 60:02d}:{local % 60:02d} Pacific"


def test_release_is_weekly() -> None:
    entries = cron_fields(RELEASE_PATH)
    assert len(entries) == 1
    day_of_month, month, day_of_week = entries[0][2:]
    assert (day_of_month, month) == ("*", "*")
    assert day_of_week.isdigit() and int(day_of_week) in range(7), entries[0]


# --- enable switch ------------------------------------------------------------


@PATHS
def test_every_job_is_gated_on_refresh_enabled(path: Path) -> None:
    document = load(path)
    for name in jobs(path):
        assert gated(document, name), f"{path.name} job {name!r} is not gated"
    # The switch is a repository variable, not a hardcoded true or a secret.
    assert "vars.REFRESH_ENABLED == 'true'" in path.read_text(encoding="utf-8")


# --- permissions --------------------------------------------------------------


@PATHS
def test_the_workflow_token_starts_read_only(path: Path) -> None:
    document = load(path)
    assert document["permissions"] == {"contents": "read"}
    for name, job in document["jobs"].items():
        permissions = job.get("permissions") or {}
        writable = permissions.get("contents") == "write"
        # The single exception is the release job that creates the tag.
        assert not writable or (path == RELEASE_PATH and name == "publish"), name


def test_only_the_publishing_job_may_write() -> None:
    document = load(RELEASE_PATH)
    writing = {
        name
        for name, job in document["jobs"].items()
        if (job.get("permissions") or {}).get("contents") == "write"
    }
    assert writing == {"publish"}
    for name, job in document["jobs"].items():
        permissions = job.get("permissions") or {}
        assert set(permissions) <= {"contents"}, name
        assert set(permissions.values()) <= {"read", "write"}, name


@PATHS
def test_no_workflow_uses_pull_request_target(path: Path) -> None:
    assert "pull_request_target" not in path.read_text(encoding="utf-8")


# --- action pins, timeouts, runners, concurrency ------------------------------


@PATHS
def test_every_action_is_pinned_to_a_commit_sha_with_its_version(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    seen = 0
    for line in text.splitlines():
        match = re.match(r"^\s*uses:\s*(\S+)(?:\s+#\s*(.+))?$", line)
        if match is None:
            continue
        seen += 1
        action, comment = match.group(1), match.group(2) or ""
        assert re.match(r"^[\w.-]+/[\w.-]+(/[\w.-]+)?@[0-9a-f]{40}$", action), action
        assert re.search(r"v\d+\.\d+\.\d+", comment), f"{action} has no version comment"
        assert not action.startswith("docker://")
    assert seen >= 2, f"{path.name} declares no actions"


@PATHS
def test_every_job_has_a_bounded_timeout(path: Path) -> None:
    for name, job in jobs(path).items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int), f"{path.name} job {name!r} has no timeout"
        assert 0 < timeout <= 360, f"{path.name} job {name!r} timeout {timeout}"


@PATHS
def test_jobs_run_on_hosted_runners(path: Path) -> None:
    # Public repository: hosted runners keep strangers' code off the owner's machine.
    for name, job in jobs(path).items():
        assert job["runs-on"] == "ubuntu-24.04", name


@PATHS
def test_one_run_at_a_time(path: Path) -> None:
    concurrency = load(path)["concurrency"]
    assert isinstance(concurrency["group"], str)
    assert concurrency["group"]
    # The group must not vary per ref, or two runs could still overlap.
    assert "${{" not in concurrency["group"]
    assert concurrency["cancel-in-progress"] is False
    for name, job in jobs(path).items():
        assert "concurrency" not in job, f"{path.name} job {name!r} overrides concurrency"


def test_refresh_and_release_share_one_schedule_lane() -> None:
    """One group across both files: they can never fetch at the same time.

    Each run holds its own SEC token bucket, so two overlapping runs would
    send up to 10 requests/s even though every single run respects 5. A
    release that arrives while the refresh is still going must queue, and
    neither may be cancelled in flight: a cancel would leave the cache with
    whatever half-finished write the runner had reached.
    """
    refresh = load(REFRESH_PATH)["concurrency"]
    release = load(RELEASE_PATH)["concurrency"]
    assert refresh["group"] == "edgarpit-scheduled"
    assert release["group"] == refresh["group"]
    assert refresh["cancel-in-progress"] is False
    assert release["cancel-in-progress"] is False


# --- released version ---------------------------------------------------------


def test_every_place_that_reports_the_version_agrees() -> None:
    """The package version, the project metadata and the lockfile are one number."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in lock["package"]}
    assert pyproject["project"]["version"] == "1.1.0"
    assert __version__ == pyproject["project"]["version"]
    assert locked["edgarpit"] == pyproject["project"]["version"]


# --- no interpolation into shell scripts --------------------------------------


@PATHS
def test_no_expression_is_interpolated_into_a_run_script(path: Path) -> None:
    for job_name, step, run in run_blocks(path):
        assert "${{" not in run, f"{job_name}:{step.get('name')} interpolates into its script"


# --- refresh command sequence -------------------------------------------------


def test_refresh_runs_the_documented_commands_in_order() -> None:
    lines = command_lines(REFRESH_PATH)
    assert lines == [
        "edgarpit fetch --universe config/universe_large.csv --out data --incremental",
        "edgarpit build --data data --out build",
        "edgarpit fetch-text --db build/edgarpit.duckdb --items 2.02 --data data",
        "edgarpit fx --out data",
        "edgarpit build --data data --out build",
        "edgarpit qa --db build/edgarpit.duckdb | tee build/qa.txt",
    ]
    assert_ordered(lines, REFRESH_COMMANDS)


def test_refresh_restores_the_cache_before_fetching_and_saves_it_after_qa() -> None:
    ordered = list(steps(REFRESH_PATH))
    names = [step.get("name") for _, step in ordered]

    def index(name: str) -> int:
        return names.index(name)

    restore = step_named(REFRESH_PATH, "Restore raw cache")
    save = step_named(REFRESH_PATH, "Save raw cache")
    assert restore["uses"].startswith("actions/cache/restore@")
    assert save["uses"].startswith("actions/cache/save@")
    assert restore["with"]["path"] == "data/raw"
    assert save["with"]["path"] == "data/raw"
    # A fresh key per run rolls the cache forward; the prefix restores the newest one.
    assert "github.run_id" in restore["with"]["key"]
    assert restore["with"]["key"] == save["with"]["key"]
    assert "edgarpit-raw-" in restore["with"]["restore-keys"]
    # The first run of all has no cache, so a miss must not fail the refresh.
    assert restore["with"].get("fail-on-cache-miss") is not True
    assert "always()" in str(save.get("if", ""))
    assert index("Restore raw cache") < index("Fetch (incremental)")
    assert index("QA") < index("Save raw cache")


def test_refresh_saves_the_cache_only_when_it_has_content() -> None:
    check = step_named(REFRESH_PATH, "Check the raw cache has content")
    assert "data/raw/manifest.jsonl" in check["run"]
    assert "$GITHUB_OUTPUT" in check["run"]
    assert "always()" in str(check.get("if", ""))
    save = step_named(REFRESH_PATH, "Save raw cache")
    assert "steps.cache-present.outputs.present == 'true'" in str(save.get("if", ""))
    names = [step.get("name") for _, step in steps(REFRESH_PATH)]
    assert names.index("Check the raw cache has content") < names.index("Save raw cache")


def test_refresh_uploads_the_build_info_and_qa_report() -> None:
    upload = step_named(REFRESH_PATH, "Upload build report")
    assert upload["uses"].startswith("actions/upload-artifact@")
    paths = upload["with"]["path"]
    assert "build/build-info.json" in paths
    assert "build/qa.txt" in paths
    assert "always()" in str(upload.get("if", ""))


def test_release_keeps_the_build_report_of_a_failed_run() -> None:
    report = step_named(RELEASE_PATH, "Upload the build report")
    assert report["with"]["name"] == "release-report"
    assert "always()" in str(report.get("if", ""))
    assert "build/build-info.json" in report["with"]["path"]
    assert "build/qa.txt" in report["with"]["path"]


# --- secret handling ----------------------------------------------------------


def test_refresh_passes_the_sec_user_agent_only_to_the_fetching_steps() -> None:
    document = load(REFRESH_PATH)
    assert SECRET not in str(document.get("env", {}))
    receivers: set[str] = set()
    for job_name, job in document["jobs"].items():
        assert SECRET not in str(job.get("env", {})), job_name
        for step in job.get("steps", []):
            if SECRET in str(step.get("env", {})):
                receivers.add(step["name"])
    assert receivers == SECRET_RECEIVERS
    # The value is read exactly once, by the emptiness test; no step prints it.
    for _, _, run in run_blocks(REFRESH_PATH):
        for line in run.splitlines():
            if '"$SEC_USER_AGENT"' in line:
                assert 'if [ -z "$SEC_USER_AGENT" ]; then' in line
            if line.strip().startswith("echo"):
                assert "$SEC_USER_AGENT" not in line
    assert "set -x" not in REFRESH_PATH.read_text(encoding="utf-8")


def test_refresh_fails_fast_without_the_sec_user_agent() -> None:
    guard = step_named(REFRESH_PATH, "Check SEC_USER_AGENT")
    script = guard["run"]
    assert "::error::" in script
    assert "SEC_USER_AGENT" in script
    assert "exit 1" in script
    # The guard runs before the first request.
    names = [step.get("name") for _, step in steps(REFRESH_PATH)]
    assert names.index("Check SEC_USER_AGENT") < names.index("Fetch (incremental)")


def test_release_passes_the_sec_user_agent_only_to_the_fetching_steps() -> None:
    """The weekly release fetches, so it needs the secret - but only where it fetches."""
    document = load(RELEASE_PATH)
    assert SECRET not in str(document.get("env", {}))
    receivers: set[str] = set()
    for job_name, job in document["jobs"].items():
        assert SECRET not in str(job.get("env", {})), job_name
        for step in job.get("steps", []):
            if SECRET in str(step.get("env", {})):
                receivers.add(step["name"])
    assert receivers == RELEASE_SECRET_RECEIVERS
    text = RELEASE_PATH.read_text(encoding="utf-8")
    for _, _, run in run_blocks(RELEASE_PATH):
        for line in run.splitlines():
            assert "$SEC_USER_AGENT" not in line or 'if [ -z "$SEC_USER_AGENT" ]; then' in line
    assert "secrets.GITHUB_TOKEN" in text
    assert "set -x" not in text


def test_release_fails_fast_without_the_sec_user_agent() -> None:
    guard = step_named(RELEASE_PATH, "Check SEC_USER_AGENT")
    assert "::error::" in guard["run"]
    assert "SEC_USER_AGENT" in guard["run"]
    assert "exit 1" in guard["run"]
    # Before the first request, so an empty secret costs no rate-limit slot.
    names = [step.get("name") for _, step in steps(RELEASE_PATH)]
    assert names.index("Check SEC_USER_AGENT") < names.index("Fetch (full)")


# --- release ------------------------------------------------------------------


def test_release_reads_the_cache_without_writing_it() -> None:
    text = RELEASE_PATH.read_text(encoding="utf-8")
    assert "actions/cache/save" not in text
    restore = step_named(RELEASE_PATH, "Restore raw cache")
    assert restore["uses"].startswith("actions/cache/restore@")
    assert restore["with"]["path"] == "data/raw"
    assert "github.run_id" in restore["with"]["key"]
    assert "edgarpit-raw-" in restore["with"]["restore-keys"]
    # Releasing a database built from an empty cache is worse than no release.
    assert restore["with"]["fail-on-cache-miss"] is True


def test_release_fetches_the_whole_universe_before_building() -> None:
    lines = command_lines(RELEASE_PATH)
    assert lines == [
        "edgarpit fetch --universe config/universe_large.csv --out data",
        "edgarpit build --data data --out build",
        "edgarpit qa --db build/edgarpit.duckdb | tee build/qa.txt",
        "edgarpit export --db build/edgarpit.duckdb --out release",
    ]
    # The weekly run is the full-fetch safety net (decision A-42); only the
    # nightly refresh may use the incremental probe.
    assert "--incremental" not in RELEASE_PATH.read_text(encoding="utf-8")
    # QA runs before the export, so a key violation stops the release.
    assert_ordered(lines, RELEASE_COMMANDS)
    names = [step.get("name") for _, step in steps(RELEASE_PATH)]
    assert names.index("Restore raw cache") < names.index("Fetch (full)")
    assert names.index("Check SEC_USER_AGENT") < names.index("Fetch (full)")
    assert names.index("Fetch (full)") < names.index("Build")


def test_release_assembles_every_documented_asset() -> None:
    assemble = step_named(RELEASE_PATH, "Assemble the release assets")["run"]
    # One glob copies whatever the exporter wrote; the exporter's table list
    # below is what the notes must describe.
    assert "cp release/*.parquet release/manifest.json assets/" in assemble
    for asset in (
        "edgarpit.duckdb",
        "manifest.json",
        "build-info.json",
        "SHA256SUMS",
    ):
        assert asset in assemble
    assert "sha256sum -c SHA256SUMS" in assemble
    assert 'TAG="data-$(date -u +%F)"' in assemble


def test_release_notes_list_every_exported_asset() -> None:
    """The notes name all eight tables the export writes, and invent none.

    The list is pinned against ``edgarpit.export`` rather than against a copy,
    so adding a table to the release without describing it fails here.
    """
    notes = step_named(RELEASE_PATH, "Assemble the release assets")["run"]
    tables = [table.name for table in EXPORTS + PHASE2_EXPORTS]
    assert len(tables) == 8
    for table in tables:
        assert f"- {table}.parquet - " in notes, table
    for asset in ("edgarpit.duckdb", "manifest.json", "build-info.json", "SHA256SUMS"):
        assert f"- {asset} - " in notes, asset
    # No stale or invented Parquet name in the notes.
    mentioned = set(re.findall(r"\b([a-z][a-z0-9_]*)\.parquet\b", notes))
    assert mentioned == set(tables)
    # A stranger must learn what the two Phase 2 additions mean, not just their names.
    assert "8-K text is split on the filing's own Item headings" in notes
    assert "USD values are converted with the latest Bank of Canada rate" in notes


def test_release_notes_explain_how_the_release_became_known() -> None:
    notes = step_named(RELEASE_PATH, "Assemble the release assets")["run"]
    # The point-in-time rule is the one thing every consumer must understand.
    assert "acceptance timestamp" in notes
    assert "asof_ts" in notes
    assert "sha256sum -c SHA256SUMS" in notes
    assert "SEC EDGAR" in notes
    assert "MIT" in notes
    assert "assets:" in notes.lower()


def test_publishing_is_a_separate_least_privilege_job() -> None:
    document = load(RELEASE_PATH)
    publish = document["jobs"]["publish"]
    assert publish["needs"] == "build"
    assert publish["permissions"] == {"contents": "write"}
    publish_run = step_named(RELEASE_PATH, "Publish the GitHub Release")["run"]
    assert 'TAG="$(cat meta/release-tag.txt)"' in publish_run
    assert 'gh release create "$TAG"' in publish_run
    assert '--target "$GITHUB_SHA"' in publish_run
    assert "--notes-file meta/release-notes.md" in publish_run
    assert "assets/*" in publish_run
    # An existing tag is never overwritten: published data releases are immutable.
    assert "gh release view" in publish_run
    assert "exit 1" in publish_run
    build_run = step_named(RELEASE_PATH, "Assemble the release assets")["run"]
    upload = step_named(RELEASE_PATH, "Upload the release assets")
    assert "assets/" in upload["with"]["path"]
    assert "meta/" in upload["with"]["path"]
    assert "release-notes.md" in build_run
    assert "release-tag.txt" in build_run


# --- shared failure policy ----------------------------------------------------


@PATHS
def test_no_step_silences_a_failure(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "continue-on-error" not in text
    for line in text.splitlines():
        assert "|| true" not in line
        assert "|| echo" not in line
        assert "set +e" not in line
