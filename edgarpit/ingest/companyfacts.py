"""Companyfacts JSON to the ``facts_raw`` DataFrame (decision A-16)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import polars as pl


def parse_companyfacts(doc: Mapping[str, Any]) -> pl.DataFrame:
    """Flatten a companyfacts document into the long ``facts_raw`` table.

    One row per (taxonomy, concept, unit, period, accession) entry. Returns a
    frame valid under :class:`edgarpit.schemas.FactsRawSchema`; exact duplicate
    entries are deduplicated in staging (decision A-7).
    """
    raise NotImplementedError
