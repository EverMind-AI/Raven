"""Which role may write which path, read off the roster rather than a guard file."""

from __future__ import annotations

import pytest

from raven.stint.ownership import (
    APPENDS,
    CONTINUE,
    FRESH,
    HARD,
    NEVER,
    OWNS,
    SOFT,
    Role,
    Roster,
    RosterError,
    describe,
    merged_globs,
    reading_list,
    writable,
)


def _roster() -> Roster:
    return Roster(
        roles=[
            Role(name="planner", order=1, owns=("reports/brief_{NN}.md",), reads=(".stint/SPEC.md",)),
            Role(
                name="developer",
                order=2,
                session=CONTINUE,
                owns=("project/**", "tools/**"),
                appends=(".stint/FIXLOG.md",),
            ),
            Role(name="qa", order=3, owns=("reports/qa_{NN}.md",), appends=(".stint/FIXLOG.md",)),
        ]
    )


def test_asking_for_a_role_the_project_never_declared_names_the_ones_it_did() -> None:
    """A typo in a role name is the common case, and the answer is the fix."""
    with pytest.raises(RosterError) as caught:
        _roster().get("designer")

    assert "designer" in str(caught.value)
    for declared in ("planner", "developer", "qa"):
        assert declared in str(caught.value)


def test_a_directory_covers_what_is_under_it_however_the_pattern_spells_it() -> None:
    roster = Roster(roles=[Role(name="qa", owns=("demo_outputs/", "reports/*/"))])

    assert roster.grade("qa", "demo_outputs/round_01/shot.png") == OWNS
    assert roster.grade("qa", "reports/round_01/notes.md") == OWNS
    assert roster.grade("qa", "demo_outputs_old/shot.png") == NEVER


def test_a_role_that_owns_a_tree_but_only_appends_to_one_file_in_it_may_only_append_there() -> None:
    """Specificity decides within one role's own claims too, not just between roles."""
    roster = Roster(roles=[Role(name="developer", owns=("tools/**",), appends=("tools/FIXLOG.md",))])

    assert roster.grade("developer", "tools/build.py") == OWNS
    assert roster.grade("developer", "tools/FIXLOG.md") == APPENDS


def test_naming_one_file_before_the_tree_around_it_leaves_the_tree_owned() -> None:
    """The claim that answers is the most specific one, not the last one read."""
    roster = Roster(roles=[Role(name="developer", owns=("tools/build.py", "tools/**"))])

    assert roster.grade("developer", "tools/build.py") == OWNS
    assert roster.grade("developer", "tools/gate_checks/check_qa_7.py") == OWNS


def test_a_roster_carries_every_role_with_its_grades_and_its_policies() -> None:
    """This dict is what a run record and a page read; a field missing here is a
    field nobody outside the process can see."""
    payload = _roster().to_dict()

    assert [role["name"] for role in payload["roles"]] == ["planner", "developer", "qa"]
    developer = payload["roles"][1]
    assert developer["order"] == 2
    assert developer["session"] == CONTINUE
    assert developer["enforce"] == {"read": SOFT, "write": HARD}
    assert developer["owns"] == ["project/**", "tools/**"]
    assert developer["appends"] == [".stint/FIXLOG.md"]
    assert payload["roles"][0]["reads"] == [".stint/SPEC.md"]
    assert payload["roles"][0]["session"] == FRESH


def test_what_a_role_may_write_names_this_rounds_number() -> None:
    role = Role(
        name="qa",
        owns=("reports/qa_{NN}.md",),
        appends=(".stint/FIXLOG.md",),
        reads=("reports/dev_{NN-1}.md",),
    )

    assert writable(role, round_index=7) == ("reports/qa_07.md", ".stint/FIXLOG.md")
    assert reading_list(role, round_index=7) == ("reports/dev_06.md",)


def test_the_table_a_person_reads_names_the_roles_outside_the_round_too() -> None:
    """A role with no turn is the easiest one to declare wrongly and never notice."""
    roster = Roster(roles=[*_roster().roles, Role(name="asset")])

    lines = describe(roster).splitlines()

    assert lines[0].split() == ["role", "order", "session", "read", "write", "owns"]
    assert [line.split()[0] for line in lines[1:]] == ["planner", "developer", "qa", "asset"]
    assert lines[1].split()[1] == "1"
    assert lines[4].split()[1] == "--"
    assert lines[4].split()[-1] == "(nothing)"


def test_the_rosters_globs_can_be_collected_by_grade_for_the_round_being_run() -> None:
    roster = _roster()

    assert list(merged_globs(roster, OWNS, round_index=3)) == [
        "reports/brief_03.md",
        "project/**",
        "tools/**",
        "reports/qa_03.md",
    ]
    assert sorted(set(merged_globs(roster, APPENDS))) == [".stint/FIXLOG.md"]


def test_an_appender_does_not_take_a_tree_away_from_the_role_that_owns_it() -> None:
    """The weaker grade may not outrank the stronger one, however exactly it is spelled.

    Compared across grades, QA's word about one file inside the tree answered
    for the whole claim and the Developer came back ``never`` -- while the guard
    section, which is read off the Developer's own row and knows nothing of
    QA's, still told it ``tools/**`` was its own. The write it was instructed to
    make was reverted and filed as a violation.

    The FIXLOG pattern only looked like it worked because both roles happened to
    name the path with equal precision; an owner that said ``.stint/**`` instead
    of ``.stint/FIXLOG.md`` lost it to the appender.
    """
    roster = Roster(
        roles=[
            Role(name="developer", owns=("tools/**",)),
            Role(name="qa", appends=("tools/notes.md",)),
        ]
    )

    assert roster.grade("developer", "tools/notes.md") == OWNS
    assert roster.grade("developer", "tools/build.py") == OWNS
    assert roster.grade("qa", "tools/notes.md") == APPENDS
    assert roster.grade("qa", "tools/build.py") == NEVER


def test_an_owner_does_not_take_an_appenders_claim_away_either() -> None:
    """The same rule read from the other end: grades do not contest each other."""
    roster = Roster(
        roles=[
            Role(name="developer", appends=("tools/**",)),
            Role(name="qa", owns=("tools/notes.md",)),
        ]
    )

    assert roster.grade("developer", "tools/notes.md") == APPENDS
    assert roster.grade("qa", "tools/notes.md") == OWNS


def test_two_owners_of_one_tree_still_resolve_by_how_exactly_they_name_it() -> None:
    """The contest that is real is owner against owner, and it is unchanged.

    This is the rule cross-grade comparison was over-serving: an exact name
    carves an exception out of a wildcard, the way `.gitignore` reads it.
    """
    roster = Roster(
        roles=[
            Role(name="developer", owns=("tools/**",)),
            Role(name="qa", owns=("tools/gate_checks/check_qa_*.py",)),
        ]
    )

    assert roster.grade("qa", "tools/gate_checks/check_qa_7.py") == OWNS
    assert roster.grade("developer", "tools/gate_checks/check_qa_7.py") == NEVER
    assert roster.grade("developer", "tools/build.py") == OWNS


def test_the_appender_that_names_a_path_most_exactly_is_the_one_that_may_add() -> None:
    """Appenders contest each other on specificity, like owners do."""
    roster = Roster(
        roles=[
            Role(name="developer", appends=("notes/**",)),
            Role(name="qa", appends=("notes/qa.md",)),
        ]
    )

    assert roster.grade("qa", "notes/qa.md") == APPENDS
    assert roster.grade("developer", "notes/qa.md") == NEVER
    assert roster.grade("developer", "notes/plan.md") == APPENDS
