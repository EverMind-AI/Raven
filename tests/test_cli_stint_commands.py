"""``raven playbook stint``: the backlog its roles share, the questions, and the confirm."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.stint_commands import stint_app

runner = CliRunner()


def _backlog_project(tmp_path, tasks):
    project = tmp_path / "game"
    (project / ".stint").mkdir(parents=True, exist_ok=True)
    (project / ".stint" / "backlog.json").write_text(json.dumps({"meta": {}, "tasks": tasks}), encoding="utf-8")
    return project


def test_task_commands_walk_a_task_through_the_three_roles(tmp_path, monkeypatch) -> None:
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "open"}])
    monkeypatch.setenv("STINT_ROUND", "4")

    for role, args in (
        ("planner", ["task", "assign", "1"]),
        ("builder", ["task", "implement", "1", "--commit", "abc1234"]),
        ("verifier", ["task", "verdict", "1", "--proven", "--evidence", "demo/round_04/"]),
    ):
        monkeypatch.setenv("STINT_ROLE", role)
        result = runner.invoke(stint_app, [*args, "--project", str(project)])
        assert result.exit_code == 0, result.output

    written = json.loads((project / ".stint" / "backlog.json").read_text(encoding="utf-8"))
    task = written["tasks"][0]
    assert task["state"] == "done"
    assert task["implements"][0]["commit"] == "abc1234"
    assert task["history"][-1]["round"] == 4


def test_a_role_calling_a_transition_it_does_not_own_is_refused(tmp_path, monkeypatch) -> None:
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "in_review"}])
    monkeypatch.setenv("STINT_ROLE", "builder")

    result = runner.invoke(
        stint_app, ["task", "verdict", "1", "--proven", "--evidence", "x", "--project", str(project)]
    )

    assert result.exit_code == 1
    assert "may not" in result.output
    assert json.loads((project / ".stint" / "backlog.json").read_text())["tasks"][0]["state"] == "in_review"


def test_every_attempt_lands_in_the_run_s_task_log(tmp_path, monkeypatch) -> None:
    """The refusals are the point: they leave no trace in the backlog itself."""
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "open"}])
    state_dir = tmp_path / "run"
    monkeypatch.setenv("STINT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("STINT_ROUND", "2")

    monkeypatch.setenv("STINT_ROLE", "verifier")
    runner.invoke(stint_app, ["task", "assign", "1", "--project", str(project)])
    monkeypatch.setenv("STINT_ROLE", "planner")
    runner.invoke(stint_app, ["task", "assign", "1", "--project", str(project)])

    lines = [json.loads(line) for line in (state_dir / "tasks.log").read_text(encoding="utf-8").splitlines()]
    assert [entry["role"] for entry in lines] == ["verifier", "planner"]
    assert lines[0]["outcome"].startswith("refused:")
    assert lines[1]["outcome"] == "assigned"
    assert lines[1]["round"] == 2


def test_the_verdict_needs_exactly_one_of_proven_or_not_proven(tmp_path, monkeypatch) -> None:
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "in_review"}])
    monkeypatch.setenv("STINT_ROLE", "verifier")
    for extra in ([], ["--proven", "--not-proven"]):
        result = runner.invoke(stint_app, ["task", "verdict", "1", *extra, "--project", str(project)])
        assert result.exit_code != 0


def test_listing_shows_only_what_can_be_started(tmp_path) -> None:
    project = _backlog_project(
        tmp_path,
        [
            {"id": 1, "title": "infra", "state": "done"},
            {"id": 2, "title": "ready now", "depends_on": [1], "state": "open"},
            {"id": 3, "title": "waits on 2", "depends_on": [2], "state": "open"},
        ],
    )
    result = runner.invoke(stint_app, ["task", "list", "--ready", "--project", str(project)])
    assert result.exit_code == 0
    assert "ready now" in result.output
    assert "waits on 2" not in result.output


def test_a_person_on_a_terminal_is_human_unless_they_say_otherwise(tmp_path, monkeypatch) -> None:
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "open"}])
    monkeypatch.delenv("STINT_ROLE", raising=False)

    assert runner.invoke(stint_app, ["task", "assign", "1", "--project", str(project)]).exit_code == 1
    dropped = runner.invoke(
        stint_app, ["task", "drop", "1", "--reason", "cut from the stint", "--project", str(project)]
    )
    assert dropped.exit_code == 0
    assert json.loads((project / ".stint" / "backlog.json").read_text())["tasks"][0]["state"] == "dropped"


def test_ask_files_the_question_and_blocks_the_task_on_the_id_it_minted(tmp_path, monkeypatch) -> None:
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "open"}])
    monkeypatch.setenv("STINT_ROLE", "planner")
    monkeypatch.setenv("STINT_ROUND", "2")

    result = runner.invoke(stint_app, ["ask", "Which clock is canonical?", "--blocks", "1", "--project", str(project)])

    assert result.exit_code == 0, result.output
    decisions = (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8")
    assert "Q01 (round 02) Which clock is canonical? | blocks: 1" in decisions
    task = json.loads((project / ".stint" / "backlog.json").read_text())["tasks"][0]
    assert task["state"] == "blocked"
    assert task["blocked_by"] == ["human:Q01"]


def test_block_refuses_a_question_id_the_decisions_file_does_not_carry(tmp_path, monkeypatch) -> None:
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "open"}])
    monkeypatch.setenv("STINT_ROLE", "planner")

    result = runner.invoke(
        stint_app, ["task", "block", "1", "--by", "human:q_7_2_definition", "--project", str(project)]
    )

    assert result.exit_code == 1
    assert "Ask it first" in result.output
    assert json.loads((project / ".stint" / "backlog.json").read_text())["tasks"][0]["state"] == "open"


def test_add_takes_a_name_and_the_title_as_an_option(tmp_path, monkeypatch) -> None:
    project = _backlog_project(tmp_path, [])
    monkeypatch.setenv("STINT_ROLE", "planner")

    result = runner.invoke(
        stint_app,
        ["task", "add", "--name", "Boot shell", "--title", "Boot shell: the render path", "--project", str(project)],
    )

    assert result.exit_code == 0, result.output
    task = json.loads((project / ".stint" / "backlog.json").read_text())["tasks"][0]
    assert (task["name"], task["title"]) == ("Boot shell", "Boot shell: the render path")


def test_ask_with_a_ruling_records_it_and_blocks_nothing(tmp_path, monkeypatch) -> None:
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "open"}])
    monkeypatch.setenv("STINT_ROLE", "planner")

    result = runner.invoke(
        stint_app, ["ask", "Which clock?", "--decide", "the engine tick, 8.3 pins it", "--project", str(project)]
    )

    assert result.exit_code == 0, result.output
    # Unwrapped: rich breaks the line at the terminal width, and where it
    # breaks moved when the directory name got a character longer.
    assert "stands until a person" in " ".join(result.output.split())
    line = (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8")
    assert "Q01 (round 00) Which clock? | provisional: the engine tick, 8.3 pins it" in line
    assert json.loads((project / ".stint" / "backlog.json").read_text())["tasks"][0]["state"] == "open"


def test_confirm_records_the_persons_word_on_the_backlog(tmp_path) -> None:
    import json

    project = tmp_path / "game"
    (project / ".stint").mkdir(parents=True)
    (project / ".stint" / "backlog.json").write_text(
        json.dumps(
            {
                "meta": {"planned_at": "now", "confirmed_by_human": False},
                "tasks": [
                    {
                        "id": 1,
                        "source": "spec:7.1",
                        "title": "Boot",
                        "gates": ["7.1"],
                        "depends_on": [],
                        "state": "open",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(stint_app, ["confirm", "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert "confirmed" in result.output
    assert json.loads((project / ".stint" / "backlog.json").read_text())["meta"]["confirmed_by_human"] is True

    missing = runner.invoke(stint_app, ["confirm", "--project", str(tmp_path / "nowhere")])
    assert missing.exit_code == 1


def _read(project) -> dict:
    return json.loads((project / ".stint" / "backlog.json").read_text(encoding="utf-8"))


def test_a_round_number_the_environment_got_wrong_is_read_as_round_zero(tmp_path, monkeypatch) -> None:
    """The round only labels history. Taking the turn down over a malformed label
    would cost the work the role is in the middle of."""
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "open"}])
    monkeypatch.setenv("STINT_ROLE", "planner")
    monkeypatch.setenv("STINT_ROUND", "round three")

    result = runner.invoke(stint_app, ["task", "assign", "1", "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert _read(project)["tasks"][0]["history"][-1]["round"] == 0


def test_a_task_log_that_cannot_be_written_does_not_cost_the_transition(tmp_path, monkeypatch) -> None:
    """The log is for reading afterwards; the backlog is the run itself."""
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "open"}])
    blocked = tmp_path / "state-dir-that-is-a-file"
    blocked.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setenv("STINT_STATE_DIR", str(blocked))
    monkeypatch.setenv("STINT_ROLE", "planner")

    result = runner.invoke(stint_app, ["task", "assign", "1", "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert _read(project)["tasks"][0]["state"] == "assigned"


def test_listing_a_project_with_no_backlog_answers_that_the_pool_is_empty(tmp_path) -> None:
    """It used to refuse, with the same "no backlog" message every writing verb
    gets. Looking at the pool is a Planner's first action on a project's first
    round, and an error there read as something having gone wrong: one went off
    reading this program's own installation to find out what. Nothing had."""
    result = runner.invoke(stint_app, ["task", "list", "--project", str(tmp_path / "nowhere")])

    assert result.exit_code == 0, result.output
    assert "no backlog" not in result.output


