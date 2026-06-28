"""Write Eval Engine verdicts back into MemoryEngine.

Writes a one-line HISTORY.md entry per judged turn through the
MemoryEngine facade. When ``MemoryEngine`` gains a dedicated
``write_observations`` method, the adapter will switch to it; for now
``append_history`` keeps the audit trail visible
to Sentinel ContextAssembler without inventing new file types.

The adapter is intentionally a thin shim — it doesn't perform any I/O
of its own beyond the MemoryEngine call. All semantic decisions
(verdict mapping, formatting) live here so the hook stays focused on
when-to-fire logic.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from raven.eval_engine.judge.judge import JudgeVerdict
from raven.memory_engine.consolidate.consolidator import MemoryStore

logger = logging.getLogger(__name__)


class EvalAdapter:
    """Writes judge verdicts to the long-term ``HISTORY.md`` via :class:`MemoryStore`.

    Phase B-3: re-targeted from the deleted ``MemoryEngine`` facade
    to ``MemoryStore`` directly. The only method this adapter needed
    was ``append_history``, which is on ``MemoryStore`` anyway —
    removing the facade indirection just makes the dependency honest.
    """

    def __init__(
        self,
        memory: MemoryStore,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory = memory
        self._now_fn = now_fn or datetime.now

    def record_task_completion(
        self,
        verdict: JudgeVerdict,
        user_goal: str,
        session_key: str,
    ) -> None:
        """Append a one-line ``[YYYY-MM-DD HH:MM] eval verdict=...`` entry
        to HISTORY.md.

        ``unknown`` verdicts are intentionally NOT recorded — they're
        signal noise. ``completed`` and ``failed`` go through so the
        Sentinel ContextAssembler's history tail sees recent outcomes.
        """
        if verdict is JudgeVerdict.unknown:
            return
        timestamp = self._now_fn().strftime("%Y-%m-%d %H:%M")
        # Keep the goal short so the HISTORY.md tail stays grep-friendly.
        truncated_goal = (user_goal or "").strip().splitlines()[0][:160]
        entry = (
            f"[{timestamp}] eval verdict={verdict.value} "
            f"session={session_key} goal=\"{truncated_goal}\""
        )
        try:
            self._memory.append_history(entry)
        except Exception as exc:  # noqa: BLE001 — adapter must not crash AgentLoop
            logger.debug(
                "EvalAdapter.append_history failed (%s): %s",
                type(exc).__name__, exc,
            )


__all__ = ["EvalAdapter"]
