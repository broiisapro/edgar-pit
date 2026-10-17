"""SGML header parsing, acceptance attachment and required-accession selection.

The fixture tree is the real SEC responses under ``tests/fixtures/sec``, read
through :class:`edgarpit.rawcache.RawCache` as an offline cache. No test here
touches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from edgarpit.ingest.headers import (
    NEW_YORK,
    HeaderInfo,
    apply_acceptance,
    parse_header,
    required_accessions,
)
from edgarpit.rawcache import RawCache
from edgarpit.schemas import FilingsSchema

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
CACHE = RawCache(FIXTURES)

RY_CIK = "0001000275"
APPLE_CIK = "0000320193"
SHOP_CIK = "0001594805"
CIKS = (RY_CIK, APPLE_CIK, SHOP_CIK)

RY_FY2023_40F = "0001193125-23-285639"
APPLE_8K = "0000320193-26-000018"

_HEADER_TEMPLATE = """<SEC-HEADER>0000000000-00-000000.hdr.sgml : 20240101
<ACCEPTANCE-DATETIME>{acceptance}
<ACCESSION-NUMBER>0000000000-00-000000
<TYPE>8-K
<PERIOD>20240101
<FILING-DATE>20240101
</SEC-HEADER>
"""

_FRAME_DTYPES: dict[str, type[pl.DataType]] = {
    "cik": pl.String,
    "accession": pl.String,
    "form": pl.String,
    "filing_date": pl.Date,
    "report_date": pl.Date,
    "acceptance_ts": pl.Datetime("us", "UTC"),
    "acceptance_ts_source": pl.String,
    "acceptance_ts_api": pl.Datetime("us", "UTC"),
    "ts_imputed": pl.Boolean,
    "primary_document": pl.String,
    "items": pl.String,
    "is_amendment": pl.Boolean,
    "is_xbrl": pl.Boolean,
    "source": pl.String,
}


def cached_text(endpoint: str, cik: str, key: str | None = None) -> str:
    body = CACHE.latest(endpoint, cik, key)
    assert body is not None, f"no cached {endpoint} for {cik}/{key}"
    return body.decode("utf-8")


def header_info(cik: str, accession: str) -> HeaderInfo:
    return parse_header(cached_text("headers", cik, accession))


def header_text(acceptance: str, period: str = "20240101") -> str:
    return _HEADER_TEMPLATE.format(acceptance=acceptance).replace(
        "<PERIOD>20240101", f"<PERIOD>{period}"
    )


def is_skipped(local: datetime) -> bool:
    """True when ``local`` is in the hour DST skips in America/New_York."""
    for fold in (0, 1):
        aware = local.replace(tzinfo=NEW_YORK, fold=fold)
        if aware.astimezone(UTC).astimezone(NEW_YORK).replace(tzinfo=None) == local:
            return False
    return True


def synthetic_filings(rows: list[tuple[str, date]], cik: str = "0000000000") -> pl.DataFrame:
    """A minimal valid ``filings`` frame: ``(accession, filing_date)`` rows."""
    columns: dict[str, list[object]] = {name: [] for name in _FRAME_DTYPES}
    for accession, filing_date in rows:
        values: dict[str, object] = {
            "cik": cik,
            "accession": accession,
            "form": "10-K",
            "filing_date": filing_date,
            "report_date": None,
            "acceptance_ts": None,
            "acceptance_ts_source": None,
            "acceptance_ts_api": None,
            "ts_imputed": None,
            "primary_document": None,
            "items": None,
            "is_amendment": False,
            "is_xbrl": True,
            "source": "recent",
        }
        for name in _FRAME_DTYPES:
            columns[name].append(values[name])
    return pl.DataFrame(columns, schema=_FRAME_DTYPES)


def fixture_filing_forms(cik: str) -> dict[str, str]:
    """Map every fixture submissions row's accession to its form (recent first)."""
    main = json.loads(cached_text("submissions", cik))
    rows: dict[str, str] = {}
    recent = main["filings"]["recent"]
    for index, accession in enumerate(recent["accessionNumber"]):
        rows[accession] = recent["form"][index]
    for page_key in CACHE.keys("submissions_page", cik):
        page = json.loads(cached_text("submissions_page", cik, page_key))
        for index, accession in enumerate(page["accessionNumber"]):
            rows.setdefault(accession, page["form"][index])
    return rows


