"""Tests for the Bank of Canada Valet parser and FX fetch step (decision A-32).

The fixtures are the real ``FXUSDCAD`` history under ``tests/fixtures/sec/raw``
(decisions A-44) plus the pre-2017 noon series ``IEXE0101`` under
``tests/fixtures/fx/raw``, which stays as evidence only and is never ingested
(decision A-39). ``parse_valet`` turns one document into ``raw.fx_daily`` rows
and pins the point in time: a rate for date ``d`` is known at ``d`` 17:00
``America/Toronto`` in UTC, 21:00 UTC under daylight saving and 22:00 UTC in
winter. ``fetch_fx`` stores a fetched series only when its body changed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import polars as pl
import pytest
import respx
from hypothesis import given, settings
from hypothesis import strategies as st

from edgarpit.config import Settings
from edgarpit.fx.valet import FxFetchReport, ValetClient, fetch_fx, parse_valet
from edgarpit.rawcache import RawCache
from edgarpit.schemas import FxDailySchema

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SEC_FX_ROOT = FIXTURE_ROOT / "sec"
EVIDENCE_FX_ROOT = FIXTURE_ROOT / "fx"
EXPECTED_PATH = FIXTURE_ROOT / "fx_expected.json"

SERIES = "FXUSDCAD"
NOON_SERIES = "IEXE0101"
# The ingested daily series lives in the merged SEC cache; the retired noon
# series is evidence only and stays in its own root (decisions A-39, A-44).
SERIES_ROOTS = {SERIES: SEC_FX_ROOT, NOON_SERIES: EVIDENCE_FX_ROOT}
VALET_URL = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
TORONTO = ZoneInfo("America/Toronto")

# 50 examples keep the property suite near a second on the shared build
# machine; the deadline is off because a Polars/Pandera pass can exceed the
# 200 ms default under load without any behavioural difference.
PROPERTY_SETTINGS = settings(deadline=None, max_examples=50)


class FakeClock:
    """Monotonic clock whose ``sleep`` advances it, so no test waits for the bucket."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_settings() -> Settings:
    return Settings(sec_user_agent="Test User test@example.com")


def make_client() -> ValetClient:
    clock = FakeClock()
    return ValetClient(make_settings(), clock=clock.monotonic, sleep=clock.sleep)


def read_fixture(series: str = SERIES) -> dict[str, Any]:
    """Read the newest committed document for ``series`` through the raw cache."""
    body = RawCache(SERIES_ROOTS[series]).latest("fx", "", series)
    assert body is not None
    doc = json.loads(body)
    assert isinstance(doc, dict)
    return doc


def stored_document(data_dir: Path) -> dict[str, Any]:
    """Return the newest cached body for ``FXUSDCAD`` as parsed JSON."""
    body = RawCache(data_dir).latest("fx", "", SERIES)
    assert body is not None
    doc = json.loads(body)
    assert isinstance(doc, dict)
    return doc


def make_document(observations: list[dict[str, Any]], series: str = SERIES) -> dict[str, Any]:
    """Wrap observations in the envelope Valet serves (series detail included)."""
    return {
        "terms": {"url": "https://www.bankofcanada.ca/terms/"},
        "seriesDetail": {series: {"label": "USD/CAD", "dimension": {"key": "d"}}},
        "observations": observations,
    }


def rate_on_or_before(frame: pl.DataFrame, period_end: date) -> tuple[date | None, float | None]:
    """The latest observation dated on or before ``period_end`` (decision A-32).

    This is the lookup a conversion uses; it is written here, in the test, so the
    package stays a parser and the rule is checked against the fixture data.
    """
    candidates = frame.filter(pl.col("rate_date") <= period_end)
    if candidates.is_empty():
        return None, None
    row = candidates.sort("rate_date").row(-1, named=True)
    return row["rate_date"], row["value"]


