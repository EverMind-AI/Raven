"""RoutineAggregator — LLM polish for candidate routines.

The deterministic RoutineLearner produces patterns like
``"Tuesday 09:00-12:00 — meeting · standup · pr"`` — useful for matching
but unfriendly when surfaced to the user. RoutineAggregator runs once
per discovery cycle (daily-cadence) and:

1. Generates a one-line natural-language description for each routine
   (``"Tuesday morning engineering standup"``).
2. Assigns a ``semantic_group`` key so two near-duplicate routines
   ("Wednesday 9am — sync · meeting" and "Wednesday 9am — meeting")
   share the same group and get collapsed when surfaced as menu options.

Output is persisted via ``RoutineStore.upsert_description`` so subsequent
TaskDiscoverer passes can reuse the polished text without re-asking the
LLM. Skipped silently when no provider/model is configured (default OFF
deployment) — the discoverer falls back to the raw learner-generated
``pattern`` string.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.proactive_engine.sentinel.types import Routine

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider
    from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore


_AGGREGATE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "describe_routines",
        "description": (
            "Emit a human-friendly description and semantic group for "
            "each routine. Group key should be a short snake_case "
            "identifier. Routines that look like duplicates "
            "(e.g. 'morning_standup' on different weekdays) MAY share "
            "the same group key — the menu collapses them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "routines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": (
                                    "The routine id you were given — "
                                    "verbatim, no invention."
                                ),
                            },
                            "description": {
                                "type": "string",
                                "description": (
                                    "Imperative-mood one-liner the user "
                                    "will see (≤ 60 chars). E.g. "
                                    "'Tuesday morning engineering "
                                    "standup'."
                                ),
                            },
                            "semantic_group": {
                                "type": "string",
                                "description": (
                                    "Short snake_case cluster key "
                                    "(≤ 24 chars), e.g. "
                                    "'morning_standup'."
                                ),
                            },
                        },
                        "required": ["id", "description"],
                    },
                }
            },
            "required": ["routines"],
        },
    },
}


class RoutineAggregator:
    """LLM-driven natural-language description + semantic clustering."""

    def __init__(
        self,
        *,
        provider: "LLMProvider",
        model: str,
        routine_store: "RoutineStore",
    ) -> None:
        self.provider = provider
        self.model = model
        self.routine_store = routine_store

    async def aggregate(self, routines: list[Routine]) -> int:
        """Polish a batch of routines. Returns the number of routines
        successfully described (and persisted via RoutineStore).

        Skips routines that already have ``description`` set — re-asking
        the LLM each day for stable routines wastes tokens. Drop the
        description manually (RoutineStore.upsert_description with
        description='') if you want to force a re-aggregate.

        Never raises — failures degrade to 0 + warning log so the
        discoverer's daily pass isn't blocked by a flaky LLM call."""
        try:
            return await self._aggregate_inner(routines)
        except Exception as exc:
            logger.exception("RoutineAggregator.aggregate raised: {}", exc)
            return 0

    async def _aggregate_inner(self, routines: list[Routine]) -> int:
        pending = [r for r in routines if not (r.description or "").strip()]
        if not pending:
            return 0

        messages = self._build_messages(pending)
        response = await self.provider.chat_with_retry(
            messages=messages,
            tools=[_AGGREGATE_TOOL_SCHEMA],
            model=self.model,
            tool_choice="required",
        )

        items = self._parse_response(response)
        if not items:
            return 0

        # Guard against LLM-hallucinated ids we never sent.
        valid_ids = {r.id for r in pending}
        applied = 0
        for item in items:
            rid = item.get("id")
            description = (item.get("description") or "").strip()
            semantic_group = (item.get("semantic_group") or "").strip() or None
            if not rid or not description:
                continue
            if rid not in valid_ids:
                logger.warning(
                    "RoutineAggregator: LLM returned unknown id {!r}; "
                    "ignoring", rid,
                )
                continue
            if self.routine_store.upsert_description(
                rid, description=description, semantic_group=semantic_group,
            ):
                applied += 1
        logger.info(
            "RoutineAggregator: applied {} description(s) of {} "
            "candidate(s)", applied, len(pending),
        )
        return applied

    @staticmethod
    def _build_messages(
        routines: list[Routine],
    ) -> list[dict[str, Any]]:
        dow_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        lines: list[str] = []
        for r in routines:
            dow = dow_names[r.day_of_week] if r.day_of_week is not None else "*"
            slot = (
                f"{r.time_slot[0]:02d}:00-{r.time_slot[1]:02d}:00"
                if r.time_slot else "*"
            )
            kw = ", ".join(r.keywords) if r.keywords else "(no keywords)"
            lines.append(
                f"- id={r.id}, day={dow}, slot={slot}, "
                f"count={r.occurrence_count}, weight={r.weight:.1f}, "
                f"keywords={kw}"
            )
        listing = "\n".join(lines)
        system = (
            "You polish raw recurring-activity patterns into short "
            "user-friendly descriptions. Each routine has an id you "
            "MUST use verbatim. Generate one description per routine. "
            "Routines that capture the same underlying habit (e.g. "
            "'Monday 09:00 standup' and 'Wednesday 09:00 standup') "
            "should share the same semantic_group key so the menu "
            "can collapse them."
        )
        user = (
            "Routines to describe:\n"
            f"{listing}\n\n"
            "Return one entry per routine via the describe_routines "
            "tool."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _parse_response(response: Any) -> list[dict[str, Any]]:
        if not getattr(response, "has_tool_calls", False):
            return []
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            return []
        args = getattr(tool_calls[0], "arguments", None)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return []
        if not isinstance(args, dict):
            return []
        items = args.get("routines")
        if not isinstance(items, list):
            return []
        return [it for it in items if isinstance(it, dict)]


__all__ = ["RoutineAggregator"]