def fixture_fact_accessions(cik: str) -> set[str]:
    """Every accession referenced by a ``us-gaap``/``ifrs-full`` fixture fact."""
    document = json.loads(cached_text("companyfacts", cik))
    accessions: set[str] = set()
    for taxonomy in ("us-gaap", "ifrs-full"):
        for entry in document["facts"].get(taxonomy, {}).values():
            for unit_rows in entry["units"].values():
                for row in unit_rows:
                    accessions.add(row["accn"])
    return accessions


def test_every_fixture_header_parses_and_matches_its_directory() -> None:
    total = 0
    for cik in CIKS:
        accessions = CACHE.keys("headers", cik)
        assert accessions, f"no cached headers for {cik}"
        for accession in accessions:
            info = header_info(cik, accession)
            assert info.accession == accession
            assert info.acceptance_ts.utcoffset() == timedelta(0)
            assert info.form
            assert info.items is None or info.items
            # Every fixture header carries exactly one non-empty <PERIOD>.
            assert info.period is not None
            total += 1
    assert total > 400, f"only {total} fixture headers"


def test_royal_bank_40f_acceptance_is_utc() -> None:
    # 2023-11-30 06:45:12 New York (EST, UTC-5) is 11:45:12Z.
    info = header_info(RY_CIK, RY_FY2023_40F)
    assert info.acceptance_ts == datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
    assert info.filing_date == date(2023, 11, 30)
    assert info.period == date(2023, 10, 31)
    assert info.form == "40-F"


def test_daylight_time_acceptance_uses_the_four_hour_offset() -> None:
    # Apple's 2026-07-30 8-K was accepted 16:30:28 New York (EDT, UTC-4).
    info = header_info(APPLE_CIK, APPLE_8K)
    assert info.acceptance_ts == datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC)
    assert info.items == "2.02,9.01"
    assert info.period == date(2026, 7, 30)


def test_ambiguous_fall_back_hour_resolves_to_the_later_utc_instant() -> None:
    # 2024-11-03 01:30 exists twice: 05:30Z as EDT and 06:30Z as EST.
    info = parse_header(header_text("20241103013000"))
    assert info.acceptance_ts == datetime(2024, 11, 3, 6, 30, tzinfo=UTC)


def test_skipped_spring_forward_hour_raises() -> None:
    # 2024-03-10 02:30 never existed in New York.
    with pytest.raises(ValueError, match=r"does not exist|<ACCEPTANCE-DATETIME>"):
        parse_header(header_text("20240310023000"))


@given(
    local=st.datetimes(
        min_value=datetime(1990, 1, 1), max_value=datetime(2040, 12, 31, 23, 59, 59)
    ).map(lambda moment: moment.replace(microsecond=0))
)
def test_any_valid_new_york_wall_time_round_trips(local: datetime) -> None:
    text = header_text(local.strftime("%Y%m%d%H%M%S"))
    if is_skipped(local):
        with pytest.raises(ValueError):
            parse_header(text)
        return
    info = parse_header(text)
    back = info.acceptance_ts.astimezone(NEW_YORK).replace(tzinfo=None)
    assert back == local


def test_period_is_optional_and_items_join_in_file_order() -> None:
    text = header_text("20240101120000", period="")
    info = parse_header(text)
    assert info.period is None
    assert info.items is None
    with_items = text.replace("<PERIOD>", "<ITEMS>1.01\n<ITEMS>9.01\n<PERIOD>")
    assert parse_header(with_items).items == "1.01,9.01"


VALID_HEADER = header_text("20240101120000")

_BROKEN_HEADERS = [
    (VALID_HEADER.replace("<ACCEPTANCE-DATETIME>20240101120000\n", ""), "ACCEPTANCE-DATETIME"),
    (VALID_HEADER.replace("<ACCESSION-NUMBER>0000000000-00-000000", ""), "ACCESSION-NUMBER"),
    (VALID_HEADER.replace("<TYPE>8-K", ""), "TYPE"),
    (VALID_HEADER.replace("<FILING-DATE>20240101", ""), "FILING-DATE"),
    (header_text("not-a-stamp"), "ACCEPTANCE-DATETIME"),
    (header_text("20240230120000"), "ACCEPTANCE-DATETIME"),
    (VALID_HEADER.replace("<FILING-DATE>20240101", "<FILING-DATE>2024-01-01"), "FILING-DATE"),
    (VALID_HEADER.replace("<PERIOD>20240101", "<PERIOD>2024"), "PERIOD"),
]


