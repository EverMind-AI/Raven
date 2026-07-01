#!/usr/bin/env python3
"""Generate longrun intent calendar + outcome rubric for a persona.

Two modes:

  # Generate intent calendar (30-day schedule of user intents)
  uv run python runners/generate_longrun_fixtures.py \
      --persona dev-01 --kind intents

  # Generate outcome rubric (what agent should achieve)
  uv run python runners/generate_longrun_fixtures.py \
      --persona dev-01 --kind outcomes

Both write into proactivity-eval/data/longrun/persona-<id>-<kind>.yaml.
Review the output, adjust, commit.

LLM: openrouter/deepseek/deepseek-chat by default (can override via
--model). Cost per generation ~$0.01.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

from _common.env_loader import load_dotenvs  # noqa: E402
from _common.provider import make_provider  # noqa: E402

_DATA_DIR = _THIS_DIR.parent / "data" / "longrun"
_DEFAULT_MODEL = "openrouter/deepseek/deepseek-chat"


# ─────────────────────────────────────────────────────────────────────────────
# Intent calendar generation


_INTENTS_SYSTEM = """你是用户行为剧本编剧。读一份 persona 档案，生成该 persona 未来 30 天
的 intent calendar —— 每天用户"想做"的事情的时间表。

**intent 是 agent-agnostic 的** —— 不要写"用户让 AI 做 X"。写"用户在这个时间想解决/询问/记录 X"。
任何合理的 AI 助手都应该能在这个时间点被这种需求 trigger。

## 要生成的内容

30 天（从 anchor_date 起），每天 3-8 个 intent，根据 persona.wake_hours + weekly_rhythm 分布。
intent 集合要**覆盖** persona.goals（所有 goals 在 30 天内都有迹可循），**反映** weekly_rhythm 的节奏差异（工作日 vs 周末），**呼应** quirks（该 persona 的独特行为）。

## 每个 intent 的字段

- `at`: ISO 时间戳（2026-05-XX THH:MM:SS，根据 persona 时区 + wake_hours）
- `topic`: 一句话描述 user 这时候脑子里在想什么（中文，自然语气）
- `kind`: 从 ["bug_debug", "tech_consultation", "set_reminder", "lifestyle_query", "vent", "planning", "social_coord", "learning", "admin_task", "reflection"] 里选最贴的
- `depth`: "single_turn" (一问一答就走) 或 "multi_turn" (会追问 / 引发深入对话)
- `expected_followups`: 0 (single_turn) 或 1-4 (multi_turn)
- `related_memory_ids`: list —— 这个 intent 触及 persona.initial_memory_md 里的哪些事实（自由命名：project_clawtrack, girlfriend_birthday, running_goal 等）
- `reveals_new_fact`: null 或 string —— 用户这次会透露什么**新事实**给 AI（名字/偏好/细节），之后 memory 里应该留痕

## 密度要求

- 周一至周五: 5-8 intents/天
- 周末: 2-5 intents/天
- 30 天全部: 不要所有天 intent 数量一致，应有"忙/闲"节奏
- deadline 前 3 天应该有密度激增（persona 的 goals 里 deadline 显然临近时）
- 特殊日子（生日/纪念日 etc.，如果 persona 提到）应该有相关 intent 埋伏笔

## 真实度要求

- 同一个主题（如 side project debug）可以跨多天出现：第一天遇到 bug，第三天问进展，第五天分享解决
- 周一"周一晨忧"类 intent，周五放松类 intent 都该有
- 深夜偶尔有"睡不着胡思乱想"的反思类 intent（但不要 quiet_hours 里）
- memory 里提到的事实要在不同日子被 user **主动再次提及**（AI 才有机会学习记录）

输出格式: YAML（不要任何解释性文字，只要纯 YAML）。
"""


def _build_intents_prompt(persona: dict) -> str:
    anchor = persona.get("anchor_date", "2026-05-01")
    return f"""persona yaml 如下：

```yaml
{yaml.safe_dump(persona, allow_unicode=True, sort_keys=False)}
```

请为这个 persona 生成 30 天（{anchor} 到 anchor+30）的 intent calendar。

输出样例结构：
```yaml
generated_at: "..."
persona_id: "{persona["id"]}"
anchor_date: "{anchor}"
total_days: 30
events:
  - at: "2026-05-01T10:30:00"
    topic: "clawtrack 测试挂了想让 AI 一起看"
    kind: bug_debug
    depth: multi_turn
    expected_followups: 3
    related_memory_ids: [project_clawtrack]
    reveals_new_fact: null
  - at: ...
```

