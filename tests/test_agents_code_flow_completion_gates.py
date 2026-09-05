"""Completion-gate guardrail (empty-diff falsification): pure decision logic.

Ported from the fork's tests/test_completion_gates.py against the plugin's
completion-gates module. Written test-first (2026-08-10). Evidence base:
trajectory error analysis (two independent model rounds; the evidence chain
lives in the guardrail design notes). Design constraints inherited from the
fork's guardrail design notes: opt-in via env (default OFF, behavior
byte-identical when unset), one-shot per turn, facts only,
innocent-until-proven, never inject on the last iteration.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow import completion_gates as cg  # noqa: E402

# ---------------------------------------------------------------- empty-diff gate


def _nudge(**overrides):
    kwargs = dict(
        enabled=True,
        code_edits_observed=False,
        workspace_dirty=None,
        already_nudged=False,
        iteration=3,
        max_iterations=200,
    )
    kwargs.update(overrides)
    return cg.empty_diff_nudge(**kwargs)


def test_empty_diff_disabled_is_silent():
    assert _nudge(enabled=False) is None


def test_empty_diff_fires_once_when_finishing_with_no_edits():
    text = _nudge()
    assert text is not None
    # Strong form: must demand the discriminating evidence -- a symptom
    # reproduction that fails on the current, unmodified code.
    assert "unmodified" in text.lower()
    assert "reproduc" in text.lower()


def test_empty_diff_respects_one_shot():
    assert _nudge(already_nudged=True) is None


def test_empty_diff_silent_when_code_was_edited():
    assert _nudge(code_edits_observed=True) is None


def test_empty_diff_never_fires_on_last_iteration():
    assert _nudge(iteration=200, max_iterations=200) is None


# ------------------------------------------------- workspace state beats turn state

# Regression (2026-08-13): ``code_edits_observed`` is turn-local, but a harness may
# run a second turn to collect a completion token. The edits then live in turn one,
# the second turn edits nothing, and the gate asserted "no repository modification"
# over a workspace holding a 8 KB diff. Measured 12/12 false positives on WorkBuddy
# code; the model rebutted the claim in-trajectory. The workspace's own git is the
# authority whenever it can answer.


def test_empty_diff_silent_when_the_workspace_already_holds_changes():
    assert _nudge(workspace_dirty=True, code_edits_observed=False) is None


def test_empty_diff_fires_when_the_workspace_is_verifiably_clean():
    assert _nudge(workspace_dirty=False, code_edits_observed=False) is not None


def test_empty_diff_needs_both_signals_to_agree_before_firing():
    assert _nudge(workspace_dirty=True, code_edits_observed=False) is None
    assert _nudge(workspace_dirty=False, code_edits_observed=True) is None
    assert _nudge(workspace_dirty=True, code_edits_observed=True) is None
    assert _nudge(workspace_dirty=False, code_edits_observed=False) is not None


def test_empty_diff_falls_back_to_turn_edits_when_git_cannot_answer():
    assert _nudge(workspace_dirty=None, code_edits_observed=True) is None
    assert _nudge(workspace_dirty=None, code_edits_observed=False) is not None


# ------------------------------------------------------------- the git probe itself


@pytest.mark.asyncio
async def test_probe_reports_none_outside_a_git_repository(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert await cg.workspace_has_changes(tmp_path) is None


@pytest.mark.asyncio
async def test_probe_reports_false_for_a_clean_checkout(tmp_path):
    _init_repo(tmp_path)
    assert await cg.workspace_has_changes(tmp_path) is False


@pytest.mark.asyncio
async def test_probe_reports_true_for_a_modified_tracked_file(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "app.py").write_text("x = 2\n", encoding="utf-8")
    assert await cg.workspace_has_changes(tmp_path) is True


@pytest.mark.asyncio
async def test_probe_reports_true_for_a_new_untracked_file(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "new_module.py").write_text("y = 1\n", encoding="utf-8")
    assert await cg.workspace_has_changes(tmp_path) is True


@pytest.mark.asyncio
async def test_probe_ignores_documentation_and_scratch_paths(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "NOTES.md").write_text("thinking out loud\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "plan.rst").write_text("plan\n", encoding="utf-8")
    assert await cg.workspace_has_changes(tmp_path) is False


def _init_repo(path: Path) -> None:
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    for args in (
        ("init", "-q"),
        ("add", "-A"),
        ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"),
    ):
        subprocess.run(("git", *args), cwd=path, check=True, env=env, capture_output=True)
