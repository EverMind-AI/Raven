"""Unit tests for the keyword context synthesizer.

Pins the synthesizer's contract independent of any specific benchmark —
catches benchmark-targeted regressions (e.g. someone tuning rules to hit
ProactiveBench 4-cell stratification). Generic inputs only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add runners/ to sys.path so the synthesizers package is importable.
# Path: benchmarks/proactivity_eval/runners.
_RUNNERS = (
    Path(__file__).resolve().parent.parent
    / "benchmarks"
    / "proactivity_eval"
    / "runners"
)
if str(_RUNNERS) not in sys.path:
    sys.path.insert(0, str(_RUNNERS))

from synthesizers import SYNTHESIZERS, get_synthesizer  # noqa: E402
from synthesizers.keyword import KeywordSynthesizer  # noqa: E402


@pytest.fixture
def synth():
    return KeywordSynthesizer()


# ---------------------------------------------------------------------------
# Profile detection

def test_empty_obs_returns_general_profile(synth):
    ctx = synth.synthesize([])
    assert "focused work session" in ctx.user_profile
    assert ctx.routines == []
    assert ctx.memory_md == ""


def test_coding_python_detection(synth):
    obs = [
        {"time": "1", "event": "User opens main.py in VSCode"},
        {"time": "2", "event": "User runs python main.py"},
    ]
    ctx = synth.synthesize(obs)
    assert "Python" in ctx.user_profile
    assert "developer" in ctx.user_profile.lower()


def test_coding_ruby_detection(synth):
    obs = [
        {"time": "1", "event": "User opens CheckName.rb in IDE"},
        {"time": "2", "event": "User runs ruby tests"},
    ]
    ctx = synth.synthesize(obs)
    assert "Ruby" in ctx.user_profile


def test_writing_falls_back_without_topic(synth):
    obs = [
        {"time": "1", "event": "User writes a markdown paragraph"},
        {"time": "2", "event": "User writes a markdown document"},
    ]
    ctx = synth.synthesize(obs)
    assert "writing" in ctx.user_profile.lower()


def test_research_with_topic(synth):
    obs = [
        {"time": "1", "event": "User researches on sustainable fashion brands"},
        {"time": "2", "event": "User searches for sustainable fashion brands"},
    ]
    ctx = synth.synthesize(obs)
    assert "sustainable fashion" in ctx.user_profile.lower()


def test_topic_regex_does_not_swallow_location_clause(synth):
    """'research on X in VSCode' → extracted topic=X, not 'X in VSCode'.

    Verifies the extractor directly — whether the topic surfaces in the final
    user_profile depends on category detection which is tested elsewhere.
    """
    obs = [
        {"time": "1", "event": "User types a Markdown entry about research on sustainable branding in Visual Studio Code."},
    ]
    topic = synth._extract_topic(obs)
    assert topic is not None
    assert "visual studio" not in topic.lower()
    assert "sustainable" in topic.lower()


def test_falls_back_to_general_when_unknown(synth):
    obs = [{"time": "1", "event": "The system runs background tasks"}]
    ctx = synth.synthesize(obs)
    assert ctx.user_profile == "The user is engaged in a focused work session."


# ---------------------------------------------------------------------------
# Routine emission invariants

def test_routines_below_threshold_not_emitted(synth):
    obs = [
        {"time": "1", "event": "User searches Google for 'ruby'"},
        {"time": "2", "event": "User searches Google for 'python'"},
    ]
    ctx = synth.synthesize(obs)
    assert all("search" not in r.pattern.lower() for r in ctx.routines)


def test_routines_at_threshold_emit_candidate(synth):
    obs = [
        {"time": str(i), "event": f"User searches for topic{i}"}
        for i in range(3)
    ]
    ctx = synth.synthesize(obs)
    assert any("search" in r.pattern.lower() for r in ctx.routines)


def test_editor_browser_alternation_routine(synth):
    obs = [
        {"time": "1", "event": "User edits code in VSCode"},
        {"time": "2", "event": "User switches to browser and searches Google"},
        {"time": "3", "event": "User scrolls through search results"},
        {"time": "4", "event": "User returns to VSCode"},
    ]
    ctx = synth.synthesize(obs)
    pattern_text = " ".join(r.pattern for r in ctx.routines)
    assert "alternat" in pattern_text.lower()


def test_candidate_routines_never_user_confirmed(synth):
    """CRITICAL invariant: synthesizer must never pretend a routine is verified.
    user_confirmed=True / status=active are reserved for real user interaction.
    Violation would cause Planner to treat fabricated signals as ground truth."""
    obs = [{"time": str(i), "event": "User searches"} for i in range(10)]
    ctx = synth.synthesize(obs)
    assert ctx.routines, "expected at least one routine for this input"
    for r in ctx.routines:
        assert r.status == "candidate", f"got status={r.status!r} on {r.id}"
        assert r.user_confirmed is False, f"got confirmed=True on {r.id}"


def test_routine_count_upper_bound(synth):
    """Don't spam Planner with many candidate routines."""
    obs = (
        [{"time": str(i), "event": "User edits in VSCode"} for i in range(5)]
        + [{"time": str(i), "event": "User searches Google"} for i in range(5, 15)]
    )
    ctx = synth.synthesize(obs)
    assert len(ctx.routines) <= 2


# ---------------------------------------------------------------------------
# Memory line rules

def test_memory_empty_for_short_sessions(synth):
    obs = [
        {"time": "1000", "event": "User opens a file"},
        {"time": "1010", "event": "User types a character"},
    ]
    ctx = synth.synthesize(obs)
    assert ctx.memory_md == ""


def test_memory_populated_for_long_sessions(synth):
    obs = [
        {"time": "1000", "event": "User opens main.py in VSCode"},
        {"time": "1300", "event": "User steps through debugger"},
    ]
    ctx = synth.synthesize(obs)
    assert ctx.memory_md != ""
    assert "min session" in ctx.memory_md


def test_prose_timestamps_dont_crash(synth):
    """Some ProactiveAgent records use prose like 'Day 1, 12:30 AM'."""
    obs = [
        {"time": "Day 1, 12:30 AM", "event": "User opens a Ruby file"},
        {"time": "Day 1, 12:35 AM", "event": "User searches for Ruby syntax"},
    ]
    ctx = synth.synthesize(obs)
    assert isinstance(ctx.user_profile, str)
    # Duration uncomputable from prose timestamps → memory_md stays empty.
    assert ctx.memory_md == ""


# ---------------------------------------------------------------------------
# Determinism + registry

def test_deterministic(synth):
    obs = [
        {"time": "1", "event": "User opens main.py"},
        {"time": "2", "event": "User searches for Python imports"},
        {"time": "3", "event": "User searches for modules"},
        {"time": "4", "event": "User searches for decorators"},
    ]
    a = synth.synthesize(obs)
    b = synth.synthesize(obs)
    assert a.user_profile == b.user_profile
    assert [r.pattern for r in a.routines] == [r.pattern for r in b.routines]
    assert a.memory_md == b.memory_md


def test_registry_lookup():
    assert "keyword" in SYNTHESIZERS
    s = get_synthesizer("keyword")
    assert s.name == "keyword"


def test_registry_unknown_name_raises():
    with pytest.raises(KeyError, match="Unknown synthesizer"):
        get_synthesizer("bogus")


def test_kwargs_passthrough():
    s = get_synthesizer("keyword", routine_threshold=5, min_duration_for_memory=120)
    assert s.routine_threshold == 5
    assert s.min_duration_for_memory == 120
