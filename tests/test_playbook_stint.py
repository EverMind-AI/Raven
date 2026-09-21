"""A stint playbook compiled into one graph a round, and run for several."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.config.schema import ThirdPartyCliSubagentConfig
from raven.playbook.executor import PlaybookExecutor
from raven.playbook.stint import StintDriver, compile_round, journal_entry
from raven.playbook.types import PlaybookSpec
from raven.stint.git import ProjectGit
from raven.stint.record import StintRef, StintStore
from raven.stint.verify import CheckSpec
from tests.test_subagent_dag_runner import draining_dag_runs


def tmpdir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp())


def _spec(**over: Any) -> PlaybookSpec:
    base: dict[str, Any] = {
        "name": "game-dev",
        "description": "push the project forward a round at a time",
        "taskSummary": "run the next rounds of the project",
        "mode": "stint",
        "triggers": {"keywords": ["game"]},
        "roles": [
            {
                "as": "planner",
                "name": "echo",
                "promptTemplate": "round {{round.index}}\n{{round.journal}}",
                "owns": ["reports/brief_{NN}.md"],
            },
            {
                "as": "developer",
                "name": "echo",
                "dependsOn": ["planner"],
                "promptTemplate": "do this: {{planner.output}}",
                "owns": ["src/**"],
            },
        ],
        "stop": {"maxRounds": 2},
    }
    base.update(over)
    return PlaybookSpec.model_validate(base)


class TestCompile:
    def test_a_round_becomes_one_node_per_role_under_that_round_s_own_ids(self) -> None:
        """Ids carry the round because a node id is claimed for the whole
        conversation, and one stint submits thirty graphs into one."""
        nodes = compile_round(_spec(), 12)

        assert [node["id"] for node in nodes] == ["game-dev-r12-planner", "game-dev-r12-developer"]
        assert nodes[1]["depends_on"] == ["game-dev-r12-planner"]
        assert nodes[0]["subagent"] == "echo"

    def test_a_round_numbered_path_is_spelled_out_for_the_role_that_owns_it(self) -> None:
        """The guard is what the role is told; the same pattern, expanded, is
        what undoes its work. `brief_7.md` is not `brief_07.md`, so a role given
        only the unexpanded spelling loses the file for obeying it."""
        from raven.stint.ownership import expand

        told = compile_round(_spec(), 7)[0]["prompt_template"]

        assert expand("reports/brief_{NN}.md", 7) in told
        assert "{NN}" not in told

    def test_a_role_names_another_role_and_the_graph_gets_a_node_id(self) -> None:
        """A playbook's author knows labels; the runner resolves node ids."""
        nodes = compile_round(_spec(), 3)

        assert "{{game-dev-r03-planner.output}}" in nodes[1]["prompt_template"]
        assert "{{planner.output}}" not in nodes[1]["prompt_template"]

    def test_the_round_s_own_facts_are_filled_in_before_the_graph_is_submitted(self) -> None:
        nodes = compile_round(_spec(), 7, journal="## Round 06\n\n### Dev\n\nlast round's note\n")

        prompt = nodes[0]["prompt_template"]
        assert "round 7" in prompt
        assert "last round's note" in prompt
        assert "{{round." not in prompt

    def test_a_first_round_says_so_rather_than_leaving_a_hole(self) -> None:
        assert "first round" in compile_round(_spec(), 1)[0]["prompt_template"]

    def test_the_guard_tells_a_role_that_the_tool_running_it_is_not_the_project(self) -> None:
        """Three roles spent hours of a live stint reading Raven's source, its
        session directories and the shadow repository, digging for files the
        enforcement pass had undone. Nothing about the boundary changes from
        that, so the guard says where the work is and where it is not."""
        told = compile_round(_spec(), 2)[0]["prompt_template"]

        assert "~/.raven" in told
        assert ".raven/" in told
        assert "filesystem root" in told
        assert "undone is kept outside the project" in told

    def test_what_a_role_owns_reaches_the_role_even_when_the_author_forgot_to_ask(self) -> None:
        """One declaration, read twice: here and by the pass that undoes a stray
        write. Leaving the slot out does not opt out of being enforced, so it
        cannot be allowed to opt out of being told either."""
        prompt = compile_round(_spec(), 4)[0]["prompt_template"]

        assert "{{round.guard}}" not in _spec().roles[0].prompt_template
        assert "reports/brief_04.md" in prompt
        assert "undone" in prompt

    def test_the_slot_decides_where_the_boundary_goes_when_the_author_writes_one(self) -> None:
        spec = _spec(
            roles=[
                {
                    "as": "dev",
                    "name": "echo",
                    "owns": ["src/**"],
                    "promptTemplate": "{{round.guard}}\n\nnow do the work",
                }
            ]
        )
        prompt = compile_round(spec, 1)[0]["prompt_template"]

        assert prompt.index("src/**") < prompt.index("now do the work")
        assert prompt.count("These paths are yours") == 1

    def test_a_role_that_declares_no_boundary_is_told_none(self) -> None:
        spec = _spec(roles=[{"as": "dev", "name": "echo", "promptTemplate": "just do it"}])

        assert compile_round(spec, 1)[0]["prompt_template"] == "just do it"

    def test_a_resumed_round_skips_what_it_already_finished(self) -> None:
        """The finished node is still named, so its output stays readable: the
        graph contract treats a dependency an earlier run completed as met."""
        nodes = compile_round(_spec(), 5, satisfied={"planner": "game-dev-r05-planner-old"})

        assert [node["id"] for node in nodes] == ["game-dev-r05-developer"]
        assert nodes[0]["depends_on"] == ["game-dev-r05-planner-old"]
        assert "{{game-dev-r05-planner-old.output}}" in nodes[0]["prompt_template"]

    def test_the_journal_is_the_append_only_file_and_not_just_any_carried_one(self) -> None:
        spec = _spec(memory=[{"path": "backlog.json"}, {"path": "NOTES.md", "append": True, "recentRounds": 3}])

        entry = journal_entry(spec)
        assert entry is not None and entry.path == "NOTES.md" and entry.recent_rounds == 3
        assert journal_entry(_spec()) is None

    def test_a_role_s_servers_and_skills_travel_to_its_node(self) -> None:
        spec = _spec(
            roles=[{"as": "qa", "name": "echo", "promptTemplate": "check", "mcps": ["godot"], "skills": []}],
        )
        node = compile_round(spec, 1)[0]

        assert node["mcps"] == ["godot"]
        assert node["skills"] == []


