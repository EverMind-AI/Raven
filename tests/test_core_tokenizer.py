"""The word segmenter: what it does with text that has no spaces in it.

Every test here is skipped when the dictionary has not been fetched, which is
the state of a checkout where `make install` could not reach the internet. The
skip is the point of the gate in :func:`raven.core.tokenizer.available`: a
feature whose data is missing says so rather than failing somewhere deep.
"""

from __future__ import annotations

import pytest

from raven.core import tokenizer as tok

pytestmark = pytest.mark.skipif(not tok.available(), reason="the word dictionary has not been fetched")

# Every Chinese string below is written as an escape. `raven/core/` and
# `tests/test_core_*` are not among the CJK exemption zones in AGENTS.md
# section 1.3, and the strings under test are Chinese by nature, so each one
# carries its reading beside it instead.
BRIDGE = "\u5357\u4eac\u5e02\u957f\u6c5f\u5927\u6865"
"""'Nanjing City Yangtze River Bridge' -- the standard segmentation example,
because the naive cut ('Nanjing mayor' + 'Jiang Daqiao', a person's name) is
also grammatical."""

BRIDGE_CUT = ["\u5357\u4eac\u5e02", "\u957f\u6c5f\u5927\u6865"]
"""'Nanjing City' + 'Yangtze River Bridge', which is the right one."""

MIXED = "\u4f7f\u7528 PyMuPDF \u89e3\u6790 PDF \u6587\u6863"
"""'Parsing PDF documents with PyMuPDF'."""

SIMPLIFIED = "\u957f\u6c5f\u5927\u6865"
TRADITIONAL = "\u9577\u6c5f\u5927\u6a4b"
"""'Yangtze River Bridge', written in each script."""

GREETING = "\u4f60\u597d\uff0c\u4e16\u754c"
"""'Hello, world', with the full-width comma Chinese punctuates with."""


def test_a_run_of_chinese_is_cut_into_words() -> None:
    """The whole reason this exists. Indexed per character, this phrase matches
    every document containing any one of its characters."""
    assert tok.tokenizer().tokenize(BRIDGE).split() == BRIDGE_CUT


def test_english_is_stemmed_rather_than_segmented() -> None:
    """It has spaces already; what it needs is that 'foxes' and 'fox' land on
    one token, or a search for one misses the other."""
    assert tok.tokenizer().tokenize("The quick brown foxes were running") == "the quick brown fox were run"


def test_a_mixed_line_takes_both_paths() -> None:
    """Technical Chinese is full of latin runs, and a tokenizer that handled
    only one of them would cut the other into characters."""
    tokens = tok.tokenizer().tokenize(MIXED).split()

    assert "pymupdf" in tokens and "pdf" in tokens
    assert "\u4f7f\u7528" in tokens, "and the chinese around them is still segmented"


def test_traditional_characters_fold_onto_simplified() -> None:
    """So a query written in one script finds a document written in the other."""
    simplified = tok.tokenizer().tokenize(SIMPLIFIED)
    traditional = tok.tokenizer().tokenize(TRADITIONAL)

    assert simplified == traditional


def test_full_width_punctuation_separates_words() -> None:
    """A Chinese comma is punctuation, not a character to index."""
    tokens = tok.tokenizer().tokenize(GREETING).split()

    assert "\uff0c" not in tokens


def test_the_shared_tokenizer_is_built_once() -> None:
    """Building it reads eight megabytes and constructs a trie. A caller that
    got its own would pay that per call site."""
    assert tok.tokenizer() is tok.tokenizer()


# -- what it can say about a character -----------------------------


def test_it_recognises_the_scripts_it_switches_on() -> None:
    assert tok.is_chinese("\u4e2d") and not tok.is_chinese("a")
    assert tok.is_alphabet("a") and not tok.is_alphabet("\u4e2d")
    assert tok.is_number("1") and not tok.is_number("a")


def test_diacritics_fold_to_ascii() -> None:
    """For the languages with no stemmer of their own: without folding, an
    accented word is fragmented before it is indexed."""
    assert tok.fold_diacritics("\u0161kola") == "skola"
    assert tok.fold_diacritics("plain") == "plain"


# -- when the data is not there ------------------------------------


def test_a_missing_dictionary_names_the_command_that_fixes_it(monkeypatch, tmp_path) -> None:
    """It used to call `exit(1)`, which takes the gateway down with it. What a
    caller needs is the sentence saying which install step was skipped."""
    monkeypatch.setattr(tok, "RES_DIR", tmp_path)

    with pytest.raises(tok.DictionaryMissingError) as caught:
        tok.dictionary_path(required=True)

    assert "fetch_resources" in str(caught.value)
    assert not tok.available()
