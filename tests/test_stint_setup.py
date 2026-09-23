"""Laying out a project that has never run a stint, before the stint starts.

The case this exists for: somebody says "run rounds" in a repository that
was never set up. Before, that cost seven commands to discover; without them
the stint started and every round failed rendering a file reference, for as many
rounds as its budget allowed.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.i18n.zh_lexicon import SAYS_WHAT_IS_REQUIRED as SAYS_WHAT_IS_REQUIRED_ZH
from raven.stint.bootstrap import says_what_is_required
from raven.stint.setup import lay_out

SPEC = """# Ledger

A plain-text double-entry bookkeeping library.

## Requirements

- Every posting must balance: debits equal credits, or it is rejected.
- The CLI should print a trial balance with `ledger balance`.
- A malformed entry must exit with code 2 and a message on stderr.

## Acceptance

- `uv run pytest -q` passes. The scope is journals, accounts and reports.
"""


def _repo(tmp_path: Path, spec: str | None = SPEC, where: str = "docs/PRD.md") -> Path:
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / ".git").mkdir()
    if spec is not None:
        (project / where).write_text(spec, encoding="utf-8")
    return project


class TestPickingTheSpecification:
    """Name first, then content. The name is only a promise."""

    def test_a_stub_named_like_a_specification_is_not_one(self, tmp_path: Path) -> None:
        """An empty `SPEC.md` would otherwise send every round to stint from a
        blank page, on the strength of its filename."""
        project = _repo(tmp_path, spec="# SPEC\n\nTODO\n", where="SPEC.md")

        found = lay_out(project, "stint")

        assert not found.ready
        assert "nothing to stint from" in found.missing

    def test_a_long_document_that_requires_nothing_is_not_one_either(self, tmp_path: Path) -> None:
        project = _repo(tmp_path, spec="# Notes\n\n" + "we talked about the weather. " * 40, where="docs/notes.md")

        assert not lay_out(project, "stint").ready

    def test_the_document_that_says_what_has_to_be_true_is_the_one(self, tmp_path: Path) -> None:
        project = _repo(tmp_path)
        (project / "docs" / "notes.md").write_text("# Notes\n\n" + "chatter. " * 60, encoding="utf-8")

        found = lay_out(project, "stint")

        assert found.ready
        assert found.spec == "docs/PRD.md"
        assert found.also_matched == 0, "the notes were dropped, not ranked second"

    def test_the_standing_orders_are_not_candidates(self, tmp_path: Path) -> None:
        """They are full of "must" and "should" and they are requirements about
        the roles, not about the project. Laying out twice must not start
        offering them as the thing to stint from."""
        project = _repo(tmp_path)
        lay_out(project, "stint")

        again = lay_out(project, "stint")

        assert again.spec == "docs/PRD.md"
        assert again.also_matched == 0

    def test_a_specification_written_where_the_refusal_asked_for_one_is_found(self, tmp_path: Path) -> None:
        """The refusal names `.stint/SPEC.md`. A finder that did not look there
        would ask for the file again on the next run."""
        project = _repo(tmp_path, spec=None)
        first = lay_out(project, "stint")
        assert not first.ready and ".stint/SPEC.md" in first.missing

        (project / ".stint" / "SPEC.md").write_text(SPEC, encoding="utf-8")

        assert lay_out(project, "stint").spec == ".stint/SPEC.md"

    def test_the_count_is_of_words_that_say_something_is_required(self) -> None:
        assert says_what_is_required("the release notes for October") == 0
        assert says_what_is_required("Every posting must balance. Acceptance: it is rejected.") == 2
        # The zh half of the table, composed from the catalog rather than written
        # here: CJK in a test outside `tests/test_i18n_*` is a source-language
        # violation, and the words themselves are language data with one home.
        must, acceptance = SAYS_WHAT_IS_REQUIRED_ZH[0], SAYS_WHAT_IS_REQUIRED_ZH[4]
        assert says_what_is_required(f"{must}{acceptance}") == 2


class TestWhatALayoutLeavesBehind:
    def test_a_project_that_has_never_run_a_plan_is_laid_out(self, tmp_path: Path) -> None:
        project = _repo(tmp_path)

        found = lay_out(project, "stint")

        assert found.ready
        for name in ("planner.md", "builder.md", "verifier.md", "HUMAN_DECISIONS.md"):
            assert (project / ".stint" / name).is_file(), name
        assert all(name.startswith(".stint/") for name in found.wrote), found.wrote

    def test_laying_out_twice_writes_nothing_the_second_time(self, tmp_path: Path) -> None:
        """A second stint on a set-up project is not a second setup, and a person
        told to try again must not be risking their edits."""
        project = _repo(tmp_path)
        lay_out(project, "stint")
        (project / ".stint" / "planner.md").write_text("my own orders\n", encoding="utf-8")

        again = lay_out(project, "stint")

        assert again.wrote == []
        assert (project / ".stint" / "planner.md").read_text(encoding="utf-8") == "my own orders\n"

    def test_a_directory_that_is_not_a_repository_is_refused_before_anything_is_written(self, tmp_path: Path) -> None:
        """A stint cannot be held to its boundaries without one, so laying the
        project out first would leave files behind for a run that was never
        going to happen."""
        project = tmp_path / "loose"
        project.mkdir()

        found = lay_out(project, "stint")

        assert not found.ready and "git init" in found.missing
        assert not (project / ".stint").exists()

    def test_a_recipe_this_build_does_not_have_is_refused_rather_than_guessed(self, tmp_path: Path) -> None:
        """The closed set is what makes `setup` safe to honour from a file
        somebody else wrote."""
        found = lay_out(_repo(tmp_path), "whatever-i-like")

        assert not found.ready and "no setup recipe" in found.missing

    def test_a_backlog_is_counted_when_the_project_has_one(self, tmp_path: Path) -> None:
        project = _repo(tmp_path)
        lay_out(project, "stint")
        (project / ".stint" / "backlog.json").write_text(
            json.dumps({"tasks": [{"id": "T1"}, {"id": "T2"}]}), encoding="utf-8"
        )

        assert lay_out(project, "stint").backlog == 2

    def test_no_backlog_is_told_apart_from_an_empty_one(self, tmp_path: Path) -> None:
        project = _repo(tmp_path)
        assert lay_out(project, "stint").backlog == -1

        (project / ".stint" / "backlog.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
        assert lay_out(project, "stint").backlog == 0


class TestWhatTheBuilderMayWrite:
    """The grant has to cover the work the round's own check measures."""

    def test_a_greenfield_project_grants_the_tests_directory(self, tmp_path: Path) -> None:
        """Measured on a greenfield run: the brief asked for `tests/test_x.py`
        by name, the round's own check ran `compileall src tests`, and the
        Builder's grant stopped at the source -- so it wrote the code, wrote the
        tests for it, and had the tests undone as a write it may not make. The
        layout was refusing the write its own check depended on."""
        from raven.stint.bootstrap import source_dirs

        granted = source_dirs(_repo(tmp_path))

        assert "tests/**" in granted, granted

    def test_a_project_with_only_tests_is_still_greenfield(self, tmp_path: Path) -> None:
        """A test directory is somebody's work but not the thing being built, so
        having one says nothing about where the source will live."""
        from raven.stint.bootstrap import source_dirs

        project = _repo(tmp_path)
        (project / "tests").mkdir()

        granted = source_dirs(project)

        assert "src/**" in granted and "tests/**" in granted, granted

    def test_a_project_that_keeps_its_code_elsewhere_still_gets_its_own_dirs(self, tmp_path: Path) -> None:
        from raven.stint.bootstrap import source_dirs

        project = _repo(tmp_path)
        (project / "lib").mkdir()

        granted = source_dirs(project)

        assert "lib/**" in granted
        assert "project/**" not in granted, "a detected source tree is not widened by the greenfield set"

    def test_what_an_interpreter_leaves_beside_the_files_it_read_is_nobodys(self, tmp_path: Path) -> None:
        """A role that runs the tests has caches written for it. Graded as its
        writes they are a violation it can neither avoid nor undo."""
        from raven.stint.bootstrap import BYPRODUCT_GLOBS

        lay_out(_repo(tmp_path), "stint")
        guard = (tmp_path / "project" / ".stint" / "builder.md").read_text(encoding="utf-8")

        for glob in BYPRODUCT_GLOBS:
            assert glob in guard, f"{glob} is not declared as an artifact"