class TestRunning:
    """A stint that actually runs, on `cat` as the sub-agent: output is the prompt."""

    @pytest.fixture(autouse=True)
    async def _drain(self):
        async with draining_dag_runs():
            yield

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        """The repository a stint works, beside the agent's own directories.

        A directory of its own rather than `tmp_path` itself, because the default
        isolation runs the round *in* the project: with the two collapsed, the
        run's own node artifacts land inside the tree the boundary is measured
        against and are undone as writes nobody owns. In production they are
        always apart -- run dirs hang off the session directory under agent home
        (`SubAgentDagTool._run_root`), never off the project.
        """
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        ProjectGit(project).ensure_repo()
        return project

    @staticmethod
    def _executor(tmp_path: Path, announced: list[str], charges: list[str | None]) -> PlaybookExecutor:
        # A stint that enforces boundaries works a repository; which tree it gets
        # out of it is `isolation` -- see `test_a_plan_asked_for_a_checkout_of_its_own_gets_one`.
        project = TestRunning._project(tmp_path)

        async def announce(run_id: str, text: str, origin: dict) -> None:
            announced.append(text)

        def charge(session_key: str | None) -> str | None:
            charges.append(session_key)
            return None

        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            announce=announce,
            charge=charge,
        )
        tool.set_context("web", "default", "web:stint")
        return PlaybookExecutor(dag_tool=tool, workspace=project)

    @staticmethod
    async def _await_announce(announced: list[str], *, timeout: float = 30.0, count: int = 1) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(announced) < count:
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail("the stint never announced anything")
            await asyncio.sleep(0.05)
        return announced[-1]

    async def test_a_two_round_plan_runs_both_rounds_and_says_so_as_it_goes(self, tmp_path: Path) -> None:
        announced: list[str] = []
        charges: list[str | None] = []
        executor = self._executor(tmp_path, announced, charges)

        stint = await executor.execute(_spec(confirm=False), {})
        assert stint.kind == "dag", stint.reply

        summary = await self._await_announce(announced, count=2)

        store = StintStore(executor.dag_tool.stints_root("web:stint"))
        record = store.list()[0]
        assert record.status == "finished"
        assert record.stop_reason == "the round budget of 2 is spent"
        assert [entry.index for entry in record.rounds] == [1, 2]
        assert all(entry.run_id for entry in record.rounds), record.rounds
        assert len({entry.run_id for entry in record.rounds}) == 2, "each round is its own run"

        # A stint that spoke only at the end could not be told from one that hung,
        # and at eighteen minutes a round that is hours of it.
        assert len(announced) == 2, announced
        assert "finished round 1 of at most 2" in announced[0]
        assert "next round is starting" in announced[0]
        assert "ran 2 round(s) and stopped" in summary
        assert record.stint_id in summary

    async def test_a_round_count_the_caller_asked_for_is_what_runs(self, tmp_path: Path) -> None:
        """The whole way through, from the argument a tool call carries to the
        reason the stint gives for stopping."""
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])

        stint = await executor.execute(_spec(confirm=False), {}, max_rounds=1)
        assert stint.kind == "dag", stint.reply

        summary = await self._await_announce(announced)

        record = StintStore(executor.dag_tool.stints_root("web:stint")).list()[0]
        assert [entry.index for entry in record.rounds] == [1]
        assert record.stop_reason == "the round budget of 1 is spent"
        assert "ran 1 round(s) and stopped" in summary
        assert not any("finished round 2" in text for text in announced), "the last round promises no next one"

    async def test_what_a_round_did_reaches_the_round_after_it(self, tmp_path: Path) -> None:
        """The whole reason rounds carry a file. Every round is a new
        conversation, so a role that learned something last round reaches this
        one only by having had it written down -- and for two rounds the journal
        stayed empty, which made round two read as round one."""
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])
        spec = _spec(
            confirm=False,
            memory=[{"path": "JOURNAL.md", "append": True, "recentRounds": 2}],
            roles=[
                {
                    "as": "planner",
                    "name": "echo",
                    "promptTemplate": "round {{round.index}}\n{{round.journal}}",
                    "journalSection": "Stint",
                    "owns": ["reports/brief_{NN}.md"],
                }
            ],
        )

        await executor.execute(spec, {})
        # Both rounds: the first announce is now round one's progress line, and
        # reading the journal off it would race round two into existence.
        await self._await_announce(announced, count=2)

        record = StintStore(executor.dag_tool.stints_root("web:stint")).list()[0]
        journal = (Path(record.workdir) / "JOURNAL.md").read_text(encoding="utf-8")

        assert journal.startswith("## Round 01"), journal
        assert "## Round 02" in journal, journal
        # `cat` echoes its prompt, so round two's own entry quotes what it was
        # handed -- and what it was handed is round one's section.
        second = journal.split("## Round 02", 1)[1]
        assert "### Stint" in second and "## Round 01" in second, second

    async def test_a_role_that_declares_no_section_writes_no_journal(self, tmp_path: Path) -> None:
        """What keeps the file off a stint that never asked to carry one."""
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])

        await executor.execute(_spec(confirm=False), {})
        await self._await_announce(announced)

        record = StintStore(executor.dag_tool.stints_root("web:stint")).list()[0]
        assert not (Path(record.workdir) / "JOURNAL.md").exists()

    async def test_a_round_that_needs_nothing_says_so_rather_than_inviting_a_rescue(self, tmp_path: Path) -> None:
        """A failed check reads like a problem to solve, and a main agent that
        solved it would be editing the checkout the next round is about to."""
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])

        await executor.execute(_spec(confirm=False), {})
        await self._await_announce(announced, count=2)

        progress = announced[0]
        assert "progress, not a request" in progress
        assert "Do not edit the stint's checkout" in progress

    async def test_a_playbook_may_ask_for_the_silence_back(self, tmp_path: Path) -> None:
        """Reporting costs a main-agent turn a round. A long unattended stint is
        allowed to decide that is not worth it."""
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])

        await executor.execute(_spec(confirm=False, stop={"maxRounds": 2, "report": "end"}), {})
        summary = await self._await_announce(announced)

        assert len(announced) == 1, announced
        assert "ran 2 round(s) and stopped" in summary

    async def test_a_plan_asked_for_a_checkout_of_its_own_gets_one(self, tmp_path: Path) -> None:
        """`isolation: worktree` means the person who started the stint can keep
        using their own tree while it runs."""
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])

        await executor.execute(_spec(confirm=False, isolation="worktree"), {})
        await self._await_announce(announced)

        record = StintStore(executor.dag_tool.stints_root("web:stint")).list()[0]
        assert record.workdir != str(tmp_path / "project")
        assert record.branch == f"stint/{record.stint_id}"
        assert Path(record.workdir).is_dir()

    async def test_a_plan_that_says_nothing_works_the_project_on_a_branch_of_its_own(self, tmp_path: Path) -> None:
        """The default. No second checkout to pay for and none left behind, and
        the work lands where the person will look for it -- their repository, one
        `git log stint/<id>` away."""
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])

        await executor.execute(_spec(confirm=False), {})
        await self._await_announce(announced)

        record = StintStore(executor.dag_tool.stints_root("web:stint")).list()[0]
        assert record.workdir == str(tmp_path / "project")
        assert record.branch == f"stint/{record.stint_id}"
        assert ProjectGit(tmp_path / "project").branch() == record.branch
        # The echo agent hands the prompt back, so the round's summary is what
        # the role was told about where it stands. A branch is not a checkout:
        # told it was in one, a role goes looking for the "real" project.
        told = record.rounds[0].summary
        assert "the project itself" in told, told
        assert "checkout of the project made for this stint" not in told

    async def test_the_checks_ledger_the_run_writes_is_not_the_person_s_uncommitted_work(self, tmp_path: Path) -> None:
        """Resolving a check declared by description writes `.stint/checks.json`
        after the layout and before the tree is opened, and it is not in the
        layout's own list of what it wrote -- so a fresh project whose check
        was detected was refused for a file the run had just written."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname = 'thing'\n", encoding="utf-8")
        ProjectGit(project).ensure_repo()
        tool = _FakeTool(tmp_path / "stints")
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: project)
        spec = _spec(
            confirm=True,
            verify=[{"name": "build", "description": "the source compiles"}],
            roles=[
                {"as": "planner", "name": "echo", "promptTemplate": "plan", "owns": ["reports/**"]},
                {
                    "as": "developer",
                    "name": "echo",
                    "dependsOn": ["planner"],
                    "promptTemplate": "build",
                    "owns": ["src/**"],
                    "verifyAfter": ["build"],
                },
            ],
        )

        receipt = await driver.start(spec)

        assert not receipt.startswith("Error"), receipt
        ledger = json.loads((project / ".stint" / "checks.json").read_text(encoding="utf-8"))
        assert ledger["game-dev-build"]["from"] == "detected"
        assert ProjectGit(project).branch().startswith("stint/")

    async def test_a_run_refused_after_its_layout_is_not_refused_again_for_that_layout(self, tmp_path: Path) -> None:
        """The layout is written before the checks are asked about, and a check
        nobody has answered refuses the run -- so the second attempt finds
        `.stint/` in the tree, written by nobody it can name. Read as the
        person's unfinished work, every fresh project that had to be asked
        anything was refused on the retry, for the files the first try wrote."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "docs").mkdir()
        (project / "docs" / "PRD.md").write_text(
            "# The thing\n\n" + "The build must pass. The player must be able to move.\n" * 20, encoding="utf-8"
        )
        ProjectGit(project).ensure_repo()
        tool = _FakeTool(tmp_path / "stints")
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: project)
        spec = _spec(
            confirm=True,
            setup="stint",
            verify=[{"name": "build", "description": "the source compiles"}],
            roles=[
                {"as": "planner", "name": "echo", "promptTemplate": "plan", "owns": ["reports/**"]},
                {
                    "as": "developer",
                    "name": "echo",
                    "dependsOn": ["planner"],
                    "promptTemplate": "build",
                    "owns": ["src/**"],
                    "verifyAfter": ["build"],
                },
            ],
        )

        first = await driver.start(spec)
        assert first.startswith("Error") and "never answered" in first, first
        assert (project / ".stint" / "planner.md").is_file(), "the layout was written before the refusal"

        from raven.stint.verify import remember_check

        remember_check(project, "game-dev", "build", "true")
        second = await driver.start(spec)

        assert not second.startswith("Error"), second
        assert ProjectGit(project).branch().startswith("stint/")
        record = StintStore(tmp_path / "stints").list()[0]
        assert ".stint/planner.md" in record.untracked_at_start

    async def test_a_layout_file_the_person_edited_after_committing_it_still_stops_the_run(
        self, tmp_path: Path
    ) -> None:
        """The exemption above is for files nobody committed. One the person
        committed and then changed is theirs: round one would grade the change
        as the first role's stray write and put it back."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "docs").mkdir()
        (project / "docs" / "PRD.md").write_text(
            "# The thing\n\n" + "The build must pass. The player must be able to move.\n" * 20, encoding="utf-8"
        )
        git = ProjectGit(project)
        git.ensure_repo()
        tool = _FakeTool(tmp_path / "stints")
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: project)
        spec = _spec(confirm=True, setup="stint", stop={"maxRounds": 1})

        from raven.stint.setup import lay_out

        lay_out(project, "stint")
        git.commit("chore: the layout, committed by the person")
        (project / ".stint" / "planner.md").write_text("my own orders\n", encoding="utf-8")

        receipt = await driver.start(spec)

        assert receipt.startswith("Error") and ".stint/planner.md" in receipt and "Commit or stash" in receipt

    async def test_a_second_stint_of_one_playbook_in_one_conversation_gets_ids_of_its_own(self, tmp_path: Path) -> None:
        """Node ids are claimed for the life of a conversation, and a round's
        were `<playbook>-rNN-<role>`: the second run of a probe playbook from
        the same terminal was refused at round one -- `node id
        'mcp-probe-r01-probe' is already used by run ...` (2026-09-21)."""
        from raven.stint.record import FINISHED

        tool = _FakeTool(tmp_path / "stints")
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: tmp_path)
        spec = _spec(confirm=False, roles=[{"as": "probe", "name": "echo", "promptTemplate": "look"}])

        assert not (await driver.start(spec)).startswith("Error")
        store = StintStore(tool.stints_root("web:stint"))
        [first] = store.list()
        first.status = FINISHED
        store.write(first)
        assert not (await driver.start(spec)).startswith("Error")

        [one], [two] = tool.submitted
        assert one["id"] != two["id"]
        assert one["id"].startswith("game-dev-") and one["id"].endswith("-r01-probe")
        assert first.token and first.token in one["id"]

    async def test_a_round_the_graph_tool_refused_opens_nothing_and_holds_nothing(self, tmp_path: Path) -> None:
        """A validation error came back as text with no run id in it, and the
        driver opened a round on it anyway: a beat started for a run that did
        not exist, and the terminal holding on that beat never came back."""
        tool = _FakeTool(tmp_path / "stints")
        tool.refuse = "Error: invalid DAG -- node id 'x' is already used by run 'r'. No sub-agent was run."
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: tmp_path)
        spec = _spec(confirm=False, roles=[{"as": "probe", "name": "echo", "promptTemplate": "look"}])

        receipt = await driver.start(spec)

        assert receipt.startswith("Error") and "already used" in receipt
        [record] = StintStore(tool.stints_root("web:stint")).list()
        assert record.status == "stopped"
        assert record.round(1) is None
        assert driver.holding() is False and driver._beats == {}

    async def test_a_first_round_the_person_refused_closes_the_stint_rather_than_leaving_it_open(
        self, tmp_path: Path
    ) -> None:
        """The denial came back as a sentence that did not begin with `Error`, so
        the record stayed running with round one open and the beat going. The
        next `resume` -- which asks nobody, the stint having been approved --
        ran the graph the person had just refused, shell commands included."""
        tool = _FakeTool(tmp_path / "stints")
        tool.deny = True
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: tmp_path)
        spec = _spec(
            confirm=True,
            roles=[
                {"as": "planner", "name": "echo", "promptTemplate": "plan"},
                {"as": "developer", "name": "echo", "dependsOn": ["planner"], "promptTemplate": "build"},
            ],
        )

        receipt = await driver.start(spec)

        assert receipt.startswith("Error") and "did not approve" in receipt
        assert tool.submitted == []
        [record] = StintStore(tool.stints_root("web:stint")).list()
        assert record.status == "stopped"
        assert record.stop_reason == "the first round was not approved"
        assert record.round(1) is None or not record.round(1).run_id
        assert "nothing left to take up" in await driver.resume(record.stint_id, "web:stint")
        assert tool.submitted == []

    async def test_a_round_no_role_finished_pauses_the_stint_instead_of_costing_a_round(self, tmp_path: Path) -> None:
        """Every node failed, so the round left nothing to build on, and the
        next would hit the same fault. Counted, a stint ran three rounds in two
        seconds. Paused rather than stopped: the round is still un-done on the
        record, which is what lets `resume` put it up again once the cause is
        fixed."""
        announced: list[str] = []
        project = self._project(tmp_path)

        async def announce(run_id: str, text: str, origin: dict) -> None:
            announced.append(text)

        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="broken", command="false")],
            announce=announce,
        )
        tool.set_context("web", "default", "web:stint")
        executor = PlaybookExecutor(dag_tool=tool, workspace=project)
        spec = _spec(
            confirm=False,
            roles=[
                {"as": "planner", "name": "broken", "promptTemplate": "plan", "owns": ["reports/**"]},
                {"as": "developer", "name": "broken", "dependsOn": ["planner"], "promptTemplate": "build"},
            ],
            stop={"maxRounds": 5},
        )

        stint = await executor.execute(spec, {})
        assert stint.kind == "dag", stint.reply
        said = await self._await_announce(announced)

        record = StintStore(tool.stints_root("web:stint")).list()[0]
        assert record.status == "paused"
        assert record.stop_reason == "round 1: no role finished"
        assert [(entry.index, entry.status) for entry in record.rounds] == [(1, "failed")]
        assert "no role finished" in said and "stints resume" in said

    async def test_work_the_person_left_in_the_tree_stops_a_plan_that_would_undo_it(self, tmp_path: Path) -> None:
        """Round one measures its boundary against an empty base, which reads as
        the whole dirty state -- so their file would be graded as the first
        role's stray write, reverted and quarantined. Said before anything runs."""
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])
        (tmp_path / "project" / "mine.md").write_text("half a thought\n", encoding="utf-8")

        stint = await executor.execute(_spec(confirm=False), {})

        assert stint.kind == "questions"
        assert "mine.md" in stint.reply and "Commit or stash" in stint.reply
        assert not StintStore(executor.dag_tool.stints_root("web:stint")).list()
        assert not ProjectGit(tmp_path / "project").branch().startswith("stint/")

    async def test_the_host_s_own_checkpoint_in_the_tree_does_not_read_as_the_person_s_work(
        self, tmp_path: Path
    ) -> None:
        """`.raven/shadow.git/` is written into whatever directory the host is
        working, on the turn that asks for the stint. Counting it as uncommitted
        work refuses every real project at the moment it is asked."""
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])
        shadow = tmp_path / "project" / ".raven" / "shadow.git"
        shadow.mkdir(parents=True)
        (shadow / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        stint = await executor.execute(_spec(confirm=False), {})
        await self._await_announce(announced)

        assert stint.kind == "dag", stint.reply
        record = StintStore(executor.dag_tool.stints_root("web:stint")).list()[0]
        assert record.branch == f"stint/{record.stint_id}"

    async def test_a_repository_with_no_commits_is_refused_before_a_boundary_is_promised(self, tmp_path: Path) -> None:
        """`git init` and nothing else. There is no state to put a stray write
        back to, so the undo the boundary is made of cannot happen -- and the
        checkout step would have failed and quietly carried on in place."""
        project = tmp_path / "fresh"
        project.mkdir()
        ProjectGit(project)._run("init", "--quiet")
        executor = PlaybookExecutor(
            dag_tool=SubAgentDagTool(
                workspace=tmp_path, agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")]
            ),
            workspace=project,
        )

        stint = await executor.execute(_spec(confirm=False), {})

        assert stint.kind == "questions"
        assert "no commits" in stint.reply and "Commit something here first" in stint.reply

    async def test_a_plan_that_enforces_boundaries_needs_a_repository_to_enforce_them_against(
        self, tmp_path: Path
    ) -> None:
        """Refused at the start rather than run to the end looking enforced."""
        bare = tmp_path / "not-a-repo"
        bare.mkdir()
        executor = PlaybookExecutor(
            dag_tool=SubAgentDagTool(workspace=bare, agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")]),
            workspace=bare,
        )

        stint = await executor.execute(_spec(confirm=False), {})

        assert stint.kind == "questions"
        assert "not one" in stint.reply and "owns/appends" in stint.reply

    async def test_every_round_is_charged_to_the_dispatch_budget(self, tmp_path: Path) -> None:
        """One approval must not buy an unmetered run.

        The per-hour budget exists to bound an unattended loop, and a stint is
        that loop: at the declared ceilings one confirmation would otherwise
        authorise orders of magnitude more sub-agent dispatches than the hourly
        allowance the budget is there to hold.
        """
        announced: list[str] = []
        charges: list[str | None] = []
        executor = self._executor(tmp_path, announced, charges)

        await executor.execute(_spec(confirm=False), {})
        await self._await_announce(announced, count=2)

        assert charges == ["web:stint", "web:stint"], "one charge per round, not one per stint"

    async def test_a_round_the_budget_turns_down_pauses_the_stint(self, tmp_path: Path) -> None:
        """A refused round is a round that will be fine an hour from now.

        Stopping the stint would throw away every round it has done over a
        limit that recovers by itself, so it is parked where `stints resume`
        can take it up, and said out loud -- nobody asked for this pause, so
        nobody would otherwise know to resume it.
        """
        announced: list[str] = []
        charges: list[str | None] = []
        executor = self._executor(tmp_path, announced, charges)
        spent = "Error: this session hit its sub-agent dispatch rate limit (30 per hour)."

        def charge(session_key: str | None) -> str | None:
            charges.append(session_key)
            return None if len(charges) == 1 else spent

        executor.dag_tool._charge = charge

        await executor.execute(_spec(confirm=False), {})
        await self._await_announce(announced, count=2)

        record = StintStore(executor.dag_tool.stints_root("web:stint")).list()[0]
        assert record.status == "paused"
        assert "dispatch budget is spent before round 2" in record.stop_reason
        assert [entry.index for entry in record.rounds] == [1], "the refused round opened no entry"
        assert "paused before round 2" in announced[-1]
        assert "stints resume" in announced[-1]

    async def test_a_marker_a_role_reports_ends_the_plan_early(self, tmp_path: Path) -> None:
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])
        spec = _spec(
            roles=[{"as": "planner", "name": "echo", "promptTemplate": "nothing left:\nDONE-FOR-NOW"}],
            stop={"maxRounds": 9, "until": "DONE-FOR-NOW"},
        )

        await executor.execute(spec, {})
        await self._await_announce(announced)

        record = StintStore(executor.dag_tool.stints_root("web:stint")).list()[0]
        assert record.stop_reason == "a role reported 'DONE-FOR-NOW'"
        assert [entry.index for entry in record.rounds] == [1]
        # The echo agent hands the whole prompt back, so this round's output also
        # carries the sentence that told the role the word. It mentions it
        # mid-line, and what ended the stint is the line that is only the word --
        # the false positive the line rule exists for, on real output.
        lines = record.rounds[0].summary.splitlines()
        assert any("DONE-FOR-NOW" in line and line.strip() != "DONE-FOR-NOW" for line in lines), lines
        assert any(line.strip() == "DONE-FOR-NOW" for line in lines)

    async def test_a_round_of_a_plan_cannot_be_replanned_into_another_graph(self, tmp_path: Path) -> None:
        """A replanned run announces nothing and hands nothing on, so the plan
        would wait for a hand-over that never comes and stop without saying so."""
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])

        await executor.execute(_spec(confirm=False), {})
        tool = executor.dag_tool
        run_id = next(iter(tool.active_run_ids()), "")
        assert run_id, "the first round should be in flight"

        refusal = await tool.prepare_replan(
            run_id,
            from_node="game-dev-r01-planner",
            nodes=[],
            reason="try another way",
            session_key="web:stint",
            live={},
        )

        assert isinstance(refusal, str)
        assert "cannot be replanned" in refusal
        await self._await_announce(announced)

    async def test_the_plan_file_carries_the_spec_so_a_later_round_needs_no_library(self, tmp_path: Path) -> None:
        announced: list[str] = []
        executor = self._executor(tmp_path, announced, [])

        await executor.execute(_spec(confirm=False), {})
        await self._await_announce(announced)

        path = next(Path(executor.dag_tool.stints_root("web:stint")).glob("*.json"))
        stored = json.loads(path.read_text(encoding="utf-8"))["spec"]
        assert PlaybookSpec.model_validate(stored).name == "game-dev"


