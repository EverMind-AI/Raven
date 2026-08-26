"""PlaybookRuntime — the library, and the one entry the model loads a playbook by.

One object bundles the library (loaded once at construction), the retrieval index
over its trigger vocabularies, and the executor. :meth:`load` is the only
execution entry; :meth:`listing` and :meth:`names` are what the tool advertises.

**This used to be a funnel.** It scanned every user message, spent an LLM gate
call on any message that mentioned a trigger word, and on a hit took over the
whole turn -- the main agent never ran. That is gone (see
:mod:`raven.playbook.matcher` for why), and with it three mechanisms that only
existed to patch it: the per-conversation memory of refusals, the "which of these
two did you mean" user prompt, and the gate itself. What remains is a library the
model chooses from.

Library reloads are by rebuilding the runtime (restart or a future hot-reload
hook); nothing here watches the directory.
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
        self._index: TriggerIndex | None = None
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
                # the kind of gap nobody thinks to ask about.
                logger.warning("Playbook {!r} is not usable and is not being offered: {}", name, "; ".join(errors))
                self._specs.pop(name, None)
                continue
            self._specs[name] = spec
            if first_sight:
                # Every arrival that did not come through ``adopt`` -- a
                # hand-written directory, a pull, an edit by hand. The gate this
                # library has is that what lands in it is visible and checked,
                # not that writing to it is hard: ``write_file`` is a general
                # capability and the directory is an ordinary directory.
                logger.info("Playbook {!r} appeared in the library at {}", name, self._store.path_for(name))
        self._fingerprints = current
        if changed or self._index is None:
            self._reindex()

    def _known_agents(self) -> list[str] | None:
        """The agent table the graph will be dispatched against, or None.

        Read off the executor rather than held here: it is the same table the
        dispatch resolves node names against, and a second copy would let this
        refuse a playbook the graph would have run. ``None`` when there is no
        table, which is ``validate_structure``'s own way of saying "do not check
        the names at all" rather than checking them against a guess.
        """
        roster = getattr(self._executor, "_roster_cache", None)
        return sorted(roster) if roster else None

    def _reindex(self) -> None:
        """Rebuild the trigger index over what is currently offered.

        Called after the library changes rather than on every read: the index
        normalizes the whole vocabulary once, which is the work the per-turn
        ranking exists not to redo. The deny list is *not* baked into it -- a
        name switched off between two calls has to disappear without anything
        being rebuilt, so :meth:`listing` filters at the point of use.
        """
        self._index = TriggerIndex({pid: s.triggers for pid, s in self._specs.items()})

    def adopt(self, name: str) -> bool:
        """Take a playbook that was just written into the live library.

        The library was read once at construction, so a playbook created mid
        conversation was invisible until the process restarted -- including to
        the tool whose whole job is to load it. Loading the one file that
        changed rather than rescanning: the caller knows the name, and a rescan
        would parse every other file to learn nothing.

        Returns False when the file cannot be read, which is reported by the
        caller rather than raised: a playbook that was written but cannot be
        parsed back is worth saying out loud, and is not a reason to fail the
        turn that wrote it.
        """
        try:
            self._specs[name] = self._store.load(name)
        except Exception as exc:  # noqa: BLE001 - the caller reports it
            logger.warning("Playbook {!r} was written but could not be loaded back: {}", name, exc)
            return False
        # Recorded here too, so the next refresh sees a name it already holds at
        # the digest it already read and does not report it as an arrival.
        self._fingerprints = self._store.fingerprints()
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

    def listing(self, message: str = "") -> list[tuple[str, str]]:
        """``(name, detail)`` for the playbooks worth describing in full this turn.

        The expensive half of advertising a library, so it is the half that gets
        narrowed (:mod:`raven.playbook.router`). ``detail`` carries what the model
        needs to call one correctly and cannot guess: the description, the
        parameter table, and which node fields were left blank for it to fill.
        Without the parameter table it can only guess key names, and a guessed key
        is dropped silently and comes back as the same question.
        """
        self._refresh()
        deny = self.disabled()
        chosen = select_playbooks(
            {pid: spec for pid, spec in self._specs.items() if pid not in deny},
            message,
            # The index this object already built at load: it normalized the whole
            # vocabulary once, which is the work the ranking would otherwise redo
            # per keyword per playbook per turn.
            index=self._index,
            sizes=self._router,
        )
        return [(pid, self._detail(self._specs[pid])) for pid in chosen]

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
        and its confirm gate passes), a ``prompt`` one comes back as composition
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
