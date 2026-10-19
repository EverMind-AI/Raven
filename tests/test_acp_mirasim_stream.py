"""A Mirasim session's snapshot rebuilt from the host's patches, and the ACP updates each change makes."""

from __future__ import annotations

from typing import Any

from raven.acp.mirasim.stream import SessionView, TurnStream, is_running, replay, tool_kind
from tests.acp_schema import validate_def


def session(seq: int, patch: dict[str, Any], *, start: int | None = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "session", "sessionKey": "claude:k", "seq": seq, "patch": patch}
    if start is not None:
        frame["fromSeq"] = start
    return frame


def texts(updates: list[dict[str, Any]], kind: str) -> list[str]:
    return [update["content"]["text"] for update in updates if update["sessionUpdate"] == kind]


def valid(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for update in updates:
        validate_def("SessionUpdate", update)
    return updates


# -- SessionView ----------------------------------------------------------------


def test_patches_append_text_and_reasoning_then_replace_keys() -> None:
    view = SessionView()
    view.reset({"phase": "queued", "taskId": "t1", "text": "", "reasoning": ""}, 0)
    assert view.apply(session(1, {"set": {"phase": "streaming"}}))
    assert view.apply(session(5, {"appendReasoning": "The", "set": {"updatedAt": 1}}, start=1))
    assert view.apply(session(9, {"appendReasoning": " user", "appendText": "Hi"}, start=5))
    assert view.snapshot["phase"] == "streaming"
    assert (view.snapshot["reasoning"], view.snapshot["text"]) == ("The user", "Hi")
    assert view.seq == 9


def test_a_set_in_the_same_patch_wins_over_an_append_as_the_host_applies_them() -> None:
    view = SessionView()
    view.reset({"text": "old"}, 3)
    assert view.apply(session(4, {"appendText": "er", "set": {"text": "", "taskId": "t2"}}))
    assert view.snapshot["text"] == ""


def test_a_full_patch_stands_on_its_own() -> None:
    view = SessionView()
    assert view.apply(session(1, {"full": {"phase": "streaming", "taskId": "t1", "text": ""}}, start=-1))
    assert view.snapshot == {"phase": "streaming", "taskId": "t1", "text": ""}
    assert view.seq == 1


def test_list_patches_update_items_by_index() -> None:
    view = SessionView()
    view.reset({"toolCalls": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}, 7)
    lists = {
        "toolCalls": {"len": 2, "at": {"1": {"id": "b", "status": "done"}}},
        "artifacts": {"len": 1, "at": {"0": {"path": "/x"}}},
    }
    assert view.apply(session(8, {"lists": lists}, start=7))
    assert view.snapshot["toolCalls"] == [{"id": "a"}, {"id": "b", "status": "done"}]
    assert view.snapshot["artifacts"] == [{"path": "/x"}]


def test_a_list_also_set_in_the_patch_keeps_the_set() -> None:
    view = SessionView()
    view.reset({"toolCalls": [{"id": "a"}]}, 1)
    assert view.apply(
        session(2, {"set": {"toolCalls": []}, "lists": {"toolCalls": {"len": 1, "at": {"0": {"id": "z"}}}}})
    )
    assert view.snapshot["toolCalls"] == []


def test_a_frame_that_does_not_follow_the_last_asks_for_a_fresh_snapshot() -> None:
    view = SessionView()
    view.reset({"text": "a"}, 10)
    assert not view.apply(session(14, {"appendText": "b"}, start=12))
    assert view.seq is None
    # Until a snapshot arrives nothing more is applied.
    assert not view.apply(session(15, {"appendText": "c"}, start=14))
    assert view.snapshot["text"] == "a"
    view.reset({"text": "abc"}, 15)
    assert view.apply(session(16, {"appendText": "d"}))
    assert view.snapshot["text"] == "abcd"


def test_without_a_starting_sequence_each_frame_follows_the_last_by_one() -> None:
    view = SessionView()
    view.reset({}, 1)
    assert view.apply(session(2, {"set": {"phase": "streaming"}}))
    assert not view.apply(session(4, {"set": {"phase": "done"}}))


def test_a_patch_before_any_snapshot_is_a_gap_and_malformed_frames_are_ignored() -> None:
    view = SessionView()
    assert not view.apply(session(1, {"appendText": "x"}))
    view.reset("not a snapshot", "nor a seq")
    assert view.snapshot == {} and view.seq is None
    view.reset({}, 0)
    assert view.apply({"type": "session", "seq": 1, "patch": "nonsense"})
    assert view.apply({"type": "session", "seq": True, "patch": {}})
    assert view.apply(session(1, {"lists": "nonsense", "set": "nonsense"}))
    assert view.apply(session(2, {"lists": {"toolCalls": "nonsense", "x": {"len": 1, "at": {"one": 1}}}}))
    assert view.snapshot == {"x": []}


def test_the_running_phases_are_queued_and_streaming() -> None:
    assert is_running({"phase": "queued"}) and is_running({"phase": "streaming"})
    assert not is_running({"phase": "done"}) and not is_running({"phase": "idle"}) and not is_running({})


# -- TurnStream -----------------------------------------------------------------


def test_text_and_reasoning_go_out_as_they_grow() -> None:
    stream = TurnStream()
    first = valid(stream.updates({"taskId": "t1", "reasoning": "Let me", "text": ""}))
    assert texts(first, "agent_thought_chunk") == ["Let me"]
    second = valid(stream.updates({"taskId": "t1", "reasoning": "Let me see", "text": "Hello"}))
    assert texts(second, "agent_thought_chunk") == [" see"]
    assert texts(second, "agent_message_chunk") == ["Hello"]
    assert stream.updates({"taskId": "t1", "reasoning": "Let me see", "text": "Hello"}) == []


def test_a_new_turn_starts_the_stream_over() -> None:
    stream = TurnStream()
    stream.updates({"taskId": "t1", "text": "first answer", "toolCalls": [{"id": "c1", "name": "Bash"}]})
    updates = stream.updates({"taskId": "t2", "text": "first", "toolCalls": [{"id": "c1", "name": "Bash"}]})
    assert texts(updates, "agent_message_chunk") == ["first"]
    assert [update["sessionUpdate"] for update in updates].count("tool_call") == 1


def test_text_rewritten_under_the_client_is_not_sent_again() -> None:
    stream = TurnStream()
    stream.updates({"taskId": "t1", "text": "draft one"})
    assert stream.updates({"taskId": "t1", "text": "another"}) == []
    assert texts(stream.updates({"taskId": "t1", "text": "another day"}), "agent_message_chunk") == [" day"]
    assert stream.updates({"taskId": "t1", "text": "x"}) == []


def test_a_tool_call_opens_runs_and_settles_with_its_result() -> None:
    stream = TurnStream()
    call = {
        "id": "call_b5",
        "name": "Bash",
        "summary": "Run shell: list files",
        "input": "ls -la .",
        "status": "running",
    }
    opened = valid(stream.updates({"taskId": "t1", "toolCalls": [call]}))
    assert opened == [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call_b5",
            "title": "Run shell: list files",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": "ls -la .",
        }
    ]
    assert stream.updates({"taskId": "t1", "toolCalls": [call]}) == []
    settled = valid(stream.updates({"taskId": "t1", "toolCalls": [{**call, "status": "done", "result": "total 0"}]}))
    assert settled == [
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_b5",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "total 0"}}],
        }
    ]
    failed = stream.updates({"taskId": "t1", "toolCalls": [{**call, "status": "failed", "result": {"code": 1}}]})
    assert failed[0]["status"] == "failed"
    assert failed[0]["content"][0]["content"]["text"] == '{"code": 1}'


