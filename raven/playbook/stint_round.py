"""What a stint does to one round of its own graph: measure, undo, hand back.

The three layers of a boundary all live here, and they are three because one
is not enough:

* the role is **told** what it owns, which the compiler does;
* what it wrote is **measured** against that when it stops, and what it had no
  claim to is put back -- here;
* its own checks are **run**, and a failure it can fix is handed straight back
  to it rather than to a person -- also here.

There is no layer that refuses a write before it lands. A charter narrows a
role's tools only where the playbook declares one, ``owns`` is not turned into a
charter, and a role that shells out would go around a tool gate anyway -- which
is why the measuring pass is the boundary rather than a backstop to one.

Everything in this module runs on the graph runner's own hooks, inside the round,
with no turn and nobody watching. That shapes two decisions:

* a check failure the role can act on becomes a **follow-up**, not a question --
  there is nobody to ask, and "the build failed, here is the error" is already a
  complete instruction;
* once the handback budget is spent, the round **moves on with the failure
  recorded**. It does not fail the node: a failed node cascades a skip through
  everything downstream, so a builder who could not make the build pass would
  take the reviewer down with it, and a round that ends with a known defect
  written down is worth more than a round that ends with nothing.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from loguru import logger

from raven.agent.subagent.dag_adjudication import ABANDON
from raven.agent.subagent.dag_verdict import Verdict
from raven.i18n import t
from raven.playbook.stint_prompt import render
from raven.playbook.stint_spec import RoleEntry
from raven.playbook.types import PlaybookSpec
from raven.stint.checks import resolve_checks
from raven.stint.enforce import EnforceReport, enforce, roster_grader
from raven.stint.git import HistoryError, ProjectGit
from raven.stint.journal import JOURNAL, append_entry
from raven.stint.ownership import Role, Roster
from raven.stint.record import StintRecord, StintStore
from raven.stint.verify import CheckResult, CheckSpec, curated_env, resolve_display, run_checks, start_display

__all__ = ["RoundContext", "roster_from"]


def roster_from(spec: PlaybookSpec, *, project: Path) -> Roster:
    """The ownership table a round enforces, read off the role rows.

    The same :class:`Roster` H* builds out of guard files, from a different
    declaration: what a grade means is not the business of where it was written
    down.
    """
    return Roster(
        roles=[
            Role(
                name=role.label,
                order=index,
                enforce_read=role.enforce.read,
                enforce_write=role.enforce.write,
                owns=tuple(role.owns),
                appends=tuple(role.appends),
                reads=tuple(role.reads),
                artifacts=tuple(role.artifacts),
            )
            for index, role in enumerate(spec.roles or [])
        ],
        project=project,
    )


@dataclass
class RoundContext:
    """One round's own state, for the hooks the graph runner calls into.

    Lives only as long as the round. The stint's durable state is its file; this
    is the bookkeeping a round needs while it is running -- where each role
    started from, how many handbacks it has spent -- and none of it is worth
    keeping once the round is over.
    """

    spec: PlaybookSpec
    record: StintRecord
    store: StintStore
    index: int
    workdir: Path
    artifacts_dir: Path

    _bases: dict[str, str] = field(default_factory=dict)
    _spent: dict[str, int] = field(default_factory=dict)
    _results: dict[str, CheckResult] = field(default_factory=dict)
    _git: ProjectGit | None = None
    _git_ready: bool = False

    @property
    def enforcing(self) -> bool:
        """Whether any role declared a boundary worth measuring.

        A stint whose roles claim nothing takes no git snapshots at all, which is
        what keeps the cost of this module off a stint that is not asking for it.
        """
        return any(role.owns or role.appends for role in (self.spec.roles or []))

    @property
    def max_handbacks(self) -> int:
        return max((role.max_handbacks for role in (self.spec.roles or [])), default=0)

    def role_of(self, node_id: str) -> RoleEntry | None:
        """The role a node of this round runs as, whatever attempt the round is on.

        A round taken up again submits its nodes as ``<playbook>-r02x1-<role>``
        (see :func:`raven.playbook.stint.namespace`), and a reader that knew only
        the first attempt's spelling answered None for every one of them -- so
        on a resumed round nothing was enforced, no check ran, and no role's
        work was committed, silently, while the round reported completed.
        """
        head = re.escape(self.spec.name) + (f"-{re.escape(self.record.token)}" if self.record.token else "")
        match = re.match(rf"^{head}-r{self.index:02d}(?:x\d+)?-(.+)$", node_id)
        label = match.group(1) if match else node_id
        for role in self.spec.roles or []:
            if role.label == label:
                return role
        return None

    def git(self) -> ProjectGit | None:
        """The project's repository, or None if this tree cannot have one.

        Resolved once and remembered, including the failure: a stint that cannot
        take a git snapshot degrades to a boundary nobody enforces, and saying
        so once is more useful than saying it per node for thirty rounds.
        """
        if self._git_ready:
            return self._git
        self._git_ready = True
        try:
            repository = ProjectGit(self.workdir)
            repository.ensure_repo()
            self._git = repository
        except (HistoryError, OSError) as exc:
            logger.error(
                "stint {} works a tree with no usable git ({}); what a role writes cannot be undone",
                self.record.stint_id,
                exc,
            )
            self._git = None
        return self._git

    async def node_started(self, node_id: str) -> None:
        """Remember where the tree stood before this role touched it.

        Taken per attempt, not per node: a handback is a fresh attempt, and the
        second attempt's boundary is measured from where the first left the tree
        -- what the first attempt legitimately wrote is not the second's doing.
        """
        if not self.enforcing or (repository := self.git()) is None:
            return
        self._bases[node_id] = repository.head()

    async def judge(
        self,
        *,
        node: Any,
        store: Any = None,
        output: str = "",
        error: str = "",
        crashed: bool = False,
        output_limited: bool = False,
    ) -> Verdict:
        """Measure what the role wrote, run its checks, and decide what happens next."""
        role = self.role_of(node.id)
        if role is None:
            return Verdict(accomplished=True)
        report = self._enforce(node.id, role)
        failures = await self._verify(role)
        spent = self._spent.get(node.id, 0)
        if (failures or (report is not None and not report.clean)) and spent < role.max_handbacks:
            self._spent[node.id] = spent + 1
            return Verdict(
                accomplished=False,
                category="checks",
                what_is_missing=_complaint(failures, report),
                follow_up=_complaint(failures, report),
            )
        self._journal(role, output)
        self._commit(node.id, role)
        self._record(report, failures, handbacks=spent, role=role.label, node_id=node.id)
        return Verdict(accomplished=True)

    async def adjudicate(self, node_id: str, report: str) -> tuple[str, str]:
        """Answer a suspended node on behalf of a stint nobody is watching.

        The desk is waiting for a person or for the main agent, and a round
        dispatched between turns has neither -- left alone it waits out the
        whole adjudication timeout and fails anyway. Abandoning says the same
        thing sooner, and in the vocabulary the desk already has: the node is
        `failed`, its dependents are skipped, and the round ends rather than
        stalling.

        The question is written down first, which is the part that is not just
        a faster failure: the next round carries it into its prompts and a
        person reading the stint between rounds finds it there.
        """
        await self.record_question(node_id, report)
        return ABANDON, "Nobody was watching this stint, so the round recorded the question and moved on."

    async def record_question(self, node_id: str, reason: str) -> bool:
        """Put a question nobody answered where a person will find it.

        Written down whatever is decided about the node: a stint that failed a
        role and never said why is a stint nobody can take up again.
        """
        role = self.role_of(node_id)
        self._persist(
            lambda record: record.questions.append(
                {
                    "round": self.index,
                    "role": role.label if role is not None else node_id,
                    "text": reason,
                    "answered_at": None,
                    "answer": "",
                }
            )
        )
        logger.info("stint {} round {} filed a question nobody answered", self.record.stint_id, self.index)
        return True

    def _enforce(self, node_id: str, role: RoleEntry) -> EnforceReport | None:
        if not (role.owns or role.appends) or role.enforce.write != "hard":
            return None
        repository = self.git()
        if repository is None:
            return None
        roster = roster_from(self.spec, project=self.workdir)
        quarantine = self.artifacts_dir / f"round-{self.index:02d}" / "violations" / role.label
        try:
            return enforce(
                repository,
                role=role.label,
                grade=roster_grader(roster, role.label, self.index),
                quarantine=quarantine,
                stage_base=self._bases.get(node_id, ""),
                allowed=self._still_nobodys(repository, self._bases.get(node_id, "")),
                artifacts=roster.artifacts(round_index=self.index),
                commit_revert=lambda paths: repository.commit(
                    f"revert(round-{self.index:02d}): {role.label} wrote where it may not",
                    author=role.label,
                    paths=paths,
                ),
            )
        except (HistoryError, OSError) as exc:
            logger.error("stint {} could not measure what {} wrote: {}", self.record.stint_id, role.label, exc)
            return None

    def _still_nobodys(self, repository: ProjectGit, base: str) -> list[str]:
        """The paths the stint opened over that no round has committed yet.

        What the tree already held, uncommitted, when the stint opened it -- the
        standing orders the setup pass wrote -- is nobody's write, and the first
        role of round one has no earlier commit to be measured from. Without the
        exemption those files are graded as its writes and quarantined, and the
        next round cannot read them.

        But the exemption is for as long as the file is uncommitted, not for the
        stint's life: the first role's commit takes everything in the tree with
        it, so from the second stage on a change to `.stint/verifier.md` has a base to
        be measured from and is a role's edit like any other. Kept on the record
        as the list it was, and narrowed here to what the base does not hold.
        """
        return [path for path in self.record.untracked_at_start if not repository.known_at(base, path)]

    async def _verify(self, role: RoleEntry) -> list[CheckResult]:
        """The role's checks, run off the event loop.

        Every one of these is a real command with a real timeout -- the default
        is half an hour -- and this coroutine is awaited by the graph runner on
        the host's own loop. Run inline, a five-minute test suite is five
        minutes in which no other node advances, no RPC is answered and no
        message is replied to: the whole gateway waits on somebody's pytest.

        A thread rather than `asyncio.create_subprocess_shell`, because what is
        in the thread is not only the subprocess: it is the Xvfb this starts and
        reaps around the batch, and the log files it reads back. Moving the
        whole synchronous block is one seam; converting it piecewise would leave
        the blocking parts that are not the subprocess still on the loop.
        """
        return await asyncio.to_thread(self._verify_now, role)

    def _verify_now(self, role: RoleEntry) -> list[CheckResult]:
        if not role.verify_after:
            return []
        declared = {entry.name: entry for entry in (self.spec.verify or [])}
        wanted = [declared[name] for name in role.verify_after if name in declared]
        # Against the project, not this round's tree: a check's command is a fact
        # about the repository, and a worktree is a copy that may not carry the
        # file it was written into. `start` refuses a run whose checks nobody has
        # answered, so anything missing here appeared between then and now.
        project = Path(self.record.project or self.workdir)
        specs, missing = resolve_checks(project, self.spec.name, wanted)
        for name in missing:
            logger.error(
                "stint {} round {}: nothing says what {!r} runs here, so it was not measured",
                self.record.stint_id,
                self.index,
                name,
            )
        if not specs:
            return []
        display, screen = self._screen(specs)
        try:
            results = run_checks(
                specs,
                cwd=self.workdir,
                log_dir=self.artifacts_dir / f"round-{self.index:02d}" / "checks",
                display=display,
                env=curated_env(),
            )
        finally:
            if screen is not None:
                screen.terminate()
        for result in results:
            self._results[result.name] = result
        # A skipped check is not a failure. Handing one back would ask a role to
        # fix the machine it is running on, forever, on every round.
        return [result for result in results if result.status in ("failed", "timeout")]

    def _screen(self, specs: Sequence[CheckSpec]) -> tuple[Any, Any]:
        """A display for the checks that render, and the process holding it open.

        Started per batch and torn down in the caller's ``finally``: a stint runs
        for hours and an Xvfb nobody reaps is a process left behind on every
        round. A machine with no way to provide one is not an error -- the checks
        that needed it come back skipped, which is what happened.
        """
        if not any(spec.needs_display for spec in specs):
            return None, None
        display = resolve_display(None, start_xvfb=True)
        try:
            display, process = start_display(display)
        except (OSError, ValueError) as exc:  # noqa: BLE001 - no screen is a result, not a crash
            logger.info("stint {} could not open a display for its checks: {}", self.record.stint_id, exc)
            process = None
        # The unavailable display is returned rather than ``None``: ``run_check``
        # skips on a display that is not available and *runs* on no display at
        # all, so swallowing the failure here would run the rendering check on a
        # machine that cannot render and call the result a defect.
        return display, process

    def _journal(self, role: RoleEntry, output: str) -> None:
        """This role's account of the round, where the next round reads it.

        Written by the stint rather than asked of the role. The section is a
        declaration on the role, and a handoff that depends on a model
        remembering to write it is a handoff that goes missing on the round it
        mattered -- every round is a new conversation, so what is not written
        here did not happen as far as the next one is concerned.

        Placed after the boundary pass and before the commit: the stint's own
        bookkeeping is not graded as the role's stray write, and it travels in
        the role's own commit rather than sitting loose for the next role to
        find as an unexplained change.
        """
        if not role.journal_section:
            return
        carried = next((e for e in (self.spec.memory or []) if e.append), None)
        path = self.workdir / (carried.path if carried is not None else JOURNAL)
        try:
            append_entry(path, self.index, role.journal_section, output)
        except OSError as exc:  # noqa: BLE001 - a journal that cannot be written must not end the round
            logger.warning("stint {} could not write {}'s journal entry: {}", self.record.stint_id, role.label, exc)

    def _commit(self, node_id: str, role: RoleEntry) -> None:
        """This role's work, as its own commit under its own name.

        One commit a role rather than one a round, so "what did the reviewer
        change" is a question git can answer, and so the next role's boundary is
        measured from a tree this one has finished with.

        Every role of an enforcing stint, not only the hard-enforced ones. A role
        whose work stayed uncommitted was not merely unrecorded: the next role's
        base is this commit, so what the soft role wrote read as the hard role's
        stray write, and the hard role's pass quarantined and reverted it.
        """
        if not self.enforcing or (repository := self.git()) is None:
            return
        try:
            if repository.dirty():
                repository.commit(f"round({self.index:02d}): {role.label}", author=role.label)
            self._bases[node_id] = repository.head()
        except (HistoryError, OSError) as exc:
            logger.warning("stint {} could not commit {}'s work: {}", self.record.stint_id, role.label, exc)

    def _persist(self, mutate: Callable[[StintRecord], None]) -> None:
        """Apply a round's own finding to the stint as it stands on disk.

        The record this context was handed is a snapshot taken when the round
        opened, and the round outlives it: a `stop`, a `pause` or an `answer`
        arrives from a terminal or an RPC while the roles are running and writes
        the file. Writing the snapshot back at the end of each role restored the
        status it was opened with, so the stop was read as `running` at the
        hand-over and another round opened -- the person's instruction lost
        without a word.

        So the file is re-read, the finding applied to *that*, and the result
        kept as what this round now believes. What a round writes here only ever
        grows -- a round entry, a handback count, a question -- which is what
        makes re-reading a merge rather than a guess at who wrote last.
        """
        current = self.store.read(self.record.stint_id) or self.record
        mutate(current)
        self.store.write(current)
        self.record = current

    def _record(
        self,
        report: EnforceReport | None,
        failures: Sequence[CheckResult],
        *,
        handbacks: int = 0,
        role: str = "",
        node_id: str = "",
    ) -> None:
        """Put this role's findings on the round, where the next round reads them.

        Written as each role finishes rather than once at the end: the round may
        be interrupted, and a finding only in memory is a finding nobody has.

        The role is written down as finished under the node it ran as, which is
        what `resume` reads to know what not to run again. The node registry
        says the same thing, but from a different process -- a terminal, an RPC
        with no turn -- the registry of the wrong conversation was read, and a
        finished role was either named where the graph could not see it or run
        a second time. The record travels with the stint; the registry does not.

        The handbacks a role spent are written down as well, because the report
        that reaches here is the last attempt's and a clean one says nothing
        about the two before it: a Verifier handed back three times for the same
        append read, on the record, as a Verifier that stayed inside its paths.
        """

        def apply(record: StintRecord) -> None:
            existing = record.round(self.index)
            entry = record.open_round(self.index, existing.run_id if existing is not None else "")
            if node_id and role:
                entry.finished[role] = node_id
            if handbacks:
                record.handbacks[f"r{self.index:02d}-{role}"] = handbacks
                entry.violations.append(f"{role} was handed back {handbacks} time(s) before this attempt")
            if report is not None:
                entry.violations.extend(report.violations)
            entry.verify = [result.to_dict() for result in self._results.values()]
            if failures:
                entry.violations.append(
                    f"{len(failures)} check(s) still failing when the round moved on: "
                    + ", ".join(result.name for result in failures)
                )

        self._persist(apply)


UNDONE_HEADING = "What you wrote outside your own paths was undone:"


def _complaint(failures: Sequence[CheckResult], report: EnforceReport | None) -> str:
    """What to hand back, in the order the role should read it.

    The boundary first: a role that wrote where it may not has just had that
    work undone, and every other sentence is about a tree that no longer looks
    the way it thinks.

    Two findings with two provenances, so two pieces of writing. A check's
    result is a command's own answer, and saying so is what stops a role
    arguing with it; a boundary is a diff of the tree against a declaration, and
    no command ran at all. Telling a role its stray write came back from running
    something sends it to debug the tooling -- which is a whole round spent
    looking for a command that never ran.
    """
    said: list[str] = []
    if report is not None and not report.clean:
        undone = [t(UNDONE_HEADING), *(f"- {note}" for note in report.violations)]
        said.append(render("stint_boundary_handback", findings="\n".join(undone)))
    if failures:
        lines: list[str] = []
        for result in failures:
            summary = result.summary()
            lines.append(f"`{result.name}` {summary.status}: {summary.detail}")
            tail = (result.stderr_tail or result.stdout_tail or "").strip()
            if tail:
                lines.append("```")
                lines.append(tail[-2000:])
                lines.append("```")
        said.append(render("stint_verify_handback", findings="\n".join(lines).strip()))
    return "\n\n".join(said)
