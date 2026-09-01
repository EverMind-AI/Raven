"""Guardrail switch semantics: the call site owns the default, env overrides.

Ported from the fork's tests/test_gate_env.py against the plugin's
completion-gates module. On the swarm-integration line every gate is opt-in
(the loop passes ``default=False``): an orchestrated worker also serves
non-coding requests (explain code, follow-up Q&A turns), where change-the-code
gates only mis-fire. Coding-style runs arm gates explicitly through the
environment. An explicit environment value overrides the default in either
direction.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow import completion_gates as cg  # noqa: E402

SWITCH = "RAVEN_TEST_SWITCH"


def test_unset_follows_the_call_site_default(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    assert cg.gate_enabled(SWITCH, default=True) is True
    assert cg.gate_enabled(SWITCH, default=False) is False


def test_the_default_is_mandatory_so_a_new_gate_cannot_silently_arm(monkeypatch):
    """Omitting the default used to yield True, which on this line is the
    opposite of the policy: a gate added later as ``gate_enabled(NEW_ENV)``
    would arm itself in an orchestrated worker -- exactly the mis-firing this
    branch exists to prevent. The call site must state its policy."""
    monkeypatch.delenv(SWITCH, raising=False)
    with pytest.raises(TypeError):
        cg.gate_enabled(SWITCH)


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "anything"])
def test_explicit_truthy_forces_on_even_in_interactive_mode(monkeypatch, raw):
    monkeypatch.setenv(SWITCH, raw)
    assert cg.gate_enabled(SWITCH, default=False) is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " off ", "disabled"])
def test_explicit_opt_out_forces_off_even_in_task_mode(monkeypatch, raw):
    monkeypatch.setenv(SWITCH, raw)
    assert cg.gate_enabled(SWITCH, default=True) is False
