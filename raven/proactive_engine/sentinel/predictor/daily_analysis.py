"""DailyAnalysisService — one LLM call/day, three attention.md sections.

Reads:

- ``user_memory/episodic/episodes.md`` (last ``episodes_window_days`` days,
  capped at ``max_episodes`` lines)
- ``RoutineStore`` active + candidate routines
- Session inbound messages within ``inbound_window_hours`` (capped at
  ``max_inbound_messages``)

Calls the LLM once via the ``emit_daily_analysis`` tool. The tool's
structured output ``DailyAnalysisResult`` carries three arrays:

- ``stance_entries``: user-expressed preference statements detected in
  the inbound window (feeds ``## Recent stance log (30d)``)
- ``predictions``: 3-day forecast (feeds ``## Predicted next 3 days``)
- ``patterns``: cross-project behavior summaries (feeds
  ``## Cross-project behavior patterns (14d)``)

The result is cached for ``cooldown_hours`` so the three producer
classes in ``attention_producers/`` can each call ``service.get(now)``
on the same tick; only the first call hits the LLM.

When the LLM call fails (provider error, timeout, parse failure) and
``enable_prefix_fallback`` is True, a cheap stance-prefix heuristic
runs over the inbound window so the stance log still gets something —
predictions / patterns stay empty in that fallback path.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from loguru import logger

from raven.memory_engine.consolidate.consolidator import _parse_episode_line

if TYPE_CHECKING:
    from raven.config.raven import DailyAnalysisConfig
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.providers.base import LLMProvider
    from raven.proactive_engine.sentinel.predictor.routine_store import (
        RoutineStore,
    )
    from raven.session.manager import SessionManager


@dataclass
class StanceEntry:
    ts: str          # ISO datetime when the inbound was authored
    text: str        # original sentence


@dataclass
class Prediction:
    date: str        # ISO YYYY-MM-DD
    text: str
    confidence: str  # "low" | "medium" | "high"
    basis: str       # evidence the LLM grounded on


@dataclass
class BehaviorPattern:
    kind: str        # "temporal" | "workflow" | "topical"
    text: str
    supporting_projects: list[str] = field(default_factory=list)
    confidence: str = "medium"


@dataclass
class DailyAnalysisResult:
    generated_at: datetime
    stance_entries: list[StanceEntry] = field(default_factory=list)
    predictions: list[Prediction] = field(default_factory=list)
    patterns: list[BehaviorPattern] = field(default_factory=list)


_TOOL_NAME = "emit_daily_analysis"

# Short cooldown for empty/failed cache entries. 1h trades a worst-case
# 24 retries/day (well below "every tick" retry storm) for fast recovery
# from transient provider blips.
_FAILURE_COOLDOWN = timedelta(hours=1)


def _tool_schema() -> list[dict[str, Any]]:
    return [{
        "type": "function",
        "function": {
            "name": _TOOL_NAME,
            "description": (
                "Emit a structured daily analysis covering three views of "
                "the user's recent state. Empty arrays acceptable when "
                "the input lacks signal for a given view — better to "
                "skip than fabricate."
            ),
            "parameters": {
                "type": "object",
                "required": [
                    "stance_entries", "predictions", "patterns",
                ],
                "properties": {
                    "stance_entries": {
                        "type": "array",
                        "description": (
                            "Sentences in the recent inbound window where "
                            "the user expressed a preference / directive. "
                            "Triggers: 'I prefer X', 'stop Y', 'always Z', "
                            "'from now on...', 'avoid W'. Skip casual "
                            "statements. Use the original sentence as "
                            "``text``; ``source_ts`` is the inbound's "
                            "timestamp."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["text", "source_ts"],
                            "properties": {
                                "text": {"type": "string"},
                                "source_ts": {"type": "string"},
                            },
                        },
                    },
                    "predictions": {
                        "type": "array",
                        "description": (
                            "Three-day forecast — one entry per concrete "
                            "predicted activity. Ground on the episodes "
                            "+ active routines context. ``date`` ISO "
                            "YYYY-MM-DD; ``confidence`` ∈ {low, medium, "
                            "high}; ``basis`` cites evidence (e.g. "
                            "'4/5 last Sundays at 19:00')."
                        ),
                        "items": {
                            "type": "object",
                            "required": [
                                "date", "text", "confidence", "basis",
                            ],
                            "properties": {
                                "date": {"type": "string"},
                                "text": {"type": "string"},
                                "confidence": {
                                    "type": "string",
                                    "enum": ["low", "medium", "high"],
                                },
                                "basis": {"type": "string"},
                            },
                        },
                    },
                    "patterns": {
                        "type": "array",
                        "description": (
                            "Cross-project behavior summaries observed "
                            "in the 14d window. ``kind`` ∈ {temporal, "
                            "workflow, topical}. ``supporting_projects`` "
                            "lists the project tags evidencing the "
                            "pattern."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["kind", "text", "confidence"],
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": [
                                        "temporal", "workflow", "topical",
                                    ],
                                },
                                "text": {"type": "string"},
                                "supporting_projects": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "confidence": {
                                    "type": "string",
                                    "enum": ["low", "medium", "high"],
                                },
                            },
                        },
                    },
                },
            },
        },
    }]


_SYSTEM_PROMPT = """\
You read a snapshot of one user's recent activity (episodes log + active
routines + last 24h of inbound messages) and emit three coordinated
views via the ``emit_daily_analysis`` tool:

1. **Stance entries** — sentences where the user expressed an explicit
   preference or directive. Lift the original sentence verbatim.
2. **Predictions** — next 3 days, one entry per concrete activity. Each
   prediction must cite specific evidence in ``basis`` (e.g. "weekly
   pattern, 4/5 last Sundays" / "scheduled deadline"). Don't fabricate
   beyond what the data shows.
3. **Patterns** — behaviors observed across projects in the 14d window.
   Stick to grounded observations; one bullet per distinct pattern.

If a view has no signal, return an empty array for it. Don't pad.
"""


def _prefix_detect_stance(text: str, prefixes: list[str]) -> str | None:
    norm = text.lower().strip()
    for prefix in prefixes:
        if norm.startswith(prefix.lower()):
            return text.strip()
    return None


class DailyAnalysisService:
    """Owns the LLM call + cache for the three daily-analysis sections.

    Producers call ``await service.get(now)`` from their ``compute_body``;
    first call within ``cooldown_hours`` triggers the LLM, subsequent
    calls return the cached :class:`DailyAnalysisResult`.
    """

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        routine_store: "RoutineStore",
        session_manager: "SessionManager",
        provider: "LLMProvider",
        config: "DailyAnalysisConfig",
        model: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._routine_store = routine_store
        self._session_manager = session_manager
        self._provider = provider
        self._config = config
        self._model = model or config.model or ""
        self._now_fn = now_fn or datetime.now
        self._cache: DailyAnalysisResult | None = None
        # Serializes ``get()`` so concurrent producers share one LLM call
        # rather than each missing the cache.
        self._inflight_lock = asyncio.Lock()

    @property
    def cache(self) -> DailyAnalysisResult | None:
        return self._cache

    async def get(
        self, now: datetime, *, force: bool = False,
    ) -> DailyAnalysisResult | None:
        """Return the cached result if fresh, otherwise run the LLM call
        and cache. Failure paths cache an empty result so the cooldown
        gate throttles retries — empty caches use a 1h cooldown so a
        transient provider hiccup recovers within the hour; successful
        results use ``cooldown_hours`` (default 24h, daily cadence).

        ``force=True`` bypasses the cooldown (used by CLI rebuild).

        Serialized by ``self._inflight_lock`` — concurrent callers see
        the cache populated by the first call instead of duplicating
        the LLM request.
        """
        if not self._config.enabled:
            return None
        async with self._inflight_lock:
            if not force and self._cache is not None:
                # Empty cache (LLM failure or no-data) uses the short
                # FAILURE_COOLDOWN so transient hiccups recover quickly;
                # successful results use the full configured cooldown.
                has_content = bool(
                    self._cache.stance_entries
                    or self._cache.predictions
                    or self._cache.patterns
                )
                cooldown = (
                    timedelta(hours=self._config.cooldown_hours)
                    if has_content
                    else _FAILURE_COOLDOWN
                )
                if now - self._cache.generated_at < cooldown:
                    return self._cache

            try:
                result = await self._run_llm(now)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "DailyAnalysisService LLM call failed: {}: {}",
                    type(exc).__name__, exc,
                )
                result = None

            if result is None and self._config.enable_prefix_fallback:
                stance = self._prefix_fallback_stance(now)
                if stance:
                    result = DailyAnalysisResult(
                        generated_at=now, stance_entries=stance,
                    )

            if result is None:
                result = DailyAnalysisResult(generated_at=now)
            self._cache = result
            return result

    # ── LLM call ────────────────────────────────────────────────────

    async def _run_llm(
        self, now: datetime,
    ) -> DailyAnalysisResult | None:
        episodes = self._assemble_episodes(now)
        routines = self._assemble_routines()
        inbound = self._assemble_inbound(now)
        if not episodes and not routines and not inbound:
            return DailyAnalysisResult(generated_at=now)
        prompt = self._render_prompt(now, episodes, routines, inbound)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        response = await self._provider.chat_with_retry(
            messages=messages,
            tools=_tool_schema(),
            model=self._model or None,
            tool_choice={
                "type": "function",
                "function": {"name": _TOOL_NAME},
            },
        )
        if not response.has_tool_calls:
            return None
        args = response.tool_calls[0].arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return None
        if not isinstance(args, dict):
            return None
        return _parse_result(args, now)

    # ── Input assembly ──────────────────────────────────────────────

    def _assemble_episodes(self, now: datetime) -> list[str]:
        path = self._memory_store.history_file
        if not path.exists():
            return []
        cutoff = now - timedelta(days=self._config.episodes_window_days)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        kept: list[str] = []
        for line in text.splitlines():
            parsed = _parse_episode_line(line)
            if not parsed:
                continue
            ts, _, _ = parsed
            try:
                dt = datetime.strptime(
                    ts.replace("T", " "), "%Y-%m-%d %H:%M",
                )
            except ValueError:
                continue
            if dt < cutoff:
                continue
            kept.append(line.strip())
        return kept[-self._config.max_episodes:]

    def _assemble_routines(self) -> list[str]:
        out: list[str] = []
        try:
            for r in self._routine_store.all_routines():
                if r.status not in {"active", "candidate"}:
                    continue
                weight = r.weight or 0.0
                out.append(
                    f"- [{r.status}] {r.pattern} "
                    f"(occ {r.occurrence_count}, weight {weight:.2f})"
                )
        except Exception:  # noqa: BLE001
            return []
        return out

    def _assemble_inbound(
        self, now: datetime,
    ) -> list[tuple[str, str, str]]:
        """Return ``[(session_key, ts_iso, text), ...]`` from session
        files within the inbound window."""
        cutoff = now - timedelta(hours=self._config.inbound_window_hours)
        sessions_dir = self._session_manager.sessions_dir
        if not sessions_dir.is_dir():
            return []
        out: list[tuple[str, str, str]] = []
        for path in sessions_dir.rglob("*.jsonl"):
            key = self._session_manager.key_from_path(path)
            try:
                with path.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get("_type") == "metadata":
                            mk = data.get("key")
                            if isinstance(mk, str) and mk:
                                key = mk
                            continue
                        if data.get("role") != "user":
                            continue
                        ts_raw = data.get("timestamp", "")
                        try:
                            ts = datetime.fromisoformat(ts_raw)
                        except (ValueError, TypeError):
                            continue
                        if ts.tzinfo is not None:
                            ts = ts.replace(tzinfo=None)
                        if ts < cutoff:
                            continue
                        text = data.get("content")
                        if isinstance(text, list):
                            text = " ".join(
                                blk.get("text", "")
                                for blk in text
                                if isinstance(blk, dict)
                                and blk.get("type") == "text"
                            )
                        if not isinstance(text, str) or not text.strip():
                            continue
                        out.append((key, ts_raw, text.strip()))
            except OSError:
                continue
        out.sort(key=lambda t: t[1])
        return out[-self._config.max_inbound_messages:]

    def _render_prompt(
        self,
        now: datetime,
        episodes: list[str],
        routines: list[str],
        inbound: list[tuple[str, str, str]],
    ) -> str:
        parts: list[str] = []
        parts.append(f"Current time: {now.isoformat(timespec='minutes')}")
        parts.append(f"Day of week: {now.strftime('%A')}")
        parts.append("")
        if episodes:
            parts.append(
                f"## Recent episodes "
                f"({self._config.episodes_window_days}d window, "
                f"{len(episodes)} entries)"
            )
            parts.extend(episodes)
            parts.append("")
        if routines:
            parts.append("## Active routines + candidates")
            parts.extend(routines)
            parts.append("")
        if inbound:
            parts.append(
                f"## Recent user inbound "
                f"({self._config.inbound_window_hours}h window, "
                f"{len(inbound)} messages)"
            )
            for key, ts, text in inbound:
                parts.append(f"- [{ts[:16]}] {key}: {text}")
            parts.append("")
        return "\n".join(parts)

    # ── Prefix fallback ─────────────────────────────────────────────

    def _prefix_fallback_stance(
        self, now: datetime,
    ) -> list[StanceEntry]:
        out: list[StanceEntry] = []
        prefixes = list(self._config.stance_prefix_fallback)
        for _key, ts, text in self._assemble_inbound(now):
            detected = _prefix_detect_stance(text, prefixes)
            if detected:
                out.append(StanceEntry(ts=ts, text=detected))
        return out


def _parse_result(
    args: dict[str, Any], now: datetime,
) -> DailyAnalysisResult:
    stance: list[StanceEntry] = []
    for raw in args.get("stance_entries", []) or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        ts = str(raw.get("source_ts") or "").strip()
        if text and ts:
            stance.append(StanceEntry(ts=ts, text=text))
    predictions: list[Prediction] = []
    for raw in args.get("predictions", []) or []:
        if not isinstance(raw, dict):
            continue
        date = str(raw.get("date") or "").strip()
        text = str(raw.get("text") or "").strip()
        confidence = str(raw.get("confidence") or "").strip()
        basis = str(raw.get("basis") or "").strip()
        if not (date and text and confidence in {"low", "medium", "high"}):
            continue
        predictions.append(Prediction(
            date=date, text=text, confidence=confidence, basis=basis,
        ))
    patterns: list[BehaviorPattern] = []
    for raw in args.get("patterns", []) or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip()
        text = str(raw.get("text") or "").strip()
        confidence = str(raw.get("confidence") or "medium").strip()
        if kind not in {"temporal", "workflow", "topical"} or not text:
            continue
        sp_raw = raw.get("supporting_projects") or []
        if not isinstance(sp_raw, list):
            sp_raw = []
        sp = [str(p).strip() for p in sp_raw if str(p).strip()]
        patterns.append(BehaviorPattern(
            kind=kind, text=text, supporting_projects=sp,
            confidence=confidence,
        ))
    return DailyAnalysisResult(
        generated_at=now,
        stance_entries=stance,
        predictions=predictions,
        patterns=patterns,
    )


__all__ = [
    "DailyAnalysisService",
    "DailyAnalysisResult",
    "StanceEntry",
    "Prediction",
    "BehaviorPattern",
]
