"""The roles a project declares: what they may touch, and what the loop checks first."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.stint import roster as roster_mod
from raven.stint.roster import APPENDS, CONTINUE, NEVER, OWNS, RosterError

GUARD = """---
role: {name}
{order}session: {session}
enforce:
  read: {read}
  write: hard
owns:
{owns}
appends:
{appends}
reads:
  - .stint/SPEC.md
tasks:
  - list
---

# {name}

{name} does its work.
"""


def _guard(project: Path, name: str, *, owns=(), appends=(), order: int | None = 1, session="fresh", read="soft"):
    path = project / ".stint" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        GUARD.format(
            name=name,
            order=f"order: {order}\n" if order is not None else "",
            session=session,
            read=read,
            owns="\n".join(f"  - {item}" for item in owns) or "  []",
            appends="\n".join(f"  - {item}" for item in appends) or "  []",
        ),
        encoding="utf-8",
    )
    return path


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "game"
    _guard(project, "planner", owns=["reports/brief_{NN}.md"], order=1)
    _guard(project, "builder", owns=[".stint/FIXLOG.md", "project/**", "tools/**"], order=2, session=CONTINUE)
    _guard(
        project,
        "verifier",
        owns=["reports/verify_{NN}.md", "tools/gate_checks/check_verify_*.py"],
        appends=[".stint/FIXLOG.md"],
        order=3,
    )
    return project


def test_a_roster_reads_the_order_and_the_session_policy(tmp_path: Path) -> None:
    roster = roster_mod.load(_project(tmp_path))
    assert [role.name for role in roster.in_round()] == ["planner", "builder", "verifier"]
    assert roster.get("builder").session == CONTINUE
    assert roster.get("planner").session == "fresh"


def test_a_role_with_no_order_is_not_in_the_round(tmp_path: Path) -> None:
    """That is how a one-shot phase or a parallel role is declared."""
    project = _project(tmp_path)
    _guard(project, "asset", owns=["assets_raw/**"], order=None)
    roster = roster_mod.load(project, names=("planner", "builder", "verifier", "asset"))
    assert "asset" not in [role.name for role in roster.in_round()]
    assert roster.get("asset").in_round is False


def test_the_more_specific_glob_wins(tmp_path: Path) -> None:
    roster = roster_mod.load(_project(tmp_path))
    assert roster.grade("builder", "tools/run_round.py") == OWNS
    assert roster.grade("builder", "tools/gate_checks/check_verify_7.py") == NEVER
    assert roster.grade("verifier", "tools/gate_checks/check_verify_7.py") == OWNS
    assert roster.grade("verifier", "tools/run_round.py") == NEVER


def test_one_role_owning_while_another_appends_is_not_a_contest(tmp_path: Path) -> None:
    """The shape the ownership table is built on: the Builder writes it, Verifier adds to it."""
    roster = roster_mod.load(_project(tmp_path))
    assert roster.grade("builder", ".stint/FIXLOG.md") == OWNS
    assert roster.grade("verifier", ".stint/FIXLOG.md") == APPENDS
    assert roster.grade("planner", ".stint/FIXLOG.md") == NEVER
    assert roster.conflicts() == []


def test_two_roles_owning_one_pattern_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _guard(project, "planner", owns=["reports/brief_{NN}.md", ".stint/FIXLOG.md"], order=1)
    conflicts = roster_mod.load(project).conflicts()
    assert len(conflicts) == 1
    assert ".stint/FIXLOG.md" in conflicts[0] and "owned by" in conflicts[0]


def test_a_path_nobody_claims_is_nobody_s(tmp_path: Path) -> None:
    roster = roster_mod.load(_project(tmp_path))
    for role in ("planner", "builder", "verifier"):
        assert roster.grade(role, ".stint/SPEC.md") == NEVER


def test_the_round_number_is_substituted_into_a_pattern(tmp_path: Path) -> None:
    roster = roster_mod.load(_project(tmp_path))
    assert roster.grade("verifier", "reports/verify_04.md", round_index=4) == OWNS
    assert roster.grade("verifier", "reports/verify_03.md", round_index=4) == NEVER


def test_a_reading_list_can_name_the_round_before(tmp_path: Path) -> None:
    project = _project(tmp_path)
    path = project / ".stint" / "planner.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("  - .stint/SPEC.md", "  - reports/verify_{NN-1}.md"), encoding="utf-8"
    )
    role = roster_mod.load(project).get("planner")
    assert roster_mod.reading_list(role, round_index=4) == ("reports/verify_03.md",)


@pytest.mark.parametrize(
    "frontmatter, needle",
    [
        ("role: verifier\norder: 1", "guard file for"),
        ("role: planner\nsession: sometimes", "session is"),
        ("role: planner\nenforce:\n  read: maybe", "enforce.read is"),
        ("role: planner\n  bad: [", "readable YAML"),
    ],
)
def test_a_guard_file_that_declares_nonsense_says_which_part(frontmatter, needle) -> None:
    with pytest.raises(RosterError) as caught:
        roster_mod.parse(f"---\n{frontmatter}\n---\n\nbody\n", name="planner")
    assert needle in str(caught.value)


def test_a_file_with_no_frontmatter_is_refused() -> None:
    with pytest.raises(RosterError) as caught:
        roster_mod.parse("# planner\n\njust prose\n", name="planner")
    assert "frontmatter" in str(caught.value)


def test_a_missing_guard_file_says_how_to_get_one(tmp_path: Path) -> None:
    with pytest.raises(RosterError) as caught:
        roster_mod.load(tmp_path / "game")
    assert "raven playbook stint init" in str(caught.value)


def test_the_prose_half_is_kept_for_the_prompt(tmp_path: Path) -> None:
    role = roster_mod.load(_project(tmp_path)).get("verifier")
    assert role.guard.startswith("# verifier")
    assert "---" not in role.guard


# --------------------------------------------------------------------- preflight


def _seed_backlog(project: Path, *, confirmed: bool = True, tasks: list | None = None) -> None:
    path = project / ".stint" / "backlog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "meta": {"confirmed_by_human": confirmed},
                "tasks": tasks if tasks is not None else [{"id": 1, "title": "the work", "state": "open"}],
            }
        ),
        encoding="utf-8",
    )


def _ready(tmp_path: Path) -> Path:
    project = _project(tmp_path)
    _seed_backlog(project)
    (project / ".stint" / "SPEC.md").write_text("# spec\n", encoding="utf-8")
    return project


def test_a_project_that_has_everything_passes_preflight(tmp_path: Path) -> None:
    assert roster_mod.preflight(_ready(tmp_path)) == []


def test_a_driver_whose_planner_writes_the_backlog_can_leave_the_backlog_gates_off(tmp_path: Path) -> None:
    """The two backlog gates belong to a flow where a person plans before round
    one. A playbook whose Planner plans *in* round one has an empty, unconfirmed
    backlog by design at the start, and the roster, specification and harness
    checks are still worth having there."""
    project = _project(tmp_path)
    (project / ".stint" / "SPEC.md").write_text("# spec\n", encoding="utf-8")

    assert any("no backlog" in reason for reason in roster_mod.preflight(project))
    assert roster_mod.preflight(project, require_backlog=False) == [], "the Planner files the first backlog itself"
    _seed_backlog(project, confirmed=False, tasks=[])
    assert roster_mod.preflight(project) != []
    assert roster_mod.preflight(project, require_backlog=False) == []
    (project / ".stint" / "SPEC.md").unlink()
    assert any("no specification" in reason for reason in roster_mod.preflight(project, require_backlog=False))


def test_preflight_reports_every_reason_at_once(tmp_path: Path) -> None:
    """One pass to fix a project, rather than one refusal per attempt."""
    project = _project(tmp_path)
    reasons = roster_mod.preflight(project)
    assert any("backlog" in reason for reason in reasons)
    assert any("SPEC.md" in reason for reason in reasons)


def test_an_unconfirmed_plan_is_not_ready_to_run(tmp_path: Path) -> None:
    project = _ready(tmp_path)
    _seed_backlog(project, confirmed=False)
    assert any("confirmed_by_human" in reason for reason in roster_mod.preflight(project))


def test_an_empty_backlog_would_leave_the_planner_nothing_to_pick(tmp_path: Path) -> None:
    project = _ready(tmp_path)
    _seed_backlog(project, tasks=[])
    assert any("empty" in reason for reason in roster_mod.preflight(project))


def test_hard_read_enforcement_is_refused_on_an_acp_harness(tmp_path: Path) -> None:
    """Announced and unenforceable is worse than announced as prompt policy."""
    project = _ready(tmp_path)
    _guard(project, "planner", owns=["reports/brief_{NN}.md"], order=1, read="hard")

    assert roster_mod.preflight(project, harness="raven") == []
    reasons = roster_mod.preflight(project, harness="Raven-Code")
    assert any("read: hard" in reason and "raven harness" in reason for reason in reasons)


def test_two_roles_appending_to_one_file_is_not_a_conflict(tmp_path: Path) -> None:
    """Appending is additive and order-independent, which is what makes it the weaker grade."""
    project = _project(tmp_path)
    _guard(project, "planner", owns=["reports/brief_{NN}.md"], appends=[".stint/FIXLOG.md"], order=1)

    roster = roster_mod.load(project)

    assert roster.conflicts() == []
    assert roster.grade("planner", ".stint/FIXLOG.md") == APPENDS
    assert roster.grade("verifier", ".stint/FIXLOG.md") == APPENDS
    assert roster.grade("builder", ".stint/FIXLOG.md") == OWNS


def test_a_game_s_own_output_is_nobody_s_writing(tmp_path: Path) -> None:
    """The fourth kind of path: what the build writes, whichever role ran it.

    The specification these guards are generated from has the game write
    `demo_outputs/feel_log.jsonl` every time it runs in test mode. Granting that
    to one role means the other's measurements are reverted for having been
    made, so it is declared and left ungraded.
    """
    project = tmp_path / "game"
    for name, owns in (("builder", ["project/**"]), ("verifier", ["reports/verify_{NN}.md"])):
        path = _guard(project, name, owns=owns, order=2 if name == "builder" else 3)
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("reads:\n", "artifacts:\n  - demo_outputs/**\nreads:\n", 1), encoding="utf-8")
    _guard(project, "planner", owns=["reports/brief_{NN}.md"], order=1)

    roster = roster_mod.load(project)

    assert roster.artifacts() == ("demo_outputs/**",)
    assert roster.conflicts() == []
    # Still nobody's by the three grades -- which is why the runtime consults
    # the artifact list separately rather than reading a grade off it.
    for role in ("planner", "builder", "verifier"):
        assert roster.grade(role, "demo_outputs/feel_log.jsonl") == NEVER, role
    assert roster_mod.matches_any("demo_outputs/round_01/shot.png", roster.artifacts())
    assert not roster_mod.matches_any("project/main.gd", roster.artifacts())


def test_an_artifact_path_can_name_the_round(tmp_path: Path) -> None:
    project = _project(tmp_path)
    path = project / ".stint" / "verifier.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("reads:\n", "artifacts:\n  - demo_outputs/round_{NN}/**\nreads:\n", 1), "utf-8")

    assert roster_mod.load(project).artifacts(round_index=4) == ("demo_outputs/round_04/**",)
