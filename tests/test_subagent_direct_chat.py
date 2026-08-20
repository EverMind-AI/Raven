"""Direct-chat resumption: the Raven-owned message store and its replay."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from raven.agent.subagent.direct_chat import DirectChatError, direct_root
from raven.agent.subagent.instance_state import InstanceState, instance_state_path
from raven.providers.base import LLMResponse


def _direct_chat_manager(tmp_path, monkeypatch, *, fail: bool = False):
    """A manager whose built-in backend answers without touching a provider."""
    from raven.agent.subagent.manager import SubagentManager

    class StubProvider:
        def get_default_model(self):
            return "stub"

    manager = SubagentManager(
        provider=StubProvider(),
        workspace=tmp_path / "home",
        session_dir=lambda key: tmp_path / "sessions" / key,
    )

    async def fake_run(
        task,
        *,
        task_id,
        workspace,
        executor,
        session_key=None,
        instance=None,
        provider=None,
        model=None,
        history=None,
        on_messages=None,
        on_delta=None,
    ):
        if fail:
            raise RuntimeError("backend exploded")
        msgs = list(history) if history else [{"role": "system", "content": "sys"}]
        msgs.append({"role": "user", "content": task})
        if on_messages is not None:
            on_messages([*msgs, {"role": "assistant", "content": f"re: {task}"}])
        if on_delta is not None:
            # Two frames whose concatenation is the return value, so a test can
            # tell a stream apart from one whole delivery.
            await on_delta("re: ")
            await on_delta(task)
        return f"re: {task}"

    monkeypatch.setattr(manager.registry.backend("raven"), "run", fake_run)
    return manager


def _no_tool_response(text: str) -> LLMResponse:
    """A provider response with no tool calls, ending the sub-agent loop."""
    return LLMResponse(content=text, finish_reason="stop")


class _RecordingProvider:
    """Records the message list handed to each ``chat_with_retry`` call, then
    replies with no tool calls so the sub-agent loop always stops after one turn.
    """

    def __init__(self) -> None:
        self.seen: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "stub"

    async def chat_with_retry(self, *, messages, tools, model):
        self.seen.append(list(messages))
        return _no_tool_response("done")


def test_state_path_is_scoped_by_agent_and_handle(tmp_path):
    p = instance_state_path(tmp_path, "Raven-Code", "refactor-auth")
    assert p == tmp_path / "subagents" / "direct" / "Raven-Code" / "refactor-auth" / "messages.json"


def test_load_of_a_missing_file_is_empty(tmp_path):
    assert InstanceState(tmp_path / "nope.json").load() == []


def test_save_then_load_roundtrips(tmp_path):
    state = InstanceState(tmp_path / "messages.json")
    msgs = [{"role": "system", "content": "you are"}, {"role": "user", "content": "hi"}]
    state.save(msgs)
    assert state.load() == msgs


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    state = InstanceState(tmp_path / "messages.json")
    state.save([{"role": "user", "content": "a"}])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["messages.json"]


def test_load_of_corrupt_json_is_empty_not_an_error(tmp_path):
    p = tmp_path / "messages.json"
    p.write_text("{ truncated", encoding="utf-8")
    assert InstanceState(p).load() == []


def test_load_rejects_a_non_list_document(tmp_path):
    p = tmp_path / "messages.json"
    p.write_text(json.dumps({"role": "user"}), encoding="utf-8")
    assert InstanceState(p).load() == []


async def test_raven_loop_replays_injected_history(tmp_path):
    """A second turn must see the first turn's messages, not a fresh prompt."""
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    provider = _RecordingProvider()
    captured: list[list[dict]] = []
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=tmp_path)

    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first-answer"},
    ]

    await backend.run(
        "second",
        task_id="t1",
        workspace=tmp_path,
        executor=None,
        history=history,
        on_messages=captured.append,
    )

    # The provider saw the prior turns, and the system prompt was not rebuilt.
    assert provider.seen[0][:3] == history
    assert provider.seen[0][-1] == {"role": "user", "content": "second"}
    # The backend handed back the full list for persistence, ending in the reply.
    assert captured[-1][-1]["role"] == "assistant"
    assert captured[-1][-1]["content"] == "done"


async def test_empty_history_still_builds_exactly_one_system_prompt(tmp_path):
    """``history=[]`` is what InstanceState.load() returns for a missing file --
    the normal first-turn case, not an edge case. It must still get the built
    sub-agent prompt, not a bare user turn with no instructions.
    """
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    provider = _RecordingProvider()
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=tmp_path)

    await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, history=[])

    sent = provider.seen[0]
    assert sent[0]["role"] == "system"
    assert sum(1 for m in sent if m["role"] == "system") == 1


async def test_none_history_behaves_like_empty_history(tmp_path):
    """The default (``history`` omitted, i.e. ``None``) must match ``history=[]``."""
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    provider = _RecordingProvider()
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=tmp_path)

    await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, history=None)

    sent = provider.seen[0]
    assert sent[0]["role"] == "system"
    assert sum(1 for m in sent if m["role"] == "system") == 1