def test_a_tool_call_first_seen_settled_opens_with_its_result_and_nameless_ones_are_skipped() -> None:
    stream = TurnStream()
    updates = valid(
        stream.updates(
            {
                "taskId": "t1",
                "toolCalls": [{"id": "r1", "name": "Read", "status": "done", "result": "x"}, {"name": "?"}, "junk"],
            }
        )
    )
    assert len(updates) == 1
    assert (updates[0]["title"], updates[0]["kind"], updates[0]["status"]) == ("Read", "read", "completed")
    assert "rawInput" not in updates[0]
    assert updates[0]["content"][0]["content"]["text"] == "x"


def test_a_result_that_is_not_json_is_sent_as_its_text() -> None:
    stream = TurnStream()
    stream.updates({"taskId": "t1", "toolCalls": [{"id": "c", "name": "x", "status": "running"}]})
    update = stream.updates(
        {"taskId": "t1", "toolCalls": [{"id": "c", "name": "x", "status": "done", "result": {1, 2}}]}
    )
    assert update[0]["content"][0]["content"]["text"] in ("{1, 2}", "{2, 1}")


def test_the_plan_is_sent_whole_each_time_it_changes_and_cleared_when_it_goes() -> None:
    stream = TurnStream()
    plan = {
        "explanation": None,
        "steps": [
            {"text": "Read", "status": "completed"},
            {"text": "Write", "status": "inProgress"},
            {"text": "", "status": "pending"},
        ],
    }
    sent = valid(stream.updates({"taskId": "t1", "plan": plan}))
    assert sent == [
        {
            "sessionUpdate": "plan",
            "entries": [
                {"content": "Read", "priority": "medium", "status": "completed"},
                {"content": "Write", "priority": "medium", "status": "in_progress"},
            ],
        }
    ]
    assert stream.updates({"taskId": "t1", "plan": plan}) == []
    assert stream.updates({"taskId": "t1", "plan": None}) == [{"sessionUpdate": "plan", "entries": []}]
    assert stream.updates({"taskId": "t1", "plan": None}) == []


