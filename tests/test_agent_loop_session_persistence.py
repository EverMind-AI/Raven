"""The turn's record reaches disk as the turn produces it.

A process killed at the task budget used to leave only the question behind: the
whole turn was written at the end. These pin the pieces that make an interrupted
turn readable -- the incremental append, and the repair of a call that never got
its result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.hook import AgentHook, HookDecision
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.spine import ChatType, Origin, Source, TurnRequest


class _ScriptedProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls += 1
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]

    def get_default_model(self) -> str:
        return "fake/model"


def _text(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop")


def _tool_call(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(content="", tool_calls=[ToolCallRequest(id="c1", name=name, arguments=arguments)])


def _req() -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="hi",
    )


def _loop(tmp_path: Path, provider, hooks=None) -> AgentLoop:
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=6),
        host=HostWiring(hooks=hooks or []),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    async def _noop(**_kw) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


def _filed(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            data = json.loads(line)
            if data.get("_type") != "metadata":
                rows.append(data)
    return rows


@pytest.mark.asyncio
async def test_a_tool_round_is_on_disk_before_the_next_call(tmp_path):
    """The call and its result are filed while the turn is still running."""
    seen: list[tuple[int, int]] = []
    path_holder: dict[str, Path] = {}

    class Watch(AgentHook):
        async def before_iteration(self, ctx):
            path = path_holder.get("path")
            if path is not None and path.exists():
                rows = _filed(path)
                seen.append(
                    (
                        sum(1 for r in rows if r.get("role") == "assistant" and r.get("tool_calls")),
                        sum(1 for r in rows if r.get("role") == "tool"),
                    )
                )
            return HookDecision()

    loop = _loop(tmp_path, _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("done")]), [Watch()])
    path_holder["path"] = loop.sessions.session_path("cli:c")

    await loop._process_message(_req())

    rows = _filed(path_holder["path"])
    assert any(r.get("role") == "assistant" and r.get("tool_calls") for r in rows)
    assert any(r.get("role") == "tool" for r in rows)
    assert any(count >= 1 for count, _ in seen), "the second iteration saw the first round already filed"


def test_a_call_that_never_returned_is_repaired_on_read(tmp_path):
    """A transcript can end mid-call; a strict provider rejects it unrepaired."""
    from raven.session.manager import read_session_file

    path = tmp_path / "s.jsonl"
    path.write_text(
        json.dumps({"_type": "metadata", "key": "cli:c", "created_at": "2026-01-01T00:00:00", "metadata": {}})
        + "\n"
        + json.dumps(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
            }
        )
        + "\n"
        + json.dumps({"role": "assistant", "content": "unfinished"}).replace("unfinished", "later")
        + "\n",
        encoding="utf-8",
    )

    session = read_session_file(path, "cli:c")

    assert session is not None
    assert [m.get("role") for m in session.messages] == ["assistant", "tool", "assistant"]
    repaired = [m for m in session.messages if m.get("role") == "tool"]
    assert repaired and repaired[0]["tool_call_id"] == "c1"
    assert repaired[0]["name"] == "exec"
    assert repaired[0]["content"].startswith("[interrupted]")


def test_a_repaired_call_keeps_its_place_across_a_restart(tmp_path):
    """The synthetic result stays at the call boundary when the turn resumes and saves."""
    from raven.session.manager import SessionManager, read_session_file

    manager = SessionManager(tmp_path)
    path = manager.session_path("cli:c")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                json.dumps({"_type": "metadata", "key": "cli:c", "created_at": "2026-01-01T00:00:00", "metadata": {}}),
                json.dumps({"role": "user", "content": "go"}),
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
                        ],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    first = read_session_file(path, "cli:c")
    assert first is not None
    assert [m.get("role") for m in first.messages] == ["user", "assistant", "tool"]

    first.record({"role": "user", "content": "continue"})
    first.record({"role": "assistant", "content": "resumed"})
    manager.save(first)

    second = read_session_file(path, "cli:c")
    assert second is not None
    assert [m.get("role") for m in second.messages] == ["user", "assistant", "tool", "user", "assistant"]


def test_only_the_missing_result_of_a_multi_call_is_inserted(tmp_path):
    """A partial round keeps its answered call first and repairs the one still open."""
    from raven.session.manager import read_session_file

    path = tmp_path / "s.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"_type": "metadata", "key": "cli:c", "created_at": "2026-01-01T00:00:00", "metadata": {}}),
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
                            {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                        ],
                    }
                ),
                json.dumps({"role": "tool", "tool_call_id": "c1", "name": "exec", "content": "ok"}),
                json.dumps({"role": "assistant", "content": "later"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    session = read_session_file(path, "cli:c")

    assert session is not None
    assert [m.get("role") for m in session.messages] == ["assistant", "tool", "tool", "assistant"]
    assert [m.get("tool_call_id") for m in session.messages if m.get("role") == "tool"] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_a_turns_closing_row_is_filed_and_never_twice(tmp_path):
    """The last answer is a row of its own, and the id keeps it from repeating."""
    loop = _loop(tmp_path, _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("all done")]))
    path = loop.sessions.session_path("cli:c")

    await loop._process_message(_req())

    rows = _filed(path)
    answers = [r for r in rows if r.get("role") == "assistant" and r.get("content") == "all done"]
    assert len(answers) == 1, "the closing answer is filed exactly once"
    turn_rows = [r for r in rows if r.get("role")]
    assert turn_rows, "the turn filed no rows at all"
    assert all(r.get("_row_id") for r in turn_rows), "every turn row is written under an identifier"
    ids = [r["_row_id"] for r in turn_rows]
    assert len(ids) == len(set(ids)), "no row is written under two identifiers"


def test_a_turn_meta_only_restates_known_stamp_keys(tmp_path):
    """The record is a stamp carrier, not a second way to rewrite a message."""
    from raven.session.manager import read_session_file

    path = tmp_path / "s.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"_type": "metadata", "key": "cli:c", "created_at": "2026-01-01T00:00:00", "metadata": {}}),
                json.dumps({"role": "assistant", "content": "done", "_row_id": "abc123"}),
                json.dumps(
                    {
                        "_type": "turn_meta",
                        "patches": [
                            {
                                "row_id": "abc123",
                                "keys": {"content": "rewritten", "replied_via_tool": True, "not_a_stamp": 1},
                            }
                        ],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    session = read_session_file(path, "cli:c")

    assert session is not None
    assert session.messages[0]["content"] == "done", "a patch may not rewrite message content"
    assert session.messages[0]["replied_via_tool"] is True
    assert "not_a_stamp" not in session.messages[0]


@pytest.mark.asyncio
async def test_a_failed_turn_meta_write_does_not_fail_the_turn(tmp_path, monkeypatch):
    """The reply is already on disk; losing the late stamp is the lesser loss.

    A turn the message tool answered files its call while the turn runs, and the
    reply's provenance lands on that row afterwards, as a turn_meta record. When
    filing that record fails, the turn has still produced and filed its answer.
    """
    from raven.agent.tools.message import MessageTool
    from raven.session import manager as manager_mod

    class Counter(AgentHook):
        async def after_send(self, ctx):
            ctx.metadata.setdefault("observers", {})["send_gate"] = {"fired": 1}
            return HookDecision()

    failed: list[list[dict]] = []

    def _boom(_path, patches, **_kwargs):
        failed.append(patches)
        raise OSError("disk full")

    loop = _loop(
        tmp_path,
        _ScriptedProvider([_tool_call("message", {"content": "hello"}), _text("")]),
        [Counter()],
    )
    message = loop.tools.get("message")
    assert isinstance(message, MessageTool)
    message.set_context("cli", "c")

    async def _send(_content: str, _media: list[str]) -> None:
        return None

    message.set_send_callback(_send)
    monkeypatch.setattr(manager_mod, "append_turn_meta", _boom)

    out = await loop._process_message(_req())

    assert failed, "the turn filed no late stamp, so the failure path was not exercised"
    assert out is None, "the message tool answered, so the turn has no reply of its own"
    rows = _filed(loop.sessions.session_path("cli:c"))
    calls = [call["function"]["name"] for row in rows for call in (row.get("tool_calls") or [])]
    assert "message" in calls, "the answer's call is on disk"


@pytest.mark.asyncio
async def test_another_clients_metadata_patch_survives_the_turns_late_stamps(tmp_path):
    """A flag another process sets while the turn finishes is still on disk after it.

    The late stamps are filed after the turn's last incremental write, which is
    the window in which another client archives, pins or renames the session. That
    patch is not a second writer, so the closing save folds it in -- unless the
    late stamps moved this copy's stamp past it, which made the closing save take
    the file for unchanged and write this copy's metadata over the flag.
    """
    from raven.agent.tools.message import MessageTool
    from raven.session.manager import SessionManager, read_session_file

    class ArchivedElsewhere(AgentHook):
        async def after_send(self, ctx):
            ctx.metadata.setdefault("observers", {})["send_gate"] = {"fired": 1}
            assert SessionManager(tmp_path).append_metadata_patch("cli:c", {"archived": True})
            return HookDecision()

    loop = _loop(
        tmp_path,
        _ScriptedProvider([_tool_call("message", {"content": "hello"}), _text("")]),
        [ArchivedElsewhere()],
    )
    message = loop.tools.get("message")
    assert isinstance(message, MessageTool)
    message.set_context("cli", "c")

    async def _send(_content: str, _media: list[str]) -> None:
        return None

    message.set_send_callback(_send)

    await loop._process_message(_req())

    path = loop.sessions.session_path("cli:c")
    kinds = [json.loads(line).get("_type") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert "turn_meta" in kinds, "the turn filed no late stamp, so the window was not under test"
    assert read_session_file(path, "cli:c").metadata.get("archived") is True, (
        "the closing save wrote over the archive flag another client set"
    )


def test_a_turn_meta_restates_a_row_by_its_identifier(tmp_path):
    """A stamp filed after the row was written is merged on the way in."""
    from raven.session.manager import read_session_file

    path = tmp_path / "s.jsonl"
    row = {"role": "assistant", "content": "done", "_row_id": "abc123"}
    path.write_text(
        "\n".join(
            [
                json.dumps({"_type": "metadata", "key": "cli:c", "created_at": "2026-01-01T00:00:00", "metadata": {}}),
                json.dumps({"role": "user", "content": "hi"}),
                json.dumps(row),
                json.dumps(
                    {"_type": "turn_meta", "patches": [{"row_id": "abc123", "keys": {"replied_via_tool": True}}]}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    session = read_session_file(path, "cli:c")

    assert session is not None
    filed = [m for m in session.messages if m.get("content") == "done"]
    assert filed and filed[0]["replied_via_tool"] is True
    assert "_row_id" not in filed[0], "the filing key is not handed to readers"


def test_a_turn_meta_starts_on_a_fresh_line_after_a_crashed_append(tmp_path):
    """A patch filed after a partial last line still applies on the way in."""
    from raven.session.manager import append_turn_meta, read_session_file

    path = tmp_path / "s.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"_type": "metadata", "key": "cli:c", "created_at": "2026-01-01T00:00:00", "metadata": {}}),
                json.dumps({"role": "assistant", "content": "done", "_row_id": "abc123"}),
            ]
        )
        + '\n{"role": "assistant", "content": "half-writ',
        encoding="utf-8",
    )

    append_turn_meta(path, [{"row_id": "abc123", "keys": {"replied_via_tool": True}}])

    session = read_session_file(path, "cli:c")
    assert session is not None
    filed = [m for m in session.messages if m.get("content") == "done"]
    assert filed and filed[0]["replied_via_tool"] is True


def test_an_incremental_write_does_not_wait_for_a_picture():
    """A picture row is fileable: the writer replaces its bytes with a note.

    The old boundary held every row behind a picture until a window pass
    rewrote the row, and the newest pictures stay in the window for the whole
    turn, so a turn whose only picture was recent filed nothing after it.
    """
    from raven.agent.loop.turn_path import _incremental_upto

    text = {"role": "assistant", "content": "looking"}
    picture = {"role": "tool", "content": [{"type": "image_url", "image_url": {"url": "data:,"}}]}
    after = {"role": "assistant", "content": "done"}

    assert _incremental_upto([text, picture, after], 0) == 3
    assert _incremental_upto([picture, after], 0) == 2
    assert _incremental_upto([text, after], 2) is None, "nothing new is nothing to write"


def test_a_multipart_picture_row_is_filed_with_its_text_and_a_placeholder(tmp_path):
    """The check must read every part: an image after the first one is still an image."""
    from raven.agent.loop.turn_path import _incremental_upto

    picture = {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "screenshot",
        "content": [
            {"type": "text", "text": "the window"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }
    after = {"role": "assistant", "content": "done"}

    assert _incremental_upto([picture, after], 0) == 2

    loop = _loop(tmp_path, _ScriptedProvider([_text("unused")]))
    session = loop.sessions.get_or_create("cli:c")
    loop._save_turn(session, [picture, after], 0, final=False)
    loop.sessions.save(session)

    path = loop.sessions.session_path("cli:c")
    rows = _filed(path)
    filed_picture = next(row for row in rows if row.get("tool_call_id") == "c1")
    assert filed_picture["content"] == [
        {"type": "text", "text": "the window"},
        {"type": "text", "text": "[image]"},
    ]
    assert any(row.get("content") == "done" for row in rows), "the row after the picture was filed"
    assert "base64" not in path.read_text(encoding="utf-8")


def test_a_row_rewritten_after_it_was_filed_is_filed_in_its_new_form(tmp_path):
    """A filed row can be rewritten in place: the image window turns a picture
    into a note. The latest version is what the file holds, once, under the
    identifier it was filed by."""
    from raven.agent.window.images import filed_image_note, withdrawn_image_note

    loop = _loop(tmp_path, _ScriptedProvider([_text("unused")]))
    session = loop.sessions.get_or_create("cli:c")
    path = loop.sessions.session_path("cli:c")
    picture = {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "screenshot",
        "content": [
            {"type": "text", "text": "the window"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }
    after = {"role": "assistant", "content": "done"}

    loop._save_turn(session, [picture, after], 0, final=False)
    loop.sessions.save(session)
    filed = next(row for row in _filed(path) if row.get("tool_call_id") == "c1")
    assert filed["content"][-1]["text"] == "[image]"
    row_id = filed["_row_id"]

    note = withdrawn_image_note(
        {"caption": "the window", "tool": "screenshot", "iteration": 1},
        index=1,
        total=1,
        reason="budget",
        keep=0,
    )
    picture["content"][-1] = {"type": "text", "text": note}

    loop._save_turn(session, [picture, after], len(session.messages), final=False)
    loop.sessions.save(session)

    rows = [row for row in _filed(path) if row.get("tool_call_id") == "c1"]
    assert len(rows) == 1, "the rewritten row is replaced, not appended beside the old one"
    assert rows[0]["_row_id"] == row_id, "the row keeps the identifier a late stamp addresses"
    assert rows[0]["content"][-1]["text"] == filed_image_note(note)
    assert "[image]" not in json.dumps(rows[0]["content"])
    assert loop._save_turn(session, [picture, after], len(session.messages), final=False) == [], (
        "an unchanged filed row was prepared and written again"
    )


def test_a_row_replaced_in_place_is_not_one_this_write_added(tmp_path):
    """The rerun cut is built from what the write appended, so a rewrite is not in it.

    A row already in the file that a later write replaces keeps its identifier --
    the image window withdraws a picture from a row the turn already filed. It is
    not a second row, and the attempt did not write it now. Counted among the
    attempt's rows, the cut that clears the attempt would take it off the
    transcript as well: the user's own question went that way.
    """
    from raven.agent.window.images import withdrawn_image_note

    loop = _loop(tmp_path, _ScriptedProvider([_text("unused")]))
    session = loop.sessions.get_or_create("cli:c")
    picture = {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "screenshot",
        "content": [
            {"type": "text", "text": "the window"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }
    after = {"role": "assistant", "content": "done"}

    appended = loop._save_turn(session, [picture, after], 0, final=False)
    assert appended == [picture["_row_id"], after["_row_id"]], "the fixture did not append both rows"

    picture["content"][-1] = {
        "type": "text",
        "text": withdrawn_image_note(
            {"caption": "the window", "tool": "screenshot", "iteration": 1},
            index=1,
            total=1,
            reason="budget",
            keep=0,
        ),
    }
    replaced = loop._save_turn(session, [picture, after], len(session.messages), final=False)

    assert replaced == [], "the row it replaced in place was reported as one it added"
    assert loop.sessions.drop_filed_rows(session, set(replaced)) is True
    assert any(row.get("tool_call_id") == "c1" for row in session.messages), (
        "the rerun's cut took a row the attempt did not write"
    )


def test_an_image_question_keeps_its_placeholder_when_the_turn_closes(tmp_path):
    """The opening write files a multimodal question as blocks plus the placeholder.

    The closing write prepares that row again from the live message, which still
    carries the bytes. Restoring the words the user typed would make the closing
    write a rewrite -- the file would keep the question's text alone, with the
    placeholder gone -- so what the inbound row is restored to is the form the
    opening write filed.
    """
    loop = _loop(tmp_path, _ScriptedProvider([_text("unused")]))
    session = loop.sessions.get_or_create("cli:c")
    question = {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }

    loop._save_turn(session, [question], 0, received_at="2026-01-01T00:00:00", inbound_original="what is this?")
    loop.sessions.save(session)
    loop._save_turn(session, [question, {"role": "assistant", "content": "a picture"}], 1)
    loop.sessions.save(session)

    rows = _filed(loop.sessions.session_path("cli:c"))
    assert rows[0]["content"] == [
        {"type": "text", "text": "what is this?"},
        {"type": "text", "text": "[image]"},
    ], "the closing write rewrote the question as text"