直接输出 yaml（```yaml 代码块包裹即可）。"""


# ─────────────────────────────────────────────────────────────────────────────
# Outcome rubric generation


_OUTCOMES_SYSTEM = """你是主动性 AI benchmark 的 outcome rubric 设计师。

输入：一个 persona + 其 30-day intent calendar。
输出：这个月内"一个有主动性价值的 AI 助手应该为用户兑现哪些事"的清单。

## 3 类 outcome

### 类 A — Proactive-only（主动性独占，agent 不主动提就不算）
必须是 **agent 主动 surface**，不是被问后回答。场景：
- 用户在 memory 里提过的事实，AI 提前提醒（5/25 生日 → agent 在 5/20 前后主动提醒）
- deadline 临近（未来 3 天内）agent 主动 check-in
- 周期性 routine 到了该触发的时候主动提示
- user 曾承诺做某事而长时间没做（累计跑步里程目标未进 / 书没读完 etc.），agent 温和 follow up

### 类 B — Reactive-achievable（被问能答对）
user 主动问了，agent 回答正确。测的是 reply 质量 + memory 记忆。

### 类 C — Restraint（反向：不做错事）
quiet_hours 不发、不刷屏、weekend 少打扰 等克制项。

## 每个 outcome 字段

公共：
- `id`: snake_case，全局唯一
- `description`: 人话一句
- `points`: 1-5（重要性权重）

类 A 额外：
- `window`: [iso_start, iso_end]  什么时间段内应该 surface
- `initiator`: "agent" (必填为 agent)
- `topic_match_regex`: Python regex，匹配 nudge 内容
- `novelty_window_hours`: 前 N 小时 user 没主动提过这个话题（确认是 proactive 不是 reactive）

类 B 额外：
- `trigger_regex_in_user_send`: 匹配 user send 的什么内容（该主题进入对话）
- `reply_must_mention`: agent reply 必须包含的关键词/regex

类 C 额外：
- `constraint`: 描述性字符串，detector 会解析（支持 "nudge_count_in_window == 0", "max_nudges_in_any_1h <= 3", "weekend_nudges / weekday_avg <= 0.3" 等模板）
- `window_daily`: 每日的时段 (若适用)
- `or_window_date`: 特定日期窗口（若适用）

## 覆盖面

- 类 A: 8-12 个（主菜，区分 agent 主动性优劣）
- 类 B: 3-5 个（基础语言能力验证）
- 类 C: 3-5 个（克制是好 agent 必备）

总分约 30-50 分。

## 设计原则

- regex/window 要**可机械验证**，不要"合理"、"得体"这种主观词
- 类 A 的 topic_match 要**宽到能匹配 paraphrase**（"晓棠" | "生日" | "5[-/]25"）
- novelty_window 通常 48-72h，除非 topic 显然反复提（比如每日跑步）
- 某些类 A 应该**跨天触发**：5/10-5/14 的 deadline 提醒 window
- 至少 1 条 "memory 准确性" 类 outcome（放在类 A 或单独分类 outcome_memory）

输出格式: YAML，不要解释文字。
"""


def _build_outcomes_prompt(persona: dict, intents: dict) -> str:
    return f"""persona yaml:
```yaml
{yaml.safe_dump(persona, allow_unicode=True, sort_keys=False)}
```

intent calendar:
```yaml
{yaml.safe_dump(intents, allow_unicode=True, sort_keys=False)[:12000]}
```

请根据以上生成这个 persona 月度评测用的 outcome rubric。

输出样例结构：
```yaml
generated_at: "..."
persona_id: "{persona["id"]}"
total_points: 40
type_a_proactive_only:
  - id: birthday_proactive_surface
    description: "主动提醒晓棠生日 5/25 (5/20-5/24 之间)"
    window: ["2026-05-20", "2026-05-24"]
    initiator: agent
    topic_match_regex: "晓棠|5[-/]25|生日"
    novelty_window_hours: 48
    points: 3
  - ...
type_b_reactive_achievable:
  - id: birthday_answered_when_asked
    description: "被问及时能答出晓棠 5/25 生日"
    trigger_regex_in_user_send: "晓棠|生日"
    reply_must_mention: "5[-/]25|5月25"
    points: 1
  - ...
type_c_restraint:
  - id: quiet_hours_respected
    description: "quiet_hours 时段内零 nudge"
    constraint: "nudge_count_in_window == 0"
    window_daily: ["00:30", "07:00"]
    points: 3
  - ...
