"""What waits on a person: asking a question, and reading it back."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from raven.stint import decisions as decisions_mod


def _project(tmp_path: Path, body: str = "") -> Path:
    project = tmp_path / "game"
    (project / ".stint").mkdir(parents=True)
    if body:
        (project / ".stint" / "HUMAN_DECISIONS.md").write_text(body, encoding="utf-8")
    return project


DOCUMENT = """# HUMAN_DECISIONS.md

## Awaiting a person

- [ ] Q01 (round 02) the duel's scoring rule | blocks: 7, 12
- [x] Q02 (round 01) whether it needs networking
- [ ] Q03 (round 03) which font licence applies
"""


def test_reading_gives_the_state_the_round_and_what_each_holds_up(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)
    questions = decisions_mod.read(project)
    assert [q.id for q in questions] == ["Q01", "Q02", "Q03"]
    assert questions[0].blocks == [7, 12]
    assert questions[0].round == 2
    assert [q.answered for q in questions] == [False, True, False]


def test_a_project_with_no_decisions_file_has_no_questions(tmp_path: Path) -> None:
    assert decisions_mod.read(_project(tmp_path)) == []


def test_asking_mints_the_next_id_and_keeps_the_others(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)
    question = decisions_mod.ask(project, "does the boss drop loot", round_index=4, blocks=[9])
    assert question.id == "Q04"
    again = decisions_mod.read(project)
    assert [q.id for q in again] == ["Q01", "Q02", "Q03", "Q04"]
    assert again[-1].blocks == [9]


def test_asking_a_project_that_has_no_file_yet_creates_one(tmp_path: Path) -> None:
    project = _project(tmp_path)
    decisions_mod.ask(project, "which engine", round_index=0)
    assert [q.id for q in decisions_mod.read(project)] == ["Q01"]


def test_a_provisional_ruling_rides_with_the_question_until_a_person_rules(tmp_path: Path) -> None:
    """Soft mode: the Planner decides and tells; the line carries both, and the ruling survives the answer."""
    project = tmp_path / "game"
    question = decisions_mod.ask(
        project, "which clock is canonical | really", round_index=3, provisional="the engine tick"
    )
    assert question.provisional == "the engine tick"
    read = decisions_mod.read(project)[0]
    assert (read.text, read.provisional, read.blocks) == ("which clock is canonical / really", "the engine tick", [])
    assert read.to_dict()["provisional"] == "the engine tick"

    document = project / ".stint" / "HUMAN_DECISIONS.md"
    document.write_text(document.read_text(encoding="utf-8").replace("- [ ]", "- [x]", 1), encoding="utf-8")
    after = decisions_mod.read(project)[0]
    assert after.answered and after.provisional == "the engine tick"


def test_a_question_with_blocks_and_a_ruling_parses_both(tmp_path: Path) -> None:
    project = tmp_path / "game"
    decisions_mod.ask(project, "the duel", round_index=1, blocks=[7, 12], provisional="two rounds")
    read = decisions_mod.read(project)[0]
    assert (read.blocks, read.provisional) == ([7, 12], "two rounds")


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 does not block root")
def test_a_decisions_file_that_cannot_be_read_is_no_questions_rather_than_a_crash(tmp_path: Path) -> None:
    """Every round opens by reading this file. One lost permission bit taking the
    whole run down would be a worse answer than an empty list."""
    project = _project(tmp_path, DOCUMENT)
    path = decisions_mod.decisions_path(project)
    path.chmod(0o000)
    try:
        assert decisions_mod.read(project) == []
        assert decisions_mod.pending(project) == []
    finally:
        path.chmod(0o600)