class TestUnwired:
    async def test_a_host_with_no_driver_says_so_rather_than_running_an_empty_graph(self, tmp_path: Path) -> None:
        executor = PlaybookExecutor(dag_tool=object(), workspace=tmp_path)

        stint = await executor.execute(_spec(), {})

        assert stint.kind == "questions"
        assert "no driver" in stint.reply


def test_the_driver_keeps_no_plan_in_memory(tmp_path: Path) -> None:
    """What makes "any process can pick this up" true: the state is the file."""
    driver = StintDriver(object(), stints_root=lambda _key: tmp_path, workspace_for=lambda _key: tmp_path)

    assert not [name for name in vars(driver) if "record" in name or "plan_" in name]


def test_the_specification_link_is_carried_into_a_checkout_of_the_run_s_own(tmp_path: Path) -> None:
    """The layout spells the link `<path> -> <target>` in what it wrote, and the
    carry looked that string up as a file: the one document every role is told
    to read was the one thing a worktree stint did not get."""
    import os

    from raven.playbook.stint import _carry_layout
    from raven.stint.setup import Layout

    project, tree = tmp_path / "project", tmp_path / "tree"
    for root in (project, tree):
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "PRD.md").write_text("# what it is\n", encoding="utf-8")
    (project / ".stint").mkdir()
    (project / ".stint" / "planner.md").write_text("orders\n", encoding="utf-8")
    os.symlink("../docs/PRD.md", project / ".stint" / "SPEC.md")

    _carry_layout(project, tree, Layout(wrote=[".stint/SPEC.md -> ../docs/PRD.md", ".stint/planner.md"]))

    carried = tree / ".stint" / "SPEC.md"
    assert carried.is_symlink() and carried.resolve() == (tree / "docs" / "PRD.md").resolve()
    assert (tree / ".stint" / "planner.md").read_text(encoding="utf-8") == "orders\n"


class TestBoundaries:
    """The three layers a round adds to a graph: measure, undo, hand back."""

    @staticmethod
    def _context(tmp_path: Path, spec: PlaybookSpec) -> Any:
        from raven.playbook.stint_round import RoundContext
        from raven.stint.record import StintRecord, StintStore

        tree = tmp_path / "project"
        tree.mkdir(exist_ok=True)
        (tree / "src").mkdir(exist_ok=True)
        (tree / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        store = StintStore(tmp_path / "stints")
        record = StintRecord(
            stint_id="stint-test",
            playbook=spec.name,
            spec=spec.model_dump(by_alias=True),
            workdir=str(tree),
            round_index=1,
        )
        store.write(record)
        return RoundContext(
            spec=spec,
            record=record,
            store=store,
            index=1,
            workdir=tree,
            artifacts_dir=store.artifacts_for(record.stint_id),
        )

    @staticmethod
    def _node(node_id: str) -> Any:
        from raven.agent.subagent.dag_graph import DagNodeSpec

        return DagNodeSpec(id=node_id, subagent="echo", node_summary="s", prompt_template="p")

    async def test_a_role_that_writes_where_it_may_not_has_it_undone(self, tmp_path: Path) -> None:
        spec = _spec(
            roles=[
                {"as": "dev", "name": "echo", "promptTemplate": "work", "owns": ["src/**"], "maxHandbacks": 0},
                {"as": "qa", "name": "echo", "promptTemplate": "check", "owns": ["reports/**"]},
            ]
        )
        context = self._context(tmp_path, spec)
        node = self._node("game-dev-r01-dev")
        await context.node_started(node.id)

        (context.workdir / "reports").mkdir()
        (context.workdir / "reports" / "qa.md").write_text("not mine\n", encoding="utf-8")
        verdict = await context.judge(node=node)

        assert verdict.accomplished, "past its budget the round moves on rather than failing the node"
        assert not (context.workdir / "reports" / "qa.md").exists()
        violations = context.record.round(1).violations
        assert any("may not write" in note for note in violations), violations

    async def test_a_resumed_round_s_roles_are_still_judged(self, tmp_path: Path) -> None:
        """A round taken up again runs its nodes as `r01x1-<role>`. The judge
        read the role off the first attempt's spelling and found none, so on
        every resumed round a stray write stayed, no check ran and nothing was
        committed -- while the round reported completed (measured 2026-09-20)."""
        spec = _spec(
            roles=[
                {"as": "dev", "name": "echo", "promptTemplate": "work", "owns": ["src/**"], "maxHandbacks": 0},
                {"as": "qa", "name": "echo", "promptTemplate": "check", "owns": ["reports/**"]},
            ]
        )
        context = self._context(tmp_path, spec)
        node = self._node("game-dev-r01x2-dev")
        await context.node_started(node.id)

        (context.workdir / "reports").mkdir()
        (context.workdir / "reports" / "qa.md").write_text("not mine\n", encoding="utf-8")
        await context.judge(node=node)

        assert not (context.workdir / "reports" / "qa.md").exists(), "the boundary held on the second attempt too"
        assert any("may not write" in note for note in context.record.round(1).violations)

    async def test_what_the_run_left_uncommitted_is_nobody_s_only_until_a_round_commits_it(
        self, tmp_path: Path
    ) -> None:
        """The standing orders are exempt from the first role's boundary because
        the setup pass left them uncommitted and there is no base to measure
        them from. The first role's commit takes them with it, and an exemption
        that outlived that let any later role rewrite any other's orders unseen."""
        spec = _spec(
            roles=[
                {"as": "dev", "name": "echo", "promptTemplate": "work", "owns": ["src/**"], "maxHandbacks": 0},
                {"as": "qa", "name": "echo", "promptTemplate": "check", "owns": ["reports/**"], "maxHandbacks": 0},
            ]
        )
        context = self._context(tmp_path, spec)
        context.git()
        orders = context.workdir / ".stint" / "qa.md"
        orders.parent.mkdir()
        orders.write_text("judge fairly\n", encoding="utf-8")
        context.record.untracked_at_start = [".stint/qa.md"]

        dev = self._node("game-dev-r01-dev")
        await context.node_started(dev.id)
        await context.judge(node=dev)
        assert orders.read_text(encoding="utf-8") == "judge fairly\n", "round one's first role left it alone"
        assert not context.record.round(1).violations

        qa = self._node("game-dev-r01-qa")
        await context.node_started(qa.id)
        orders.write_text("judge leniently\n", encoding="utf-8")
        await context.judge(node=qa)

        assert orders.read_text(encoding="utf-8") == "judge fairly\n"
        assert any(".stint/qa.md" in note for note in context.record.round(1).violations)

    async def test_a_failing_check_is_handed_back_to_the_role_not_to_a_person(self, tmp_path: Path) -> None:
        """There is nobody to ask on an unattended run, and the judge already
        holds the output that says what went wrong."""
        spec = _spec(
            roles=[
                {"as": "dev", "name": "echo", "promptTemplate": "work", "verifyAfter": ["build"], "maxHandbacks": 1}
            ],
            verify=[{"name": "build", "run": "exit 3"}],
        )
        context = self._context(tmp_path, spec)
        node = self._node("game-dev-r01-dev")

        verdict = await context.judge(node=node)

        assert verdict.accomplished is False
        assert verdict.follow_up, "a follow-up is what makes it a retry rather than a question"
        assert "`build`" in verdict.follow_up
        assert "exit 3" in verdict.follow_up

    async def test_once_the_budget_is_spent_the_round_moves_on_with_the_failure_written_down(
        self, tmp_path: Path
    ) -> None:
        """Failing the node would cascade a skip through the rest of the round."""
        spec = _spec(
            roles=[
                {"as": "dev", "name": "echo", "promptTemplate": "work", "verifyAfter": ["build"], "maxHandbacks": 1}
            ],
            verify=[{"name": "build", "run": "exit 3"}],
        )
        context = self._context(tmp_path, spec)
        node = self._node("game-dev-r01-dev")

        first = await context.judge(node=node)
        second = await context.judge(node=node)

        assert first.accomplished is False
        assert second.accomplished is True
        entry = context.record.round(1)
        assert [row["name"] for row in entry.verify] == ["build"]
        assert entry.verify[0]["status"] == "failed"
        assert any("still failing when the round moved on" in note for note in entry.violations)

    async def test_a_passing_check_says_nothing_and_costs_no_handback(self, tmp_path: Path) -> None:
        spec = _spec(
            roles=[{"as": "dev", "name": "echo", "promptTemplate": "work", "verifyAfter": ["build"]}],
            verify=[{"name": "build", "run": "true"}],
        )
        context = self._context(tmp_path, spec)

        verdict = await context.judge(node=self._node("game-dev-r01-dev"))

        assert verdict.accomplished is True
        assert not verdict.follow_up
        assert context.record.round(1).verify[0]["status"] == "ok"

    async def test_a_plan_whose_roles_claim_nothing_takes_no_snapshots(self, tmp_path: Path) -> None:
        """What keeps the cost of all this off a stint that is not asking for it."""
        spec = _spec(roles=[{"as": "dev", "name": "echo", "promptTemplate": "work"}])

        assert self._context(tmp_path, spec).enforcing is False
        assert self._context(tmp_path, _spec()).enforcing is True

    async def test_a_question_nobody_answered_is_filed(self, tmp_path: Path) -> None:
        context = self._context(tmp_path, _spec())

        await context.record_question("game-dev-r01-planner", "which of the two should it be?")

        [question] = context.store.read("stint-test").questions
        assert question["role"] == "planner"
        assert question["round"] == 1
        assert "which of the two" in question["text"]

    async def test_a_suspended_node_is_abandoned_by_the_driver_and_written_down(self, tmp_path: Path) -> None:
        """A round dispatched between turns has nobody at the desk.

        Left alone the node waits out the whole adjudication timeout and fails
        anyway, so the stint says the same thing sooner, in the vocabulary the
        desk already has. The node is `failed` either way -- what the stint adds
        is the record, which is what the next round and the person reading
        between rounds actually need.
        """
        from raven.agent.subagent.dag_adjudication import ABANDON

        context = self._context(tmp_path, _spec())

        decision, message = await context.adjudicate("game-dev-r01-planner", "which of the two should it be?")

        assert decision == ABANDON
        assert "recorded the question" in message
        [question] = context.store.read("stint-test").questions
        assert "which of the two" in question["text"]


class _FakeTool:
    """Enough of the graph tool for the driver: what it submitted, and what ran before."""

    def __init__(self, root: Path, *, readable: set[str] | None = None, live: list[str] | None = None) -> None:
        self.root = root
        self.readable = readable or set()
        self._live = live or []
        self.submitted: list[list[dict[str, Any]]] = []
        self.summaries: list[str] = []

    def stints_root(self, _session_key: str | None = None) -> Path:
        return self.root

    def turn_origin(self) -> dict[str, str]:
        return {"channel": "web", "chat_id": "default", "session_key": "web:stint"}

    def active_run_ids(self) -> list[str]:
        return list(self._live)

    async def session_nodes(self, _session_key: str | None = None) -> Any:
        from raven.agent.subagent.dag_store import SessionNodes

        return SessionNodes(
            owner={node: "run-old" for node in self.readable},
            state={node: "completed" for node in self.readable},
            has_output=dict.fromkeys(self.readable, True),
        )

    async def run_round(self, nodes: list[dict[str, Any]], **kwargs: Any) -> str:
        if getattr(self, "refuse", ""):
            return str(self.refuse)
        if getattr(self, "deny", False) and kwargs.get("confirm"):
            from raven.agent.subagent.prompt_errors import RoundNotApprovedError

            raise RoundNotApprovedError("the person did not approve the round")
        self.submitted.append(nodes)
        self.summaries.append(str(kwargs.get("task_summary") or ""))
        return f"DAG run run-{len(self.submitted)} started in the background ({len(nodes)} nodes)."


class TestWhatTheReadsListIsFor:
    def test_the_reads_list_does_not_read_as_a_third_grade_of_permission(self) -> None:
        """Owned, append-only, and then a third list in the same shape reads as a
        third permission. It is not one: unless read enforcement is hard it
        points, and a role that took it for a fence would stop opening the file
        it needed."""
        told = compile_round(_spec(), 1)[0]["prompt_template"]

        assert "not a fence" in told
        assert "says nothing about what you may write" in told
        # and the closing sentence is about writing, not about reading
        assert "is another role's to *write*" in told

    def test_a_role_whose_reads_are_enforced_is_told_that_instead(self) -> None:
        spec = _spec(
            roles=[
                {
                    "as": "planner",
                    "name": "echo",
                    "promptTemplate": "hi",
                    "owns": ["src/**"],
                    "reads": ["SPEC.md"],
                    "enforce": {"read": "hard"},
                }
            ]
        )

        told = compile_round(spec, 1)[0]["prompt_template"]

        assert "The only paths you may read" in told
        assert "not a fence" not in told


class TestWhereTheRoleIsStanding:
    def test_a_role_in_a_plan_s_checkout_is_told_that_it_is_the_project(self) -> None:
        """A role that runs `cat .git` in a worktree reads a gitdir pointing
        somewhere else, concludes the real project is elsewhere, and spends the
        round crawling the machine for one it was already inside."""
        from raven.playbook.stint_prompt import where_section

        told = compile_round(_spec(), 1, where=where_section("/stints/p1/tree", True))[0]["prompt_template"]

        assert "/stints/p1/tree" in told
        assert "is* the project" in told or "is the project" in told

    def test_a_plan_working_in_place_is_not_told_it_has_a_checkout(self) -> None:
        from raven.playbook.stint_prompt import where_section

        told = compile_round(_spec(), 1, where=where_section("/srv/game", False))[0]["prompt_template"]

        assert "/srv/game" in told
        assert "checkout of the project made for this stint" not in told

    def test_a_round_compiled_without_a_directory_says_nothing_about_one(self) -> None:
        """The slot is filled from a stint record; a caller compiling a round on
        its own has no directory to name and must not get an empty claim."""
        told = compile_round(_spec(), 1)[0]["prompt_template"]

        assert "project" not in told.split("## What is yours this round")[-1].split("These paths")[0]


class TestWhatIsHandedBack:
    """A finding is handed back saying where it came from, because a role acts on that."""

    @staticmethod
    def _report(path: str, note: str):
        from raven.stint.enforce import EnforceReport

        return EnforceReport(stray=(path,), quarantined=(path,), violations=(note,))

    @staticmethod
    def _failure():
        from raven.stint.verify import CheckResult

        return CheckResult(name="build", command="make", returncode=1, duration_sec=2.0, stderr_tail="boom")

    def test_a_stray_write_is_not_reported_as_a_command_s_answer(self) -> None:
        """It is a diff of the tree against a declaration, and no command ran.
        Told otherwise, a role goes looking for the command that failed -- which
        is a whole round spent debugging tooling that is working."""
        from raven.playbook.stint_round import _complaint

        said = _complaint(
            [], self._report(".stint/backlog.json", "planner wrote 1 path(s) it may not write: .stint/backlog.json")
        )

        assert ".stint/backlog.json" in said
        assert "commands' own answer" not in said, said
        assert "no check has failed" in said

    def test_a_failed_check_is_still_reported_as_the_command_s_own_answer(self) -> None:
        from raven.playbook.stint_round import _complaint

        said = _complaint([self._failure()], None)

        assert "boom" in said
        assert "commands' own answer" in said

    def test_both_findings_are_said_separately_rather_than_under_one_claim(self) -> None:
        from raven.playbook.stint_round import _complaint

        said = _complaint(
            [self._failure()], self._report("src/x.py", "planner wrote 1 path(s) it may not write: src/x.py")
        )

        assert said.index("src/x.py") < said.index("boom"), "the undone work comes first"
        assert "no check has failed" in said
        assert "commands' own answer" in said


class TestAScreenForTheChecks:
    """H* gap 2: a check that renders needs somewhere to render."""

    async def test_the_checks_do_not_run_on_the_event_loop(self, tmp_path: Path, monkeypatch) -> None:
        """`judge` is awaited by the graph runner on the host's own loop, and a
        check is a real command with a default timeout of half an hour. Run
        inline, a five-minute test suite is five minutes in which no other node
        advances and no message is answered."""
        import asyncio

        import raven.playbook.stint_round as mod

        spec = _spec(
            verify=[{"name": "build", "run": "true"}],
            roles=[
                {
                    "as": "developer",
                    "name": "echo",
                    "promptTemplate": "do it",
                    "owns": ["src/**"],
                    "verifyAfter": ["build"],
                }
            ],
        )
        context = TestBoundaries._context(tmp_path, spec)
        loop = asyncio.get_running_loop()
        ran_on: list[object] = []

        def watch(*args, **kwargs):
            ran_on.append(asyncio.get_event_loop_policy())
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                ran_on.append("off the loop")
            return []

        monkeypatch.setattr(mod, "run_checks", watch)

        await context._verify(spec.roles[0])

        assert "off the loop" in ran_on, "run_checks was called on the running loop"
        assert loop.is_running()

    async def test_a_check_that_needs_a_screen_and_finds_none_is_skipped_not_failed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Failing it would hand the role a defect in the machine, every round,
        forever. Skipped is what actually happened."""
        import raven.playbook.stint_round as mod
        from raven.stint.verify import Display

        spec = _spec(
            verify=[{"name": "render", "run": "true", "needsDisplay": True}],
            roles=[
                {
                    "as": "developer",
                    "name": "echo",
                    "promptTemplate": "do it",
                    "owns": ["src/**"],
                    "verifyAfter": ["render"],
                }
            ],
        )
        context = TestBoundaries._context(tmp_path, spec)
        nowhere = Display(name="", provider="xvfb")
        monkeypatch.setattr(mod, "resolve_display", lambda *a, **k: nowhere)
        monkeypatch.setattr(mod, "start_display", lambda display, **k: (display, None))

        failures = await context._verify(spec.roles[0])

        assert failures == [], "a skipped check is not handed back"
        assert context._results["render"].status == "skipped", context._results["render"]

    def test_a_check_that_does_not_render_opens_no_screen(self, tmp_path: Path) -> None:
        """What keeps the cost off every stint that never asked for one."""
        spec = _spec(verify=[{"name": "build", "run": "true"}])
        context = TestBoundaries._context(tmp_path, spec)

        assert context._screen([CheckSpec(name="build", command="true")]) == (None, None)


class TestWhereAGraphCameFrom:
    """A round's graph has no tool call, so its progress has to name the stint."""

    def test_a_round_s_progress_names_the_plan_and_the_round(self) -> None:
        """A reader given only a tool call id has nothing to hang a round's graph
        on -- it was dispatched between turns, by the stint, not by a call."""
        import asyncio

        from raven.agent.subagent.dag_tool import SubAgentDagTool
        from raven.stint.record import StintRef

        seen: list[tuple[str, dict]] = []

        async def sink(conversation: str, name: str, value: dict) -> None:
            seen.append((name, value))

        tool = SubAgentDagTool(workspace=tmpdir(), agents=[])
        tool.set_progress_sink(sink)
        emit = tool._emitter("web:stint", None, StintRef(stint_id="stint-x", round_index=3))

        asyncio.run(emit("dag_run_started", {"run_id": "r1"}))

        assert seen[0][1]["stint_id"] == "stint-x"
        assert seen[0][1]["round_index"] == 3

    def test_an_ordinary_run_says_nothing_new(self) -> None:
        """What keeps a reader that does not know the field seeing what it saw."""
        import asyncio

        from raven.agent.subagent.dag_tool import SubAgentDagTool

        seen: list[dict] = []

        async def sink(conversation: str, name: str, value: dict) -> None:
            seen.append(value)

        tool = SubAgentDagTool(workspace=tmpdir(), agents=[])
        tool.set_progress_sink(sink)

        asyncio.run(tool._emitter("web:stint", "call-7")("dag_run_started", {"run_id": "r1"}))

        assert seen[0] == {"run_id": "r1", "tool_call_id": "call-7"}


class TestWhoseNameIsOnTheWork:
    async def test_a_commit_a_role_makes_itself_is_not_attributed_to_the_person(self, tmp_path: Path) -> None:
        """A role given a shell commits its own work, and then the history says
        the person who started the stint wrote it -- six such commits, measured
        2026-09-17. The checkout claims a name so anyone committing in it gets
        that one."""
        import subprocess

        project = tmp_path / "game"
        project.mkdir()
        git = ProjectGit(project)
        git.ensure_repo()
        (project / "README.md").write_text("start\n", encoding="utf-8")
        git.commit("start")

        tree = tmp_path / "tree"
        git.worktree_add(tree, "stint/x", git.head())
        (tree / "README.md").write_text("a role wrote this\n", encoding="utf-8")
        # check=True, because a swallowed failure makes this test pass for the
        # wrong reason: the log then still names the bootstrap commit, whose
        # author is the very name being asserted.
        subprocess.run(["git", "-C", str(tree), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tree), "commit", "-m", "role did it"], capture_output=True, check=True)

        author = subprocess.run(
            ["git", "-C", str(tree), "log", "-1", "--format=%an"], capture_output=True, text=True, check=False
        ).stdout.strip()

        assert author == git.author_name, author

    def test_the_person_s_own_tree_keeps_their_own_name(self, tmp_path: Path) -> None:
        """A linked worktree shares the repository's config, so the obvious way
        to do this renames the person in their own tree too."""
        import subprocess

        project = tmp_path / "game"
        project.mkdir()
        git = ProjectGit(project)
        git.ensure_repo()
        # A complete local identity, and commits that fail loudly. Name alone is
        # not an identity: on a machine with no ambient email the commits abort
        # with `Author identity unknown`, the log still names the bootstrap
        # commit, and the assertion below fails for a reason that has nothing to
        # do with what it is testing.
        subprocess.run(["git", "-C", str(project), "config", "user.name", "A Person"], check=True)
        subprocess.run(["git", "-C", str(project), "config", "user.email", "person@example.com"], check=True)
        (project / "README.md").write_text("start\n", encoding="utf-8")
        git._run("add", "--all")
        subprocess.run(["git", "-C", str(project), "commit", "-qm", "start"], check=True)
        git.worktree_add(tmp_path / "tree", "stint/x", git.head())

        (project / "README.md").write_text("the person wrote this\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-qm", "mine"], check=True)
        author = subprocess.run(
            ["git", "-C", str(project), "log", "-1", "--format=%an"], capture_output=True, text=True, check=False
        ).stdout.strip()

        assert author == "A Person", author


