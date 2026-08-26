"""The display-width measure the session namer gates on.

A length in code points is not comparable across scripts, and the whole point of
this function is that one threshold has to mean the same thing to a Chinese and
an English writer.
"""

from __future__ import annotations

import pytest

from raven.utils.text import display_width


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("hi", 2),
        ("hello", 5),
        ("nihao", 5),
        ("rpc", 3),
        ("你好", 4),
        ("你是谁", 6),
        ("你能做什么", 10),
        # Mixed, which is what most real openings here look like.
        ("调研hermes", 10),
        # Full-width latin is wide too; it is the width property, not the script.
        ("ｈｉ", 4),
    ],
)
def test_measures_columns_not_code_points(text: str, expected: int) -> None:
    assert display_width(text) == expected


def test_cjk_outweighs_latin_per_code_point() -> None:
    """The property the gate depends on, stated on its own.

    Same code-point count, different information: a code-point threshold that
    admits one has to admit the other, which is exactly the unfairness columns
    remove.
    """
    assert len("你能做什么") == len("nihao")
    assert display_width("你能做什么") > display_width("nihao")