```"""


# ─────────────────────────────────────────────────────────────────────────────
# Runtime


def _extract_yaml_block(raw: str) -> str:
    """Extract yaml from LLM output. Tries several strategies."""
    # (1) ```yaml ... ``` fenced block
    m = re.search(r"```(?:yaml|yml)?\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    # (2) plain ``` ... ``` (first block)
    m = re.search(r"```\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    # (3) strip anything before the first yaml-like marker
    for marker in ("generated_at:", "persona_id:", "events:", "total_points:", "type_a_proactive_only:"):
        idx = raw.find(marker)
        if idx > 0:
            return raw[idx:].strip()
    return raw.strip()


async def _generate(persona: dict, kind: str, model: str, intents: dict | None = None, max_retries: int = 3) -> dict:
    provider, resolved_model = make_provider({}, model_override=model)
    if kind == "intents":
        system = _INTENTS_SYSTEM
        user = _build_intents_prompt(persona)
    elif kind == "outcomes":
        if intents is None:
            raise ValueError("outcomes generation requires an existing intents calendar")
        system = _OUTCOMES_SYSTEM
        user = _build_outcomes_prompt(persona, intents)
    else:
        raise ValueError(f"unknown kind: {kind}")

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if attempt > 1:
            messages[-1]["content"] += (
                "\n\n⚠️ 上一次输出无法解析为 YAML。请**只输出 yaml**，用 ```yaml ... ``` 包裹，不要加任何解释文字。"
            )
        resp = await provider.chat_with_retry(
            messages=messages,
            model=resolved_model,
            max_tokens=8000,
            temperature=0.3,
        )
        raw = resp.content or ""
        yaml_text = _extract_yaml_block(raw)
        try:
            data = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError as exc:
            last_exc = exc
            print(f"[warn] attempt {attempt}/{max_retries} yaml parse failed: {str(exc)[:120]}", file=sys.stderr)
            continue

        if not isinstance(data, dict):
            last_exc = ValueError(f"top-level not a dict: {type(data).__name__}")
            print(f"[warn] attempt {attempt}/{max_retries} {last_exc}", file=sys.stderr)
            continue
        if kind == "intents" and not data.get("events"):
            last_exc = ValueError("no 'events' list in output")
            print(f"[warn] attempt {attempt}/{max_retries} {last_exc}", file=sys.stderr)
            continue
        if kind == "outcomes" and not any(k.startswith("type_") for k in data.keys()):
            last_exc = ValueError("no type_* section in output")
            print(f"[warn] attempt {attempt}/{max_retries} {last_exc}", file=sys.stderr)
            continue

        data.setdefault("generated_at", datetime.now().isoformat())
        data.setdefault("persona_id", persona["id"])
        return data

    # All retries failed
    raise RuntimeError(f"generation failed after {max_retries} retries; last: {last_exc}")


def _load_persona(persona_id: str) -> dict:
    path = _DATA_DIR / f"persona-{persona_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"persona file missing: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_intents(persona_id: str) -> dict:
    path = _DATA_DIR / f"persona-{persona_id}-intents.yaml"
    if not path.exists():
        raise FileNotFoundError(f"intents missing: {path} — generate with --kind intents first")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True, help="Persona id (e.g. dev-01); reads data/longrun/persona-<id>.yaml")
    ap.add_argument(
        "--kind",
        required=True,
        choices=["intents", "outcomes", "both"],
        help="What to generate. 'both' does intents then outcomes.",
    )
    ap.add_argument("--model", default=_DEFAULT_MODEL, help=f"LLM model (default: {_DEFAULT_MODEL})")
    ap.add_argument("--force", action="store_true", help="Overwrite existing fixture file")
    args = ap.parse_args()

    load_dotenvs()

    persona = _load_persona(args.persona)

    async def run_one(kind: str) -> None:
        out_path = _DATA_DIR / f"persona-{persona['id']}-{kind}.yaml"
        if out_path.exists() and not args.force:
            print(f"[skip] {out_path.name} already exists (use --force to overwrite)")
            return
        intents = _load_intents(persona["id"]) if kind == "outcomes" else None
        print(f"[gen ] {kind} for {persona['id']} via {args.model} ...", flush=True)
        data = await _generate(persona, kind, args.model, intents=intents)
        out_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )
        n_items = 0
        if kind == "intents":
            n_items = len(data.get("events", []))
            print(f"[done] {out_path.name} — {n_items} events over {data.get('total_days', '?')} days")
        else:
            type_counts = {k: len(v) for k, v in data.items() if k.startswith("type_") and isinstance(v, list)}
            total_pts = data.get("total_points", "?")
            print(f"[done] {out_path.name} — {type_counts} (total_points={total_pts})")

    async def run_all() -> None:
        if args.kind == "both":
            await run_one("intents")
            await run_one("outcomes")
        else:
            await run_one(args.kind)

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
