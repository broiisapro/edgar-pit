"""Item segmentation of 8-K text (decision A-31).

The synthetic documents are modelled on the three primary documents in
``tests/fixtures/text_expected.json``: Apple's two earnings 8-Ks (Items 2.02
and 9.01) and Shopify's 8-K (Items 2.02, 7.01 and 9.01). The cleaner runs in a
separate workstream, so these tests feed :func:`segment.segment_items` text that
is already the shape ``html_to_text`` produces. No test here touches the
network.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from edgarpit.text.segment import Section, segment_items

# One Apple primary: preamble, the two declared item headings and prose that
# mentions an item number inside a sentence.
APPLE_TEXT = """UNITED STATES
SECURITIES AND EXCHANGE COMMISSION
WASHINGTON, D.C. 20549

FORM 8-K

Item 2.02 Results of Operations and Financial Condition.

On January 27, 2022, Apple Inc. ("Apple") issued a press release regarding
Apple's financial results for its first fiscal quarter ended December 25, 2021.
A copy of Apple's press release is attached hereto as Exhibit 99.1.

The information contained in this Current Report shall not be deemed "filed"
for purposes of Section 18 of the Securities Exchange Act of 1934.

Item 9.01 Financial Statements and Exhibits.

(d) Exhibits
Press release issued by Apple Inc. on January 27, 2022."""

# One Shopify primary: three declared items, and a reference to Item 7.01 that
# sits inside a sentence rather than at the start of a line.
SHOPIFY_TEXT = """FORM 8-K

Item 2.02 Results of Operations and Financial Condition.

On February 11, 2026, Shopify Inc. issued a press release announcing its
financial results for the quarter and full year ended December 31, 2025, as
well as a share repurchase program. The program is described in Item 7.01 above.

Item 7.01 Regulation FD Disclosure.

The repurchase program will go into effect on February 17, 2026 and does not
have a fixed expiration.

Item 9.01 Financial Statements and Exhibits.

(d) Exhibits
99.1 Press release dated February 11, 2026."""

CAPTIONS = {
    "1.01": "Entry into a Material Definitive Agreement.",
    "2.02": "Results of Operations and Financial Condition.",
    "5.02": "Departure of Directors or Certain Officers.",
    "7.01": "Regulation FD Disclosure.",
    "8.01": "Other Events.",
    "9.01": "Financial Statements and Exhibits.",
}

ITEM_CODES = st.sampled_from(sorted(CAPTIONS))
# Body lines without leading or trailing whitespace, so the generated documents
# are the canonical shape the exact-join half of the reconstruction rule covers.
BODY_LINES = st.lists(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz.,;", min_size=1, max_size=40),
    min_size=1,
    max_size=4,
)


def assert_tokens_preserved(sections: list[Section], text: str) -> None:
    """Assert the module's reconstruction rule: no token of ``text`` is lost."""
    assert [token for section in sections for token in section.text.split()] == text.split()


def assert_exact_join(sections: list[Section], text: str) -> None:
    """Assert the exact join for trimmed text without blank lines or indentation."""
    assert "\n".join(section.text for section in sections) == text.strip()


@settings(max_examples=50, deadline=None)
@given(items=st.lists(ITEM_CODES, min_size=1, max_size=3, unique=True), bodies=BODY_LINES)
def test_property_a_confident_split_has_contiguous_orders(
    items: list[str], bodies: list[str]
) -> None:
    lines = ["FORM 8-K"]
    for code in items:
        lines.append(f"Item {code} {CAPTIONS[code]}")
        lines.extend(bodies * 2)
    text = "\n".join(lines)

    sections = segment_items(text, items)

    assert [section.section for section in sections] == items
    assert [section.order for section in sections] == list(range(1, len(items) + 1))
    assert all(section.text for section in sections)
    assert_tokens_preserved(sections, text)
    assert_exact_join(sections, text)


