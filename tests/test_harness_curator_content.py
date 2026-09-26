"""An authored file edited outside curation belongs to its editor: the host stops authoring it and never writes over it."""

from types import SimpleNamespace

import pytest

from experimental.curator.harness import Artifact
from experimental.curator.raven_adapter.baselines import Baseline
from experimental.curator.raven_adapter.worker import Worker
from raven.config.raven import RavenConfig
from raven.config.schema import Config

DECLARATION = SimpleNamespace(target=lambda name: SimpleNamespace(binding="skill_files"))
SKILLS = {"price-list/SKILL.md": "placeholder", "scripts/SKILL.md": "scripts"}


def worker_with_authored_skills(tmp_path):
    home = tmp_path / "home"
    config = Config.model_validate({"agents": {"defaults": {"workspace": str(home)}}})
    worker = Worker(Baseline(config, RavenConfig(), tmp_path), tmp_path / "worker")
    worker.artifact = Artifact(values={"planning.skills": dict(SKILLS)})
    for relative, text in SKILLS.items():
        path = home / "skills" / relative
        path.parent.mkdir(parents=True)
        path.write_text(text)
        worker._content_baseline[path] = (None, 0)
    return worker, home / "skills" / "price-list" / "SKILL.md"


def test_untouched_authored_files_stay_authored_and_can_be_retired(tmp_path):
    worker, price = worker_with_authored_skills(tmp_path)
    assert worker._release_edited(Artifact(values={}), worker.artifact, DECLARATION) == worker.artifact
    assert worker._retired_content(Artifact(values={}), DECLARATION) == {
        price: (None, 0),
        price.parent.parent / "scripts" / "SKILL.md": (None, 0),
    }


def test_a_file_the_owner_edited_is_released_from_a_carried_over_binding(tmp_path):
    worker, price = worker_with_authored_skills(tmp_path)
    price.write_text("the agency's real price list")
    released = worker._release_edited(Artifact(values={}), worker.artifact, DECLARATION)
    assert released.values == {"planning.skills": {"scripts/SKILL.md": "scripts"}}
    assert worker._retired_content(released, DECLARATION) == {}
    assert price.read_text() == "the agency's real price list"


def test_submitting_content_for_a_file_the_owner_edited_is_refused(tmp_path):
    worker, price = worker_with_authored_skills(tmp_path)
    price.write_text("the agency's real price list")
    for content in ("placeholder v2", "the agency's real price list"):
        submitted = Artifact(values={"planning.skills": {"price-list/SKILL.md": content}})
        with pytest.raises(ValueError, match="now belongs to its editor; leave it out of the candidate"):
            worker._release_edited(submitted, worker.artifact, DECLARATION)


def test_playbook_files_are_written_under_the_agent_home_playbooks_folder(tmp_path):
    from experimental.curator.harness import Artifact, Declaration
    from experimental.curator.raven_adapter.materialize import content_updates
    from experimental.curator.raven_adapter.targets import catalogue

    declaration = Declaration(
        "baseline", tuple(target for target in catalogue() if target.name == "planning.playbooks")
    )
    artifact = Artifact(values={"planning.playbooks": {"plan-delivery/playbook.md": "spec"}}, files={})
    assert content_updates(tmp_path, artifact, declaration) == {
        tmp_path / "playbooks" / "plan-delivery" / "playbook.md": "spec"
    }


def test_the_host_checks_each_authored_playbook_was_loaded():
    from experimental.curator.raven_adapter.bind import _verify_playbooks

    runtime = SimpleNamespace(loop=SimpleNamespace(_playbooks=SimpleNamespace(names=lambda: ["plan-delivery"])))
    _verify_playbooks(runtime, Artifact(values={"planning.playbooks": {"plan-delivery/playbook.md": "spec"}}, files={}))
    with pytest.raises(ValueError, match="was not loaded"):
        _verify_playbooks(runtime, Artifact(values={"planning.playbooks": {"other/playbook.md": "spec"}}, files={}))
    with pytest.raises(ValueError, match="<name>/playbook.md"):
        _verify_playbooks(runtime, Artifact(values={"planning.playbooks": {"plan-delivery/notes.md": "x"}}, files={}))
