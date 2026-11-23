"""Availability is judged at capture; a switch flipped later only changes whether calls run.

The array is the prompt-cache prefix and is frozen for the session by
``ToolRegistry``. A tool that is unavailable at the refresh point does not
enter it at all. This file pins the loop's half: what capture filters, the
reasons it can name, the notices it sends for a switch flipped after capture,
and the refusal a call gets.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from raven.agent.loop.main import AgentLoop
from raven.context_engine.runtime_context import notice_changes, notice_key
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


def test_a_switched_off_tool_never_enters_the_array(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.json"
    _write_switches(cfg, ["grep"])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)

    with loop.tools.session_scope_for("s1"):
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}

    assert "grep" not in names, "capture filters what the operator switched off"
    assert loop.tools.has("grep"), "withheld, not destroyed -- the switch stays reversible"


def test_the_five_unconfigured_tools_stay_out_of_the_array(tmp_path: Path, monkeypatch) -> None:
    """An empty config leaves five registered tools with nothing behind them.

    Credentials and a selected base are judged at capture like any other
    availability switch, so their schemas never reach the model.
    """
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)

    with loop.tools.session_scope_for("s1"):
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
        notes = loop._tool_notices()

    for name in ("image_generate", "knowledge_search", "text_to_speech", "video_generate", "web_search"):
        assert name not in names, name
    assert not any(notice_key(note) == ("tool", "web_search") for note in notes), notes


def test_a_key_added_after_capture_joins_at_the_next_refresh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    loop.tools.bind_session_revision("s1", "log-a")

    with loop.tools.session_scope_for("s1"):
        assert "web_search" not in {d["function"]["name"] for d in loop.tools.get_definitions()}

    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    loop.tools.bind_session_revision("s1", "log-b")

    with loop.tools.session_scope_for("s1"):
        assert "web_search" in {d["function"]["name"] for d in loop.tools.get_definitions()}


def test_a_switch_flipped_mid_session_keeps_the_schema_and_gets_a_notice(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)

    with loop.tools.session_scope_for("s1"):
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
        assert "grep" in names
        _write_switches(cfg, ["grep"])
        assert not loop.tools.offers_by_name("grep")
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
        assert "grep" in names, "the array is the prefix; a switch must not move it"
        notes = loop._tool_notices()

    by_key = {notice_key(note): note for note in notes}
    assert ("tool", "grep") in by_key, notes
    assert "switched off in settings" in by_key[("tool", "grep")]


def test_a_credential_withdrawn_after_capture_gets_a_notice(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    loop = _loop(tmp_path)

    with loop.tools.session_scope_for("s1"):
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
        assert "web_search" in names
        monkeypatch.delenv("SERPER_API_KEY")
        notes = loop._tool_notices()

    by_key = {notice_key(note): note for note in notes}
    assert ("tool", "web_search") in by_key, notes
    assert "no search key" in by_key[("tool", "web_search")]


def test_an_unselected_knowledge_base_after_capture_gets_a_notice(tmp_path: Path, monkeypatch) -> None:
    """A base the session selected at capture is in the array; unselecting it
    afterwards must leave the schema in place and explain the refusal."""
    from raven.agent import knowledge_scope

    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)

    with knowledge_scope.bind(("kb-1",), "s1"):
        with loop.tools.session_scope_for("s1"):
            names = {d["function"]["name"] for d in loop.tools.get_definitions()}
            assert "knowledge_search" in names

    with loop.tools.session_scope_for("s1"):
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
        notes = loop._tool_notices()
        answer = asyncio.run(loop.tools.execute("knowledge_search", {"query": "x"}))

    assert "knowledge_search" in names, "the array is the prefix; unselecting must not move it"
    by_key = {notice_key(note): note for note in notes}
    assert ("tool", "knowledge_search") in by_key, notes
    assert "no knowledge base is selected" in by_key[("tool", "knowledge_search")]
    assert "no knowledge base is selected" in answer


def test_a_tool_switched_off_after_capture_leaves_at_the_next_refresh(tmp_path: Path, monkeypatch) -> None:
    """The refresh point re-applies the availability filter; it does not merely
    recapture the names of the array it replaces."""
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)
    loop.tools.bind_session_revision("s1", "log-a")

    with loop.tools.session_scope_for("s1"):
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
        assert "grep" in names
        _write_switches(cfg, ["grep"])
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
        assert "grep" in names, "the array is the prefix; a switch must not move it"

    loop.tools.bind_session_revision("s1", "log-b")

    with loop.tools.session_scope_for("s1"):
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
    assert "grep" not in names, "the next refresh re-applies the availability filter"


def test_a_constructor_disabled_tool_stays_out_and_gets_no_notice(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path, disabled_tools=["grep"])

    with loop.tools.session_scope_for("s1"):
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
        assert "grep" not in names, "the eval harness subset never enters the snapshot"
        notes = loop._tool_notices()

    assert not any(notice_key(note) == ("tool", "grep") for note in notes)


def _capture_without_web_search(loop) -> None:
    loop.tools.bind_session_revision("s1", "log-a")
    with loop.tools.session_scope_for("s1"):
        assert "web_search" not in {d["function"]["name"] for d in loop.tools.get_definitions()}


def test_a_tool_that_became_available_is_announced_with_its_schema(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)

    monkeypatch.setenv("SERPER_API_KEY", "test-key")

    with loop.tools.session_scope_for("s1"):
        names = {d["function"]["name"] for d in loop.tools.get_definitions()}
        notes = loop._tool_notices()

    assert "web_search" not in names, "the array is the prefix; a late key must not move it"
    by_key = {notice_key(note): note for note in notes}
    assert ("tool", "web_search") in by_key, notes
    line = by_key[("tool", "web_search")]
    assert "now available" in line
    assert "calls to it run normally" in line
    schema = json.dumps(loop.tools.definition_of("web_search"), separators=(",", ":"))
    assert schema in line, "the model needs the definition it was never shown"
    assert "UNTRUSTED" not in line, "this is our own definition, not external data"


def test_a_tool_announced_as_available_actually_runs(tmp_path: Path, monkeypatch) -> None:
    class _Serving:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, *args, **kwargs):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url: str, **kwargs):
            import httpx

            self.calls.append(url)
            payload = {"organic": [{"title": "Raven", "link": "https://a.example", "snippet": "A bird."}]}
            return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)

    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    client = _Serving()
    monkeypatch.setattr("raven.agent.tools.web.httpx.AsyncClient", client)

    with loop.tools.session_scope_for("s1"):
        notes = loop._tool_notices()
        answer = asyncio.run(loop.tools.execute("web_search", {"query": "hello"}))

    assert any("now available" in note for note in notes), notes
    assert "Results for: hello" in answer, answer
    assert "https://a.example" in answer
    assert "not available" not in answer, "the announced tool must not be refused as absent"
    assert "currently unavailable" not in answer
    assert client.calls, "the call never reached the vendor"


def test_the_announcement_stops_at_the_next_refresh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    with loop.tools.session_scope_for("s1"):
        assert any("now available" in note for note in loop._tool_notices())

    loop.tools.bind_session_revision("s1", "log-b")

    with loop.tools.session_scope_for("s1"):
        entries = {d["function"]["name"]: d for d in loop.tools.get_definitions()}
        notes = loop._tool_notices()

    assert "web_search" in entries, "the refresh captures the tool it can now offer"
    assert entries["web_search"] == loop.tools.definition_of("web_search"), (
        "the announced definition is the array's own JSON, not a second rendering"
    )
    assert not any("now available" in note for note in notes), notes
    assert not any('"name":"web_search"' in note for note in notes), "the array already carries it"


def test_an_unchanged_tool_is_not_announced(tmp_path: Path, monkeypatch) -> None:
    """Only the tool that changed is reported. A tool off at capture and still
    off was never available, and a tool the array excludes by design was
    available all along: announcing either is the every-turn spam the change
    detection exists to avoid."""
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)

    monkeypatch.setenv("SERPER_API_KEY", "test-key")

    with loop.tools.session_scope_for("s1"):
        notes = loop._tool_notices()
        array = loop.tools.session_definition_names() or frozenset()

    keys = {notice_key(note) for note in notes}
    assert ("tool", "web_search") in keys, notes
    for still_off in ("image_generate", "knowledge_search", "text_to_speech", "video_generate"):
        assert ("tool", still_off) not in keys, f"{still_off} never changed and must stay silent"
    for already_available in ("cancel_dag", "dag_status", "resolve_dag_node"):
        assert loop.tools.has(already_available), "registered"
        assert already_available not in array, "excluded from the array by design"
        assert ("tool", already_available) not in keys, f"{already_available} was available all along"


def test_the_announcement_is_not_resent_while_it_still_holds(tmp_path: Path, monkeypatch) -> None:
    """The line stays true until the refresh, so it is returned again; the notice
    machinery sends it only when the replayed history does not already hold it."""
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)
    monkeypatch.setenv("SERPER_API_KEY", "test-key")

    with loop.tools.session_scope_for("s1"):
        first = loop._tool_notices()
    assert first

    with loop.tools.session_scope_for("s1"):
        second = loop._tool_notices()

    told = {notice_key(note): note for note in first if notice_key(note)}
    assert notice_changes(second, told) == [], "an unchanged announcement must not be sent again"


def test_a_switch_flipped_mid_session_refuses_with_the_reason(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)

    with loop.tools.session_scope_for("s1"):
        loop.tools.get_definitions()
        _write_switches(cfg, ["grep"])
        answer = asyncio.run(loop.tools.execute("grep", {}))

    assert "currently unavailable" in answer
    assert "switched off in settings" in answer
    assert "not available" not in answer, "an advertised tool is not 'absent'"


def test_a_tool_filtered_from_the_array_reads_as_absent(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.json"
    _write_switches(cfg, ["grep"])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)

    with loop.tools.session_scope_for("s1"):
        loop.tools.get_definitions()
        answer = asyncio.run(loop.tools.execute("grep", {}))

    assert "not available" in answer
    assert "currently unavailable" not in answer, "it was never advertised in this session"


def test_a_charter_restriction_is_not_reported_as_a_settings_switch(tmp_path: Path, monkeypatch) -> None:
    from raven.agent.subagent.charter import Charter, charter_scope

    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    with loop.tools.session_scope_for("s1"):
        loop.tools.get_definitions()
        with charter_scope(Charter(tools=("grep",))):
            notes = loop._tool_notices()

    assert not any(notice_key(note) == ("tool", "exec") for note in notes), notes
