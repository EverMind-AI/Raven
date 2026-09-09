"""match_ds word-boundary regression tests.

`bench/score.py` is a separate tree (see AGENTS.md #1.2 exception) imported
here by file path, the same way `pipeline/rescore_short_gold.py` loads it, so
this file must not `import score` as a package.

The bug under test: `t in a` / `g in a` were plain substring checks with no
word boundary, so any gold whose norm() form is 1-2 chars (a single letter or
digit, "yes"/"no") matched almost any prose — e.g. gold "b" is a substring of
"table". Measured false-positive rate on the eval_web_dr33_dsv4f batch was
35.5% overall, 84.2% when norm(gold) <= 2 chars.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_RAVEN_TRAIN_ROOT = Path(__file__).resolve().parents[2]
_PIPELINE_DIR = str(_RAVEN_TRAIN_ROOT / "pipeline")  # score.py does `from common import ...`
if not (_RAVEN_TRAIN_ROOT / "pipeline" / "common.py").exists():
    # The pipeline tree lives NEXT TO the repo, not inside it, so a checkout
    # on a host without it must skip -- an ImportError here fails the whole
    # collection, not just this file.
    pytest.skip("raven_train pipeline tree not present next to the repo", allow_module_level=True)
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

_SCORE_PY = Path(__file__).resolve().parents[1] / "bench" / "score.py"
_spec = importlib.util.spec_from_file_location("score_engine_under_test", _SCORE_PY)
score = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("score_engine_under_test", score)
_spec.loader.exec_module(score)


def test_short_gold_letter_not_a_substring_hit():
    # "b" must not match merely because it occurs inside "table".
    assert not score.match_ds("b", "the table has four legs")


def test_short_gold_letter_matches_as_a_standalone_word():
    assert score.match_ds("b", "the correct option is b, not c")


def test_short_gold_digit_not_a_substring_hit():
    assert not score.match_ds("3", "there are 30 items in the box")


def test_short_gold_yes_no_are_whole_word_matched():
    assert score.match_ds("yes", "the answer is yes")
    assert not score.match_ds("yes", "yesterday it rained")


def test_multiword_gold_still_matches_as_a_phrase():
    # token-recall path (and the `g in a` fast path) must still handle a
    # multi-word gold embedded in prose, e.g. a species name.
    assert score.match_ds(
        "x congolensis", "researchers isolated X congolensis from soil samples"
    )


def test_hle_836_style_five_letter_golds_do_not_cross_match():
    # Regression for the actual audited case: five single-letter golds, each
    # embedded only *inside* unrelated words ("best" contains "b", "definitely"
    # contains "d"/"f"/"i", "other" contains "h") of an answer that argues for
    # a *different* set of letters (c, e, g, j).
    answer = ("the best evidence points toward choices c, e, g, and j; "
              "definitely rule out other options")
    for gold in ["b", "d", "f", "h", "i"]:
        assert not score.match_ds(gold, answer), f"gold={gold!r} false-matched"
