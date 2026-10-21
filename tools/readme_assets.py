#!/usr/bin/env python3
"""Build the two images used by ``README.md`` from the offline SEC fixtures.

Run it locally, not in CI (it needs the optional ``agg`` binary)::

    brew install agg                      # once; renders the terminal cast to GIF
    uv run python tools/readme_assets.py

The tool builds ``edgarpit.duckdb`` from ``tests/fixtures/sec`` into a
temporary work directory (no network, no SEC credentials), then writes:

- ``docs/img/ry-revenue-restatement.svg`` — Royal Bank of Canada annual
  revenue by fiscal year (CAD, billions; fiscal year ends 31 October). Every
  filing version from ``main.fundamentals_pit`` is a dot: filled for the
  first filing of a period, open for later versions of the same period. The
  FY2023 revision is drawn in red and labelled with the value and acceptance
  timestamp of both the original 40-F and the 40-F that restated it.
- ``docs/img/asof-demo.gif`` — a terminal recording of two ``edgarpit asof``
  calls, one second before and exactly at the restating 40-F's acceptance
  timestamp, so the FY2023 value flips between them. The cast is generated
  here with fixed timings from the real command output and rendered by
  ``agg``; it is not hand-edited.

Both assets are deterministic: the chart is byte-identical across runs, and
the cast carries no wall-clock timestamps, so only the ``agg`` version and
font fallback can change the GIF.

Inputs: the fixture raw cache (``--fixtures``, default ``tests/fixtures/sec``)
and the image directory (``--out``, default ``docs/img``). Outputs: the two
files above. Errors: a failing build, missing ``agg`` or ``edgarpit`` binary,
or fixture data that no longer contains the expected RY restatement raises
with a one-line cause and a non-zero exit.

The commands shown in the recording use ``--db "$DB"``; the script exports
``DB`` to the database it just built, so the recorded lines are exactly the
commands that ran.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import altair as alt
import duckdb
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "sec"
DEFAULT_IMAGE_DIR = ROOT / "docs" / "img"
TICKER = "RY"
ISSUER_NAME = "Royal Bank of Canada"
FIELD = "revenue"
TERMINAL_COLUMNS = 92
TERMINAL_ROWS = 38
MAX_GIF_BYTES = 2_000_000

# GitHub's light-theme palette: the SVG has a solid white background, so the
# chart reads the same in light and dark viewers.
BLUE = "#0969da"
RED = "#cf222e"
INK = "#1f2328"
MUTED = "#57606a"
GRID = "#d8dee4"
AXIS_LINE = "#8c959f"
BACKGROUND = "#ffffff"


class AssetError(RuntimeError):
    """A README asset cannot be built; the message names the cause."""


def _format_ts(moment: datetime) -> str:
    """Format ``moment`` as UTC ISO-8601 with seconds and a ``Z`` suffix."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _relative(path: Path) -> str:
    """Return ``path`` relative to the repository when it lives inside it."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def build_database(fixtures: Path, work: Path) -> Path:
    """Build ``<work>/edgarpit.duckdb`` from the ``fixtures`` raw cache.

    ``fixtures`` is a raw-cache root (``tests/fixtures/sec`` by default) and
    ``work`` the build directory. The build runs through the ``edgarpit``
    executable, so it exercises the same command a reader runs; its console
    output goes to ``<work>/build.log``. Returns the database path. Raises
    :class:`AssetError` when the cache is missing, the executable is not on
    ``PATH``, or the build fails.
    """
    if not (fixtures / "raw").is_dir():
        raise AssetError(f"{fixtures} is not a raw-cache directory (no raw/ subdirectory)")
    executable = shutil.which("edgarpit")
    if executable is None:
        raise AssetError(
            "the edgarpit executable is not on PATH; run this tool with "
            "`uv run python tools/readme_assets.py`"
        )
    work.mkdir(parents=True, exist_ok=True)
    log_path = work / "build.log"
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [
                executable,
                "build",
                "--offline",
                "--fixtures",
                str(fixtures),
                "--out",
                str(work),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    database = work / "edgarpit.duckdb"
    if result.returncode != 0:
        raise AssetError(f"build from {fixtures} failed; see {log_path}")
    if not database.is_file():
        raise AssetError(f"build did not produce {database}; see {log_path}")
    return database


def load_versions(database: Path) -> pl.DataFrame:
    """Return the RY annual revenue versions from ``main.fundamentals_pit``.

    One row per filing version, ordered by ``(period_end, version)``. Columns:
    ``period_end`` (date), ``acceptance_ts`` (timezone-aware UTC), ``value``
    (CAD), ``unit``, ``version``, ``form``, ``accession``, ``first_value``,
    ``restatement_kind``. Raises :class:`AssetError` when the fixture data no
    longer has exactly one downward ``revision``.
    """
    connection = duckdb.connect(str(database), read_only=True)
    try:
        connection.execute("SET TimeZone='UTC'")
        frame = connection.sql(
            """
            SELECT period_end, accession, form, acceptance_ts,
                   value, unit, version, first_value, restatement_kind
            FROM main.fundamentals_pit
            WHERE ticker = ? AND field = ? AND period_type = 'FY'
            ORDER BY period_end, version
            """,
            params=[TICKER, FIELD],
        ).pl()
    finally:
        connection.close()
    if frame.is_empty():
        raise AssetError(f"no {FIELD!r} rows for {TICKER} in {database}")
    units = set(frame["unit"].to_list())
    if units != {"CAD"}:
        raise AssetError(f"{TICKER} {FIELD} is not reported in CAD only: units {sorted(units)}")
    first_periods = frame.filter(pl.col("version") == 1).height
    if first_periods != frame["period_end"].n_unique():
        raise AssetError("every period must have exactly one version 1")
    revisions = frame.filter(pl.col("restatement_kind") == "revision")
    if revisions.height != 1:
        raise AssetError(
            f"expected exactly one revision in {TICKER} {FIELD}, found {revisions.height}"
        )
    revision = revisions.row(0, named=True)
    if revision["value"] >= revision["first_value"]:
        raise AssetError("the reference restatement is expected to revise the value downwards")
    return frame


def _billions(frame: pl.DataFrame) -> pl.DataFrame:
    """Add the chart's ``year``, ``x`` and ``billions`` columns to ``frame``."""
    return frame.with_columns(
        year=pl.col("period_end").dt.year(),
        billions=pl.col("value") / 1e9,
        # Open dots sit a fraction of a year to the right of the filled dots so
        # a repeated version stays visible instead of hiding under the first.
        x=pl.col("period_end").dt.year().cast(pl.Float64)
        + (pl.col("version") - 1).cast(pl.Float64) * 0.09,
    )


