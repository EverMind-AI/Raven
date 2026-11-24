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
from raven.context_engine.runtime_context import notice_changes, notice_key, told_notices
from raven.session.manager import Session, SessionManager
from tests._notice_log import NoticeLog as _NoticeLog
from tests._notice_log import filed as _filed
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


class _ServingSearch:
    """A stand-in for the vendor client: proves an announced tool really dispatches."""

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


@pytest.mark.parametrize("refresh", ["clear", "compaction"])
def test_a_key_added_after_capture_joins_at_the_next_refresh(tmp_path: Path, monkeypatch, refresh: str) -> None:
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    loop.tools.bind_session_revision("s1", "log-a")

    with loop.tools.session_scope_for("s1"):
        assert "web_search" not in {d["function"]["name"] for d in loop.tools.get_definitions()}

    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    _refresh(loop, refresh)

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


@pytest.mark.parametrize("refresh", ["clear", "compaction"])
def test_a_tool_switched_off_after_capture_leaves_at_the_next_refresh(
    tmp_path: Path, monkeypatch, refresh: str
) -> None:
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

    _refresh(loop, refresh)

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


def _compact(loop: AgentLoop, log: "_NoticeLog | None" = None) -> None:
    """The compaction refresh: marked by one turn, cashed by the next bind on the same log."""
    loop.tools.schedule_definitions_refresh("s1")
    (log if log is not None else _NoticeLog()).bind(loop)


def _refresh(loop: AgentLoop, kind: str, log: "_NoticeLog | None" = None) -> None:
    """One refresh point: ``compaction`` on the same log, or ``clear`` -- a new, empty log."""
    log = log if log is not None else _NoticeLog()
    if kind == "clear":
        log.clear(f"{log.log_id}-cleared")
        log.bind(loop)
    else:
        _compact(loop, log)


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
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)

    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    client = _ServingSearch()
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


@pytest.mark.parametrize("refresh", ["clear", "compaction"])
def test_the_announcement_stops_at_the_next_refresh(tmp_path: Path, monkeypatch, refresh: str) -> None:
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    log = _NoticeLog()
    log.turn(loop)
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    assert "now available" in log.turn(loop)[("tool", "web_search")]

    _refresh(loop, refresh, log)

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


def _announce_then_withdraw_web_search(loop, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    """Capture without a search key, add one, then take it away again."""
    announced: list[str] = []
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    with loop.tools.session_scope_for("s1"):
        announced = loop._tool_notices()
    monkeypatch.delenv("SERPER_API_KEY")
    with loop.tools.session_scope_for("s1"):
        withdrawn = loop._tool_notices()
    return announced, withdrawn


def test_a_tool_withdrawn_again_is_reported_unavailable_not_available_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse flip has a sentence of its own.

    Falling out of the announcement leaves a hole in the array's lane (capture
    never put the name there) and in the announcement's (the switch is off
    again), and the notice machinery would fill it with the cleared line, which
    says the tool is available when dispatch refuses it.
    """
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)

    with loop.tools.session_scope_for("s1"):
        before = loop._tool_notices()
    assert not any(notice_key(note) == ("tool", "web_search") for note in before), before

    announced, withdrawn = _announce_then_withdraw_web_search(loop, monkeypatch)

    announced_keys = {notice_key(note) for note in announced if notice_key(note) is not None}
    assert announced_keys == {("tool", "web_search")}, f"only the tool that changed: {announced}"

    by_key = {notice_key(note): note for note in withdrawn}
    line = by_key[("tool", "web_search")]
    assert "currently unavailable" in line, line
    assert "no search key" in line, "the reason is the operator's setting, not a guess"
    assert "available again" not in line, "the cleared line would claim a tool dispatch refuses"
    assert "now available" not in line, line


def test_a_tool_withdrawn_again_is_refused_with_the_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is what makes "available again" a false statement.

    The call does not run, and the answer names the switch instead of reading
    as an unloading or a misspelt name: the model holds this schema from the
    announcement, so "not found" would send it hunting for the wrong fault.
    """
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)
    announced, _ = _announce_then_withdraw_web_search(loop, monkeypatch)
    assert any("now available" in note for note in announced), announced

    with loop.tools.session_scope_for("s1"):
        answer = asyncio.run(loop.tools.execute("web_search", {"query": "x"}))

    assert answer.startswith("Error:"), answer
    assert "currently unavailable" in answer, answer
    assert "no search key" in answer, "the refusal carries the reason, not a generic absence"
    assert "not available" not in answer, answer
    assert "Results for" not in answer, answer