def test_fixture_parses_into_fx_daily_rows() -> None:
    frame = parse_valet(read_fixture())

    assert frame.height == 2449
    assert frame.columns == ["series", "rate_date", "value", "known_at", "source"]
    assert frame["series"].unique().to_list() == [SERIES]
    assert frame["source"].unique().to_list() == ["valet"]
    assert frame["rate_date"].min() == date(2017, 1, 3)
    assert frame["rate_date"].max() == date(2026, 10, 27)
    assert frame["value"].min() is not None and frame["value"].min() > 0
    assert frame["rate_date"].n_unique() == frame.height
    assert frame["rate_date"].is_sorted()
    FxDailySchema.validate(frame)


def test_known_at_is_five_pm_toronto_in_winter_and_summer() -> None:
    frame = parse_valet(
        make_document(
            [
                {"d": "2024-01-15", "FXUSDCAD": {"v": "1.3400"}},
                {"d": "2024-07-15", "FXUSDCAD": {"v": "1.3700"}},
            ]
        )
    )

    assert frame["known_at"].to_list() == [
        datetime(2024, 1, 15, 22, 0, tzinfo=UTC),  # EST, UTC-5
        datetime(2024, 7, 15, 21, 0, tzinfo=UTC),  # EDT, UTC-4
    ]
    assert frame["known_at"].dt.convert_time_zone("America/Toronto").dt.hour().to_list() == [
        17,
        17,
    ]


def test_known_at_follows_the_daylight_saving_change() -> None:
    frame = parse_valet(
        make_document(
            [
                {"d": "2024-03-08", "FXUSDCAD": {"v": "1.3455"}},
                {"d": "2024-03-11", "FXUSDCAD": {"v": "1.3480"}},
            ]
        )
    )

    # 2024-03-10 02:00 Toronto jumps from EST to EDT, so the offset changes but
    # the local known time stays 17:00.
    assert frame["known_at"].to_list() == [
        datetime(2024, 3, 8, 22, 0, tzinfo=UTC),
        datetime(2024, 3, 11, 21, 0, tzinfo=UTC),
    ]


def test_expected_conversion_cases_use_the_latest_rate_on_or_before() -> None:
    frame = parse_valet(read_fixture())
    cases = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    assert len(cases) == 6  # the hand-checked cases of decision A-32

    for case in cases:
        rate_date, rate = rate_on_or_before(frame, date.fromisoformat(case["period_end"]))
        expected_date = (
            None
            if case["expected_rate_date"] is None
            else date.fromisoformat(case["expected_rate_date"])
        )
        assert rate_date == expected_date, case["period_end"]
        assert rate == case["expected_rate"], case["period_end"]


def test_value_is_parsed_from_the_decimal_string() -> None:
    frame = parse_valet(
        make_document([{"d": "2023-10-31", "FXUSDCAD": {"v": "1.3871"}}]),
    )

    assert frame["value"].to_list() == [1.3871]
    assert frame["value"].dtype == pl.Float64