async def test_nonempty_history_yields_no_second_system_message(tmp_path):
    """A resumed instance's own system turn must not be joined by a rebuilt one."""
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    provider = _RecordingProvider()
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=tmp_path)

    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first-answer"},
    ]

    await backend.run("second", task_id="t1", workspace=tmp_path, executor=None, history=history)

    sent = provider.seen[0]
    history_system_count = sum(1 for m in history if m["role"] == "system")
    assert sum(1 for m in sent if m["role"] == "system") == history_system_count


async def test_plain_spawn_sends_only_system_and_user(tmp_path):
    """A plain spawn (no ``history``/``on_messages``) must behave exactly as
    before: the provider sees only the system and user turns.
    """
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    provider = _RecordingProvider()
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=tmp_path)

    result = await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)

    assert result == "done"
    assert len(provider.seen) == 1
    assert [m["role"] for m in provider.seen[0]] == ["system", "user"]


async def test_openai_backend_replays_history(monkeypatch, tmp_path):
    from raven.agent.subagent.backends.openai_api import OpenAIApiBackend

    sent: list[dict] = []

    async def fake_post(url, *, json, headers, timeout=None):
        sent.append(json)
        return {"choices": [{"message": {"content": "reply"}}]}

    backend = OpenAIApiBackend(
        name="mirothinker",
        base_url="http://localhost:8000/v1",
        model="miro",
        system_prompt="sys",
    )
    monkeypatch.setattr(backend, "_post_chat", fake_post)

    captured: list[list[dict]] = []
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first-answer"},
    ]

    out = await backend.run(
        "second",
        task_id="t1",
        workspace=tmp_path,
        executor=None,
        history=history,
        on_messages=captured.append,
    )

    assert out == "reply"
    assert sent[0]["messages"][:3] == history
    assert sent[0]["messages"][-1] == {"role": "user", "content": "second"}
    assert captured[-1][-1] == {"role": "assistant", "content": "reply"}


async def test_openai_empty_history_still_sends_the_system_prompt(monkeypatch, tmp_path):
    """``history=[]`` is what InstanceState.load() returns for a missing file --
    the normal first direct-chat turn, not an edge case. It must still get the
    configured system prompt, not a bare user turn.
    """
    from raven.agent.subagent.backends.openai_api import OpenAIApiBackend

    sent: list[dict] = []

    async def fake_post(url, *, json, headers, timeout=None):
        sent.append(json)
        return {"choices": [{"message": {"content": "reply"}}]}

    backend = OpenAIApiBackend(
        name="mirothinker",
        base_url="http://localhost:8000/v1",
        model="miro",
        system_prompt="sys",
    )
    monkeypatch.setattr(backend, "_post_chat", fake_post)

    await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, history=[])

    messages = sent[0]["messages"]
    assert messages[0] == {"role": "system", "content": "sys"}
    assert sum(1 for m in messages if m["role"] == "system") == 1


async def test_openai_none_history_behaves_like_empty_history(monkeypatch, tmp_path):
    """The default (``history`` omitted, i.e. ``None``) must match ``history=[]``."""
    from raven.agent.subagent.backends.openai_api import OpenAIApiBackend

    sent: list[dict] = []

    async def fake_post(url, *, json, headers, timeout=None):
        sent.append(json)
        return {"choices": [{"message": {"content": "reply"}}]}

    backend = OpenAIApiBackend(
        name="mirothinker",
        base_url="http://localhost:8000/v1",
        model="miro",
        system_prompt="sys",
    )
    monkeypatch.setattr(backend, "_post_chat", fake_post)

    await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, history=None)

    messages = sent[0]["messages"]
    assert messages[0] == {"role": "system", "content": "sys"}
    assert sum(1 for m in messages if m["role"] == "system") == 1


async def test_openai_nonempty_history_yields_no_second_system_message(monkeypatch, tmp_path):
    """A resumed instance's own system turn must not be joined by a rebuilt one."""
    from raven.agent.subagent.backends.openai_api import OpenAIApiBackend

    sent: list[dict] = []

    async def fake_post(url, *, json, headers, timeout=None):
        sent.append(json)
        return {"choices": [{"message": {"content": "reply"}}]}

    backend = OpenAIApiBackend(
        name="mirothinker",
        base_url="http://localhost:8000/v1",
        model="miro",
        system_prompt="sys",
    )
    monkeypatch.setattr(backend, "_post_chat", fake_post)

    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first-answer"},
    ]

    await backend.run("second", task_id="t1", workspace=tmp_path, executor=None, history=history)

    messages = sent[0]["messages"]
    history_system_count = sum(1 for m in history if m["role"] == "system")
    assert sum(1 for m in messages if m["role"] == "system") == history_system_count


async def test_openai_empty_history_without_system_prompt_sends_only_user(monkeypatch, tmp_path):
    """No ``system_prompt`` configured is this kind's pre-existing optional-system
    behaviour: with an empty history and no configured prompt, only the user
    turn goes out. The compound seeding guard must not start sending a system
    message where none was ever configured.
    """
    from raven.agent.subagent.backends.openai_api import OpenAIApiBackend

    sent: list[dict] = []

    async def fake_post(url, *, json, headers, timeout=None):
        sent.append(json)
        return {"choices": [{"message": {"content": "reply"}}]}

    backend = OpenAIApiBackend(
        name="mirothinker",
        base_url="http://localhost:8000/v1",
        model="miro",
    )
    monkeypatch.setattr(backend, "_post_chat", fake_post)

    await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, history=[])

    messages = sent[0]["messages"]
    assert [m["role"] for m in messages] == ["user"]