def test_the_withdrawn_again_notice_is_not_resent_while_it_still_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The line stays true for the rest of the window, so it is returned again;
    the notice machinery sends it only when the replayed history lacks it."""
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)
    _, withdrawn = _announce_then_withdraw_web_search(loop, monkeypatch)

    told = {notice_key(note): note for note in withdrawn if notice_key(note) is not None}
    with loop.tools.session_scope_for("s1"):
        again = loop._tool_notices()

    assert any("currently unavailable" in note for note in again), "still withheld inside the window"
    assert notice_changes(again, told) == [], "an unchanged withdrawal must not be sent again"


def test_the_withdrawn_again_notice_survives_a_refresh_while_still_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh drops the name from the array, not the fact that the session was told.

    The key has one meaning: a standing availability note. Omitting it while the
    switch is still off is what let the consumer infer "available again" from a
    refresh."""
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)
    _, withdrawn = _announce_then_withdraw_web_search(loop, monkeypatch)
    assert any("currently unavailable" in note for note in withdrawn), withdrawn
    log = _NoticeLog()
    log.messages.append(_filed(withdrawn))
    told = log.told

    _compact(loop, log)

    with loop.tools.session_scope_for("s1"):
        notes = loop._tool_notices()
        names = loop.tools.session_definition_names() or frozenset()

    assert "web_search" not in names, "the refresh re-judged it and kept it out"
    assert any("currently unavailable" in note for note in notes), notes
    assert notice_changes(notes, told) == [], "an unchanged withdrawal must not be cleared"
    reasons = frozenset(loop._withheld_tool_reasons())
    with loop.tools.session_scope_for("s1"):
        assert loop.tools.notified_withheld_again(reasons) == ["web_search"]

    with loop.tools.session_scope_for("s1"):
        answer = asyncio.run(loop.tools.execute("web_search", {"query": "x"}))
    assert "currently unavailable" in answer, answer
    assert "not available" not in answer, "the session holds this schema; it is not absent"


def test_a_tool_never_announced_is_never_reported_as_withdrawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a real flip is reported. A tool that was off at capture and stayed
    off was never available, and a line per turn for each of them is the spam
    the change detection exists to avoid."""
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)

    for _ in range(3):
        with loop.tools.session_scope_for("s1"):
            notes = loop._tool_notices()
        keys = {notice_key(note) for note in notes}
        for name in ("image_generate", "knowledge_search", "text_to_speech", "video_generate", "web_search"):
            assert ("tool", name) not in keys, f"{name} never changed and must stay silent: {notes}"


def _session_names(loop: AgentLoop) -> frozenset[str]:
    with loop.tools.session_scope_for("s1"):
        return loop.tools.session_definition_names() or frozenset()


def _execute(loop: AgentLoop, name: str, params: dict) -> str:
    with loop.tools.session_scope_for("s1"):
        return asyncio.run(loop.tools.execute(name, params))


def _serve_search(monkeypatch: pytest.MonkeyPatch) -> _ServingSearch:
    client = _ServingSearch()
    monkeypatch.setattr("raven.agent.tools.web.httpx.AsyncClient", client)
    return client


def test_the_reverse_lane_keeps_its_key_across_a_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole reverse-lane cycle: absent, announced, withdrawn, refreshed, enabled.

    Every step executes the tool: the notice is true exactly when dispatch runs
    or refuses as the line says. No step may produce a cleared line.
    """
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    loop.tools.bind_session_revision("s1", "log-a")
    log = _NoticeLog()
    query = {"query": "hello"}

    current = log.turn(loop)
    assert ("tool", "web_search") not in current
    assert "not available" in _execute(loop, "web_search", query)

    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    client = _serve_search(monkeypatch)
    current = log.turn(loop)
    assert "now available" in current[("tool", "web_search")]
    assert "Results for: hello" in _execute(loop, "web_search", query)

    monkeypatch.delenv("SERPER_API_KEY")
    current = log.turn(loop)
    assert "currently unavailable" in current[("tool", "web_search")]
    assert "currently unavailable" in _execute(loop, "web_search", query)

    _compact(loop, log)
    current = log.turn(loop)
    assert "web_search" not in _session_names(loop), "the refresh re-judged it and kept it out"
    assert "currently unavailable" in current[("tool", "web_search")]
    assert log.cleared() == [], "the note still holds; no key may be cleared"
    assert log.sent == [], "the unchanged withdrawal is not re-sent"
    assert "currently unavailable" in _execute(loop, "web_search", query)

    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    current = log.turn(loop)
    assert "now available" in current[("tool", "web_search")]
    assert any("now available" in line for line in log.sent)
    assert log.cleared() == []
    assert "Results for: hello" in _execute(loop, "web_search", query)
    assert client.calls