class TestAPlanNobodyIsAdvancing:
    @staticmethod
    def _went_quiet(store, stint_id: str, seconds: float = 3600.0) -> None:
        """Backdate the stamp, which is what a host that died leaves behind.

        It stops beating and never writes again, so the stamp stays where the
        last ordinary write put it. Written straight into the file rather than
        through ``write``, which would stamp it fresh on the way out.
        """
        import json
        import time

        path = store.path_for(stint_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["touched_at_ms"] = int((time.time() - seconds) * 1000)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    async def test_a_plan_whose_host_is_gone_stops_claiming_it_is_running(self, tmp_path: Path) -> None:
        """A host that dies mid-round writes nothing on its way out, so the file
        says `running` forever and `stint list` reports a corpse as work."""
        tool = _FakeTool(tmp_path / "stints", live=[])
        project = tmp_path / "game"
        project.mkdir()
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: project)
        await driver.start(_spec(confirm=False, roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}]))
        store = driver.store_for("web:stint")
        self._went_quiet(store, store.list()[0].stint_id)

        adrift = driver.adrift("web:stint")

        assert [record.stint_id for record in adrift] == [store.list()[0].stint_id]
        assert store.list()[0].status == "interrupted"

    async def test_a_plan_another_host_is_beating_for_is_left_alone(self, tmp_path: Path) -> None:
        """The case the stamp exists for. `active_run_ids` is this process's own
        memory, so a second host saw an empty one and would have called a working
        stint dead -- and a person who then resumed it would have two hosts on one
        checkout. A fresh stamp is the only thing a second process can read."""
        tool = _FakeTool(tmp_path / "stints", live=[])
        project = tmp_path / "game"
        project.mkdir()
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: project)
        await driver.start(_spec(confirm=False, roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}]))

        # Nothing backdated: the record was written a moment ago, exactly as a
        # live holder's beat leaves it, and this driver knows nothing of its run.
        assert driver.adrift("web:stint") == []
        assert driver.store_for("web:stint").list()[0].status == "running"

    async def test_a_plan_whose_round_is_still_in_flight_is_left_alone(self, tmp_path: Path) -> None:
        """The whole point is picking up what nobody is advancing, not racing
        whoever still is."""
        project = tmp_path / "game"
        project.mkdir()
        tool = _FakeTool(tmp_path / "stints")
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: project)
        await driver.start(_spec(confirm=False, roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}]))
        store = driver.store_for("web:stint")
        record = store.list()[0]
        self._went_quiet(store, record.stint_id)
        tool._live = [record.round(1).run_id]

        assert driver.adrift("web:stint") == []
        assert driver.store_for("web:stint").list()[0].status == "running"

    async def test_noticing_one_does_not_start_it(self, tmp_path: Path) -> None:
        """Opening a window is not a request to spend hours and money."""
        project = tmp_path / "game"
        project.mkdir()
        tool = _FakeTool(tmp_path / "stints", live=[])
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: project)
        await driver.start(_spec(confirm=False, roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}]))
        store = driver.store_for("web:stint")
        self._went_quiet(store, store.list()[0].stint_id)
        submitted = len(tool.submitted)

        driver.adrift("web:stint")

        assert len(tool.submitted) == submitted, "nothing was dispatched"


class TestWhatAResumedRoundIsStillHeldTo:
    async def test_a_resumed_round_keeps_its_charters(self, tmp_path: Path) -> None:
        """A round taken up again compiles its nodes under an attempt suffix, and
        the tool wraps a backend only on an exact node-id match. Keyed for
        attempt nought the charters reach nothing, and every role still pending
        after a restart runs with no charter -- the layer that refuses a stray
        write before it lands, gone, and gone quietly."""
        tool = _FakeTool(tmp_path / "stints", live=[])
        project = tmp_path / "game"
        project.mkdir()
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: project)
        # A declared boundary needs a repository to undo a stray write in, and
        # the charter is the layer this test is about, so the role declares one.
        subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
        (project / "seed.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(project), "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-qm", "seed"],
            check=True,
        )
        spec = _spec(
            confirm=False,
            roles=[
                {
                    "as": "planner",
                    "name": "echo",
                    "promptTemplate": "hi",
                    "owns": ["reports/**"],
                    "playbook": {"capability": {"tools": ["read_file", "write_file"]}},
                }
            ],
        )
        started = await driver.start(spec)
        assert "Error" not in started, started
        store = driver.store_for("web:stint")
        record = store.list()[0]
        record.status = "interrupted"
        store.write(record)

        await driver.resume(record.stint_id, "web:stint")

        again = store.read(record.stint_id)
        keys = set(driver.hooks(again.ref(1))["charters"])
        ids = {node["id"] for node in tool.submitted[-1]}
        assert keys == ids, f"charters {sorted(keys)} do not name the nodes {sorted(ids)}"
        assert any("x1" in node_id for node_id in ids), "the resumed round did not take an attempt suffix"


class TestWhatARoundsHandbackBudgetMeans:
    """`maxHandbacks: 0` is a number a role may declare, not an absent one."""

    @staticmethod
    async def _handbacks_reaching_the_runner(tmp_path: Path, monkeypatch, declared: int) -> list[int]:
        from raven.agent.subagent import dag_tool as tool_mod
        from raven.agent.subagent.dag_tool import SubAgentDagTool

        seen: list[int] = []

        async def _fake_run_dag(spec, **kwargs):
            seen.append(kwargs.get("max_continuations"))
            raise RuntimeError("far enough")

        monkeypatch.setattr(tool_mod, "run_dag", _fake_run_dag)

        class _Driver:
            async def advance(self, *_args, **_kwargs):
                return None

            def hooks(self, _ref):
                return {"max_continuations": declared}

        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            stint_driver=_Driver(),
        )
        tool.set_context("web", "default", "web:stint")

        receipt = await tool.run_round(
            [{"id": "a", "subagent": "echo", "node_summary": "s", "prompt_template": "p"}],
            task_summary="one round",
            stint=StintRef(stint_id="stint-1", round_index=1, session_key="web:stint"),
        )
        assert "Error" not in str(receipt), receipt
        await asyncio.gather(*list(tool._runs.values()), return_exceptions=True)
        return seen

    async def test_a_declared_zero_is_not_read_as_no_answer_at_all(self, tmp_path: Path, monkeypatch) -> None:
        """`maxHandbacks` is bounded `ge=0`, so zero is a legal declaration and
        it means something: record the failed check and carry on, never hand
        back. Read as absence it becomes the default instead, and the role
        retries twice -- each retry a real sub-agent dispatch it declared it did
        not want."""
        assert await self._handbacks_reaching_the_runner(tmp_path, monkeypatch, 0) == [0]

    async def test_a_declared_budget_is_what_the_round_gets(self, tmp_path: Path, monkeypatch) -> None:
        assert await self._handbacks_reaching_the_runner(tmp_path, monkeypatch, 3) == [3]