@pytest.mark.asyncio
async def test_chat_resumes_the_built_in_subagent(tmp_path, monkeypatch):
    """Two chat turns on one handle: the second must see the first."""
    manager = _direct_chat_manager(tmp_path, monkeypatch)

    reply1, meta1 = await manager.chat(session_key="s1", agent="raven", handle="notes", text="first")
    reply2, meta2 = await manager.chat(session_key="s1", agent="raven", handle="notes", text="second")

    assert reply1 and reply2
    assert meta1.call_id != meta2.call_id
    assert meta2.status == "completed"

    # Both turns left a record, and the state file grew rather than reset.
    from raven.agent.subagent.direct_chat import direct_root

    root = direct_root(manager._session_dir("s1"), "raven", "notes")
    assert len([p for p in root.iterdir() if p.is_dir()]) == 2

    stored = InstanceState(instance_state_path(manager._session_dir("s1"), "raven", "notes")).load()
    assert [m["content"] for m in stored if m["role"] == "user"] == ["first", "second"]


@pytest.mark.asyncio
async def test_chat_does_not_write_the_main_transcript(tmp_path, monkeypatch):
    manager = _direct_chat_manager(tmp_path, monkeypatch)
    await manager.chat(session_key="s1", agent="raven", handle="notes", text="hi")

    transcript = manager._session_dir("s1").parent / "s1.jsonl"
    assert not transcript.exists()


@pytest.mark.asyncio
async def test_chat_records_a_failure(tmp_path, monkeypatch):
    manager = _direct_chat_manager(tmp_path, monkeypatch, fail=True)

    with pytest.raises(RuntimeError):
        await manager.chat(session_key="s1", agent="raven", handle="notes", text="hi")

    from raven.agent.subagent.direct_chat import direct_root

    root = direct_root(manager._session_dir("s1"), "raven", "notes")
    call_dir = next(p for p in root.iterdir() if p.is_dir())
    assert json.loads((call_dir / "meta.json").read_text())["status"] == "failed"
    assert (call_dir / "error.md").exists()


@pytest.mark.asyncio
async def test_chat_failure_raises_direct_chat_error_carrying_the_record_meta(tmp_path, monkeypatch):
    """A failed turn's meta must reach the caller, or the handoff never learns
    about it (F7). ``DirectChatError`` is what carries it out.
    """
    from raven.agent.subagent.direct_chat import DirectChatError

    manager = _direct_chat_manager(tmp_path, monkeypatch, fail=True)

    with pytest.raises(DirectChatError) as exc_info:
        await manager.chat(session_key="s1", agent="raven", handle="notes", text="hi")

    err = exc_info.value
    assert err.meta.status == "failed"
    assert err.meta.directory.is_dir()
    assert (err.meta.directory / "meta.json").exists()
    assert isinstance(err.__cause__, RuntimeError)
    assert str(err.__cause__) == "backend exploded"


@pytest.mark.asyncio
async def test_chat_with_a_disabled_agent_raises_and_records_nothing(tmp_path, monkeypatch):
    """An agent absent from the live backend roster (removed from config, or
    merely toggled off mid-session) must fail loudly instead of silently
    answering as the built-in raven sub-agent (F6). Nothing should be opened
    or written: the turn never ran.
    """
    from raven.agent.subagent import manager as manager_mod
    from raven.agent.subagent.direct_chat import direct_root
    from raven.agent.subagent.instances import InstanceRegistry

    manager = _direct_chat_manager(tmp_path, monkeypatch)
    registry = InstanceRegistry(path=tmp_path / "instances.json")
    monkeypatch.setattr(manager_mod, "get_registry", lambda: registry)

    with pytest.raises(RuntimeError, match="disabled or no longer configured"):
        await manager.chat(session_key="s1", agent="removed-agent", handle="notes", text="hi")

    assert registry.list_instances("s1") == []

    root = direct_root(manager._session_dir("s1"), "removed-agent", "notes")
    assert not root.exists()


@pytest.mark.asyncio
async def test_chat_and_spawn_on_one_handle_do_not_interleave(tmp_path, monkeypatch):
    manager = _direct_chat_manager(tmp_path, monkeypatch)
    inside: list[str] = []

    original = manager.registry.backend("raven").run

    async def tracked(task, **kwargs):
        inside.append(f"in:{task}")
        await asyncio.sleep(0.01)
        inside.append(f"out:{task}")
        return await original(task, **kwargs)

    monkeypatch.setattr(manager.registry.backend("raven"), "run", tracked)

    await asyncio.gather(
        manager.chat(session_key="s1", agent="raven", handle="h", text="a"),
        manager.chat(session_key="s1", agent="raven", handle="h", text="b"),
    )

    assert inside in (
        ["in:a", "out:a", "in:b", "out:b"],
        ["in:b", "out:b", "in:a", "out:a"],
    )