def test_the_array_lane_keeps_its_key_across_a_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool the array carried, withdrawn, and still withdrawn at the refresh.

    The old array's name is the advertisement; after the refresh the array no
    longer carries it, so only the standing set can keep the notice. Every step
    executes the tool to check that the line matches dispatch.
    """
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)
    loop.tools.bind_session_revision("s1", "log-a")
    note = tmp_path / "note.txt"
    note.write_text("hello from the array lane", encoding="utf-8")
    params = {"path": str(note)}
    log = _NoticeLog()

    current = log.turn(loop)
    assert ("tool", "read_file") not in current
    assert "hello from the array lane" in _execute(loop, "read_file", params)

    _write_switches(cfg, ["read_file"])
    current = log.turn(loop)
    assert "currently unavailable" in current[("tool", "read_file")]
    assert "currently unavailable" in _execute(loop, "read_file", params)

    _compact(loop, log)
    current = log.turn(loop)
    assert "read_file" not in _session_names(loop)
    assert "currently unavailable" in current[("tool", "read_file")]
    assert log.cleared() == []
    assert log.sent == []
    assert "currently unavailable" in _execute(loop, "read_file", params)

    _write_switches(cfg, [])
    current = log.turn(loop)
    assert "now available" in current[("tool", "read_file")]
    assert any("now available" in line for line in log.sent)
    assert log.cleared() == []
    assert "hello from the array lane" in _execute(loop, "read_file", params)

    _compact(loop, log)
    log.turn(loop)
    assert "read_file" in _session_names(loop)
    assert ("tool", "read_file") not in log.told, "the cleared line went into the log"
    log.bind(loop)
    assert not loop.tools._session_notified_tools.get("s1"), "so the next bind speaks for nothing"


def test_a_refresh_that_captures_the_tool_clears_the_note_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the array carries the schema, a cleared line is true: dispatch runs."""
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    loop.tools.bind_session_revision("s1", "log-a")
    log = _NoticeLog()
    query = {"query": "hello"}

    log.turn(loop)
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    client = _serve_search(monkeypatch)
    current = log.turn(loop)
    assert "now available" in current[("tool", "web_search")]

    _compact(loop, log)
    current = log.turn(loop)
    assert "web_search" in _session_names(loop), "the refresh captured the now-callable tool"
    assert ("tool", "web_search") not in current
    assert any("available again" in line for line in log.cleared()), log.sent
    assert "Results for: hello" in _execute(loop, "web_search", query)
    assert client.calls
    assert ("tool", "web_search") not in log.told, "the cleared line went into the log"
    log.bind(loop)
    assert not loop.tools._session_notified_tools.get("s1"), "so the next bind speaks for nothing"