def revenue_chart(frame: pl.DataFrame) -> alt.LayerChart:
    """Return the Altair chart for the RY revenue versions in ``frame``.

    ``frame`` is the output of :func:`load_versions`. The returned chart is not
    saved here. Values are plotted in CAD billions; ``acceptance_ts`` labels
    are UTC. Raises :class:`AssetError` if the revision row cannot be found.
    """
    data = _billions(frame)
    revisions = data.filter(pl.col("restatement_kind") == "revision")
    if revisions.height != 1:
        raise AssetError("the chart needs exactly one revision to highlight")
    revision = revisions.row(0, named=True)
    original = data.filter(
        (pl.col("period_end") == revision["period_end"]) & (pl.col("version") == 1)
    )
    if original.height != 1:
        raise AssetError("the revision's first version is missing")
    filed = original.row(0, named=True)

    years = sorted(data["year"].unique().to_list())
    x_domain = [years[0] - 0.45, years[-1] + 0.45]
    y_top = math.ceil(float(data["billions"].max()) * 1.05 / 10) * 10
    x_scale = alt.Scale(domain=x_domain)
    y_scale = alt.Scale(domain=[0, y_top], nice=False)

    def points(rows: pl.DataFrame, *, filled: bool, colour: str, size: int) -> alt.Chart:
        return (
            alt.Chart(rows)
            .mark_point(filled=filled, color=colour, size=size, stroke=colour, strokeWidth=1.5)
            .encode(x=alt.X("x:Q", scale=x_scale), y=alt.Y("y:Q", scale=y_scale))
        )

    base = data.select("x", "billions", "version").rename({"billions": "y"})
    first = base.filter(pl.col("version") == 1)
    later = base.filter(pl.col("version") > 1)
    restated_point_data = pl.DataFrame(
        {"x": [float(revision["x"])], "y": [float(revision["billions"])]}
    )
    original_point_data = pl.DataFrame({"x": [float(filed["x"])], "y": [float(filed["billions"])]})

    line = (
        alt.Chart(first)
        .mark_line(color=BLUE, strokeWidth=2)
        .encode(
            x=alt.X(
                "x:Q",
                scale=x_scale,
                title="Fiscal year ended 31 October",
                axis=alt.Axis(
                    format="d",
                    values=years,
                    tickMinStep=1,
                    labelExpr="'FY' + datum.label",
                ),
            ),
            y=alt.Y(
                "y:Q",
                scale=y_scale,
                title="Revenue (CAD billions)",
                axis=alt.Axis(format=".0f", tickMinStep=10),
            ),
        )
    )
    points_first = points(first, filled=True, colour=BLUE, size=70)
    points_later = points(later, filled=False, colour=BLUE, size=70)
    restated_point = points(restated_point_data, filled=False, colour=RED, size=150)
    original_point = points(original_point_data, filled=True, colour=RED, size=110)

    connector = (
        alt.Chart(
            pl.DataFrame(
                {
                    "x": [float(filed["x"])],
                    "x2": [float(revision["x"])],
                    "y": [float(filed["billions"])],
                    "y2": [float(revision["billions"])],
                }
            )
        )
        .mark_rule(color=RED, strokeDash=[4, 3], strokeWidth=1.5)
        .encode(x="x:Q", x2="x2:Q", y="y:Q", y2="y2:Q")
    )

    left_label = pl.DataFrame(
        {
            "x": [float(filed["x"]) - 0.03] * 2,
            "y": [float(filed["billions"]) + 5.2, float(filed["billions"]) + 2.3],
            "text": [
                f"{filed['billions']:.3f} B as filed",
                _format_ts(filed["acceptance_ts"]),
            ],
        }
    )
    right_label = pl.DataFrame(
        {
            "x": [float(revision["x"]) + 0.03] * 2,
            "y": [float(revision["billions"]) - 2.6, float(revision["billions"]) - 5.5],
            "text": [
                f"{revision['billions']:.3f} B as restated",
                _format_ts(revision["acceptance_ts"]),
            ],
        }
    )

    def label_box(rows: pl.DataFrame, *, align: str) -> alt.Chart:
        return (
            alt.Chart(rows)
            .mark_text(color=RED, fontSize=11, align=align)
            .encode(
                x=alt.X("x:Q", scale=x_scale),
                y=alt.Y("y:Q", scale=y_scale),
                text="text:N",
            )
        )

    change = (float(revision["value"]) / float(revision["first_value"]) - 1.0) * 100.0
    direction = "down" if change < 0 else "up"
    subtitle = [
        "Filled dots: the value as first filed. Open dots: later versions of the same period.",
        (
            f"FY{int(filed['year'])} revenue was restated {direction} {abs(change):.1f}% "
            f"by the {revision['form']} accepted {_format_ts(revision['acceptance_ts'])}."
        ),
    ]

    chart = (
        (
            line
            + points_first
            + points_later
            + connector
            + restated_point
            + original_point
            + label_box(left_label, align="right")
            + label_box(right_label, align="left")
        )
        .properties(
            width=860,
            height=380,
            background=BACKGROUND,
            padding={"left": 8, "top": 8, "right": 16, "bottom": 8},
            title=alt.Title(
                f"{ISSUER_NAME} — annual revenue, every filing version",
                subtitle=subtitle,
                fontSize=17,
                subtitleFontSize=11,
                subtitleColor=MUTED,
                color=INK,
                anchor="start",
                offset=14,
            ),
        )
        .configure(font="Helvetica Neue, Helvetica, Arial, sans-serif")
        .configure_view(stroke=None, fill=BACKGROUND)
        .configure_axis(
            gridColor=GRID,
            domainColor=AXIS_LINE,
            tickColor=AXIS_LINE,
            labelColor=MUTED,
            titleColor=INK,
            labelFontSize=11,
            titleFontSize=12,
            titleFontWeight="normal",
            grid=True,
        )
    )
    return chart


