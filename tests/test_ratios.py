"""Tests for :mod:`edgarpit.ratios`, the Python half of the ratio rule (A-11)."""

from __future__ import annotations

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from edgarpit.ratios import (
    RatioInput,
    RatioValue,
    UnitMismatchError,
    compute_ratio,
    currency_of,
    unit_family,
)

CURRENCY_ALPHABET = string.ascii_uppercase
CURRENCY_CODES = st.text(CURRENCY_ALPHABET, min_size=3, max_size=3)


def make_input(field: str, value: float, unit: str) -> RatioInput:
    return RatioInput(field=field, value=value, unit=unit, currency=currency_of(unit))


def test_unit_family_classification() -> None:
    assert unit_family("USD") == "currency"
    assert unit_family("CAD") == "currency"
    assert unit_family("EUR") == "currency"
    assert unit_family("USD/shares") == "currency_per_share"
    assert unit_family("CAD/shares") == "currency_per_share"
    assert unit_family("shares") == "shares"
    assert unit_family("pure") == "pure"
    assert unit_family("usd") == "other"
    assert unit_family("USD/share") == "other"
    assert unit_family("shares/shares") == "other"
    assert unit_family("m2") == "other"


def test_currency_of_extracts_the_code() -> None:
    assert currency_of("USD") == "USD"
    assert currency_of("CAD") == "CAD"
    assert currency_of("USD/shares") == "USD"
    assert currency_of("CAD/shares") == "CAD"
    assert currency_of("shares") is None
    assert currency_of("pure") is None
    assert currency_of("m2") is None


def test_mixed_currency_ratio_is_rejected() -> None:
    numerator = make_input("revenue", 100.0, "CAD")
    denominator = make_input("total_assets", 50.0, "USD")

    with pytest.raises(UnitMismatchError):
        compute_ratio(numerator, denominator)


def test_mixed_currency_per_share_ratio_is_rejected() -> None:
    numerator = make_input("eps_diluted", 1.5, "USD/shares")
    denominator = make_input("eps_basic", 1.4, "CAD/shares")

    with pytest.raises(UnitMismatchError):
        compute_ratio(numerator, denominator)


def test_per_share_vs_currency_is_rejected() -> None:
    numerator = make_input("eps_diluted", 1.5, "USD/shares")
    denominator = make_input("revenue", 100.0, "USD")

    with pytest.raises(UnitMismatchError):
        compute_ratio(numerator, denominator)


def test_same_currency_ratio_succeeds() -> None:
    numerator = make_input("net_income", 10.0, "USD")
    denominator = make_input("revenue", 200.0, "USD")

    result = compute_ratio(numerator, denominator)

    assert isinstance(result, RatioValue)
    assert result.value == pytest.approx(0.05)
    assert result.unit == "pure"
    assert result.currency is None
    assert result.numerator is numerator
    assert result.denominator is denominator


def test_same_currency_per_share_ratio_succeeds() -> None:
    numerator = make_input("eps_diluted", 1.5, "CAD/shares")
    denominator = make_input("eps_basic", 3.0, "CAD/shares")

    result = compute_ratio(numerator, denominator)

    assert result.value == pytest.approx(0.5)
    assert result.unit == "pure"


def test_shares_vs_shares_is_allowed() -> None:
    numerator = make_input("shares_outstanding", 30.0, "shares")
    denominator = make_input("shares_outstanding", 40.0, "shares")

    result = compute_ratio(numerator, denominator)

    assert result.value == pytest.approx(0.75)
    assert result.unit == "pure"


def test_pure_vs_pure_is_allowed() -> None:
    numerator = make_input("ratio_a", 0.25, "pure")
    denominator = make_input("ratio_b", 0.5, "pure")

    assert compute_ratio(numerator, denominator).value == pytest.approx(0.5)


def test_identical_other_units_are_allowed() -> None:
    numerator = make_input("ratio_a", 6.0, "m2")
    denominator = make_input("ratio_b", 3.0, "m2")

    assert compute_ratio(numerator, denominator).value == pytest.approx(2.0)


def test_different_other_units_are_rejected() -> None:
    numerator = make_input("ratio_a", 6.0, "m2")
    denominator = make_input("ratio_b", 3.0, "acres")

    with pytest.raises(UnitMismatchError):
        compute_ratio(numerator, denominator)


def test_shares_vs_currency_is_rejected() -> None:
    numerator = make_input("shares_outstanding", 30.0, "shares")
    denominator = make_input("total_assets", 100.0, "USD")

    with pytest.raises(UnitMismatchError):
        compute_ratio(numerator, denominator)


def test_zero_denominator_raises() -> None:
    numerator = make_input("net_income", 10.0, "USD")
    denominator = make_input("total_equity", 0.0, "USD")

    with pytest.raises(ZeroDivisionError):
        compute_ratio(numerator, denominator)


@given(numerator_currency=CURRENCY_CODES, denominator_currency=CURRENCY_CODES)
def test_differing_currencies_are_always_rejected(
    numerator_currency: str, denominator_currency: str
) -> None:
    if numerator_currency == denominator_currency:
        return
    numerator = make_input("numerator", 123.0, numerator_currency)
    denominator = make_input("denominator", 456.0, denominator_currency)

    with pytest.raises(UnitMismatchError):
        compute_ratio(numerator, denominator)


@given(currency=CURRENCY_CODES, other=CURRENCY_CODES)
def test_currency_vs_per_share_of_a_different_currency_is_rejected(
    currency: str, other: str
) -> None:
    if currency == other:
        return
    numerator = make_input("numerator", 123.0, currency)
    denominator = make_input("denominator", 456.0, f"{other}/shares")

    with pytest.raises(UnitMismatchError):
        compute_ratio(numerator, denominator)
