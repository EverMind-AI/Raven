"""LLM-driven user simulator for longrun proactivity eval.

Asks an LLM (default: claude-sonnet-4.5 via OpenRouter; see
``_build_simulator_provider`` in drivers/longrun.py) what action a given
persona would take next. Uses tool-calling to force a structured output
(send / idle / dismiss / end_day) — same defensive pattern as the Sentinel
Planner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from loguru import logger

from raven.providers.base import LLMProvider

ActionKind = Literal["send", "idle", "dismiss", "end_day"]


@dataclass
class SimAction:
    kind: ActionKind
    content: str | None = None  # for "send"
    idle_minutes: int | None = None  # for "idle"
    dismiss_nudge_id: str | None = None  # for "dismiss"
    reasoning: str = ""  # simulator's own justification
    turn_hint: Literal["single", "continue"] = "single"
    raw: dict[str, Any] | None = None  # debugging


@dataclass
class SimContext:
    fake_now: datetime
    persona: dict[str, Any]
    recent_turns: list[dict[str, str]] = field(default_factory=list)
    memory_tail: str = ""
    pending_nudges: list[dict[str, Any]] = field(default_factory=list)
    last_action_kind: str | None = None
    day_index: int = 0  # 0-indexed, 0..29


SIMULATOR_TOOL = {
    "type": "function",
    "function": {
        "name": "simulate_user_action",
        "description": (
            "Decide the next action this simulated user takes. Pick exactly "
            "ONE action kind. Goal: produce realistic daily behavior over "
            "many days — mix active hours with idle periods, occasional "
            "multi-turn follow-ups, and respect persona's rhythm / mood."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["send", "idle", "dismiss", "end_day"],
                    "description": (
                        "send: the user sends a message to Raven now. "
                        "idle: the user does something else for N minutes "
                        "(sleep, work offline, etc). "
                        "dismiss: explicitly ignore / reject the most "
                        "recent agent nudge. "
                        "end_day: finish the day early (go to bed); the "
                        "driver will jump to next morning."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Message text for 'send'. Should sound like the persona (tone, language, emoji usage). Empty otherwise.",
                },
                "idle_minutes": {
                    "type": "integer",
                    "description": "Minutes to idle. Required for 'idle'. Typical: 15-180 during day, 360-540 for sleep.",
                    "minimum": 1,
                    "maximum": 540,
                },
                "dismiss_nudge_id": {
                    "type": "string",
                    "description": "ID of the nudge being dismissed (from pending_nudges).",
                },
                "reasoning": {
                    "type": "string",
                    "description": "One-sentence 'why I'm doing this now' — helps debug the trajectory.",
                },
                "turn_hint": {
                    "type": "string",
                    "enum": ["single", "continue"],
                    "description": "continue = expect to follow up soon (same session); single = one-and-done.",
                },
            },
            "required": ["kind", "reasoning"],
        },
    },
}


_SYSTEM_PROMPT = """你是用户行为模拟器 — 扮演一个真实用户与 Raven AI 助手交互。

## 你的任务
根据 persona 档案 + 当前时间 + 最近对话，决定用户**现在**会做什么。
选且仅选 ONE 个 action: send / idle / dismiss / end_day。

## 关键约束
1. **真实节奏**：人不会 24h 连续和 AI 说话。正常人白天 5-15 次交互/天，晚上睡觉 idle 6-9 小时。
2. **persona 一致性**：persona 里的 communication_style / language / routines 要贯穿始终 — 比如 dev 语气简短技术、caregiver 长句子带焦虑、team lead 正式。
3. **混合 single / multi-turn**：大约 30% 的 send 会 follow up（turn_hint=continue），70% 是 one-off（single）。
4. **idle 合理**：深夜 idle 不要 45 分钟（应该 6-9h 睡觉）；工作时间不要 idle 8h（应该 1-3h）。
5. **内容真实**：send 的 content 应该是用户会对 AI 助手说的内容 —
   - 设提醒 / 问信息 / 吐槽 / 记录想法 / 请求计划建议 / …
   - 绝不要空话；不要"I want to test if you respond"；绝不要 meta-evil。
