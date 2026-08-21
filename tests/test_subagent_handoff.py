"""The pointer block a direct chat owes the main agent on its next turn."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.subagent.direct_chat import DirectChatCreation, DirectChatHandoff, DirectTurnMeta


def _meta(
    agent="Raven-Code",
    handle="refactor-auth",
    call_id="20260813T041207Z-a1b2c3d4",
    started=1786000000000,
    ended=1786000060000,
    status="completed",
):
    return DirectTurnMeta(
        agent=agent,
        handle=handle,
        call_id=call_id,
        directory=Path("/root/.raven/workspace/sessions/g/c/subagents/direct") / agent / handle / call_id,
        started_at_ms=started,
        ended_at_ms=ended,
        status=status,
    )


_META_ROOT = "/root/.raven/workspace/sessions/g/c/subagents/direct"


@pytest.fixture(autouse=True)
def _meta_root_looks_recorded(monkeypatch):
    """``_meta()``'s directory is illustrative and is never created on disk.

    The renderer's missing-record check reads ``Path.is_dir()``, which would
    otherwise mark every turn built from ``_meta()`` as a lost record. Treat
    only that one illustrative prefix as present; every other path (in
    particular the missing-record test's own ``tmp_path`` directory) still
    hits the real filesystem.
    """
    real_is_dir = Path.is_dir

    def _is_dir(self):
        if str(self).startswith(_META_ROOT):
            return True
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", _is_dir)


def test_nothing_pending_takes_nothing():
    h = DirectChatHandoff()
    assert h.pending_count("s1") == 0
    assert h.take("s1") is None


def test_take_clears_the_pending_list():
    h = DirectChatHandoff()
    h.record("s1", _meta())
    assert h.pending_count("s1") == 1
    assert h.take("s1") is not None
    assert h.pending_count("s1") == 0
    assert h.take("s1") is None


def test_sessions_do_not_share_pending_state():
    h = DirectChatHandoff()
    h.record("s1", _meta())
    assert h.pending_count("s2") == 0
    assert h.take("s2") is None
    assert h.pending_count("s1") == 1


def test_block_uses_utc_iso_timestamps_not_epoch_ms():
    h = DirectChatHandoff()
    h.record("s1", _meta())
    block = h.take("s1")
    assert "1786000000000" not in block
    assert "2026-" in block and "Z" in block


def test_turns_are_grouped_by_instance():
    h = DirectChatHandoff()
    h.record("s1", _meta(call_id="c1"))
    h.record("s1", _meta(call_id="c2"))
    h.record("s1", _meta(agent="mirothinker", handle="scan", call_id="c3"))
    block = h.take("s1")

    # One header line per instance, not per turn.
    assert block.count("Raven-Code / refactor-auth") == 1
    assert block.count("mirothinker / scan") == 1
    assert "2 turns" in block
    assert "1 turn," in block


def test_an_unlanded_turn_is_marked_and_lists_only_its_prompt():
    h = DirectChatHandoff()
    h.record("s1", _meta(ended=None, status="running"))
    block = h.take("s1")

    assert "running at handoff time" in block
    assert "prompt.md" in block
    assert "out.md" not in block


def test_the_first_path_is_absolute_and_later_ones_are_abbreviated():
    h = DirectChatHandoff()
    h.record("s1", _meta(call_id="c1"))
    h.record("s1", _meta(call_id="c2"))
    block = h.take("s1")

    assert "/root/.raven/workspace/sessions/g/c/subagents/direct/Raven-Code/refactor-auth/" in block
    assert "c1/{prompt.md,out.md}" in block
    assert "c2/{prompt.md,out.md}" in block


def test_the_block_carries_no_subagent_authored_text():
    """Every byte is raven-minted, which is why the block needs no untrusted wrap."""
    h = DirectChatHandoff()
    h.record("s1", _meta())
    block = h.take("s1")
    assert "sub reply" not in block


def test_a_missing_record_directory_is_marked_unavailable_and_still_counted(tmp_path):
    """DirectChatRecord.open can fail to create its directory (a full or
    read-only disk); the handoff must not then point the main agent at
    prompt.md/out.md paths that were never written."""
    h = DirectChatHandoff()
    meta = DirectTurnMeta(
        agent="Raven-Code",
        handle="refactor-auth",
        call_id="never-written",
        directory=tmp_path / "never-created",
        started_at_ms=1786000000000,
        ended_at_ms=None,
        status="running",
    )
    h.record("s1", meta)
    block = h.take("s1")

    assert "never-written" in block
    assert "record unavailable" in block
    assert "prompt.md" not in block
    assert "out.md" not in block


def _creation(agent="Raven-PPT", handle="raven-ppt-a1b2c3", created=1786000000000):
    return DirectChatCreation(agent=agent, handle=handle, created_at_ms=created)


def test_the_header_covers_creations_not_only_chats():
    """A creation with no chat in it reads as a contradiction under the old
    wording, to the model that has to act on the block."""
    h = DirectChatHandoff()
    h.record_created("s1", _creation())

    assert h.take("s1").splitlines()[0] == "[subagent direct chat activity since your last turn]"


def test_a_creation_with_no_turns_is_still_announced():
    """The whole point of reporting a creation is the case where the user has
    not used it yet."""
    h = DirectChatHandoff()
    h.record_created("s1", _creation())
    block = h.take("s1")

    assert "Raven-PPT / raven-ppt-a1b2c3" in block
    assert "created by the user at 2026-" in block
    assert "no turns yet" in block


def test_a_creation_only_group_names_no_record_path():
    """No turn has run, so ``DirectChatRecord.open`` has made no directory."""
    h = DirectChatHandoff()
    h.record_created("s1", _creation())
    block = h.take("s1")

    assert "subagents" not in block
    assert "prompt.md" not in block
    assert "out.md" not in block


def test_a_creation_counts_toward_the_pending_count():
    h = DirectChatHandoff()
    h.record_created("s1", _creation())

    assert h.pending_count("s1") == 1
    assert h.take("s1") is not None
    assert h.pending_count("s1") == 0


def test_creations_do_not_leak_across_sessions():
    h = DirectChatHandoff()
    h.record_created("s1", _creation())

    assert h.pending_count("s2") == 0
    assert h.take("s2") is None


def test_a_creation_and_that_instances_turns_share_one_heading():
    h = DirectChatHandoff()
    h.record_created("s1", _creation(agent="Raven-Code", handle="refactor-auth"))
    h.record("s1", _meta(call_id="c1"))
    h.record("s1", _meta(call_id="c2"))
    block = h.take("s1")

    assert block.count("Raven-Code / refactor-auth") == 1
    assert "created by the user at" in block
    assert "no turns yet" not in block
    assert "2 turns" in block
    assert "c1/{prompt.md,out.md}" in block


def test_a_creation_carries_no_subagent_authored_text():
    """Same invariant the turn block relies on: the handle is minted and the
    agent name comes from config, which is why the block needs no untrusted wrap."""
    h = DirectChatHandoff()
    h.record_created("s1", _creation())
    block = h.take("s1")

    assert block.count("raven-ppt-a1b2c3") == 1
    assert "1786000000000" not in block


def _loop_with_handoff(tmp_path, monkeypatch):
    """An AgentLoop whose provider records the message list it is handed and
    whose sandbox/MCP bring-up is stubbed out, mirroring the harness
    ``tests/test_agent_loop_run_emit.py`` uses for its ``deliver_text`` tests."""
    from raven.agent.loop import AgentLoop
    from raven.providers.base import LLMResponse

    class _RecordingProvider:
        def __init__(self) -> None:
            self.seen: list[list[dict]] = []

        async def chat_with_retry(self, **kwargs):
            # Copy: the loop appends the assistant reply to this same list
            # object afterward, which would otherwise show up here too.
            self.seen.append(list(kwargs["messages"]))
            return LLMResponse(content="ok", finish_reason="stop")

        def get_default_model(self) -> str:
            return "fake/model"

    async def _noop(**_kw) -> None:
        return None

    loop = AgentLoop(provider=_RecordingProvider(), workspace=tmp_path / "home")
    loop._start_executor = _noop
    loop._connect_mcp = _noop
    # Runtime-context injection (current time / channel / chat id) is an
    # unrelated, pre-existing feature of the context engine that always
    # precedes the user message; bypass it so the assertions below observe
    # only what this task changed -- the handoff prepended to req.text.
    loop.context_engine._build_user = lambda ctx: {"role": "user", "content": ctx.current_message}
    return loop


async def _noop_emit(event) -> None:
    return None


async def _run_ordinary_turn(loop, text: str) -> list[dict]:
    from raven.spine import ChatType, Origin, Source, TurnRequest

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text=text,
        conversation="s1",
    )
    await loop.run_turn(req, _noop_emit, lambda: [], stream=False)
    return loop.provider.seen[-1]


async def _run_direct_turn(loop, text: str, *, target: tuple[str, str]) -> None:
    from raven.spine import ChatType, Origin, Source, TurnRequest

    async def fake_chat(*, session_key, agent, handle, text, workspace=None, on_delta=None):
        return "sub reply", DirectTurnMeta(
            agent=agent,
            handle=handle,
            call_id="direct-c1",
            directory=Path("/tmp/does-not-matter"),
            started_at_ms=1,
            ended_at_ms=2,
            status="completed",
        )

    loop.subagents.chat = fake_chat

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text=text,
        conversation="s1",
        direct_target=target,
    )
    await loop.run_turn(req, _noop_emit, lambda: [], stream=False)


@pytest.mark.asyncio
async def test_an_ordinary_turn_carries_the_pending_block(tmp_path, monkeypatch):
    """The block reaches the model, prepended to the user's own text."""
    loop = _loop_with_handoff(tmp_path, monkeypatch)
    loop._direct_handoff.record("s1", _meta())

    seen = await _run_ordinary_turn(loop, "what did you find?")

    user_text = seen[-1]["content"]
    assert user_text.startswith("[subagent direct chat activity since your last turn]")
    assert user_text.endswith("what did you find?")
    assert loop._direct_handoff.pending_count("s1") == 0


@pytest.mark.asyncio
async def test_a_turn_with_nothing_pending_is_untouched(tmp_path, monkeypatch):
    loop = _loop_with_handoff(tmp_path, monkeypatch)
    seen = await _run_ordinary_turn(loop, "plain question")
    assert seen[-1]["content"] == "plain question"


@pytest.mark.asyncio
async def test_a_direct_turn_does_not_consume_the_block(tmp_path, monkeypatch):
    """Only a turn addressed to the main agent takes the handoff."""
    loop = _loop_with_handoff(tmp_path, monkeypatch)
    loop._direct_handoff.record("s1", _meta())

    await _run_direct_turn(loop, "more work", target=("Raven-Code", "refactor-auth"))

    # Still pending: the main agent has not had a turn yet. Plus this turn's own.
    assert loop._direct_handoff.pending_count("s1") == 2