def write_chart(frame: pl.DataFrame, destination: Path) -> None:
    """Write the RY revenue chart to ``destination`` as SVG.

    The SVG carries an explicit white background so it is readable in both
    light and dark document themes. Raises :class:`AssetError` if the renderer
    is unavailable or cannot write the file.
    """
    chart = revenue_chart(frame)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        chart.save(str(destination), format="svg")
    except Exception as error:  # vl-convert reports renderer problems as varied exceptions
        raise AssetError(f"cannot render {destination}: {type(error).__name__}: {error}") from error
    if not destination.is_file() or destination.stat().st_size == 0:
        raise AssetError(f"chart was not written to {destination}")


def asof_commands(frame: pl.DataFrame) -> tuple[str, str]:
    """Return the two ``edgarpit asof`` command lines, one second apart.

    The second timestamp is the acceptance instant of the restating 40-F from
    ``frame``; the first is one second earlier. Both ask for two periods
    because the restating 40-F also introduces the next fiscal year, so the
    restated period is only visible alongside it.
    """
    revisions = frame.filter(pl.col("restatement_kind") == "revision")
    if revisions.height != 1:
        raise AssetError("the recording needs exactly one revision to flip")
    accepted = revisions.row(0, named=True)["acceptance_ts"]
    before = (accepted - timedelta(seconds=1)).astimezone(accepted.tzinfo)
    template = 'edgarpit asof --db "$DB" --ticker {ticker} --field {field} --asof {moment} --n 2'
    return (
        template.format(ticker=TICKER, field=FIELD, moment=_format_ts(before)),
        template.format(ticker=TICKER, field=FIELD, moment=_format_ts(accepted)),
    )