@pytest.mark.parametrize("refresh", ["clear", "compaction"])
def test_a_tool_never_announced_stays_silent_across_a_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refresh: str
) -> None:
    """A tool off at capture and still off was never spoken for; a refresh must
    not invent a notice for it. Silence is the absence of a key, not a clear."""
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    log = _NoticeLog()

    for _ in range(3):
        current = log.turn(loop)
        assert ("tool", "web_search") not in current
        assert log.cleared() == []
        _refresh(loop, refresh, log)

    assert "not available" in _execute(loop, "web_search", {"query": "hello"})


def test_a_release_drops_registry_state_and_the_log_brings_the_notes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release drops what the registry holds; what the model was told is in the log.

    ``session/close`` keeps the stored conversation, so a reopened session that
    replays the same log speaks for the same subjects -- no cleared line while
    the tool is still off -- and a log that holds no note speaks for nothing.
    """
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)
    note = tmp_path / "note.txt"
    note.write_text("released", encoding="utf-8")
    params = {"path": str(note)}
    log = _NoticeLog()

    log.turn(loop)
    _write_switches(cfg, ["read_file"])
    log.turn(loop)
    _compact(loop, log)
    current = log.turn(loop)
    assert "currently unavailable" in current[("tool", "read_file")]

    loop.tools.release_session_tools("s1")
    assert "s1" not in loop.tools._session_notified_tools
    assert "s1" not in loop.tools._session_notified_mcp
    assert "s1" not in loop.tools._session_definitions

    reopened = log.turn(loop)
    assert reopened[("tool", "read_file")] == current[("tool", "read_file")]
    assert log.cleared() == [] and log.sent == [], log.sent
    assert "currently unavailable" in _execute(loop, "read_file", params)

    loop.tools.release_session_tools("s1")
    fresh = _NoticeLog(log_id="log-c")
    assert ("tool", "read_file") not in fresh.turn(loop)
    assert "not available" in _execute(loop, "read_file", params)


_SWITCHED_OFF = ["edit_file", "grep", "list_dir", "read_file", "write_file"]


def _five_switched_off_in_one_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgentLoop, _NoticeLog]:
    """Five tools in the array, then switched off: five standing lines filed in a real session's log."""
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)
    log = _NoticeLog(session=Session(key="cli:c1"))
    log.turn(loop)
    assert set(_SWITCHED_OFF) <= _session_names(loop)
    _write_switches(cfg, _SWITCHED_OFF)
    current = log.turn(loop)
    assert set(current) == {("tool", name) for name in _SWITCHED_OFF}, current
    assert set(log.told) == set(current), "the turn filed the five lines in the log"
    return loop, log


def test_a_clear_starts_a_log_with_no_standing_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``/clear`` is a new log the model has been told nothing in.

    The registry reads the standing subjects from that empty log, so the new
    conversation gets no line for a tool it never saw, and a call to one is
    answered as absent, exactly as on a session key that never existed.
    """
    loop, log = _five_switched_off_in_one_log(tmp_path, monkeypatch)
    old_log = log.log_id

    log.session.clear()
    assert log.log_id != old_log and log.messages == []
    current = log.turn(loop)

    assert current == {} and log.sent == [], log.sent
    with loop.tools.session_scope_for("s2"):
        assert loop._tool_notices() == [], "a cleared log reads like a new session"
    names = _session_names(loop)
    for name in _SWITCHED_OFF:
        assert name not in names, "the new log's capture re-judged it"
        answer = _execute(loop, name, {})
        assert "is not available" in answer, answer
        assert "currently unavailable" not in answer, "the new log never held this schema"


def test_a_compaction_keeps_the_standing_notice_on_the_same_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compaction keeps the log, so the notes it holds keep standing.

    The other half of the clear test, on the path a compaction really takes:
    a refresh marked by one turn and cashed by the next bind on the same log id.
    """
    loop, log = _five_switched_off_in_one_log(tmp_path, monkeypatch)
    log_id = log.log_id

    _compact(loop, log)
    assert log.log_id == log_id, "a compaction keeps the log"
    current = log.turn(loop)

    names = _session_names(loop)
    assert log.cleared() == [] and log.sent == [], log.sent
    for name in _SWITCHED_OFF:
        assert name not in names, "the refresh re-judged it and kept it out"
        assert "currently unavailable" in current[("tool", name)]
        assert "currently unavailable" in _execute(loop, name, {})


