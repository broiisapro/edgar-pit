"""Filing SGML headers: the authoritative acceptance time (decisions A-1, A-16)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

import polars as pl


@dataclass(frozen=True)
class HeaderInfo:
    """The fields edgar-pit keeps from one ``.hdr.sgml`` filing header."""

    accession: str
    form: str
    acceptance_ts: datetime
    filing_date: date
    period: date
    items: str | None


def parse_header(text: str) -> HeaderInfo:
    """Parse an SGML filing header.

    ``acceptance_ts`` comes from ``<ACCEPTANCE-DATETIME>`` interpreted in
    ``America/New_York`` and converted to UTC. Raises ``ValueError`` when a
    required tag is missing or malformed.
    """
    raise NotImplementedError


def apply_acceptance(filings: pl.DataFrame, headers: Mapping[str, HeaderInfo]) -> pl.DataFrame:
    """Attach acceptance times and return a frame valid under ``FilingsSchema``.

    For a filing whose accession is in ``headers``, set ``acceptance_ts`` from
    the header with ``acceptance_ts_source='sgml_header'`` and ``ts_imputed=False``.
    Every other filing falls back to ``filing_date`` 23:59:59 America/New_York
    with ``acceptance_ts_source='filing_date_eod'`` and ``ts_imputed=True``
    (decision A-1).
    """
    raise NotImplementedError
