"""Streamlit explorer over the point-in-time query layer (issue #31).

Reads ``EDGARPIT_DB`` through :mod:`edgarpit.query` only (decision A-21). A
value is known at time ``t`` if and only if its filing's ``acceptance_ts <= t``;
the "as of" control uses that rule to mark and grey out versions.

Run with ``streamlit run explorer/app.py``; the tests drive it headlessly with
``streamlit.testing.v1.AppTest``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import altair as alt
import duckdb
import polars as pl
import streamlit as st

from edgarpit import query
from edgarpit.config import Settings

STATUS_ORDER = ["version 1", "restatement", "later version", "not yet accepted"]
STATUS_COLORS = ["#1f77b4", "#d62728", "#ff7f0e", "#9e9e9e"]
STATUS_SHAPES = ["circle", "diamond", "square", "cross"]

VERSION_COLUMNS = [
    "period_end",
    "version",
    "value",
    "unit",
    "accepted",
    "known_at_asof",
    "status",
    "accession",
]


def filing_url(cik: str, accession: str) -> str:
    """Return the EDGAR filing-index URL for ``accession`` of issuer ``cik``.

    ``cik`` is a 10-character CIK (converted to its integer form) and
    ``accession`` is the dashed accession number.
    """
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}/{accession}-index.htm"
    )


def _status(version: int, is_restatement: bool, known: bool) -> str:
    if not known:
        return "not yet accepted"
    if is_restatement:
        return "restatement"
    if version == 1:
        return "version 1"
    return "later version"


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def versions_frame(rows: list[query.AsOfResult], asof: datetime) -> pl.DataFrame:
    """Build the versions table for ``rows`` as a Polars frame.

    ``asof`` is the UTC instant of the "as of" control; each row gets a
    ``known_at_asof`` flag (``acceptance_ts <= asof``) and a ``status`` used by
    the chart and the table. One row per filing version, in query order.
    """
    return pl.DataFrame(
        {
            "period_start": [row.period_start for row in rows],
            "period_end": [row.period_end for row in rows],
            "version": [row.version for row in rows],
            "value": [row.value for row in rows],
            "unit": [row.unit for row in rows],
            "accession": [row.accession for row in rows],
            "form": [row.form for row in rows],
            "accepted": [_iso_z(row.acceptance_ts) for row in rows],
            "known_at_asof": [row.acceptance_ts <= asof for row in rows],
            "is_restatement": [row.is_restatement for row in rows],
            "status": [
                _status(row.version, row.is_restatement, row.acceptance_ts <= asof) for row in rows
            ],
        }
    )


def events_frame(rows: list[query.EventResult]) -> pl.DataFrame:
    """Build the 8-K events table for ``rows`` as a Polars frame.

    ``filing`` holds the EDGAR index URL; the table shows ``accepted`` (UTC,
    ``Z`` suffix), ``item_code``, ``item_label`` and ``form``.
    """
    return pl.DataFrame(
        {
            "accepted": [_iso_z(row.acceptance_ts) for row in rows],
            "item_code": [row.item_code for row in rows],
            "item_label": [row.item_label for row in rows],
            "form": [row.form for row in rows],
            "filing": [filing_url(row.cik, row.accession) for row in rows],
        }
    )


def timeline_chart(frame: pl.DataFrame) -> alt.Chart:
    """Return the Altair timeline: one point per version at its ``period_end``.

    Colour and shape encode ``status`` (version 1, restatement, later version,
    not yet accepted), so restatements and versions not yet accepted at the
    "as of" time are visually distinct with a legend; the tooltip carries the
    value, unit, accession and acceptance time.
    """
    units = frame["unit"].unique().to_list()
    y_title = f"value ({units[0]})" if len(units) == 1 else "value"
    chart: alt.Chart = (
        alt.Chart(frame)
        .mark_point(filled=True, size=120)
        .encode(
            x=alt.X("period_end:T", title="period end"),
            y=alt.Y("value:Q", title=y_title),
            color=alt.Color(
                "status:N",
                title="version status",
                scale=alt.Scale(domain=STATUS_ORDER, range=STATUS_COLORS),
            ),
            shape=alt.Shape(
                "status:N",
                title="version status",
                scale=alt.Scale(domain=STATUS_ORDER, range=STATUS_SHAPES),
            ),
            tooltip=[
                alt.Tooltip("period_end:T", title="period end"),
                alt.Tooltip("value:Q", title="value", format=",.2f"),
                alt.Tooltip("unit:N", title="unit"),
                alt.Tooltip("version:Q", title="version"),
                alt.Tooltip("accession:N", title="accession"),
                alt.Tooltip("accepted:N", title="accepted (UTC)"),
                alt.Tooltip("known_at_asof:N", title="known at as-of"),
            ],
        )
    )
    return chart


def main() -> None:
    """Render the explorer page for the database at ``Settings().edgarpit_db``."""
    st.set_page_config(page_title="edgar-pit explorer", layout="wide")
    st.title("edgar-pit explorer")
    st.caption(
        "Point-in-time SEC fundamentals and corporate events. A value is known at time t "
        "if and only if the filing that carried it was accepted at or before t."
    )

    db_path = Settings().edgarpit_db
    if not Path(db_path).exists():
        st.error(
            f"No database at {db_path}. Set EDGARPIT_DB to a built database, or create one "
            "with `uv run python -m edgarpit.devdb --out <dir>` and the dbt models."
        )
        return

    try:
        issuers = query.issuers()
    except duckdb.Error as exc:
        st.error(f"Could not read {db_path}: {exc}")
        return
    if not issuers:
        st.warning("The database has no issuers with fundamentals or events yet.")
        return

    labels = {f"{ticker or 'no ticker'} ({cik})": cik for cik, ticker in issuers}
    issuer = st.sidebar.selectbox("Issuer (ticker and CIK)", options=list(labels), key="issuer")
    cik = labels[issuer]

    available_fields = query.fields(cik)
    field = st.sidebar.selectbox("Field", options=available_fields) if available_fields else None
    period_choice = st.sidebar.radio("Period", options=["FY", "Q"], horizontal=True, key="period")
    quarterly = period_choice == "Q"
    period: Literal["FY", "Q"] = "Q" if quarterly else "FY"
    asof_date = st.sidebar.date_input("As of (UTC midnight)", value=date.today(), key="asof")
    asof = datetime(asof_date.year, asof_date.month, asof_date.day, tzinfo=UTC)
    st.sidebar.caption("Versions accepted after this instant are greyed out and marked not known.")

    st.subheader(f"{issuer} — {field if field is not None else 'no fields'}")
    if field is None:
        st.info("This issuer has no fundamental values in the database.")
    else:
        rows = query.history(cik, field, period=period)
        if not rows:
            st.info(f"No {period} versions of {field} for this issuer.")
        else:
            frame = versions_frame(rows, asof)
            known = int(frame["known_at_asof"].sum())
            chart_column, table_column = st.columns([3, 2])
            with chart_column:
                st.altair_chart(timeline_chart(frame), width="stretch")
            with table_column:
                st.metric("versions known at the as-of", f"{known} of {len(frame)}")
                st.dataframe(frame[VERSION_COLUMNS], width="stretch", hide_index=True)

    st.subheader("8-K events")
    event_rows = query.events(cik)
    if not event_rows:
        st.info("This issuer has no 8-K events in the database.")
    else:
        st.dataframe(
            events_frame(event_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "filing": st.column_config.LinkColumn(
                    "filing", display_text=r"(\d{10}-\d{2}-\d{6})-index\.htm$"
                )
            },
        )


if __name__ == "__main__":
    main()