class TestARecipeAndTheRolesThatNameIt:
    def test_a_playbook_whose_roles_the_recipe_does_not_know_is_told_so(self, tmp_path: Path) -> None:
        """The recipe writes a guard file per role and knows three. A playbook
        naming others got those three written anyway -- files belonging to
        nobody in the run -- and found out later, if at all, when a `{{ref:}}`
        to its own guard resolved to nothing."""
        project = _repo(tmp_path)

        found = lay_out(project, "stint", roles=["writer", "checker"])

        assert not found.ready
        assert "writer, checker" in found.missing
        assert "planner, builder, verifier" in found.missing
        assert not (project / ".stint").exists(), "nothing is written when the recipe cannot serve the roles"

    def test_the_roles_the_recipe_knows_are_laid_out_as_before(self, tmp_path: Path) -> None:
        found = lay_out(_repo(tmp_path), "stint", roles=["planner", "builder", "verifier"])

        assert found.ready, found.missing
        assert (tmp_path / "project" / ".stint" / "builder.md").is_file()

    def test_a_caller_that_names_no_roles_is_laid_out_as_before(self, tmp_path: Path) -> None:
        """The check is on what a playbook declares; a caller with nothing to
        declare -- `stint init` on a terminal -- is not refused for it."""
        assert lay_out(_repo(tmp_path), "stint").ready