def run_asof(command: str, database: Path) -> str:
    """Run one ``command`` against ``database`` and return its stdout.

    ``DB`` is exported for the command's ``--db "$DB"`` argument. Raises
    :class:`AssetError` when the ``edgarpit`` binary is not on ``PATH`` or the
    command exits non-zero.
    """
    if shutil.which("edgarpit") is None:
        raise AssetError(
            "the edgarpit executable is not on PATH; run this tool with "
            "`uv run python tools/readme_assets.py`"
        )
    environment = {**os.environ, "DB": str(database)}
    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise AssetError(f"`{command}` exited {result.returncode}: {result.stderr.strip()}")
    if not result.stdout.strip():
        raise AssetError(f"`{command}` printed nothing")
    return result.stdout


def write_cast(destination: Path, commands: Sequence[str], outputs: Sequence[str]) -> None:
    """Write an asciinema v2 cast of ``commands`` and their ``outputs``.

    The header and every event carry fixed values (``timestamp: 0``, fixed
    delays), so re-running the tool produces the same cast. ``destination`` is
    written as UTF-8 with one JSON object per line.
    """
    if len(commands) != len(outputs):
        raise AssetError("every recorded command needs one output")
    header = json.dumps(
        {
            "version": 2,
            "width": TERMINAL_COLUMNS,
            "height": TERMINAL_ROWS,
            "timestamp": 0,
            "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
        }
    )
    moments: list[float] = []
    payloads: list[str] = []
    moment = 0.5
    for command, output in zip(commands, outputs, strict=True):
        moments.append(round(moment, 2))
        payloads.append(f"$ {command}\r\n")
        moment += 0.45
        paragraphs = output.rstrip("\n").split("\n\n")
        for paragraph in paragraphs:
            moments.append(round(moment, 2))
            payloads.append(paragraph.replace("\n", "\r\n") + "\r\n\r\n")
            moment += 0.4
        moment += 1.4

    rows = sum(payload.count("\r\n") for payload in payloads)
    if rows > TERMINAL_ROWS:
        raise AssetError(
            f"the recording needs {rows} terminal rows, more than the {TERMINAL_ROWS} set here"
        )
    wide = [
        line
        for payload in payloads
        for line in payload.split("\r\n")
        if len(line) > TERMINAL_COLUMNS
    ]
    if wide:
        raise AssetError(
            f"a recorded line is {len(wide[0])} columns, over the {TERMINAL_COLUMNS} set here"
        )
    lines = [header] + [
        json.dumps([moment, "o", payload])
        for moment, payload in zip(moments, payloads, strict=True)
    ]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_gif(cast: Path, destination: Path) -> None:
    """Render ``cast`` to a looping GIF at ``destination`` with ``agg``.

    Raises :class:`AssetError` when ``agg`` is missing, exits non-zero, or the
    GIF exceeds :data:`MAX_GIF_BYTES` (GitHub renders larger files poorly).
    """
    binary = shutil.which("agg")
    if binary is None:
        raise AssetError("agg is not installed; install it with `brew install agg`")
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            binary,
            "--theme",
            "github-dark",
            "--font-size",
            "15",
            "--line-height",
            "1.35",
            "--speed",
            "1",
            "--idle-time-limit",
            "2",
            "--last-frame-duration",
            "4",
            "--quiet",
            str(cast),
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssetError(f"agg exited {result.returncode}: {result.stderr.strip()}")
    size = destination.stat().st_size if destination.is_file() else 0
    if size == 0:
        raise AssetError(f"agg did not write {destination}")
    if size > MAX_GIF_BYTES:
        raise AssetError(f"{destination} is {size} bytes, over the {MAX_GIF_BYTES}-byte limit")


