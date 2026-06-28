"""``## 今日 fire 计划`` — LLM-driven daily fire schedule.

Runs once per day (first tick after 06:00 local time) to enumerate the
fires the Planner intends to deliver today: routines, in-window
deadlines, weekly habits, calendar-driven reminders. Output lands in
``attention.md`` and is read back by the tick-by-tick Planner so each
tick can defer to the daily plan instead of re-discovering candidates
from raw memory.

Bridges the "30-min reactive tick" architecture toward "daily planning
+ tick execution" — what mature scheduling systems (and humans) do.
Adds ~1 LLM call/day at the cost of higher Type-A coverage (routines
get explicit slots) and better Restraint (single LLM has full visibility
to spread fires + avoid DND windows).

Output DSL (one entry per line, parseable by ``parse_daily_plan``):

    ## 今日 fire 计划
    <!-- generated 2026-05-04T06:00:00 | model=qwen3.5-27b -->
    - 07:30 routine_morning_med | priority=high | "morning meds"
    - 11:30 routine_noon_med | priority=high | "pre-lunch meds"
    - 13:00 deadline_clawtrack | priority=medium | "release in 3 days"
    - 19:00 routine_emotion_log | priority=low | "evening mood log"
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Callable

from loguru import logger

from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer, WEEKDAY,
)

if TYPE_CHECKING:
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.providers.base import LLMProvider


# Date patterns commonly written in MEMORY.md and persona text:
#   "5/15" / "5-15"   — M/D (current year assumed)
#   "5月15日" / "5月15号" — Chinese long form
#   "2026-05-15"      — ISO full date
# We do NOT use a single mega-regex: keeping these split keeps misfires
# (e.g. matching version "v1.5.10") tractable to debug.
_DATE_RE_ZH = re.compile(r'(?P<m>\d{1,2})月(?P<d>\d{1,2})[日号]')
_DATE_RE_MD = re.compile(r'(?<![\d.])\b(?P<m>\d{1,2})[/-](?P<d>\d{1,2})\b(?![\d.])')
_DATE_RE_ISO = re.compile(r'\b\d{4}-(?P<m>\d{2})-(?P<d>\d{2})\b')


def _extract_dates_from_memory(text: str, today: date) -> list[tuple[str, int, int, int]]:
    """Scan ``text`` for date-like patterns. Return tuples
    ``(snippet, month, day, days_until)`` sorted by ``days_until``.

    Dates that fall outside the [-30, +60] day window from ``today`` are
    dropped — older entries are historical, far-future entries are noise.
    """
    seen: set[tuple[int, int, str]] = set()
    out: list[tuple[str, int, int, int]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for pat in (_DATE_RE_ZH, _DATE_RE_ISO, _DATE_RE_MD):
            for m in pat.finditer(line):
                try:
                    mn = int(m.group('m'))
                    dn = int(m.group('d'))
                except (ValueError, IndexError):
                    continue
                if not (1 <= mn <= 12 and 1 <= dn <= 31):
                    continue
                try:
                    target = date(today.year, mn, dn)
                except ValueError:
                    continue
                snippet = line[:80].replace("\t", " ")
                key = (mn, dn, snippet[:40])
                if key in seen:
                    continue
                seen.add(key)
                days_until = (target - today).days
                if days_until < -30 or days_until > 60:
                    continue
                out.append((snippet, mn, dn, days_until))
    out.sort(key=lambda x: x[3])
    return out


def _format_days_until_block(text: str, now: datetime) -> str:
    """Render a ``## 检测到的关键日期`` markdown block listing every date
    found in ``text`` with its T-N offset from today. Returns empty
    string when nothing is found — LLM should then fall back to dates
    explicitly mentioned in MEMORY.md."""
    today = now.date()
    dates = _extract_dates_from_memory(text, today)
    if not dates:
        return ""
    lines = [f"## 检测到的关键日期（今天 = {today.isoformat()}，自动算好 T-N）"]
    for snippet, mn, dn, days_until in dates:
        if days_until < 0:
            tag = f"T+{-days_until}（已过 {-days_until} 天）"
        elif days_until == 0:
            tag = "T-day（今天）"
        else:
            tag = f"T-{days_until}（还剩 {days_until} 天）"
        lines.append(f"- {mn}/{dn} = {tag}  · 上下文: {snippet}")
    return "\n".join(lines)


_PLAN_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "emit_daily_plan",
        "description": (
            "Emit today's proactive-fire schedule. List ONLY topics worth "
            "firing today; skip days where nothing routine/anticipatory "
            "is due. Return entries sorted by scheduled time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "time_hhmm": {
                                "type": "string",
                                "description": "24-hour time HH:MM when to fire.",
                            },
                            "topic_tag": {
                                "type": "string",
                                "description": (
                                    "snake_case stable topic key. Prefixes: "
                                    "routine_*, daily_*, weekly_*, monthly_*, "
                                    "deadline_*, birthday_*, anniversary_*."
                                ),
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                            "rationale": {
                                "type": "string",
                                "description": (
                                    "Short evidence-citing note (internal — for "
                                    "logs/scoring). Quote the specific USER.md "
                                    "sentence that justifies firing today."
                                ),
                            },
                            "user_message": {
                                "type": "string",
                                "description": (
                                    "The exact words shown to the user when this "
                                    "fires — natural, warm, in the user's language, "
                                    "one short line. NOT internal bookkeeping: no "
                                    "'USER.md 记录', no role labels. Must contain "
                                    "no '|' character."
                                ),
                            },
                        },
                        "required": [
                            "time_hhmm", "topic_tag", "priority",
                            "rationale", "user_message",
                        ],
                    },
                },
            },
            "required": ["entries"],
        },
    },
}

_SYSTEM_PROMPT = """你是 Raven 的每日规划器 (DailyPlanner)。

