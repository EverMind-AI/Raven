"""A live off switch changes availability, never the array; the runtime context says which.

The array is the prompt-cache prefix and is frozen for the session by
``ToolRegistry``. This file pins the loop's half: the reasons it can name, the
notices it sends, and the refusal a call gets when the schema is visible but the
switch is off.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from raven.agent.loop.main import AgentLoop
from raven.context_engine.runtime_context import notice_key
from tests._wiring import wire


class _StubProvider:
    """Construction reads the default model; no turn is run here."""

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs):  # pragma: no cover - never invoked
        raise NotImplementedError


def _loop(tmp_path: Path, **kwargs) -> AgentLoop:
    return AgentLoop(provider=_StubProvider(), workspace=tmp_path / "ws", **wire(**kwargs))


def _write_switches(path: Path, names: list[str]) -> None:
    path.write_text(json.dumps({"tools": {"disabledTools": names}}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_ambient_search_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERPER_API_KEY", raising=False)


def test_a_live_disabled_tool_keeps_its_schema_and_gets_a_notice(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.json"
    _write_switches(cfg, ["grep"])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)

    with loop.tools.session_scope_for("s1"):
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
        assert "grep" in names, "the array is the prefix; a switch must not move it"
        assert not loop.tools.offers_by_name("grep")
        notes = loop._tool_notices()

    by_key = {notice_key(note): note for note in notes}
    assert ("tool", "grep") in by_key, notes
    assert "switched off in settings" in by_key[("tool", "grep")]


def test_an_unconfigured_tool_says_why_it_cannot_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)

    with loop.tools.session_scope_for("s1"):
        loop.tools.get_definitions()
        notes = loop._tool_notices()

    by_key = {notice_key(note): note for note in notes}
    assert ("tool", "web_search") in by_key, notes
    assert "no search key" in by_key[("tool", "web_search")]
    assert ("tool", "knowledge_search") in by_key, notes
    assert "no knowledge base is selected" in by_key[("tool", "knowledge_search")]


def test_a_constructor_disabled_tool_stays_out_and_gets_no_notice(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path, disabled_tools=["grep"])

    with loop.tools.session_scope_for("s1"):
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
        assert "grep" not in names, "the eval harness subset never enters the snapshot"
        notes = loop._tool_notices()

    assert not any(notice_key(note) == ("tool", "grep") for note in notes)


def test_an_unavailable_tool_call_is_refused_with_the_reason(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.json"
    _write_switches(cfg, ["grep"])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)

    with loop.tools.session_scope_for("s1"):
        loop.tools.get_definitions()
        answer = asyncio.run(loop.tools.execute("grep", {}))

    assert "currently unavailable" in answer
    assert "switched off in settings" in answer
    assert "not available" not in answer, "an advertised tool is not 'absent'"


def test_a_charter_restriction_is_not_reported_as_a_settings_switch(tmp_path: Path, monkeypatch) -> None:
    from raven.agent.subagent.charter import Charter, charter_scope

    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    with loop.tools.session_scope_for("s1"):
        loop.tools.get_definitions()
        with charter_scope(Charter(tools=("grep",))):
            notes = loop._tool_notices()

    assert not any(notice_key(note) == ("tool", "exec") for note in notes), notes
