"""Phase 2 end-to-end test over the merged SEC fixture cache (issue #132).

One build of ``tests/fixtures/sec`` through :func:`edgarpit.load.build_from_raw`
— the code path behind ``edgarpit build --offline`` — exercises 8-K text
extraction and USD conversion on the real fixture bytes, with no network. The
cache holds the submissions, companyfacts and header fixtures, the 8-K text
fixtures (decisions A-30, A-44) and the Bank of Canada ``FXUSDCAD`` series, so a
single build produces every Phase 2 table the release ships.

Each test pins one acceptance criterion: the sections of all 15 fixture
filings, the hand-checked sentences of ``tests/fixtures/text_expected.json``,
the acceptance time copied from ``raw.filings``, the single ingested FX series,
the conversion rules of decision A-32, the USD and text read surface, the QA
keys, and byte-identical release exports.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from edgarpit import query
from edgarpit.export import EXPORTS, PHASE2_EXPORTS, export_release
from edgarpit.load import build_from_raw
from edgarpit.qa import run_qa
from edgarpit.rawcache import RawCache

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
EXPECTED_TEXT = Path(__file__).parent / "fixtures" / "text_expected.json"

RY_CIK = "0001000275"
RY_FY2023_40F = "0001193125-23-285639"
RY_FY2023_REVENUE = 56_129_000_000.0
RY_FY2023_ACCEPTED = datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
RY_FY2023_RATE = 1.3871
RY_FY2023_RATE_DATE = date(2023, 10, 31)
RY_FY2023_RATE_KNOWN_AT = datetime(2023, 10, 31, 21, 0, tzinfo=UTC)

APPLE_TICKER = "AAPL"

FX_SERIES = "FXUSDCAD"
FX_FIRST_DATE = date(2017, 1, 3)

TEXT_FILINGS = 15
TEXT_EXHIBITS = 12

CIKS = ("0001000275", "0000320193", "0001594805")

# The sections of each hand-checked primary document, in document order, and
# the section each expected sentence of ``text_expected.json`` lands in (the
# file lists a document's sentences in order). The first two sentences of an
# earnings 8-K describe the item 2.02 disclosure, the exhibit sentence closes
# item 9.01, and Shopify's repurchase paragraph is item 7.01.
EXPECTED_SECTIONS: dict[str, list[str]] = {
    "0000320193-22-000006": ["2.02", "9.01"],
    "0000320193-26-000018": ["2.02", "9.01"],
    "0001594805-26-000006": ["2.02", "7.01", "9.01"],
}
EXPECTED_SENTENCE_SECTIONS: dict[str, list[str]] = {
    "0000320193-22-000006": ["2.02", "2.02", "9.01"],
    "0000320193-26-000018": ["2.02", "2.02", "9.01"],
    "0001594805-26-000006": ["2.02", "7.01", "9.01"],
}

pytestmark = pytest.mark.e2e


@dataclass(frozen=True)
class Build:
    """The built Phase 2 database and its output directory."""

    out_dir: Path
    db: Path


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Build:
    """Build the database once for the whole module from the fixture cache."""
    out_dir = tmp_path_factory.mktemp("phase2-build")
    return Build(out_dir=out_dir, db=build_from_raw(FIXTURES, out_dir))


@pytest.fixture(scope="module")
def con(built: Build) -> Iterator[duckdb.DuckDBPyConnection]:
    """A UTC connection to the built database, shared by the tests.

    ``build_from_raw`` runs dbt in this process and dbt-duckdb keeps the file
    attached with its default configuration, so the tests open the database the
    same way; every statement here is a read.
    """
    connection = duckdb.connect(str(built.db))
    connection.execute("SET TimeZone='UTC'")
    try:
        yield connection
    finally:
        connection.close()


def rows(
    con: duckdb.DuckDBPyConnection, sql: str, parameters: list[object] | None = None
) -> list[tuple[Any, ...]]:
    """Run one read-only query and return every row."""
    return con.execute(sql, parameters or []).fetchall()


def scalar(con: duckdb.DuckDBPyConnection, sql: str, parameters: list[object] | None = None) -> Any:
    """Run one aggregate query and return its single value."""
    result = rows(con, sql, parameters)
    assert len(result) == 1 and len(result[0]) == 1, sql
    return result[0][0]


def cache_documents(cache: RawCache, accession: str) -> dict[str, str]:
    """Map each cached document filename of ``accession`` to its CIK."""
    documents: dict[str, str] = {}
    for cik in CIKS:
        for key in cache.keys("documents", cik):
            key_accession, _, filename = key.partition("__")
            if key_accession == accession:
                documents[filename] = cik
    return documents


def test_build_produces_the_phase2_tables(con: duckdb.DuckDBPyConnection) -> None:
    for table in (
        "raw.filings_text",
        "raw.fx_daily",
        "main.filings_text",
        "main.fundamentals_pit_usd",
    ):
        assert scalar(con, f"select count(*) from {table}") > 0, table


def test_filings_text_covers_every_fixture_filing(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """A-40: one row per section, every primary split on its own items.

    The cache indexes exactly the 15 fixture 8-Ks. Each one appears in
    ``raw.filings_text``; its primary document's sections are the filing's own
    declared 8-K items in the order the filing lists them, and every other
    cached document of the filing is an ``EX-99.1`` with one ``unsegmented``
    row. That simultaneously proves every primary segmented confidently (no
    ``unsegmented`` fallback) and that no document is missing or repeated.
    """
    cache = RawCache(FIXTURES)
    indexed = {(cik, accession) for cik in CIKS for accession in cache.keys("filing_index", cik)}
    assert len(indexed) == TEXT_FILINGS

    text_accessions = {
        row[0] for row in rows(con, "select distinct accession from raw.filings_text")
    }
    assert text_accessions == {accession for _cik, accession in indexed}

    expected_rows = 0
    for _cik, accession in sorted(indexed):
        declared = scalar(con, "select items from raw.filings where accession = ?", [accession])
        declared_items = [item.strip() for item in (declared or "").split(",") if item.strip()]
        event_items = [
            row[0]
            for row in rows(
                con,
                "select item_code from main.events_8k where accession = ? order by item_code",
                [accession],
            )
        ]
        assert set(declared_items) == set(event_items), accession

        primary = rows(
            con,
            """
            select document, section
            from raw.filings_text
            where accession = ? and doc_type = 'primary'
            order by section_order
            """,
            [accession],
        )
        assert primary, accession
        primary_documents = {document for document, _section in primary}
        assert len(primary_documents) == 1, accession
        assert primary_documents <= set(cache_documents(cache, accession)), accession
        assert [section for _document, section in primary] == declared_items, accession

        exhibits = rows(
            con,
            """
            select document, section, section_order
            from raw.filings_text
            where accession = ? and doc_type = 'EX-99.1'
            """,
            [accession],
        )
        exhibit_documents = set(cache_documents(cache, accession)) - primary_documents
        assert {document for document, _section, _order in exhibits} == exhibit_documents, accession
        assert [(section, order) for _document, section, order in exhibits] == [
            ("unsegmented", 1)
        ] * len(exhibit_documents), accession
        expected_rows += len(declared_items) + len(exhibit_documents)

    assert scalar(con, "select count(*) from raw.filings_text") == expected_rows


def test_every_indexed_filing_has_a_primary_and_its_exhibits(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """The 15 primaries and their 12 ``EX-99.1`` exhibits are all rows."""
    document_key = "accession || '/' || document"
    assert (
        scalar(
            con,
            f"select count(distinct {document_key}) from raw.filings_text "
            "where doc_type = 'EX-99.1'",
        )
        == TEXT_EXHIBITS
    )
    assert (
        scalar(
            con,
            f"select count(distinct {document_key}) from raw.filings_text "
            "where doc_type = 'primary'",
        )
        == TEXT_FILINGS
    )
    assert scalar(con, "select count(distinct accession) from raw.filings_text") == TEXT_FILINGS
    assert (
        scalar(con, f"select count(distinct {document_key}) from raw.filings_text")
        == TEXT_FILINGS + TEXT_EXHIBITS
    )


def test_hand_checked_sentences_land_in_their_section(con: duckdb.DuckDBPyConnection) -> None:
    """The three hand-checked documents keep every sentence in the right item.

    Each sentence of ``text_expected.json`` must appear in exactly the section
    the item semantics put it in, so a segmentation that moved prose across an
    item boundary fails here even though every token is still in the table.
    """
    expected = json.loads(EXPECTED_TEXT.read_text(encoding="utf-8"))
    assert len(expected["documents"]) == 3

    for entry in expected["documents"]:
        sections = rows(
            con,
            """
            select section, text
            from raw.filings_text
            where accession = ? and document = ? and doc_type = 'primary'
            order by section_order
            """,
            [entry["accession"], entry["filename"]],
        )
        assert [section for section, _text in sections] == EXPECTED_SECTIONS[entry["accession"]]
        assert len(entry["sentences"]) == len(EXPECTED_SENTENCE_SECTIONS[entry["accession"]])
        for sentence, expected_section in zip(
            entry["sentences"], EXPECTED_SENTENCE_SECTIONS[entry["accession"]], strict=True
        ):
            containing = [section for section, text in sections if sentence in text]
            assert containing == [expected_section], (entry["filename"], sentence[:40])
        for heading in entry["item_headings"]:
            heading_text = next(
                text for section, text in sections if section == heading.removeprefix("Item ")
            )
            assert heading in heading_text, (entry["filename"], heading)


def test_acceptance_ts_equals_the_filings_row(con: duckdb.DuckDBPyConnection) -> None:
    """A section is known exactly when its filing was accepted (decision A-31)."""
    mismatched = rows(
        con,
        """
        select text.accession, text.acceptance_ts, filings.acceptance_ts
        from raw.filings_text as text
        left join raw.filings as filings on filings.accession = text.accession
        where filings.acceptance_ts is null
           or text.acceptance_ts is distinct from filings.acceptance_ts
           or text.ts_imputed is distinct from filings.ts_imputed
        """,
    )
    assert mismatched == []

    expected = rows(
        con,
        """
        select distinct text.accession, filings.acceptance_ts
        from raw.filings_text as text
        join raw.filings as filings on filings.accession = text.accession
        order by text.accession
        """,
    )
    assert len(expected) == TEXT_FILINGS
    for accession, acceptance in expected:
        assert rows(
            con,
            "select distinct acceptance_ts from raw.filings_text where accession = ?",
            [accession],
        ) == [(acceptance,)]


def test_fx_daily_holds_only_the_ingested_daily_series(con: duckdb.DuckDBPyConnection) -> None:
    """A-44: the ``FXUSDCAD`` series is ingested and ``IEXE0101`` never is."""
    assert rows(con, "select distinct series from raw.fx_daily") == [(FX_SERIES,)]
    assert scalar(con, "select count(*) from raw.fx_daily where series <> 'FXUSDCAD'") == 0
    assert scalar(con, "select min(rate_date) from raw.fx_daily") == FX_FIRST_DATE
    assert scalar(con, "select count(distinct rate_date) from raw.fx_daily") == scalar(
        con, "select count(*) from raw.fx_daily"
    )
    # 17:00 America/Toronto, never earlier: 22:00Z in winter, 21:00Z in summer.
    assert rows(
        con,
        """
        select count(*)
        from raw.fx_daily
        where extract(hour from timezone('America/Toronto', known_at)) <> 17
           or known_at <= cast(rate_date as timestamp) at time zone 'America/Toronto'
        """,
    ) == [(0,)]


def test_every_usd_row_is_converted_at_one(con: duckdb.DuckDBPyConnection) -> None:
    """A USD value needs no rate: it converts at 1.0 and is known at acceptance."""
    assert scalar(con, "select count(*) from main.fundamentals_pit_usd where currency = 'USD'") > 0
    assert rows(
        con,
        """
        select count(*)
        from main.fundamentals_pit_usd
        where currency = 'USD'
          and not (
              converted
              and fx_rate = 1.0
              and fx_rate_date is null
              and fx_known_at is null
              and value_usd = value
              and asof_ts = acceptance_ts
              and unconverted_reason is null
          )
        """,
    ) == [(0,)]


def test_every_cad_period_after_the_series_start_is_converted(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """A-32: a CAD period end on or after 2017-01-03 must find a rate."""
    assert scalar(con, "select count(*) from main.fundamentals_pit_usd where currency = 'CAD'") > 0
    assert rows(
        con,
        """
        select count(*)
        from main.fundamentals_pit_usd
        where currency = 'CAD' and period_end >= date '2017-01-03'
          and not (converted and fx_rate is not null and fx_rate_date is not null)
        """,
    ) == [(0,)]
    # The rate is the latest observation on or before the period end, and the
    # converted value is the reported value divided by it.
    assert rows(
        con,
        """
        select count(*)
        from main.fundamentals_pit_usd
        where currency = 'CAD' and converted
          and (fx_rate <= 0
               or fx_rate_date > period_end
               or abs(value_usd - value / fx_rate) > 1e-9 * abs(value / fx_rate))
        """,
    ) == [(0,)]
    # A CAD period before the series starts has no rate and is flagged, never
    # silently dropped or converted with a later rate (decision A-39).
    assert rows(
        con,
        """
        select count(*)
        from main.fundamentals_pit_usd
        where currency = 'CAD' and not converted
          and unconverted_reason <> 'no_rate'
        """,
    ) == [(0,)]
    assert rows(
        con,
        """
        select count(*)
        from main.fundamentals_pit_usd
        where currency = 'CAD' and unconverted_reason = 'no_rate'
          and period_end >= date '2017-01-03'
        """,
    ) == [(0,)]


def test_ry_fy2023_revenue_is_converted_at_the_period_end_rate(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """The reference restatement case converts at the 2023-10-31 observation."""
    conversions = rows(
        con,
        """
        select accession, acceptance_ts, fx_rate, fx_rate_date, fx_known_at, asof_ts,
               value, value_usd, converted
        from main.fundamentals_pit_usd
        where cik = ? and field = 'revenue' and period_end = date '2023-10-31'
        order by acceptance_ts
        """,
        [RY_CIK],
    )
    assert len(conversions) == 2
    original = conversions[0]
    assert original[0] == RY_FY2023_40F
    assert original[1] == RY_FY2023_ACCEPTED
    assert original[2] == RY_FY2023_RATE
    assert original[3] == RY_FY2023_RATE_DATE
    assert original[4] == RY_FY2023_RATE_KNOWN_AT
    # The rate was published before the 40-F was accepted, so the converted
    # value is known exactly at the filing's acceptance time.
    assert original[5] == RY_FY2023_ACCEPTED
    assert original[6] == RY_FY2023_REVENUE
    assert original[7] == pytest.approx(RY_FY2023_REVENUE / RY_FY2023_RATE, rel=1e-12)
    assert original[8] is True


def test_no_usd_row_is_known_before_its_rate(con: duckdb.DuckDBPyConnection) -> None:
    """The whole table obeys A-32's point-in-time rule, not just the RY case."""
    total = scalar(con, "select count(*) from main.fundamentals_pit_usd")
    assert total > 0
    assert rows(
        con,
        """
        select count(*)
        from main.fundamentals_pit_usd
        where asof_ts < acceptance_ts
           or (fx_known_at is not null and asof_ts < fx_known_at)
           or (fx_rate_date is not null and fx_rate_date > period_end)
        """,
    ) == [(0,)]
    assert (
        scalar(con, "select count(*) from main.fundamentals_pit_usd where fx_known_at is not null")
        > 0
    )


