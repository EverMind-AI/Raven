from __future__ import annotations

import json

import pytest

from raven.agent import workdir
from raven_design.task_state import TaskStateError, TaskStateManager
from raven_design.task_state.tool import TaskStateTool


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
    with workdir.bind(tmp_path):
        await tool.execute([{"operation": "initialize", "state": _state()}])

        result = await tool.execute(
            [{"operation": "complete", "item_number": 1}],
        )

    assert "1/2 completed" in result
    assert manager.get(str(tmp_path))["items"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_tool_rejects_unknown_top_level_fields(tmp_path):
    manager = TaskStateManager(tmp_path)
    tool = TaskStateTool(manager)
    with workdir.bind(tmp_path):
        result = await tool.execute(
            [{"operation": "initialize", "state": _state()}],
            unexpected="value",
        )

    assert result == "Error: Unknown Task State fields: ['unexpected']"
    assert manager.get(str(tmp_path)) is None


@pytest.mark.asyncio
async def test_undo_reverts_all_mutations_from_one_turn(tmp_path):
    manager = TaskStateManager(tmp_path)
    manager.apply("cli:one", [{"operation": "initialize", "state": _state()}])
    manager.apply(
        "cli:one",
        [{"operation": "update", "item_number": 1, "changes": {"status": "in_progress"}}],
        mutation_group="turn-one",
    )
    manager.apply(
        "cli:one",
        [{"operation": "update", "item_number": 2, "changes": {"status": "in_progress"}}],
        mutation_group="turn-one",
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
