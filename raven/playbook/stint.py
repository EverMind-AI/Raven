"""Turning a stint playbook into one graph per round, for as many rounds as it takes.

The whole of what ``mode: stint`` adds, and it adds no execution layer: a round
is compiled into an ordinary sub-agent graph and submitted through the ordinary
entry, so validation, quota, scheduling, the five backends and the result path
are the ones that were already there. What is new is only the loop around them,
and the loop is a file plus a callback.

Where each piece runs:

* **start** is on the turn that loaded the playbook. It writes the stint, compiles
  round one and submits it, and returns a receipt immediately -- nothing waits.
* **advance** is on the finishing run's own task, with no turn anywhere. It reads
  what the round left, decides whether another is worth opening, and either
  submits the next one and says nothing, or returns the stint's whole result to
  be announced once.

The driver holds no stint in memory between those two. Everything needed to
compile round twelve is in the stint file, which is what lets a stint outlive the
process that started it.

The main agent is present at both ends and absent in between, which is the
point: a run that takes hours should not hold a conversation open.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from loguru import logger

from raven.playbook.agent_generator import build_payload
from raven.playbook.stint_prompt import (
    fill_round_slots,
    finish_section,
    questions_section,
    round_slots,
    where_section,
)
from raven.playbook.stint_round import RoundContext
from raven.playbook.stint_spec import DEFAULT_MAX_ROUNDS, MAX_ROUNDS, MemoryEntry, RoleEntry, StopSpec
from raven.playbook.types import PlaybookSpec
from raven.stint.git import HistoryError, ProjectGit
from raven.stint.journal import JOURNAL
from raven.stint.record import (
    FINISHED,
    HEARTBEAT_EVERY_SEC,
    INTERRUPTED,
    PAUSED,
    RUNNING,
    STOPPED,
    StintRecord,
    StintRef,
    StintStore,
    make_stint_id,
    mark_adrift,
    peer_stores,
)
from raven.stint.setup import Layout, lay_out

__all__ = [
    "StintDriver",
    "approval",
    "charters_for",
    "compile_round",
    "journal_entry",
    "namespace",
    "terminal_roles",
]

_OUTPUT_REF = re.compile(r"\{\{([A-Za-z0-9_-]+)\.(output|output_path)\}\}")
GUARD_SLOT = "{{round.guard}}"
FINISH_SLOT = "{{round.finish}}"
_RECEIPT_RUN_ID = re.compile(r"DAG run (\S+?) started")


def namespace(playbook: str, index: int, attempt: int = 0) -> str:
    """The id prefix one round's nodes share.

    Carries the round, so a node id is unique across the whole stint with no
    bookkeeping: node ids are claimed for the life of a conversation, and one
    stint submits thirty graphs into one conversation.

    And carries the attempt, for the same reason at a smaller scale: a round
    taken up again after an interruption is a second graph, and the ids of the
    first are claimed whether or not it ever finished.
    """
    return f"{playbook}-r{index:02d}" if attempt <= 0 else f"{playbook}-r{index:02d}x{attempt}"


def charters_for(spec: PlaybookSpec, index: int, attempt: int = 0) -> dict[str, Any]:
    """Each role's charter, keyed by the node it will run as.

    Built from the same ``playbook`` block a delegate row carries, through the
    same builder, so a worker briefed for one turn and a role briefed for thirty
    rounds are narrowed by one piece of code rather than two that drift.
    """
    prefix = namespace(spec.name, index, attempt)
    found: dict[str, Any] = {}
    for role in spec.roles or []:
        if (payload := build_payload("", role.playbook)) is not None:
            found[f"{prefix}-{role.label}"] = payload
    return found


def journal_entry(spec: PlaybookSpec) -> MemoryEntry | None:
    """The carried file a role's own notes go into, if the playbook declares one.

    The append-only one: that is what "a record the next round reads" means, and
    a file a role may rewrite is not a record of anything. A playbook that names
    several takes the first, because the window in a prompt is one window.
    """
    for entry in spec.memory or []:
        if entry.append:
            return entry
    return None


def compile_round(
    spec: PlaybookSpec,
    index: int,
    *,
    journal: str = "",
    verify: Sequence[Mapping[str, Any]] = (),
    satisfied: Mapping[str, str] | None = None,
    attempt: int = 0,
    where: str = "",
) -> list[dict[str, Any]]:
    """One round's roles as one graph's nodes.

    ``satisfied`` maps a role's label to the node id of an *earlier* run that
    already did this role's work, which is how a resumed round skips what it
    finished before it was interrupted. The dependency is still named, and still
    resolves: the graph contract treats a dependency on a node an earlier run of
    the session completed as already met, and its output stays readable.
    """
    done = dict(satisfied or {})
    entry = journal_entry(spec)
    prefix = namespace(spec.name, index, attempt)
    marker = spec.stop.until if spec.stop is not None else ""
    last = terminal_roles(spec)
    nodes: list[dict[str, Any]] = []
    for role in spec.roles or []:
        if role.label in done:
            continue
        slots = round_slots(
            role,
            index=index,
            journal=journal,
            memory=entry,
            verify=verify,
            where=where,
            finish=finish_section(marker, role.label in last),
        )
        node: dict[str, Any] = {
            "id": f"{prefix}-{role.label}",
            "subagent": role.name,
            "node_summary": role.node_summary or f"round {index}: {role.label}",
            "prompt_template": _resolve_labels(_told(role, slots), prefix, done),
            "depends_on": [_node_id(dep, prefix, done) for dep in role.depends_on],
        }
        if role.mcps is not None:
            node["mcps"] = list(role.mcps)
        if role.skills is not None:
            node["skills"] = list(role.skills)
        nodes.append(node)
    return nodes


def _told(role: RoleEntry, slots: Mapping[str, str]) -> str:
    """The role's prompt with the round in it, and with its boundary in it.

    ``{{round.guard}}`` places the boundary where the author wants it. Leaving
    the slot out does not opt out of having one: a role that declares what it
    owns is enforced against that declaration whether or not the prompt
    mentions it, and a boundary a role was never told about is a trap rather
    than a rule. So an author who omits the slot gets it appended; one who
    declares nothing gets nothing, because there is nothing to say.

    ``{{round.finish}}`` is placed the same way and appended for the same
    reason: the stint watches for a word, and a role that was never told the word
    cannot say it.
    """
    prompt = fill_round_slots(role.prompt_template, slots)
    if GUARD_SLOT not in role.prompt_template and (role.owns or role.appends or role.reads):
        prompt = f"{prompt.rstrip()}\n\n{slots['guard']}"
    if FINISH_SLOT not in role.prompt_template and slots["finish"]:
        prompt = f"{prompt.rstrip()}\n\n{slots['finish']}"
    return prompt


def terminal_roles(spec: PlaybookSpec) -> set[str]:
    """The roles nothing else waits on -- the ones the stint actually reads.

    The same shape the runner uses to pick a run's terminal outputs, which is
    the only text `_stop_reason` gets to look in. Read off the declared roles
    rather than off the round being compiled: on a round taken up again the
    roles that already finished are not submitted, and the last one still
    running would inherit a say it was never given.
    """
    waited_on = {dep for role in spec.roles or [] for dep in role.depends_on}
    return {role.label for role in spec.roles or []} - waited_on


def _node_id(label: str, prefix: str, satisfied: Mapping[str, str]) -> str:
    return satisfied.get(label) or f"{prefix}-{label}"


def _resolve_labels(template: str, prefix: str, satisfied: Mapping[str, str]) -> str:
    """``{{planner.output}}`` to the node id the planner actually ran under.

    A playbook names the other *role*, because a label is what its author knows;
    the graph runner resolves node ids. The two differ by the round, and on a
    resumed round by more than the round.
    """

    def resolve(match: "re.Match[str]") -> str:
        label, field = match.group(1), match.group(2)
        return "{{" + f"{_node_id(label, prefix, satisfied)}.{field}" + "}}"

    return _OUTPUT_REF.sub(resolve, template)


@dataclass
class StintDriver:
    """Starts stints, and advances one whenever a round of it finishes."""

    dag_tool: Any
    plans_root: Callable[[str | None], Path]
    #: The project a stint started here works, asked per session rather than
    #: fixed at construction: one driver serves every conversation on the host,
    #: and a conversation pointed at a repository of its own is the ordinary
    #: case, not the exception. A stint takes its answer once, at ``start``, and
    #: records it -- moving the session afterwards does not move a running stint.
    workspace_for: Callable[[str | None], Path]

    #: One beat per stint this process is holding a round for. Keyed by stint id
    #: and cancelled when the round hands over, so the beat stops exactly when
    #: the claim it is making stops being true -- and dies with the process,
    #: which is the case the whole signal exists for.
    _beats: dict[str, "asyncio.Task[None]"] = field(default_factory=dict, repr=False)

    def store_for(self, session_key: str | None) -> StintStore:
        return StintStore(self.plans_root(session_key))

    def workspace_at(self, session_key: str | None) -> Path:
        return Path(self.workspace_for(session_key))

    def hooks(self, ref: StintRef) -> Mapping[str, Any]:
        """What this stint asks the graph runner to do differently for its round.

        Built per round, from the stint file, because the round is about to run
        and the file is where the stint's state is. An empty mapping is a stint
        that asks for nothing, and the round then runs exactly as any graph does.
        """
        store = self.store_for(ref.session_key or None)
        record = store.read(ref.stint_id)
        if record is None:
            logger.warning("stint {} asked for round hooks with no record on disk", ref.stint_id)
            return {}
        spec = PlaybookSpec.model_validate(record.spec)
        # The attempt this round is on, not nought. A round taken up again
        # compiles its nodes under an attempt suffix (`...-r01x1-developer`),
        # and the tool wraps a backend only on an exact node-id match -- so a
        # charter keyed for attempt nought reaches nothing, and every role still
        # pending after a restart runs with no charter at all. The layer that
        # refuses a stray write before it lands is exactly the one that
        # disappears, and it disappears quietly.
        entry = record.round(ref.round_index)
        attempt = entry.attempt if entry is not None else 0
        context = RoundContext(
            spec=spec,
            record=record,
            store=store,
            index=ref.round_index,
            workdir=Path(ref.workdir or record.workdir),
            artifacts_dir=store.artifacts_for(record.stint_id),
        )
        return {
            "on_node_start": context.node_started,
            "judge_node": context.judge,
            "unanswered": context.record_question,
            "max_continuations": context.max_handbacks,
            "charters": charters_for(spec, ref.round_index, attempt),
        }

    def _start_beat(self, record: StintRecord, store: StintStore) -> None:
        """Say once a minute that this process still holds this stint.

        The stamp is what lets a second process tell a working stint from one
        whose host is gone, and a round can run for hours between two ordinary
        writes. Every failure here is swallowed and logged: a missed beat costs
        at most one more sweep, and a beat that raised would take the round with
        it -- the wrong trade for a signal that only exists to tidy up after a
        crash.
        """
        stint_id = record.stint_id

        async def beat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_EVERY_SEC)
                try:
                    current = store.read(stint_id)
                    if current is None or current.status != RUNNING:
                        return
                    store.write(current)
                except Exception as exc:  # noqa: BLE001 - a missed beat is not a failed round
                    logger.debug("stint {} could not be touched: {}", stint_id, exc)

        self._stop_beat(stint_id)
        try:
            self._beats[stint_id] = asyncio.create_task(beat())
        except RuntimeError:
            # No running loop: a synchronous caller (a test, a one-shot) whose
            # round is not going to outlive it anyway.
            logger.debug("stint {} runs with no beat: no loop to hang one on", stint_id)

    def _stop_beat(self, stint_id: str) -> None:
        task = self._beats.pop(stint_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _in_flight(self, record: StintRecord) -> bool:
        """A round of this stint that some task in this process is still running.

        ``active_run_ids`` is this process's own memory, so a false answer means
        "not here" rather than "nowhere" -- enough to stop promising a round
        nobody is working on, and not enough to declare the stint dead. ``adrift``
        is the verb that declares.
        """
        entry = record.round(record.round_index)
        return entry is not None and bool(entry.run_id) and entry.run_id in set(self.dag_tool.active_run_ids())

    def _already_running(self, store: StintStore, playbook: str, project: Path) -> StintRecord | None:
        """A stint on this project that is not over, whatever playbook it runs.

        The project, deliberately not the pair of project and playbook. What
        cannot be had twice is a repository: each stint cuts a worktree from the
        same HEAD and commits to a branch of its own, so a second one diverges
        from the first whether or not they were started from the same file. The
        pairing let two near-identical playbooks past this gate on one
        repository, which is how it was found.

        Asked across every conversation's store rather than this one's. A second
        window on the same repository is a new conversation with a store of its
        own, and that is precisely the case this exists for: the stint the person
        started yesterday is invisible to the driver that would start today's.
        """
        del playbook  # named by the caller for the message, not for the match
        for peer in peer_stores(store.root):
            for record in peer.list():
                if not record.unfinished:
                    continue
                if record.project and Path(record.project) == project:
                    return record
        return None

    async def start(
        self, spec: PlaybookSpec, *, values: Mapping[str, str] | None = None, max_rounds: int | None = None
    ) -> str:
        """Write the stint, submit its first round, and return the receipt.

        ``max_rounds`` is the one thing about a run its caller may overrule the
        playbook on. Which model a role uses and what it may reach are the
        author's and the roster's, because a playbook is a file that travels and
        a caller who could change those could change what a trusted name does.
        How long to keep going is not like that: it is a question about this
        run, on this project, this afternoon, and the person answering it is the
        one who reads the answer. It also lands in the one place the whole run
        is approved, because the approval names the number.

        Absent means the playbook's own, which is `DEFAULT_MAX_ROUNDS` when the
        playbook says nothing either.
        """
        if max_rounds is not None:
            if not 1 <= max_rounds <= MAX_ROUNDS:
                return (
                    f"Error: a stint runs between 1 and {MAX_ROUNDS} rounds, so it cannot be started for {max_rounds}."
                )
            spec = _with_budget(spec, max_rounds)
        origin = dict(self.dag_tool.turn_origin())
        store = self.store_for(origin.get("session_key"))
        project = self.workspace_at(origin.get("session_key"))
        if (already := self._already_running(store, spec.name, project)) is not None:
            return _second_plan_refused(already, spec.name, project)
        record = StintRecord(
            stint_id=make_stint_id(),
            playbook=spec.name,
            # The whole spec, not the machine block: a stint resumed tomorrow must
            # not depend on the playbook still being installed, on its parameters
            # still being in somebody's conversation, or on nobody having edited
            # the library in between.
            spec=spec.model_dump(by_alias=True, exclude_none=True),
            values=dict(values or {}),
            workdir=str(project),
            project=str(project),
            round_index=1,
            status=RUNNING,
            origin=origin,
        )
        layout = None
        if spec.setup is not None:
            # Before the checkout, because a project that cannot hold a stint
            # should be told so before one is opened on it, and because what
            # this writes has to exist for the checkout to carry it.
            layout = lay_out(project, spec.setup)
            if not layout.ready:
                return f"Error: {layout.missing}"
        if problem := self._open_tree(spec, record, layout):
            return problem
        store.write(record)
        receipt = await self._submit(spec, record, store, index=1, confirm=spec.confirm, layout=layout)
        if receipt.startswith("Error"):
            record.status = STOPPED
            record.stop_reason = "the first round was refused"
            store.write(record)
        return receipt

    async def _report(self, spec: PlaybookSpec, record: StintRecord, finished: Any) -> None:
        """Say a round is done, for a person who cannot otherwise tell.

        A stint that speaks only when it stops is indistinguishable from a stint
        that hung, and at eighteen minutes a round that is hours of it. The cost
        is a main-agent turn a round, so a playbook that would rather have the
        silence says ``stop.report: end``.
        """
        if spec.stop is not None and spec.stop.report == "end":
            return
        say = getattr(self.dag_tool, "say", None)
        if say is None:
            return
        checks = ", ".join(f"{row.get('name')}={row.get('status')}" for row in finished.verify) or "no checks"
        line = (
            f"Stint {record.stint_id} ({record.playbook}) finished round {finished.index} "
            f"of at most {_budget(spec)}: {checks}"
        )
        if finished.violations:
            line += f"; {len(finished.violations)} boundary violation(s) undone"
        waiting = [q for q in record.questions if not str(q.get("answer") or "").strip()]
        if waiting:
            line += f"; {len(waiting)} unanswered question(s)"
        line += ". The next round is starting."
        await say(
            finished.run_id or record.stint_id, f"{line}\n\n{_what_to_do(record, waiting)}", record.origin or None
        )

    def adrift(self, session_key: str | None = None, *, everywhere: bool = False) -> list[StintRecord]:
        """Plans that believe they are running and are not, marked as what they are.

        A stint's own file is the only claim that it is going; the process that
        wrote it may be long gone. Nothing else notices -- a host that died
        mid-round writes nothing on its way out, so the record says ``running``
        forever and `stint list` reports a corpse as work in progress.

        Marking is separated from taking up on purpose. Finding one costs
        nothing; continuing it spends money and hours, and a person who opened a
        window to ask an unrelated question did not ask for that.

        ``everywhere`` looks across every conversation's store, for a caller that
        is asking about the machine rather than about one conversation.

        Judged on the stamp the holder keeps moving (``StintRecord.stale``), not
        on this process's own ``active_run_ids``: that set only ever answered
        "not mine", so a second host read it as "dead" and a person who then
        resumed would have two hosts advancing one stint. A round this process
        is running is skipped by both tests, which costs nothing and means a
        beat that missed a write cannot make us call our own work adrift.

        ``everywhere`` looks across every conversation's store, for a caller that
        is asking about the machine rather than about one conversation.
        """
        store = self.store_for(session_key)
        stores = peer_stores(store.root) if everywhere else [store]
        mine = {record.stint_id for peer in stores for record in peer.live() if self._in_flight(record)}
        return mark_adrift(stores, held=mine)

    async def sweep(self, session_key: str | None = None) -> list[str]:
        """Take up every stint here that believes it is running and is not."""
        taken: list[str] = []
        for record in self.adrift(session_key):
            await self.resume(record.stint_id, session_key)
            taken.append(record.stint_id)
        return taken

    async def advance(self, ref: StintRef, run_id: str, result: Any, stopped: bool) -> Any | None:
        """One round finished. Open the next, or hand the stint's result back.

        ``None`` means there is nothing to announce: either the stint is still
        going, or it was stopped, and a stopped run says nothing here for the
        same reason it says nothing anywhere else. Anything returned is
        announced once, as the stint's own result.
        """
        # Stopped first, and whatever follows: this process no longer holds a
        # round of this stint, and a beat still claiming it would keep a record
        # that nothing is advancing looking alive. `_submit` starts a new one if
        # this hand-over opens another round.
        self._stop_beat(ref.stint_id)
        store = self.store_for(ref.session_key or None)
        record = store.read(ref.stint_id)
        if record is None:
            # The stint's own file is how it knows anything. Without it the round
            # still ran and its result is still worth saying, so it is announced
            # as an ordinary graph's would be.
            logger.warning("stint {} finished round {} with no record on disk", ref.stint_id, ref.round_index)
            return result
        finished = record.open_round(ref.round_index, run_id)
        finished.status = "stopped" if stopped else "completed"
        finished.summary = _text_of(result)

        if stopped:
            record.status = STOPPED
            record.stop_reason = "the user stopped it"
            store.write(record)
            return None

        if record.status == STOPPED:
            # Stopped out of band, between this round starting and finishing --
            # somebody ran `playbook stint stop` while it was working. The round
            # is not thrown away, it is just the last one.
            record.stop_reason = record.stop_reason or "a person stopped the stint"
            store.write(record)
            return _summary(record)

        if record.status == PAUSED:
            # Paused out of band, the same way. Unlike a stop this is not the
            # end: no round opens, nothing is announced, and the stint keeps
            # every round it has so `stint resume` can take it up where the pause
            # caught it. A pause that summarised would read as a stint that ended.
            record.stop_reason = record.stop_reason or "a person paused the stint"
            store.write(record)
            logger.info("stint {} is paused after round {}; no further round opens", record.stint_id, ref.round_index)
            return None

        spec = PlaybookSpec.model_validate(record.spec)
        if reason := _stop_reason(spec, record, finished.summary):
            record.status = FINISHED
            record.stop_reason = reason
            store.write(record)
            return _summary(record)

        # Said here rather than above, so the round that turns out to be the last
        # one does not promise a next: what a person hears about that one is the
        # stint's summary, which is already on its way.
        await self._report(spec, record, finished)

        record.round_index = ref.round_index + 1
        store.write(record)
        receipt = await self._submit(spec, record, store, index=record.round_index, confirm=False)
        if receipt.startswith("Error"):
            record.status = FINISHED
            record.stop_reason = f"round {record.round_index} could not start ({receipt})"
            store.write(record)
            return _summary(record)
        return None

    def _open_tree(self, spec: PlaybookSpec, record: StintRecord, layout: Layout | None = None) -> str:
        """Give the stint a checkout of its own, or say why it cannot have one.

        A stint edits files and commits for hours. Sharing the session's checkout
        means the person who started it cannot use their own tree until it is
        done, and every branch either of them switches to surprises the other.
        A worktree costs one directory and removes the whole class.

        A stint that enforces boundaries and has no repository to enforce them
        against is refused rather than started: it would run to the end looking
        like it was being held to its declarations, having held nobody to
        anything.
        """
        enforcing = any(role.owns or role.appends for role in (spec.roles or []))
        project = Path(record.workdir)
        repository = ProjectGit(project)
        if not (project / ".git").exists():
            if enforcing:
                return (
                    f"Error: '{spec.name}' declares what each role owns, and that is enforced by undoing "
                    f"what a role wrote outside it -- which needs a git repository. {project} is not "
                    "one. Initialise it, or remove the owns/appends declarations."
                )
            logger.info(
                "stint {} works {} in place: no repository, so no checkout of its own", record.stint_id, project
            )
            return ""
        branch = f"stint/{record.stint_id}"
        tree = (
            self.store_for(str(record.origin.get("session_key") or "") or None).artifacts_for(record.stint_id) / "tree"
        )
        try:
            repository.worktree_add(tree, branch, repository.head())
        except (HistoryError, OSError) as exc:
            logger.error("stint {} could not open a checkout of its own ({}); working in place", record.stint_id, exc)
            return ""
        _carry_layout(project, tree, layout)
        record.workdir = str(tree)
        record.branch = branch
        return ""

    async def extend(self, stint_id: str, rounds: int, session_key: str | None = None) -> str:
        """Give a stint more rounds than it was started with, and open one if it is over.

        The budget is the one thing about a stint a person routinely judges wrong,
        and they find out only once it is spent: five rounds of good work end on
        ``the round budget of 5 is spent``, and what is wanted next is a sixth.

        Without this there was nothing that gave them one. ``resume`` takes up a
        round that was interrupted and a stint that ran its budget out has none,
        so the only move left was to start a second stint -- which cuts its
        worktree from the project's HEAD, and the first stint's work is on a
        branch nobody merged. That starts again from before the first commit,
        and says nothing about having done so. "Three more rounds" does not mean
        that.

        Raising the budget on a stint still going is the same verb and costs
        nothing extra: no round is opened, because one is already in flight and
        ``advance`` reads the budget from the file when it lands.

        The round opens without asking again. The one approval a stint gets named
        a round count, and this is a person changing that count deliberately --
        asking them to approve what they just typed is not a second opinion.
        """
        store = self.store_for(session_key)
        record = store.read(stint_id)
        if record is None:
            return f"Error: no stint {stint_id} here."
        if rounds < 1:
            return f"Error: {rounds} is not more rounds."
        spec = PlaybookSpec.model_validate(record.spec)
        budget = _budget(spec) + rounds
        if budget > MAX_ROUNDS:
            return (
                f"Error: that would give {stint_id} a budget of {budget}, and {MAX_ROUNDS} is the most "
                f"any stint may have. It has {_budget(spec)} now."
            )
        spec = _with_budget(spec, budget)
        record.spec = spec.model_dump(by_alias=True, exclude_none=True)
        if record.unfinished:
            store.write(record)
            if record.status == RUNNING and self._in_flight(record):
                return f"Stint {stint_id} may now run {budget} rounds. The round in flight is no longer its last."
            return (
                f"Stint {stint_id} may now run {budget} rounds. No round of it is going here, so nothing is "
                f"about to read that -- if nothing elsewhere is advancing it, take it up with "
                f"`raven playbook stints resume {stint_id}`."
            )
        # An over stint is not going to be advanced by anything, so raising its
        # budget alone would leave it exactly where it was. Open the round here.
        if not Path(record.workdir).is_dir():
            return (
                f"Error: {stint_id} worked in {record.workdir}, which is not there any more. "
                "Its rounds cannot be continued from a checkout that is gone."
            )
        # The guard `start` has, on the other way in. A stint that ended releases
        # its project, and the next stint may already have taken it; reviving this
        # one would put two of them on one repository, each committing from a
        # base the other does not have. It cannot match itself here -- a record
        # this branch is reached with is over, and the question is about the
        # ones that are not.
        project = Path(record.project or record.workdir)
        if (already := self._already_running(store, record.playbook, project)) is not None:
            return (
                f"Error: {stint_id} cannot open another round on {project}: {already.stint_id} is {already.status} "
                f"there and has been since this one ended. End that one first, or give it the rounds instead."
            )
        # The round it last ran, plus one -- except where it never finished one,
        # as a stint whose first round was refused never did. Then this is that
        # round, because skipping it would leave the stint without one.
        index = record.round_index + (1 if _round_done(record) else 0)
        record.status = RUNNING
        record.stop_reason = ""
        record.round_index = index
        store.write(record)
        receipt = await self._submit(spec, record, store, index=index, confirm=False)
        if receipt.startswith("Error"):
            record.status = FINISHED
            record.stop_reason = f"round {index} could not start ({receipt})"
            store.write(record)
        return receipt

    async def resume(self, stint_id: str, session_key: str | None = None) -> str:
        """Take an interrupted stint up again, from the node it stopped at.

        Not from the round it stopped at: a round is several roles, and re-running
        a role that already finished would redo work, re-measure a boundary
        against the wrong baseline, and spend a dispatch on an answer already on
        disk. The finished nodes are named as dependencies of the new graph
        instead, which is legal and keeps their output readable -- a dependency
        an earlier run of the session completed is already met.
        """
        store = self.store_for(session_key)
        record = store.read(stint_id)
        if record is None:
            return f"Error: no stint {stint_id} here."
        if not record.unfinished:
            return f"Stint {stint_id} is {record.status} and has nothing left to take up."
        if record.status == RUNNING and not record.stale():
            # Still saying it is running, and still being touched: something is
            # holding it, and it need not be this process -- the stamp is the one
            # signal that crosses a process boundary. Two hosts advancing one
            # stint is the failure the heartbeat exists to prevent, and a person
            # reaching for `resume` is how it would happen.
            #
            # Narrowed to `running` on purpose. A paused or interrupted record
            # has a fresh stamp too -- from the write that paused or marked it --
            # and taking those up is exactly what this verb is for.
            return (
                f"Stint {stint_id} says it is running and was touched moments ago, so something is "
                f"still working it. Wait for it, or stop it first."
            )
        if self._in_flight(record):
            # Taking up a round that is going would put two graphs on one
            # checkout: the same roles, the same paths, each judged against a
            # baseline the other is moving. The other guards here read the file,
            # and the file cannot say this -- only the process holding the round
            # knows, and this is it.
            return (
                f"Stint {stint_id} is working round {record.round_index} here right now, so there is "
                f"nothing to take up. Stop it first if you want it to start that round over."
            )
        spec = PlaybookSpec.model_validate(record.spec)
        if _round_done(record):
            # A pause lands *between* rounds, not inside one: the round in flight
            # finishes and is recorded, and only the next is stopped from
            # opening. So what this stint is missing is that next round -- putting
            # the finished one up again finds every role already done and comes
            # back with nothing left to run, which is what a paused stint got.
            if record.round_index >= _budget(spec):
                return (
                    f"Stint {stint_id} finished round {record.round_index} of {_budget(spec)} before it "
                    f"{record.status}, so the round after it is past its budget. Give it more with "
                    f"`raven playbook stints extend {stint_id} --rounds N`."
                )
            index, attempt, satisfied = record.round_index + 1, 0, {}
            record.round_index = index
        else:
            index = record.round_index or 1
            previous = record.round(index)
            attempt = (previous.attempt if previous is not None else 0) + 1
            satisfied = await self._completed_of(record, spec, index, previous)
        record.status = RUNNING
        store.write(record)
        receipt = await self._submit(
            spec, record, store, index=index, confirm=False, attempt=attempt, satisfied=satisfied
        )
        if receipt.startswith("Error"):
            record.status = INTERRUPTED
            store.write(record)
        return receipt

    async def _completed_of(self, record: StintRecord, spec: PlaybookSpec, index: int, previous: Any) -> dict[str, str]:
        """Which roles of the interrupted round already finished, by node id."""
        if previous is None or not previous.run_id:
            return {}
        try:
            nodes = await self.dag_tool.session_nodes(str(record.origin.get("session_key") or "") or None)
        except Exception as exc:  # noqa: BLE001 - a resume with no registry redoes the round
            logger.warning("stint {} could not read what its last round finished: {}", record.stint_id, exc)
            return {}
        prefix = namespace(spec.name, index, previous.attempt)
        # `is_readable` is both halves at once, and both matter: a node that
        # completed but left no output is not something a later role can read,
        # so naming it as a dependency would hand the round a reference that
        # resolves to nothing.
        return {
            role.label: f"{prefix}-{role.label}"
            for role in (spec.roles or [])
            if nodes.is_readable(f"{prefix}-{role.label}")
        }

    async def _submit(
        self,
        spec: PlaybookSpec,
        record: StintRecord,
        store: StintStore,
        *,
        index: int,
        confirm: bool,
        attempt: int = 0,
        satisfied: Mapping[str, str] | None = None,
        layout: Layout | None = None,
    ) -> str:
        entry = journal_entry(spec)
        nodes = compile_round(
            spec,
            index,
            journal=_with_questions(_read_carried(Path(record.workdir), entry), record),
            verify=_last_verify(record, index),
            satisfied=satisfied,
            attempt=attempt,
            where=where_section(record.workdir, bool(record.branch)),
        )
        if not nodes:
            # Every role of this round already finished, which is what a stint
            # interrupted between its last node and its hand-over looks like.
            return "Error: every role of this round had already finished, so there was nothing left to run."
        receipt = await self.dag_tool.execute(
            nodes,
            task_summary=f"{spec.name}: round {index} of at most {_budget(spec)}",
            background=True,
            confirm=confirm,
            stint=record.ref(index),
            origin=record.origin or None,
            # Built when asked rather than now, so a round that is not going to
            # be asked about does not pay for the text.
            confirm_question=lambda: approval(spec, record, layout),
        )
        text = _text_of(receipt)
        record.open_round(index, _run_id_of(text), attempt=attempt)
        store.write(record)
        # After the write, so the first beat cannot race the record into being.
        self._start_beat(record, store)
        return text


def _stop_reason(spec: PlaybookSpec, record: StintRecord, last_summary: str) -> str:
    """Why this stint opens no further round, or empty if it should."""
    marker = spec.stop.until if spec.stop is not None else ""
    if marker and _reported(marker, last_summary):
        return f"a role reported {marker!r}"
    budget = _budget(spec)
    if record.round_index >= budget:
        return f"the round budget of {budget} is spent"
    return ""


def _what_to_do(record: StintRecord, waiting: Sequence[Mapping[str, Any]]) -> str:
    """What the reader of a round's progress is expected to do about it.

    Said because without it the reader improvises. A round that ended with a
    failed check or an undone write reads like a problem to solve, and a main
    agent that went and solved it would be editing the same checkout the next
    round is about to work -- two hands on one tree, which is the whole reason
    the stint has a checkout of its own.

    So: everything except an unanswered question is news, and the only thing
    that is not is named with the command that settles it.
    """
    if not waiting:
        return (
            "This is progress, not a request. The stint opens the next round itself, a check that stayed "
            "failed and a write that was undone are already recorded where the next round's roles read "
            "them, and the work is on a branch of its own. Do not edit the stint's checkout -- the next "
            "round is about to. Relay this if the user is here; otherwise nothing is needed."
        )
    first = str(waiting[0].get("text") or "").strip().splitlines()[0][:160]
    return (
        f"One of these needs a person, and the stint goes on without an answer: {first!r}. Show the user "
        f"`raven playbook stints get {record.stint_id}` for all of them, and when they say what to answer, "
        f'record it with `raven playbook stints answer {record.stint_id} -q <number> -t "..."`. It reaches '
        "the round after the one now running. Everything else here is news: do not edit the stint's checkout."
    )


def _second_plan_refused(running: StintRecord, playbook: str, project: Path) -> str:
    """Why a second stint was not started, and the two ways out of it.

    A refusal rather than a question, because the surfaces that cannot ask are
    the ones where the mistake is worst: a cron trigger or a reconnecting client
    that quietly opened a second stint would run two of them against one
    repository, on two branches, each redoing the other's work. The graph
    confirm gate runs when nobody can be asked; this one must not.
    """
    reached = running.round_index
    same = " " if running.playbook == playbook else f" (running {running.playbook}) "
    return (
        f"Error: a stint is already{same}on {project}: {running.stint_id}, on round {reached}. "
        f"Take that one up again with `raven playbook stints resume {running.stint_id}`, or end it with "
        f"`raven playbook stints stop {running.stint_id}` and start fresh. Starting a second stint here would "
        "put two of them on one repository, each working from a base that does not have the other's work."
    )


def _summary(record: StintRecord) -> str:
    """What the person who started the stint is told, once, at the end."""
    done = [entry for entry in record.rounds if entry.status == "completed"]
    violations = [note for entry in record.rounds for note in entry.violations]
    where = f"It worked in {record.workdir}"
    # The branch is said because nothing merges it. A stint commits to a branch
    # of its own and the project it was started from is untouched, so a reader
    # told only the directory has been told the work is somewhere it is not.
    where += f", on branch {record.branch}, which nothing has merged." if record.branch else "."
    lines = [
        f"Stint {record.stint_id} ({record.playbook}) ran {len(done)} round(s) and stopped: {record.stop_reason}.",
        where,
        # Said whatever stopped it: a budget spent and a person's stop both end
        # here, and from here there is one way to get another round. Without it
        # the reader's only visible move is to start a second stint, which begins
        # again from before this one's first commit.
        f"More rounds on this same tree: `raven playbook stints extend {record.stint_id} --rounds N`.",
    ]
    if violations:
        lines.append(f"{len(violations)} boundary violation(s) were undone; the stint's record names them.")
    if record.questions:
        lines.append(f"{len(record.questions)} question(s) are waiting for a person.")
    if done:
        lines.append("The last round reported:")
        lines.append(done[-1].summary.strip()[:2000])
    return "\n".join(lines)


def _reported(marker: str, summary: str) -> str | None:
    """A line of the round's output that is the marker, and nothing else.

    A line rather than a substring, because the role told to write the word is
    also the role most likely to mention it: "the planner claims NOTHING-LEFT,
    but I found three things" would end the stint on the sentence disputing it.
    A whole line saying only the word cannot be an aside.
    """
    return next((line for line in summary.splitlines() if line.strip() == marker), None)


def _carry_layout(project: Path, tree: Path, layout: Layout | None) -> None:
    """Copy what the setup pass just wrote into the stint's fresh checkout.

    A worktree is cut from ``HEAD``, and what setup wrote is not committed --
    deliberately, because committing to somebody's branch for a run they have
    not approved yet is not setup's to do. So the files exist in the project and
    not in the checkout, and every round would fail rendering ``{{ref:}}``
    against a file that is right there in the directory the person is looking
    at. Copied instead: the person keeps their copy untracked and decides what
    to do with it, and the stint's own branch carries one its rounds can commit.
    """
    if layout is None or not layout.wrote:
        return
    for name in layout.wrote:
        source, target = project / name, tree / name
        if not source.is_file() or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def approval(spec: PlaybookSpec, record: StintRecord, layout: Layout | None = None) -> str:
    """What a person is shown before a stint's first round.

    It is the only thing they are shown about the whole run: the gate is asked
    once, at round one, and every round after it is already decided. The generic
    graph question lists node ids and agent names, which for a stint describes
    three steps and omits that they repeat thirty times running shell commands
    on this machine -- so a person approved the visible tenth of it.

    What goes in, and why each:

    * the round budget, because it is the size of what is being agreed to;
    * every ``verify`` command **in full**, because they execute here and a
      playbook is a file that travels; this is the one moment to read them;
    * the branch, because the answer to "will this touch my working tree";
    * how it can end early, because otherwise the budget reads as a promise;
    * what the roles may write, as counts and the first few -- ownership
      restricts rather than grants, and listing forty globs is how a question
      becomes a wall nobody reads. The full list is on the playbook's page.

    ``extra`` lines are for what the run had to do before it could ask -- the
    files a setup pass wants to commit, the specification it picked, the backlog
    it proposed. They are last because they are about this project rather than
    about the playbook.
    """
    budget = _budget(spec)
    chain = " -> ".join(role.label for role in (spec.roles or [])) or "(no roles)"
    lines = [
        f'Start "{spec.name}" on {record.project or record.workdir}?',
        "",
        f"  up to {budget} round(s) of: {chain}",
    ]
    if record.branch:
        lines.append(f"  on branch {record.branch}, in a checkout of its own -- your working tree is untouched")
    if spec.verify:
        lines.append("  every round may run, on this machine:")
        lines.extend(f"      {check.run}" for check in spec.verify)
    for role in spec.roles or []:
        if paths := [*role.owns, *role.appends]:
            shown = ", ".join(paths[:3])
            more = f" and {len(paths) - 3} more" if len(paths) > 3 else ""
            undone = "" if role.enforce.write == "hard" else "; writes outside are recorded, not undone"
            lines.append(f"  {role.label} writes {shown}{more}{undone}")
    marker = spec.stop.until if spec.stop is not None else ""
    lines.append(
        f"  it stops early only if the last role writes {marker}" if marker else "  nothing ends it before the budget"
    )
    lines.extend(_about_this_project(layout))
    return "\n".join(lines)


def _about_this_project(layout: Layout | None) -> list[str]:
    """What the run had to do to this project before it could ask.

    Last, because it is about the project rather than the playbook, and said at
    all because both halves are judgements a person should get to overrule: the
    files are new in their repository, and the specification was *picked* out of
    the documents there rather than named by anyone.
    """
    if layout is None:
        return []
    lines = []
    if layout.wrote:
        lines.append(f"  it has just written {len(layout.wrote)} file(s) here, untracked -- {', '.join(layout.wrote)}")
    if layout.spec:
        others = f" ({layout.also_matched} other document(s) matched)" if layout.also_matched else ""
        lines.append(f"  the roles stint from {layout.spec}{others}")
    if layout.backlog >= 0:
        lines.append(f"  {layout.backlog} task(s) in the backlog")
    return lines


def _round_done(record: StintRecord) -> bool:
    """The round this stint is on has finished, so what it is missing is a new one.

    Asked by both verbs that take a stint up again, because both have to tell the
    stint that stopped *between* rounds from the one that stopped *inside* one,
    and a second copy of the question would be a second answer waiting to differ.
    Only ``completed`` counts: a round cancelled part-way left work half done,
    and putting the next one on top of it would build on that.
    """
    entry = record.round(record.round_index)
    return entry is not None and entry.status == "completed"


def _budget(spec: PlaybookSpec) -> int:
    return spec.stop.max_rounds if spec.stop is not None else DEFAULT_MAX_ROUNDS


def _with_budget(spec: PlaybookSpec, rounds: int) -> PlaybookSpec:
    """The same spec, running for a different number of rounds.

    A copy rather than an assignment: the spec a stint is started from is the
    library's, shared with whoever loads that playbook next, and a budget set
    for one run must not be what the next one inherits.
    """
    return spec.model_copy(update={"stop": (spec.stop or StopSpec()).model_copy(update={"max_rounds": rounds})})


def _with_questions(journal: str, record: StintRecord) -> str:
    """The carried record, with what a person was asked and said appended to it."""
    section = questions_section(record.questions)
    return f"{journal.rstrip()}\n\n{section}".strip() if section else journal


def _read_carried(workdir: Path, entry: MemoryEntry | None) -> str:
    path = workdir / (entry.path if entry is not None else JOURNAL)
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _last_verify(record: StintRecord, index: int) -> Sequence[Mapping[str, Any]]:
    previous = record.round(index - 1)
    return previous.verify if previous is not None else ()


def _text_of(result: Any) -> str:
    return str(getattr(result, "model_text", result) or "")


def _run_id_of(receipt: str) -> str:
    match = _RECEIPT_RUN_ID.search(receipt or "")
    return match.group(1) if match else ""
