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

from typing import TYPE_CHECKING

from loguru import logger

from raven.memory_engine.playbook.executor import ExecutionPlan, PlaybookExecutor
from raven.memory_engine.playbook.matcher import MatchCandidate, TriggerIndex, gate
from raven.memory_engine.playbook.store import PlaybookStore
from raven.memory_engine.playbook.triggers import find_collisions
from raven.memory_engine.playbook.types import PlaybookSpec

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider


class PlaybookRuntime:
    """Match-and-execute funnel over one loaded playbook library."""

    def __init__(
        self,
        *,
        provider: "LLMProvider",
        store: PlaybookStore,
        executor: PlaybookExecutor,
        model: str | None = None,
        include_draft: bool = False,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._model = model
        self._specs: dict[str, PlaybookSpec] = {}
        for pid in store.list_ids():
            try:
                spec = store.load(pid)
            except Exception as exc:  # noqa: BLE001 - one bad file must not sink the library
                logger.warning("Skipping unloadable playbook {!r}: {}", pid, exc)
                continue
            if spec.status != "ready" and not (include_draft and spec.status == "draft"):
                continue
            if not spec.triggers.keywords:
                logger.info("Playbook {!r} has no trigger vocabulary; it will never match passively", pid)
                continue
            self._specs[pid] = spec
        self._index = TriggerIndex({pid: s.triggers for pid, s in self._specs.items()})
        collisions = find_collisions({pid: s.triggers for pid, s in self._specs.items()})
        if collisions:
            logger.warning("Playbook trigger collisions (adjudicated by the gate at runtime): {}", collisions)
        logger.info("Playbook runtime loaded {} matchable playbook(s)", len(self._specs))

    @property
    def empty(self) -> bool:
        return not self._specs

    async def consider(
        self,
        message: str,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
        session_key: str | None = None,
    ) -> ExecutionPlan | None:
        """One message through the funnel; ``None`` means pass through."""
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
        if not verdict.actionable or verdict.match is None:
            return None
        spec = self._specs[verdict.match]
        logger.info("Playbook {} matched (reason: {})", spec.name, verdict.reason)
        self._executor.set_context(channel=channel, chat_id=chat_id, session_key=session_key)
        try:
            return await self._executor.execute(spec, verdict.params)
        except Exception:  # noqa: BLE001 - an executor bug must degrade to a normal turn
            logger.opt(exception=True).warning("Playbook execution failed; falling back to the normal turn")
            return None