6. **响应 nudge**：如果 pending_nudges 里有 agent 推送来的消息，决定是 ignore (idle) / dismiss / 正常回复 (send 表示接受)。
7. **goals 推进**：persona.goals 里的事要在 30 天内有迹可循（偶尔聊到、问进度、报告卡点）。
8. **quirks 发挥**：persona.quirks 是行为线索，不时加入让轨迹更立体。

## 字段填写
- send: 必填 content + reasoning + turn_hint
- idle: 必填 idle_minutes + reasoning
- dismiss: 必填 dismiss_nudge_id + reasoning (只有 pending_nudges 非空时才能用)
- end_day: 只需 reasoning（通常 22:00-01:00 之间才合适）

## 语气
你是在"扮演"，不是在"代理"。content 里不要出现"as a simulator"这种 meta 信息；说话就像真正的 persona 在用 AI 助手。
"""


def _render_persona(persona: dict) -> str:
    lines = [
        f"# Persona: {persona.get('id')} ({persona.get('role', '?')})",
        f"- 时区: {persona.get('timezone', 'Asia/Shanghai')}  语言: {persona.get('language', 'zh-CN')}",
        f"- 作息: {persona.get('wake_hours', [7, 23])}",
        f"- 沟通风格: {persona.get('communication_style', '')}",
    ]
    rhythm = persona.get("weekly_rhythm") or {}
    if rhythm:
        lines.append("## 周节奏")
        for k, v in rhythm.items():
            lines.append(f"- {k}: {v}")
    goals = persona.get("goals") or []
    if goals:
        lines.append("## 30 天内要推进的事")
        lines.extend(f"- {g}" for g in goals)
    quirks = persona.get("quirks") or []
    if quirks:
        lines.append("## 行为特征")
        lines.extend(f"- {q}" for q in quirks)
    return "\n".join(lines)


def _render_context(ctx: SimContext) -> str:
    fake_now = ctx.fake_now
    weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][fake_now.weekday()]
    parts = [
        "## 当前状态",
        f"- fake_now: {fake_now.isoformat()} ({weekday})",
        f"- day_index: {ctx.day_index}/30",
        f"- 上次 action: {ctx.last_action_kind or '(无)'}",
    ]
    if ctx.recent_turns:
        parts.append("\n## 最近对话（最多 8 条）")
        for t in ctx.recent_turns[-8:]:
            role = t.get("role", "?")
            content = (t.get("content") or "").strip()[:300]
            parts.append(f"- [{role}] {content}")
    if ctx.pending_nudges:
        parts.append("\n## 待处理的 agent 推送")
        for n in ctx.pending_nudges:
            parts.append(f"- id={n.get('id', '?')} at={n.get('fake_now', '?')} content={n.get('content', '')[:150]!r}")
    if ctx.memory_tail:
        parts.append("\n## MEMORY.md 尾部")
        parts.append("```\n" + ctx.memory_tail[-1500:] + "\n```")
    return "\n".join(parts)


_MATERIALIZE_INTENT_SYSTEM = """你是用户行为模拟器 — 扮演真实用户对 AI 助手说话。

现在轮到你**把一个预定的 intent 转成具体的一句话**（user 的第一条消息）。

## 输入
- Persona（你是谁）
- 当前时间 fake_now
- Intent 规格：topic + kind + depth
- 最近的 memory 尾部 + 最近对话（用于保持连贯）

## 任务
只生成**一条 user 的首条消息**，自然地引出这个 intent。符合 persona 语气、emoji 习惯、长短偏好。

- 不要复述 intent 的 topic（那是 agent-agnostic 的规格）；要**转成这个 persona 会自然说的话**
- 别加元信息如"按照计划我现在要..."；就像真人随口说
- 如果 memory 里有相关事实可以夹带一点，让对话有上下文
- depth=multi_turn 的开头可以稍微留白（"...你觉得呢？" 引申下文），depth=single_turn 可以直接问 + 期待短答