def test_a_sub_agent_is_a_tool_call_that_settles_with_it() -> None:
    stream = TurnStream()
    started = valid(
        stream.updates(
            {"taskId": "t1", "subAgents": [{"id": "s1", "description": "Explore the repo", "status": "running"}, {}]}
        )
    )
    assert started == [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "subagent-s1",
            "title": "Explore the repo",
            "kind": "other",
            "status": "in_progress",
        }
    ]
    done = stream.updates({"taskId": "t1", "subAgents": [{"id": "s1", "status": "done"}]})
    assert done == [{"sessionUpdate": "tool_call_update", "toolCallId": "subagent-s1", "status": "completed"}]
    assert stream.updates({"taskId": "t1", "subAgents": [{"id": "s2"}]})[0]["title"] == "Sub-agent"


def test_a_delivered_file_is_a_link_once(tmp_path) -> None:
    stream = TurnStream()
    target = tmp_path / "report.pdf"
    artifacts = [
        {"path": str(target), "fileName": "report.pdf", "mime": "application/pdf", "fileSize": 42},
        {"path": "relative/only.txt"},
        {"fileName": "no path"},
    ]
    sent = valid(stream.updates({"taskId": "t1", "artifacts": artifacts}))
    assert sent == [
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {
                "type": "resource_link",
                "uri": target.as_uri(),
                "name": "report.pdf",
                "mimeType": "application/pdf",
                "size": 42,
            },
        }
    ]
    assert stream.updates({"taskId": "t1", "artifacts": artifacts}) == []


def test_a_steer_the_turn_took_is_the_users_words_and_a_pending_one_is_not_yet() -> None:
    stream = TurnStream()
    assert stream.updates({"taskId": "t1", "steers": [{"text": "wait", "pending": True}]}) == []
    landed = valid(stream.updates({"taskId": "t1", "steers": [{"text": "Change of plan", "at": 1, "textOffset": 65}]}))
    assert texts(landed, "user_message_chunk") == ["Change of plan"]
    assert stream.updates({"taskId": "t1", "steers": [{"text": "Change of plan"}]}) == []
    assert stream.updates({"taskId": "t1", "steers": None}) == []


def test_a_turn_whose_end_was_reported_adds_nothing_more() -> None:
    stream = TurnStream()
    stream.updates({"taskId": "t1", "text": "done"})
    stream.ended = True
    assert stream.updates({"taskId": "t1", "text": "done and more"}) == []
    assert texts(stream.updates({"taskId": "t2", "text": "next"}), "agent_message_chunk") == ["next"]


def test_updates_come_in_the_order_a_turn_makes_them() -> None:
    stream = TurnStream()
    updates = stream.updates(
        {
            "taskId": "t1",
            "steers": [{"text": "also"}],
            "reasoning": "hm",
            "text": "Let me look.",
            "toolCalls": [{"id": "c", "name": "Grep", "status": "running"}],
            "plan": {"steps": [{"text": "a", "status": "pending"}]},
        }
    )
    assert [update["sessionUpdate"] for update in updates] == [
        "user_message_chunk",
        "agent_thought_chunk",
        "agent_message_chunk",
        "tool_call",
        "plan",
    ]


