"""One effective mechanism view serves responsibilities, channels and host facts."""

from pathlib import Path

import pytest

from experimental.curator.harness import Artifact, Declaration
from experimental.curator.harness.view import project, validate
from experimental.curator.raven_adapter.inspection import Inspection, file_source
from experimental.curator.raven_adapter.inspection.mechanisms import describe
from experimental.curator.raven_adapter.materialize import extend_artifact
from experimental.curator.raven_adapter.targets import catalogue
from tests.test_harness_curator_generation import plan


def test_generated_mechanism_roles_come_from_contracts_and_do_not_expand_grants():
    declaration = Declaration("current", catalogue()).restrict(["planning.skills"])
    facts = {"authored": {"values": {"planning.skills": {"guide/SKILL.md": "guide"}}}}
    sources = {"planning.skills": {"path": "unused"}}
    mechanisms = describe(facts, sources, declaration)
    assert len(mechanisms) == 1 and mechanisms[0].roles == ("capability",)
    assert mechanisms[0].targets == ("planning.skills",)
    assert project(mechanisms, role="planning") == ()
    assert project(mechanisms, channel="model_input") == mechanisms
    validate(mechanisms, facts, sources, declaration)
    reduced = Inspection.restore(
        {
            "identity": "current",
            "unavailable": {},
            "facts": {**facts, "mechanisms": [m.model_dump() for m in mechanisms]},
            "sources": sources,
        },
        names=[],
    )
    assert reduced.mechanisms[0].targets == ()
    assert reduced.mechanisms[0].roles == ("capability",)


def test_unknown_hooks_remain_visible_without_inventing_responsibilities():
    scope = Declaration("current", catalogue())
    facts = {"hooks": [{"name": "mystery", "type": "unknown.Hook"}]}
    mechanism = describe(facts, {}, scope)[0]
    assert mechanism.roles == () and mechanism.gaps
    assert mechanism.components == (("hooks", 0),)
    assert not mechanism.targets


def test_explicit_retirement_is_checked_and_omission_preserves_bindings():
    scope = Declaration("current", catalogue())
    active = Artifact(values={"action.config": {"temperature": 0.2}, "memory.prompt": {"TOOLS.md": "guide"}})
    keep = scope.accept(plan("action.config"), {"values": {"action.config": {"temperature": 0.4}}})
    assert extend_artifact(active, keep.artifact).values["memory.prompt"] == {"TOOLS.md": "guide"}
    removal = scope.accept(plan("memory.prompt"), {"values": {}, "remove": ["memory.prompt"]})
    assert extend_artifact(active, removal.artifact).values == {"action.config": {"temperature": 0.2}}
    for payload in (
        {"values": {}},
        {"values": {}, "remove": ["action.config"]},
        {"values": {"memory.prompt": {}}, "remove": ["memory.prompt"]},
    ):
        with pytest.raises(ValueError):
            scope.accept(plan("memory.prompt"), payload)
    with pytest.raises(ValueError, match="not currently authored"):
        extend_artifact(Artifact(values={}), removal.artifact)


def test_component_and_source_references_are_validated():
    scope = Declaration("current", catalogue())
    facts = {"native_modules": {"planning": "NativePlanning"}}
    mechanism = describe(facts, {"instance.planning": {}}, scope)[0]
    with pytest.raises(KeyError):
        validate((mechanism,), {}, {"instance.planning": {}}, scope)
    with pytest.raises(ValueError, match="unknown mechanism source"):
        validate((mechanism,), facts, {}, scope)


@pytest.mark.asyncio
async def test_registered_and_searchable_materials_share_external_package_scope(tmp_path):
    from experimental.curator.raven_adapter.exploration import Exploration
    from raven.config.schema import Config

    repo = tmp_path / "repo"
    repo.mkdir()
    package = tmp_path / "plugin"
    package.mkdir()
    entry = package / "entry.py"
    entry.write_text("from helper import evidence\n")
    (package / "helper.py").write_text("evidence = 'FOUND_HELPER'\n")
    (package / ".env").write_text("PRIVATE=not-for-context\n")
    source = {**file_source(entry), "root": str(package)}
    inspection = Inspection(Declaration("baseline", catalogue()), {}, {"entry": source})
    async with Exploration(Config(), inspection, repository=repo, source_paths=()) as exploration:
        registered = exploration.read_source(name="entry")
        copied = exploration.sources["entry"]["snapshot_path"]
        assert registered["path"] == copied
        result = await exploration.registry.execute(
            "grep", {"pattern": "FOUND_HELPER", "path": str(Path(copied).parent)}
        )
        assert "helper.py" in result
        assert not (Path(copied).parent / ".env").exists()
        (package / "helper.py").write_text("evidence = 'changed'\n")
        with pytest.raises(ValueError, match="changed"):
            exploration.verify()


def test_retirement_cannot_reset_fields_outside_the_current_grant(tmp_path):
    from experimental.curator.raven_adapter.baselines import Baseline
    from experimental.curator.raven_adapter.worker import Worker
    from raven.config.raven import RavenConfig
    from raven.config.schema import Config

    worker = Worker(Baseline(Config(), RavenConfig(), tmp_path), tmp_path / "worker")
    worker.artifact = Artifact(values={"action.config": {"temperature": 0.2, "max_tool_iterations": 8}})
    scope = Declaration("current", catalogue()).restrict(["action.config"], fields={"action.config": ["temperature"]})
    candidate = scope.accept(plan("action.config"), {"values": {}, "remove": ["action.config"]})
    with pytest.raises(ValueError, match="fields not granted"):
        worker._accept(candidate, Inspection(scope, {}, {}))
    assert worker.artifact.values["action.config"]["max_tool_iterations"] == 8


def test_existing_strategy_state_is_visible_even_when_its_modification_is_not_granted():
    from experimental.curator.generation.context.collect import collect
    from experimental.curator.generation.stages.understand import materials

    context = collect(
        "Adjust behavior",
        Declaration("current", catalogue()).restrict(["action.config"]),
        facts={"planning": {"state": {"progress": "retained"}}},
        sources={},
        read_source=lambda **kwargs: {},
    )
    assert materials(context)["worker"]["planning"]["state"]["progress"] == "retained"
    assert [target.name for target in context.declaration.targets] == ["action.config"]


def test_prepared_config_uses_the_same_declared_mode_resolution_as_agent_preparation(tmp_path):
    import json

    from experimental.curator.raven_adapter.baselines import prepare

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "acp": {
                    "defaultMode": "review",
                    "modes": {
                        "review": {"name": "Review", "reasoningEffort": "low"},
                        "deep": {"name": "Deep", "reasoningEffort": "high"},
                    },
                }
            }
        )
    )
    assert prepare(path, workdir=tmp_path).mode == "review"
    assert prepare(path, workdir=tmp_path, mode="deep").mode == "deep"
    with pytest.raises(ValueError, match="mode is not declared"):
        prepare(path, workdir=tmp_path, mode="missing")