def test_a_clear_after_the_reverse_lane_leaves_nothing_standing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The announced-then-withdrawn lane is read from the log like the array lane.

    Its note is the only thing that kept a name the array never carried in the
    standing set, so a cleared log must leave that name unspoken for too: no
    line, and a call answered as absent.
    """
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    log = _NoticeLog(session=Session(key="cli:c1"))
    log.turn(loop)
    assert "web_search" not in _session_names(loop)

    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    assert "now available" in log.turn(loop)[("tool", "web_search")]
    monkeypatch.delenv("SERPER_API_KEY")
    assert "currently unavailable" in log.turn(loop)[("tool", "web_search")]
    assert "currently unavailable" in _execute(loop, "web_search", {"query": "x"})

    log.session.clear()
    current = log.turn(loop)

    assert current == {} and log.sent == [], log.sent
    answer = _execute(loop, "web_search", {"query": "x"})
    assert "is not available" in answer and "currently unavailable" not in answer, answer


def _saved_log_with_a_standing_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgentLoop, str]:
    """A conversation whose saved log holds "grep: currently unavailable"; its loop and key."""
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)
    session = loop.sessions.get_or_create("cli:c1")
    log = _NoticeLog(session=session)
    log.turn(loop)
    _write_switches(cfg, ["grep"])
    assert "currently unavailable" in log.turn(loop)[("tool", "grep")]
    loop.sessions.save(session)
    return loop, session.key


@pytest.mark.parametrize("reopen", ["restart", "close_then_load"])
def test_a_reopened_log_keeps_its_standing_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reopen: str) -> None:
    """The log outlives the process and the release; the notes it holds still stand.

    A restart builds a new loop with an empty registry; ``session/close``
    releases the registry's state but keeps the stored conversation. Either
    way the next turn replays the same log, so while grep is still off it gets
    no cleared line, and a call to it is still refused with the reason.
    """
    first, key = _saved_log_with_a_standing_notice(tmp_path, monkeypatch)
    if reopen == "restart":
        loop = _loop(tmp_path)
    else:
        loop = first
        loop.tools.release_session_tools("s1")
        loop.sessions.invalidate(key)
    session = loop.sessions.get_or_create(key)
    assert ("tool", "grep") in told_notices(session.messages), "the saved log carries the note"
    log = _NoticeLog(session=session)

    current = log.turn(loop)

    assert log.cleared() == [], log.sent
    assert "currently unavailable" in current[("tool", "grep")]
    assert log.sent == [], "the note is unchanged, so nothing is sent"
    answer = _execute(loop, "grep", {})
    assert "currently unavailable" in answer and "switched off in settings" in answer, answer


def test_a_log_adopted_from_another_writer_keeps_its_unsaved_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log id change is not always an empty log.

    Another writer clears the conversation while this copy holds an unsaved
    turn whose block carries a standing note. Saving adopts the other writer's
    log id and keeps that turn, so the new log holds the note, and the next
    turn must keep speaking for it rather than clear it.
    """
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)
    mine = loop.sessions.get_or_create("cli:c1")
    mine.add_message("user", "first")
    mine.add_message("assistant", "ok")
    loop.sessions.save(mine)

    other = SessionManager(loop.sessions.workspace)
    theirs = other.get_or_create("cli:c1")
    theirs.clear()
    other.save(theirs)

    log = _NoticeLog(session=mine)
    log.turn(loop)
    _write_switches(cfg, ["grep"])
    assert "currently unavailable" in log.turn(loop)[("tool", "grep")]
    old_log = mine.revision.log_id
    loop.sessions.save(mine)
    assert mine.revision.log_id != old_log, "the save adopted the other writer's log id"
    assert ("tool", "grep") in told_notices(mine.messages), "and kept the unsaved turn that holds the note"

    current = log.turn(loop)

    assert log.cleared() == [], log.sent
    assert "currently unavailable" in current[("tool", "grep")]
    assert "currently unavailable" in _execute(loop, "grep", {})