class TestWhoAnswersARoundsSuspendedNode:
    """The desk is the same one a person uses; for a stint the driver is at it."""

    @staticmethod
    def _tool(tmp_path: Path):
        from raven.agent.subagent.dag_tool import SubAgentDagTool

        tool = SubAgentDagTool(workspace=tmp_path, agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")])
        tool.set_context("web", "default", "web:stint")
        return tool

    async def test_the_driver_answers_and_the_host_is_not_woken(self, tmp_path: Path) -> None:
        from raven.agent.subagent.dag_adjudication import ABANDON, AdjudicationDesk

        woken: list[str] = []

        async def host(run_id, node_id, report, origin, *, awaiting_decision, informational=False):
            woken.append(node_id)

        async def adjudicate(node_id: str, report: str) -> tuple[str, str]:
            return ABANDON, f"the stint decided about {node_id}"

        desk = AdjudicationDesk()
        desk.open("a")
        announce = self._tool(tmp_path)._answered_by(desk, adjudicate, host)

        await announce("run-1", "a", "a did not do it", {}, awaiting_decision=True)

        assert woken == [], "a round between turns has no main agent to wake"
        assert desk.take("a").decision == ABANDON

    async def test_a_stall_notice_still_reaches_the_host(self, tmp_path: Path) -> None:
        """Only the decision is taken over. A notice nothing is waiting on is
        news for whoever is watching, and the driver has no use for it."""
        from raven.agent.subagent.dag_adjudication import AdjudicationDesk

        woken: list[str] = []

        async def host(run_id, node_id, report, origin, *, awaiting_decision, informational=False):
            woken.append(node_id)

        async def adjudicate(node_id: str, report: str) -> tuple[str, str]:
            raise AssertionError("a stall notice is not a decision")

        announce = self._tool(tmp_path)._answered_by(AdjudicationDesk(), adjudicate, host)

        await announce("run-1", "a", "no sign of life", {}, awaiting_decision=False, informational=True)

        assert woken == ["a"]

    async def test_a_driver_that_cannot_decide_abandons_rather_than_hanging(self, tmp_path: Path) -> None:
        from raven.agent.subagent.dag_adjudication import ABANDON, AdjudicationDesk

        async def adjudicate(node_id: str, report: str) -> tuple[str, str]:
            raise RuntimeError("the record is gone")

        desk = AdjudicationDesk()
        desk.open("a")
        announce = self._tool(tmp_path)._answered_by(desk, adjudicate, None)

        await announce("run-1", "a", "a did not do it", {}, awaiting_decision=True)

        assert desk.take("a").decision == ABANDON


class TestSayingItIsStillHeld:
    """The beat, and the guard it makes possible."""

    @staticmethod
    def _driver(tmp_path: Path, tool: "_FakeTool") -> StintDriver:
        project = tmp_path / "game"
        project.mkdir(exist_ok=True)
        return StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: project)

    @staticmethod
    def _one_role():
        return _spec(confirm=False, roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}])

    async def test_a_dispatched_round_leaves_a_beat_behind(self, tmp_path: Path) -> None:
        """A round can run for hours between two ordinary writes, so the stamp
        has to move on its own or a reader five minutes later calls it dead."""
        tool = _FakeTool(tmp_path / "stints", live=[])
        driver = self._driver(tmp_path, tool)

        await driver.start(self._one_role())

        stint_id = driver.store_for("web:stint").list()[0].stint_id
        assert stint_id in driver._beats, "nothing is saying this process still holds it"
        assert not driver._beats[stint_id].done()
        driver._stop_beat(stint_id)

    async def test_the_beat_stops_when_the_round_hands_over(self, tmp_path: Path) -> None:
        """A beat outliving the round it speaks for would keep a stint nothing is
        advancing looking held."""
        tool = _FakeTool(tmp_path / "stints", live=[])
        driver = self._driver(tmp_path, tool)
        await driver.start(self._one_role())
        record = driver.store_for("web:stint").list()[0]

        await driver.advance(record.ref(1), record.round(1).run_id, "done", stopped=True)

        assert record.stint_id not in driver._beats

    async def test_resume_refuses_a_stint_something_is_still_touching(self, tmp_path: Path) -> None:
        """The guard that crosses a process boundary. Two graphs on one checkout
        is the same roles, the same paths, each judged against a baseline the
        other is moving -- and a person reaching for `resume` is how it happens."""
        tool = _FakeTool(tmp_path / "stints", live=[])
        driver = self._driver(tmp_path, tool)
        await driver.start(self._one_role())
        record = driver.store_for("web:stint").list()[0]
        submitted = len(tool.submitted)

        answer = await driver.resume(record.stint_id, "web:stint")

        assert "still working it" in answer
        assert len(tool.submitted) == submitted, "nothing was dispatched"

    async def test_resume_refuses_a_round_this_process_is_working_even_with_a_cold_stamp(self, tmp_path: Path) -> None:
        """The second guard, for when the first cannot help: a beat that could not
        write leaves a stale stamp on a round that is genuinely going here. The
        file cannot say that -- only the process holding the round knows."""
        import json
        import time

        tool = _FakeTool(tmp_path / "stints", live=[])
        driver = self._driver(tmp_path, tool)
        await driver.start(self._one_role())
        store = driver.store_for("web:stint")
        record = store.list()[0]
        tool._live = [record.round(1).run_id]
        path = store.path_for(record.stint_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["touched_at_ms"] = int((time.time() - 3600) * 1000)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        submitted = len(tool.submitted)

        answer = await driver.resume(record.stint_id, "web:stint")

        assert "working round 1 here right now" in answer
        assert len(tool.submitted) == submitted, "nothing was dispatched"


class TestASecondPlanOnOneProject:
    """What stops a person who came back tomorrow from silently redoing a week."""

    @staticmethod
    def _driver(tmp_path: Path, tool: "_FakeTool", project: Path) -> StintDriver:
        return StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _key: project)

    @staticmethod
    def _plain():
        return _spec(confirm=False, roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}])

    async def test_a_second_plan_on_the_same_project_is_refused_with_the_first_one_named(self, tmp_path: Path) -> None:
        """Two stints on one repository work from two bases, neither holding the
        other's rounds, so the second silently redoes the first."""
        project = tmp_path / "game"
        project.mkdir()
        tool = _FakeTool(tmp_path / "stints")
        driver = self._driver(tmp_path, tool, project)
        await driver.start(self._plain())
        first = driver.store_for("web:stint").list()[0]

        refused = await driver.start(self._plain())

        assert refused.startswith("Error"), refused
        assert first.stint_id in refused
        assert "stints stop" in refused and "takes it up" in refused
        assert "stints resume" not in refused, "a second engine in the caller's process is not the way out"
        assert len(driver.store_for("web:stint").list()) == 1, "nothing was written for the second"
        assert len(tool.submitted) == 1, "and nothing was dispatched"

    async def test_another_playbook_on_the_same_project_is_refused_too(self, tmp_path: Path) -> None:
        """What cannot be had twice is the repository, not the playbook. Two
        near-identical playbooks went past a gate that paired the two, and both
        cut a worktree from the same HEAD."""
        project = tmp_path / "game"
        project.mkdir()
        tool = _FakeTool(tmp_path / "stints")
        driver = self._driver(tmp_path, tool, project)
        await driver.start(self._plain())

        other = _spec(
            confirm=False,
            name="rounds-too",
            roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}],
        )
        refused = await driver.start(other)

        assert refused.startswith("Error"), refused
        assert "running game-dev" in refused, refused
        assert len(tool.submitted) == 1

    async def test_a_plan_that_ended_does_not_block_the_next_one(self, tmp_path: Path) -> None:
        project = tmp_path / "game"
        project.mkdir()
        tool = _FakeTool(tmp_path / "stints")
        driver = self._driver(tmp_path, tool, project)
        await driver.start(self._plain())
        store = driver.store_for("web:stint")
        record = store.list()[0]
        record.status = "finished"
        store.write(record)

        receipt = await driver.start(self._plain())

        assert not receipt.startswith("Error"), receipt
        assert len(store.list()) == 2

    async def test_another_project_is_another_plan(self, tmp_path: Path) -> None:
        """The gate is per project, not per playbook: one playbook run against
        two repositories is two jobs, not a collision."""
        tool = _FakeTool(tmp_path / "stints")
        for name in ("game", "docs"):
            (tmp_path / name).mkdir()
            receipt = await self._driver(tmp_path, tool, tmp_path / name).start(self._plain())
            assert not receipt.startswith("Error"), receipt

        assert (
            len(
                StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _k: tmp_path)
                .store_for("web:stint")
                .list()
            )
            == 2
        )


class TestWhereAPlanWorks:
    async def test_a_plan_works_the_directory_its_own_conversation_works(self, tmp_path: Path) -> None:
        """One driver serves every conversation on the host. A stint that took
        the host's own directory would open a checkout of the wrong repository
        -- and on a gateway, of whatever directory it was launched from."""
        projects = {"web:stint": tmp_path / "game", "web:other": tmp_path / "docs"}
        for path in projects.values():
            path.mkdir()
        tool = _FakeTool(tmp_path / "stints")
        driver = StintDriver(
            tool,
            stints_root=tool.stints_root,
            workspace_for=lambda key: projects[key or ""],
        )

        plain = _spec(confirm=False, roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}])
        await driver.start(plain)

        record = driver.store_for("web:stint").list()[0]
        assert Path(record.workdir) == projects["web:stint"]

    async def test_a_plan_that_enforces_a_boundary_refuses_a_directory_that_is_not_a_repository(
        self, tmp_path: Path
    ) -> None:
        """Said about the project it was pointed at, because that is the one the
        person has to go and initialise."""
        project = tmp_path / "game"
        project.mkdir()
        tool = _FakeTool(tmp_path / "stints")
        driver = StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _key: project)

        problem = await driver.start(_spec(confirm=False))

        assert str(project) in problem
        assert not tool.submitted