def _agent_loop_for_direct_chat(tmp_path, monkeypatch):
    """An AgentLoop for run_turn's direct-target branch.

    The branch returns before the sandbox/MCP bring-up or the model provider
    would ever be reached, so neither needs stubbing beyond a provider that
    fails loudly if the loop ever does call it -- the point of this harness is
    only a loop whose ``subagents`` attribute can be monkeypatched and whose
    ``sessions`` is inspectable.
    """
    from raven.agent.loop import AgentLoop

    class _NoCallProvider:
        async def chat_with_retry(self, **kwargs):
            raise AssertionError("model must not be called for a direct-target turn")

        async def chat_stream(self, **kwargs):
            raise AssertionError("model must not be called for a direct-target turn")
            yield  # unreachable; keeps this an async generator

        def get_default_model(self) -> str:
            return "fake/model"

    return AgentLoop(provider=_NoCallProvider(), workspace=tmp_path / "home")


@pytest.mark.asyncio
async def test_run_turn_routes_a_direct_target_to_the_subagent(tmp_path, monkeypatch):
    """The model is never called, and the transcript is never written."""
    emitted: list[Any] = []
    called: dict[str, Any] = {}

    loop = _agent_loop_for_direct_chat(tmp_path, monkeypatch)

    async def fake_chat(*, session_key, agent, handle, text, workspace=None, on_delta=None):
        called.update(session_key=session_key, agent=agent, handle=handle, text=text)
        from raven.agent.subagent.direct_chat import DirectTurnMeta

        return "sub reply", DirectTurnMeta(
            agent=agent,
            handle=handle,
            call_id="c1",
            directory=tmp_path,
            started_at_ms=1,
            ended_at_ms=2,
            status="completed",
        )

    monkeypatch.setattr(loop.subagents, "chat", fake_chat)

    async def emit(event):
        emitted.append(event)

    from raven.spine import ChatType, Origin, Source, TurnRequest

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="fix it",
        conversation="s1",
        direct_target=("Raven-Code", "refactor-auth"),
    )

    await loop.run_turn(req, emit, lambda: [])

    assert called == {
        "session_key": "s1",
        "agent": "Raven-Code",
        "handle": "refactor-auth",
        "text": "fix it",
    }
    assert any(getattr(e, "content", None) == "sub reply" for e in emitted)
    assert loop.sessions.get_or_create("s1").messages == []


@pytest.mark.asyncio
async def test_a_direct_chat_runs_in_the_session_workdir_not_agent_home(tmp_path, monkeypatch):
    """The same directory the turn's own tools get, and that ``spawn`` captures.

    Agent home is raven's memory and skills; the session working directory is
    the work. Passing nothing fell back to the first, so a direct chat about the
    checkout the user is sitting in ran its commands against ``~/.raven/workspace``
    and answered about that instead -- measured on a live codex turn, whose
    ``pwd`` came back as agent home.

    ``workdir.current()`` cannot be used here the way ``spawn`` does: this branch
    returns before ``workdir.bind`` wraps the main turn body, so there is no
    binding to read yet.
    """
    from raven.agent.loop import AgentLoop
    from raven.agent.workdir import WorkdirPolicy, WorkdirResolver

    home = tmp_path / "home"
    work = tmp_path / "checkout"
    work.mkdir()
    seen: dict[str, Any] = {}

    loop = AgentLoop(
        provider=_agent_loop_for_direct_chat(tmp_path, monkeypatch).provider,
        workspace=home,
        workdir_resolver=WorkdirResolver(WorkdirPolicy.LAUNCH_DIR, agent_home=home, launch_dir=work),
    )

    async def fake_chat(*, session_key, agent, handle, text, workspace=None, on_delta=None):
        from raven.agent.subagent.direct_chat import DirectTurnMeta

        seen["workspace"] = workspace
        return "ok", DirectTurnMeta(
            agent=agent,
            handle=handle,
            call_id="c1",
            directory=tmp_path,
            started_at_ms=1,
            ended_at_ms=2,
            status="completed",
        )

    monkeypatch.setattr(loop.subagents, "chat", fake_chat)

    from raven.spine import ChatType, Origin, Source, TurnRequest

    await loop.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
            text="what is this project",
            conversation="s1",
            direct_target=("Raven-Code", "refactor-auth"),
        ),
        lambda event: _noop(),
        lambda: [],
    )

    assert seen["workspace"] == work
    assert seen["workspace"] != home


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_run_turn_records_a_failed_direct_turn_in_the_handoff_before_reraising(tmp_path, monkeypatch):
    """A failed direct turn must still reach the per-session handoff, or the
    main agent is never told a direct chat happened at all -- and the turn
    must still fail so the user sees the error.
    """
    from raven.agent.subagent.direct_chat import DirectChatError, DirectTurnMeta

    loop = _agent_loop_for_direct_chat(tmp_path, monkeypatch)

    fail_meta = DirectTurnMeta(
        agent="Raven-Code",
        handle="refactor-auth",
        call_id="c1",
        directory=tmp_path,
        started_at_ms=1,
        ended_at_ms=2,
        status="failed",
    )

    async def failing_chat(*, session_key, agent, handle, text, workspace=None, on_delta=None):
        raise DirectChatError(fail_meta) from RuntimeError("backend exploded")

    monkeypatch.setattr(loop.subagents, "chat", failing_chat)

    recorded: list[tuple[str, Any]] = []

    class _FakeHandoff:
        def record(self, cid, meta):
            recorded.append((cid, meta))

    loop._direct_handoff = _FakeHandoff()

    async def emit(event):
        pass

    from raven.spine import ChatType, Origin, Source, TurnRequest

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="fix it",
        conversation="s1",
        direct_target=("Raven-Code", "refactor-auth"),
    )

    with pytest.raises(DirectChatError):
        await loop.run_turn(req, emit, lambda: [])

    assert recorded == [("s1", fail_meta)]