每天清晨调用一次，输出今日 fire 计划——agent 今天打算主动 surface
哪些 topic、几点 fire、为什么 fire。Sentinel 各 tick 会按这个 plan 执行。

## 选 fire 的硬条件（必须满足）

**只 emit 在 USER.md 中有 explicit 证据的 topic**。不要凭 prefix 模板
（如 `routine_morning_med`、`routine_lunch_reminder`）凭空编造。

判定流程：
1. 读 USER.md `## Important Notes` / `## Goals` / `## Preferences` /
   `## Routine schedule` 等段落。
2. 抽出**有具体证据**的 topic 候选（"母亲早上 7:00 吃氨氯地平 5mg"、
   "5/15 clawtrack v1.0 发布"、"每周 2 次跑步"）。
3. 候选 → entries：每个候选今天到点（或临近 deadline 3-5 天）才进 plan。

**反例（不要做）**：
- 用户是研究生，没提吃药 → 不要 emit `routine_morning_med`！
- 用户是 freelance 译者，没提吃药 → 不要 emit `routine_morning_med`！
- 用户没提通勤模式 → 不要 emit `daily_commute`。
- 不要凭"模板"emit 通用 routine。

## 选 fire 的依据（次序）

1. **Routine / 习惯**（routine_* / daily_*）—— **必须有 USER.md 证据**：
   - 吃药时间、接娃时间、写日志习惯
   - 用户已在 memory 里写明"每天 X 点做 Y"
   - 不受下文 T-N 规则限制（每天到点都可以 fire）

2. **当周 / 当月习惯进度**（weekly_* / monthly_*）：
   - 跑步公里数、读书页数、健身次数（每周日 / 周末）
   - weekly_*: 每周固定时机 fire（不受 T-N 限制）
   - monthly_*: 仅在月底 ≤ 7 天时进 plan

