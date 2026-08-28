from __future__ import annotations

import json

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.tools.task_state import TaskStateTool
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.task_state import TaskStateError, TaskStateManager


def _state() -> dict:
    return {
        "goal": "Write a report",
        "requirements": ["Keep it concise"],
        "items": [
            {
                "title": "Collect results",
                "requirements": ["Read all result files"],
            },
            {
                "title": "Write report",
                "requirements": ["Save the final report"],
            },
        ],
    }


def test_initialize_renders_visible_numbers_without_ids(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])

    rendered = manager.render("cli:one")

    assert "1. [pending] Collect results" in rendered
    assert "2. [pending] Write report" in rendered
    assert "item_number" in rendered
    assert "item_id" not in rendered


def test_item_number_updates_the_snapshot_seen_by_the_model(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])

    manager.apply(
        "cli:one",
        [{"operation": "update", "item_number": 2, "changes": {"status": "in_progress"}}],
    )

    assert manager.get("cli:one")["items"][1]["status"] == "in_progress"


def test_batch_operations_resolve_numbers_against_the_original_snapshot(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])

    manager.apply(
        "cli:one",
        [
            {"operation": "remove", "item_number": 1, "reason": "Merged into the report step."},
            {"operation": "complete", "item_number": 2},
        ],
    )

    state = manager.get("cli:one")
    assert len(state["items"]) == 1
    assert state["items"][0]["title"] == "Write report"
    assert state["items"][0]["status"] == "completed"