# --- the handle lock ---------------------------------------------------------


async def test_a_nested_acquire_from_one_task_does_not_deadlock():
    """The direct-chat path takes this around a whole turn and the cli backend
    takes it again around its own lookup-run-commit. Both are right to, and a
    non-reentrant lock makes that pair hang with no error and no timeout -- the
    turn's record just stays 'running' forever.
    """
    import asyncio

    from raven.agent.subagent.instances import hold_handle

    async def nested():
        async with hold_handle("s1", "Coder", "h"):
            async with hold_handle("s1", "Coder", "h"):
                return "reached"

    assert await asyncio.wait_for(nested(), timeout=2) == "reached"


async def test_a_second_task_still_waits_for_the_first():
    """Re-entrancy must not turn the lock off: two runs resuming one handle
    still interleave that CLI session's transcript."""
    import asyncio

    from raven.agent.subagent.instances import hold_handle

    order: list[str] = []
    released = asyncio.Event()

    async def holder():
        async with hold_handle("s1", "Coder", "h"):
            order.append("first-in")
            await released.wait()
            order.append("first-out")

    async def waiter():
        async with hold_handle("s1", "Coder", "h"):
            order.append("second-in")

    first = asyncio.create_task(holder())
    await asyncio.sleep(0)
    second = asyncio.create_task(waiter())
    await asyncio.sleep(0)

    assert order == ["first-in"], "the second task got in while the first held it"
    released.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
    assert order == ["first-in", "first-out", "second-in"]


async def test_the_owner_is_dropped_so_a_later_task_is_not_mistaken_for_it():
    """A stale owner entry would let an unrelated task skip the lock entirely."""
    import asyncio

    from raven.agent.subagent import instances as instances_mod

    async with instances_mod.hold_handle("s1", "Coder", "h"):
        pass

    assert instances_mod._handle_owners == {}
    assert instances_mod._handle_locks == {}
    del asyncio


async def test_chat_with_a_backend_that_takes_the_handle_lock_completes(tmp_path, monkeypatch):
    """The end-to-end shape of the deadlock: ``chat`` holds the handle lock for
    the whole turn and the cli backend takes it again around its own
    lookup-run-commit. Non-reentrant, this hangs with nothing logged and the
    turn's record stuck at 'running' -- which is exactly what it did.

    Driven through the manager rather than through ``hold_handle`` alone,
    because the two acquires being on opposite sides of that boundary is the
    whole defect: neither side is wrong by itself.
    """
    import asyncio

    from raven.agent.subagent.instances import hold_handle

    manager = _direct_chat_manager(tmp_path, monkeypatch)

    async def locking_run(task, *, task_id, workspace, executor, session_key=None, instance=None, **_kw):
        # What CliAgentBackend.run does for a resumable named handle.
        async with hold_handle(session_key or "default", "raven", instance or task_id):
            return f"re: {task}"

    monkeypatch.setattr(manager.registry.backend("raven"), "run", locking_run)

    reply, meta = await asyncio.wait_for(
        manager.chat(session_key="s1", agent="raven", handle="notes", text="who are you"),
        timeout=3,
    )

    assert reply == "re: who are you"
    assert meta.status == "completed"


