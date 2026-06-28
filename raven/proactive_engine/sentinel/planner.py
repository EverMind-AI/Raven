"""ProactivePlanner — periodic tick that decides whether to act proactively."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from loguru import logger

from raven.proactive_engine.sentinel.trigger_policy.prompts import (
    PLANNER_TOOL,
    SYSTEM_PROMPT,
    build_context_prompt,
)
from raven.proactive_engine.sentinel.types import PlannerContext, PlannerDecision

# Words to drop when deriving an auto topic_tag from nudge_message — common
# Chinese/English stopwords + sentinel-specific filler that adds no
# topic signal.
_AUTO_TAG_STOPWORDS = frozenset({
    "你", "我", "的", "了", "是", "和", "或", "在", "有", "要", "对", "也",
    "都", "就", "但", "可以", "可能", "应该", "需要", "还是", "如果", "因为",
    "所以", "提醒", "记得", "建议", "注意", "另外", "顺便", "另", "今天",
    "明天", "昨天", "最近", "马上", "the", "a", "an", "is", "are", "to",
    "of", "and", "or", "in", "on", "for", "with", "you", "your", "i",
    "me", "my", "be", "this", "that", "it",
})


def _derive_auto_tag(message: str | None, action: str) -> str:
    """Derive a stable snake_case topic tag from message content.

    Used as fallback when the Planner LLM omits ``topic_tag`` (qwen-27b on
    volcano returns null 100% of the time despite schema marking it
    required). Without a tag, every NudgePolicy per-topic gate in
    ``policy.py`` is bypassed because of ``if topic_tag:`` guards —
    paraphrased duplicates of the same topic all fire freely.

    Strategy: take the first ~3 content words (stopwords dropped) and
    normalize to snake_case. NO hash suffix — paraphrases of the same
    logical topic should collapse to the same tag so the per-topic dedup
    / cap engage. Better than ``None`` because at least the content-hash
    dedup and per-tag cap will engage.
    """
    base = (message or "").strip().lower()
    if not base:
        return f"auto_{action}_empty"
    # Pull alphanumeric / CJK words; drop stopwords; keep top 3. NO hash
    # suffix — we WANT paraphrases of the same topic ("clawtrack 3 days left"
    # vs "clawtrack v1.0 release due in 2 days") to collapse to the same auto-tag
    # so the per-topic dedup engages. The downside is unrelated content
    # sharing first-3-words collides, which is rare and harmless.
    words = re.findall(r"[a-z0-9]+|[一-鿿]+", base)
    keep = [w for w in words if w not in _AUTO_TAG_STOPWORDS][:3]
    stem = "_".join(keep) if keep else f"hash_{hashlib.md5(base.encode()).hexdigest()[:6]}"
    return f"auto_{stem}"[:64]

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider


_VALID_ACTIONS = frozenset({
    "skip", "nudge", "nudge_inject", "nudge_defer", "spawn_agent",
})
_VALID_PRIORITIES = frozenset({"low", "medium", "high"})
_NUDGE_ACTIONS = frozenset({"nudge", "nudge_inject", "nudge_defer"})

# Pin generation params so a globally-configured reasoning_effort or
# high temperature cannot leak in via LLMProvider.generation defaults.
# Module constants so tests and impl share one source of truth.
_PLANNER_MAX_TOKENS = 1024
_PLANNER_TEMPERATURE = 0.3
_PLANNER_REASONING_EFFORT: str | None = None


class ProactivePlanner:
    """Periodic decision-maker that reads global context → picks skip/nudge/spawn.

    Designed as a pure function of (context, provider, model) → decision. No
    side effects; the caller is responsible for applying the decision (routing
    through NudgePolicy, dispatching spawn, etc.).

    Tests can pass a mock LLMProvider and verify decisions without network.
    """

    def __init__(
        self,
        provider: "LLMProvider",
        model: str,
        *,
        temperature: float | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        # Explicit value lets eval harnesses pin T=0 for reproducible
        # cold-vs-warm comparisons; None falls back to the module default.
        self.temperature = _PLANNER_TEMPERATURE if temperature is None else temperature

    async def decide(self, ctx: PlannerContext) -> PlannerDecision:
        """Run a single planning tick.

        Returns a structured decision. Never raises for LLM/parse errors —
        falls back to a safe skip decision and logs the problem, so a failing
        tick never crashes the surrounding scheduler.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_context_prompt(ctx)},
        ]

        # Tool-choice compatibility varies: OpenAI/Anthropic accept
        # {"type":"function","function":{"name":...}}; OpenRouter auto-
        # routing often rejects both that and "required". "auto" (None)
        # works everywhere; system prompt already constrains the model
        # to call the tool, and we fall back to skip if it doesn't.
        response = await self.provider.chat_with_retry(
            messages=messages,
            tools=[PLANNER_TOOL],
            model=self.model,
            max_tokens=_PLANNER_MAX_TOKENS,
            temperature=self.temperature,
            reasoning_effort=_PLANNER_REASONING_EFFORT,
        )

        if response.finish_reason == "error":
            logger.warning("Planner LLM error; defaulting to skip: {}", response.content)
            return PlannerDecision(
                action="skip",
                reason=f"llm_error: {response.content or 'unknown'}"[:300],
            )

        if not response.has_tool_calls:
            logger.warning(
                "Planner got no tool call; defaulting to skip. content={}",
                (response.content or "")[:200],
            )
            return PlannerDecision(
                action="skip",
                reason="model did not call planner_decision tool",
            )

        args = response.tool_calls[0].arguments
        if not isinstance(args, dict):
            logger.warning("Planner tool args not a dict: {!r}; defaulting to skip", args)
            return PlannerDecision(action="skip", reason="invalid tool args shape")

        action = args.get("action", "skip")
        if action not in _VALID_ACTIONS:
            logger.warning("Planner returned invalid action {!r}; forcing skip", action)
            action = "skip"

        priority = args.get("priority", "low")
        if priority not in _VALID_PRIORITIES:
            priority = "low"

        try:
            score = float(args.get("proactivity_score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))

        reason = str(args.get("reason", ""))[:500]
        nudge_message = args.get("nudge_message") or None
        spawn_task = args.get("spawn_task") or None
        defer_condition = args.get("defer_condition") or None
        target_session = args.get("target_session") or None
        # topic_tag: short stable identifier for per-topic hour quota.
        # Lowercase + strip. For real nudges/spawns, fall back to a
        # derived auto-tag when the LLM omits it (qwen-27b returns null
        # 100% on volcano); this lets the NudgePolicy per-topic gates
        # engage instead of being silently bypassed by ``if topic_tag:``.
        raw_tag = args.get("topic_tag")
        topic_tag: str | None = (
            str(raw_tag).strip().lower()[:64]
            if isinstance(raw_tag, str) and raw_tag.strip() else None
        )
        if action != "skip" and not topic_tag:
            topic_tag = _derive_auto_tag(args.get("nudge_message") or args.get("spawn_task"), action)
            logger.warning(
                "Planner omitted topic_tag for action={}; auto-derived {!r}",
                action, topic_tag,
            )

        # The schema only requires action/reason/proactivity_score, so an
        # nudge-* without nudge_message or spawn_agent without spawn_task
        # is "valid" per schema. Downgrade to skip so the runner never
        # has to guess or fall back to reason-as-message.
        if action in _NUDGE_ACTIONS and not nudge_message:
            logger.warning(
                "Planner chose {} without nudge_message; downgrading to skip. reason={}",
                action, reason[:120],
            )
            return PlannerDecision(
                action="skip",
                reason=f"downgrade: {action} missing nudge_message",
                proactivity_score=score,
                raw_llm_response={"arguments": args},
            )
        if action == "nudge_defer" and not defer_condition:
            logger.warning(
                "Planner chose nudge_defer without defer_condition; downgrading to skip. reason={}",
                reason[:120],
            )
            return PlannerDecision(
                action="skip",
                reason="downgrade: nudge_defer missing defer_condition",
                proactivity_score=score,
                raw_llm_response={"arguments": args},
            )
        if action == "spawn_agent" and not spawn_task:
            logger.warning(
                "Planner chose spawn_agent without spawn_task; downgrading to skip. reason={}",
                reason[:120],
            )
            return PlannerDecision(
                action="skip",
                reason="downgrade: spawn_agent missing spawn_task",
                proactivity_score=score,
                raw_llm_response={"arguments": args},
            )

        return PlannerDecision(
            action=action,  # type: ignore[arg-type]
            reason=reason,
            priority=priority,  # type: ignore[arg-type]
            proactivity_score=score,
            target_session=target_session,
            nudge_message=nudge_message,
            spawn_task=spawn_task,
            defer_condition=defer_condition,
            topic_tag=topic_tag,
            raw_llm_response={"arguments": args},
        )


__all__ = ["ProactivePlanner"]
