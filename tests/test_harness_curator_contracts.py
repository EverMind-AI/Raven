"""Authoring grants stay aligned across planning, schemas and native payload validation."""

from dataclasses import replace
from importlib import import_module

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, RootModel

from experimental.curator.harness import Artifact, Change, Declaration, Plan, StateUse, Target
from experimental.curator.harness.channels import execution_control, model_decision, model_input, tool_interaction
from experimental.curator.harness.declaration import parse_as, schema_for
from experimental.curator.raven_adapter.targets import catalogue
from raven.agent.harness.participants import read_intake, read_verdict
from raven.config.raven import ContextConfig
from raven.contracts.participant import Accept, End, Intake, Resample
from raven.plugins.manifest import Contributes


@pytest.fixture
def host():
    return Declaration("worker:revision-0", catalogue())


def plan_for(*names):
    return Plan(
        design="Invoke the selected native bindings and verify their observable effects.",
        understanding="Improve the current task using the granted native interfaces.",
        changes=tuple(
            Change(
                target=name,
                reason="Observed task need",
                expected="The proposed behavior",
                verification="Native evidence",
            )
            for name in names
        ),
    )


def validator(schema):
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_native_catalogue_covers_carriers_and_references_real_contracts(host):
    bindings = {target.binding for target in host.targets}
    native_contributions = set(Contributes.model_fields) - {"onboard"}
    assert {f"plugin.contributes.{name}" for name in native_contributions} <= bindings
    assert {"bootstrap_files", "skill_files", "config.tools.mcp_servers", "build_runtime.context_engine"} <= bindings
    assert {target.name.partition(".")[0] for target in host.targets} == {
        "memory",
        "planning",
        "capability",
        "action",
        "prompt",
    }
    for row in host.describe():
        module, name = row["contract"].split(":")
        referenced = import_module(module)
        for part in name.split("."):
            referenced = getattr(referenced, part)
        assert referenced is host.target(row["target"]).contract
        validator(row["schema"])
    # Protocol methods without a native generated-Participant binding are knowledge, not grants.
    for unavailable in ("action.judge", "memory.outbound", "action.module"):
        with pytest.raises(ValueError, match="not granted"):
            host.target(unavailable)


def test_model_plan_schema_and_host_reject_an_ungranted_target(host):
    scope = host.restrict(["planning.advise"])
    good = plan_for("planning.advise").model_dump(mode="json")
    assert validator(scope.plan_schema()).is_valid(good)
    assert scope.parse_plan(good).changes[0].target == "planning.advise"
    bad = plan_for("capability.tools").model_dump(mode="json")
    assert not validator(scope.plan_schema()).is_valid(bad)
    with pytest.raises(ValueError, match="not granted"):
        scope.parse_plan(bad)
    with pytest.raises(ValueError, match="not granted"):
        scope.artifact_schema(plan_for("capability.tools"))


def test_a_manual_can_only_remove_existing_targets_fields_and_phases(host):
    scope = host.restrict(
        ["action.config", "action.review"],
        fields={"action.config": ["temperature"]},
        phases={"action.review": ["after_iteration"]},
    )
    assert scope.target("action.review").phases == ("after_iteration",)
    assert scope.target("action.config").schema()["properties"].keys() == {"temperature"}
    with pytest.raises(ValueError, match="not granted"):
        scope.restrict(["capability.tools"])
    with pytest.raises(ValueError, match="cannot add fields"):
        scope.restrict(["action.config"], fields={"action.config": ["model"]})
    with pytest.raises(ValueError, match="cannot add phases"):
        scope.restrict(["action.review"], phases={"action.review": ["execute_tools"]})
    with pytest.raises(ValueError, match="selected targets"):
        scope.restrict(["action.config"], phases={"action.review": ["after_iteration"]})
    with pytest.raises(ValueError, match="phase not granted"):
        scope.target("action.review").parse_result(Accept(), phase="execute_tools")
    assert host.target("action.review").phases == ("execute_tools", "after_iteration")


def test_schema_projection_and_parser_apply_the_same_field_grant(host):
    scope = host.restrict(["action.config"], fields={"action.config": ["temperature"]})
    plan = plan_for("action.config")
    check = validator(scope.artifact_schema(plan))
    good = {"values": {"action.config": {"temperature": 0.4}}}
    check.validate(good)
    candidate = scope.accept(plan, good)
    assert candidate.artifact.values == good["values"]
    for fields in ({"model": "other"}, {"maxConcurrentSubagents": 2}, {"temperature": 0.4, "unknown": True}):
        bad = {"values": {"action.config": fields}}
        assert not check.is_valid(bad)
        with pytest.raises(ValueError):
            scope.accept(plan, bad)


