"""What a Mirasim turn stops to ask, put to an ACP client and read back as an answer."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from raven.acp.mirasim import interactions
from raven.acp.mirasim.interactions import (
    ANSWER_FIELD,
    CUSTOM_FIELD,
    Reply,
    describe,
    elicitation_request,
    pending,
    permission_request,
    read_elicitation,
    read_permission,
)
from tests.acp_schema import validate_def


def ask(**question: Any) -> dict[str, Any]:
    """An interaction in the shape Mirasim 0.0.465 builds for Claude Code's ``AskUserQuestion``."""
    base = {
        "id": "q0",
        "header": "Colour",
        "text": "Which colour?",
        "type": "select",
        "choices": [
            {"label": "Red", "description": "warm", "value": "Red"},
            {"label": "Blue", "value": "Blue"},
        ],
        "allowFreeform": True,
        "allowEmpty": False,
    }
    return {
        "promptId": "claude-control-1",
        "kind": "user-input",
        "title": "Claude asks",
        "hint": None,
        "questions": [{**base, **question}],
        "currentIndex": 0,
    }


def approval() -> dict[str, Any]:
    """A tool approval, which Mirasim marks with its own ``kind``."""
    return {
        "promptId": "claude-control-2",
        "kind": "permission",
        "title": "Approve tool use?",
        "hint": "Runs a shell command",
        "questions": [
            {
                "id": "decision",
                "header": "Bash",
                "text": 'Claude wants to use Bash: {"command": "ls"}',
                "type": "select",
                "choices": [{"label": "Allow"}, {"label": "Deny"}],
                "allowFreeform": False,
                "allowEmpty": False,
            }
        ],
    }


def test_only_interactions_with_a_prompt_id_are_pending() -> None:
    snapshot = {"interactions": [ask(), {"kind": "user-input"}, "noise", {"promptId": ""}]}
    assert [item["promptId"] for item in pending(snapshot)] == ["claude-control-1"]
    assert pending({}) == pending({"interactions": None}) == []


def test_a_single_choice_with_a_free_text_box_is_an_enum_and_a_marked_sibling() -> None:
    params = elicitation_request("sess-1", ask())
    validate_def("CreateElicitationRequest", params)
    assert params["sessionId"] == "sess-1" and params["mode"] == "form"
    assert "Claude asks" in params["message"] and "- Red: warm" in params["message"]
    schema = params["requestedSchema"]
    assert schema["properties"][ANSWER_FIELD] == {"type": "string", "title": "Which colour?", "enum": ["Red", "Blue"]}
    custom = schema["properties"][CUSTOM_FIELD]
    assert custom["type"] == "string" and custom["_meta"] == {"raven": {"customAnswerFor": ANSWER_FIELD}}
    # Neither is required: an answer off the list can never satisfy a required enum.
    assert schema["required"] == []


def test_a_closed_choice_is_a_required_enum_and_a_multi_choice_is_an_array() -> None:
    closed = elicitation_request("s", ask(allowFreeform=False))["requestedSchema"]
    assert set(closed["properties"]) == {ANSWER_FIELD} and closed["required"] == [ANSWER_FIELD]
    multi = elicitation_request("s", ask(allowFreeform=False, multiSelect=True))
    validate_def("CreateElicitationRequest", multi)
    field = multi["requestedSchema"]["properties"][ANSWER_FIELD]
    assert field == {"type": "array", "title": "Which colour?", "items": {"type": "string", "enum": ["Red", "Blue"]}}


def test_a_text_question_is_a_string_required_unless_it_may_be_empty() -> None:
    text = ask(type="text", choices=None, text="Name it")
    schema = elicitation_request("s", text)["requestedSchema"]
    assert schema["properties"] == {ANSWER_FIELD: {"type": "string", "title": "Name it"}}
    assert schema["required"] == [ANSWER_FIELD]
    assert elicitation_request("s", ask(type="text", choices=[], allowEmpty=True))["requestedSchema"]["required"] == []


def test_the_question_answered_is_the_current_one_and_its_step_is_said() -> None:
    item = ask()
    item["questions"].append({"id": "q1", "text": "Which fruit?", "type": "text"})
    item["currentIndex"] = 1
    item["step"] = {"index": 1, "total": 2, "answered": [0]}
    params = elicitation_request("s", item)
    assert params["requestedSchema"]["properties"][ANSWER_FIELD]["title"] == "Which fruit?"
    assert "Question 2 of 2" in params["message"]
    item["currentIndex"] = 7
    assert elicitation_request("s", item)["requestedSchema"]["properties"][ANSWER_FIELD]["title"] == "Which colour?"


def test_an_interaction_with_no_question_still_asks_in_its_title() -> None:
    bare = {"promptId": "p", "kind": "user-input", "title": "Echo asks"}
    schema = elicitation_request("s", bare)["requestedSchema"]
    assert schema["properties"][ANSWER_FIELD]["title"] == "Echo asks"


def test_an_accepted_form_answers_with_the_choice_or_the_typed_text() -> None:
    item = ask()
    assert read_elicitation(item, {"action": "accept", "content": {ANSWER_FIELD: "Blue"}}) == Reply("select", "Blue")
    typed = {"action": "accept", "content": {ANSWER_FIELD: "Mauve?", CUSTOM_FIELD: "Mauve"}}
    assert read_elicitation(item, typed) == Reply("select", "Mauve")
    # A client that wrote its typed answer into the enum property is still believed.
    assert read_elicitation(item, {"action": "accept", "content": {ANSWER_FIELD: "Teal"}}) == Reply("select", "Teal")
    multi = ask(multiSelect=True)
    picked = {"action": "accept", "content": {ANSWER_FIELD: ["Red", " Blue "]}}
    assert read_elicitation(multi, picked) == Reply("select", "Red, Blue")
    text = ask(type="text", choices=None)
    assert read_elicitation(text, {"action": "accept", "content": {ANSWER_FIELD: 42}}) == Reply("text", "42")