@pytest.mark.parametrize(("text", "tag"), _BROKEN_HEADERS)
def test_missing_or_malformed_tags_raise_naming_the_tag(text: str, tag: str) -> None:
    with pytest.raises(ValueError, match=f"<{tag}>"):
        parse_header(text)


def test_apply_acceptance_prefers_the_header() -> None:
    filings = synthetic_filings([("0001193125-23-285639", date(2023, 11, 30))])
    result = apply_acceptance(filings, {RY_FY2023_40F: header_info(RY_CIK, RY_FY2023_40F)})
    row = result.row(0, named=True)
    assert row["acceptance_ts"] == datetime(2023, 11, 30, 11, 45, 12, tzinfo=UTC)
    assert row["acceptance_ts_source"] == "sgml_header"
    assert row["ts_imputed"] is False
    FilingsSchema.validate(result)


@pytest.mark.parametrize(
    ("filing_date", "expected"),
    [
        (date(2010, 1, 25), datetime(2010, 1, 26, 4, 59, 59, tzinfo=UTC)),
        # DST transition days: 23:59:59 local is EDT on 2024-03-10 and EST on
        # 2024-11-03, so a fixed offset or a UTC-side addition is wrong.
        (date(2024, 3, 10), datetime(2024, 3, 11, 3, 59, 59, tzinfo=UTC)),
        (date(2024, 11, 3), datetime(2024, 11, 4, 4, 59, 59, tzinfo=UTC)),
    ],
)
def test_apply_acceptance_falls_back_to_filing_date_end_of_day(
    filing_date: date, expected: datetime
) -> None:
    filings = synthetic_filings([("0000000000-00-000001", filing_date)])
    result = apply_acceptance(filings, {})
    row = result.row(0, named=True)
    assert row["acceptance_ts"] == expected
    assert row["acceptance_ts_source"] == "filing_date_eod"
    assert row["ts_imputed"] is True
    FilingsSchema.validate(result)


def test_apply_acceptance_leaves_unmatched_filings_on_the_fallback() -> None:
    filings = synthetic_filings(
        [
            ("0001193125-23-285639", date(2023, 11, 30)),
            ("0000000000-00-000001", date(2024, 1, 2)),
        ]
    )
    result = apply_acceptance(filings, {RY_FY2023_40F: header_info(RY_CIK, RY_FY2023_40F)})
    sources = dict(zip(result["accession"], result["acceptance_ts_source"], strict=True))
    assert sources[RY_FY2023_40F] == "sgml_header"
    assert sources["0000000000-00-000001"] == "filing_date_eod"


def test_required_accessions_units() -> None:
    filings = synthetic_filings(
        [
            ("0000000000-00-000001", date(2024, 1, 2)),  # 10-K, not a fact
            ("0000000000-00-000002", date(2024, 1, 3)),  # 8-K, also a fact
        ]
    )
    filings = filings.with_columns(
        pl.when(pl.col("accession") == "0000000000-00-000002")
        .then(pl.lit("8-K/A"))
        .otherwise(pl.col("form"))
        .alias("form")
    )
    result = required_accessions(
        filings,
        [("0000000000", "0000000000-00-000002"), ("0000000000", "0000000000-00-000003")],
    )
    assert result == [
        ("0000000000", "0000000000-00-000002"),
        ("0000000000", "0000000000-00-000003"),
    ]
    assert required_accessions(filings, []) == [("0000000000", "0000000000-00-000002")]


def test_required_accessions_on_fixtures_covers_facts_and_eight_ks() -> None:
    total_eight_ks = 0
    for cik in CIKS:
        forms = fixture_filing_forms(cik)
        filings = synthetic_filings([(accession, date(2024, 1, 1)) for accession in forms], cik=cik)
        filings = filings.with_columns(
            pl.col("accession").replace_strict(forms, return_dtype=pl.String).alias("form")
        )
        facts = {(cik, accession) for accession in fixture_fact_accessions(cik)}
        result = required_accessions(filings, facts)
        assert list(result) == sorted(set(result))
        assert facts <= set(result)
        eight_ks = {
            (cik, accession) for accession, form in forms.items() if form in ("8-K", "8-K/A")
        }
        assert eight_ks <= set(result)
        total_eight_ks += len(eight_ks)
    assert total_eight_ks >= 20, f"only {total_eight_ks} fixture 8-Ks"
