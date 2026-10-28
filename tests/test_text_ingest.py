"""Tests for ``raw.filings_text`` assembly (decision A-31).

``discover.exhibit_documents``, ``clean.html_to_text`` and
``segment.segment_items`` are replaced by small doubles in these tests (they are
implemented by their own issues), so what is pinned here is the row assembly of
:func:`edgarpit.text.ingest.ingest_text`: which documents become rows, how the
sections are carried over, the copied acceptance time, the hashing, the
ordering and the schema.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from edgarpit.rawcache import RawCache, document_key
from edgarpit.schemas import FilingsTextSchema
from edgarpit.sec_client import document_url, filing_index_url
from edgarpit.text import clean, discover, ingest, segment
from edgarpit.text.discover import DocumentRef

CIK = "0000320193"
ACCESSION = "0000320193-26-000018"
ACCEPTED = datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC)
PRIMARY = "aapl-20260730.htm"
EXHIBIT = "a8-kex991q3202606272026.htm"

# The ``raw.filings_text`` column contract of docs/architecture.md section 6,
# spelled out here so a contract change has to be made deliberately.
TEXT_COLUMNS = [
    "cik",
    "accession",
    "acceptance_ts",
    "ts_imputed",
    "document",
    "doc_type",
    "section",
    "section_order",
    "text",
    "sha256",
    "char_count",
]

_FILINGS_DTYPES: dict[str, pl.DataType] = {
    "cik": pl.String,
    "accession": pl.String,
    "form": pl.String,
    "filing_date": pl.Date,
    "report_date": pl.Date,
    "acceptance_ts": pl.Datetime("us", "UTC"),
    "ts_imputed": pl.Boolean,
    "items": pl.String,
}


def sha256_of(text: str) -> str:
    """Return the lowercase hex SHA-256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def filings_frame(
    *,
    accession: str = ACCESSION,
    acceptance_ts: datetime | None = ACCEPTED,
    ts_imputed: bool = False,
    items: str | None = "2.02,9.01",
    filing_date: date = date(2026, 7, 31),
    report_date: date | None = date(2026, 7, 30),
) -> pl.DataFrame:
    """Return a one-row ``raw.filings``-shaped frame for the tests."""
    return pl.DataFrame(
        [
            (
                CIK,
                accession,
                "8-K",
                filing_date,
                report_date,
                acceptance_ts,
                ts_imputed,
                items,
            )
        ],
        schema=_FILINGS_DTYPES,
        orient="row",
    )


def store_index(cache: RawCache, accession: str, body: str) -> None:
    """Write a filing index page into the test cache."""
    cache.write(
        "filing_index",
        CIK,
        accession,
        body.encode("utf-8"),
        url=filing_index_url(CIK, accession),
        http_status=200,
    )


def store_document(
    cache: RawCache, accession: str, filename: str, body: bytes, *, cik: str = CIK
) -> None:
    """Write one filing document into the test cache."""
    cache.write(
        "documents",
        cik,
        document_key(accession, filename),
        body,
        url=document_url(cik, accession, filename),
        http_status=200,
    )


class Fakes:
    """Doubles for the three text collaborators, looked up by their input.

    ``pages`` maps an index page's text to its document references, ``cleaned``
    maps a raw document body to its cleaned text and ``sections`` maps cleaned
    text to the sections the segmenter returns. ``segment_calls`` records every
    ``segment_items`` call so a test can prove which documents were segmented
    and with which expected items.
    """

    def __init__(self) -> None:
        self.pages: dict[str, list[DocumentRef]] = {}
        self.cleaned: dict[str, str] = {}
        self.sections: dict[str, list[segment.Section]] = {}
        self.segment_calls: list[tuple[str, tuple[str, ...]]] = []

    def exhibit_documents(self, index_headers_html: str) -> list[DocumentRef]:
        return self.pages[index_headers_html]

    def html_to_text(self, html: str) -> str:
        return self.cleaned[html]

    def segment_items(self, text: str, expected_items: Sequence[str]) -> list[segment.Section]:
        self.segment_calls.append((text, tuple(expected_items)))
        return self.sections[text]


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    """Replace the three collaborators that land with their own issues."""
    doubles = Fakes()
    monkeypatch.setattr(discover, "exhibit_documents", doubles.exhibit_documents)
    monkeypatch.setattr(clean, "html_to_text", doubles.html_to_text)
    monkeypatch.setattr(segment, "segment_items", doubles.segment_items)
    return doubles


