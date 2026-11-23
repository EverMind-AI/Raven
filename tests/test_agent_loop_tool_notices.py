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
from raven.session.manager import Session
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


def _compact(loop: AgentLoop, log_id: str = "log-a") -> None:
    """The compaction refresh: marked by one turn, cashed by the next on the same log."""
    loop.tools.schedule_definitions_refresh("s1")
    loop.tools.bind_session_revision("s1", log_id)


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
    told = {notice_key(note): note for note in withdrawn if notice_key(note) is not None}

    _compact(loop)

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


class _NoticeLog:
    """One session's replayed notice history, advanced the way real history is.

    ``notice_changes`` compares the turn's lines with what the transcript holds;
    recording its output here makes each refresh step a real transition rather
    than a string comparison against the previous list.
    """

    def __init__(self) -> None:
        self.told: dict[tuple[str, str], str] = {}
        self.current: dict[tuple[str, str], str] = {}
        self.sent: list[str] = []

    def turn(self, loop: AgentLoop) -> dict[tuple[str, str], str]:
        with loop.tools.session_scope_for("s1"):
            notes = loop._tool_notices()
            current = {notice_key(note): note for note in notes if notice_key(note) is not None}
        sent = notice_changes(notes, self.told)
        for line in sent:
            key = notice_key(line)
            if key is None:
                continue
            if key in current:
                self.told[key] = current[key]
            else:
                self.told.pop(key, None)
        self.current = current
        self.sent = sent
        return current

    def cleared(self) -> list[str]:
        """The lines that say a keyed note no longer applies."""
        return [line for line in self.sent if (key := notice_key(line)) is not None and key not in self.current]


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

    _compact(loop)
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

    _compact(loop)
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

    _compact(loop)
    log.turn(loop)
    assert "read_file" in _session_names(loop)
    assert not loop.tools._session_notified_tools.get("s1"), "the array carries it; the standing name is pruned"


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

    _compact(loop)
    current = log.turn(loop)
    assert "web_search" in _session_names(loop), "the refresh captured the now-callable tool"
    assert ("tool", "web_search") not in current
    assert any("available again" in line for line in log.cleared()), log.sent
    assert "Results for: hello" in _execute(loop, "web_search", query)
    assert client.calls
    assert not loop.tools._session_notified_tools.get("s1"), "the array carries it; the standing name is pruned"


def test_a_tool_never_announced_stays_silent_across_a_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool off at capture and still off was never spoken for; a refresh must
    not invent a notice for it. Silence is the absence of a key, not a clear."""
    monkeypatch.setattr("raven.home._current_config_path", tmp_path / "absent.json")
    loop = _loop(tmp_path)
    log = _NoticeLog()

    for revision in ("log-a", "log-b", "log-c"):
        loop.tools.bind_session_revision("s1", revision)
        current = log.turn(loop)
        assert ("tool", "web_search") not in current
        assert log.cleared() == []

    assert "not available" in _execute(loop, "web_search", {"query": "hello"})


def test_releasing_the_session_drops_its_standing_notices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A released session's history is gone, so its standing subjects go too."""
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)
    loop.tools.bind_session_revision("s1", "log-a")
    note = tmp_path / "note.txt"
    note.write_text("released", encoding="utf-8")
    params = {"path": str(note)}
    log = _NoticeLog()

    log.turn(loop)
    _write_switches(cfg, ["read_file"])
    log.turn(loop)
    _compact(loop)
    current = log.turn(loop)
    assert "currently unavailable" in current[("tool", "read_file")]
    assert loop.tools._session_notified_tools.get("s1") == {"read_file"}

    loop.tools.release_session_tools("s1")

    assert "s1" not in loop.tools._session_notified_tools
    assert "s1" not in loop.tools._session_notified_mcp
    assert "s1" not in loop.tools._session_definitions

    loop.tools.bind_session_revision("s1", "log-c")
    fresh = _NoticeLog()
    current = fresh.turn(loop)
    assert ("tool", "read_file") not in current
    assert "not available" in _execute(loop, "read_file", params)


_SWITCHED_OFF = ["edit_file", "grep", "list_dir", "read_file", "write_file"]


def _five_switched_off_in_one_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgentLoop, Session]:
    """Five tools in the array, then switched off: five standing lines in this log."""
    cfg = tmp_path / "config.json"
    _write_switches(cfg, [])
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(tmp_path)
    session = Session(key="s1")
    loop.tools.bind_session_revision("s1", session.revision.log_id)
    assert set(_SWITCHED_OFF) <= _session_names(loop)
    _write_switches(cfg, _SWITCHED_OFF)
    with loop.tools.session_scope_for("s1"):
        notes = loop._tool_notices()
    standing = {key for note in notes if (key := notice_key(note)) is not None}
    assert standing == {("tool", name) for name in _SWITCHED_OFF}, notes
    return loop, session


def test_a_clear_starts_a_log_with_no_standing_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``/clear`` is a new log the model has been told nothing in.

    The old log's standing notices go with it: the new conversation gets no
    line for a tool it never saw, and a call to one is answered as absent,
    exactly as on a session key that never existed.
    """
    loop, session = _five_switched_off_in_one_log(tmp_path, monkeypatch)
    old_log = session.revision.log_id

    session.clear()
    assert session.revision.log_id != old_log and session.messages == []
    loop.tools.bind_session_revision("s1", session.revision.log_id)

    with loop.tools.session_scope_for("s1"):
        notes = loop._tool_notices()
    assert notice_changes(notes, told_notices(session.messages)) == [], notes
    with loop.tools.session_scope_for("s2"):
        assert loop._tool_notices() == notes, "a cleared log reads like a new session"
    names = _session_names(loop)
    for name in _SWITCHED_OFF:
        assert name not in names, "the new log's capture re-judged it"
        answer = _execute(loop, name, {})
        assert "is not available" in answer, answer
        assert "currently unavailable" not in answer, "the new log never held this schema"


def test_a_compaction_keeps_the_standing_notice_on_the_same_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compaction keeps the log and retells its notes, so the notices stand.

    The other half of the clear test, on the path a compaction really takes:
    a refresh marked for the next assembly and cashed under the same log id.
    """
    loop, session = _five_switched_off_in_one_log(tmp_path, monkeypatch)
    log = _NoticeLog()
    log.turn(loop)
    log_id = session.revision.log_id

    loop.tools.schedule_definitions_refresh("s1")
    loop.tools.bind_session_revision("s1", session.revision.log_id)
    assert session.revision.log_id == log_id, "a compaction keeps the log"

    current = log.turn(loop)
    names = _session_names(loop)
    assert log.cleared() == [] and log.sent == [], log.sent
    for name in _SWITCHED_OFF:
        assert name not in names, "the refresh re-judged it and kept it out"
        assert "currently unavailable" in current[("tool", name)]
        assert "currently unavailable" in _execute(loop, name, {})


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
    loop.tools.bind_session_revision("s1", "log-a")
    assert "read_file" in _session_names(loop)
    _write_switches(cfg, ["read_file"])
    listed = _execute(loop, "read_file", {})
    assert "stays in this conversation's tool list" in listed, "the array still carries it"

    _compact(loop)
    assert "read_file" not in _session_names(loop)
    dropped = _execute(loop, "read_file", {})
    assert "currently unavailable" in dropped and "switched off in settings" in dropped
    assert "stays in this conversation's tool list" not in dropped, dropped
    assert "absent from this conversation's tool list" in dropped
