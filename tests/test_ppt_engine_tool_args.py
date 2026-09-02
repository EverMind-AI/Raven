"""Reading arguments as models actually send them.

Written from one live failure: `ppt_outline` declares `pages` as an array of
objects, a model sent the array JSON-encoded inside a string, and the tool iterated
the string and called `.get` on a character. The author got
`AttributeError: 'str' object has no attribute 'get'` and sent the same shape again,
so two of its calls bought nothing.
"""

from __future__ import annotations

import pytest

from raven_ppt.tools._args import ArgumentError, as_ints, as_list, as_objects, as_strings


def test_a_json_encoded_array_is_read_rather_than_refused() -> None:
    """The exact shape that crashed a live run. Nothing else could have been meant."""
    assert as_objects('[{"page": 1, "claim": "one model, four tasks"}]', "pages") == [
        {"page": 1, "claim": "one model, four tasks"}
    ]
    assert as_ints("[2, 4, 5]", "pages") == [2, 4, 5]


def test_a_lone_item_is_a_list_of_one() -> None:
    assert as_strings("fig_one", "figures") == ["fig_one"]
    assert as_objects({"page": 1}, "pages") == [{"page": 1}]
    assert as_ints(3, "pages") == [3]


def test_a_bare_string_is_never_iterated_into_characters() -> None:
    """The bug itself: a string is one item, not a list of its letters."""
    assert as_list("tarvis", "figures") == ["tarvis"]


def test_numbers_sent_as_strings_are_read() -> None:
    assert as_ints(["1", " 2 "], "slides") == [1, 2]


def test_absent_is_empty() -> None:
    assert as_list(None, "pages") == []
    assert as_ints(None, "slides") == []


def test_a_list_of_strings_where_objects_belong_is_refused_by_name() -> None:
    with pytest.raises(ArgumentError, match="list of objects, not a list of strings"):
        as_objects(["a claim", "another claim"], "pages")


def test_json_that_does_not_parse_says_so() -> None:
    with pytest.raises(ArgumentError, match="does not parse"):
        as_ints("[1, 2", "pages")


def test_something_that_is_not_a_page_number_is_refused() -> None:
    with pytest.raises(ArgumentError, match="not a page number"):
        as_ints(["cover"], "pages")


def test_a_quote_inside_a_double_encoded_array_is_repaired_not_refused() -> None:
    """The same repair a reply gets, because the same hand wrote both.

    A live run sent `pages` JSON-encoded inside a string with a quote inside one of
    its claims, and got `Expecting ',' delimiter: line 1 column 919` back for a
    twelve-page outline it had just written.
    """
    encoded = '[{"page": 1, "claim": "他说 "统一" 是关键", "says": ["a"]}]'
    pages = as_objects(encoded, "pages")
    assert len(pages) == 1
    assert pages[0]["claim"] == '他说 "统一" 是关键'


def test_a_python_escape_inside_a_double_encoded_array_survives() -> None:
    encoded = '[{"page": 1, "claim": "match \\d+ digits"}]'
    assert as_objects(encoded, "pages")[0]["claim"] == "match \\d+ digits"


def test_json_that_no_repair_reaches_says_where_it_broke() -> None:
    with pytest.raises(ArgumentError) as caught:
        as_list('[{"page": 1,', "pages")
    assert "pages looks like JSON" in str(caught.value)