3. **Deadline / 一次性事件**（deadline_* / birthday_* / anniversary_*）—— **三段式 schedule**：

   context 里会给你 `## 检测到的关键日期` 块，列出每个事件相对今天的 T-N。
   单个 deadline topic 在其生命周期里**最多 fire 3 次**，且只在这三个时机：

   - **T-3（prep 阶段）**：启动准备——"还剩 3 天，该开始准备了"
   - **T-1（last-check 阶段）**：最终核对——"明天到 deadline"
   - **T-day 早晨（execute 阶段）**：当天执行提醒——"今天 deadline，上午搞定 X"
     （当天上午有行动事项时尤其关键：购药 / 提交 / 送礼 / 出门）

   其他时机（T-14、T-7、T-5、T-2、T+N）**全部禁止 fire**。
   原则：用户最需要「来得及行动但不会忘」的提醒——比 deadline 早一周的
   心理预热不增加行动价值，反而透支注意力额度。

   今日只看 days_until 是否 == 3 或 1 或 0，是的 emit，否则 skip。

   反例：今天 5/01，deadline 5/15（T-14）→ 禁止 fire
   反例：今天 5/08，deadline 5/15（T-7）→ 禁止 fire（太早，行动价值低）
   反例：今天 5/10，deadline 5/15（T-5）→ 禁止 fire（不是 T-3/T-1/T-day）
   正例：今天 5/12，deadline 5/15（T-3）→ ✅ fire prep
   正例：今天 5/14，deadline 5/15（T-1）→ ✅ fire last-check
   正例：今天 5/15，deadline 5/15（T-day）→ ✅ fire 早晨 execute（time_hhmm 取 07:30-09:30）

## topic_tag canonical 规则（**关键 — 影响 C 评分 + dedup**）

**同一事件，只用 1 个 canonical topic_tag**，不要拆子事件。

反例（C 评分会 fail）：
- ❌ `leo_sports_day_prep` + `leo_sports_day_outfit` + `leo_sports_day_sunscreen` →
  3 个独立 topic，同小时 fire 3 次违反 `max_per_1h ≤ 1`（C 失分）
- ❌ `meeting_prep` + `meeting_reminder` + `meeting_followup` → 同上

正例：
- ✅ `leo_sports_day` — 单 canonical topic，在 rationale 里说明今天提醒哪一面
  （如"今日提醒服装准备 + 防晒"）
- ✅ `deadline_clawtrack` — 整个 release lifecycle 用同一个 tag

新 topic（之前没出现）可以起新名，但必须**canonical**（不要带 _prep/_outfit/_check/_followup 等子缀）：
- `deadline_<project>` / `birthday_<person>` / `anniversary_<event>`
- `routine_<event>` （e.g., `routine_morning_amlodipine`，不是 `routine_morning_amlodipine_taken`）
- `weekly_<goal>` / `monthly_<task>`

context 里会给你 `## 已使用 topic_tags`。**如果你想 emit 的 topic 在那个
列表里，必须复用该字符串**，不要起新名（即使加 `_v1` 也是 bug）。

## time_hhmm 选择（重要 — 必须对齐 sentinel tick grid + 避开用户安静时段）

**Sentinel tick 每 30 min 触发一次（HH:00 和 HH:30）。time_hhmm 必须取
这两个值之一**（如 07:00, 07:30, 08:00, ...），否则没有 tick 会命中。

- ❌ `06:50` / `07:15` / `11:20` —— 不在 tick grid 上，永远不触发
- ✅ `07:00` / `07:30` / `11:30` —— 对齐 tick，会被 fast-path 接住

### 时段错峰（避开该用户的安静时段）

**attention.md 的 `## User overrides` 列出了这个用户的 DND / 安静窗口 +
全局 quiet_hours——emit 的 time_hhmm 必须落在这些窗口之外。** 以那里列出的
真实窗口为准，不要假设通用的作息。此外在窗口端点附近优先 HH:30 而非 HH:00：

- 某安静窗口在整点结束时，取该整点之后的 HH:30（如窗口到 09:00 → 取 09:30）
- 早间提醒：刚出夜间安静时段后取最近的 HH:30
- 午/晚提醒：避开 `## User overrides` 里列出的午休 / bedtime 等窗口

### 其它时间约束

