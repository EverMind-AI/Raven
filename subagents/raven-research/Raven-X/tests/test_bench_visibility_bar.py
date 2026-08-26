"""Two-directional control for the runtime-faithful answer-visibility bar.

Why this file exists
--------------------
``answer_visible`` is computed with a bar that asks the arm's *config* whether a
closing think tag is required. The runtime asks a different question
(``loop/main.py:581`` + ``:2814``)::

    required = (self._dr_flow is not None) and think_closing_tag_required and not oob

The loader materialises ``DRFlowConfig()`` even for an arm whose flow is OFF, so
the scoring side reads the class default ``True`` there while the runtime reads
``False``. Measured through the real loader on 2026-08-21:
``dr32_web_base128.json`` -> ``enabled=False`` / ``think_req=True`` (strict) and
``dr33_web_dsv4f_dr128.json`` -> ``True`` / ``False`` (lenient).

That asymmetry is NOT in any published reading. The eval_web_dr33_dsv4f_20260817
batch shipped before the exemption existed at all -- its frozen
``_iso/pipeline/04_rollout.py`` contains zero occurrences of the exemption -- so
both arms there were judged strictly and ``answer_visible`` is a *constant*
(0/875 and 1/875 True), not a per-side bias. Fixing it now is therefore free,
and this file is the reason the fix is not itself a second constant: a new
criterion that only ever returns one value is indistinguishable from one that
never ran, so each direction below must be exercised by a test that fails if the
value stops moving.

The four cases are (bar x evidence), and both bars must disagree on exactly the
case the fix is for.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_RAVEN_TRAIN_ROOT = Path(__file__).resolve().parents[2]
_PIPELINE_DIR = _RAVEN_TRAIN_ROOT / "pipeline"
if not (_PIPELINE_DIR / "eval_clean_adapter.py").exists():
    pytest.skip("raven_train pipeline tree not present next to the repo", allow_module_level=True)
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

_spec = importlib.util.spec_from_file_location(
    "eval_clean_adapter_under_test", _PIPELINE_DIR / "eval_clean_adapter.py"
)
adapter = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("eval_clean_adapter_under_test", adapter)
_spec.loader.exec_module(adapter)

# An out-of-band-reasoning answer: real content, no closing tag anywhere. This is
# what deepseek-v4-flash and Opus 5 hand back -- reasoning rides its own
# ``reasoning_content`` field, so ``content`` structurally cannot carry one.
_OOB_ANSWER = "The actor is Kiefer Sutherland."
# A same-channel answer that does close its think block.
_CLOSED_ANSWER = "<think>weighing the constraints</think>Kiefer Sutherland."


def _run_dir(tmp_path: Path, template: str) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "arm_env.json").write_text('{"config_template": "%s"}' % template)
    return str(tmp_path)


def test_strict_bar_blanks_an_out_of_band_answer():
    """The failure this fix is about: a real answer read as no answer."""
    assert adapter.extract(_OOB_ANSWER, exempt_closing_tag=False).strip() == ""


def test_lenient_bar_keeps_the_same_answer():
    """Green direction. Without this the test above alone would also pass on a
    bar that blanks everything."""
    assert "Kiefer" in adapter.extract(_OOB_ANSWER, exempt_closing_tag=True)


def test_both_bars_agree_when_the_tag_is_present():
    """The fix must not move the case it is not for -- otherwise it is not a
    correction, it is a second ruler."""
    strict = adapter.extract(_CLOSED_ANSWER, exempt_closing_tag=False)
    lenient = adapter.extract(_CLOSED_ANSWER, exempt_closing_tag=True)
    assert strict.strip() == lenient.strip() != ""


def test_flow_off_arm_resolves_false_and_flow_on_arm_resolves_true(tmp_path):
    """``_flow_enabled`` must move in both directions on real templates, and
    return None -- not False -- when it cannot tell. 'Missing' is not 'off':
    reading it as off would silently apply the lenient bar to an arm whose
    config simply failed to load."""
    off = _run_dir(tmp_path / "off", "configs/dr32_web_base128.json")
    on = _run_dir(tmp_path / "on", "configs/dr33_web_dsv4f_dr128.json")
    assert adapter._flow_enabled(off) is False
    assert adapter._flow_enabled(on) is True
    assert adapter._flow_enabled(str(tmp_path / "nonexistent")) is None


def test_the_two_bars_actually_differ_on_a_flow_off_arm(tmp_path):
    """The whole point, stated as one assertion: on an arm whose flow is off and
    whose model reasons out of band, the config-derived bar and the
    runtime-derived bar give opposite answers about the same text."""
    off = Path(_run_dir(tmp_path / "off", "configs/dr32_web_base128.json"))
    config_bar_requires_tag = adapter._think_closing_tag_required(str(off))
    runtime_bar_requires_tag = bool(adapter._flow_enabled(str(off))) and config_bar_requires_tag
    assert config_bar_requires_tag is True
    assert runtime_bar_requires_tag is False
    assert adapter.extract(_OOB_ANSWER, exempt_closing_tag=not config_bar_requires_tag).strip() == ""
    assert "Kiefer" in adapter.extract(_OOB_ANSWER, exempt_closing_tag=not runtime_bar_requires_tag)
