"""Unit tests for the claude-CLI driver transport (providers.claude_cli).

Every evolver LLM role rides this seam when provider=claude_cli; a subprocess
is injected so the transcript serialization, JSON result parsing, retry and
rate-limit behavior are pinned without touching a real CLI or sleeping.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from raven.evolver.orchestrator.providers.claude_cli import (
    make_claude_call_fn,
    render_messages,
)


def _proc(result: str = "ok", *, returncode: int = 0, is_error: bool = False,
          stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps({"result": result, "is_error": is_error}),
        stderr=stderr,
    )


class _FakeRun:
    """subprocess.run stand-in replaying scripted results; records invocations."""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, argv, **kw):
        self.calls.append({"argv": list(argv), **kw})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestRenderMessages:
    def test_single_user_message_passes_through(self):
        system, prompt = render_messages([
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
        ])
        assert system == "be terse" and prompt == "hello"

    def test_multi_turn_serializes_with_role_tags(self):
        system, prompt = render_messages([
            {"role": "user", "content": "do X"},
            {"role": "assistant", "content": "did X"},
            {"role": "user", "content": "now Y"},
        ])
        assert system == ""
        assert "[USER]\ndo X" in prompt
        assert "[ASSISTANT]\ndid X" in prompt
        assert prompt.strip().endswith("now Y")
        assert "NEXT message only" in prompt  # the continue-instruction header


class TestCallFn:
    def test_success_path_and_argv_shape(self):
        run = _FakeRun([_proc("answer")])
        call = make_claude_call_fn("claude-test", run=run, retry_delays=())
        assert call([{"role": "user", "content": "q"}]) == "answer"
        argv = run.calls[0]["argv"]
        assert argv[:2] == ["claude", "-p"]
        assert argv[argv.index("--model") + 1] == "claude-test"
        # Tools must be OFF: this is Claude-as-completion, the FSM keeps control.
        assert argv[argv.index("--allowedTools") + 1] == ""
        # The prompt travels via stdin (argv overflows on rendered trajectories).
        assert run.calls[0]["input"] == "q"

    def test_retries_then_succeeds(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", sleeps.append)
        run = _FakeRun([_proc("", returncode=1, stderr="transient"), _proc("recovered")])
        call = make_claude_call_fn("m", run=run, retry_delays=(3.0,))
        assert call([{"role": "user", "content": "q"}]) == "recovered"
        assert sleeps == [3.0]

    def test_rate_limit_stretches_the_delay(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", sleeps.append)
        run = _FakeRun([_proc("", returncode=1, stderr="429 rate limit"), _proc("ok")])
        call = make_claude_call_fn("m", run=run, retry_delays=(3.0,),
                                   rate_limit_delay=120.0)
        assert call([{"role": "user", "content": "q"}]) == "ok"
        assert sleeps == [120.0]

    def test_exhausted_retries_raise_with_cause(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        run = _FakeRun([_proc("", returncode=1, stderr="dead")] * 2)
        call = make_claude_call_fn("m", run=run, retry_delays=(1.0,))
        with pytest.raises(RuntimeError, match="failed after 2 attempts") as exc:
            call([{"role": "user", "content": "q"}])
        assert "dead" in str(exc.value.__cause__)

    def test_is_error_and_empty_results_are_failures(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        for bad in (_proc("boom", is_error=True), _proc("   ")):
            run = _FakeRun([bad])
            call = make_claude_call_fn("m", run=run, retry_delays=())
            with pytest.raises(RuntimeError):
                call([{"role": "user", "content": "q"}])