def prime_apple_filing(fakes: Fakes, cache: RawCache, *, accession: str = ACCESSION) -> None:
    """Store and register a two-document 8-K (primary plus one exhibit)."""
    store_index(cache, accession, "index page")
    store_document(cache, accession, PRIMARY, b"<html>primary</html>")
    store_document(cache, accession, EXHIBIT, b"<html>exhibit</html>")
    fakes.pages["index page"] = [
        DocumentRef(accession=accession, filename=PRIMARY, doc_type="primary"),
        DocumentRef(accession=accession, filename=EXHIBIT, doc_type="EX-99.1"),
    ]
    fakes.cleaned["<html>primary</html>"] = "Item 2.02 Results\nRevenue rose."
    fakes.cleaned["<html>exhibit</html>"] = "  Apple reports results.  "
    fakes.sections["Item 2.02 Results\nRevenue rose."] = [
        segment.Section(section="2.02", order=1, text="Item 2.02 Results\nRevenue rose."),
        segment.Section(section="9.01", order=2, text="Item 9.01 Exhibits"),
    ]


def test_ingest_assembles_the_primary_and_each_exhibit(fakes: Fakes, tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    prime_apple_filing(fakes, cache)

    frame = ingest.ingest_text(cache, CIK, [ACCESSION], filings_frame())

    assert frame.columns == TEXT_COLUMNS
    # Sorted by (accession, document, section_order): the exhibit's file name
    # sorts before the primary's.
    assert frame["document"].to_list() == [EXHIBIT, PRIMARY, PRIMARY]
    assert frame["doc_type"].to_list() == ["EX-99.1", "primary", "primary"]
    assert frame["section"].to_list() == ["unsegmented", "2.02", "9.01"]
    assert frame["section_order"].to_list() == [1, 1, 2]
    texts = ["Apple reports results.", "Item 2.02 Results\nRevenue rose.", "Item 9.01 Exhibits"]
    assert frame["text"].to_list() == texts
    assert frame["sha256"].to_list() == [sha256_of(text) for text in texts]
    assert frame["char_count"].to_list() == [len(text) for text in texts]
    assert frame["cik"].to_list() == [CIK, CIK, CIK]
    assert frame["accession"].to_list() == [ACCESSION, ACCESSION, ACCESSION]
    assert frame["acceptance_ts"].to_list() == [ACCEPTED, ACCEPTED, ACCEPTED]
    assert frame["ts_imputed"].to_list() == [False, False, False]
    FilingsTextSchema.validate(frame)
    # Only the primary document is segmented, and with the filing's own items.
    assert fakes.segment_calls == [("Item 2.02 Results\nRevenue rose.", ("2.02", "9.01"))]


def test_expected_items_keep_the_filings_order_and_drop_blanks(
    fakes: Fakes, tmp_path: Path
) -> None:
    cache = RawCache(tmp_path)
    prime_apple_filing(fakes, cache)

    ingest.ingest_text(cache, CIK, [ACCESSION], filings_frame(items="9.01, 2.02 ,"))

    assert fakes.segment_calls == [("Item 2.02 Results\nRevenue rose.", ("9.01", "2.02"))]


def test_a_filing_without_items_is_segmented_with_an_empty_expectation(
    fakes: Fakes, tmp_path: Path
) -> None:
    cache = RawCache(tmp_path)
    prime_apple_filing(fakes, cache)

    ingest.ingest_text(cache, CIK, [ACCESSION], filings_frame(items=None))

    assert fakes.segment_calls == [("Item 2.02 Results\nRevenue rose.", ())]


def test_the_frame_is_sorted_by_accession_then_document(fakes: Fakes, tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    earlier = "0000320193-25-000077"
    earlier_accepted = datetime(2025, 10, 30, 20, 30, 35, tzinfo=UTC)
    for accession in (ACCESSION, earlier):
        store_index(cache, accession, f"index of {accession}")
        store_document(cache, accession, PRIMARY, f"primary body of {accession}".encode())
        store_document(cache, accession, EXHIBIT, f"exhibit body of {accession}".encode())
        # The index lists the primary first, so a missing sort would keep it
        # first even though its file name sorts after the exhibit's.
        fakes.pages[f"index of {accession}"] = [
            DocumentRef(accession=accession, filename=PRIMARY, doc_type="primary"),
            DocumentRef(accession=accession, filename=EXHIBIT, doc_type="EX-99.1"),
        ]
        fakes.cleaned[f"primary body of {accession}"] = f"text of {accession}"
        fakes.cleaned[f"exhibit body of {accession}"] = f"exhibit of {accession}"
        fakes.sections[f"text of {accession}"] = [
            segment.Section(section="2.02", order=1, text=f"text of {accession}")
        ]
    filings = pl.concat(
        [
            filings_frame(accession=earlier, acceptance_ts=earlier_accepted),
            filings_frame(),
        ]
    )

    frame = ingest.ingest_text(cache, CIK, [ACCESSION, earlier], filings)

    assert frame["accession"].to_list() == [earlier, earlier, ACCESSION, ACCESSION]
    assert frame["document"].to_list() == [EXHIBIT, PRIMARY, EXHIBIT, PRIMARY]
    assert frame["section"].to_list() == ["unsegmented", "2.02", "unsegmented", "2.02"]
    assert frame["acceptance_ts"].to_list() == [
        earlier_accepted,
        earlier_accepted,
        ACCEPTED,
        ACCEPTED,
    ]


def test_the_acceptance_time_comes_from_filings_never_from_the_document(
    fakes: Fakes, tmp_path: Path
) -> None:
    """A document's own dates must never move the instant its text is known."""
    cache = RawCache(tmp_path)
    prime_apple_filing(fakes, cache)
    # Dates the document talks about, and the API dates in filings, all differ
    # from the acceptance time. Only acceptance_ts may reach the row.
    fakes.cleaned["<html>exhibit</html>"] = (
        "Press release dated August 5, 2026 for the quarter ended June 27, 2026."
    )
    filing_acceptance = datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC)

    frame = ingest.ingest_text(
        cache,
        CIK,
        [ACCESSION],
        filings_frame(
            acceptance_ts=filing_acceptance,
            ts_imputed=True,
            filing_date=date(2026, 7, 31),
            report_date=date(2026, 6, 27),
        ),
    )

    assert frame["acceptance_ts"].unique().to_list() == [filing_acceptance]
    assert frame["ts_imputed"].unique().to_list() == [True]
    assert "2026-08-05" not in str(frame["acceptance_ts"].to_list())


def test_a_filing_without_a_cached_index_is_skipped_and_counted(
    fakes: Fakes, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = RawCache(tmp_path)
    prime_apple_filing(fakes, cache)
    missing = "0000320193-25-000077"
    filings = pl.concat([filings_frame(), filings_frame(accession=missing)])
    caplog.set_level(logging.INFO, logger="edgarpit.text.ingest")

    frame = ingest.ingest_text(cache, CIK, [missing, ACCESSION], filings)

    assert frame["accession"].unique().to_list() == [ACCESSION]
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "skipped 1 accession(s)" in message and missing in message for message in messages
    ), messages


def test_a_document_without_text_contributes_no_row(fakes: Fakes, tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    prime_apple_filing(fakes, cache)
    fakes.cleaned["<html>exhibit</html>"] = "   \n\t "

    frame = ingest.ingest_text(cache, CIK, [ACCESSION], filings_frame())

    assert frame["doc_type"].unique().to_list() == ["primary"]
    assert frame.height == 2
    FilingsTextSchema.validate(frame)


def test_a_filing_with_no_extractable_text_yields_the_empty_contract_frame(
    fakes: Fakes, tmp_path: Path
) -> None:
    cache = RawCache(tmp_path)
    prime_apple_filing(fakes, cache)
    fakes.cleaned["<html>primary</html>"] = " "
    fakes.cleaned["<html>exhibit</html>"] = ""

    frame = ingest.ingest_text(cache, CIK, [ACCESSION], filings_frame())

    assert frame.height == 0
    assert frame.columns == TEXT_COLUMNS
    FilingsTextSchema.validate(frame)


def test_no_accessions_yields_the_empty_contract_frame(tmp_path: Path) -> None:
    frame = ingest.ingest_text(RawCache(tmp_path), CIK, [], filings_frame())

    assert frame.height == 0
    assert frame.columns == TEXT_COLUMNS
    assert frame.schema["acceptance_ts"] == pl.Datetime("us", "UTC")
    assert frame.schema["section_order"] == pl.Int64
    FilingsTextSchema.validate(frame)


def test_a_document_listed_by_the_index_but_missing_from_the_cache_raises(
    fakes: Fakes, tmp_path: Path
) -> None:
    cache = RawCache(tmp_path)
    store_index(cache, ACCESSION, "index page")
    fakes.pages["index page"] = [
        DocumentRef(accession=ACCESSION, filename=PRIMARY, doc_type="primary")
    ]

    with pytest.raises(LookupError, match=re.escape(document_key(ACCESSION, PRIMARY))):
        ingest.ingest_text(cache, CIK, [ACCESSION], filings_frame())


def test_an_accession_missing_from_filings_raises(fakes: Fakes, tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    prime_apple_filing(fakes, cache)

    with pytest.raises(ValueError, match="not in the filings frame"):
        ingest.ingest_text(cache, CIK, [ACCESSION], filings_frame(accession="0000320193-25-000077"))


def test_duplicate_filings_rows_are_refused(fakes: Fakes, tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    prime_apple_filing(fakes, cache)
    filings = pl.concat([filings_frame(), filings_frame(ts_imputed=True)])

    with pytest.raises(ValueError, match="2 rows for accession"):
        ingest.ingest_text(cache, CIK, [ACCESSION], filings)


def test_a_filing_without_an_acceptance_time_raises(fakes: Fakes, tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    prime_apple_filing(fakes, cache)

    with pytest.raises(ValueError, match="no acceptance_ts"):
        ingest.ingest_text(cache, CIK, [ACCESSION], filings_frame(acceptance_ts=None))


def test_an_index_page_of_another_accession_is_refused(fakes: Fakes, tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    prime_apple_filing(fakes, cache)
    fakes.pages["index page"][0] = DocumentRef(
        accession="0000320193-25-000077", filename=PRIMARY, doc_type="primary"
    )

    with pytest.raises(ValueError, match="lists a document of 0000320193-25-000077"):
        ingest.ingest_text(cache, CIK, [ACCESSION], filings_frame())


def test_document_bytes_that_are_not_utf8_are_decoded_not_raised(
    fakes: Fakes, tmp_path: Path
) -> None:
    cache = RawCache(tmp_path)
    store_index(cache, ACCESSION, "index page")
    store_document(cache, ACCESSION, PRIMARY, b"<html>\xff</html>")
    fakes.pages["index page"] = [
        DocumentRef(accession=ACCESSION, filename=PRIMARY, doc_type="primary")
    ]
    fakes.cleaned["<html>\ufffd</html>"] = "Item 2.02 Results"
    fakes.sections["Item 2.02 Results"] = [
        segment.Section(section="2.02", order=1, text="Item 2.02 Results")
    ]

    frame = ingest.ingest_text(cache, CIK, [ACCESSION], filings_frame())

    assert frame["text"].to_list() == ["Item 2.02 Results"]
