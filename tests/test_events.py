"""8-K item labels and event explosion.

Fixture tests read the real SEC responses under ``tests/fixtures/sec`` through
:class:`edgarpit.rawcache.RawCache`; synthetic frames cover the labelling edge
cases. No test here touches the network.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from edgarpit.ingest.headers import HeaderInfo, apply_acceptance, parse_header
from edgarpit.ingest.submissions import explode_8k_items, load_item_labels, parse_submissions
from edgarpit.rawcache import RawCache
from edgarpit.schemas import Events8kSchema, FilingsSchema

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
CACHE = RawCache(FIXTURES)

RY_CIK = "0001000275"
APPLE_CIK = "0000320193"
SHOP_CIK = "0001594805"
CIKS = (RY_CIK, APPLE_CIK, SHOP_CIK)

APPLE_8K = "0000320193-26-000018"
EVENT_FORMS = ("8-K", "8-K/A")

MODERN_CODES = tuple(
    f"{section}.{item:02d}"
    for section, count in ((1, 5), (2, 6), (3, 3), (4, 2), (5, 8), (6, 6), (7, 1), (8, 1), (9, 1))
    for item in range(1, count + 1)
)
LEGACY_CODES = tuple(str(number) for number in range(1, 13))


def cached_text(endpoint: str, cik: str, key: str | None = None) -> str:
    body = CACHE.latest(endpoint, cik, key)
    assert body is not None, f"no cached {endpoint} for {cik}/{key}"
    return body.decode("utf-8")


def cached_json(endpoint: str, cik: str, key: str | None = None) -> dict[str, Any]:
    document = json.loads(cached_text(endpoint, cik, key))
    assert isinstance(document, dict)
    return document


def fixture_filings(cik: str) -> tuple[pl.DataFrame, dict[str, HeaderInfo]]:
    main = cached_json("submissions", cik)
    pages = [
        cached_json("submissions_page", cik, key) for key in CACHE.keys("submissions_page", cik)
    ]
    headers = {
        accession: parse_header(cached_text("headers", cik, accession))
        for accession in CACHE.keys("headers", cik)
    }
    frame = apply_acceptance(parse_submissions(main, pages), headers)
    FilingsSchema.validate(frame)
    return frame, headers


def fixture_events(cik: str) -> pl.DataFrame:
    frame, _ = fixture_filings(cik)
    return explode_8k_items(frame, load_item_labels())


def synthetic_filings(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """A frame with only the columns ``explode_8k_items`` reads."""
    schema: dict[str, pl.DataType | type[pl.DataType]] = {
        "cik": pl.String,
        "accession": pl.String,
        "form": pl.String,
        "acceptance_ts": pl.Datetime("us", "UTC"),
        "ts_imputed": pl.Boolean,
        "items": pl.String,
    }
    columns: dict[str, list[Any]] = {name: [] for name in schema}
    for row in rows:
        for name in schema:
            columns[name].append(row.get(name))
    return pl.DataFrame(columns, schema=schema)


def test_modern_item_codes_carry_official_titles() -> None:
    labels = load_item_labels()
    assert set(MODERN_CODES) <= set(labels)
    assert set(LEGACY_CODES) <= set(labels)
    assert labels["2.02"] == "Results of Operations and Financial Condition"
    assert labels["9.01"] == "Financial Statements and Exhibits"
    assert labels["1.04"] == "Mine Safety \u2013 Reporting of Shutdowns and Patterns of Violations"
    assert labels["5"] == "Other Events"
    assert labels["12"] == "Results of Operations and Financial Condition"
    assert all(label != "unknown" for label in labels.values())


def test_load_item_labels_from_a_custom_file(tmp_path: Path) -> None:
    path = tmp_path / "8k_items.yaml"
    path.write_text(
        "items:\n  '1.01': Entry into a Material Definitive Agreement\n"
        "legacy:\n  '5': Other Events\n",
        encoding="utf-8",
    )
    assert load_item_labels(path) == {
        "1.01": "Entry into a Material Definitive Agreement",
        "5": "Other Events",
    }


@pytest.mark.parametrize(
    "text",
    [
        "legacy:\n  '5': Other Events\n",  # no items section
        "items:\n  '1.01': ''\n",  # empty label
        "items:\n  'Item 1.01': One\n",  # not an item code
        "items:\n  '1.01': One\nlegacy:\n  '1.01': One\n",  # duplicate across sections
    ],
)
def test_bad_item_label_files_raise(text: str, tmp_path: Path) -> None:
    path = tmp_path / "8k_items.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_item_labels(path)


def test_missing_item_label_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read item labels"):
        load_item_labels(tmp_path / "missing.yaml")


def test_apple_8k_item_2_02_events_have_labels() -> None:
    events = fixture_events(APPLE_CIK)
    labels = load_item_labels()
    filing_events = events.filter(pl.col("accession") == APPLE_8K)
    codes = set(filing_events["item_code"])
    assert codes == {"2.02", "9.01"}
    labels_by_code = dict(zip(filing_events["item_code"], filing_events["item_label"], strict=True))
    assert labels_by_code["2.02"] == labels["2.02"]
    assert labels_by_code["9.01"] == labels["9.01"]
    assert filing_events["item_label"].null_count() == 0


def test_item_codes_match_the_headers_for_every_fixture_8k() -> None:
    checked = 0
    for cik in CIKS:
        frame, headers = fixture_filings(cik)
        events = explode_8k_items(frame, load_item_labels())
        event_codes: dict[str, set[str]] = {}
        for row in events.iter_rows(named=True):
            event_codes.setdefault(row["accession"], set()).add(row["item_code"])
        eight_ks = frame.filter(pl.col("form").is_in(EVENT_FORMS))
        for row in eight_ks.iter_rows(named=True):
            info: HeaderInfo = headers[row["accession"]]
            header_codes = set((info.items or "").split(",")) - {""}
            assert event_codes.get(row["accession"], set()) == header_codes, row["accession"]
            checked += 1
    assert checked >= 50, f"only {checked} fixture 8-Ks cross-checked"


def test_events_carry_the_filing_timestamp() -> None:
    frame, _ = fixture_filings(APPLE_CIK)
    events = explode_8k_items(frame, load_item_labels())
    by_accession = {row["accession"]: row for row in frame.iter_rows(named=True)}
    for row in events.iter_rows(named=True):
        filing = by_accession[row["accession"]]
        assert row["acceptance_ts"] == filing["acceptance_ts"]
        assert row["ts_imputed"] == filing["ts_imputed"]
        assert row["cik"] == filing["cik"]
        assert row["form"] == filing["form"]


def test_events_valid_and_unique_on_fixtures() -> None:
    for cik in CIKS:
        events = fixture_events(cik)
        assert events.height > 0 or cik == RY_CIK
        keys = list(zip(events["accession"], events["item_code"], strict=True))
        assert len(keys) == len(set(keys))
        assert (events["item_label"] == "unknown").sum() == 0
        Events8kSchema.validate(events)


def test_unknown_and_spaced_codes_are_labelled_unknown() -> None:
    frame = synthetic_filings(
        [
            {
                "cik": "0000000000",
                "accession": "0000000000-24-000001",
                "form": "8-K",
                "acceptance_ts": datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
                "ts_imputed": False,
                "items": " 2.02 , 99.99, ,5.02,5.02 ",
            }
        ]
    )
    events = explode_8k_items(frame, load_item_labels())
    assert list(zip(events["item_code"], events["item_label"], strict=True)) == [
        ("2.02", "Results of Operations and Financial Condition"),
        ("99.99", "unknown"),
        ("5.02", load_item_labels()["5.02"]),
    ]


def test_eight_ks_without_items_produce_no_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    frame = synthetic_filings(
        [
            {
                "cik": "0000000000",
                "accession": "0000000000-24-000001",
                "form": "8-K",
                "acceptance_ts": datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
                "ts_imputed": False,
                "items": None,
            },
            {
                "cik": "0000000000",
                "accession": "0000000000-24-000002",
                "form": "8-K/A",
                "acceptance_ts": datetime(2024, 1, 3, 21, 0, tzinfo=UTC),
                "ts_imputed": True,
                "items": " , ",
            },
            {
                "cik": "0000000000",
                "accession": "0000000000-24-000003",
                "form": "10-K",
                "acceptance_ts": datetime(2024, 1, 4, 21, 0, tzinfo=UTC),
                "ts_imputed": False,
                "items": "2.02",
            },
        ]
    )
    with caplog.at_level(logging.INFO, logger="edgarpit.ingest.submissions"):
        events = explode_8k_items(frame, load_item_labels())
    assert events.height == 0
    assert list(events.columns) == [
        "cik",
        "accession",
        "form",
        "acceptance_ts",
        "ts_imputed",
        "item_code",
        "item_label",
    ]
    assert "2 filings carry no items" in caplog.text