async def test_a_running_direct_chat_is_reported_live(tmp_path, monkeypatch):
    """``live_handles`` is what stops a running row being reconciled back to
    'interrupted'. A direct chat runs inside the turn, not as a spawned task, so
    without an explicit index entry the chip loses its dot mid-conversation.
    """
    import asyncio

    manager = _direct_chat_manager(tmp_path, monkeypatch)
    seen: list[set] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_run(task, *, task_id, workspace, executor, session_key=None, instance=None, **_kw):
        entered.set()
        await release.wait()
        return "done"

    monkeypatch.setattr(manager.registry.backend("raven"), "run", slow_run)

    turn = asyncio.create_task(manager.chat(session_key="s1", agent="raven", handle="notes", text="hi"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    seen.append(manager.live_handles("s1"))
    release.set()
    await asyncio.wait_for(turn, timeout=2)

    assert seen[0] == {("raven", "notes")}
    # And released when it lands, or the row would read live forever.
    assert manager.live_handles("s1") == set()


async def test_cancel_by_instance_stops_a_direct_chat(tmp_path, monkeypatch):
    """The stop button's granularity. Without the index entry this returned
    False and cancelled nothing, while the turn kept running."""
    import asyncio

    manager = _direct_chat_manager(tmp_path, monkeypatch)
    entered = asyncio.Event()

    async def hanging_run(task, *, task_id, workspace, executor, session_key=None, instance=None, **_kw):
        entered.set()
        await asyncio.Event().wait()
        return "never"

    monkeypatch.setattr(manager.registry.backend("raven"), "run", hanging_run)

    turn = asyncio.create_task(manager.chat(session_key="s1", agent="raven", handle="notes", text="hi"))
    await asyncio.wait_for(entered.wait(), timeout=2)

    assert await asyncio.wait_for(manager.cancel_by_instance("s1", "raven", "notes"), timeout=3) is True
    with pytest.raises(asyncio.CancelledError):
        await turn


async def test_cancel_by_instance_of_an_idle_instance_reports_false(tmp_path, monkeypatch):
    manager = _direct_chat_manager(tmp_path, monkeypatch)

    assert await manager.cancel_by_instance("s1", "raven", "never-ran") is False


# --- streaming a direct reply ------------------------------------------------


def _collect(sink: list[str]):
    """An ``on_delta`` that appends every chunk to ``sink``."""

    async def on_delta(text: str) -> None:
        sink.append(text)

    return on_delta


class _StreamingProvider:
    """A provider whose ``chat_stream`` yields the reply in pieces.

    Also implements ``chat_with_retry`` so a test can tell which path a backend
    took: the two answer with different text.
    """

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.stream_kwargs: list[dict] = []
        self.retry_calls = 0

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.retry_calls += 1
        return LLMResponse(content="whole", finish_reason="stop")

    async def chat_stream(self, **kwargs):
        from raven.providers.base import StreamDelta

        self.stream_kwargs.append(kwargs)
        for piece in self.pieces:
            yield StreamDelta(content=piece)

    def get_default_model(self) -> str:
        return "stub"


async def test_raven_loop_streams_its_reply_when_a_delta_hook_is_wired(tmp_path):
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    provider = _StreamingProvider(["par", "tial"])
    seen: list[str] = []
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=tmp_path)

    out = await backend.run(
        "hi",
        task_id="t1",
        workspace=tmp_path,
        executor=None,
        on_delta=_collect(seen),
    )

    assert seen == ["par", "tial"]
    # What streamed and what returned are the same text, so a caller that
    # rendered the deltas must not also render the return value.
    assert out == "partial"
    assert provider.retry_calls == 0


async def test_raven_loop_keeps_the_retry_ladder_when_nothing_is_watching(tmp_path):
    """A spawn must not lose ``chat_with_retry``: streaming trades the retry
    ladder away, and only a caller that asked to watch the reply pays that."""
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    provider = _StreamingProvider(["par", "tial"])
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=tmp_path)

    out = await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None)

    assert out == "whole"
    assert provider.retry_calls == 1
    assert provider.stream_kwargs == []


async def test_raven_loop_streams_under_the_providers_own_generation_budget(tmp_path):
    """``chat_stream``'s signature carries literal defaults (4096 / 0.7) while
    ``chat_with_retry`` reads ``provider.generation``. Left to the signature, a
    direct chat would answer under a different budget than the same instance's
    spawns -- visible as a reply truncated where a spawn's is not.
    """
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend
    from raven.providers.base import GenerationSettings

    provider = _StreamingProvider(["ok"])
    provider.generation = GenerationSettings(temperature=0.1, max_tokens=32000)
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=tmp_path)

    await backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, on_delta=_collect([]))

    assert provider.stream_kwargs[0]["max_tokens"] == 32000
    assert provider.stream_kwargs[0]["temperature"] == 0.1


async def test_chat_offers_the_delta_hook_to_a_streaming_backend(tmp_path, monkeypatch):
    manager = _direct_chat_manager(tmp_path, monkeypatch)
    seen: list[str] = []

    reply, _meta = await manager.chat(
        session_key="s1", agent="raven", handle="notes", text="hi", on_delta=_collect(seen)
    )

    assert seen == ["re: ", "hi"]
    assert reply == "re: hi"


async def test_chat_withholds_the_delta_hook_from_a_backend_that_cannot_stream(tmp_path, monkeypatch):
    """A cli agent is handed no ``on_delta``: its transport buffers the whole
    reply, and offering the hook would be a capability it cannot honour."""
    manager = _direct_chat_manager(tmp_path, monkeypatch)
    offered: list[Any] = []

    async def fake_run(task, *, task_id, workspace, executor, **kwargs):
        offered.append(kwargs.get("on_delta"))
        return "buffered"

    monkeypatch.setattr(manager.registry.backend("raven"), "run", fake_run)
    monkeypatch.setattr(manager.registry.backend("raven"), "streams", False)

    reply, _meta = await manager.chat(session_key="s1", agent="raven", handle="notes", text="hi", on_delta=_collect([]))

    assert offered == [None]
    assert reply == "buffered"


async def test_a_streamed_direct_turn_records_the_whole_reply(tmp_path, monkeypatch):
    """Deltas are an observation of a turn, never the source of truth for one:
    the record has to hold the same text whether or not anyone watched."""
    manager = _direct_chat_manager(tmp_path, monkeypatch)

    _reply, meta = await manager.chat(session_key="s1", agent="raven", handle="notes", text="hi", on_delta=_collect([]))

    assert (meta.directory / "out.md").read_text(encoding="utf-8") == "re: hi"
    assert json.loads((meta.directory / "meta.json").read_text(encoding="utf-8"))["status"] == "completed"