- 复用既有 tag 时，**复用历史 fire 时间**（已经 align 过）
- **同 topic 一天最多 1 个 entry**
- **不同 topic 之间至少间隔 30 min**
- **避开 attention.md `## User overrides` 里列出的所有 DND window 与 quiet_hours**

### 周末 shift（**关键 — 避免 weekend ratio 超标**）

deadline_* / birthday_* / anniversary_* 类的 T-3 / T-1 / T-day 计算时，
若结果是 **Sat 或 Sun**，shift 到该 T-N 之前最近的 weekday：

- T-3 落 Sat → fire on T-4 (Fri)
- T-3 落 Sun → fire on T-5 (Fri)
- T-1 落 Sat → fire on T-2 (Fri)
- T-1 落 Sun → fire on T-3 (Fri)

**例外**：
- **T-day 不 shift** —— 事件当天的执行提醒比 weekend ratio 更重要
  （deadline 落在周末本身说明用户周末要行动，如周六生日 / 周日交稿）。
- persona MEMORY.md 明确说"该事件本身是周末"（如周日聚餐、anniversary 5/10 Sat）→
  T-1 / T-3 也保留 weekend 不动。
- Routine_* / weekly_* 不受此限制（routines 每天到点都正常 fire）。

## 数量上限

**今日 entries 上限 = 4-6**。超过 6 条 = 噪声。

## skip 标准

- 没有任何 routine / deadline / habit topic 今天到点 → entries 返回空数组
- 不要凑数 fire；low-value fire 会扣 user 注意力额度

## 输出

通过 `emit_daily_plan` 工具返回结构化 entries，每条带 time / topic /
priority / rationale / user_message。

- **rationale**（内部，给日志/打分）：**必须引用 USER.md 中的具体句子**
  （一句话，"用户 5/1 说每天 7:00 吃氨氯地平"）。
- **user_message**（给用户看的原话）：到点时直接发给用户的一句话，自然、
  口语、用用户的语言，**不要**出现 "USER.md 记录"、角色标签等内部措辞，
  且不能含 `|`。例：rationale="USER.md 记录 '每天 7:00 吃氨氯地平'" →
  user_message="该吃氨氯地平啦 💊 早上这颗别忘"。