class TestResume:
    @staticmethod
    def _driver(tmp_path: Path, tool: _FakeTool) -> StintDriver:
        return StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _key: tmp_path)

    @staticmethod
    def _interrupted(tmp_path: Path, tool: _FakeTool, *, attempt: int = 0, quiet_for_sec: float = 3600.0) -> Any:
        """A stint whose file says it is running and whose holder has gone quiet.

        ``quiet_for_sec`` backdates the stamp, which is what a host that died
        leaves behind: the record is never rewritten, so the beat stops and the
        stamp stays where it was. Pass 0 for a stint somebody else is working.
        """
        import time

        from raven.stint.record import StintRecord, StintStore

        store = StintStore(tool.stints_root())
        record = StintRecord(
            stint_id="stint-x",
            playbook="game-dev",
            spec=_spec().model_dump(by_alias=True),
            workdir=str(tmp_path),
            project=str(tmp_path),
            round_index=1,
            origin=tool.turn_origin(),
        )
        record.open_round(1, "run-old", attempt=attempt)
        store.write(record)
        if quiet_for_sec:
            record.touched_at_ms = int((time.time() - quiet_for_sec) * 1000)
            path = store.path_for(record.stint_id)
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            data["touched_at_ms"] = record.touched_at_ms
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return store

    def test_a_round_taken_up_again_gets_new_ids(self) -> None:
        """The old ones are claimed for the life of the conversation, finished or not."""
        first = compile_round(_spec(), 4)
        again = compile_round(_spec(), 4, attempt=1)

        assert first[0]["id"] == "game-dev-r04-planner"
        assert again[0]["id"] == "game-dev-r04x1-planner"
        assert not {node["id"] for node in first} & {node["id"] for node in again}

    async def test_resume_names_a_finished_role_instead_of_running_it_again(self, tmp_path: Path) -> None:
        tool = _FakeTool(tmp_path / "stints", readable={"game-dev-r01-planner"})
        store = self._interrupted(tmp_path, tool)

        await self._driver(tmp_path, tool).resume("stint-x", "web:stint")

        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r01x1-developer"]
        assert nodes[0]["depends_on"] == ["game-dev-r01-planner"]
        assert "{{game-dev-r01-planner.output}}" in nodes[0]["prompt_template"]
        assert store.read("stint-x").round(1).attempt == 1

    async def test_resume_reads_who_finished_off_the_record_and_only_confirms_with_the_registry(
        self, tmp_path: Path
    ) -> None:
        """The record travels with the stint; the registry belongs to one
        conversation's directory. Asked of the registry alone, a resume from a
        terminal or an RPC with no turn read the wrong one -- and either named a
        finished role where the graph could not see it (`depends on unknown
        r03-planner`) or ran a planner that had finished an hour earlier."""
        from raven.stint.record import PAUSED

        tool = _FakeTool(tmp_path / "stints", readable={"game-dev-r01-planner"})
        store = self._interrupted(tmp_path, tool)
        record = store.read("stint-x")
        record.round(1).finished = {"planner": "game-dev-r01-planner"}
        record.status = PAUSED
        store.write(record)

        await self._driver(tmp_path, tool).resume("stint-x", "web:stint")

        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r01x1-developer"]
        assert nodes[0]["depends_on"] == ["game-dev-r01-planner"]

    async def test_a_role_the_record_says_finished_but_the_registry_cannot_read_runs_again(
        self, tmp_path: Path
    ) -> None:
        """Naming it would fail validation with a reference to nothing."""
        from raven.stint.record import PAUSED

        tool = _FakeTool(tmp_path / "stints", readable=set())
        store = self._interrupted(tmp_path, tool)
        record = store.read("stint-x")
        record.round(1).finished = {"planner": "game-dev-r01-planner"}
        record.status = PAUSED
        store.write(record)

        await self._driver(tmp_path, tool).resume("stint-x", "web:stint")

        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r01x1-planner", "game-dev-r01x1-developer"]

    async def test_a_completed_round_sends_what_it_promised_and_did_not_deliver_back_to_open(
        self, tmp_path: Path
    ) -> None:
        """Both rules lived in `backlog.py` with no caller. A Developer cut off by
        its turn budget left tasks `assigned` for the rest of the stint, and a
        task QA never judged stayed `in_review`."""
        import json

        from raven.stint import backlog as backlog_mod

        tool = _FakeTool(tmp_path / "stints", readable={"game-dev-r01-planner"})
        store = self._interrupted(tmp_path, tool, quiet_for_sec=0)
        record = store.read("stint-x")
        path = backlog_mod.backlog_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "meta": {},
                    "tasks": [
                        {"id": 1, "title": "promised", "state": "assigned", "owner": "developer"},
                        {"id": 2, "title": "unjudged", "state": "in_review"},
                        {"id": 3, "title": "finished", "state": "done"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        await self._driver(tmp_path, tool).advance(record.ref(1), "run-old", "finished: 2 completed, 0 failed", False)

        backlog = backlog_mod.load(tmp_path)
        assert [backlog.get(n).state for n in (1, 2, 3)] == ["open", "open", "done"]
        assert backlog.get(1).owner == ""

    async def test_a_cut_round_keeps_its_tasks_assigned_for_the_developer_that_resumes_it(
        self, tmp_path: Path
    ) -> None:
        import json

        from raven.stint import backlog as backlog_mod

        tool = _FakeTool(tmp_path / "stints", readable={"game-dev-r01-planner"})
        store = self._interrupted(tmp_path, tool, quiet_for_sec=0)
        record = store.read("stint-x")
        path = backlog_mod.backlog_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"meta": {}, "tasks": [{"id": 1, "title": "promised", "state": "assigned"}]}),
            encoding="utf-8",
        )

        await self._driver(tmp_path, tool).advance(record.ref(1), "run-old", "cut", True)

        assert backlog_mod.load(tmp_path).get(1).state == "assigned"

    async def test_resume_reruns_a_role_that_finished_without_leaving_output(self, tmp_path: Path) -> None:
        """Naming it would hand the round a reference that resolves to nothing."""
        tool = _FakeTool(tmp_path / "stints", readable=set())
        self._interrupted(tmp_path, tool)

        await self._driver(tmp_path, tool).resume("stint-x", "web:stint")

        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r01x1-planner", "game-dev-r01x1-developer"]

    async def test_resuming_a_finished_plan_says_so_rather_than_starting_a_round(self, tmp_path: Path) -> None:
        from raven.stint.record import FINISHED

        tool = _FakeTool(tmp_path / "stints")
        store = self._interrupted(tmp_path, tool)
        record = store.read("stint-x")
        record.status = FINISHED
        store.write(record)

        answer = await self._driver(tmp_path, tool).resume("stint-x", "web:stint")

        assert "nothing left to take up" in answer
        assert tool.submitted == []

    async def test_a_round_cut_short_leaves_the_plan_paused_and_resume_takes_it_up(self, tmp_path: Path) -> None:
        """`cancel_dag` on a round used to end the stint: recorded stopped, which
        `resume` refuses, and `extend` re-submitted the round under ids the cut
        attempt still owns. A person who stops a round wants the round stopped,
        not the week of rounds before it thrown away."""
        tool = _FakeTool(tmp_path / "stints", readable={"game-dev-r01-planner"})
        store = self._interrupted(tmp_path, tool, quiet_for_sec=0)
        driver = self._driver(tmp_path, tool)
        record = store.read("stint-x")

        assert await driver.advance(record.ref(1), "run-old", "cut", True) is None
        record = store.read("stint-x")
        assert record.status == "paused"
        assert "stopped before it finished" in record.stop_reason
        assert record.round(1).status == "stopped"

        await driver.resume("stint-x", "web:stint")

        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r01x1-developer"]
        assert store.read("stint-x").status == "running"

    async def test_a_plan_told_to_stop_stays_stopped_when_its_last_round_is_cut(self, tmp_path: Path) -> None:
        from raven.stint.record import STOPPED

        tool = _FakeTool(tmp_path / "stints")
        store = self._interrupted(tmp_path, tool, quiet_for_sec=0)
        record = store.read("stint-x")
        record.status = STOPPED
        record.stop_reason = "a person stopped the stint"
        store.write(record)

        await self._driver(tmp_path, tool).advance(record.ref(1), "run-old", "cut", True)

        assert store.read("stint-x").status == "stopped"
        assert store.read("stint-x").stop_reason == "a person stopped the stint"

    async def test_extend_puts_an_unfinished_round_up_again_under_a_new_attempt(self, tmp_path: Path) -> None:
        """The round that did not finish is the round `extend` opens, and its
        first attempt's node ids are claimed for the conversation's life -- so
        submitting them again was refused at validation and the stint recorded
        as finished with `round 1 could not start`."""
        from raven.stint.record import STOPPED

        tool = _FakeTool(tmp_path / "stints", readable={"game-dev-r01-planner"})
        store = self._interrupted(tmp_path, tool, quiet_for_sec=0)
        record = store.read("stint-x")
        record.status = STOPPED
        record.round(1).status = "stopped"
        store.write(record)

        answer = await self._driver(tmp_path, tool).extend("stint-x", 1, "web:stint")

        assert not answer.startswith("Error"), answer
        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r01x1-developer"]
        assert store.read("stint-x").round(1).attempt == 1

    async def test_a_dead_holder_is_as_good_as_a_quiet_one(self, tmp_path: Path) -> None:
        """A gateway killed mid-round leaves a stamp that stays fresh for five
        minutes, and a person who restarts it inside those minutes was told the
        stint was still being worked. The pid it recorded is not alive, and
        the kernel answers that at once."""
        import socket
        import subprocess

        tool = _FakeTool(tmp_path / "stints", readable={"game-dev-r01-planner"})
        store = self._interrupted(tmp_path, tool, quiet_for_sec=0)
        gone = subprocess.Popen(["true"])
        gone.wait()
        record = store.read("stint-x")
        record.holder_pid, record.holder_host = gone.pid, socket.gethostname()
        store.write(record)
        assert not store.read("stint-x").stale(), "the stamp is fresh; only the pid says otherwise"

        driver = self._driver(tmp_path, tool)
        assert [found.stint_id for found in driver.adrift("web:stint")] == ["stint-x"]
        await driver.resume("stint-x", "web:stint")

        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r01x1-developer"]

    async def test_a_live_holder_on_this_machine_keeps_resume_off(self, tmp_path: Path) -> None:
        import os
        import socket

        tool = _FakeTool(tmp_path / "stints")
        store = self._interrupted(tmp_path, tool, quiet_for_sec=0)
        record = store.read("stint-x")
        record.holder_pid, record.holder_host = os.getpid(), socket.gethostname()
        store.write(record)

        answer = await self._driver(tmp_path, tool).resume("stint-x", "web:stint")

        assert "still working it" in answer
        assert tool.submitted == []

    async def test_asking_for_the_same_playbook_on_an_abandoned_plan_takes_it_up(self, tmp_path: Path) -> None:
        """The person restarted raven and said "carry on" in the same
        conversation. The model reaches for the playbook by name, and a start
        that refused with "resume it yourself" would send them to a terminal;
        a start that started would redo the stint from an older base."""
        tool = _FakeTool(tmp_path / "stints", readable={"game-dev-r01-planner"})
        store = self._interrupted(tmp_path, tool)
        driver = self._driver(tmp_path, tool)

        receipt = await driver.start(_spec(confirm=False))

        assert not receipt.startswith("Error"), receipt
        assert "taken up rather than started over" in receipt
        assert [record.stint_id for record in store.list()] == ["stint-x"], "no second stint was written"
        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r01x1-developer"]

    async def test_a_plan_somebody_is_still_working_is_not_taken_from_them_by_a_start(self, tmp_path: Path) -> None:
        tool = _FakeTool(tmp_path / "stints")
        self._interrupted(tmp_path, tool, quiet_for_sec=0)

        receipt = await self._driver(tmp_path, tool).start(_spec(confirm=False))

        assert receipt.startswith("Error") and "already" in receipt
        assert tool.submitted == []

    async def test_a_sweep_takes_up_a_plan_whose_run_is_in_flight_nowhere(self, tmp_path: Path) -> None:
        tool = _FakeTool(tmp_path / "stints", live=[])
        self._interrupted(tmp_path, tool)

        taken = await self._driver(tmp_path, tool).sweep("web:stint")

        assert taken == ["stint-x"]
        assert len(tool.submitted) == 1

    async def test_a_sweep_leaves_a_plan_whose_round_is_still_going(self, tmp_path: Path) -> None:
        """The whole point is picking up what nobody is advancing, not racing
        whoever still is."""
        tool = _FakeTool(tmp_path / "stints", live=["run-old"])
        self._interrupted(tmp_path, tool)

        assert await self._driver(tmp_path, tool).sweep("web:stint") == []
        assert tool.submitted == []


class TestTheWordThatEndsItEarly:
    """`stop.until` was read by the machine and said to nobody."""

    @staticmethod
    def _chain(**over: Any):
        return _spec(stop={"maxRounds": 9, "until": "NOTHING-LEFT"}, **over)

    def test_the_last_role_is_told_the_word(self) -> None:
        [_planner, developer] = compile_round(self._chain(), 3)

        assert "NOTHING-LEFT" in developer["prompt_template"]
        assert "last role of this round" in developer["prompt_template"]

    def test_a_role_something_waits_on_is_not(self) -> None:
        """Only a terminal node's output reaches the check, so the word would be
        a request to write something nobody reads."""
        [planner, _developer] = compile_round(self._chain(), 3)

        assert "NOTHING-LEFT" not in planner["prompt_template"]

    def test_a_plan_with_no_marker_says_nothing_about_ending_early(self) -> None:
        """There is no way to end it early, and a role told otherwise would spend
        rounds looking for the words that do it."""
        nodes = compile_round(_spec(stop={"maxRounds": 9}), 3)

        assert not any("ends it early" in node["prompt_template"] for node in nodes)

    def test_an_author_who_places_the_slot_is_not_given_a_second_copy(self) -> None:
        roles = [{"as": "planner", "name": "echo", "promptTemplate": "do it\n\n{{round.finish}}"}]
        [node] = compile_round(self._chain(roles=roles), 3)

        assert node["prompt_template"].count("NOTHING-LEFT") == 1

    def test_every_role_of_a_flat_round_is_a_last_role(self) -> None:
        """Nothing waits on any of them, so the check reads all of their output."""
        roles = [
            {"as": "one", "name": "echo", "promptTemplate": "a"},
            {"as": "two", "name": "echo", "promptTemplate": "b"},
        ]
        nodes = compile_round(self._chain(roles=roles), 1)

        assert all("NOTHING-LEFT" in node["prompt_template"] for node in nodes)

    async def test_the_word_a_role_writes_ends_the_plan(self, tmp_path: Path) -> None:
        """The whole loop: told in the prompt, written in the output, read in the
        summary, recorded as the reason."""
        from raven.playbook.stint import _stop_reason
        from raven.stint.record import StintRecord

        spec = self._chain()
        record = StintRecord(stint_id="stint-x", playbook="game-dev", spec={}, round_index=2)

        said = "I have checked the backlog and there is nothing worth another round.\nNOTHING-LEFT\n"
        assert _stop_reason(spec, record, said) == "a role reported 'NOTHING-LEFT'"
        assert _stop_reason(spec, record, "plenty left to do") == ""

    def test_the_word_has_to_be_the_whole_line(self) -> None:
        """The role told to write it is the role most likely to mention it, and a
        sentence disputing the claim would otherwise settle it."""
        from raven.playbook.stint import _stop_reason
        from raven.stint.record import StintRecord

        spec = self._chain()
        record = StintRecord(stint_id="stint-x", playbook="game-dev", spec={}, round_index=2)

        disputed = "The planner claims NOTHING-LEFT, but I found three tasks it missed."
        assert _stop_reason(spec, record, disputed) == ""
        assert _stop_reason(spec, record, "here it is:\n  NOTHING-LEFT  \nthat is all") != ""


class TestAProjectThatWasNeverSetUp:
    """`setup:` -- the playbook says what layout it is written against."""

    @staticmethod
    def _spec_file() -> str:
        return (
            "# Ledger\n\n## Requirements\n\n- Every posting must balance.\n"
            "- The CLI should print a trial balance.\n\n## Acceptance\n\n"
            "- `pytest -q` passes; the scope is journals and accounts.\n" + "More detail. " * 20
        )

    def _project(self, tmp_path: Path) -> Path:
        project = tmp_path / "game"
        project.mkdir()
        ProjectGit(project).ensure_repo()
        (project / "PRD.md").write_text(self._spec_file(), encoding="utf-8")
        return project

    @staticmethod
    def _driver(tool: _FakeTool, project: Path) -> StintDriver:
        return StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _key: project)

    async def test_the_layout_reaches_the_checkout_the_rounds_actually_run_in(self, tmp_path: Path) -> None:
        """The failure this exists to stop. A worktree is cut from HEAD and what
        setup writes is not committed, so the standing orders sat in the project
        the person was looking at and not in the tree the roles read -- every
        round failing on `{{ref:}}`, for the whole budget."""
        project = self._project(tmp_path)
        tool = _FakeTool(tmp_path / "stints")
        spec = _spec(
            confirm=False,
            setup="stint",
            isolation="worktree",
            roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}],
        )

        await self._driver(tool, project).start(spec)

        [record] = self._driver(tool, project).store_for("web:stint").list()
        tree = Path(record.workdir)
        assert tree != project, "the stint got a checkout of its own"
        assert (tree / ".stint" / "planner.md").is_file(), sorted(p.name for p in tree.iterdir())
        # And it is named as having been there before any role ran, or the first
        # role of round one is graded against it and it is quarantined as that
        # role's stray write -- which is how it went missing for round two.
        assert ".stint/planner.md" in record.untracked_at_start

    async def test_what_setup_writes_is_not_committed_to_the_person_s_branch(self, tmp_path: Path) -> None:
        """Committing to somebody's branch for a run they have not approved yet
        is not setup's to do. They get untracked files and the choice."""
        project = self._project(tmp_path)
        tool = _FakeTool(tmp_path / "stints")
        spec = _spec(confirm=False, setup="stint", roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}])

        await self._driver(tool, project).start(spec)

        status = ProjectGit(project)._run("status", "--porcelain").stdout
        assert "?? .stint/" in status, status

    async def test_a_project_with_nothing_to_plan_from_is_not_started(self, tmp_path: Path) -> None:
        project = tmp_path / "game"
        project.mkdir()
        ProjectGit(project).ensure_repo()
        tool = _FakeTool(tmp_path / "stints")
        spec = _spec(confirm=False, setup="stint", roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}])

        answer = await self._driver(tool, project).start(spec)

        assert answer.startswith("Error") and ".stint/SPEC.md" in answer
        assert tool.submitted == [], "no round was opened"
        assert self._driver(tool, project).store_for("web:stint").list() == [], "and no stint was written"

    async def test_a_playbook_that_asks_for_no_layout_gets_none(self, tmp_path: Path) -> None:
        """Most stints work a project as they find it, and a directory
        full of standing orders nobody asked for is not setup."""
        project = self._project(tmp_path)
        tool = _FakeTool(tmp_path / "stints")

        await self._driver(tool, project).start(
            _spec(confirm=False, roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}])
        )

        assert not (project / ".stint").exists()

    def test_the_approval_says_what_it_wrote_and_what_it_picked(self, tmp_path: Path) -> None:
        """Both are judgements a person should get to overrule: the files are
        new in their repository, and the specification was picked rather than
        named by anyone."""
        from raven.playbook.stint import approval
        from raven.stint.record import StintRecord
        from raven.stint.setup import lay_out

        project = self._project(tmp_path)
        layout = lay_out(project, "stint")
        record = StintRecord(stint_id="stint-x", playbook="game-dev", spec={}, project=str(project))

        asked = approval(_spec(), record, layout)

        assert "untracked" in asked and ".stint/planner.md" in asked
        assert "the roles stint from PRD.md" in asked