async def test_run_turn_streams_a_direct_reply_instead_of_a_closing_text(tmp_path, monkeypatch):
    """What streamed is already on screen; a closing Text would render it twice."""
    from raven.spine import ChatType, Origin, Source, TurnRequest
    from raven.spine.events import StreamDelta, Text

    emitted: list[Any] = []
    loop = _agent_loop_for_direct_chat(tmp_path, monkeypatch)

    async def fake_chat(*, session_key, agent, handle, text, workspace=None, on_delta=None):
        from raven.agent.subagent.direct_chat import DirectTurnMeta

        assert on_delta is not None
        await on_delta("sub ")
        await on_delta("reply")
        return "sub reply", DirectTurnMeta(
            agent=agent,
            handle=handle,
            call_id="c1",
            directory=tmp_path,
            started_at_ms=1,
            ended_at_ms=2,
            status="completed",
        )

    monkeypatch.setattr(loop.subagents, "chat", fake_chat)

    async def emit(event):
        emitted.append(event)

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="fix it",
        conversation="s1",
        direct_target=("Raven-Code", "refactor-auth"),
    )

    await loop.run_turn(req, emit, lambda: [])

    assert [e.delta for e in emitted if isinstance(e, StreamDelta)] == ["sub ", "reply"]
    assert not [e for e in emitted if isinstance(e, Text)]


async def test_run_turn_delivers_a_whole_direct_reply_when_nothing_streamed(tmp_path, monkeypatch):
    """The fallback is the absence of deltas, not a branch: an instance whose
    transport cannot stream is delivered exactly as it was before."""
    from raven.spine import ChatType, Origin, Source, TurnRequest
    from raven.spine.events import StreamDelta, Text

    emitted: list[Any] = []
    loop = _agent_loop_for_direct_chat(tmp_path, monkeypatch)

    async def fake_chat(*, session_key, agent, handle, text, workspace=None, on_delta=None):
        from raven.agent.subagent.direct_chat import DirectTurnMeta

        return "sub reply", DirectTurnMeta(
            agent=agent,
            handle=handle,
            call_id="c1",
            directory=tmp_path,
            started_at_ms=1,
            ended_at_ms=2,
            status="completed",
        )

    monkeypatch.setattr(loop.subagents, "chat", fake_chat)

    async def emit(event):
        emitted.append(event)

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="fix it",
        conversation="s1",
        direct_target=("Raven-Code", "refactor-auth"),
    )

    await loop.run_turn(req, emit, lambda: [])

    assert [e.content for e in emitted if isinstance(e, Text)] == ["sub reply"]
    assert not [e for e in emitted if isinstance(e, StreamDelta)]


async def test_run_turn_withholds_the_delta_hook_from_a_non_streaming_outlet(tmp_path, monkeypatch):
    """The REPL assembles no deltas, so it gets none -- same as a normal turn."""
    from raven.spine import ChatType, Origin, Source, TurnRequest
    from raven.spine.events import Text

    emitted: list[Any] = []
    offered: list[Any] = []
    loop = _agent_loop_for_direct_chat(tmp_path, monkeypatch)

    async def fake_chat(*, session_key, agent, handle, text, workspace=None, on_delta=None):
        from raven.agent.subagent.direct_chat import DirectTurnMeta

        offered.append(on_delta)
        return "sub reply", DirectTurnMeta(
            agent=agent,
            handle=handle,
            call_id="c1",
            directory=tmp_path,
            started_at_ms=1,
            ended_at_ms=2,
            status="completed",
        )

    monkeypatch.setattr(loop.subagents, "chat", fake_chat)

    async def emit(event):
        emitted.append(event)

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="fix it",
        conversation="s1",
        direct_target=("Raven-Code", "refactor-auth"),
    )

    await loop.run_turn(req, emit, lambda: [], stream=False)

    assert offered == [None]
    assert [e.content for e in emitted if isinstance(e, Text)] == ["sub reply"]


class _FakeProvider:
    """Minimal LLMProvider stand-in: the manager only asks it for a default model."""

    def get_default_model(self) -> str:
        return "fake/model"


# ---- what the roster promises, every dispatch path has to deliver -----------