def test_tool_kinds_are_matched_without_case_and_anything_else_is_other() -> None:
    assert tool_kind("Bash") == "execute" and tool_kind("exec_command") == "execute"
    assert tool_kind("Read") == "read" and tool_kind("Edit") == "edit" and tool_kind("apply_patch") == "edit"
    assert tool_kind("Grep") == "search" and tool_kind("WebFetch") == "fetch" and tool_kind("TodoWrite") == "think"
    assert tool_kind("mcp__mirasim__deliver_artifact") == "other" and tool_kind(None) == "other"


async def test_a_snapshot_that_carries_segments_keeps_every_segment_in_order() -> None:
    from raven.acp_client.acp_agent import _TurnCollector

    snapshot = {
        "taskId": "t1",
        "text": "\U0001f600Look.Check.Done.",
        "reasoning": "Think.\U0001f642Again.",
        "toolCalls": [
            {"id": "c1", "name": "Read", "status": "done", "result": "first"},
            {"id": "c2", "name": "Read", "status": "done", "result": "second"},
        ],
        "subAgents": [{"id": "s1", "status": "done", "description": "Inspect"}],
        "segments": [
            {"kind": "thinking", "from": 0, "to": 6},
            {"kind": "text", "from": 0, "to": 7},
            {"kind": "tools", "ids": ["c1"]},
            {"kind": "thinking", "from": 6, "to": 14},
            {"kind": "text", "from": 7, "to": 13},
            {"kind": "tools", "ids": ["c2"]},
            {"kind": "agents", "ids": ["s1"]},
            {"kind": "text", "from": 13, "to": 18},
        ],
    }
    stream = TurnStream()
    updates = valid(stream.updates(snapshot))
    assert [(u["sessionUpdate"], u.get("toolCallId") or u["content"]["text"]) for u in updates] == [
        ("agent_thought_chunk", "Think."),
        ("agent_message_chunk", "\U0001f600Look."),
        ("tool_call", "c1"),
        ("agent_thought_chunk", "\U0001f642Again."),
        ("agent_message_chunk", "Check."),
        ("tool_call", "c2"),
        ("tool_call", "subagent-s1"),
        ("agent_message_chunk", "Done."),
    ]
    collector = _TurnCollector(track_files=False)
    for update in updates:
        await collector("session/update", {"sessionId": "s1", "update": update})
    assert collector.reply == "Done."
    assert stream.updates(snapshot) == []
    snapshot["text"] += " More."
    snapshot["segments"][-1]["to"] += 6
    assert texts(valid(stream.updates(snapshot)), "agent_message_chunk") == [" More."]


async def test_a_whole_snapshot_without_segments_keeps_the_answer_after_a_call_it_brings() -> None:
    from raven.acp_client.acp_agent import _TurnCollector

    stream = TurnStream()
    updates = stream.updates({"phase": "streaming", "taskId": "t1", "text": "I will", "toolCalls": []})
    # Live frames carry no segments. Across a reconnect, one snapshot brings the rest of the
    # sentence, a call that started and ended meanwhile, and the start of the answer after it.
    done = {"id": "c1", "name": "Bash", "status": "done", "result": "ok"}
    caught_up = {"phase": "streaming", "taskId": "t1", "text": "I will check.All checks", "toolCalls": [done]}
    updates += stream.updates(caught_up, whole=True)
    updates += stream.updates({**caught_up, "phase": "done", "text": "I will check.All checks passed."})
    collector = _TurnCollector(track_files=False)
    for update in valid(updates):
        await collector("session/update", {"sessionId": "s1", "update": update})
    assert collector.reply.endswith("All checks passed.")
    assert [u["sessionUpdate"] for u in updates] == [
        "agent_message_chunk",
        "tool_call",
        "agent_message_chunk",
        "agent_message_chunk",
    ]


def test_a_patch_without_segments_sends_its_text_before_a_call_it_opens() -> None:
    stream = TurnStream()
    stream.updates({"phase": "streaming", "taskId": "t1", "text": "", "toolCalls": []})
    opened = {"id": "c1", "name": "Bash", "status": "running"}
    updates = stream.updates({"phase": "streaming", "taskId": "t1", "text": "I will check.", "toolCalls": [opened]})
    assert [u["sessionUpdate"] for u in valid(updates)] == ["agent_message_chunk", "tool_call"]


