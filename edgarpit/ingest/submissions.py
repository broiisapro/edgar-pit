"""Submissions JSON to ``filings`` and ``events_8k`` DataFrames (decision A-16)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import polars as pl


def parse_submissions(main: Mapping[str, Any], pages: Sequence[Mapping[str, Any]]) -> pl.DataFrame:
    """Flatten a submissions document and its paginated ``filings.files`` pages.

    ``main`` is the payload of ``CIK##########.json``; ``pages`` are the payloads
    of the paginated ``filings.files[]`` pages in the order listed. Returns one
    row per filing with the columns of :class:`edgarpit.schemas.FilingsSchema`.
    ``acceptance_ts_api`` carries the API's ``acceptanceDateTime`` parsed exactly
    as given; ``acceptance_ts``, ``acceptance_ts_source`` and ``ts_imputed`` are
    still null because they are filled from the filing's SGML header by
    :func:`edgarpit.ingest.headers.apply_acceptance` (decision A-1).
    """
    raise NotImplementedError


def explode_8k_items(filings: pl.DataFrame, item_labels: Mapping[str, str]) -> pl.DataFrame:
    """Explode every 8-K/8-K/A filing's ``items`` into one row per item code.

    ``item_labels`` maps an item code (``"2.02"``) to its label; codes that are
    missing from the mapping get the label ``"unknown"``. Returns a frame valid
    under :class:`edgarpit.schemas.Events8kSchema`.
    """
    raise NotImplementedError
