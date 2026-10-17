"""The Python ratio rule: same-currency, same-unit-family inputs (decision A-11).

``ratios_pit`` in dbt follows exactly this rule in SQL; this module holds it in
Python so tests can pin the behaviour down. Inputs must share currency and unit
family, otherwise no ratio exists and :class:`UnitMismatchError` is raised.
"""

from __future__ import annotations

from dataclasses import dataclass


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


def compute_ratio(numerator: RatioInput, denominator: RatioInput) -> RatioValue:
    """Compute ``numerator / denominator``.

    Both inputs must carry the same unit family and, for currency units, the
    same currency; otherwise :class:`UnitMismatchError` is raised and no value
    exists. Raises ``ZeroDivisionError`` when the denominator is zero.
    """
    raise NotImplementedError