class TestWhatAPersonApproves:
    """A stint is approved once, and the question is all they are shown."""

    @staticmethod
    def _record(**over: Any) -> Any:
        from raven.stint.record import StintRecord

        base = {
            "stint_id": "stint-x",
            "playbook": "game-dev",
            "spec": {},
            "project": "/home/me/game",
            "workdir": "/stints/tree",
            "branch": "stint/stint-x",
        }
        return StintRecord(**{**base, **over})

    def test_the_question_names_the_budget_the_commands_and_the_branch(self) -> None:
        """The generic graph question lists three node ids, which describes the
        visible tenth of thirty rounds running shell commands on this machine."""
        from raven.playbook.stint import approval

        spec = _spec(
            stop={"maxRounds": 30, "until": "NOTHING-LEFT"},
            verify=[
                {"name": "build", "run": "python3 -m compileall -q src"},
                {"name": "tests", "run": "uv run pytest -q"},
            ],
        )

        asked = approval(spec, self._record())

        assert "up to 30 round(s) of: planner -> developer" in asked
        # Every command in full: they run here, and this is the one moment.
        assert "python3 -m compileall -q src" in asked
        assert "uv run pytest -q" in asked
        assert "stint/stint-x" in asked and "the branch you are on now is left where it is" in asked
        assert "NOTHING-LEFT" in asked
        assert "/home/me/game" in asked

    def test_the_question_says_which_tree_the_run_takes(self) -> None:
        """The three isolations differ in what the person gives up, and that is
        the half of the question they cannot get from the round count."""
        from raven.playbook.stint import approval

        own = approval(_spec(isolation="worktree"), self._record())
        theirs = approval(_spec(isolation="branch"), self._record())

        assert "in a checkout of its own -- your working tree is untouched" in own
        assert "the tree is the stint's until it ends" in theirs

    def test_a_budget_with_no_way_out_does_not_read_as_a_promise(self) -> None:
        from raven.playbook.stint import approval

        asked = approval(_spec(stop={"maxRounds": 4}), self._record())

        assert "nothing ends it before the budget" in asked

    def test_a_boundary_nothing_undoes_is_not_shown_as_one_that_does(self) -> None:
        from raven.playbook.stint import approval

        spec = _spec(
            roles=[
                {
                    "as": "planner",
                    "name": "echo",
                    "promptTemplate": "hi",
                    "owns": ["a.md"],
                    "enforce": {"write": "soft"},
                }
            ]
        )

        assert "recorded, not undone" in approval(spec, self._record())

    def test_a_long_ownership_list_is_counted_rather_than_recited(self) -> None:
        """A question long enough to scroll is a question answered without
        reading, which is worse than a short one."""
        from raven.playbook.stint import approval

        spec = _spec(
            roles=[
                {
                    "as": "planner",
                    "name": "echo",
                    "promptTemplate": "hi",
                    "owns": [f"src/{n}/**" for n in range(9)],
                }
            ]
        )

        asked = approval(spec, self._record())

        assert "and 6 more" in asked
        assert "src/8/**" not in asked

    @staticmethod
    def _asking_tool(tmp_path: Path, asked: list[str]):
        from raven.agent.subagent.dag_tool import SubAgentDagTool

        async def ask(_conversation: str, question: str) -> bool:
            asked.append(question)
            return False

        tool = SubAgentDagTool(
            workspace=tmp_path, agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")], ask=ask
        )
        tool.set_context("web", "default", "web:stint")
        return tool

    async def test_the_plan_asks_its_own_question_and_an_ordinary_graph_does_not(self, tmp_path: Path) -> None:
        """The seam: a stint hands the gate a question, an ordinary run leaves it
        to build the one it always built."""
        asked: list[str] = []
        tool = self._asking_tool(tmp_path, asked)
        nodes = [{"id": "a", "subagent": "echo", "node_summary": "s", "prompt_template": "p"}]

        from raven.agent.subagent.prompt_errors import RoundNotApprovedError

        await tool.execute(nodes, task_summary="ordinary", confirm=True, background=False)
        # A refused round is an exception for the driver, which has a record
        # to close; the ordinary run above got the sentence a model reads.
        with pytest.raises(RoundNotApprovedError):
            await tool.run_round(
                nodes,
                task_summary="a stint",
                confirm=True,
                stint=StintRef(stint_id="stint-1", round_index=1, session_key="web:stint"),
                confirm_question=lambda: "Start a stint? up to 30 rounds",
            )

        assert asked[0].startswith("Run this 1 step graph?")
        assert asked[1] == "Start a stint? up to 30 rounds"

    async def test_the_model_facing_entry_cannot_be_handed_a_question_at_all(self, tmp_path: Path) -> None:
        """The composer of a graph must not also write the sentence it is approved by.

        `execute` is what a model reaches through the registry, and it takes no
        such argument: one sent anyway lands in `**kwargs` and goes nowhere, and
        the gate asks what it always asked.
        """
        import inspect

        asked: list[str] = []
        tool = self._asking_tool(tmp_path, asked)

        await tool.execute(
            [{"id": "a", "subagent": "echo", "node_summary": "s", "prompt_template": "p"}],
            task_summary="innocent",
            confirm=True,
            background=False,
            confirm_question="Run this harmless 1 step graph?",
        )

        assert asked == ["Run this 1 step graph?\n- a: echo"]
        # Not in the schema, and not in the signature either: the host's own
        # arguments live on `run_round`, which no tool call can reach.
        assert "confirm_question" not in tool.parameters.get("properties", {})
        taken = set(inspect.signature(tool.execute).parameters)
        assert taken.isdisjoint({"confirm_question", "stint", "origin"})


class TestTakingAPlanUpAgain:
    """What has to hold when a stint that stopped is made to go again.

    `extend` and `resume` are two ways into the same act, so what is true of one
    is asked of the other here rather than in two places that drift.
    """

    @staticmethod
    def _driver(tmp_path: Path, tool: _FakeTool, project: Path | None = None) -> StintDriver:
        return StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _key: project or tmp_path)

    @staticmethod
    def _plan(tmp_path: Path, tool: _FakeTool, *, status: str, rounds: int = 1, done: bool = True) -> Any:
        from raven.stint.record import StintRecord, StintStore

        (tmp_path / "tree").mkdir(exist_ok=True)
        store = StintStore(tool.stints_root())
        record = StintRecord(
            stint_id="stint-x",
            playbook="game-dev",
            spec=_spec().model_dump(by_alias=True),
            workdir=str(tmp_path / "tree"),
            project=str(tmp_path),
            branch="stint/stint-x",
            round_index=rounds,
            status=status,
            origin=tool.turn_origin(),
        )
        for index in range(1, rounds + 1):
            record.open_round(index, f"run-{index}")
            record.rounds[-1].status = "completed" if done or index < rounds else "running"
        store.write(record)
        return store

    async def test_a_paused_plan_opens_the_round_after_the_one_it_paused_on(self, tmp_path: Path) -> None:
        """A pause lands between rounds: the one in flight finishes and is
        recorded, and no further one opens. Taking it up again therefore means
        the next round -- re-running the finished one finds every role already
        done and nothing left to run."""
        tool = _FakeTool(tmp_path / "stints", readable={"game-dev-r01-planner", "game-dev-r01-developer"})
        store = self._plan(tmp_path, tool, status="paused")

        answer = await self._driver(tmp_path, tool).resume("stint-x", "web:stint")

        assert not answer.startswith("Error"), answer
        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r02-planner", "game-dev-r02-developer"]
        assert store.read("stint-x").status == "running"

    async def test_a_plan_paused_on_its_last_round_is_sent_to_the_verb_that_can_help(self, tmp_path: Path) -> None:
        """Taking it up means the round after, and there is no round after a
        budget that is spent. Opening one anyway would run it and then stop on
        the budget it was already past."""
        tool = _FakeTool(tmp_path / "stints")
        self._plan(tmp_path, tool, status="paused", rounds=2)

        answer = await self._driver(tmp_path, tool).resume("stint-x", "web:stint")

        assert "extend stint-x --rounds N" in answer
        assert tool.submitted == []

    async def test_an_interrupted_round_is_still_taken_up_where_it_broke(self, tmp_path: Path) -> None:
        """The other half of the same question, kept here so a fix for the one
        above cannot quietly turn every resume into a skipped round."""
        tool = _FakeTool(tmp_path / "stints", readable={"game-dev-r01-planner"})
        self._plan(tmp_path, tool, status="interrupted", done=False)

        await self._driver(tmp_path, tool).resume("stint-x", "web:stint")

        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r01x1-developer"]

    async def test_an_answer_given_after_the_plan_ended_reaches_the_round_that_follows(self, tmp_path: Path) -> None:
        """The order a person actually works in: read the question the stint left,
        answer it, then ask for the rounds that act on the answer."""
        tool = _FakeTool(tmp_path / "stints")
        store = self._plan(tmp_path, tool, status="finished")
        record = store.read("stint-x")
        record.questions = [{"round": 1, "role": "planner", "text": "which schema?", "answer": "the second one"}]
        store.write(record)

        await self._driver(tmp_path, tool).extend("stint-x", 2, "web:stint")

        [nodes] = tool.submitted
        assert "the second one" in nodes[0]["prompt_template"]

    async def test_a_plan_is_not_revived_beside_another_one_on_its_project(self, tmp_path: Path) -> None:
        """The guard `start` has, on the other way in. Stopping one stint and
        starting a second is ordinary; extending the first afterwards would put
        two of them on one repository, each committing from a base the other
        does not have."""
        tool = _FakeTool(tmp_path / "stints")
        store = self._plan(tmp_path, tool, status="stopped")
        driver = self._driver(tmp_path, tool)
        await driver.start(_spec(confirm=False, roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}]))
        assert len(tool.submitted) == 1, "the second stint started"

        answer = await driver.extend("stint-x", 3, "web:stint")

        assert answer.startswith("Error"), answer
        assert driver.store_for("web:stint").list()[0].stint_id in answer, "the stint holding the project is named"
        assert len(tool.submitted) == 1, "and no round was opened for the first"
        assert store.read("stint-x").status == "stopped"

    async def test_a_plan_nobody_is_advancing_is_not_told_a_round_is_in_flight(self, tmp_path: Path) -> None:
        """Its file says running; the process that wrote that is gone. Raising the
        budget and promising the round in flight would leave a person waiting on
        a round that does not exist."""
        tool = _FakeTool(tmp_path / "stints", live=[])
        self._plan(tmp_path, tool, status="running", done=False)

        answer = await self._driver(tmp_path, tool).extend("stint-x", 3, "web:stint")

        assert "in flight is no longer its last" not in answer
        assert "resume stint-x" in answer

    async def test_a_plan_whose_round_really_is_in_flight_is_left_to_finish_it(self, tmp_path: Path) -> None:
        tool = _FakeTool(tmp_path / "stints", live=["run-1"])
        self._plan(tmp_path, tool, status="running", done=False)

        answer = await self._driver(tmp_path, tool).extend("stint-x", 3, "web:stint")

        assert "in flight is no longer its last" in answer
        assert tool.submitted == []


class TestHowLongToKeepGoing:
    """The budget is the caller's to name, unlike everything else about a run."""

    @staticmethod
    def _driver(tmp_path: Path, tool: _FakeTool) -> StintDriver:
        return StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _key: tmp_path)

    @staticmethod
    def _plain():
        return _spec(confirm=False, roles=[{"as": "planner", "name": "echo", "promptTemplate": "hi"}])

    async def test_a_number_the_caller_names_is_the_one_the_plan_runs(self, tmp_path: Path) -> None:
        tool = _FakeTool(tmp_path / "stints")
        driver = self._driver(tmp_path, tool)

        await driver.start(self._plain(), max_rounds=3)

        [record] = driver.store_for("web:stint").list()
        assert record.spec["stop"]["maxRounds"] == 3
        # Said in the text the approval shows, because that is where the number
        # has to be true: the whole run is approved once, against this line.
        assert tool.summaries == ["game-dev: round 1 of at most 3"]

    async def test_a_number_nobody_names_is_the_playbook_s_own(self, tmp_path: Path) -> None:
        tool = _FakeTool(tmp_path / "stints")
        driver = self._driver(tmp_path, tool)

        await driver.start(self._plain())

        [record] = driver.store_for("web:stint").list()
        assert record.spec["stop"]["maxRounds"] == 2

    async def test_past_the_ceiling_starts_nothing(self, tmp_path: Path) -> None:
        from raven.playbook.stint_spec import MAX_ROUNDS

        tool = _FakeTool(tmp_path / "stints")
        driver = self._driver(tmp_path, tool)

        answer = await driver.start(self._plain(), max_rounds=MAX_ROUNDS + 1)

        assert answer.startswith("Error") and str(MAX_ROUNDS) in answer
        assert driver.store_for("web:stint").list() == []
        assert tool.submitted == []

    async def test_one_run_s_number_does_not_follow_the_playbook_to_the_next(self, tmp_path: Path) -> None:
        """The spec handed in belongs to the library, and whoever loads that
        playbook next is handed the same object."""
        tool = _FakeTool(tmp_path / "stints")
        spec = self._plain()

        await self._driver(tmp_path, tool).start(spec, max_rounds=7)

        assert spec.stop is not None and spec.stop.max_rounds == 2


