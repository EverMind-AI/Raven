"""Turning a project into one a run can work in: the scaffolding, and the plan's shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.stint import bootstrap
from raven.stint import roster as roster_mod
from raven.stint.bootstrap import InitError

PRD = """# Arena

## 7. Acceptance gates

| # | name | criterion |
|---|------|-----------|
| 7.1 | clean_boot | it boots with an empty stderr |
| 7.2 | feel_log | every event carries a timestamp |
| 7.3 | input | the shot leaves within 60 ms |
"""


def _project(tmp_path: Path, *, docs: dict[str, str] | None = None) -> Path:
    project = tmp_path / "game"
    (project / "docs").mkdir(parents=True)
    for name, body in (docs or {"PRD-arena.md": PRD}).items():
        (project / "docs" / name).write_text(body, encoding="utf-8")
    (project / "README.md").write_text("# readme\n", encoding="utf-8")
    return project


def test_init_lays_out_the_directory_and_links_the_specification(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = bootstrap.init(project)

    home = project / ".stint"
    assert (home / "SPEC.md").is_symlink()
    assert (home / "SPEC.md").resolve() == (project / "docs" / "PRD-arena.md").resolve()
    for name in ("HUMAN_DECISIONS.md", "AGENT_DECISIONS.md", "FIXLOG.md", "PLAYBOOK.md"):
        assert (home / name).is_file(), name
    for role in ("planner", "developer", "qa"):
        assert (home / f"{role}.md").is_file(), role
    assert (project / "reports").is_dir()
    assert result.spec.endswith("PRD-arena.md")


def test_a_link_rather_than_a_copy_so_a_revision_is_one_edit(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bootstrap.init(project)
    (project / "docs" / "PRD-arena.md").write_text(PRD + "\n| 7.4 | new | added later |\n", encoding="utf-8")
    assert "7.4" in (project / ".stint" / "SPEC.md").read_text(encoding="utf-8")


def test_the_guard_files_it_writes_are_a_roster_the_runtime_can_read(tmp_path: Path) -> None:
    """The point of the exercise: init's output is the runtime's input, with nothing in between."""
    project = _project(tmp_path)
    bootstrap.init(project)

    roster = roster_mod.load(project)
    assert [role.name for role in roster.in_round()] == ["planner", "developer", "qa"]
    assert roster.get("developer").session == roster_mod.CONTINUE
    assert roster.get("planner").session == roster_mod.CONTINUE, "the Planner holds the whole project across rounds"
    assert roster.conflicts() == []
    assert roster.grade("planner", "reports/brief_01.md", round_index=1) == roster_mod.OWNS
    assert roster.grade("qa", "reports/brief_01.md", round_index=1) == roster_mod.NEVER


def test_no_slot_is_left_unfilled_in_a_generated_guard(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bootstrap.init(project)
    for role in ("planner", "developer", "qa"):
        assert "{{" not in (project / ".stint" / f"{role}.md").read_text(encoding="utf-8"), role


def test_the_enforcement_grade_is_the_operator_s_to_choose(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bootstrap.init(project, enforce_read="hard", enforce_write="soft")
    role = roster_mod.load(project).get("qa")
    assert role.enforce_read == roster_mod.HARD
    assert role.enforce_write == roster_mod.SOFT


def test_running_init_twice_keeps_what_a_person_edited(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bootstrap.init(project)
    edited = project / ".stint" / "planner.md"
    mine = edited.read_text(encoding="utf-8") + "\n## My own rule\n\nAlways ask me first.\n"
    edited.write_text(mine, encoding="utf-8")

    again = bootstrap.init(project)

    assert edited.read_text(encoding="utf-8") == mine
    assert ".stint/planner.md" in again.kept
    assert bootstrap.init(project, force=True) and "My own rule" not in edited.read_text(encoding="utf-8")


def test_several_candidate_specifications_are_reported_rather_than_guessed(tmp_path: Path) -> None:
    project = _project(tmp_path, docs={"PRD-a.md": PRD, "spec-b.md": PRD, "design-c.md": PRD})
    result = bootstrap.init(project)
    assert any("more than one document" in note for note in result.notes)


def test_a_project_with_no_specification_says_so(tmp_path: Path) -> None:
    project = tmp_path / "bare"
    project.mkdir()
    result = bootstrap.init(project)
    assert any("no specification document" in note for note in result.notes)
    assert not (project / ".stint" / "SPEC.md").exists()


def test_a_named_specification_that_is_not_there_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InitError):
        bootstrap.init(_project(tmp_path), spec=tmp_path / "nope.md")


def test_the_facts_section_is_drafted_for_a_person_to_check(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bootstrap.init(project)
    text = (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8")
    assert "## Facts about this project" in text
    assert "## Awaiting a person" in text
    assert "- Machine:" in text and "- Display:" in text
    # Not the game engine: the layout is what any multi-round project gets,
    # and a Godot line in every project's facts was H* showing through.
    assert "Godot" not in text


def test_a_freshly_initialised_project_is_one_step_from_ready(tmp_path: Path) -> None:
    """Everything but the plan, which is the step that needs a model and a person."""
    project = _project(tmp_path)
    bootstrap.init(project)
    reasons = roster_mod.preflight(project)
    assert len(reasons) == 1
    assert "raven playbook stint task add" in reasons[0]


# ------------------------------------------------------------------------- plan


def test_the_plan_prompt_names_the_three_files_it_works_between(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bootstrap.init(project)
    prompt = bootstrap.plan_prompt(project)
    assert str(project / ".stint" / "SPEC.md") in prompt
    assert str(project / ".stint" / "HUMAN_DECISIONS.md") in prompt
    assert str(project / ".stint" / "backlog.json") in prompt
    assert bootstrap.PLAN_SENTINEL in prompt
    assert "depends_on" in prompt and "never a round number" in prompt


def test_gates_are_read_off_the_specification_only_to_check_coverage(tmp_path: Path) -> None:
    assert bootstrap.gates_in(PRD) == ("7.1", "7.2", "7.3")


def test_review_reports_the_gates_no_task_covers(tmp_path: Path) -> None:
    """A gate nobody planned for is a requirement nobody planned for."""
    project = _project(tmp_path)
    bootstrap.init(project)
    (project / ".stint" / "backlog.json").write_text(
        json.dumps(
            {
                "meta": {},
                "tasks": [
                    {"id": 1, "title": "boot", "gates": ["7.1"], "state": "open"},
                    {"id": 2, "title": "log", "gates": ["7.2"], "state": "open"},
                ],
            }
        ),
        encoding="utf-8",
    )

    review = bootstrap.review(project)

    assert review.tasks == 2
    assert review.gates == ("7.1", "7.2")
    assert review.uncovered == ("7.3",)
    assert "not covered: 7.3" in review.render()


def test_the_documents_a_project_carried_are_found_at_any_depth_under_docs(tmp_path: Path) -> None:
    """A half-built project keeps what it learned in the role folders it grew up with."""
    project = _project(tmp_path, docs={"PRD-arena.md": PRD, "DECISIONS.md": "- settled: one boss\n"})
    (project / "docs" / "qa").mkdir()
    (project / "docs" / "qa" / "QA.md").write_text("# QA\n", encoding="utf-8")
    (project / "CLAUDE.md").write_text("# instructions\n", encoding="utf-8")
    (project / "AGENTS.md").symlink_to("CLAUDE.md")
    (project / "reports").mkdir()
    (project / "reports" / "qa_01.md").write_text("# not a document\n", encoding="utf-8")
    bootstrap.init(project)

    found = bootstrap.project_documents(project, spec=project / ".stint" / "SPEC.md")

    assert found == ["CLAUDE.md", "docs/DECISIONS.md", "docs/qa/QA.md"], (
        "the spec, README, .stint/ and reports/ are not documents to absorb"
    )


def test_the_plan_prompt_asks_for_the_carry_over_and_the_ledger(tmp_path: Path) -> None:
    project = _project(tmp_path, docs={"PRD-arena.md": PRD, "DECISIONS.md": "- one boss\n"})
    bootstrap.init(project)

    prompt = bootstrap.plan_prompt(project)

    assert "- `docs/DECISIONS.md`" in prompt
    assert str(project / ".stint" / "SOURCES.md") in prompt
    assert str(project / ".stint" / "PLAYBOOK.md") in prompt and "## Settled" in prompt
    assert "do not edit or delete them" in prompt
    assert "the reading map" in prompt, "reference material is mapped, not copied"
    assert "is **not**\n  copied" in prompt
    assert "a reference image" in prompt and "open the image itself" in prompt, (
        "a picture gets a row that sends the Developer and QA to look at it"
    )
    assert "a reference image" in prompt and "open the image itself" in prompt, (
        "a picture gets a row that sends the Developer and QA to look at it"
    )


def test_a_greenfield_project_is_told_there_is_nothing_to_carry(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "README.md").unlink()
    bootstrap.init(project)

    assert bootstrap.NO_DOCUMENTS in bootstrap.plan_prompt(project)


def test_review_names_the_documents_the_ledger_left_out(tmp_path: Path) -> None:
    project = _project(tmp_path, docs={"PRD-arena.md": PRD, "DECISIONS.md": "- one boss\n", "LESSONS.md": "- x\n"})
    bootstrap.init(project)
    (project / ".stint" / "backlog.json").write_text(
        json.dumps(
            {"meta": {}, "tasks": [{"id": 1, "title": "boot", "gates": ["7.1", "7.2", "7.3"], "state": "open"}]}
        ),
        encoding="utf-8",
    )

    assert bootstrap.review(project).unabsorbed == ("docs/DECISIONS.md", "docs/LESSONS.md")

    (project / ".stint" / "SOURCES.md").write_text(
        "| document | went to |\n| docs/DECISIONS.md | HUMAN_DECISIONS.md Settled |\n", encoding="utf-8"
    )
    plan = bootstrap.review(project)
    assert plan.unabsorbed == ("docs/LESSONS.md",)
    assert "not carried into .stint/" in plan.render() and "LESSONS.md" in plan.render()


def test_the_plan_is_told_what_the_tree_already_holds(tmp_path: Path) -> None:
    """The documents say what was meant; the tree says what is."""
    project = _project(tmp_path)
    (project / "project").mkdir()
    (project / "project" / "project.godot").write_text("[application]\n", encoding="utf-8")
    (project / "reports").mkdir()
    (project / "reports" / "qa_02.md").write_text("# qa 02\n", encoding="utf-8")
    bootstrap.init(project)

    prompt = bootstrap.plan_prompt(project)

    assert "## Then, look at the tree" in prompt
    assert "`project/`" in prompt and "`qa_02.md`" in prompt
    assert "checks detected: `import`" in prompt
    assert "look rather than assume" in prompt, "the layout is the repository's own, so the prompt guides a search"
    assert "Work that already is\n  goes in as `in_review`" in prompt
    assert "Nothing is done yet" not in prompt


def test_a_greenfield_tree_is_named_as_such(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bootstrap.init(project)
    prompt = bootstrap.plan_prompt(project)
    assert "none found -- this may be a greenfield project" in prompt and "Markdown under `reports/`: none" in prompt


def test_the_planner_is_asked_for_a_coherent_piece_not_a_count(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bootstrap.init(project)
    guard = (project / ".stint" / "planner.md").read_text(encoding="utf-8")
    assert "one coherent piece of work" in guard and "the number is not the point" in guard
    assert "At most three priorities" not in guard
