"""Stored transcript -> session/update stream, on Raven-X's real stored shape."""

from raven.acp.replay import MAX_REPLAYED_MESSAGES, MAX_REPLAYED_TEXT, replay


def _kinds(updates):
    return [u["sessionUpdate"] for u in updates]


def test_user_and_assistant_text_replay_in_order():
    messages = [
        {"role": "user", "content": "question", "timestamp": "t", "flow_version": "dr@3.4"},
        {"role": "assistant", "content": "answer", "finish_reason": "stop"},
    ]
    updates = replay(messages)
    assert _kinds(updates) == ["user_message_chunk", "agent_message_chunk"]
    assert updates[0]["content"]["text"] == "question"
    assert updates[1]["content"]["text"] == "answer"


def test_assistant_replays_thought_then_words_then_calls():
    # The real persisted shape (verified against a session file): text under
    # ``content``, reasoning under ``reasoning_content``, OpenAI-shaped
    # tool_calls with arguments as the JSON string the provider sent.
    entry = {
        "role": "assistant",
        "content": "let me check",
        "reasoning_content": "hmm",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query": "fed rate"}'},
            }
        ],
    }
    updates = replay([entry])
    assert _kinds(updates) == ["agent_thought_chunk", "agent_message_chunk", "tool_call"]
    call = updates[2]
    assert call["toolCallId"] == "call-1"
    assert call["kind"] == "search"
    # pending, not in_progress: the work finished long ago and its own
    # tool_call_update follows from the role="tool" entry.
    assert call["status"] == "pending"
    assert "fed rate" in call["title"]


def test_tool_result_answers_its_call_and_orphans_are_dropped():
    messages = [
        {"role": "tool", "content": "result body", "tool_call_id": "call-1", "name": "web_search"},
        {"role": "tool", "content": "orphan with no id"},
    ]
    updates = replay(messages)
    assert _kinds(updates) == ["tool_call_update"]
    assert updates[0]["toolCallId"] == "call-1"
    assert updates[0]["status"] == "completed"
    assert updates[0]["content"][0]["content"]["text"] == "result body"


def test_flat_tool_call_shape_is_accepted_too():
    entry = {"role": "assistant", "tool_calls": [{"id": "c2", "name": "read_file", "arguments": {"path": "/a"}}]}
    (call,) = replay([entry])
    assert call["kind"] == "read"
    assert call["locations"] == [{"path": "/a"}]


def test_unparseable_arguments_degrade_to_a_bare_title():
    entry = {"role": "assistant", "tool_calls": [{"id": "c3", "function": {"name": "exec", "arguments": "{broken"}}]}
    (call,) = replay([entry])
    assert call["title"] == "exec"


def test_system_entries_and_junk_are_not_replayed():
    messages = [
        {"role": "system", "content": "instructions"},
        "not a dict",
        {"no_role": True},
        {"role": "user", "content": "hi"},
    ]
    assert _kinds(replay(messages)) == ["user_message_chunk"]
    assert replay(None) == []
    assert replay({"role": "user"}) == []


def test_replay_redacts_and_clips():
    long = "x" * (MAX_REPLAYED_TEXT + 10)
    messages = [
        {"role": "user", "content": "api_key=sk-abcdefghijklmnop1234"},
        {"role": "assistant", "content": long},
    ]
    updates = replay(messages)
    assert "sk-abcdefghijklmnop1234" not in updates[0]["content"]["text"]
    assert updates[1]["content"]["text"].endswith("[truncated]")


def test_oldest_messages_are_dropped_with_a_notice():
    messages = [{"role": "user", "content": f"m{i}"} for i in range(MAX_REPLAYED_MESSAGES + 3)]
    updates = replay(messages)
    assert len(updates) == MAX_REPLAYED_MESSAGES + 1
    assert "earlier message(s) are not shown" in updates[0]["content"]["text"]
    assert updates[1]["content"]["text"] == "m3"  # oldest dropped, newest kept