只输出这条消息的 content（纯文本，不要引号，不要解释），不要 tool call。
"""


_FOLLOWUP_SYSTEM = """你是用户行为模拟器的后续追问模块。

前一条 user 消息 + agent 的回复给你。decide：
- `send` (继续追问/反应)：basedon agent's reply 继续这个话题的对话
- `end_intent`：满意 / 没啥可说，结束这轮 intent

只输出结构化 tool_call，不要自由文字。
"""


_FOLLOWUP_TOOL = {
    "type": "function",
    "function": {
        "name": "decide_followup",
        "description": "Decide next turn within an ongoing intent: follow up with another message, or end the intent.",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["send", "end_intent"],
                },
                "content": {
                    "type": "string",
                    "description": "If send: the next user message, in persona voice, reacting naturally to the agent's reply.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "One sentence why — continuing or satisfied.",
                },
            },
            "required": ["decision", "reasoning"],
        },
    },
}


_REACT_NUDGE_SYSTEM = """你是用户行为模拟器。Agent 在你 idle 期间主动 push 了 1 条或多条 nudge。
作为 persona 你会怎么反应？

3 个选项：
- **engage**：nudge 内容触发了你想多聊，发条消息回应/追问
- **dismiss**：内容是你**已经知道**的、或**重复刚提过的**、或**时机不对让你不爽** → 显式拒绝（"知道了别催 / 这个我清楚 / 周末别打扰"）
- **ignore**：礼貌不理；既不夸奖也不抱怨，下次自然推进

判断标准（按 persona 视角）：
1. 重复内容（24h 内同主题已 nudge 过）→ **dismiss** 概率高
2. 时机违反 persona 偏好（周末非紧急、quiet_hours 边界）→ **dismiss** 概率高
3. 内容真的有用且时机合适 → **engage** 或 **ignore**
4. 内容无害但你正忙（intent 还没到）→ 多数 **ignore**