def test_a_refusal_says_whether_the_tool_list_still_carries_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a name in the frozen array stays in the tool list.

    A name announced and withdrawn was never in it, and a name a compaction
    dropped is no longer in it: both refusals say so, the way the MCP notice
    moves to "absent" once a refresh drops a server's tools.
    """
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    _capture_without_web_search(loop)
    _announce_then_withdraw_web_search(loop, monkeypatch)
    withdrawn = _execute(loop, "web_search", {"query": "x"})
    assert "currently unavailable" in withdrawn and "no search key" in withdrawn
    assert "stays in this conversation's tool list" not in withdrawn, withdrawn
    assert "absent from this conversation's tool list" in withdrawn

    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)
    log = _NoticeLog()
    log.turn(loop)
    assert "read_file" in _session_names(loop)
    _write_switches(cfg, ["read_file"])
    log.turn(loop)
    listed = _execute(loop, "read_file", {})
    assert "stays in this conversation's tool list" in listed, "the array still carries it"

    _compact(loop, log)
    assert "read_file" not in _session_names(loop)
    dropped = _execute(loop, "read_file", {})
    assert "currently unavailable" in dropped and "switched off in settings" in dropped
    assert "stays in this conversation's tool list" not in dropped, dropped
    assert "absent from this conversation's tool list" in dropped


@pytest.mark.parametrize("where", ["in_session", "at_restart"])
def test_an_unloaded_tool_gets_a_true_line_not_a_cleared_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """A tool the log holds a note about leaves the registry entirely.

    No switch's reason names it any more, so the array lane and the withdrawn
    lane fall silent; without a lane of its own its key would vanish and be
    cleared as "available again" while a call is answered as not available.
    The line it gets says exactly that it is not loaded, and dispatch agrees.
    """
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)
    session = loop.sessions.get_or_create("cli:c1")
    log = _NoticeLog(session=session)
    log.turn(loop)
    _write_switches(cfg, ["read_file"])
    assert "currently unavailable" in log.turn(loop)[("tool", "read_file")]

    if where == "in_session":
        loop.tools.unregister("read_file")
    else:
        loop.sessions.save(session)
        loop = _loop(tmp_path)
        loop.tools.unregister("read_file")
        log = _NoticeLog(session=loop.sessions.get_or_create(session.key))
    current = log.turn(loop)

    line = current[("tool", "read_file")]
    assert log.cleared() == [], log.sent
    assert "no longer available" in line and "not loaded any more" in line, line
    assert "currently unavailable" not in line and "switched off" not in line, "no switch would bring it back"
    if where == "in_session":
        assert "read_file" in _session_names(loop)
        assert "stays in this conversation's tool list" in line, "the frozen array still lists it"
    else:
        assert "read_file" not in _session_names(loop)
        assert "absent from this conversation's tool list" in line
    answer = _execute(loop, "read_file", {"path": "x"})
    assert "is not available" in answer and "currently unavailable" not in answer, answer

    _compact(loop, log)
    current = log.turn(loop)
    assert "absent from this conversation's tool list" in current[("tool", "read_file")]
    assert log.cleared() == [], log.sent
    assert "is not available" in _execute(loop, "read_file", {"path": "x"})


def test_a_tool_loaded_again_outside_the_array_is_reported_as_it_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unloaded, then loaded again inside one window, with the array still without it.

    It is in neither the array nor the capture's baseline, so only the note
    the log holds can keep a lane speaking for it. Loaded with its switch off
    it is reported unavailable with the reason; switched on it is announced
    with its schema, because a call then runs. At no step is it cleared as
    "available again" without a definition to call it by.
    """
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)
    note = tmp_path / "note.txt"
    note.write_text("payload-reloaded", encoding="utf-8")
    params = {"path": str(note)}
    log = _NoticeLog()
    key = ("tool", "read_file")
    log.turn(loop)
    _write_switches(cfg, ["read_file"])
    log.turn(loop)
    tool = loop.tools.get("read_file")
    loop.tools.unregister("read_file")
    assert "no longer available" in log.turn(loop)[key]
    _compact(loop, log)
    log.turn(loop)
    assert "read_file" not in _session_names(loop), "captured while it was not loaded"

    loop.tools.register(tool)
    current = log.turn(loop)
    assert "read_file" not in _session_names(loop), "the window's array does not move"
    assert "currently unavailable" in current[key] and "switched off" in current[key], current[key]
    assert log.cleared() == [], log.sent
    assert "currently unavailable" in _execute(loop, "read_file", params)

    _write_switches(cfg, [])
    current = log.turn(loop)
    assert "now available" in current[key], current[key]
    assert json.dumps(loop.tools.definition_of("read_file"), separators=(",", ":")) in current[key]
    assert log.cleared() == [] and not any("available again" in line for line in log.sent), log.sent
    assert "payload-reloaded" in _execute(loop, "read_file", params)