def test_the_ry_fy2023_value_becomes_visible_exactly_at_acceptance(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """``asof(currency='USD')`` never returns the USD value before its rate."""
    at_rows = query.asof(RY_CIK, "revenue", RY_FY2023_ACCEPTED, n=2, db=con, currency="USD")
    converted = next(row for row in at_rows if row.period_end == date(2023, 10, 31))
    assert converted.value == pytest.approx(RY_FY2023_REVENUE / RY_FY2023_RATE, rel=1e-12)
    assert converted.unit == "USD"
    assert converted.currency == "USD"
    assert converted.accession == RY_FY2023_40F
    assert converted.acceptance_ts == RY_FY2023_ACCEPTED

    before = query.asof(
        RY_CIK, "revenue", RY_FY2023_ACCEPTED - timedelta(seconds=1), n=2, db=con, currency="USD"
    )
    assert all(row.period_end != date(2023, 10, 31) for row in before)

    # The rate's known time is earlier than the acceptance time here, and the
    # query must still refuse the value before the filing was accepted.
    assert RY_FY2023_RATE_KNOWN_AT < RY_FY2023_ACCEPTED


def test_query_filing_text_returns_the_expected_sections(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """``query.filing_text`` surfaces the item 2.02 filings with all sections."""
    results = query.filing_text(APPLE_TICKER, item="2.02", db=con)
    assert results
    assert {row.ticker for row in results} == {APPLE_TICKER}

    by_accession: dict[str, list[query.TextResult]] = {}
    for result in results:
        by_accession.setdefault(result.accession, []).append(result)
    assert len(by_accession) == 8, sorted(by_accession)

    for accession, sections in by_accession.items():
        assert [section.section for section in sections if section.doc_type == "primary"] == [
            "2.02",
            "9.01",
        ], accession
        exhibits = [section for section in sections if section.doc_type == "EX-99.1"]
        assert [(section.section, section.section_order) for section in exhibits] == [
            ("unsegmented", 1)
        ], accession
        assert all(section.text.strip() for section in sections), accession
        acceptance = scalar(
            con,
            "select acceptance_ts from raw.filings where accession = ?",
            [accession],
        )
        assert {section.acceptance_ts for section in sections} == {acceptance}, accession
        assert [section.section_order for section in sections if section.doc_type == "primary"] == [
            1,
            2,
        ]


def test_qa_reports_no_key_violations(con: duckdb.DuckDBPyConnection) -> None:
    """The Phase 2 tables pass the QA keys and the point-in-time check."""
    report = run_qa(con)

    assert report.key_violations == []
    assert report.usd_conversion.asof_violation_count == 0
    assert report.ok

    assert report.usd_conversion.built
    assert report.text_coverage.built
    assert report.fx_coverage.built
    assert report.fx_coverage.by_series[0].series == FX_SERIES
    assert [series.series for series in report.fx_coverage.by_series] == [FX_SERIES]

    # Every primary document segmented confidently: no fallback rows at all.
    assert report.text_coverage.primary_documents == TEXT_FILINGS
    assert report.text_coverage.unsegmented_primaries == 0
    assert report.text_coverage.zero_length_sections == 0
    item_202_filings = scalar(
        con,
        "select count(distinct accession) from main.events_8k where item_code = '2.02'",
    )
    assert report.text_coverage.item_202_filings == item_202_filings
    assert report.text_coverage.item_202_without_text == item_202_filings - 12


def test_export_twice_is_byte_identical(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    """The Phase 2 release is a deterministic function of the database."""
    names: list[str] = []
    exports: list[dict[str, bytes]] = []
    for name in ("first", "second"):
        out_dir = tmp_path / name
        manifest = export_release(con, out_dir)
        files = sorted(path.name for path in out_dir.glob("*.parquet"))
        assert len(files) == len(EXPORTS) + len(PHASE2_EXPORTS) == 8
        assert {entry.table for entry in manifest.files} == {
            table.name for table in (*EXPORTS, *PHASE2_EXPORTS)
        }
        names = files
        exports.append({file: (out_dir / file).read_bytes() for file in files})

    assert exports[0].keys() == exports[1].keys()
    for name in names:
        assert exports[0][name] == exports[1][name], name