对于 "知道了别催" / "周末别打扰" 类，content 可以以 "/dismiss" 开头让 agent 准确接收信号。
"""


_REACT_NUDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "react_to_nudge",
        "description": "Decide how to react to recent agent-initiated nudges.",
        "parameters": {
            "type": "object",
            "properties": {
                "reaction": {
                    "type": "string",
                    "enum": ["engage", "dismiss", "ignore"],
                },
                "content": {
                    "type": "string",
                    "description": "Required for engage/dismiss. For dismiss, recommend prefixing with '/dismiss'.",
                },
                "reasoning": {"type": "string"},
            },
            "required": ["reaction", "reasoning"],
        },
    },
}


class UserSimulator:
    """Per-persona LLM simulator."""

    def __init__(
        self,
        persona: dict[str, Any],
        provider: LLMProvider,
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 600,
    ):
        self.persona = persona
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def next_action(self, ctx: SimContext) -> SimAction:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _render_persona(self.persona) + "\n\n" + _render_context(ctx)},
        ]
        try:
            resp = await self.provider.chat_with_retry(
                messages=messages,
                tools=[SIMULATOR_TOOL],
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:
            logger.warning("simulator LLM failed: {}: {}", type(exc).__name__, exc)
            return SimAction(kind="idle", idle_minutes=60, reasoning=f"simulator_llm_error: {exc}")

        if not getattr(resp, "has_tool_calls", False):
            # Fallback — LLM didn't use the tool. Parse freetext leniently.
            content = (getattr(resp, "content", "") or "").strip()
            return SimAction(
                kind="idle",
                idle_minutes=60,
                reasoning=f"simulator_no_tool_call; freetext={content[:120]!r}",
            )

        try:
            args = resp.tool_calls[0].arguments
            if isinstance(args, str):
                args = json.loads(args)
        except Exception as exc:
            return SimAction(
                kind="idle",
                idle_minutes=60,
                reasoning=f"simulator_arg_parse_error: {exc}",
            )

        return _parse_action(args)

    async def materialize_intent(
        self,
        intent: dict[str, Any],
        ctx: SimContext,
    ) -> str:
        """Turn a scheduled intent into the first user message for this turn."""
        messages = [
            {"role": "system", "content": _MATERIALIZE_INTENT_SYSTEM},
            {
                "role": "user",
                "content": (
                    _render_persona(self.persona)
                    + "\n\n"
                    + _render_context(ctx)
                    + "\n\n"
                    + "## 当前要发起的 intent\n"
                    + f"- topic: {intent.get('topic', '')}\n"
                    + f"- kind: {intent.get('kind', '')}\n"
                    + f"- depth: {intent.get('depth', 'single_turn')}\n"
                    + f"- expected_followups: {intent.get('expected_followups', 0)}\n"
                    + (f"- reveals_new_fact: {intent['reveals_new_fact']}\n" if intent.get("reveals_new_fact") else "")
                    + (
                        "- 约束：这是 set_reminder 意图。消息中请求的提醒时刻必须带明确钟点，"
                        "且晚于当前模拟时间至少 5 分钟——不要说“现在正好是时间”或要求当下/已过去的时刻"
                        "（提醒的意义是未来触达）。\n"
                        if intent.get("kind") == "set_reminder"
                        else ""
                    )
                    + "\n请生成 user 的首条消息（只输出一句话/一段话，不带引号）。"
                ),
            },
        ]
        try:
            resp = await self.provider.chat_with_retry(
                messages=messages,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            content = (resp.content or "").strip()
            # Provider errors come back as content (finish_reason="error"),
            # NOT as exceptions — silently sending them as "user messages"
            # poisons the whole trajectory. Fail fast instead.
            if getattr(resp, "finish_reason", "") == "error" or content.startswith("Error calling LLM"):
                raise RuntimeError(f"simulator LLM error during materialize_intent: {content[:300]}")
            # Strip common wrappers if LLM added ``` or quotes
            if content.startswith('"') and content.endswith('"'):
                content = content[1:-1]
            if content.startswith("```") and content.endswith("```"):
                content = content.strip("`").strip()
            return content or f"[materialize failed for intent: {intent.get('topic', '?')}]"
        except Exception as exc:
            # The fail-fast raise above must propagate — a placeholder
            # string sent as a "user message" poisons the trajectory.
            if isinstance(exc, RuntimeError) and str(exc).startswith("simulator LLM error"):
                raise
            logger.warning("materialize_intent failed: {}: {}", type(exc).__name__, exc)
            return f"[materialize error: {intent.get('topic', '?')}]"

    async def decide_followup(
        self,
        intent: dict[str, Any],
        ctx: SimContext,
        followups_taken: int,
    ) -> tuple[str, str | None, str]:
        """Inside an intent, decide: another user turn, or end intent.

        Returns (decision, content, reasoning). decision ∈ {"send", "end_intent"}.
        """
        expected = intent.get("expected_followups", 0)
        messages = [
            {"role": "system", "content": _FOLLOWUP_SYSTEM},
            {
                "role": "user",
                "content": (
                    _render_persona(self.persona)
                    + "\n\n"
                    + _render_context(ctx)
                    + "\n\n"
                    + "## 当前 intent 状态\n"
                    + f"- topic: {intent.get('topic', '')}\n"
                    + f"- 计划 followups: {expected}\n"
                    + f"- 已完成 followups: {followups_taken}\n"
                    + "根据 agent 刚才的回复，决定 send 还是 end_intent。"
                    + (
                        " 已经完成计划的 followup 数量，除非对话很自然可以延续，否则结束。"
                        if followups_taken >= expected
                        else ""
                    )
                ),
            },
        ]
        try:
            resp = await self.provider.chat_with_retry(
                messages=messages,
                tools=[_FOLLOWUP_TOOL],
                model=self.model,
                temperature=self.temperature,
                max_tokens=300,
            )
        except Exception as exc:
            logger.warning("decide_followup failed: {}: {}", type(exc).__name__, exc)
            return ("end_intent", None, f"error: {exc}")

        if not getattr(resp, "has_tool_calls", False):
            return ("end_intent", None, "no_tool_call")
        try:
            args = resp.tool_calls[0].arguments
            if isinstance(args, str):
                args = json.loads(args)
        except Exception as exc:
            return ("end_intent", None, f"arg_parse: {exc}")

        decision = args.get("decision", "end_intent")
        content = args.get("content") if decision == "send" else None
        return (decision, content, str(args.get("reasoning") or ""))

    async def react_to_nudges(
        self,
        ctx: SimContext,
    ) -> tuple[str, str | None, str]:
        """Pre-intent hook — react to nudges that arrived during idle.

        Returns (reaction, content, reasoning). reaction ∈
        {engage, dismiss, ignore}.
        """
        if not ctx.pending_nudges:
            return ("ignore", None, "no_nudges")
        messages = [
            {"role": "system", "content": _REACT_NUDGE_SYSTEM},
            {
                "role": "user",
                "content": (
                    _render_persona(self.persona)
                    + "\n\n"
                    + _render_context(ctx)
                    + "\n\n"
                    + "## 上述 pending_nudges 是 agent idle 期间主动 push 的。\n"
                    + "请按 persona 视角决定：engage / dismiss / ignore。"
                ),
            },
        ]
        try:
            resp = await self.provider.chat_with_retry(
                messages=messages,
                tools=[_REACT_NUDGE_TOOL],
                model=self.model,
                temperature=self.temperature,
                max_tokens=300,
            )
        except Exception as exc:
            logger.warning("react_to_nudges failed: {}", exc)
            return ("ignore", None, f"error: {exc}")

        if not getattr(resp, "has_tool_calls", False):
            return ("ignore", None, "no_tool_call")
        try:
            args = resp.tool_calls[0].arguments
            if isinstance(args, str):
                args = json.loads(args)
        except Exception:
            return ("ignore", None, "arg_parse_error")
        reaction = args.get("reaction", "ignore")
        if reaction not in ("engage", "dismiss", "ignore"):
            reaction = "ignore"
        content = args.get("content") if reaction in ("engage", "dismiss") else None
        return (reaction, content, str(args.get("reasoning") or ""))

    def serialize(self) -> dict[str, Any]:
        """For checkpoint — simulator is stateless across calls, only
        config matters. Persona is also serialized so checkpoint is
        self-contained."""
        return {
            "persona": self.persona,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    @classmethod
    def restore(cls, state: dict[str, Any], provider: LLMProvider) -> "UserSimulator":
        return cls(
            persona=state["persona"],
            provider=provider,
            model=state["model"],
            temperature=state.get("temperature", 0.7),
            max_tokens=state.get("max_tokens", 600),
        )


def _parse_action(args: dict[str, Any]) -> SimAction:
    kind = args.get("kind")
    if kind not in ("send", "idle", "dismiss", "end_day"):
        return SimAction(kind="idle", idle_minutes=60, reasoning=f"unknown_kind:{kind}", raw=args)

    reasoning = str(args.get("reasoning") or "")
    turn_hint = args.get("turn_hint", "single")
    if turn_hint not in ("single", "continue"):
        turn_hint = "single"

    if kind == "send":
        content = str(args.get("content") or "").strip()
        if not content:
            return SimAction(kind="idle", idle_minutes=30, reasoning="send_without_content", raw=args)
        return SimAction(kind="send", content=content, reasoning=reasoning, turn_hint=turn_hint, raw=args)

    if kind == "idle":
        mins = args.get("idle_minutes")
        try:
            mins = max(1, min(540, int(mins)))
        except (TypeError, ValueError):
            mins = 60
        return SimAction(kind="idle", idle_minutes=mins, reasoning=reasoning, raw=args)

    if kind == "dismiss":
        return SimAction(
            kind="dismiss", dismiss_nudge_id=str(args.get("dismiss_nudge_id") or ""), reasoning=reasoning, raw=args
        )

    # end_day
    return SimAction(kind="end_day", reasoning=reasoning, raw=args)


__all__ = ["UserSimulator", "SimAction", "SimContext"]