@pytest.mark.asyncio
async def test_a_spawn_carries_the_instances_message_list(tmp_path, monkeypatch):
    """The promise `stateful` makes, on the path that never kept it.

    Only `chat` passed `history` / `on_messages`, so an agent the roster
    advertises as stateful started every spawn from an empty list and wrote
    none back. Reusing a handle read as the sub-agent having forgotten the
    earlier turns rather than as an argument that was refused -- which is the
    exact failure the `instance` gate's docstring says it exists to prevent.
    """
    from raven.agent.subagent.manager import SubagentManager

    seen: list[Any] = []

    class _Backend:
        kind = "raven"
        streams = False

        async def run(self, task, **kw):
            seen.append(kw.get("history"))
            on_messages = kw.get("on_messages")
            if on_messages is not None:
                on_messages([{"role": "user", "content": task}, {"role": "assistant", "content": "ok"}])
            return "ok"

    manager = SubagentManager(provider=_FakeProvider(), workspace=tmp_path, session_dir=lambda k: tmp_path)
    manager.registry.set_builtin_builder(lambda _row, _build, _b=_Backend(): _b)
    # The result re-injection submits a turn; this test is about the message
    # list the dispatch carried, not about what the main agent hears afterwards.
    manager._submit = lambda *a, **k: None

    origin = {
        "channel": "cli",
        "chat_id": "direct",
        "session_key": "s1",
        "agent": None,
        "handle": "author",
        "instance": "author",
        "workspace": tmp_path,
    }
    for task_id in ("t1", "t2"):
        await manager._run_subagent_inner(
            task_id=task_id,
            task="write",
            label="l",
            origin=origin,
            executor=None,
            provider=_FakeProvider(),
            model="m",
        )

    assert seen[0] == [], "the first spawn starts fresh"
    assert seen[1], "the second spawn must see what the first one said"


@pytest.mark.asyncio
async def test_a_spawn_that_named_no_instance_persists_nothing(tmp_path, monkeypatch):
    """A plain spawn's handle is a fresh task id, so a transcript under it is
    addressable by nobody and reclaimed by nothing.

    Paired with the case above: the state has to follow the `instance` argument,
    not the handle, because a handle always exists. Writing one per spawn is
    pure growth on disk for a conversation that by construction has no second
    turn.
    """
    from raven.agent.subagent.manager import SubagentManager

    class _Backend:
        kind = "raven"
        streams = False

        async def run(self, task, **kw):
            on_messages = kw.get("on_messages")
            if on_messages is not None:
                on_messages([{"role": "user", "content": task}])
            return "ok"

    manager = SubagentManager(provider=_FakeProvider(), workspace=tmp_path, session_dir=lambda k: tmp_path)
    manager.registry.set_builtin_builder(lambda _row, _build, _b=_Backend(): _b)
    manager._submit = lambda *a, **k: None

    await manager._run_subagent_inner(
        task_id="t1",
        task="do it",
        label="l",
        origin={
            "channel": "cli",
            "chat_id": "direct",
            "session_key": "s1",
            "agent": None,
            "handle": "t1",
            "instance": None,
            "workspace": tmp_path,
        },
        executor=None,
        provider=_FakeProvider(),
        model="m",
    )

    assert not instance_state_path(tmp_path, "raven", "t1").exists(), (
        "an unaddressable handle must leave no message list behind"
    )


@pytest.mark.asyncio
async def test_a_stateless_agent_cannot_be_direct_chatted(tmp_path, monkeypatch):
    """A direct chat is a continuation; against a stateless agent there is none.

    Every turn would start from nothing, so the conversation on screen would be
    a run of unrelated first turns -- which reads as the instance forgetting.
    Refusing says so instead of offering something that disappoints.
    """
    from raven.agent.subagent.manager import SubagentManager

    manager = SubagentManager(provider=_FakeProvider(), workspace=tmp_path, session_dir=lambda k: tmp_path)
    from raven.config.schema import ThirdPartyOpenAISubagentConfig

    manager.apply_agents(
        [ThirdPartyOpenAISubagentConfig(name="mirothinker", base_url="http://x", model="m", stateful=False)]
    )

    with pytest.raises(RuntimeError) as excinfo:
        await manager.chat(session_key="s1", agent="mirothinker", handle="h", text="hi")

    # Not `match=`, and not `RuntimeError` alone: `DirectChatError` *is* a
    # RuntimeError, so a turn that ran and then failed downstream satisfies both
    # -- which is how the first version of this test passed with the guard
    # removed. What distinguishes a refusal is that nothing was opened.
    assert not isinstance(excinfo.value, DirectChatError)
    assert "stateless" in str(excinfo.value)
    assert not direct_root(tmp_path, "mirothinker", "h").exists(), "a refused chat must leave no record behind"


def test_replay_follows_the_declaration_not_the_kind(tmp_path):
    """An endpoint that ignores a system prompt continues nothing under replay.

    The mirothinker preset says exactly that, and keying on `kind == "openai"`
    alone replayed at it anyway: every turn re-posts the whole transcript, which
    grows until it trips the endpoint's context limit, to buy nothing.
    """
    from raven.agent.subagent.manager import SubagentManager

    manager = SubagentManager(provider=_FakeProvider(), workspace=tmp_path, session_dir=lambda k: tmp_path)

    from raven.config.schema import ThirdPartyOpenAISubagentConfig

    manager.apply_agents(
        [
            ThirdPartyOpenAISubagentConfig(name="stateful_ep", base_url="http://x", model="m", stateful=True),
            ThirdPartyOpenAISubagentConfig(name="stateless_ep", base_url="http://x", model="m", stateful=False),
        ]
    )
    assert manager._is_replayed("stateful_ep") is True
    assert manager._is_replayed("stateless_ep") is False
    # A built-in row is replayed whatever it declares: an in-process loop has no
    # session store of its own, so the message list raven keeps is its memory.
    assert manager._is_replayed("raven") is True