def test_partial_or_malformed_segments_drop_neither_text_nor_calls() -> None:
    updates = valid(
        TurnStream().updates(
            {
                "taskId": "t1",
                "text": "answer",
                "reasoning": "thinking",
                "toolCalls": [{"id": "c1", "name": "Read"}],
                "segments": [
                    None,
                    {"kind": []},
                    {"kind": "text", "from": -1, "to": 6},
                    {"kind": "tools", "ids": "c1"},
                    {"kind": "tools", "ids": [False]},
                    {"kind": "thinking", "from": 0, "to": 3},
                    {"kind": "thinking", "from": 0, "to": 3},
                    {"kind": "text", "from": 0, "to": "6"},
                ],
            }
        )
    )
    assert texts(updates, "agent_message_chunk") == ["answer"]
    assert texts(updates, "agent_thought_chunk") == ["thi", "nking"]
    assert updates[-1]["toolCallId"] == "c1"


# -- replay ---------------------------------------------------------------------


def test_a_turn_replays_its_prompt_then_its_segments_in_order() -> None:
    turn = {
        "taskId": "t1",
        "prompt": "Count to three",
        "reasoning": "The user wants a count. Easy.",
        "text": "1\n2\n3",
        "segments": [
            {"kind": "thinking", "from": 0, "to": 24},
            {"kind": "text", "from": 0, "to": 5},
            {"kind": "thinking", "from": 24, "to": 30},
        ],
    }
    updates = valid(replay(turn))
    assert [(update["sessionUpdate"], update["content"]["text"]) for update in updates] == [
        ("user_message_chunk", "Count to three"),
        ("agent_thought_chunk", "The user wants a count. "),
        ("agent_message_chunk", "1\n2\n3"),
        ("agent_thought_chunk", "Easy."),
    ]


def test_a_turn_without_segments_replays_its_reasoning_then_its_text() -> None:
    updates = replay({"prompt": "", "reasoning": "hm", "text": "answer", "segments": [{"kind": "tool"}, "junk"]})
    assert [update["sessionUpdate"] for update in updates] == ["agent_thought_chunk", "agent_message_chunk"]


async def test_replay_uses_utf16_offsets_around_tools_and_thoughts() -> None:
    from raven.acp_client.acp_agent import _TurnCollector

    turn = {
        "text": "\U0001f600OK",
        "reasoning": "\U0001f642check",
        "toolCalls": [{"id": "c1", "name": "Read", "status": "done", "result": "ok"}],
        "segments": [
            {"kind": "text", "from": 0, "to": 2},
            {"kind": "thinking", "from": 0, "to": 2},
            {"kind": "tools", "ids": ["c1"]},
            {"kind": "thinking", "from": 2, "to": 7},
            {"kind": "text", "from": 2, "to": 4},
        ],
    }
    updates = valid(replay(turn))
    collector = _TurnCollector(track_files=False)
    for update in updates:
        await collector("session/update", {"sessionId": "s1", "update": update})
    assert collector.reply == "OK"
    assert texts(updates, "agent_message_chunk") == ["\U0001f600", "OK"]
    assert texts(updates, "agent_thought_chunk") == ["\U0001f642", "check"]


def test_tool_calls_the_history_kept_replay_before_the_last_stretch_of_text(tmp_path) -> None:
    turn = {
        "prompt": "go",
        "text": "Looking. Done.",
        "segments": [{"kind": "text", "from": 0, "to": 9}, {"kind": "text", "from": 9, "to": 14}],
        "toolCalls": [{"id": "c1", "name": "Bash", "summary": "ls", "result": "a b"}, {"name": "unnamed"}],
        "artifacts": [{"path": str(tmp_path / "out.txt")}],
    }
    kinds = [
        (update["sessionUpdate"], update.get("toolCallId") or update.get("content", {}).get("type"))
        for update in valid(replay(turn))
    ]
    assert kinds == [
        ("user_message_chunk", "text"),
        ("agent_message_chunk", "text"),
        ("tool_call", "c1"),
        ("agent_message_chunk", "text"),
        ("agent_message_chunk", "resource_link"),
    ]
    assert replay(turn)[2]["status"] == "completed"


def test_a_turn_with_tools_and_no_text_replays_the_tools_last() -> None:
    updates = replay({"reasoning": "r", "toolCalls": [{"id": "c1", "name": "Read"}]})
    assert [update["sessionUpdate"] for update in updates] == ["agent_thought_chunk", "tool_call"]
