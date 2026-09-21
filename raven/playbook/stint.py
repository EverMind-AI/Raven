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
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from loguru import logger

from raven.agent.subagent.prompt_errors import RoundBudgetSpentError, RoundNotApprovedError
from raven.playbook.agent_generator import build_payload
from raven.playbook.stint_prompt import (
    fill_round_slots,
    finish_section,
    questions_section,
    round_slots,
    where_section,
)
from raven.playbook.stint_round import RoundContext
from raven.playbook.stint_spec import (
    DEFAULT_ISOLATION,
    DEFAULT_MAX_ROUNDS,
    MAX_ROUNDS,
    Isolation,
    MemoryEntry,
    RoleEntry,
    StopSpec,
)
from raven.playbook.types import PlaybookSpec
from raven.stint import backlog as backlog_mod
from raven.stint.backlog import STINT_DIR
from raven.stint.checks import CHECKS_FILE, resolve_checks
from raven.stint.git import HistoryError, ProjectGit
from raven.stint.journal import JOURNAL
from raven.stint.record import (
    FINISHED,
    HEARTBEAT_EVERY_SEC,
    INTERRUPTED,
    PAUSED,
    RUNNING,
    STALE_AFTER_SEC,
    STOPPED,
    StintRecord,
    StintRef,
    StintStore,
    make_stint_id,
    mark_adrift,
    peer_stores,
    stint_token,
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
_ROUND_OUTCOME = re.compile(r"finished: (\d+) completed, (\d+) failed")


def namespace(playbook: str, index: int, attempt: int = 0, token: str = "") -> str:
    """The id prefix one round's nodes share.

    Carries the round, so a node id is unique across the whole stint with no
    bookkeeping: node ids are claimed for the life of a conversation, and one
    stint submits thirty graphs into one conversation.

    And carries the attempt, for the same reason at a smaller scale: a round
    taken up again after an interruption is a second graph, and the ids of the
    first are claimed whether or not it ever finished.

    And the stint's own ``token``, for the same reason at the largest scale: a
    second stint of one playbook in one conversation is a second set of thirty
    graphs, and without it the second was refused at round one for the ids the
    first still owned (measured 2026-09-21, on the second run of a probe).
    """
    head = f"{playbook}-{token}" if token else playbook
    return f"{head}-r{index:02d}" if attempt <= 0 else f"{head}-r{index:02d}x{attempt}"


def charters_for(spec: PlaybookSpec, index: int, attempt: int = 0, token: str = "") -> dict[str, Any]:
    """Each role's charter, keyed by the node it will run as.

    Built from the same ``playbook`` block a delegate row carries, through the
    same builder, so a worker briefed for one turn and a role briefed for thirty
    rounds are narrowed by one piece of code rather than two that drift.

    Only that block. ``owns`` and ``appends`` are not turned into a charter: a
    role with a boundary and no ``playbook:`` block has no tool gate, and what
    holds its boundary is the prompt and the pass that undoes what it wrote.
    The shipped playbook is such a role, three times over.
    """
    prefix = namespace(spec.name, index, attempt, token)
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
    token: str = "",
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
    prefix = namespace(spec.name, index, attempt, token)
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
    stints_root: Callable[[str | None], Path]
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
    _handing_over: set[str] = field(default_factory=set, repr=False)

    def store_for(self, session_key: str | None) -> StintStore:
        return StintStore(self.stints_root(session_key))

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
        # compiles its nodes under an attempt suffix (`...-r01x1-builder`),
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
            "adjudicate": context.adjudicate,
            "max_continuations": context.max_handbacks,
            "charters": charters_for(spec, ref.round_index, attempt, record.token),
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
            try:
                while True:
                    await asyncio.sleep(HEARTBEAT_EVERY_SEC)
                    try:
                        current = store.read(stint_id)
                        if current is None or current.status != RUNNING:
                            return
                        store.write(current)
                    except Exception as exc:  # noqa: BLE001 - a missed beat is not a failed round
                        logger.debug("stint {} could not be touched: {}", stint_id, exc)
            finally:
                # Off the table once it ends, so `holding()` does not count a
                # beat that has already noticed the stint is over.
                if self._beats.get(stint_id) is asyncio.current_task():
                    self._beats.pop(stint_id, None)

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
            if already.playbook == spec.name and not _held(already):
                # The same playbook asked for again on a project whose stint
                # nobody is advancing -- paused, interrupted, or left running by
                # a host that died -- is a person saying "carry on", and the
                # only move that does not redo the first stint's rounds from an
                # older base is to take that stint up. Said, because what they
                # asked for by name was a start.
                taken = await self.resume(already.stint_id, str(already.origin.get("session_key") or "") or None)
                return (
                    f"Stint {already.stint_id} was already on {project} at round {already.round_index} and nothing "
                    f"was advancing it, so it was taken up rather than started over.\n{taken}"
                )
            return _second_plan_refused(already, spec.name, project)
        stint_id = make_stint_id()
        record = StintRecord(
            stint_id=stint_id,
            token=stint_token(stint_id),
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
            if problem := self._left_in_the_tree(spec, record, layout):
                return problem
            if reasons := _preflight_layout(project, spec):
                return "Error: this project is not ready for a stint:\n" + "\n".join(f"- {r}" for r in reasons)
        if problem := _unanswered_checks(project, spec):
            return problem
        if problem := self._open_tree(spec, record, layout):
            return problem
        store.write(record)
        receipt = await self._submit(spec, record, store, index=1, confirm=spec.confirm, layout=layout)
        if receipt.startswith("Error"):
            # Closed, not left running: a denied round left the record open
            # with round one on it, and the next `resume` -- which asks nobody,
            # the stint having been "approved" -- ran the graph the person had
            # just refused.
            record.status = STOPPED
            record.stop_reason = (
                "the first round was not approved" if receipt == _NOT_APPROVED else "the first round was refused"
            )
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
        # Marked for the whole hand-over, because between the finished run
        # leaving the active set and the next round entering it there is no run
        # and no beat, and a host asking `holding()` in that gap would let go.
        self._handing_over.add(ref.stint_id)
        try:
            return await self._advance(ref, run_id, result, stopped)
        finally:
            self._handing_over.discard(ref.stint_id)

    def holding(self) -> bool:
        """Whether this process is working a round of any stint right now.

        The question a host that is nothing but this driver has to ask before it
        returns: a terminal that opened a round and exits closes the loop under
        it. Answered from this process's own state -- a run in flight, a beat, a
        hand-over between rounds -- and never from the records, which say what
        *some* process holds. A refusal leaves nothing held here, and a record
        another process is beating for is not this one's to wait on.
        """
        if self._handing_over or any(not task.done() for task in self._beats.values()):
            return True
        live = set(self.dag_tool.active_run_ids())
        if not live:
            return False
        store = self.store_for(None)
        return any(
            (entry := record.round(record.round_index)) is not None and entry.run_id in live
            for record in store.list()
            if record.live
        )

    async def _advance(self, ref: StintRef, run_id: str, result: Any, stopped: bool) -> Any | None:
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
        if not stopped:
            _release_tasks(record, ref.round_index)

        if stopped:
            # Cut short, not ended. The round's roles that finished are committed
            # and readable, the one that was cut left its work uncommitted in the
            # tree, and `resume` puts the round up again with those roles named
            # rather than re-run -- which is the whole point of stopping a round
            # rather than the stint. Recorded as stopped it could not be taken up
            # at all: `unfinished` is false for a stopped stint, and the one verb
            # left, `extend`, re-submitted the same node ids. A stint somebody
            # had already told to stop stays stopped; the cut was its last round.
            if record.status != STOPPED:
                record.status = PAUSED
                record.stop_reason = f"round {ref.round_index} was stopped before it finished"
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
        if _nothing_ran(finished.summary):
            # No role finished, so the round left nothing a next one could
            # build on -- and counting it would spend the budget on a fault the
            # next round hits again identically. Measured: a missing `{{ref:}}`
            # target failed every node at render, and the stint ran three
            # rounds in two seconds. Paused, not stopped: the round stays
            # un-done on the record, so `resume` puts it up again once the
            # cause is fixed.
            finished.status = "failed"
            record.status = PAUSED
            record.stop_reason = f"round {ref.round_index}: no role finished"
            store.write(record)
            await self._say_paused(record, finished.summary)
            return None
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

    async def _say_paused(self, record: StintRecord, summary: str) -> None:
        """Tell whoever is listening that a round did nothing and the stint has stopped advancing."""
        if (say := getattr(self.dag_tool, "say", None)) is None:
            return
        # The node lines carry the errors; the rest is the receipt's boilerplate.
        errors = [line.strip() for line in summary.splitlines() if "error:" in line or "[failed]" in line]
        shown = "\n".join(errors[:6]) or summary.strip()[:600]
        await say(
            record.stint_id,
            f"Stint {record.stint_id} ({record.playbook}) is paused: round {record.round_index} ended with "
            f"no role finished, so nothing was built on and no budget was spent.\n\n{shown}\n\n"
            f"Fix the cause, then `raven playbook stints resume {record.stint_id}` puts the round up again.",
            record.origin or None,
        )

    async def _pause_on_budget(self, record: StintRecord, store: StintStore, index: int, refusal: str) -> str:
        """Park a stint the hourly dispatch budget turned down, and say so.

        A pause rather than a stop, because nothing is wrong with the stint: the
        allowance it ran into recovers as earlier dispatches age out, and every
        round it has done is still on its branch. `stints resume` takes it up at
        the round that did not start.

        Said out loud, unlike a pause a person asked for: that one is known to
        whoever asked, and this one would otherwise be a stint that simply
        stopped advancing with nobody told why.
        """
        record.status = PAUSED
        record.round_index = max(index - 1, 1)
        record.stop_reason = f"the hourly sub-agent dispatch budget is spent before round {index}"
        store.write(record)
        if (say := getattr(self.dag_tool, "say", None)) is not None:
            await say(
                record.stint_id,
                f"Stint {record.stint_id} ({record.playbook}) is paused before round {index}: {refusal}\n\n"
                f"Nothing is lost. Take it up with `raven playbook stints resume {record.stint_id}` "
                f"once the hour's dispatches have aged out.",
                record.origin or None,
            )
        logger.info("stint {} paused before round {}: the dispatch budget is spent", record.stint_id, index)
        return f"Paused: the sub-agent dispatch budget is spent, so round {index} did not start."

    def _left_in_the_tree(self, spec: PlaybookSpec, record: StintRecord, layout: Layout | None = None) -> str:
        """The person's uncommitted work, as the reason a `branch` stint cannot start on it.

        Round one's boundary is measured against an empty base, which reads as
        the whole dirty state -- so work the person left in the tree would be
        graded as the first role's stray write, reverted, and copied into
        `violations/`. Said before anything is opened rather than discovered
        afterwards, and said *first*: a layout file they edited is their work
        before it is a malformed guard file, and the fix is theirs to choose.
        """
        enforcing = any(role.owns or role.appends for role in (spec.roles or []))
        project = Path(record.workdir)
        if isolation_of(spec) != "branch" or not enforcing or not (project / ".git").exists():
            return ""
        repository = ProjectGit(project)
        if not repository.head():
            return ""
        if uncommitted := _theirs(repository, layout):
            shown = ", ".join(uncommitted[:4]) + (" and more" if len(uncommitted) > 4 else "")
            return (
                f"Error: {project} has uncommitted work ({shown}), and '{spec.name}' runs in this "
                f"checkout on branch stint/{record.stint_id}. Its roles are held to declared paths by putting the "
                "tree back, so what you left here would be undone as if a role had written it. "
                "Commit or stash it first."
            )
        return ""

    def _open_tree(self, spec: PlaybookSpec, record: StintRecord, layout: Layout | None = None) -> str:
        """Give the stint the working tree its isolation asks for, or say why it cannot have one.

        A stint edits files and commits for hours, so something has to keep that
        apart from the person whose repository it is. How much is kept apart is
        the playbook's ``isolation`` (see :func:`isolation_of`):

        ``worktree``
            a second checkout, cut from ``HEAD`` onto a branch of the run's own.
            The person keeps their own checkout and can work while it runs; the
            price is a full copy of the repository per stint, and one left
            behind for every stint ever started.
        ``branch``
            their checkout, on a branch of the run's own. No copy, and the work
            lands where they will find it (``git log stint/<id>``); the price is
            that the tree is the run's until it ends.
        ``none``
            their checkout, their branch. Legal only where nothing is enforced,
            which validation holds -- see ``_enforced_without_a_tree_to_undo``.

        A stint that enforces boundaries and has no repository to enforce them
        against is refused rather than started: it would run to the end looking
        like it was being held to its declarations, having held nobody to
        anything. Two cases of "no repository" that are easy to miss, and are
        refused here for that same reason: a directory that is not one, and one
        with no commits -- there is no state to put a stray write back to.
        """
        enforcing = any(role.owns or role.appends for role in (spec.roles or []))
        isolation = isolation_of(spec)
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
        if enforcing and not repository.head():
            # `worktree_add` would fail here anyway and fall through to working
            # in place -- which is the outcome this refusal exists to prevent,
            # because in place with a hard boundary means the undo reaches the
            # person's own files.
            return (
                f"Error: {project} is a git repository with no commits, so there is nothing to put a "
                f"stray write back to -- and that undo is how '{spec.name}' holds its roles to what they "
                "declare. Commit something here first."
            )
        if isolation == "none":
            logger.info("stint {} works {} in place, on its own branch: isolation none", record.stint_id, project)
            record.untracked_at_start = list(repository.changed())
            return ""
        branch = f"stint/{record.stint_id}"
        if isolation == "branch":
            if problem := self._left_in_the_tree(spec, record, layout):
                return problem
            try:
                repository.checkout_branch(branch)
            except (HistoryError, OSError) as exc:
                return f"Error: {project} could not be put on branch {branch} ({exc})."
            record.branch = branch
            record.untracked_at_start = list(repository.changed())
            return ""
        tree = (
            self.store_for(str(record.origin.get("session_key") or "") or None).artifacts_for(record.stint_id) / "tree"
        )
        try:
            repository.worktree_add(tree, branch, repository.head())
        except (HistoryError, OSError) as exc:
            if enforcing:
                return (
                    f"Error: '{spec.name}' asks for a checkout of its own and {project} could not give it "
                    f"one ({exc}). Its boundaries are enforced by putting that checkout back, so running "
                    "here would hold nobody to anything."
                )
            logger.error("stint {} could not open a checkout of its own ({}); working in place", record.stint_id, exc)
            return ""
        _carry_layout(project, tree, layout)
        record.workdir = str(tree)
        record.branch = branch
        # After the carry, because what it copied in is exactly the case this
        # exists for: uncommitted in the new checkout, and nobody's.
        record.untracked_at_start = list(ProjectGit(tree).changed())
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
        # round, taken up the way `resume` takes one up: under a new attempt,
        # because the node ids of the attempt that did not finish are claimed
        # for the life of the conversation, and with the roles that did finish
        # named rather than re-run.
        attempt, satisfied = 0, {}
        if _round_done(record):
            index = record.round_index + 1
        else:
            index = record.round_index or 1
            previous = record.round(index)
            if previous is not None and previous.run_id:
                attempt = previous.attempt + 1
                satisfied = await self._completed_of(record, spec, index, previous)
        record.status = RUNNING
        record.stop_reason = ""
        record.round_index = index
        store.write(record)
        receipt = await self._submit(
            spec, record, store, index=index, confirm=False, attempt=attempt, satisfied=satisfied
        )
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
        if record.status == RUNNING and not record.abandoned():
            # Still saying it is running, still being touched, and its holder not
            # known to be dead: something is holding it, and it need not be this
            # process -- the stamp is the one signal that crosses a process
            # boundary, and the holder's pid the one that crosses a restart. Two
            # hosts advancing one stint is the failure the heartbeat exists to
            # prevent, and a person reaching for `resume` is how it would happen.
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
        """Which roles of the interrupted round already finished, by node id.

        The record says which: each role is written down as finished, under the
        node it ran as, when it is judged and committed. The registry is then
        asked only to confirm that node is readable -- a role that finished
        without leaving output is not something a later role can read, and
        naming it would hand the round a reference that resolves to nothing.
        The registry is that of the conversation the stint started in, which
        is also the one the new graph is validated against, so what is named
        here is what validation will find.

        A record from before roles were written down falls back to asking the
        registry about every role.
        """
        if previous is None or not previous.run_id:
            return {}
        try:
            nodes = await self.dag_tool.session_nodes(str(record.origin.get("session_key") or "") or None)
        except Exception as exc:  # noqa: BLE001 - a resume with no registry redoes the round
            logger.warning("stint {} could not read what its last round finished: {}", record.stint_id, exc)
            return {}
        prefix = namespace(spec.name, index, previous.attempt, record.token)
        candidates = dict(previous.finished) or {role.label: f"{prefix}-{role.label}" for role in (spec.roles or [])}
        done = {label: node_id for label, node_id in candidates.items() if nodes.is_readable(node_id)}
        if skipped := sorted(set(candidates) - set(done)):
            logger.info(
                "stint {} round {}: {} finished but left nothing readable, so it runs again",
                record.stint_id,
                index,
                ", ".join(skipped),
            )
        return done

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
            token=record.token,
            # A checkout of its own only under `worktree`. `branch` also records
            # one, and the text for a checkout tells the role that the project
            # is elsewhere -- which, in the project itself, sends it looking.
            where=where_section(record.workdir, isolation_of(spec) == "worktree"),
        )
        if not nodes:
            # Every role of this round already finished, which is what a stint
            # interrupted between its last node and its hand-over looks like.
            return "Error: every role of this round had already finished, so there was nothing left to run."
        try:
            receipt = await self.dag_tool.run_round(
                nodes,
                task_summary=f"{spec.name}: round {index} of at most {_budget(spec)}",
                confirm=confirm,
                stint=record.ref(index, rounds=_budget(spec)),
                origin=record.origin or None,
                # Built when asked rather than now, so a round that is not going to
                # be asked about does not pay for the text.
                confirm_question=lambda: approval(spec, record, layout),
            )
        except RoundBudgetSpentError as spent:
            return await self._pause_on_budget(record, store, index, spent.refusal)
        except RoundNotApprovedError:
            return _NOT_APPROVED
        text = _text_of(receipt)
        if not _run_id_of(text):
            # Nothing started: a graph the validation refused, a paused
            # dispatch. Opening a round on it started a beat for a run that
            # did not exist, and a terminal that holds while a beat is alive
            # then held on nothing. Said as an error so `start` closes the record.
            return text if text.startswith("Error") else f"Error: {text}"
        record.open_round(index, _run_id_of(text), attempt=attempt)
        record.claim()
        store.write(record)
        # After the write, so the first beat cannot race the record into being.
        self._start_beat(record, store)
        return text


_NOT_APPROVED = (
    "Error: the person did not approve the round, so nothing was run and the stint was not opened. "
    "Do not re-submit it; ask them what to change."
)


def _preflight_layout(project: Path, spec: PlaybookSpec) -> list[str]:
    """What the laid-out project still lacks before a round can run on it.

    The roster the guard files declare has to agree with itself and the
    specification has to be there; both are read by every round and neither
    was checked at the start until now -- `roster.preflight` existed and
    nothing on the rounds path called it. The backlog gates are left off: this
    playbook's Planner writes the backlog in round one, and the person's word
    is the round's approval.
    """
    from raven.stint.roster import RosterError, preflight

    names = [role.label for role in (spec.roles or [])]
    try:
        return preflight(project, names=names or None or (), require_backlog=False) if names else []
    except RosterError as error:  # pragma: no cover - preflight reports its own errors
        return [str(error)]


def _release_tasks(record: StintRecord, index: int) -> None:
    """A round is over: what it promised and did not deliver goes back to open.

    Two promises. A task in review that Verifier never judged is not done, and a task
    assigned that the Builder never took is nobody's. Both rules lived in
    `backlog.py` with no caller, so a Builder cut off by its turn budget left
    tasks `assigned` for the rest of the stint, where the next Planner read them
    as somebody's.
    """
    project = Path(record.workdir)
    if not backlog_mod.backlog_path(project).is_file():
        return
    try:
        backlog = backlog_mod.load(project)
        released = backlog_mod.sweep_unverified(backlog, index) + backlog_mod.release_unimplemented(backlog, index)
        if released:
            backlog_mod.save(project, backlog)
            logger.info(
                "stint {} round {} sent {} task(s) back to open: {}",
                record.stint_id,
                index,
                len(released),
                ", ".join(str(task.id) for task in released),
            )
    except (backlog_mod.BacklogError, OSError) as exc:
        logger.warning("stint {} could not tidy its backlog after round {}: {}", record.stint_id, index, exc)


def _nothing_ran(summary: str) -> bool:
    """Whether the round's receipt says no node of it completed.

    Read off the receipt the graph tool renders -- the same way `_run_id_of`
    reads the run id off it -- because that is what a finished round hands the
    driver. A receipt with no such line is not a round that did nothing.
    """
    match = _ROUND_OUTCOME.search(summary or "")
    return match is not None and int(match.group(1)) == 0


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


def _unanswered_checks(project: Path, spec: PlaybookSpec) -> str:
    """Refuse a run whose declared checks nobody has said how to run here.

    A check declared by description is a question about the project, and until
    it is answered the round would report a gate that nothing measured -- which
    reads, in the round's own table, exactly like a gate that passed. Said
    before the tree is opened, because the answer is one line in a file and the
    person is standing right here.
    """
    if not spec.verify:
        return ""
    _, missing = resolve_checks(project, spec.name, spec.verify)
    if not missing:
        return ""
    told = "; ".join(
        f"{entry.name} ({entry.description.strip()})" for entry in spec.verify if entry.name in set(missing)
    )
    first = missing[0]
    return (
        f"Error: '{spec.name}' declares {len(missing)} check(s) this project has never answered -- {told}. "
        f"A declared check that does not run is a gate the round reports and nobody measured. Say what they "
        f'run here: `raven playbook stint check set {spec.name} {first} --run "..."`.'
    )


def _held(record: StintRecord) -> bool:
    """Something is advancing this stint right now, as far as its file can say.

    Only a running record with a live holder is held. Paused and interrupted
    ones are waiting for exactly the person who just asked; a running one whose
    holder has gone quiet or is known dead is waiting too, it just does not know
    it yet.
    """
    return record.status == RUNNING and not record.abandoned()


def _second_plan_refused(running: StintRecord, playbook: str, project: Path) -> str:
    """Why a second stint was not started, and what to do instead.

    A refusal rather than a question, because the surfaces that cannot ask are
    the ones where the mistake is worst: a cron trigger or a reconnecting client
    that quietly opened a second stint would run two of them against one
    repository, on two branches, each redoing the other's work. The graph
    confirm gate runs when nobody can be asked; this one must not.

    Reached only for a stint something is still advancing -- one nobody is
    advancing is taken up instead (see ``start``). So the way out is not
    ``stints resume``: that opens a second engine in the caller's process, and
    a model running it through a shell tool has it killed at the tool's timeout
    with a round half-dispatched. Either wait, or end the running one.
    """
    reached = running.round_index
    same = " " if running.playbook == playbook else f" (running {running.playbook}) "
    ago = max(0, int(time.time() - running.touched_at_ms / 1000))
    return (
        f"Error: a stint is already{same}on {project}: {running.stint_id}, on round {reached}, and something "
        f"was advancing it {ago}s ago. Starting a second stint here would put two of them on one repository, "
        "each working from a base that does not have the other's work. If its host is still running, wait for "
        "it. If you know its host is gone, ask again in a few minutes: once nothing has touched it for "
        f"{int(STALE_AFTER_SEC // 60)}, asking for this playbook takes it up where it stopped. To end it "
        f"instead: `raven playbook stints stop {running.stint_id}`."
    )


def _summary(record: StintRecord) -> str:
    """What the person who started the stint is told, once, at the end."""
    done = [entry for entry in record.rounds if entry.status == "completed"]
    violations = [note for entry in record.rounds for note in entry.violations]
    # The branch is said because nothing merges it. A stint commits to a branch
    # of its own and the project it was started from is untouched, so a reader
    # told only the directory has been told the work is somewhere it is not.
    #
    # And when the branch was cut in their own checkout, that checkout is still
    # on it: a person who does not know that reads their own project as having
    # been rearranged. The way back is one command and it is cheap to say.
    in_place = bool(record.project) and Path(record.workdir) == Path(record.project)
    if record.branch and in_place:
        where = (
            f"It worked in {record.workdir}, on branch {record.branch}, which nothing has merged -- "
            "and that checkout is still on it, so `git checkout -` puts you back."
        )
    elif record.branch:
        where = f"It worked in {record.workdir}, on branch {record.branch}, which nothing has merged."
    else:
        where = f"It worked in {record.workdir}."
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


def isolation_of(spec: PlaybookSpec) -> "Isolation":
    """How much of the person's checkout this run borrows.

    One reader for the field so the default lives in one place: a playbook that
    says nothing gets :data:`DEFAULT_ISOLATION`, and every caller that has to
    branch on the answer -- opening the tree, writing the approval, taking the
    stint up again -- asks here rather than repeating ``or "branch"``.
    """
    return spec.isolation or DEFAULT_ISOLATION


def _theirs(repository: ProjectGit, layout: Layout | None) -> list[str]:
    """Uncommitted paths in the project that this run did not just write itself.

    The setup pass writes ``.stint/`` and does not commit it, so by the time the
    tree is opened a first run has made the project dirty itself. Counting that
    as the person's work would refuse every fresh project; counting nothing would
    miss the case the check exists for.

    The host's own ``.raven/`` is not weighed here because
    :meth:`ProjectGit.changed` does not report it at all -- see
    :data:`raven.stint.git.HOST_STATE`.
    """
    # A created entry may be spelled `<path> -> <target>`, which is how `init`
    # records the specification symlink; the part before the arrow is the path
    # git will report. Without this the link reads as the person's own file and
    # every laid-out project is refused.
    # Written by this run *or by the attempt before it*: a run refused after the
    # layout leaves the layout behind, and the retry finds it already there. A
    # file the recipe owns is the person's only once it is committed and then
    # edited -- so a kept file HEAD does not carry is the layout's, and a kept
    # file HEAD does carry, showing as changed, is theirs.
    laid_out = {_path_of(name) for name in (*layout.wrote, *layout.kept)} if layout is not None else set()
    # Resolving the declared checks writes the ledger, after the layout and
    # before the tree is opened, and it is not in the layout's lists -- so a
    # project whose check was detected was refused for its own ledger.
    laid_out.add(f"{STINT_DIR}/{CHECKS_FILE}")
    head = repository.head()
    # `changed()` already leaves the host's own directory out, so what is left
    # here is the project's: the layout, and the person's.
    return [path for path in repository.changed() if path not in laid_out or repository.known_at(head, path)]


def _path_of(wrote: str) -> str:
    """The path in a layout entry, which for the specification link is spelled ``<path> -> <target>``."""
    return str(wrote).split(" -> ", 1)[0]


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
        path = _path_of(name)
        source, target = project / path, tree / path
        if not source.is_file() or target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            # The specification link is relative to `.stint/`, and the checkout
            # carries the same document at the same place, so the link is what
            # carries -- a copy would freeze the text on the day the run began.
            target.symlink_to(os.readlink(source))
        else:
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
    if record.branch and isolation_of(spec) == "worktree":
        lines.append(f"  on branch {record.branch}, in a checkout of its own -- your working tree is untouched")
    elif record.branch:
        # The sentence above is the one a reader is most likely to carry away,
        # and here it would be false: this run is in the checkout they are
        # standing in. Their branch is still safe, and that is the part worth
        # saying, along with the part that costs them something.
        lines.append(
            f"  on branch {record.branch}, cut in this checkout -- the tree is the stint's until it ends, "
            "and the branch you are on now is left where it is"
        )
    else:
        lines.append("  in this checkout, on the branch you are on, committing nothing")
    if spec.verify:
        lines.append("  every round may run, on this machine:")
        # Resolved, not as written: a check declared by description carries no
        # command in the file, and the whole reason this text exists is that the
        # approval is the one moment somebody reads what will execute here.
        resolved, missing = resolve_checks(Path(record.project or record.workdir), spec.name, spec.verify)
        lines.extend(f"      {check.name} -> {check.command}" for check in resolved)
        lines.extend(f"      {name} -> nothing says what this runs here" for name in missing)
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