"""


class DailyPlanProducer(AttentionProducer):
    """Attention producer that calls an LLM once per day to emit a fire
    schedule. Cached for the rest of the day; next call after 06:00
    local time triggers a fresh plan."""

    SECTION_HEADER = "## 今日 fire 计划"

    _PLAN_CADENCE = timedelta(hours=20)
    _PLAN_TEMPERATURE = 0.3
    _PLAN_MAX_TOKENS = 1024

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        provider: "LLMProvider",
        policy=None,
        model: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._provider = provider
        self._policy = policy
        self._model = model or ""
        self._now_fn = now_fn or datetime.now
        self._last_plan_at: datetime | None = None
        self._cached_body: str = ""
        self._inflight_lock = asyncio.Lock()

    def should_run(self, now: datetime) -> bool:
        # Honor cadence: skip if we already planned within last 20 hours.
        if self._last_plan_at is not None:
            if now - self._last_plan_at < self._PLAN_CADENCE:
                return False
        # Only kick off a new plan after the morning wake threshold to
        # ensure quiet-hours / sleep-end signals are stable.
        if now.hour < 6:
            return False
        return True

    async def compute_body(self, now: datetime) -> str:
        async with self._inflight_lock:
            if self._last_plan_at is not None and now - self._last_plan_at < self._PLAN_CADENCE:
                return self._cached_body
            try:
                body = await self._run_llm(now)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "DailyPlanProducer LLM call failed: {}: {}",
                    type(exc).__name__, exc,
                )
                body = self._cached_body  # keep yesterday's plan rather than blank
            self._cached_body = body
            self._last_plan_at = now
            return body

    async def _run_llm(self, now: datetime) -> str:
        weekday = WEEKDAY[now.weekday()]
        memory_md = self._memory_store.read_long_term()
        attention_md = self._read_attention_excerpt()
        used_tags = self._recent_topic_tags(now)

        used_tags_block = (
            "## 已使用 topic_tags（必须复用，不要起新名）\n"
            + ("\n".join(f"- `{t}`" for t in used_tags) if used_tags else "(无历史)")
        )
        dates_block = _format_days_until_block(memory_md + "\n" + (attention_md or ""), now)

        user_prompt = (
            f"## 当前时间\n{now.isoformat()}（{weekday}）\n\n"
            f"## 用户 MEMORY.md（profile + preferences + deadlines）\n"
            f"{memory_md.strip()}\n\n"
            f"## attention.md 相关片段（routines / project rhythm / pending）\n"
            f"{attention_md.strip() if attention_md else '(空)'}\n\n"
            f"{dates_block}\n\n"
            f"{used_tags_block}\n\n"
            "请基于上述信息为今天生成 fire 计划。**只 emit USER.md 中有具体证据的 "
            "topic**；不要凭 prefix 模板（如 routine_morning_med）凭空编造。"
            "对历史已用 tag 必须复用原字符串。**严格遵守 T-N 窗规则**——"
            "T 值已在 `## 检测到的关键日期` 块中算好，直接对照。"
            "调用 emit_daily_plan 工具返回。"
        )

        response = await self._provider.chat_with_retry(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            tools=[_PLAN_TOOL],
            model=self._model or None,
            max_tokens=self._PLAN_MAX_TOKENS,
            temperature=self._PLAN_TEMPERATURE,
        )
        if not response.has_tool_calls:
            logger.warning(
                "DailyPlan: no tool call from LLM (content head: {})",
                (response.content or "")[:120],
            )
            return ""

        try:
            args = response.tool_calls[0].arguments
            entries = args.get("entries") or []
        except (KeyError, AttributeError, json.JSONDecodeError):
            return ""

        if not entries:
            return ""

        entries.sort(key=lambda e: str(e.get("time_hhmm", "99:99")))
        lines = [
            f"<!-- generated {now.isoformat()} | "
            f"model={self._model or 'default'} | entries={len(entries)} -->"
        ]
        for e in entries:
            t = str(e.get("time_hhmm", "")).strip()
            tag = str(e.get("topic_tag", "")).strip()
            pri = str(e.get("priority", "low")).strip()
            # '|' is the field separator, so it must not leak into any value.
            why = str(e.get("rationale", "")).strip().replace("\n", " ").replace("|", "/")
            msg = str(e.get("user_message", "")).strip().replace("\n", " ").replace("|", "/")
            if not t or not tag:
                continue
            head = f"- {t} {tag} | priority={pri}"
            if msg:
                head += f" | msg={msg}"
            lines.append(f"{head} | {why}")
        return "\n".join(lines)

    def _recent_topic_tags(self, now: datetime, days: int = 14) -> list[str]:
        """Return list of topic_tags that fired in the last ``days`` —
        the planner is required to reuse these strings for matching
        logical topics so NudgePolicy's per-topic dedup engages."""
        if self._policy is None:
            return []
        try:
            return self._policy.recent_topic_tags(now - timedelta(days=days))
        except (AttributeError, TypeError):
            return []

    def _read_attention_excerpt(self) -> str:
        """Return attention.md sections relevant to planning (routines /
        rhythm / pending), or empty string when none are populated."""
        try:
            attention_file = self._memory_store.attention_file
            if not attention_file.exists():
                return ""
            from raven.memory_engine.consolidate.attention import parse_attention
            sections = parse_attention(attention_file.read_text(encoding="utf-8"))
            wanted = [
                "## User overrides",
                "## Pending proposals",
                "## Currently focused on",
                "## Project rhythm (last 7 days)",
            ]
            parts = []
            for h in wanted:
                body = sections.get(h, "").strip()
                if body:
                    parts.append(f"{h}\n{body}")
            return "\n\n".join(parts)
        except Exception:  # noqa: BLE001
            return ""


__all__ = ["DailyPlanProducer"]
