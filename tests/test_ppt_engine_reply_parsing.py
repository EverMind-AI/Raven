"""Reading JSON a model wrote by hand.

Sixteen percent of one model's replies failed to parse here, a third of them for
one reason: a block of python carries `\\d` and `\\(` long before its author
remembers it is shipping them inside a JSON string.
"""

from __future__ import annotations

import json

import pytest

from raven_ppt.stages._reply import (
    dedent_fence,
    escapes_repaired,
    json_defect,
    loads_maybe_fenced,
    quotes_repaired,
)


def test_a_plain_object_parses() -> None:
    assert loads_maybe_fenced('{"verdict": "ok"}') == {"verdict": "ok"}


def test_a_fenced_object_parses() -> None:
    assert loads_maybe_fenced('```json\n{"verdict": "edited"}\n```') == {"verdict": "edited"}


def test_illegal_escapes_in_a_code_block_are_repaired_losslessly() -> None:
    """`\\d` in a regex inside the block is not an escape JSON knows."""
    raw = '{"block": "re.match(r\'\\d+\', s)"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    parsed = loads_maybe_fenced(raw)
    assert parsed is not None
    assert parsed["block"] == "re.match(r'\\d+', s)"


def test_a_legal_double_backslash_is_stepped_over_not_broken_further() -> None:
    """The scan must not restart inside a legal pair.

    In `"b\\\\c"` the first backslash is legal; treating the second as a fresh
    escape before `c` and doubling it yields `\\\\\\c`, which is more broken than
    what arrived.
    """
    assert escapes_repaired('{"a": "b\\\\c"}') is None
    assert loads_maybe_fenced('{"a": "b\\\\c"}') == {"a": "b\\c"}


def test_a_reply_mixing_legal_and_illegal_escapes_repairs_only_the_illegal_one() -> None:
    parsed = loads_maybe_fenced("{\"block\": \"p = r'C:\\\\dir' + r'\\d'\"}")
    assert parsed is not None
    assert parsed["block"] == "p = r'C:\\dir' + r'\\d'"


def test_a_legal_escape_survives_the_repair_unchanged() -> None:
    parsed = loads_maybe_fenced('{"notes": ["line\\nbreak"]}')
    assert parsed == {"notes": ["line\nbreak"]}


def test_repair_returns_none_when_there_was_nothing_to_repair() -> None:
    assert escapes_repaired('{"a": "b"}') is None
    assert escapes_repaired('{"a": "b\\nc"}') is None


def test_a_non_object_reply_is_not_a_dict() -> None:
    assert loads_maybe_fenced("[1, 2, 3]") is None
    assert loads_maybe_fenced("not json at all") is None


def test_the_defect_says_where_and_why() -> None:
    defect = json_defect('{"slide": 3, "verdict": }')
    assert "at character" in defect
    assert "near `" in defect


def test_a_valid_non_object_is_named_as_such() -> None:
    assert json_defect("[1, 2]") == "the JSON parsed but was not an object"


def test_a_fenced_code_block_loses_its_fence_and_keeps_a_trailing_newline() -> None:
    assert dedent_fence("```python\nx = 1\n```") == "x = 1\n"
    assert dedent_fence("x = 1") == "x = 1\n"
    assert dedent_fence("```\n# SLIDE 2\ny = 2\n```") == "# SLIDE 2\ny = 2\n"


def test_a_stray_quote_inside_a_string_is_repaired() -> None:
    """From a live run: an intake reply named a product in quotes and lost the string.

    `"question": "这里的 "Evermind" 指的是哪款产品？"` -- the quote before the name closed
    the value three characters in, the object failed to parse, and fifty seconds of
    intake went with it.
    """
    reply = '{"topic": "对比", "questions": [{"question": "这里的 "Evermind" 指的是哪款产品？", "why": "定位"}]}'

    parsed = loads_maybe_fenced(reply)

    assert parsed is not None
    assert parsed["questions"][0]["question"] == '这里的 "Evermind" 指的是哪款产品？'


def test_the_quote_repair_leaves_well_formed_json_alone() -> None:
    """It only ever adds a backslash, and only inside a string."""
    assert quotes_repaired('{"a": "plain", "b": ["x", "y"], "c": {"d": 1}}') is None
    assert quotes_repaired('{"block": "print(\\"hi\\")"}') is None
    assert quotes_repaired('{"a": "x" , "b": "y"}') is None


def test_a_closing_quote_is_told_apart_by_what_follows_it() -> None:
    """Whitespace and then one of `,:}]` closes a string; anything else was meant."""
    repaired = quotes_repaired('{"a": "he said "no" to it"}')

    assert repaired is not None
    assert loads_maybe_fenced(repaired)["a"] == 'he said "no" to it'