def build_assets(fixtures: Path, image_dir: Path, work: Path) -> tuple[Path, Path]:
    """Build both README assets and return their paths.

    ``fixtures`` is the raw cache, ``image_dir`` the directory for the
    ``.svg``/``.gif`` (created when missing) and ``work`` the scratch build
    directory. Raises :class:`AssetError` on any failure described by the
    functions above.
    """
    database = build_database(fixtures, work)
    frame = load_versions(database)
    chart_path = image_dir / "ry-revenue-restatement.svg"
    write_chart(frame, chart_path)

    commands = asof_commands(frame)
    outputs = [run_asof(command, database) for command in commands]
    revision = frame.filter(pl.col("restatement_kind") == "revision").row(0, named=True)
    filed = frame.filter(
        (pl.col("period_end") == revision["period_end"]) & (pl.col("version") == 1)
    ).row(0, named=True)
    restated_text = f"{float(revision['value']):.0f}"
    filed_text = f"{float(filed['value']):.0f}"
    if filed_text not in outputs[0] or restated_text not in outputs[1]:
        raise AssetError("the as-of outputs do not show the expected first and restated values")
    if restated_text in outputs[0]:
        raise AssetError("the restated value is already visible one second before its acceptance")

    cast_path = work / "asof-demo.cast"
    write_cast(cast_path, commands, outputs)
    gif_path = image_dir / "asof-demo.gif"
    render_gif(cast_path, gif_path)
    return chart_path, gif_path


def main(argv: list[str] | None = None) -> int:
    """``python tools/readme_assets.py [--fixtures DIR] [--out DIR] [--work DIR]``.

    Builds the database from the fixture cache, writes the SVG chart and the
    GIF recording, and prints their paths and sizes. Returns 0; an
    :class:`AssetError` prints one line to stderr and returns 1.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help=f"raw-cache directory to build from (default: {_relative(DEFAULT_FIXTURES)})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_IMAGE_DIR,
        help=f"directory for the SVG and GIF (default: {_relative(DEFAULT_IMAGE_DIR)})",
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=None,
        help="scratch build directory (default: a new temporary directory)",
    )
    args = parser.parse_args(argv)
    work = args.work if args.work is not None else Path(tempfile.mkdtemp(prefix="edgarpit-readme-"))
    try:
        chart_path, gif_path = build_assets(args.fixtures, args.out, work)
    except AssetError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for path in (chart_path, gif_path):
        print(f"{_relative(path)}  {path.stat().st_size:,} bytes")
    print(f"built in {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