def test_listing_can_be_narrowed_to_one_state_or_to_what_keeps_being_put_off(tmp_path) -> None:
    project = _backlog_project(
        tmp_path,
        [
            {"id": 1, "title": "boot", "state": "done"},
            {"id": 2, "title": "camera", "state": "open", "deferred": 3},
            {"id": 3, "title": "duel", "state": "open"},
        ],
    )

    done = " ".join(
        runner.invoke(stint_app, ["task", "list", "--state", "done", "--project", str(project)]).output.split()
    )
    stale = " ".join(
        runner.invoke(stint_app, ["task", "list", "--deferred", "3", "--project", str(project)]).output.split()
    )

    assert "boot" in done and "camera" not in done
    assert "camera" in stale and "duel" not in stale


@pytest.mark.parametrize(
    ("verb", "extra", "role", "state"),
    [
        ("defer", ["--reason", "phase two is not built yet"], "planner", "open"),
        ("reject", ["--reason", "the spec does not ask for it"], "planner", "rejected"),
        ("drop", ["--reason", "cut from the stint"], "human", "dropped"),
    ],
)
def test_a_task_can_be_put_off_declined_or_cut(tmp_path, monkeypatch, verb, extra, role, state) -> None:
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "open"}])
    monkeypatch.setenv("STINT_ROLE", role)

    result = runner.invoke(stint_app, ["task", verb, "1", *extra, "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert _read(project)["tasks"][0]["state"] == state


def test_only_qa_can_say_a_finished_task_is_not_finished(tmp_path, monkeypatch) -> None:
    """Not verified is not done, and the role that verified is the one that can undo it."""
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "done"}])

    monkeypatch.setenv("STINT_ROLE", "builder")
    refused = runner.invoke(
        stint_app, ["task", "reopen", "1", "--reason", "the camera regressed", "--project", str(project)]
    )
    assert refused.exit_code == 1
    assert _read(project)["tasks"][0]["state"] == "done"

    monkeypatch.setenv("STINT_ROLE", "verifier")
    result = runner.invoke(
        stint_app, ["task", "reopen", "1", "--reason", "round 08 regression, demo/round_08/", "--project", str(project)]
    )

    assert result.exit_code == 0, result.output
    assert _read(project)["tasks"][0]["state"] == "open"


