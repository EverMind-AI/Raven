"""DecisionRouter — match user replies to a pending discovery menu.

Two-tier matching:

1. **Deterministic escape hatch** — ``/pick N`` regex. Always wins,
   confidence=1.0, no LLM cost. Use case: power user / mobile typing
   shortcut / unambiguous testing.
2. **LLM classifier** — for everything else. Asked "did the user pick
   one of these N options? which? confidence?". Confidence-gated: if
   below threshold, treat the reply as a normal conversation message
   (consumed=False), so the user can carry on talking past a stale
   menu without it eating their words.

If neither matches, ``consumed=False`` and AgentLoop processes the
message normally.

Side-effects (mark_consumed / FeedbackTracker / ActionExecutor) live in
the AgentLoop hook layer (MS4) — DecisionRouter just produces a
RouteResult.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from raven.proactive_engine.sentinel.types import PendingDecision, RouteResult, TaskOption

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider
    from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore


# Deterministic regex — recognized as command-grade input. Matches:
#   /pick 1
#   /pick   3
# (lowercased before match). Anchored to start/end so a casual mention
# like "I want to /pick 1 of these" doesn't auto-trigger.
_PICK_RE = re.compile(r"^/pick\s+(\d+)\s*$", re.IGNORECASE)

# Confirm-mode regexes — only consulted when pending.awaiting_confirm=True.
# Anchored to whole-message so "yes please" matches but "yes I'd like to..."
# falls through to LLM (or treated as ambiguous).
_CONFIRM_YES_RE = re.compile(
    r"^\s*(yes|y|是|确认|好|ok|嗯|对|/confirm)\s*[!.！。]?\s*$",
    re.IGNORECASE,
)
_CONFIRM_NO_RE = re.compile(
    r"^\s*(no|n|否|取消|不|算了|cancel|/cancel)\s*[!.！。]?\s*$",
    re.IGNORECASE,
)


_CONFIRM_CLASSIFIER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "classify_confirm_response",
        "description": (
            "Classify a user reply to a yes/no confirmation prompt. Use "
            "intent='confirm' when the reply means 'go ahead' (yes / "
            "ok / 是 / 嗯 / 确认 / 'do it'), intent='cancel' when it "
            "means 'no, drop it' (no / 不 / 取消 / 算了 / 'never mind'), "
            "and intent='other' when the reply doesn't clearly answer "
            "the yes/no prompt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["confirm", "cancel", "other"],
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
            },
            "required": ["intent", "confidence"],
        },
    },
}


_CLASSIFIER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "classify_menu_response",
        "description": (
            "Classify a user reply to a numbered discovery menu. Use "
            "intent='pick' when they chose one of the options, "
            "intent='skip' when they explicitly declined the whole menu "
            "('跳过' / 'skip' / 'no thanks' / 'not now'), "
            "intent='other' when the reply is unrelated to the menu."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["pick", "skip", "other"],
                },
                "option_index": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "1-based index of the picked option. Required "
                        "when intent='pick'; ignored otherwise."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": (
                        "How sure are you (0-1). Use < 0.7 if the "
                        "reply is ambiguous; the router will treat low-"
                        "confidence as 'other' to avoid hijacking a "
                        "normal conversation."
                    ),
                },
            },
            "required": ["intent", "confidence"],
        },
    },
}


class DecisionRouter:
    """Routes user replies to pending discovery menus."""

    def __init__(
        self,
        *,
        pending_store: "PendingDecisionStore",
        provider: "LLMProvider | None" = None,
        model: str | None = None,
        confidence_threshold: float = 0.7,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.pending_store = pending_store
        self.provider = provider
        self.model = model
        self.confidence_threshold = confidence_threshold
        self._now_fn = now_fn or datetime.now

    # ------------------------------------------------------------------
    # Public API

    async def maybe_consume(
        self,
        *,
        channel: str,
        to: str,
        content: str,
    ) -> RouteResult:
        """Inspect a user message and decide whether it consumes a
        pending menu. Never raises — failures degrade to consumed=False
        so AgentLoop falls through to normal processing."""
        try:
            return await self._maybe_consume_inner(
                channel=channel, to=to, content=content,
            )
        except Exception as exc:
            logger.exception("DecisionRouter.maybe_consume raised: {}", exc)
            return RouteResult(consumed=False)

    # ------------------------------------------------------------------
    # Internals

    async def _maybe_consume_inner(
        self,
        *,
        channel: str,
        to: str,
        content: str,
    ) -> RouteResult:
        now_ms = int(self._now_fn().timestamp() * 1000)
        pending = self.pending_store.get_recent(channel, to, now_ms=now_ms)
        if pending is None:
            return RouteResult(consumed=False)

        text = (content or "").strip()
        if not text:
            return RouteResult(consumed=False)

        # ── Confirm-mode dispatch ─────────────────────────────────
        # When the decision is in AWAITING_CONFIRM, the only meaningful
        # parses are yes/no (or LLM-classified equivalents). Plain "1"
        # or "/pick 2" don't make sense here — we ignore them and let
        # the message fall through (consumed=False) so the user can
        # carry on talking past the still-live confirm prompt.
        if pending.awaiting_confirm:
            return await self._maybe_consume_confirm(pending, text)

        # Tier 1 — /pick N
        m = _PICK_RE.match(text)
        if m:
            return self._resolve_pick_index(pending, int(m.group(1)),
                                            method="regex_pick")

        # Tier 2 — LLM classifier
        if self.provider is None or self.model is None:
            # Router instantiated without an LLM (test / staged rollout) —
            # without LLM, only /pick N is recognized.
            return RouteResult(consumed=False)

        return await self._llm_classify(pending, text)

    # ------------------------------------------------------------------
    # Confirm-mode (second leg of two-step pick → confirm flow)

    async def _maybe_consume_confirm(
        self,
        pending: PendingDecision,
        text: str,
    ) -> RouteResult:
        # Tier 1 — yes/no regex (deterministic, multilingual)
        if _CONFIRM_YES_RE.match(text):
            return self._build_confirm_result(
                pending, intent="confirm", confidence=1.0,
                method="regex_yesno",
            )
        if _CONFIRM_NO_RE.match(text):
            return self._build_confirm_result(
                pending, intent="cancel", confidence=1.0,
                method="regex_yesno",
            )

        # Tier 2 — LLM classifier (if provider available)
        if self.provider is None or self.model is None:
            return RouteResult(consumed=False)

        intent, conf = await self._llm_classify_confirm(pending, text)
        if intent is None or conf < self.confidence_threshold:
            logger.info(
                "DecisionRouter: confirm-mode classifier intent={} "
                "confidence={:.2f} below threshold {:.2f}; decision {} "
                "stays awaiting",
                intent, conf, self.confidence_threshold,
                pending.decision_id,
            )
            return RouteResult(
                consumed=False,
                confidence=conf,
                raw_match_method="llm_classifier",
            )
        if intent in ("confirm", "cancel"):
            return self._build_confirm_result(
                pending, intent=intent, confidence=conf,
                method="llm_classifier",
            )
        return RouteResult(
            consumed=False, confidence=conf,
            raw_match_method="llm_classifier",
        )

    def _build_confirm_result(
        self,
        pending: PendingDecision,
        *,
        intent: str,
        confidence: float,
        method: str,
    ) -> RouteResult:
        # Hydrate the original picked option from pending.picked_option_id
        # so the consumer can pass it to ActionExecutor without a second
        # store lookup.
        option = None
        if pending.picked_option_id is not None:
            for opt in pending.options:
                if opt.id == pending.picked_option_id:
                    option = opt
                    break
        return RouteResult(
            consumed=True,
            pending_decision_id=pending.decision_id,
            option=option,
            confidence=confidence,
            raw_match_method=method,  # type: ignore[arg-type]
            confirm_intent=intent,    # type: ignore[arg-type]
        )

    async def _llm_classify_confirm(
        self,
        pending: PendingDecision,
        content: str,
    ) -> tuple[str | None, float]:
        # Same shape as _llm_classify but with the confirm tool schema.
        opt = None
        if pending.picked_option_id is not None:
            for o in pending.options:
                if o.id == pending.picked_option_id:
                    opt = o
                    break
        opt_title = opt.title if opt else "(unknown)"

        messages = [
            {
                "role": "system",
                "content": (
                    "You classify user replies to a yes/no confirmation "
                    "prompt. Use classify_confirm_response with intent ∈ "
                    "{confirm, cancel, other}. Be strict — return "
                    "intent='other' with low confidence if the reply is "
                    "ambiguous, so the system treats it as a normal "
                    "conversation message and the confirm prompt stays "
                    "live."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"User was asked: '要执行：{opt_title}? 回复 yes / no'\n\n"
                    f"User replied: {content!r}\n\n"
                    "Did they confirm or cancel?"
                ),
            },
        ]

        try:
            response = await self.provider.chat_with_retry(
                messages=messages,
                tools=[_CONFIRM_CLASSIFIER_TOOL],
                model=self.model,
                tool_choice="required",
            )
        except Exception as exc:
            logger.warning("LLM confirm-classifier raised: {}", exc)
            return None, 0.0

        return self._parse_confirm_classifier_response(response)

    @staticmethod
    def _parse_confirm_classifier_response(
        response: Any,
    ) -> tuple[str | None, float]:
        if not getattr(response, "has_tool_calls", False):
            return None, 0.0
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            return None, 0.0
        args = getattr(tool_calls[0], "arguments", None)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return None, 0.0
        if not isinstance(args, dict):
            return None, 0.0
        intent = args.get("intent")
        if intent not in ("confirm", "cancel", "other"):
            return None, 0.0
        try:
            conf = float(args.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        return intent, conf

    def _resolve_pick_index(
        self,
        pending: PendingDecision,
        index_1based: int,
        *,
        method: str,
    ) -> RouteResult:
        if index_1based < 1 or index_1based > len(pending.options):
            logger.info(
                "DecisionRouter: /pick {} out of range for decision {} "
                "(has {} options); treating as no-match",
                index_1based, pending.decision_id, len(pending.options),
            )
            return RouteResult(consumed=False)
        opt = pending.options[index_1based - 1]
        return RouteResult(
            consumed=True,
            pending_decision_id=pending.decision_id,
            option=opt,
            confidence=1.0,
            raw_match_method=method,
        )

    async def _llm_classify(
        self,
        pending: PendingDecision,
        content: str,
    ) -> RouteResult:
        messages = self._build_classifier_messages(pending, content)
        response = await self.provider.chat_with_retry(
            messages=messages,
            tools=[_CLASSIFIER_TOOL],
            model=self.model,
            tool_choice="required",
        )
        intent, opt_idx, conf = self._parse_classifier_response(response)
        if intent is None or conf < self.confidence_threshold:
            logger.info(
                "DecisionRouter: LLM classifier intent={} confidence={:.2f} "
                "below threshold {:.2f}; treating as 'other' (decision {} "
                "stays live)",
                intent, conf, self.confidence_threshold, pending.decision_id,
            )
            return RouteResult(
                consumed=False,
                confidence=conf,
                raw_match_method="llm_classifier",
            )

        if intent == "skip":
            return RouteResult(
                consumed=True,
                pending_decision_id=pending.decision_id,
                option=None,                       # skip → no option picked
                confidence=conf,
                raw_match_method="llm_classifier",
            )

        if intent == "pick" and opt_idx is not None:
            if 1 <= opt_idx <= len(pending.options):
                return RouteResult(
                    consumed=True,
                    pending_decision_id=pending.decision_id,
                    option=pending.options[opt_idx - 1],
                    confidence=conf,
                    raw_match_method="llm_classifier",
                )
            # Out-of-range index from a confident LLM — treat as no-match
            # rather than crash. Better the menu stays live than executing
            # a hallucinated option.
            logger.warning(
                "DecisionRouter: LLM picked option_index={} but only "
                "{} options exist; falling back to no-match",
                opt_idx, len(pending.options),
            )

        # 'other', 'pick' without a valid index, or some other shape
        return RouteResult(
            consumed=False,
            confidence=conf,
            raw_match_method="llm_classifier",
        )

    @staticmethod
    def _build_classifier_messages(
        pending: PendingDecision, content: str
    ) -> list[dict[str, Any]]:
        # Reproduce the menu the user saw so the LLM has the same options
        # in the same order — matters for "the second one" disambiguation.
        opt_lines = [
            f"  {i}. ({o.type}) {o.title} — {o.why}"
            for i, o in enumerate(pending.options, start=1)
        ]
        system = (
            "You classify user replies to a numbered task-suggestion menu. "
            "Call classify_menu_response with intent='pick' / 'skip' / "
            "'other' and a confidence score. Be strict — if the reply is "
            "ambiguous (e.g. '都行', 'whatever', 'maybe'), return "
            "intent='other' with confidence < 0.7 so the system treats "
            "it as a normal conversation message."
        )
        user = (
            "User was shown this menu:\n"
            + "\n".join(opt_lines)
            + "\n\nUser replied:\n"
            f"  {content!r}\n"
            "\nWhich option, if any, did they pick?"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _parse_classifier_response(
        response: Any,
    ) -> tuple[str | None, int | None, float]:
        """Extract (intent, option_index, confidence) from the tool call.
        Returns (None, None, 0.0) on any parsing failure — caller treats
        that as 'other' below threshold."""
        if not getattr(response, "has_tool_calls", False):
            return None, None, 0.0
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            return None, None, 0.0
        args = getattr(tool_calls[0], "arguments", None)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return None, None, 0.0
        if not isinstance(args, dict):
            return None, None, 0.0
        intent = args.get("intent")
        if intent not in ("pick", "skip", "other"):
            return None, None, 0.0
        idx = args.get("option_index")
        if not isinstance(idx, int):
            idx = None
        conf = args.get("confidence", 0.0)
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        return intent, idx, conf


__all__ = ["DecisionRouter"]