class TestMoreRounds:
    """A budget is judged once, at the start, and found wrong at the end."""

    @staticmethod
    def _driver(tmp_path: Path, tool: _FakeTool) -> StintDriver:
        return StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _key: tmp_path)

    @staticmethod
    def _spent(tmp_path: Path, tool: _FakeTool, *, status: str = "finished", rounds: int = 2) -> Any:
        """A stint that ran the budget `_spec` gives it, and stopped on that."""
        from raven.stint.record import StintRecord, StintStore

        tree = tmp_path / "tree"
        tree.mkdir(exist_ok=True)
        store = StintStore(tool.stints_root())
        record = StintRecord(
            stint_id="stint-x",
            playbook="game-dev",
            spec=_spec().model_dump(by_alias=True),
            workdir=str(tree),
            project=str(tmp_path),
            branch="stint/stint-x",
            round_index=rounds,
            status=status,
            stop_reason="the round budget of 2 is spent",
            origin=tool.turn_origin(),
        )
        for index in range(1, rounds + 1):
            record.open_round(index, f"run-{index}")
            record.rounds[-1].status = "completed"
        store.write(record)
        return store

    async def test_a_plan_that_spent_its_budget_runs_again_rather_than_being_stuck(self, tmp_path: Path) -> None:
        tool = _FakeTool(tmp_path / "stints")
        store = self._spent(tmp_path, tool)

        await self._driver(tmp_path, tool).extend("stint-x", 3, "web:stint")

        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r03-planner", "game-dev-r03-developer"]
        record = store.read("stint-x")
        assert (record.status, record.round_index, record.stop_reason) == ("running", 3, "")
        assert record.spec["stop"]["maxRounds"] == 5

    async def test_the_rounds_it_already_ran_are_kept_rather_than_started_over(self, tmp_path: Path) -> None:
        """The reason this verb exists. A second stint would cut its checkout from
        the project's HEAD, which is behind the first stint's first commit, so
        "three more rounds" would silently mean "all of it again"."""
        tool = _FakeTool(tmp_path / "stints")
        store = self._spent(tmp_path, tool)
        before = store.read("stint-x")

        await self._driver(tmp_path, tool).extend("stint-x", 1, "web:stint")

        record = store.read("stint-x")
        assert (record.workdir, record.branch) == (before.workdir, before.branch)
        assert [entry.index for entry in record.rounds] == [1, 2, 3]

    async def test_a_plan_still_going_is_given_the_budget_and_no_round(self, tmp_path: Path) -> None:
        """One round is already in flight, and `advance` reads the budget from
        the file when it lands. Opening a second here would run two at once."""
        tool = _FakeTool(tmp_path / "stints", live=["run-1"])
        store = self._spent(tmp_path, tool, status="running", rounds=1)

        answer = await self._driver(tmp_path, tool).extend("stint-x", 4, "web:stint")

        assert tool.submitted == []
        assert store.read("stint-x").spec["stop"]["maxRounds"] == 6
        assert "no longer its last" in answer

    async def test_a_paused_plan_is_pointed_at_resume_rather_than_woken(self, tmp_path: Path) -> None:
        tool = _FakeTool(tmp_path / "stints")
        store = self._spent(tmp_path, tool, status="paused", rounds=1)

        answer = await self._driver(tmp_path, tool).extend("stint-x", 1, "web:stint")

        assert tool.submitted == []
        assert store.read("stint-x").status == "paused"
        assert "stints resume stint-x" in answer

    async def test_past_the_ceiling_is_refused_without_changing_the_budget(self, tmp_path: Path) -> None:
        from raven.playbook.stint_spec import MAX_ROUNDS

        tool = _FakeTool(tmp_path / "stints")
        store = self._spent(tmp_path, tool)

        answer = await self._driver(tmp_path, tool).extend("stint-x", MAX_ROUNDS, "web:stint")

        assert answer.startswith("Error")
        assert str(MAX_ROUNDS) in answer
        assert tool.submitted == []
        assert store.read("stint-x").spec["stop"]["maxRounds"] == 2

    async def test_a_checkout_that_is_gone_is_said_rather_than_run_from_nowhere(self, tmp_path: Path) -> None:
        tool = _FakeTool(tmp_path / "stints")
        store = self._spent(tmp_path, tool)
        (tmp_path / "tree").rmdir()

        answer = await self._driver(tmp_path, tool).extend("stint-x", 1, "web:stint")

        assert answer.startswith("Error") and "not there any more" in answer
        assert tool.submitted == []
        assert store.read("stint-x").status == "finished"

    async def test_a_plan_whose_first_round_was_refused_opens_that_round(self, tmp_path: Path) -> None:
        """Not the one after it: round one never ran, so there is nothing to
        follow and skipping it would leave the stint with no first round."""
        from raven.stint.record import StintRecord, StintStore

        tool = _FakeTool(tmp_path / "stints")
        (tmp_path / "tree").mkdir()
        store = StintStore(tool.stints_root())
        store.write(
            StintRecord(
                stint_id="stint-x",
                playbook="game-dev",
                spec=_spec().model_dump(by_alias=True),
                workdir=str(tmp_path / "tree"),
                round_index=1,
                status="stopped",
                stop_reason="the first round was refused",
                origin=tool.turn_origin(),
            )
        )

        await self._driver(tmp_path, tool).extend("stint-x", 1, "web:stint")

        [nodes] = tool.submitted
        assert [node["id"] for node in nodes] == ["game-dev-r01-planner", "game-dev-r01-developer"]

    async def test_no_more_rounds_is_not_more_rounds(self, tmp_path: Path) -> None:
        tool = _FakeTool(tmp_path / "stints")
        self._spent(tmp_path, tool)

        assert (await self._driver(tmp_path, tool).extend("stint-x", 0, "web:stint")).startswith("Error")
        assert tool.submitted == []

    def test_the_summary_names_the_branch_and_how_to_get_another_round(self, tmp_path: Path) -> None:
        """Both are things the reader cannot see: the work is on a branch nobody
        merged, and the only other visible move is to start a second stint."""
        from raven.playbook.stint import _summary

        tool = _FakeTool(tmp_path / "stints")
        record = self._spent(tmp_path, tool).read("stint-x")

        text = _summary(record)

        assert "on branch stint/stint-x, which nothing has merged" in text
        assert "raven playbook stints extend stint-x --rounds N" in text


class TestPreCallRefusals:
    """Layer three: a write outside the line refused before it happens.

    The pass that undoes a stray write is after the fact and only sees what git
    sees. A refusal at the tool gate stops the write landing at all, and its
    reason reaches the role's own messages so its next step can be right.
    Neither layer covers the other, which is why a round has both.
    """

    @staticmethod
    def _charter_spec() -> PlaybookSpec:
        return _spec(
            roles=[
                {
                    "as": "developer",
                    "name": "echo",
                    "promptTemplate": "work",
                    "owns": ["src/**"],
                    "playbook": {
                        "capability": {"tools": ["read_file", "write_file"]},
                        "action": {
                            "checks": {
                                "rules": [
                                    {
                                        "tool": "write_file",
                                        "pathPrefix": "src/",
                                        "message": "you only write under src/",
                                    }
                                ]
                            }
                        },
                    },
                },
                {"as": "qa", "name": "echo", "promptTemplate": "check", "dependsOn": ["developer"]},
            ]
        )

    def test_a_role_s_charter_is_keyed_by_the_node_it_runs_as(self) -> None:
        from raven.playbook.stint import charters_for

        charters = charters_for(self._charter_spec(), 7)

        assert set(charters) == {"game-dev-r07-developer"}, "a role with no playbook block carries no charter"
        assert charters["game-dev-r07-developer"]["tools"] == ["read_file", "write_file"]
        assert charters["game-dev-r07-developer"]["checks"][0]["pathPrefix"] == "src/"

    def test_the_charter_is_built_by_the_same_code_a_delegate_row_uses(self) -> None:
        """One worker description, so a role briefed for thirty rounds and a
        worker briefed for one turn cannot be narrowed by two rules that drift."""
        from raven.playbook.agent_generator import build_payload
        from raven.playbook.stint import charters_for

        role = self._charter_spec().roles[0]

        assert charters_for(self._charter_spec(), 7)["game-dev-r07-developer"] == build_payload("", role.playbook)

    async def test_the_charter_reaches_the_worker_s_own_process(self, tmp_path: Path) -> None:
        """It travels as the context variable a spawn's charter travels on, so
        the worker's tool gate reads it the same way."""
        from raven.agent.subagent.charter import parse
        from raven.agent.subagent.dag_tool import _CharteredBackend
        from raven.agent.subagent.delegate import outbound_charter

        seen: list[Any] = []

        class _Backend:
            async def run(self, *_args: Any, **_kwargs: Any) -> str:
                seen.append(outbound_charter())
                return "done"

        payload = {"tools": ["read_file"], "checks": [{"tool": "write_file", "pathPrefix": "src/"}]}
        await _CharteredBackend(_Backend(), payload).run()

        assert outbound_charter() is None, "the scope closes with the call"
        assert parse(seen[0]).tools == ("read_file",)
        assert parse(seen[0]).checks[0].path_prefix == "src/"


class TestStopping:
    @staticmethod
    def _plan(tmp_path: Path, tool: _FakeTool, *, status: str = "running") -> Any:
        from raven.stint.record import StintRecord, StintStore

        store = StintStore(tool.stints_root())
        record = StintRecord(
            stint_id="stint-s",
            playbook="game-dev",
            spec=_spec().model_dump(by_alias=True),
            workdir=str(tmp_path),
            round_index=1,
            status=status,
            origin=tool.turn_origin(),
        )
        record.open_round(1, "run-old")
        store.write(record)
        return store

    @staticmethod
    def _driver(tmp_path: Path, tool: _FakeTool) -> StintDriver:
        return StintDriver(tool, stints_root=tool.stints_root, workspace_for=lambda _key: tmp_path)

    async def test_a_stopped_round_opens_no_further_one_and_says_nothing(self, tmp_path: Path) -> None:
        """A stop is silent everywhere else, and narrating what somebody just
        cancelled is what that silence exists to avoid. What it leaves is a
        paused stint, not an ended one: the round was cut, the rounds before it
        were not, and `resume` puts the cut one up again."""
        tool = _FakeTool(tmp_path / "stints")
        store = self._plan(tmp_path, tool)
        driver = self._driver(tmp_path, tool)

        announced = await driver.advance(store.read("stint-s").ref(1), "run-old", "round done", True)

        assert announced is None
        assert tool.submitted == [], "no further round was opened"
        record = store.read("stint-s")
        assert record.status == "paused"
        assert record.stop_reason == "round 1 was stopped before it finished"
        assert record.round(1).status == "stopped"

    async def test_a_plan_stopped_while_a_round_ran_reports_that_round_and_ends(self, tmp_path: Path) -> None:
        """`playbook stint stop` lands on the file; the round in flight is not
        thrown away, it is just the last one."""
        tool = _FakeTool(tmp_path / "stints")
        store = self._plan(tmp_path, tool, status="stopped")
        driver = self._driver(tmp_path, tool)

        announced = await driver.advance(store.read("stint-s").ref(1), "run-old", "round done", False)

        assert announced is not None and "ran 1 round(s) and stopped" in announced
        assert tool.submitted == []
        assert store.read("stint-s").stop_reason == "a person stopped the stint"

    async def test_an_answer_reaches_the_round_after_the_one_that_asked(self, tmp_path: Path) -> None:
        tool = _FakeTool(tmp_path / "stints")
        store = self._plan(tmp_path, tool)
        record = store.read("stint-s")
        record.questions = [
            {"round": 1, "role": "planner", "text": "which of the two?", "answer": "the second one"},
            {"round": 1, "role": "planner", "text": "and the other thing?", "answer": ""},
        ]
        store.write(record)

        await self._driver(tmp_path, tool).advance(record.ref(1), "run-old", "round done", False)

        [nodes] = tool.submitted
        prompt = nodes[0]["prompt_template"]
        assert "the second one" in prompt
        assert "still unanswered" in prompt
        assert "Do not ask it again" in prompt


class TestHandbackThatWorks:
    async def test_a_role_that_fixes_what_the_check_found_passes_on_the_second_try(self, tmp_path: Path) -> None:
        """The whole point of handing it back rather than failing the round."""
        marker = tmp_path / "fixed"
        spec = _spec(
            roles=[
                {
                    "as": "dev",
                    "name": "echo",
                    "promptTemplate": "work",
                    "verifyAfter": ["build"],
                    "maxHandbacks": 2,
                }
            ],
            verify=[{"name": "build", "run": f"test -f {marker}"}],
        )
        context = TestBoundaries._context(tmp_path, spec)
        node = TestBoundaries._node("game-dev-r01-dev")

        first = await context.judge(node=node)
        marker.write_text("the role fixed it\n", encoding="utf-8")
        second = await context.judge(node=node)

        assert first.accomplished is False and first.follow_up
        assert second.accomplished is True and not second.follow_up
        assert context.record.round(1).verify[0]["status"] == "ok"
        assert not any("still failing" in note for note in context.record.round(1).violations)


def test_the_shipped_example_is_a_playbook_that_would_actually_run() -> None:
    """It ships, so it is checked like a playbook and not like prose: a file
    that stopped loading would be a broken library entry on every install, and
    the only one a person can run without writing one first."""
    import json
    import re

    import yaml

    from raven.agent.subagent.builtin_agents import BUILTIN_AGENT_NAMES
    from raven.playbook.validate import validate_structure

    text = Path("raven/playbook/builtin/game-rounds/playbook.md").read_text(encoding="utf-8")
    front = yaml.safe_load(re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL).group(1))
    block = yaml.safe_load(re.search(r"```yaml playbook-spec\n(.*?)```", text, re.DOTALL).group(1))

    spec = PlaybookSpec.model_validate({**block, "name": front["name"], "description": front["description"]})

    assert spec.mode == "stint"
    assert [role.label for role in spec.roles] == ["planner", "developer", "qa"]
    # The roster Raven ships, read from the agent manifests. Handing this the
    # names the file already contains is what let it ship naming three agents
    # that exist nowhere: the assertion passed by construction.
    shipped = set(BUILTIN_AGENT_NAMES) | {
        json.loads(manifest.read_text(encoding="utf-8"))["name"]
        for manifest in sorted(Path("agents").glob("*/subagent.json"))
    }
    assert validate_structure(spec, known_agents=shipped) == []

    nodes = compile_round(spec, 1)
    assert [node["id"] for node in nodes] == [
        "game-rounds-r01-planner",
        "game-rounds-r01-developer",
        "game-rounds-r01-qa",
    ]
    # The standing orders are referenced, not pasted: a round that inlined every
    # rule into every prompt is how the reply ceiling was reached the first time.
    assert "{{ref:.stint/planner.md}}" in nodes[0]["prompt_template"]
    assert "undone" in nodes[1]["prompt_template"], "the developer is told what it owns"