@settings(max_examples=100, deadline=None)
@given(text=st.text(), items=st.lists(ITEM_CODES, max_size=3))
def test_property_orders_are_one_to_n_and_only_the_last_may_be_empty(
    text: str, items: list[str]
) -> None:
    sections = segment_items(text, items)

    assert [section.order for section in sections] == list(range(1, len(sections) + 1))
    assert all(section.text for section in sections[:-1])
    assert [token for section in sections for token in section.text.split()] == text.split()


@settings(max_examples=100, deadline=None)
@given(text=st.text())
def test_property_legacy_items_never_segment(text: str) -> None:
    sections = segment_items(text, ["5"])

    if text.strip():
        assert sections == [Section(section="unsegmented", order=1, text=text.strip())]
    else:
        assert sections == []


def test_apple_primary_splits_into_its_two_items() -> None:
    sections = segment_items(APPLE_TEXT, ["2.02", "9.01"])

    assert [section.section for section in sections] == ["2.02", "9.01"]
    assert [section.order for section in sections] == [1, 2]
    assert sections[0].text.startswith("UNITED STATES")
    assert "Item 2.02 Results of Operations and Financial Condition." in sections[0].text
    assert "Press release issued by Apple Inc." not in sections[0].text
    assert sections[1].text.startswith("Item 9.01 Financial Statements and Exhibits.")
    assert "Press release issued by Apple Inc." in sections[1].text
    assert_tokens_preserved(sections, APPLE_TEXT)


def test_shopify_primary_splits_into_its_three_items() -> None:
    sections = segment_items(SHOPIFY_TEXT, ["2.02", "7.01", "9.01"])

    assert [section.section for section in sections] == ["2.02", "7.01", "9.01"]
    assert [section.order for section in sections] == [1, 2, 3]
    assert sections[0].text.startswith("FORM 8-K")
    assert "described in Item 7.01 above" in sections[0].text
    assert sections[1].text.startswith("Item 7.01 Regulation FD Disclosure.")
    assert sections[2].text.startswith("Item 9.01 Financial Statements and Exhibits.")
    assert_tokens_preserved(sections, SHOPIFY_TEXT)


@pytest.mark.parametrize(
    "heading",
    [
        "Item 2.02",
        "Item 2.02 Results of Operations and Financial Condition.",
        "ITEM 2.02. Results of Operations and Financial Condition.",
        "iTeM 2.02 Results of Operations and Financial Condition.",
        "Item\t2.02 Results of Operations and Financial Condition.",
        "   Item 2.02 Results of Operations and Financial Condition.",
        "Item\u00a02.02 Results of Operations and Financial Condition.",
    ],
)
def test_heading_spellings_edgar_produces_are_recognised(heading: str) -> None:
    text = f"{heading}\nbody one\nItem 9.01 Financial Statements and Exhibits.\nbody two"

    sections = segment_items(text, ["2.02", "9.01"])

    assert [section.section for section in sections] == ["2.02", "9.01"]
    first = sections[0].text.split()
    assert first[0].casefold() == "item"
    assert first[1].rstrip(".") == "2.02"
    assert "body one" in sections[0].text
    assert sections[1].text.startswith("Item 9.01")


@pytest.mark.parametrize(
    "text",
    [
        "The information described in Item 2.02 above is furnished herewith.\nItem 9.01 X.",
        "Furnished herewith: Item 2.02 Results of Operations.\nItem 9.01 X.",
        "See Item 2.02 for the press release.\nItem 9.01 X.",
        "The filing refers to Item 2.02\nand to Item 9.01 on the next line.",
    ],
)
def test_an_item_reference_inside_running_prose_is_not_a_heading(text: str) -> None:
    sections = segment_items(text, ["2.02", "9.01"])

    assert sections == [Section(section="unsegmented", order=1, text=text.strip())]


@pytest.mark.parametrize(
    "text",
    [
        "Items 2.02 and 9.01 are furnished herewith.",
        "SubItem 2.02 Results of Operations and Financial Condition.\nItem 9.01 X.",
        "\u2022 Item 2.02 Results of Operations and Financial Condition.\nItem 9.01 X.",
        "Item 2.021 Results of Operations.\nItem 9.01 X.",
    ],
)
def test_a_heading_look_alike_that_is_not_at_a_line_start_is_ignored(text: str) -> None:
    # "Items", "SubItem", a bullet and "2.021" are not the heading "Item 2.02",
    # so only 9.01 is found and the declared pair is incomplete.
    sections = segment_items(text, ["2.02", "9.01"])

    assert sections == [Section(section="unsegmented", order=1, text=text.strip())]