@pytest.mark.parametrize("switch", ["on", "off"])
def test_a_reopened_log_with_the_tool_loaded_again_gets_no_unloaded_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switch: str
) -> None:
    """The log says the tool was not loaded; this time it is.

    The unloaded lane speaks only for a name the registry lacks, so it stays
    silent, and the tool is reported as it now stands: in the array and
    callable (the cleared line is then true), or switched off with the reason.
    """
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    note = tmp_path / "note.txt"
    note.write_text("payload-reopened", encoding="utf-8")
    params = {"path": str(note)}
    first = _loop(tmp_path)
    session = first.sessions.get_or_create("cli:c1")
    log = _NoticeLog(session=session)
    log.turn(first)
    _write_switches(cfg, ["read_file"])
    log.turn(first)
    first.tools.unregister("read_file")
    assert "no longer available" in log.turn(first)[("tool", "read_file")]
    first.sessions.save(session)

    _write_switches(cfg, [] if switch == "on" else ["read_file"])
    loop = _loop(tmp_path)
    reopened = _NoticeLog(session=loop.sessions.get_or_create(session.key))
    current = reopened.turn(loop)

    assert not any("no longer available" in line for line in current.values()), current
    if switch == "on":
        assert ("tool", "read_file") not in current
        assert "read_file" in _session_names(loop)
        assert any("available again" in line for line in reopened.cleared()), reopened.sent
        assert "payload-reopened" in _execute(loop, "read_file", params)
    else:
        assert "currently unavailable" in current[("tool", "read_file")]
        assert reopened.cleared() == [], reopened.sent
        assert "currently unavailable" in _execute(loop, "read_file", params)


def test_an_unloaded_tool_has_one_line_even_while_a_reason_still_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``knowledge_search`` keeps its reason while no base is selected, loaded or not.

    Every other reason names only registered tools, so this is the one name
    the array lane could still speak for after it is unloaded. Its line would
    say selecting a base brings it back, which is false; the unloaded lane owns
    the name, and the subject gets exactly one line.
    """
    from raven.agent import knowledge_scope

    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    log = _NoticeLog()
    with knowledge_scope.bind(("kb-1",), "s1"):
        log.turn(loop)
    assert "knowledge_search" in _session_names(loop)
    assert "no knowledge base is selected" in log.turn(loop)[("tool", "knowledge_search")]

    loop.tools.unregister("knowledge_search")
    assert "knowledge_search" in loop._withheld_tool_reasons(), "the reason outlives the tool"
    line = log.turn(loop)[("tool", "knowledge_search")]

    assert "no longer available" in line and "no knowledge base" not in line, line
    assert log.cleared() == [], log.sent
    assert "is not available" in _execute(loop, "knowledge_search", {"query": "x"})
