"""PlaybookRuntime — the library, and the one entry the model loads a playbook by.

One object bundles the live on-disk library, its retrieval index and the executor.
:meth:`load` is the execution entry; :meth:`listing` and :meth:`names` are what
the tool advertises.

Nothing here calls a model: the library is what the model chooses from, and
:mod:`raven.playbook.matcher` says why the choice is the model's.

Library reads reconcile content fingerprints with the directory, parsing only
files whose bytes changed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from loguru import logger

from raven.playbook.executor import ExecutionPlan, PlaybookExecutor
from raven.playbook.matcher import TriggerIndex
from raven.playbook.router import RouterSizes, select_playbooks
from raven.playbook.store import PlaybookStore
from raven.playbook.triggers import find_collisions
from raven.playbook.types import PlaybookSpec
from raven.playbook.validate import validate_structure

#: How many times one conversation may be told "still missing X" for the same
#: playbook before it is told to stop. Without a bound, "cannot fill it -> ask
#: again -> still cannot" is a loop the model can spend a whole turn in, and each
#: pass costs a tool call for no progress. Two is enough for the case this exists
#: for: the first reply names the gaps, the second confirms they are filled.
MAX_GAP_ROUNDS = 2


class PlaybookRuntime:
    """One loaded playbook library, plus the loader the model reaches it through."""

    def __init__(
        self,
        *,
        store: PlaybookStore,
        executor: PlaybookExecutor,
        disabled: Iterable[str] = (),
        disabled_source: "Callable[[], frozenset[str]] | None" = None,
        known_agents: "Callable[[], Iterable[str]] | None" = None,
        router: RouterSizes | None = None,
    ) -> None:
        # No provider and no model here any more. Both existed for the gate --
        # one LLM call per nominated message -- and nothing in this object calls a
        # model now. The one place that still needs one is composing a
        # prompt-mode graph on the CLI, which is the executor's own dependency.
        self._executor = executor
        self._router = router or RouterSizes()
        #: This turn's reply address, for the dispatch's progress and announce.
        #: Set per turn by the loop; a tool call arrives without one, so the last
        #: one seen is what :meth:`load` reuses.
        self._context: dict[str, str | None] = {"channel": None, "chat_id": None, "session_key": None}
        #: (conversation, playbook) -> how many times we have reported gaps.
        #: Reset when that pair finally dispatches, so a second, genuinely new
        #: run of the same playbook starts with a fresh budget.
        self._gap_rounds: dict[tuple[str, str], int] = {}
        self._store = store
        self._specs: dict[str, PlaybookSpec] = {}
        #: Parsed, and refused by the structural check. Kept rather than dropped
        #: because the reason is usually not in the file: an agent switched off,
        #: or one not added yet. Fixing that changes the agent table and not the
        #: playbook, so a refusal that was forgotten would need the file touched
        #: to be reconsidered -- a restart-shaped failure in the shape that is
        #: worse than a restart, since the corrective action succeeds and changes
        #: nothing. Re-checked on every refresh, which costs no read and no parse.
        self._refused: dict[str, PlaybookSpec] = {}
        self._index: TriggerIndex | None = None
        #: The agent table to validate against, asked rather than copied. It has
        #: to be the live one: ``apply_agents`` rebuilds the registry in place,
        #: which is what the dispatch resolves against, so any copy taken at
        #: construction can refuse a playbook the graph would have run.
        self._known_agents_source = known_agents
        #: Where the deny list is read from, asked on every access rather than
        #: captured here. A switch that was captured needed a restart to take
        #: effect, which is not something a user can be expected to know about a
        #: preference they just changed. ``disabled`` is the fallback for a caller
        #: that has a fixed list and no file to read (the CLI's explicit run,
        #: tests) and is used only when no source is wired -- deliberately *not*
        #: unioned with it. Unioning a snapshot of the same key is what makes the
        #: switch one-way: every name disabled at start stays disabled whatever
        #: the file later says, so ``disable`` applies live and ``enable`` waits
        #: for a restart. ``_withheld_tool_names`` in the agent loop carries the
        #: same warning for the same reason.
        #:
        #: The list is config (``playbooks.disabled``), not file content, so a
        #: hand-written directory participates the moment it exists. A disabled
        #: playbook stays *loaded* because ``raven playbook run`` still resolves
        #: it -- that is the user's own hand, and nothing about disabling should
        #: stop them naming one outright. What it does mean is that the model
        #: never sees it, which is the whole of what disabling can enforce now
        #: that no passive matcher remains to mute.
        self._disabled_source = disabled_source
        self._fixed_disabled = frozenset(disabled)
        #: The library as it was last read, by content. What makes a directory
        #: written by anything other than the creating tool -- a hand-written
        #: one, an edit, a ``git pull`` -- visible without a restart, and what
        #: keeps the cost of asking down to reading rather than parsing.
        self._fingerprints: dict[str, bytes] = {}
        #: Monotonic library generation. Model-facing tools use it to discard a
        #: schema snapshot when adoption or reconciliation changes what can be
        #: loaded during the same user turn.
        self._revision = 0
        self._refresh()
        deny = self.disabled()
        offered = {pid: spec.triggers for pid, spec in self._specs.items() if pid not in deny}
        if collisions := find_collisions(offered):
            # Shared vocabulary is no longer ambiguity to adjudicate -- nothing
            # dispatches off a keyword. Both playbooks simply become visible
            # together and the model picks, which is the outcome the gate's
            # contender prompt was trying to reach.
            logger.debug("Playbooks sharing trigger vocabulary (both will be offered together): {}", collisions)
        logger.info(
            "Playbook runtime loaded {} playbook(s), {} disabled",
            len(self._specs),
            len(self._specs.keys() & deny),
        )

    def disabled(self) -> frozenset[str]:
        """The names not on offer right now, read rather than remembered.

        The source wins outright where there is one. A read that fails falls back
        to the fixed list, which is the conservative direction: a torn config file
        should not silently offer the model something the user switched off.
        """
        if self._disabled_source is None:
            return self._fixed_disabled
        try:
            return self._disabled_source()
        except Exception:  # noqa: BLE001 - a bad read must not cost the turn its library
            logger.warning("playbooks: could not read the disabled list; keeping the list this loop started with")
            return self._fixed_disabled

    def _refresh(self) -> None:
        """Bring the loaded library level with the directory.

        Asked before every read rather than on a timer, and it pays for what
        changed rather than for the library: the digests come from reading the
        files (about 1.4ms across fifty of them) and only a name that is new or
        whose bytes moved is parsed (about 420us each). A library nobody touched
        costs one read and a dict comparison.

        This is what makes the library the directory's answer rather than the
        creating tool's. :meth:`adopt` stays because it does not wait for the
        next read -- the tool that just wrote a file can have it usable in the
        same breath -- but a playbook written by anything else arrives here.

        A spec that does not survive :func:`validate_structure` is kept out and
        said out loud. That check never ran on this path before: ``store.load``
        does the schema and nothing else, so a graph naming an agent that is not
        on the table used to load fine and fail when someone ran it. It is asked
        here rather than in the store because the agent table lives on this side.
        """
        current = self._store.fingerprints()
        for gone in set(self._fingerprints) - set(current):
            self._specs.pop(gone, None)
            self._refused.pop(gone, None)
            logger.info("Playbook {!r} is no longer in the library", gone)
        changed = [name for name, digest in current.items() if self._fingerprints.get(name) != digest]
        known_agents = self._known_agents()
        for name in changed:
            first_sight = name not in self._fingerprints
            try:
                spec = self._store.load(name)
            except Exception as exc:  # noqa: BLE001 - one bad file must not sink the library
                logger.warning("Skipping unloadable playbook {!r}: {}", name, exc)
                self._specs.pop(name, None)
                self._refused.pop(name, None)
                continue
            # Soundness, not completeness: a field the author left for the
            # caller to fill is what ``load_playbook`` asks for by name, and
            # refusing it here would refuse the hand-written shape this refresh
            # exists to make visible.
            if errors := validate_structure(spec, known_agents=known_agents, allow_blank_fillable=True):
                # Refused rather than offered: a graph naming an agent that is
                # not on the table cannot run, and offering it spends a turn to
                # find that out. Said at warning level because a file the user
                # can see in their library and the model cannot use is exactly
                # the kind of gap nobody thinks to ask about. Kept, so that
                # fixing the agent table is enough -- see ``_refused``.
                logger.warning("Playbook {!r} is not usable and is not being offered: {}", name, "; ".join(errors))
                self._specs.pop(name, None)
                self._refused[name] = spec
                continue
            self._refused.pop(name, None)
            self._specs[name] = spec
            if first_sight:
                # Every arrival that did not come through ``adopt`` -- a
                # hand-written directory, a pull, an edit by hand. The gate this
                # library has is that what lands in it is visible and checked,
                # not that writing to it is hard: ``write_file`` is a general
                # capability and the directory is an ordinary directory.
                logger.info("Playbook {!r} appeared in the library at {}", name, self._store.path_for(name))
        # Every refusal reconsidered against the table as it stands now. No file
        # is read and nothing is parsed: these specs are already in hand, and the
        # thing that changed is usually the agent table rather than the file.
        promoted = []
        for name, spec in list(self._refused.items()):
            if validate_structure(spec, known_agents=known_agents, allow_blank_fillable=True):
                continue
            self._specs[name] = spec
            del self._refused[name]
            promoted.append(name)
            logger.info("Playbook {!r} is usable now and is being offered", name)
        self._fingerprints = current
        if changed or promoted or self._index is None:
            self._reindex()

    def _known_agents(self) -> list[str] | None:
        """Every name a node reference can resolve to, or None if unknown.

        ``AgentRegistry.all_names`` and deliberately not the enabled subset or
        the model-facing enum: its own docstring is the rule this follows --
        being on the table is what makes a reference resolvable, whether this
        machine has that agent switched on is a runtime condition with its own
        error, and the legacy aliases resolve too. ``raven playbook validate``
        already asks that view, and two surfaces disagreeing about one file is
        worse than either answer alone.

        Asked through a callable for the reason the deny list is: the table is
        rebuilt in place by ``apply_agents`` without a restart, so anything held
        here would be the stale copy. ``None`` when no source was wired, which is
        ``validate_structure``'s own way of saying "do not check the names at
        all" rather than checking them against a guess.
        """
        if self._known_agents_source is None:
            return None
        try:
            names = sorted(self._known_agents_source())
        except Exception:  # noqa: BLE001 - a bad read must not cost the library its files
            logger.warning("playbooks: could not read the agent table; not checking agent names")
            return None
        return names or None

    def _reindex(self) -> None:
        """Rebuild the trigger index over what is currently offered.

        Called after the library changes rather than on every read: the index
        normalizes the whole vocabulary once, which is the work the per-turn
        ranking exists not to redo. The deny list is *not* baked into it -- a
        name switched off between two calls has to disappear without anything
        being rebuilt, so :meth:`listing` filters at the point of use.
        """
        self._index = TriggerIndex({pid: s.triggers for pid, s in self._specs.items()})
        self._revision += 1

    @property
    def revision(self) -> int:
        """Generation of the loaded library, for invalidating derived views."""
        return self._revision

    def adopt(self, name: str) -> bool:
        """Take a playbook that was just written into the live library.

        Creation already knows which file changed, so it need not wait for the
        next directory reconciliation. Loading that one file also avoids hashing
        the rest of the library before the creating tool reports success.

        Returns False when the file cannot be read, which is reported by the
        caller rather than raised: a playbook that was written but cannot be
        parsed back is worth saying out loud, and is not a reason to fail the
        turn that wrote it.
        """
        digest = self._store.fingerprint(name)
        try:
            spec = self._store.load(name)
        except Exception as exc:  # noqa: BLE001 - the caller reports it
            logger.warning("Playbook {!r} was written but could not be loaded back: {}", name, exc)
            self._specs.pop(name, None)
            self._refused.pop(name, None)
            if digest is None:
                self._fingerprints.pop(name, None)
            else:
                self._fingerprints[name] = digest
            self._reindex()
            return False
        self._specs[name] = spec
        self._refused.pop(name, None)
        if digest is None:
            self._fingerprints.pop(name, None)
        else:
            self._fingerprints[name] = digest
        self._reindex()
        logger.info("Playbook {!r} adopted into the live library", name)
        return True

    @property
    def dag_tool(self) -> Any:
        """The executor's private graph tool, for hosts wiring live-run concerns."""
        return self._executor.dag_tool

    @property
    def empty(self) -> bool:
        """Whether there is anything to offer. Disabled entries do not count:
        the tool exists to be called, and one that can only answer "that is
        turned off" is a tool the model should not have been given."""
        self._refresh()
        return not (set(self._specs) - self.disabled())

    def set_context(self, *, channel: str | None, chat_id: str | None, session_key: str | None) -> None:
        """Record this turn's reply address and pass it to the executor."""
        self._context = {"channel": channel, "chat_id": chat_id, "session_key": session_key}
        self._executor.set_context(channel=channel, chat_id=chat_id, session_key=session_key)

    def names(self) -> list[str]:
        """Every offered playbook name, id order -- the tool's ``enum``.

        The whole library, deliberately not the narrowed selection: a name costs a
        handful of tokens, and constraining the enum to what retrieval surfaced
        would turn a recall miss into "the model cannot reach it at all", even
        when the user has just named the playbook out loud.
        """
        self._refresh()
        return sorted(set(self._specs) - self.disabled())

    def library_view(self, message: str = "") -> tuple[list[tuple[str, str]], list[str]]:
        """One reconciled listing and full name set from the same directory view."""
        self._refresh()
        deny = self.disabled()
        offered = {pid: spec for pid, spec in self._specs.items() if pid not in deny}
        chosen = select_playbooks(
            offered,
            message,
            # The index this object already built at load: it normalized the whole
            # vocabulary once, which the ranking would otherwise redo per keyword
            # per playbook per turn.
            index=self._index,
            sizes=self._router,
        )
        listing = [(pid, self._detail(offered[pid])) for pid in chosen]
        return listing, sorted(offered)

    def listing(self, message: str = "") -> list[tuple[str, str]]:
        """``(name, detail)`` for the playbooks worth describing in full this turn.

        The expensive half of advertising a library, so it is the half that gets
        narrowed (:mod:`raven.playbook.router`). ``detail`` carries what the model
        needs to call one correctly and cannot guess: the description, the
        parameter table, and which node fields were left blank for it to fill.
        Without the parameter table it can only guess key names, and a guessed key
        is dropped silently and comes back as the same question.
        """
        return self.library_view(message)[0]

    def _detail(self, spec: PlaybookSpec) -> str:
        """One playbook as the tool description renders it."""
        parts = [spec.description]
        if spec.params:
            rows = []
            for name, p in spec.params.items():
                bits = [p.type]
                if p.required and p.default is None:
                    bits.append("required")
                elif p.default is not None:
                    bits.append(f"default={p.default!r}")
                if p.enum:
                    bits.append(f"one of {p.enum}")
                rows.append(f"{name} ({', '.join(bits)}): {p.description}")
            parts.append("params: " + "; ".join(rows))
        if gaps := _blank_fields(spec):
            parts.append("left for you to fill: " + "; ".join(f"{nid}.{field}" for nid, field in gaps))
        return " | ".join(parts)

    async def load(
        self,
        name: str,
        params: dict[str, Any] | None = None,
        fills: dict[str, dict[str, Any]] | None = None,
        *,
        allow_disabled: bool = False,
    ) -> ExecutionPlan | None:
        """Load one playbook and act on it; ``None`` if the name is unknown.

        The single entry, for the model's tool and for ``raven playbook run``
        alike. What "act on it" means is the playbook's own business rather than
        the caller's -- a ``dag`` playbook dispatches (after any gaps are filled
        and the graph-level confirm passes: ``PlaybookSpec.confirm``, dispatched
        as ``SubAgentDagSpec.confirm``), a ``prompt`` one comes back as composition
        guidance for the caller to build a graph from. The caller does not choose,
        and is not told to: ``mode`` is how the author wrote the file, not a
        decision anyone downstream should be making.

        ``allow_disabled`` is for the CLI, where the user named the playbook
        themselves.
        """
        self._refresh()
        spec = self._specs.get(name)
        if spec is None or (name in self.disabled() and not allow_disabled):
            return None
        cid = self._context.get("session_key") or ""
        key = (cid, name)
        plan = await self._executor.execute(spec, params or {}, fills=fills or {})
        if plan.kind == "gaps":
            rounds = self._gap_rounds.get(key, 0) + 1
            self._gap_rounds[key] = rounds
            if rounds > MAX_GAP_ROUNDS:
                self._gap_rounds.pop(key, None)
                logger.info("Playbook {}: gap loop hit its limit after {} rounds", name, MAX_GAP_ROUNDS)
                return ExecutionPlan(
                    kind="questions",
                    reply=(
                        f"Still missing values for '{name}' after {MAX_GAP_ROUNDS} attempts, so it was not run. "
                        "Ask the user for what is missing, or do the work another way -- do not call this again "
                        "with the same arguments."
                    ),
                )
            return plan
        self._gap_rounds.pop(key, None)
        return plan


def _blank_fields(spec: PlaybookSpec) -> list[tuple[str, str]]:
    """``(node_id, field)`` for every node field the author left for the model.

    Only the three a node cannot run without. An absent ``skills`` is *not* a gap:
    it means "this agent's own menu", which is a complete answer -- treating it as
    something to fill would put a question in front of every well-formed playbook
    in the library.
    """
    from raven.playbook.executor import FILLABLE_REQUIRED

    return [
        (node.id, field)
        for node in spec.nodes or []
        for field in FILLABLE_REQUIRED
        if not str(getattr(node, field, "") or "").strip()
    ]


__all__ = ["MAX_GAP_ROUNDS", "PlaybookRuntime"]
