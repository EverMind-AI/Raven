"""PlaybookRuntime — the per-message funnel, packaged for the agent loop.

One object bundles what the loop's interception branch needs: the library
(loaded once at construction), the L1 index over its trigger vocabularies,
the L2 gate, and the executor. ``consider`` is the only entry: it returns
``None`` for the overwhelmingly common case (no nomination, gate said no,
or anything failed), and the loop then runs the normal turn untouched.

Library reloads are by rebuilding the runtime (restart or a future
hot-reload hook); nothing here watches the directory.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.playbook.executor import ExecutionPlan, PlaybookExecutor
from raven.playbook.matcher import MatchCandidate, TriggerIndex, gate
from raven.playbook.store import PlaybookStore
from raven.playbook.triggers import find_collisions
from raven.playbook.types import PlaybookSpec

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider

#: (prompt, choices, conversation_id) -> the user's answer. ``None`` means the
#: round-trip is structurally unavailable in this environment; ``""`` means
#: the user was asked and did not answer (timeout / cancel).
AskFn = Callable[[str, "list[str] | None", str], Awaitable["str | None"]]

_RUN_CHOICE = "Run it"
_SKIP_CHOICE = "Not now"
_NONE_OF_THESE = "None of these"

# What counts as consent besides clicking the run choice: users type. CJK
# entries are unicode escapes to keep the source ASCII (hao/shi/keyi/zhixing/
# queren/pao -- ok/yes/can-do/execute/confirm/run).
_AFFIRMATIVE = {
    _RUN_CHOICE.lower(),
    "run",
    "yes",
    "y",
    "ok",
    "go",
    "sure",
    "\u597d",
    "\u662f",
    "\u53ef\u4ee5",
    "\u6267\u884c",
    "\u786e\u8ba4",
    "\u8dd1",
}


class PlaybookRuntime:
    """Match-and-execute funnel over one loaded playbook library."""

    def __init__(
        self,
        *,
        provider: "LLMProvider",
        store: PlaybookStore,
        executor: PlaybookExecutor,
        model: str | None = None,
        disabled: Iterable[str] = (),
        ask: AskFn | None = None,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._model = model
        #: The whole-run confirm channel (spec-level ``confirm``): asked before
        #: a passive dispatch, never on the explicit entries -- a run_playbook
        #: tool call or a CLI run is itself the consent. ``None`` (no channel
        #: wired, e.g. tests) keeps the pre-confirm behaviour of dispatching.
        self._ask = ask
        #: conversation -> names the user refused in that conversation's
        #: current turn (a confirm answered "Not now", or contenders answered
        #: "None of these"). run_named refuses these: the fall-through turn is
        #: exactly where the tool is live, and its instructions read like this
        #: situation -- without the memory, the model re-runs what the user
        #: just declined. Cleared by the conversation's next user message
        #: (its consider() call), so a change of mind works immediately.
        self._declined: dict[str, set[str]] = {}
        #: This turn's reply address, for the dispatch's progress and announce.
        #: ``consider`` receives it per call; a tool call arrives without it, so
        #: the last one seen is kept for :meth:`run_named` to reuse.
        self._context: dict[str, str | None] = {"channel": None, "chat_id": None, "session_key": None}
        self._specs: dict[str, PlaybookSpec] = {}
        #: Loaded but not matchable. The deny list is config
        #: (``playbooks.disabled``), not file content: absent from the list
        #: means matchable, so a hand-written directory participates the
        #: moment it exists. A disabled playbook stays loaded because
        #: disabling only mutes the passive funnel -- an explicit run (the
        #: run_playbook tool, ``raven playbook run``) still resolves it.
        self._disabled: set[str] = set()
        deny = set(disabled)
        for pid in store.list_ids():
            try:
                spec = store.load(pid)
            except Exception as exc:  # noqa: BLE001 - one bad file must not sink the library
                logger.warning("Skipping unloadable playbook {!r}: {}", pid, exc)
                continue
            if not spec.triggers.keywords:
                logger.info("Playbook {!r} has no trigger vocabulary; it will never match passively", pid)
                continue
            self._specs[pid] = spec
            if pid in deny:
                logger.info("Playbook {!r} is disabled in config; not matchable", pid)
                self._disabled.add(pid)
        matchable = {pid: s.triggers for pid, s in self._specs.items() if pid not in self._disabled}
        self._index = TriggerIndex(matchable)
        collisions = find_collisions(matchable)
        if collisions:
            logger.warning("Playbook trigger collisions (adjudicated by the gate at runtime): {}", collisions)
        logger.info(
            "Playbook runtime loaded {} matchable playbook(s), {} disabled",
            len(matchable),
            len(self._disabled),
        )

    @property
    def empty(self) -> bool:
        return not self._specs

    def set_context(self, *, channel: str | None, chat_id: str | None, session_key: str | None) -> None:
        """Record this turn's reply address and pass it to the executor."""
        self._context = {"channel": channel, "chat_id": chat_id, "session_key": session_key}
        self._executor.set_context(channel=channel, chat_id=chat_id, session_key=session_key)

    def reset_declines(self, conversation_id: str | None) -> None:
        """Forget the conversation's refusals: a new user message arrived.

        ``consider`` does this for ordinary turns. A mid-turn injected
        message never gets a ``consider`` call -- the loop merges it straight
        into the running turn -- so the loop calls this at the merge point;
        otherwise an explicit re-request inside the fall-through turn is
        still refused.
        """
        if conversation_id:
            self._declined.pop(conversation_id, None)

    def listing(self) -> list[tuple[str, str]]:
        """``(id, description)`` for every loaded playbook, id order.

        What the agent-facing tool advertises. The same library the passive
        funnel matches against, so the two entries cannot disagree about which
        playbooks exist. Disabled entries appear with a marker: they are out
        of the passive funnel but remain explicitly runnable, and the tool
        needs their names to offer that.
        """
        return [
            (pid, self._specs[pid].description + (" [disabled]" if pid in self._disabled else ""))
            for pid in sorted(self._specs)
        ]

    async def run_named(self, name: str, params: dict[str, Any]) -> ExecutionPlan | None:
        """Run a playbook the caller already identified; ``None`` if unknown.

        The named entry for :class:`~raven.agent.tools.run_playbook.RunPlaybookTool`.
        It skips L1 and the gate -- the caller has the conversation and has
        already decided -- but joins the passive path at the executor, so
        parameter filling, composition, validation and dispatch stay in one
        place. Unlike ``consider``, an executor failure propagates: a tool call
        has a caller who can be told, where a passive miss must degrade to a
        normal turn.
        """
        spec = self._specs.get(name)
        if spec is None:
            return None
        cid = self._context.get("session_key") or ""
        if cid and name in self._declined.get(cid, ()):
            # Consent-by-call does not hold right after a refusal of the same
            # playbook: on the fall-through turn the call is the model's
            # decision, made against instructions that cannot distinguish a
            # trigger miss from the user's "Not now".
            return ExecutionPlan(
                kind="questions",
                reply=(
                    f"Not dispatched: the user was just asked about running {name!r} and declined. "
                    "Acknowledge their decision instead; run it only if they explicitly ask again."
                ),
            )
        self._executor.set_context(**self._context)
        return await self._executor.execute(spec, params)

    async def consider(
        self,
        message: str,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
        session_key: str | None = None,
    ) -> ExecutionPlan | None:
        """One message through the funnel; ``None`` means pass through."""
        # Recorded before any early return, not on the match path: run_named
        # (the tool entry) reuses the last address seen, and the turns where
        # the agent reaches for the tool are exactly the turns the funnel did
        # not claim -- recording only on a match would hand a tool-started
        # run's announce a stale conversation, or the cold-start default.
        self.set_context(channel=channel, chat_id=chat_id, session_key=session_key)
        # A new user message resets the conversation's refusals: declining is
        # an answer about this turn, not a standing ban, and the reset is what
        # lets "actually, run it" on the next message just work.
        self.reset_declines(session_key or (f"{channel}:{chat_id}" if channel and chat_id else ""))
        if not self._specs:
            return None
        hits = self._index.match(message)
        if not hits:
            return None
        candidates = [
            MatchCandidate(
                playbook_id=pid,
                description=self._specs[pid].description,
                params=self._specs[pid].params,
            )
            for pid in hits
        ]
        verdict = await gate(self._provider, message, candidates, model=self._model)
        cid = session_key or (f"{channel}:{chat_id}" if channel and chat_id else "")
        if not verdict.actionable or verdict.match is None:
            return await self._offer_contenders(verdict, cid)
        spec = self._specs[verdict.match]
        logger.info("Playbook {} matched (reason: {})", spec.name, verdict.reason)
        if spec.confirm and not await self._confirmed(spec, verdict.params, cid):
            return None
        try:
            return await self._executor.execute(spec, verdict.params)
        except Exception:  # noqa: BLE001 - an executor bug must degrade to a normal turn
            logger.opt(exception=True).warning("Playbook execution failed; falling back to the normal turn")
            return None

    async def _confirmed(self, spec: PlaybookSpec, params: dict[str, Any], cid: str) -> bool:
        """The spec-level confirm gate, over the host's ask channel.

        No channel wired (tests, environments without a question broker)
        keeps the pre-confirm behaviour of dispatching -- the L2 gate is then
        the only protection, as before. When the user is actually asked,
        anything but consent (including a timeout's empty answer) skips the
        run, and the message falls through to the normal turn.
        """
        if self._ask is None or not cid:
            return True
        shown = ", ".join(f"{k}={v}" for k, v in params.items()) or "none extracted"
        answer = await self._ask(
            f"Run the stored playbook '{spec.name}'? ({spec.description}) Params: {shown}.",
            [_RUN_CHOICE, _SKIP_CHOICE],
            cid,
        )
        if answer is None:
            logger.info("Playbook {}: no ask channel at call time; proceeding without confirm", spec.name)
            return True
        if answer.strip().lower() in _AFFIRMATIVE:
            return True
        logger.info("Playbook {}: user declined the confirm ({!r}); falling through", spec.name, answer)
        self._declined.setdefault(cid, set()).add(spec.name)
        return False

    async def _offer_contenders(self, verdict: Any, cid: str) -> ExecutionPlan | None:
        """A too-close-to-call gate becomes the user's choice, not a silent drop.

        Only with an ask channel and at least two known, matchable contenders;
        picking from the list is the consent, so the pick dispatches without a
        second confirm. Params were extracted for no candidate in particular,
        so the pick runs with none and the missing-params reply guides the
        follow-up.
        """
        if self._ask is None or not cid:
            return None
        picks = [c for c in verdict.contenders if c in self._specs and c not in self._disabled]
        if len(picks) < 2:
            return None
        answer = await self._ask(
            "That request fits more than one stored playbook -- run one of these?",
            [*picks, _NONE_OF_THESE],
            cid,
        )
        if answer is None or answer.strip() not in picks:
            if answer is not None:
                # "None of these" (or a timeout) refuses the whole offer; the
                # fall-through turn must not quietly run one of them anyway.
                self._declined.setdefault(cid, set()).update(picks)
            return None
        spec = self._specs[answer.strip()]
        logger.info("Playbook {} chosen by the user among contenders {}", spec.name, picks)
        try:
            return await self._executor.execute(spec, {})
        except Exception:  # noqa: BLE001 - an executor bug must degrade to a normal turn
            logger.opt(exception=True).warning("Playbook execution failed; falling back to the normal turn")
            return None