def test_unblocking_returns_a_task_to_the_work_it_was_in(tmp_path, monkeypatch) -> None:
    project = _backlog_project(tmp_path, [{"id": 1, "title": "the work", "state": "assigned"}])
    monkeypatch.setenv("STINT_ROLE", "planner")
    blocked = runner.invoke(
        stint_app, ["task", "block", "1", "--by", "external:the art is not delivered", "--project", str(project)]
    )
    assert blocked.exit_code == 0, blocked.output
    assert _read(project)["tasks"][0]["state"] == "blocked"

    result = runner.invoke(stint_app, ["task", "unblock", "1", "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert _read(project)["tasks"][0]["state"] == "assigned"


def test_a_question_with_no_words_in_it_is_refused(tmp_path) -> None:
    """An empty entry in the file is one a person cannot answer and the run still waits on."""
    project = tmp_path / "game"
    (project / ".stint").mkdir(parents=True)

    result = runner.invoke(stint_app, ["ask", "   ", "--project", str(project)])

    assert result.exit_code == 1
    assert "needs words" in result.output
    assert not (project / ".stint" / "HUMAN_DECISIONS.md").exists()


# ----------------------------------------------------------------------- init


def _spec_project(tmp_path) -> Path:
    project = tmp_path / "game"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PRD-arena.md").write_text(
        "# Arena\n\n## 7. Acceptance gates\n\n"
        "| # | name | criterion |\n|---|------|-----------|\n"
        "| 7.1 | clean_boot | it must boot with an empty stderr |\n"
        "| 7.2 | feel_log | every event must carry a timestamp of its own |\n"
        "| 7.3 | input | the shot must leave the barrel within 60 ms of the press |\n",
        encoding="utf-8",
    )
    return project


def test_init_lays_out_the_directory_and_says_what_it_wrote(tmp_path) -> None:
    project = _spec_project(tmp_path)

    result = runner.invoke(stint_app, ["init", "--project", str(project)])

    assert result.exit_code == 0, result.output
    for role in ("planner", "builder", "verifier"):
        assert (project / ".stint" / f"{role}.md").is_file(), role
    assert (project / ".stint" / "SPEC.md").resolve() == (project / "docs" / "PRD-arena.md").resolve()
    output = " ".join(result.output.split())
    assert "wrote .stint/planner.md" in output
    assert "HUMAN_DECISIONS.md" in output, "the next step names the file a person has to read"


def test_a_second_init_keeps_what_a_person_edited_and_says_how_to_replace_it(tmp_path) -> None:
    project = _spec_project(tmp_path)
    assert runner.invoke(stint_app, ["init", "--project", str(project)]).exit_code == 0
    guard = project / ".stint" / "planner.md"
    guard.write_text(guard.read_text(encoding="utf-8") + "\nA rule this project added.\n", encoding="utf-8")

    again = runner.invoke(stint_app, ["init", "--project", str(project)])

    assert again.exit_code == 0, again.output
    assert "A rule this project added." in guard.read_text(encoding="utf-8")
    assert "--force to replace" in " ".join(again.output.split())


def test_init_on_a_project_with_no_specification_says_so_rather_than_guessing(tmp_path) -> None:
    project = tmp_path / "bare"
    project.mkdir()

    result = runner.invoke(stint_app, ["init", "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert "no specification document" in " ".join(result.output.split())


def test_init_refuses_a_specification_that_is_not_there(tmp_path) -> None:
    project = _spec_project(tmp_path)

    result = runner.invoke(stint_app, ["init", "--project", str(project), "--spec", str(tmp_path / "nowhere.md")])

    assert result.exit_code == 1
    assert "no specification at" in " ".join(result.output.split())


@pytest.mark.parametrize("flag", ["--read", "--write"])
def test_init_refuses_an_enforcement_grade_it_cannot_apply(tmp_path, flag) -> None:
    """Announced and unenforceable is worse than not announced."""
    project = _spec_project(tmp_path)

    result = runner.invoke(stint_app, ["init", "--project", str(project), flag, "sometimes"])

    assert result.exit_code != 0
    assert not (project / ".stint" / "planner.md").exists()


def test_listing_an_empty_pool_writes_nothing_on_the_way_past(tmp_path) -> None:
    """A read that laid a project out would be a read with a side effect."""
    runner.invoke(stint_app, ["task", "list", "--project", str(tmp_path)])

    assert not (tmp_path / ".stint").exists()


def test_listing_a_backlog_that_is_there_and_unreadable_still_refuses(tmp_path) -> None:
    """Answering an empty pool is not the same as forgiving a broken one."""
    path = tmp_path / ".stint" / "backlog.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    result = runner.invoke(stint_app, ["task", "list", "--project", str(tmp_path)])

    assert result.exit_code == 1