def test_observations_without_a_value_are_skipped_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    doc = make_document(
        [
            {"d": "2023-10-27", "FXUSDCAD": {"v": "1.3875"}},
            {"d": "2023-10-28", "FXUSDCAD": {"v": ""}},
            {"d": "2023-10-29", "FXUSDCAD": None},
            {"d": "2023-10-30"},
            {"d": "2023-10-31", "FXUSDCAD": {"v": "1.3659"}},
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="edgarpit.fx.valet"):
        frame = parse_valet(doc)

    assert frame["rate_date"].to_list() == [date(2023, 10, 27), date(2023, 10, 31)]
    assert "skipped 3 FXUSDCAD observations without a value" in caplog.text


def test_empty_observations_yield_an_empty_frame() -> None:
    frame = parse_valet(make_document([]))

    assert frame.height == 0
    assert frame.columns == ["series", "rate_date", "value", "known_at", "source"]
    FxDailySchema.validate(frame)


def test_rows_are_sorted_by_rate_date() -> None:
    frame = parse_valet(
        make_document(
            [
                {"d": "2023-10-31", "FXUSDCAD": {"v": "1.3659"}},
                {"d": "2023-10-27", "FXUSDCAD": {"v": "1.3875"}},
                {"d": "2023-10-30", "FXUSDCAD": {"v": "1.3831"}},
            ]
        )
    )

    assert frame["rate_date"].to_list() == [
        date(2023, 10, 27),
        date(2023, 10, 30),
        date(2023, 10, 31),
    ]


GOOD_OBSERVATION = {"d": "2023-10-31", "FXUSDCAD": {"v": "1.3659"}}


@pytest.mark.parametrize(
    ("doc", "match"),
    [
        ([], "not a JSON object"),
        ({"observations": [GOOD_OBSERVATION]}, "does not describe series"),
        (
            {"seriesDetail": {"IEXE0101": {}}, "observations": [GOOD_OBSERVATION]},
            "does not describe series",
        ),
        ({"seriesDetail": {"FXUSDCAD": {}}}, "no 'observations' list"),
        ({"seriesDetail": {"FXUSDCAD": {}}, "observations": {}}, "no 'observations' list"),
        (make_document([{"FXUSDCAD": {"v": "1.3659"}}]), "has no 'd' date"),
        (make_document([{"d": "31/10/2023", "FXUSDCAD": {"v": "1.3659"}}]), "invalid date"),
        (make_document([{"d": "2023-10-31", "FXUSDCAD": {"v": "n/a"}}]), "is not a number"),
        (make_document([{"d": "2023-10-31", "FXUSDCAD": {"v": "0"}}]), "finite positive"),
        (make_document([{"d": "2023-10-31", "FXUSDCAD": {"v": "-1.2"}}]), "finite positive"),
        (make_document([{"d": "2023-10-31", "FXUSDCAD": {"v": "nan"}}]), "finite positive"),
        (make_document([{"d": "2023-10-31", "FXUSDCAD": {"v": "inf"}}]), "finite positive"),
        (make_document([{"d": "2023-10-31", "FXUSDCAD": "1.3659"}]), "is not an object"),
        (
            make_document([GOOD_OBSERVATION, GOOD_OBSERVATION]),
            "repeats the date 2023-10-31",
        ),
    ],
)
def test_malformed_documents_raise(doc: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_valet(doc)


def test_a_value_error_names_the_offending_date() -> None:
    doc = make_document(
        [
            {"d": "2023-10-30", "FXUSDCAD": {"v": "1.3831"}},
            {"d": "2023-10-31", "FXUSDCAD": {"v": "one point three"}},
        ]
    )

    with pytest.raises(ValueError, match="2023-10-31"):
        parse_valet(doc)


def test_another_series_document_does_not_parse_as_fxusdcad() -> None:
    # IEXE0101 is the pre-2017 noon series kept as evidence (decision A-39): it
    # must never be read as a USD/CAD series, which an empty frame would allow.
    with pytest.raises(ValueError, match="does not describe series 'FXUSDCAD'"):
        parse_valet(read_fixture(NOON_SERIES))

    noon = parse_valet(read_fixture(NOON_SERIES), NOON_SERIES)
    assert noon["series"].unique().to_list() == [NOON_SERIES]
    assert noon["rate_date"].max() == date(2017, 4, 28)


@respx.mock(assert_all_called=False)
def test_fetch_fx_stores_a_new_series_once(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    doc = make_document([{"d": "2023-10-31", "FXUSDCAD": {"v": "1.3659"}}])
    route = respx_mock.get(VALET_URL).mock(return_value=httpx.Response(200, json=doc))

    with make_client() as client:
        first = fetch_fx(tmp_path, client=client)
        assert first == FxFetchReport(
            series=SERIES,
            observations=1,
            first_date=date(2023, 10, 31),
            last_date=date(2023, 10, 31),
            stored=True,
        )
        assert route.call_count == 1

        second = fetch_fx(tmp_path, client=client)

    assert second.stored is False
    assert second.observations == 1
    assert route.call_count == 2  # one request per call, the series is re-fetched
    stored = sorted((tmp_path / "raw" / "fx" / "_" / SERIES).iterdir())
    assert len(stored) == 1
    manifest = RawCache(tmp_path).manifest_rows()
    assert len(manifest) == 1
    assert manifest[0]["endpoint"] == "fx"
    assert manifest[0]["cik"] == ""
    assert manifest[0]["url"] == VALET_URL
    assert stored_document(tmp_path) == doc
    # The body is stored exactly as received, which is what makes the SHA-256
    # comparison in the next run meaningful.
    assert RawCache(tmp_path).latest("fx", "", SERIES) == route.calls.last.response.content


@respx.mock(assert_all_called=False)
def test_fetch_fx_stores_a_changed_body(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    first_doc = make_document([{"d": "2023-10-31", "FXUSDCAD": {"v": "1.3659"}}])
    second_doc = make_document(
        [
            {"d": "2023-10-31", "FXUSDCAD": {"v": "1.3659"}},
            {"d": "2023-11-01", "FXUSDCAD": {"v": "1.3720"}},
        ]
    )
    route = respx_mock.get(VALET_URL).mock(
        side_effect=[httpx.Response(200, json=first_doc), httpx.Response(200, json=second_doc)]
    )

    with make_client() as client:
        assert fetch_fx(tmp_path, client=client).stored is True
        report = fetch_fx(tmp_path, client=client)

    assert report.stored is True
    assert report.observations == 2
    assert report.last_date == date(2023, 11, 1)
    assert route.call_count == 2
    assert len(list((tmp_path / "raw" / "fx" / "_" / SERIES).iterdir())) == 2
    assert len(RawCache(tmp_path).manifest_rows()) == 2
    assert stored_document(tmp_path) == second_doc


@respx.mock(assert_all_called=False)
def test_fetch_fx_parses_before_it_stores(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    broken = make_document([{"d": "2023-10-31", "FXUSDCAD": {"v": "n/a"}}])
    respx_mock.get(VALET_URL).mock(return_value=httpx.Response(200, json=broken))

    with make_client() as client, pytest.raises(ValueError, match="2023-10-31"):
        fetch_fx(tmp_path, client=client)

    assert not (tmp_path / "raw").exists()
    assert RawCache(tmp_path).manifest_rows() == []


@respx.mock(assert_all_called=False)
def test_fetch_fx_rejects_a_malformed_series_before_any_request(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    route = respx_mock.get(url__regex=r".*/valet/observations/.*").mock(
        return_value=httpx.Response(200, json={})
    )

    with make_client() as client, pytest.raises(ValueError, match="invalid Valet series id"):
        fetch_fx(tmp_path, series="../FXUSDCAD", client=client)

    assert route.call_count == 0


@respx.mock(assert_all_called=False)
def test_fetch_fx_builds_a_client_from_settings(
    respx_mock: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = make_document([{"d": "2023-10-31", "FXUSDCAD": {"v": "1.3659"}}])
    respx_mock.get(VALET_URL).mock(return_value=httpx.Response(200, json=doc))
    monkeypatch.setenv("SEC_USER_AGENT", "Test User test@example.com")

    report = fetch_fx(tmp_path)

    assert report.stored is True
    assert stored_document(tmp_path) == doc


DATES = st.dates(min_value=date(2017, 1, 3), max_value=date(2026, 10, 27))
RATES = st.integers(min_value=1, max_value=1_000_000).map(lambda n: f"{n / 10000:.4f}")

OBSERVATIONS = st.lists(
    st.tuples(DATES, RATES), min_size=1, max_size=30, unique_by=lambda pair: pair[0]
)


@given(OBSERVATIONS)
@PROPERTY_SETTINGS
def test_known_at_is_always_after_midnight_toronto_and_dates_increase(
    observations: list[tuple[date, str]],
) -> None:
    doc = make_document(
        [{"d": rate_date.isoformat(), "FXUSDCAD": {"v": rate}} for rate_date, rate in observations]
    )
    frame = parse_valet(doc)

    rows = frame.sort("rate_date").iter_rows(named=True)
    previous: date | None = None
    for row in rows:
        rate_date = row["rate_date"]
        midnight = datetime.combine(rate_date, time(0), tzinfo=TORONTO)
        assert row["known_at"] > midnight.astimezone(UTC)
        assert row["known_at"].astimezone(TORONTO).hour == 17
        if previous is not None:
            assert rate_date > previous
        previous = rate_date
    assert frame.height == len({rate_date for rate_date, _ in observations})
