"""Unit tests for the host-side Raven-Research launcher reply composition.

The launcher (subagents/raven-research/run.py) appends the research trail to
the answer and prints the pair; Raven's CLI backend tail-truncates that stdout
to the manifest's ``maxOutputChars``. These tests pin ``compose_reply``'s
budgeting so the trail is never lost without a notice.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO_ROOT / "subagents" / "raven-research" / "run.py"
_TRUNC_NOTE = "\n\n[report truncated to preserve the research trail]"
_DROP_NOTE = "\n\n[research trail dropped: exceeds the host's output cap]"


@pytest.fixture(scope="module")
def launch() -> dict:
    spec = importlib.util.spec_from_file_location("raven_research_launcher", _LAUNCHER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {
        "compose_reply": mod.compose_reply,
        "manifest_output_cap": mod.manifest_output_cap,
    }


def test_empty_trail(launch: dict) -> None:
    reply, kept = launch["compose_reply"]("answer", "", 30000)
    assert reply == "answer"
    assert kept is False


def test_whitespace_only_trail(launch: dict) -> None:
    reply, kept = launch["compose_reply"]("answer", "  \n  ", 30000)
    assert reply == "answer"
    assert kept is False


def test_no_cap_appends_verbatim(launch: dict) -> None:
    reply, kept = launch["compose_reply"]("answer", "\n---\n\nsources", None)
    assert reply == "answer\n\n---\n\nsources"
    assert kept is True


def test_fits_under_cap(launch: dict) -> None:
    trail = "\n---\n\n**Research trail**\n- a\n- b"
    reply, kept = launch["compose_reply"]("short answer", trail, 30000)
    assert kept is True
    assert reply == "short answer\n\n---\n\n**Research trail**\n- a\n- b"


def test_unmodified_pair_at_exact_cap_is_untouched(launch: dict) -> None:
    """The boundary is the unmodified pair, not the pair plus a notice budget.

    Reserving notice room before testing this is what produced a false
    truncation notice on reports that already fitted.
    """
    answer, trail = "answer", "tt"
    cap = len(answer) + 2 + len(trail)
    reply, kept = launch["compose_reply"](answer, trail, cap)
    assert kept is True
    assert len(reply) == cap
    assert reply == f"{answer}\n\n{trail}"
    assert _TRUNC_NOTE not in reply


def test_one_char_over_cap_truncates_and_keeps_trail(launch: dict) -> None:
    answer, trail = "a" * 200, "tt"
    cap = len(answer) + 2 + len(trail) - 1
    reply, kept = launch["compose_reply"](answer, trail, cap)
    assert kept is True
    assert len(reply) <= cap
    assert reply.endswith(trail)
    assert _TRUNC_NOTE in reply


def test_long_answer_short_trail_under_real_cap_is_untouched(launch: dict) -> None:
    """Regression: 29,900 + 2 + 90 = 29,992 fits 30,000 and must not be cut."""
    answer, trail = "A" * 29900, "T" * 90
    reply, kept = launch["compose_reply"](answer, trail, 30000)
    assert kept is True
    assert reply == f"{answer}\n\n{trail}"
    assert _TRUNC_NOTE not in reply


def test_short_answer_long_trail_under_real_cap_keeps_trail(launch: dict) -> None:
    """Regression: 10 + 2 + 29,980 fits 30,000 and the trail must survive whole."""
    answer, trail = "A" * 10, "T" * 29980
    reply, kept = launch["compose_reply"](answer, trail, 30000)
    assert kept is True
    assert reply == f"{answer}\n\n{trail}"
    assert _DROP_NOTE not in reply


def test_answer_reserved_so_trail_survives(launch: dict) -> None:
    cap = 300
    trail = "\n---\n\n" + "q" * 120
    reply, kept = launch["compose_reply"]("A" * 500, trail, cap)
    assert kept is True
    assert len(reply) <= cap
    assert reply.endswith(trail.strip())
    assert _TRUNC_NOTE in reply
    assert "A" * 500 not in reply


def test_trail_alone_cannot_fit_is_dropped_loudly(launch: dict) -> None:
    cap = 60
    trail = "\n---\n\n" + "q" * 200
    reply, kept = launch["compose_reply"]("A" * 10, trail, cap)
    assert kept is False
    assert len(reply) <= cap
    assert trail.strip() not in reply
    assert _DROP_NOTE in reply


def test_manifest_cap_matches_manifest(launch: dict) -> None:
    manifest = json.loads((_LAUNCHER.parent / "subagent.json").read_text(encoding="utf-8"))
    assert launch["manifest_output_cap"]() == manifest["maxOutputChars"]