def test_failed_batch_does_not_write_partial_changes(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    before = manager.revision("cli:one")

    with pytest.raises(TaskStateError):
        manager.apply(
            "cli:one",
            [
                {"operation": "update", "item_number": 1, "changes": {"status": "in_progress"}},
                {"operation": "complete", "item_number": 99},
            ],
        )

    assert manager.revision("cli:one") == before
    assert manager.get("cli:one")["items"][0]["status"] == "pending"


def test_corrupt_storage_degrades_and_quarantines(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    path = manager.store._path("cli:one")
    path.write_text("{ not json", encoding="utf-8")

    assert manager.get("cli:one") is None
    assert manager.revision("cli:one") == 0
    assert manager.render("cli:one").startswith("<task_state>\nRevision: 0\nNot initialized")
    assert not path.exists()
    assert path.with_name(path.name + ".corrupt").exists()

    update = manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    assert update.revision == 1


def test_structurally_invalid_state_degrades_instead_of_raising(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    path = manager.store._path("cli:one")
    path.write_text(
        json.dumps({"version": 1, "revision": 3, "state": {"goal": "x"}}),
        encoding="utf-8",
    )

    assert manager.get("cli:one") is None
    assert manager.snapshot("cli:one") is None
    assert path.with_name(path.name + ".corrupt").exists()


def test_operation_rejects_unknown_fields_without_writing(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    before = manager.revision("cli:one")

    with pytest.raises(TaskStateError, match="Unknown operation fields"):
        manager.apply(
            "cli:one",
            [{"operation": "complete", "item_number": 1, "unexpected": "value"}],
        )

    assert manager.revision("cli:one") == before
    assert manager.get("cli:one")["items"][0]["status"] == "pending"


def test_initialize_accepts_placeholders_for_other_operation_shapes(tmp_path):
    manager = TaskStateManager(tmp_path)

    manager.apply(
        "cli:one",
        [
            {
                "operation": "initialize",
                "state": _state(),
                "changes": {
                    "goal": "",
                    "requirements": [],
                    "status": "pending",
                    "title": "",
                },
                "item": {
                    "requirements": [],
                    "status": "pending",
                    "title": "",
                },
                "item_number": 1,
                "reason": "",
                "target": "task_state",
            }
        ],
    )

    assert manager.get("cli:one")["goal"] == "Write a report"


def test_update_ignores_explicit_null_placeholders(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])

    manager.apply(
        "cli:one",
        [
            {
                "operation": "update",
                "target": "task_state",
                "changes": {"goal": "Updated goal", "requirements": None},
            },
            {
                "operation": "update",
                "target": "item",
                "item_number": 1,
                "changes": {
                    "title": "Updated item",
                    "status": "in_progress",
                    "requirements": None,
                },
            },
        ],
    )

    state = manager.get("cli:one")
    assert state["goal"] == "Updated goal"
    assert state["requirements"] == ["Keep it concise"]
    assert state["items"][0] == {
        "title": "Updated item",
        "status": "in_progress",
        "requirements": ["Read all result files"],
    }


def test_tool_schema_separates_operation_shapes(tmp_path):
    schema = TaskStateTool(TaskStateManager(tmp_path)).parameters
    variants = schema["properties"]["operations"]["items"]["oneOf"]

    assert [variant["properties"]["operation"]["enum"][0] for variant in variants] == [
        "initialize",
        "add",
        "update",
        "update",
        "remove",
        "complete",
    ]
    assert set(variants[2]["properties"]) == {"operation", "target", "changes"}
    assert set(variants[3]["properties"]) == {
        "operation",
        "target",
        "item_number",
        "changes",
    }


def test_initialize_replaces_the_whole_state(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    replacement = {
        "goal": "New goal",
        "requirements": [],
        "items": [{"title": "Only step", "status": "pending", "requirements": []}],
    }

    manager.apply("cli:one", [{"operation": "initialize", "state": replacement}])

    assert manager.get("cli:one") == replacement


def test_initialize_cannot_be_mixed_with_incremental_operations(tmp_path):
    manager = TaskStateManager(tmp_path)

    with pytest.raises(TaskStateError, match="must be called alone"):
        manager.apply(
            "cli:one",
            [
                {"operation": "initialize", "state": _state()},
                {"operation": "add", "item": {"title": "Extra"}},
            ],
        )


@pytest.mark.asyncio
async def test_tool_uses_the_visible_item_number(tmp_path):
    manager = TaskStateManager(tmp_path)
    tool = TaskStateTool(manager)
    tool.set_context("cli:one")
    await tool.execute([{"operation": "initialize", "state": _state()}])

    result = await tool.execute(
        [{"operation": "complete", "item_number": 1}],
    )

    assert "1/2 completed" in result
    assert manager.get("cli:one")["items"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_tool_rejects_unknown_top_level_fields(tmp_path):
    manager = TaskStateManager(tmp_path)
    tool = TaskStateTool(manager)
    tool.set_context("cli:one")

    result = await tool.execute(
        [{"operation": "initialize", "state": _state()}],
        unexpected="value",
    )

    assert result == "Error: Unknown Task State fields: ['unexpected']"
    assert manager.get("cli:one") is None


@pytest.mark.asyncio
async def test_undo_reverts_all_mutations_from_one_turn(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    tool = TaskStateTool(manager)
    tool.set_context("cli:one", mutation_group="turn-one")

    await tool.execute(
        [{"operation": "update", "item_number": 1, "changes": {"status": "in_progress"}}],
    )
    await tool.execute(
        [{"operation": "update", "item_number": 2, "changes": {"status": "in_progress"}}],
    )
    manager.undo("cli:one")

    assert [item["status"] for item in manager.get("cli:one")["items"]] == ["pending", "pending"]


def test_undo_tracks_a_turn_that_did_not_mutate_task_state(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])

    manager.begin_turn("cli:one", "turn-one")
    manager.apply(
        "cli:one",
        [{"operation": "update", "item_number": 1, "changes": {"status": "in_progress"}}],
        mutation_group="turn-one",
    )
    manager.begin_turn("cli:one", "turn-two")

    manager.undo("cli:one")

    assert manager.get("cli:one")["items"][0]["status"] == "in_progress"


def test_undo_can_restore_an_uninitialized_state(tmp_path):
    manager = TaskStateManager(tmp_path)

    manager.begin_turn("cli:one", "turn-one")
    manager.apply(
        "cli:one",
        [{"operation": "initialize", "state": _state()}],
        mutation_group="turn-one",
    )

    manager.undo("cli:one")

    assert manager.get("cli:one") is None


def test_initialize_without_a_turn_group_is_also_undoable(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])

    manager.undo("cli:one")

    assert manager.get("cli:one") is None


def test_copy_preserves_rollback_history(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    manager.apply(
        "cli:one",
        [{"operation": "update", "item_number": 1, "changes": {"status": "in_progress"}}],
    )

    manager.copy("cli:one", "cli:branch")
    manager.undo("cli:branch")

    assert manager.get("cli:branch")["items"][0]["status"] == "pending"
    assert manager.get("cli:one")["items"][0]["status"] == "in_progress"


def test_state_file_is_json_and_persistent(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])

    payload = json.loads(next((tmp_path / "task_states").glob("*.json")).read_text())

    assert payload["revision"] == 1
    assert payload["state"]["goal"] == "Write a report"


def test_unicode_session_key_uses_a_portable_bounded_filename(tmp_path):
    manager = TaskStateManager(tmp_path)
    session_key = f"channel:{'任务🚀' * 100}"

    manager.apply(session_key, [{"operation": "initialize", "state": _state()}])

    path = next((tmp_path / "task_states").glob("*.json"))
    assert len(path.name.encode()) <= 255
    assert manager.get(session_key)["goal"] == "Write a report"


def test_initialize_can_replace_with_no_items(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply(
        "cli:one",
        [{"operation": "initialize", "state": {"goal": "Answer one question", "requirements": [], "items": []}}],
    )

    assert manager.get("cli:one")["items"] == []
    assert manager.unfinished_numbers("cli:one") == []


def test_undo_restores_the_previous_state(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    manager.apply(
        "cli:one",
        [{"operation": "update", "item_number": 1, "changes": {"status": "in_progress"}}],
    )

    manager.undo("cli:one")

    assert manager.get("cli:one")["items"][0]["status"] == "pending"


def test_snapshot_contains_only_visible_item_numbers(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])

    snapshot = manager.snapshot("cli:one")

    assert snapshot["items"][0]["item_number"] == 1
    assert "item_id" not in snapshot["items"][0]


def test_projection_appends_a_transient_tail_without_mutating_history(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    loop = object.__new__(AgentLoop)
    loop.task_state_enabled = True
    loop.task_state = manager
    messages = [
        {"role": "system", "content": "System instructions"},
        {"role": "user", "content": "Write the report"},
    ]

    projected = loop._project_task_state(messages, "cli:one")

    assert messages == [
        {"role": "system", "content": "System instructions"},
        {"role": "user", "content": "Write the report"},
    ]
    assert projected[:-1] == messages
    assert projected[-1]["role"] == "assistant"
    assert projected[-1]["content"].startswith("<task_state>")
    assert projected[-1]["content"].endswith("</task_state>")


def test_projection_replaces_an_existing_transient_tail(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    loop = object.__new__(AgentLoop)
    loop.task_state_enabled = True
    loop.task_state = manager
    messages = [
        {"role": "system", "content": "System instructions"},
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": "Skills and memory",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {"role": "user", "content": "Write the report"},
    ]

    projected = loop._project_task_state(messages, "cli:one")
    manager.apply(
        "cli:one",
        [{"operation": "update", "target": "task_state", "changes": {"goal": "Updated report"}}],
    )
    refreshed = loop._project_task_state(projected, "cli:one")

    assert projected[:-1] == messages
    assert refreshed[:-1] == messages
    state_messages = [message for message in refreshed if loop._is_task_state_projection(message)]
    assert len(state_messages) == 1
    assert "Goal: Updated report" in state_messages[0]["content"]


class _TaskStateToolBatchProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls: list[list[dict]] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="state-1",
                        name="update_task_state",
                        arguments={
                            "operations": [
                                {
                                    "operation": "update",
                                    "target": "item",
                                    "item_number": 1,
                                    "changes": {"status": "in_progress"},
                                }
                            ]
                        },
                    ),
                    ToolCallRequest(
                        id="state-2",
                        name="update_task_state",
                        arguments={
                            "operations": [
                                {
                                    "operation": "update",
                                    "target": "item",
                                    "item_number": 2,
                                    "changes": {"status": "in_progress"},
                                }
                            ]
                        },
                    ),
                ],
            )
        return LLMResponse(content="Finished.", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_tool_batch_gets_one_fresh_task_state_after_the_last_result(tmp_path):
    provider = _TaskStateToolBatchProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        max_iterations=3,
        restrict_to_workspace=True,
    )
    loop.task_state.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    loop._set_tool_context("cli", "one", session_key="cli:one")
    initial_messages = [
        {"role": "system", "content": "Original system"},
        {"role": "user", "content": "Runtime metadata\n\nWrite"},
    ]

    try:
        _, _, persisted, _ = await loop._run_agent_loop(
            initial_messages,
            task_state_session_key="cli:one",
        )

        assert len(provider.calls) == 2
        first_call = provider.calls[0]
        second_call = provider.calls[1]
        assert second_call[: len(first_call)] == first_call
        assert first_call[:2] == initial_messages
        assert [message["role"] for message in second_call[-4:]] == ["assistant", "tool", "tool", "assistant"]
        state_messages = [message for message in second_call if loop._is_task_state_projection(message)]
        assert len(state_messages) == 2
        assert "Revision: 1" in state_messages[0]["content"]
        assert "1. [pending] Collect results" in state_messages[0]["content"]
        assert "Revision: 3" in state_messages[1]["content"]
        assert "1. [in_progress] Collect results" in state_messages[1]["content"]
        assert "2. [in_progress] Write report" in state_messages[1]["content"]
        assert persisted[:2] == initial_messages
        assert not any(loop._is_task_state_projection(message) for message in persisted)
    finally:
        loop.context.skills.stop_file_watcher()


class _ReadOnlyToolProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCallRequest(id="list-1", name="list_dir", arguments={"path": "."})],
            )
        return LLMResponse(content="Finished.", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_unchanged_task_state_is_not_repeated_after_a_tool_result(tmp_path):
    provider = _ReadOnlyToolProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        max_iterations=3,
        restrict_to_workspace=True,
    )
    loop.task_state.apply("cli:one", [{"operation": "initialize", "state": _state()}])

    try:
        await loop._run_agent_loop(
            [{"role": "system", "content": "System"}, {"role": "user", "content": "Inspect"}],
            task_state_session_key="cli:one",
        )

        first_call, second_call = provider.calls
        assert second_call[: len(first_call)] == first_call
        state_messages = [message for message in second_call if loop._is_task_state_projection(message)]
        assert len(state_messages) == 1
        assert "Revision: 1" in state_messages[0]["content"]
    finally:
        loop.context.skills.stop_file_watcher()


class _PrematureCompletionProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls: list[list[dict]] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls.append([dict(message) for message in messages])
        return LLMResponse(content="Finished too early.", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_unfinished_state_is_reported_without_forcing_an_extra_model_call(tmp_path):
    provider = _PrematureCompletionProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        max_iterations=5,
        restrict_to_workspace=True,
    )
    loop.task_state.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    loop._set_tool_context("cli", "one", session_key="cli:one")

    streamed: list[str] = []

    async def on_token(delta: str) -> None:
        streamed.append(delta)

    try:
        final, _, messages, _ = await loop._run_agent_loop(
            [{"role": "system", "content": "System"}, {"role": "user", "content": "Write"}],
            task_state_session_key="cli:one",
            on_token_delta=on_token,
        )

        assert final.startswith("Finished too early.")
        assert "[Task State] This task is not complete;" in final
        assert "".join(streamed) == final
        assert len(provider.calls) == 1
        assert messages[-1]["content"] == "Finished too early."
        assert "[Task State]" not in messages[-1]["content"]
        assert all(item["status"] == "pending" for item in loop.task_state.get("cli:one")["items"])
    finally:
        loop.context.skills.stop_file_watcher()


@pytest.mark.asyncio
async def test_synthesis_places_task_state_before_the_synthesis_user_message(tmp_path):
    provider = _PrematureCompletionProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        max_iterations=5,
        restrict_to_workspace=True,
    )
    loop.task_state.apply("cli:one", [{"operation": "initialize", "state": _state()}])

    try:
        await loop._synthesize_final_on_exhaustion(
            [{"role": "system", "content": "System"}, {"role": "user", "content": "Write"}],
            model="stub",
            fallback_models=None,
            task_state_session_key="cli:one",
        )

        call = provider.calls[0]
        assert call[-2]["role"] == "assistant"
        assert call[-2]["content"].startswith("<task_state>")
        assert call[-1]["role"] == "user"
        assert "used up the tool-calling budget" in call[-1]["content"]
    finally:
        loop.context.skills.stop_file_watcher()


class _WaitingCompletionProvider(LLMProvider):
    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="Everything is finished.", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


def test_feature_switch_removes_the_tool_and_projection(tmp_path):
    loop = AgentLoop(
        provider=_WaitingCompletionProvider(api_key="test"),
        workspace=tmp_path,
        model="stub",
        restrict_to_workspace=True,
        task_state_enabled=False,
    )
    messages = [{"role": "system", "content": "System"}]

    try:
        assert loop.tools.get("update_task_state") is None
        assert loop._project_task_state(messages, "cli:one") is messages
    finally:
        loop.context.skills.stop_file_watcher()


@pytest.mark.asyncio
async def test_waiting_state_notice_is_streamed_but_not_persisted(tmp_path):
    loop = AgentLoop(
        provider=_WaitingCompletionProvider(api_key="test"),
        workspace=tmp_path,
        model="stub",
        restrict_to_workspace=True,
    )
    state = _state()
    state["items"][0]["status"] = "waiting"
    state["items"][1]["status"] = "completed"
    loop.task_state.apply("cli:one", [{"operation": "initialize", "state": state}])
    streamed: list[str] = []

    async def on_token(delta: str) -> None:
        streamed.append(delta)

    try:
        final, _, messages, _ = await loop._run_agent_loop(
            [{"role": "system", "content": "System"}, {"role": "user", "content": "Write"}],
            task_state_session_key="cli:one",
            on_token_delta=on_token,
        )

        assert final.startswith("Everything is finished.")
        assert "[Task State] This task is not complete;" in final
        assert "".join(streamed) == final
        assert messages[-1]["content"] == "Everything is finished."
    finally:
        loop.context.skills.stop_file_watcher()
