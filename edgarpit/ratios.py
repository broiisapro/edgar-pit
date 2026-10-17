"""The Python ratio rule: same-currency, same-unit-family inputs (decision A-11).

``ratios_pit`` in dbt follows exactly this rule in SQL; this module holds it in
Python so tests can pin the behaviour down. Inputs must share currency and unit
family, otherwise no ratio exists and :class:`UnitMismatchError` is raised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CURRENCY = re.compile(r"^[A-Z]{3}$")


class UnitMismatchError(ValueError):
    """Two ratio inputs do not share currency and unit family."""


@dataclass(frozen=True)
class RatioInput:
    """One input version used by a ratio computation."""

    field: str
    value: float
    unit: str
    currency: str | None


@dataclass(frozen=True)
class RatioValue:
    """The value of one ratio and the inputs it was computed from."""

    value: float
    unit: str
    currency: str | None
    numerator: RatioInput
    denominator: RatioInput


def unit_family(unit: str) -> str:
    """Classify an XBRL unit into its family.

    ``unit`` is a companyfacts unit string as stored in
    :class:`edgarpit.schemas.FactsRawSchema`. Returns ``"currency"`` for an
    ISO-4217 code (three uppercase letters, e.g. ``USD``, ``CAD``),
    ``"currency_per_share"`` for ``<currency>/shares``, ``"shares"`` for share
    counts, ``"pure"`` for dimensionless units and ``"other"`` for everything
    else.
    """
    if unit == "shares":
        return "shares"
    if unit == "pure":
        return "pure"
    numerator, slash, denominator = unit.partition("/")
    if slash:
        if _CURRENCY.fullmatch(numerator) and denominator == "shares":
            return "currency_per_share"
        return "other"
    if _CURRENCY.fullmatch(unit):
        return "currency"
    return "other"


def currency_of(unit: str) -> str | None:
    """Return the ISO currency code a unit is expressed in, or ``None``.

    ``USD`` and ``USD/shares`` yield ``"USD"``; ``shares``, ``pure`` and units
    of family ``"other"`` carry no currency and yield ``None``.
    """
    family = unit_family(unit)
    if family == "currency":
        return unit
    if family == "currency_per_share":
        return unit.partition("/")[0]
    return None


def compute_ratio(numerator: RatioInput, denominator: RatioInput) -> RatioValue:
    """Compute ``numerator / denominator``.

    Both inputs must carry the same unit family and, for currency units
    (``currency`` and ``currency_per_share``), the same currency; inputs of
    family ``other`` must additionally use the identical unit string.
    Otherwise :class:`UnitMismatchError` is raised and no value exists. The
    result is dimensionless: unit ``"pure"`` and no currency. Raises
    ``ZeroDivisionError`` when the denominator's value is zero.
    """
    num_family = unit_family(numerator.unit)
    den_family = unit_family(denominator.unit)
    if num_family != den_family:
        raise UnitMismatchError(
            f"{numerator.field!r} is in {numerator.unit!r} ({num_family}) but "
            f"{denominator.field!r} is in {denominator.unit!r} ({den_family})"
        )
    if num_family in ("currency", "currency_per_share"):
        num_currency = currency_of(numerator.unit)
        den_currency = currency_of(denominator.unit)
        if num_currency != den_currency:
            raise UnitMismatchError(
                f"{numerator.field!r} is in {num_currency} but "
                f"{denominator.field!r} is in {den_currency}"
            )
    elif num_family == "other" and numerator.unit != denominator.unit:
        raise UnitMismatchError(
            f"{numerator.field!r} is in {numerator.unit!r} but "
            f"{denominator.field!r} is in {denominator.unit!r}"
        )
    if denominator.value == 0:
        raise ZeroDivisionError(
            f"ratio {numerator.field!r}/{denominator.field!r} has a zero denominator"
        )
    return RatioValue(
        value=numerator.value / denominator.value,
        unit="pure",
        currency=None,
        numerator=numerator,
        denominator=denominator,
    )