def test_nested_unknown_fields_and_invalid_native_values_are_not_silently_dropped(host):
    scope = host.restrict(["capability.mcp"])
    plan = plan_for("capability.mcp")
    bad = {"values": {"capability.mcp": {"search": {"type": "unknown"}}}}
    check = validator(scope.artifact_schema(plan))
    assert not check.is_valid(bad)
    with pytest.raises(ValueError):
        scope.accept(plan, bad)
    bad = {"values": {"capability.mcp": {"search": {"oauth": {"unknown": True}}}}}
    assert not check.is_valid(bad)
    with pytest.raises(ValueError, match="extra"):
        scope.accept(plan, bad)


def test_mixed_artifacts_keep_only_explicit_config_values_and_do_not_execute_code(host, tmp_path):
    scope = host.restrict(["action.config", "memory.prompt", "planning.skills", "capability.tools"])
    plan = plan_for(*(target.name for target in scope.targets))
    marker = tmp_path / "must_not_exist"
    raw = {
        "values": {
            "action.config": {"temperature": 0.2},
            "memory.prompt": {"TOOLS.md": "Use the probe to obtain execution evidence."},
            "planning.skills": {
                "verification/SKILL.md": "---\nname: verification\ndescription: Verify results\n---\nRead evidence."
            },
            "capability.tools": [{"name": "probe", "factory": "probe:create"}],
        },
        "files": {"probe.py": f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"},
    }
    validator(scope.artifact_schema(plan)).validate(raw)
    candidate = scope.accept(plan, raw)
    assert candidate.artifact.values == raw["values"]
    assert candidate.artifact.values["action.config"] == {"temperature": 0.2}
    assert not marker.exists()
    raw["values"]["action.config"]["temperature"] = 0.9
    raw["files"]["probe.py"] = "modified"
    assert candidate.artifact.values["action.config"]["temperature"] == 0.2
    assert candidate.artifact.files["probe.py"] != "modified"


def test_existing_and_new_tool_factories_use_the_same_native_contract(host):
    scope = host.restrict(["capability.tools"])
    plan = plan_for("capability.tools")
    raw = {"values": {"capability.tools": [{"name": "existing", "factory": "installed_tools:make"}]}}
    assert scope.accept(plan, raw).artifact.files == {}
    raw["values"]["capability.tools"][0]["factory"] = "not-an-entrypoint"
    with pytest.raises(ValueError, match="factory"):
        scope.accept(plan, raw)


def test_context_backend_hooks_gates_and_services_share_one_combined_artifact(host):
    names = (
        "memory.context_engine",
        "memory.backends",
        "memory.session_observers",
        "action.hooks",
        "action.tool_gates",
        "action.services",
    )
    scope = host.restrict(names)
    values = {
        name: "components:context"
        if name == "memory.context_engine"
        else [{"name": name.rsplit(".", 1)[-1], "factory": "components:create"}]
        for name in names
    }
    raw = {"values": values}
    plan = plan_for(*names)
    validator(scope.artifact_schema(plan)).validate(raw)
    assert scope.accept(plan, raw).artifact.values == values


def test_selected_targets_and_implementation_payloads_cannot_diverge(host):
    plan = plan_for("planning.advise")
    for raw in (
        {"values": {}},
        {"values": {"planning.advise": "behavior:create", "action.review": "behavior:create"}},
        {"values": {"planning.advise": "behavior:create"}, "baseline": "forged"},
    ):
        assert not validator(host.artifact_schema(plan)).is_valid(raw)
        with pytest.raises(ValueError):
            host.accept(plan, raw)
    with pytest.raises(ValueError, match="occur once"):
        Plan(understanding="duplicate", design=plan.design, changes=plan.changes * 2)


def test_candidate_is_rechecked_after_baseline_or_manual_changes(host):
    plan = plan_for("action.config")
    candidate = host.accept(plan, {"values": {"action.config": {"temperature": 0.4}}})
    with pytest.raises(ValueError, match="baseline"):
        replace(host, baseline="worker:revision-1").validate(candidate)
    with pytest.raises(ValueError, match="contract"):
        host.restrict(["action.config"], fields={"action.config": ["model"]}).validate(candidate)
    assert host.validate(candidate).artifact.values == candidate.artifact.values


def test_no_change_plan_needs_no_payload_and_cannot_smuggle_files(host):
    scope = host.restrict([])
    plan = plan_for()
    raw = {"values": {}}
    validator(scope.plan_schema()).validate(plan.model_dump(mode="json"))
    validator(scope.artifact_schema(plan)).validate(raw)
    assert scope.accept(plan, raw).artifact.files == {}
    assert not validator(scope.plan_schema()).is_valid(plan_for("action.review").model_dump(mode="json"))
    unselected = {"values": {}, "files": {"unselected.py": "pass"}}
    assert not validator(scope.artifact_schema(plan)).is_valid(unselected)
    with pytest.raises(ValueError):
        scope.accept(plan, unselected)


@pytest.mark.parametrize(
    "path", ["../outside.py", "/tmp/outside.py", "a/../b.py", "./a.py", "a//b", "a/", "C:\\a.py", "bad\x00file"]
)
def test_artifacts_reject_paths_that_escape_or_alias_the_package(path):
    with pytest.raises(ValueError, match="relative POSIX"):
        Artifact(values={}, files={path: "content"})


def test_a_file_cannot_also_be_a_directory():
    with pytest.raises(ValueError, match="parent file"):
        Artifact(values={}, files={"package": "file", "package/module.py": "module"})


def test_channels_are_facets_of_the_same_granted_targets(host):
    review = host.target("action.review")
    assert {model_decision.NAME, tool_interaction.NAME, execution_control.NAME} <= set(review.channels)
    assert model_input.NAME not in review.channels
    scope = host.restrict(["action.review"], phases={"action.review": ["after_iteration"]})
    controlled = [target for target in scope.targets if execution_control.NAME in target.channels]
    assert len(controlled) == 1 and controlled[0] is scope.target("action.review")
    assert controlled[0].phases == ("after_iteration",)


@pytest.mark.parametrize(
    "answer",
    [
        Accept("checked"),
        Resample("missing evidence", inject=[{"role": "user", "content": "test first"}]),
        End("finished"),
    ],
)
def test_review_results_accept_native_builders_without_reinterpreting_their_meaning(host, answer):
    target = host.target("action.review")
    validator(target.describe()["result_schema"]).validate(answer)
    parsed = target.parse_result(answer, phase="after_iteration")
    assert read_verdict(parsed) == read_verdict(answer)
    assert target.parse_result(None, phase="after_iteration") is None


def test_intake_and_system_addendum_use_the_native_result_shape(host):
    answer = Intake("context", reply=None, note="loaded")
    for name, phase in (("memory.intake", "user_inbound"), ("memory.system_addendum", "iteration")):
        target = host.target(name)
        parsed = target.parse_result(answer, phase=phase)
        assert read_intake(parsed) == read_intake(answer)


@pytest.mark.parametrize(
    "answer",
    [
        {"verdict": "retry"},
        {"verdict": "resample", "inject": "not messages"},
        {"verdict": "accept", "unexpected": True},
    ],
)
def test_malformed_participant_results_are_errors_instead_of_silent_acceptance(host, answer):
    target = host.target("action.review")
    assert not validator(target.describe()["result_schema"]).is_valid(answer)
    with pytest.raises(ValueError):
        target.parse_result(answer, phase="after_iteration")


def test_tool_selection_is_not_artificially_restricted_to_the_previous_array(host):
    answer = [{"type": "function", "function": {"name": "newly_registered", "parameters": {"type": "object"}}}]
    assert host.target("capability.select_tools").parse_result(answer, phase="iteration") == answer


def test_state_descriptions_are_separate_from_runtime_storage():
    state = StateUse(
        resource="test evidence", scope="session", access="probe writes; review reads", lifecycle="clear at task end"
    )
    plan = Plan(understanding="Share evidence", state=(state,))
    assert Plan.model_validate_json(plan.model_dump_json()).state == (state,)
    with pytest.raises(ValueError, match="one description"):
        Plan(understanding="conflicting state", state=(state, state.model_copy(update={"scope": "turn"})))


def test_one_native_type_can_have_distinct_field_grants_without_schema_leakage():
    first = Target(
        "memory.first", ContextConfig, "first", ContextConfig, ("model_input",), "first", fields=("drop_segments",)
    )
    second = replace(first, name="memory.second", binding="second", fields=("fast_path_threshold",))
    scope = Declaration("same-type", (first, second))
    plan = plan_for(first.name, second.name)
    good = {"values": {"memory.first": {"drop_segments": []}, "memory.second": {"fast_path_threshold": 0.5}}}
    validator(scope.artifact_schema(plan)).validate(good)
    assert scope.accept(plan, good).artifact.values == good["values"]
    bad = {"values": {"memory.first": {"fast_path_threshold": 0.5}, "memory.second": {}}}
    assert not validator(scope.artifact_schema(plan)).is_valid(bad)
    with pytest.raises(ValueError, match="fields not granted"):
        scope.accept(plan, bad)


def test_required_native_fields_cannot_be_removed_and_native_constraints_still_apply():
    class Settings(BaseModel):
        needed: int = Field(ge=1)
        optional: str = ""

    target = Target("action.settings", Settings, "settings", Settings, ("model_decision",), "native settings")
    scope = Declaration("custom", (target,))
    with pytest.raises(ValueError, match="required"):
        scope.restrict(["action.settings"], fields={"action.settings": ["optional"]})
    plan = plan_for("action.settings")
    raw = {"values": {"action.settings": {"needed": 0}}}
    assert not validator(scope.artifact_schema(plan)).is_valid(raw)
    with pytest.raises(ValueError):
        scope.accept(plan, raw)


def test_root_mapping_schema_remains_open_to_its_native_keys():
    class Entries(RootModel[dict[str, int]]):
        pass

    schema = schema_for(Entries)
    validator(schema).validate({"a": 1})
    assert parse_as(Entries, {"a": 1}).root == {"a": 1}


def test_result_validation_preserves_native_reply_objects(host):
    reply = ("finished", [])
    answer = host.target("action.review").parse_result(End(reply), phase="after_iteration")
    assert isinstance(answer, dict)
    assert answer["reply"] is reply
    assert read_verdict(answer).reply is reply


def test_field_projection_removes_unused_native_definitions_from_model_materials(host):
    scope = host.restrict(["action.config"], fields={"action.config": ["temperature"]})
    assert "$defs" not in scope.target("action.config").schema()
    schema = scope.artifact_schema(plan_for("action.config"))
    assert "AgentDefaults" not in schema.get("$defs", {})
    assert "CompactionConfig" not in schema.get("$defs", {})
    validator(schema).validate({"values": {"action.config": {"temperature": 0.3}}})


def test_field_restriction_cannot_be_applied_to_a_free_form_resource_mapping(host):
    with pytest.raises(ValueError, match="named fields"):
        host.restrict(["planning.skills"], fields={"planning.skills": []})


def test_bootstrap_paths_follow_the_native_loader_in_schema_and_validation(host):
    scope = host.restrict(["memory.prompt"])
    plan = plan_for("memory.prompt")
    raw = {"values": {"memory.prompt": {"not-loaded.md": "must not be advertised as loaded"}}}
    assert not validator(scope.artifact_schema(plan)).is_valid(raw)
    with pytest.raises(ValueError, match="bootstrap"):
        scope.accept(plan, raw)


def test_candidates_cannot_be_rebound_under_different_phase_grants(host):
    restricted = host.restrict(["action.review"], phases={"action.review": ["after_iteration"]})
    candidate = restricted.accept(plan_for("action.review"), {"values": {"action.review": "checks:Participant"}})
    assert candidate.baseline == host.baseline
    with pytest.raises(ValueError, match="contract"):
        host.validate(candidate)
    assert restricted.validate(candidate).contract_id == candidate.contract_id


def test_misnested_selected_target_reports_the_required_artifact_location(host):
    plan = plan_for("action.config")
    value = {"temperature": 0.2}
    with pytest.raises(ValueError, match="selected and permitted") as failure:
        host.accept(plan, {"values": {}, "action.config": value})
    assert "values['action.config']" in str(failure.value)
    assert host.accept(plan, {"values": {"action.config": value}}).artifact.values == {"action.config": value}
    assert "inside this object" in host.artifact_schema(plan)["properties"]["values"]["description"]


def test_shared_observation_knowledge_is_complete_and_registered(host):
    from inspect import getsource

    from experimental.curator.raven_adapter.inspection.sources import native_sources
    from experimental.curator.raven_adapter.planning.contracts import PlanningObservation
    from experimental.curator.raven_adapter.targets.knowledge import complete
    from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
    from raven.contracts.participant import StepView

    sources = native_sources()
    for target in host.targets:
        if not {StepView, PlanningObservation}.intersection(target.knowledge):
            continue
        assert LLMResponse in target.knowledge and ToolCallRequest in target.knowledge
        assert complete(target.knowledge) == target.knowledge
        described = target.describe()["knowledge"]
        assert any(row["content"] == getsource(ToolCallRequest) for row in described)
        for index in range(len(target.knowledge)):
            assert f"{target.name}.knowledge.{index}" in sources
    assert not host.target("action.config").knowledge
    assert len(complete((StepView, StepView))) == len(complete((StepView,)))