def test_a_repeated_heading_leaves_the_document_unsegmented() -> None:
    text = "Item 2.02 A.\nbody\nItem 2.02 B.\nbody\nItem 9.01 C.\nbody"

    sections = segment_items(text, ["2.02", "9.01"])

    assert sections == [Section(section="unsegmented", order=1, text=text)]


def test_headings_out_of_the_declared_order_leave_the_document_unsegmented() -> None:
    text = "Item 9.01 C.\nbody\nItem 2.02 A.\nbody"

    sections = segment_items(text, ["2.02", "9.01"])

    assert sections == [Section(section="unsegmented", order=1, text=text)]


def test_an_undeclared_item_heading_leaves_the_document_unsegmented() -> None:
    text = "Item 2.02 A.\nbody\nItem 5.02 B.\nbody\nItem 9.01 C.\nbody"

    sections = segment_items(text, ["2.02", "9.01"])

    assert sections == [Section(section="unsegmented", order=1, text=text)]


def test_a_missing_declared_item_leaves_the_document_unsegmented() -> None:
    text = "Item 2.02 A.\nbody\nItem 9.01 C.\nbody"

    sections = segment_items(text, ["2.02", "7.01", "9.01"])

    assert sections == [Section(section="unsegmented", order=1, text=text)]


def test_a_legacy_numeric_item_list_never_segments() -> None:
    text = "Item 5. Other Events.\nbody\nItem 7. Financial Statements and Exhibits.\nbody"

    assert segment_items(text, ["5", "7"]) == [Section(section="unsegmented", order=1, text=text)]
    assert segment_items(text, ["5"]) == [Section(section="unsegmented", order=1, text=text)]


def test_a_filing_with_no_declared_items_is_unsegmented() -> None:
    assert segment_items("Item 2.02 A.\nbody", []) == [
        Section(section="unsegmented", order=1, text="Item 2.02 A.\nbody")
    ]


@pytest.mark.parametrize("text", ["", " ", "\n\n", " \t\n  \n"])
def test_blank_text_has_no_sections(text: str) -> None:
    assert segment_items(text, ["2.02"]) == []


def test_the_fallback_strips_outer_whitespace_only() -> None:
    text = "\n\nItem 5. Other Events.\n\nbody\n\n"

    assert segment_items(text, ["5"]) == [
        Section(section="unsegmented", order=1, text="Item 5. Other Events.\n\nbody")
    ]


def test_the_preamble_before_the_first_heading_stays_in_the_first_section() -> None:
    text = "FORM 8-K\n\nItem 2.02 A.\nbody\nItem 9.01 C.\nbody"

    sections = segment_items(text, ["2.02", "9.01"])

    assert sections[0].text.startswith("FORM 8-K")
    assert sections[0].text.count("FORM 8-K") == 1
    assert "FORM 8-K" not in sections[1].text


def test_the_reconstruction_rule_collapses_blanks_and_indentation_only() -> None:
    text = "  FORM 8-K\n\n   Item 2.02 A.\n\n   body\n\n   Item 9.01 C.\n\n   body"

    sections = segment_items(text, ["2.02", "9.01"])

    assert [section.section for section in sections] == ["2.02", "9.01"]
    joined = "\n".join(section.text for section in sections)
    assert joined == "FORM 8-K\n\n   Item 2.02 A.\n\n   body\nItem 9.01 C.\n\n   body"
    assert [token for section in sections for token in section.text.split()] == text.split()


def test_section_text_is_stripped_and_starts_at_its_heading() -> None:
    text = "Item 2.02 A.\nbody\n\n\nItem 9.01 C.\nbody\n\n"

    sections = segment_items(text, ["2.02", "9.01"])

    assert sections[0].text == "Item 2.02 A.\nbody"
    assert sections[1].text == "Item 9.01 C.\nbody"