def test_declining_skips_and_anything_else_cancels() -> None:
    item = ask()
    assert read_elicitation(item, {"action": "decline"}) == Reply("skip")
    assert read_elicitation(item, {"action": "accept", "content": {ANSWER_FIELD: "  "}}) == Reply("skip")
    assert read_elicitation(item, {"action": "accept", "content": {ANSWER_FIELD: True}}) == Reply("skip")
    assert read_elicitation(item, {"action": "accept", "content": "Red"}) == Reply("skip")
    assert read_elicitation(item, {"action": "cancel"}) == Reply("cancel")
    assert read_elicitation(item, None) == Reply("cancel")


def test_an_approval_is_a_permission_request_whose_options_are_mirasims_buttons() -> None:
    params, offered = permission_request("sess-1", approval())
    validate_def("RequestPermissionRequest", params)
    assert params["toolCall"]["title"] == 'Claude wants to use Bash: {"command": "ls"}'
    assert params["toolCall"]["status"] == "pending"
    kinds = {option["name"]: option["kind"] for option in params["options"]}
    assert kinds == {"Allow": "allow_once", "Deny": "reject_once"}
    assert sorted(offered.values()) == ["Allow", "Deny"]
    chosen = next(option_id for option_id, label in offered.items() if label == "Allow")
    assert read_permission({"outcome": {"outcome": "selected", "optionId": chosen}}, offered) == Reply(
        "select", "Allow"
    )


def test_an_approval_with_no_buttons_offers_allow_and_deny_and_a_stray_answer_cancels() -> None:
    bare = {**approval(), "questions": []}
    params, offered = permission_request("s", bare)
    assert [option["name"] for option in params["options"]] == ["Allow", "Deny"]
    assert read_permission({"outcome": {"outcome": "selected", "optionId": "made-up"}}, offered) == Reply("cancel")
    assert read_permission({"outcome": {"outcome": "cancelled"}}, offered) == Reply("cancel")
    assert read_permission(None, offered) == Reply("cancel")


def test_option_ids_are_minted_per_request() -> None:
    _, first = permission_request("s", approval())
    _, second = permission_request("s", approval())
    assert not set(first) & set(second)


def test_the_text_form_names_the_question_its_choices_and_its_hint() -> None:
    said = describe(ask())
    assert said == "Claude asks: Which colour? (Red / Blue)"
    assert describe(approval()).endswith("\n  Runs a shell command")
    assert describe({"promptId": "p", "kind": "user-input"}) == "a question"


def test_the_module_says_what_it_exports() -> None:
    assert set(interactions.__all__) >= {"Reply", "pending", "elicitation_request", "permission_request"}


@pytest.mark.parametrize("kind", ["single", "multi", "permission", "no_forms"])
async def test_relay_preserves_the_sending_agents_content(tmp_path, kind):
    from unittest.mock import Mock

    from raven.acp.capabilities import ClientCapabilities
    from raven.acp.mirasim.server import MirasimAgent, Session, _render_interactions
    from raven.acp.mirasim.store import BindingStore

    value = "sk-or-v1-" + "a" * 32
    item = approval() if kind == "permission" else ask(multiSelect=kind == "multi")
    item["title"] = f"Agent asks about {value}"
    item["hint"] = "https://agent.invalid/?token=agent-example"
    question = item["questions"][0]
    question["text"] = f"Use {value}?"
    question["choices"] = [{"label": value, "description": f"Keep {value}"}, {"label": "Deny"}]
    sent = []
    agent = MirasimAgent(agent="claude", cli=Mock(), store=BindingStore(tmp_path / "state.json"), emit=sent.append)
    if kind != "no_forms":
        agent.client = ClientCapabilities.from_params({"clientCapabilities": {"elicitation": {"form": {}}}})
    notes = []
    task = asyncio.create_task(agent._relay(Session(id="session", cwd=str(tmp_path)), item, notes))
    try:
        if kind == "no_forms":
            assert await task == Reply("skip")
            assert describe(item) in notes[0]
        else:
            async with asyncio.timeout(5):
                while not sent:
                    await asyncio.sleep(0)
            request = sent[0]
            params = request["params"]
            if kind == "permission":
                assert params["toolCall"]["title"] == question["text"]
                option = params["options"][0]
                assert option["name"] == value
                result = {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
            else:
                field = params["requestedSchema"]["properties"][ANSWER_FIELD]
                assert field["title"] == question["text"]
                assert item["title"] in params["message"] and item["hint"] in params["message"]
                assert question["choices"][0]["description"] in params["message"]
                choices = field["items"]["enum"] if kind == "multi" else field["enum"]
                assert choices == [value, "Deny"]
                result = {"action": "accept", "content": {ANSWER_FIELD: [value] if kind == "multi" else value}}
            agent.outbound.resolve({"jsonrpc": "2.0", "id": request["id"], "result": result})
            assert await task == Reply("select", value)
        assert describe(item) in _render_interactions({"interactions": [item]})
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.shutdown()
