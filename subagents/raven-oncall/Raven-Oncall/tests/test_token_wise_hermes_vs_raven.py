"""Head-to-head: Hermes ``system_and_3`` vs Raven ``CacheOptimizer``.

Three real-world-representative scenarios driven through
``AgentLoop.run_turn``:

    S1  Pure conversation (no tools)
        8 turns, medium system, history grows organically.
        Tests cross-turn prefix caching.

    S2  Intra-turn tool chain
        3 turns, each turn forces a 3-step sequential tool chain
        (tool_alpha → tool_beta → tool_gamma → final answer = 4 LLM calls).
        Tests whether the strategy caches the growing intra-turn prefix.

    S3  Mixed — multi-turn with one tool call per turn
        6 turns, each turn calls one tool (2 LLM calls per turn).
        Tests the common agent workload (every turn does one tool lookup).

Variants per scenario:
    V1  baseline         — no cache_control
    V2  Raven current — tools(bp1) + system(bp2) + before-user(bp3) + mid(bp4)
    V3  Hermes faithful  — system(bp1) + last-3-messages(bp2–4)

Report: ``raven/token_wise/EXPERIMENT_REPORT_HERMES_VS_RAVEN.md``

Skipped when ``raven/key.env`` is missing.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.tools.base import Tool
from raven.providers.litellm_provider import LiteLLMProvider
from raven.token_wise.cache_optimizer import CacheOptimizer
from raven.token_wise.registry import StrategyRegistry
from raven.token_wise.system_and_tail_cache import SystemAndTailCacheStrategy
from raven.token_wise.usage_tracker import UsageTracker

KEY_FILE = Path(__file__).resolve().parent.parent / "raven" / "key.env"
REPORT_PATH = Path(__file__).resolve().parent.parent / "raven" / "token_wise" / "EXPERIMENT_REPORT_HERMES_VS_RAVEN.md"
MODEL = "anthropic/claude-sonnet-4-5"
COST_GUARD_USD = 2.00
_OPENROUTER_PIN = {"provider": {"order": ["Anthropic"], "allow_fallbacks": False}}


def _load_openrouter_key() -> str | None:
    if not KEY_FILE.exists():
        return None
    for raw in KEY_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            return line
    return None


@pytest.fixture(scope="module")
def api_key() -> str:
    key = _load_openrouter_key()
    if not key or not key.startswith("sk-or-"):
        pytest.skip("OpenRouter key not available at raven/key.env")
    return key


# ---------------------------------------------------------------------------
# Workspace seeding
# ---------------------------------------------------------------------------


def _medium_soul() -> str:
    return (
        "# Soul\n\nI am Raven, a careful assistant.\n\n"
        "## Working principles\n\n"
        "- Be precise about what you know vs what you assume.\n"
        "- Prefer direct answers over hedged ones.\n"
        "- When asked about prior context, refer to it accurately.\n"
        "- Ground every claim in the conversation history when relevant.\n"
        "- Never repeat what the user just said back to them.\n\n"
        "## Output protocol\n\n"
        "Reply in one short sentence or one word as requested. Do not preamble.\n"
    )


def _tool_chain_soul() -> str:
    return (
        "# Soul\n\nI am Raven, a tool-using assistant.\n\n"
        "## Tool-chain protocol\n\n"
        "When the user says 'investigate item_XX':\n"
        "1. Call tool_alpha with item_id=item_XX\n"
        "2. Then call tool_beta with item_id=item_XX\n"
        "3. Then call tool_gamma with item_id=item_XX\n"
        "4. After all three results, reply with one sentence summarizing.\n\n"
        "IMPORTANT: Call exactly ONE tool per response. NEVER call multiple "
        "tools in one response. NEVER skip a tool. NEVER re-call a tool.\n"
    )


def _single_tool_soul() -> str:
    return (
        "# Soul\n\nI am Raven, a tool-using assistant.\n\n"
        "## Tool protocol\n\n"
        "When the user gives an item id, immediately call data_lookup with "
        "that id. After receiving the result, reply with one short sentence "
        "summarizing what was retrieved. Be terse. Never repeat raw output.\n"
    )


def _seed_workspace(workspace: Path, soul: str) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "SOUL.md").write_text(soul, encoding="utf-8")
    (workspace / "AGENTS.md").write_text("# Agent\nTerse.\n", encoding="utf-8")
    (workspace / "USER.md").write_text("# User\nDeveloper.\n", encoding="utf-8")
    (workspace / "TOOLS.md").write_text("# Tools\nFollow SOUL instructions exactly.\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Custom tools
# ---------------------------------------------------------------------------

_BLOB = "\n".join(f"  field_{i:02d}: value_{i:02d}_aaaa_bbbb_cccc_dddd_eeee_ffff_gggg" for i in range(35))


class _ToolAlpha(Tool):
    name = "tool_alpha"
    description = "Phase 1 lookup for an item. Returns structured field data."
    parameters = {
        "type": "object",
        "properties": {"item_id": {"type": "string"}},
        "required": ["item_id"],
    }

    async def execute(self, item_id: str = "", **kw: Any) -> str:
        return f"[alpha] Lookup for {item_id}:\n{_BLOB}"


class _ToolBeta(Tool):
    name = "tool_beta"
    description = "Phase 2 analysis for an item. Returns analysis results."
    parameters = {
        "type": "object",
        "properties": {"item_id": {"type": "string"}},
        "required": ["item_id"],
    }

    async def execute(self, item_id: str = "", **kw: Any) -> str:
        return f"[beta] Analysis for {item_id}:\n{_BLOB}"


class _ToolGamma(Tool):
    name = "tool_gamma"
    description = "Phase 3 validation for an item. Returns validation status."
    parameters = {
        "type": "object",
        "properties": {"item_id": {"type": "string"}},
        "required": ["item_id"],
    }

    async def execute(self, item_id: str = "", **kw: Any) -> str:
        return f"[gamma] Validation for {item_id}:\n{_BLOB}"


class _DataLookup(Tool):
    name = "data_lookup"
    description = "Look up structured data for a given item id."
    parameters = {
        "type": "object",
        "properties": {"item_id": {"type": "string"}},
        "required": ["item_id"],
    }

    async def execute(self, item_id: str = "", **kw: Any) -> str:
        return f"Lookup for {item_id}:\n{_BLOB}"


# ---------------------------------------------------------------------------
# Recording tracker
# ---------------------------------------------------------------------------


class _RecordingTracker(UsageTracker):
    name = "usage_tracker"

    def __init__(self):
        super().__init__(persist=False)
        self.history: list = []

    async def after_llm_call(self, response, usage):
        self.history.append(usage)
        await super().after_llm_call(response, usage)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class CallResult:
    fresh_prompt: int
    cache_read: int
    cache_write: int
    completion: int
    cost_usd: float


@dataclass
class VariantResult:
    name: str
    strategy: str
    sys_prompt_chars: int
    tools_count: int
    calls: list[CallResult] = field(default_factory=list)
    error: str | None = None

    @property
    def n(self) -> int:
        return len(self.calls)

    @property
    def total_fresh(self) -> int:
        return sum(c.fresh_prompt for c in self.calls)

    @property
    def total_cache_read(self) -> int:
        return sum(c.cache_read for c in self.calls)

    @property
    def total_cache_write(self) -> int:
        return sum(c.cache_write for c in self.calls)

    @property
    def total_completion(self) -> int:
        return sum(c.completion for c in self.calls)

    @property
    def total_cost(self) -> float:
        return sum(c.cost_usd for c in self.calls)


@dataclass
class ScenarioResult:
    name: str
    description: str
    variants: list[VariantResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Unified variant runner
# ---------------------------------------------------------------------------


async def _run_user_turn(loop, content: str, *, session_key: str, chat_id: str) -> None:
    """Run one USER turn through run_turn for its side-effects: the experiment
    measures usage via the tracker, not the reply, so the output is swallowed."""
    from raven.spine import ChatType, Origin, Source, TurnRequest

    async def _swallow(_ev) -> None:
        return None

    await loop.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="cli", chat_id=chat_id, sender_id="user", chat_type=ChatType.DM),
            text=content,
            conversation=session_key,
        ),
        _swallow,
        lambda: [],
        stream=False,
    )


async def _run_variant(
    *,
    variant_name: str,
    strategy_label: str,
    api_key: str,
    workspace_root: Path,
    scenario_tag: str,
    soul: str,
    questions: list[str],
    register_tools: list[Tool] | None,
    cache_strategy: CacheOptimizer | SystemAndTailCacheStrategy | None,
    disable_provider_auto_cache: bool,
    cost_so_far: dict[str, float],
    max_iterations: int = 8,
) -> VariantResult:
    workspace = workspace_root / f"{scenario_tag}_{variant_name}"
    _seed_workspace(workspace, soul)

    provider = LiteLLMProvider(
        api_key=api_key,
        api_base="https://openrouter.ai/api/v1",
        default_model=MODEL,
        provider_name="openrouter",
        disable_auto_cache_control=disable_provider_auto_cache,
        extra_body=_OPENROUTER_PIN,
    )

    tracker = _RecordingTracker()
    strategies: list = []
    if cache_strategy is not None:
        strategies.append(cache_strategy)
    strategies.append(tracker)

    loop = AgentLoop(
        provider=provider,
        workspace=workspace,
        model=MODEL,
        max_iterations=max_iterations,
        context_window_tokens=200_000,
        mcp_servers={},
        channels_config=None,
        strategies=StrategyRegistry(strategies),
    )

    # Configure tools
    loop.tools._tools.clear()
    if register_tools:
        for t in register_tools:
            loop.tools.register(t)

    sys_prompt = loop.context.build_system_prompt()
    result = VariantResult(
        name=variant_name,
        strategy=strategy_label,
        sys_prompt_chars=len(sys_prompt),
        tools_count=len(register_tools or []),
    )
    session_key = f"{scenario_tag}:{variant_name}"

    try:
        for q in questions:
            if sum(cost_so_far.values()) > COST_GUARD_USD:
                pytest.fail(f"Cost guard at ${sum(cost_so_far.values()):.4f}")
            before_count = len(tracker.history)
            await _run_user_turn(loop, q, session_key=session_key, chat_id=variant_name)
            # Collect ALL LLM calls this turn produced (may be >1 for tool chains)
            for snap in tracker.history[before_count:]:
                result.calls.append(
                    CallResult(
                        fresh_prompt=snap.input_tokens,
                        cache_read=snap.cache_read_tokens,
                        cache_write=snap.cache_write_tokens,
                        completion=snap.output_tokens,
                        cost_usd=snap.estimated_cost_usd,
                    )
                )
            cost_so_far[f"{scenario_tag}:{variant_name}"] = result.total_cost
    except Exception as e:
        result.error = repr(e)

    return result


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def _write_report(scenarios: list[ScenarioResult]) -> str:
    lines: list[str] = []
    lines.append("# Hermes ``system_and_3`` vs Raven ``CacheOptimizer`` — Head-to-Head\n")
    lines.append(
        "_Both strategies faithfully reproduced and benchmarked through the real ``AgentLoop.run_turn`` code path._\n"
    )
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat()} UTC_\n")
    lines.append(f"Model: `{MODEL}` (via OpenRouter, pinned to Anthropic)\n")

    lines.append("## Strategy definitions\n")
    lines.append("| | Breakpoint 1 | Breakpoint 2 | Breakpoint 3 | Breakpoint 4 |")
    lines.append("|:---|:---|:---|:---|:---|")
    lines.append("| **V1 baseline** | — | — | — | — |")
    lines.append("| **V2 Raven v2** | tools[-1] (if tools) | system tail | rolling msg[-2] | rolling msg[-1] |")
    lines.append("| **V3 Hermes** | system[0] | non_sys[-3] | non_sys[-2] | non_sys[-1] |")
    lines.append("")

    for sc in scenarios:
        lines.append(f"\n---\n\n## {sc.name}\n")
        lines.append(f"{sc.description}\n")

        baseline = sc.variants[0]
        lines.append("### Aggregate\n")
        lines.append(
            "| Variant | Strategy | LLM calls | Fresh | Cache W | Cache R | Completion | "
            "Cost | vs baseline | cache hit % |"
        )
        lines.append(
            "|:--------|:---------|----------:|------:|--------:|--------:|-----------:|"
            "-----:|------------:|------------:|"
        )
        for v in sc.variants:
            delta = (
                f"{(v.total_cost - baseline.total_cost) / baseline.total_cost * 100:+.1f}%"
                if baseline.total_cost > 0
                else "n/a"
            )
            total_input = v.total_fresh + v.total_cache_read + v.total_cache_write
            hit_pct = f"{v.total_cache_read / total_input * 100:.1f}%" if total_input > 0 else "0%"
            lines.append(
                f"| {v.name} | {v.strategy} | {v.n} | {v.total_fresh:,} | "
                f"{v.total_cache_write:,} | {v.total_cache_read:,} | {v.total_completion:,} | "
                f"${v.total_cost:.6f} | {delta} | {hit_pct} |"
            )

        lines.append("\n### Per-call detail\n")
        for v in sc.variants:
            lines.append(f"#### {v.name} ({v.strategy}, sys={v.sys_prompt_chars:,}ch, tools={v.tools_count})\n")
            if v.error:
                lines.append(f"**ERROR**: `{v.error}`\n")
            lines.append("| # | Fresh | Cache R | Cache W | Compl | Cost |")
            lines.append("|--:|------:|--------:|--------:|------:|-----:|")
            for i, c in enumerate(v.calls, 1):
                lines.append(
                    f"| {i} | {c.fresh_prompt:,} | {c.cache_read:,} | "
                    f"{c.cache_write:,} | {c.completion} | ${c.cost_usd:.6f} |"
                )
            lines.append("")

        # Per-scenario conclusion
        v1 = sc.variants[0]
        lines.append("### Conclusion\n")
        for v in sc.variants[1:]:
            pct = (1 - v.total_cost / v1.total_cost) * 100 if v1.total_cost else 0
            lines.append(f"- **{v.name}** ({v.strategy}): **{pct:.1f}%** savings vs baseline")
        if len(sc.variants) > 2:
            v2, v3 = sc.variants[1], sc.variants[2]
            if v2.total_cost > 0:
                vs = (1 - v3.total_cost / v2.total_cost) * 100
                winner = v3.name if vs > 0 else v2.name
                lines.append(f"- **Winner: {winner}** (Raven vs Hermes: {vs:+.1f}%)\n")

    # Final verdict
    lines.append("\n---\n\n## Overall verdict\n")
    lines.append("| Scenario | Winner | Margin |")
    lines.append("|:---------|:-------|-------:|")
    for sc in scenarios:
        if len(sc.variants) >= 3:
            v2, v3 = sc.variants[1], sc.variants[2]
            if v2.total_cost > 0:
                margin = (1 - v3.total_cost / v2.total_cost) * 100
                winner = "Hermes" if margin > 1 else "Raven" if margin < -1 else "Tie"
                lines.append(f"| {sc.name} | {winner} | {abs(margin):.1f}% |")

    lines.append("\n---\n\n## Raw JSON\n")
    lines.append("```json")
    payload = {
        sc.name: {
            "description": sc.description,
            "variants": {
                v.name: {
                    "strategy": v.strategy,
                    "sys_chars": v.sys_prompt_chars,
                    "tools_count": v.tools_count,
                    "n_calls": v.n,
                    "totals": {
                        "fresh": v.total_fresh,
                        "cache_w": v.total_cache_write,
                        "cache_r": v.total_cache_read,
                        "completion": v.total_completion,
                        "cost": v.total_cost,
                    },
                    "calls": [
                        {
                            "fresh": c.fresh_prompt,
                            "cr": c.cache_read,
                            "cw": c.cache_write,
                            "comp": c.completion,
                            "cost": c.cost_usd,
                        }
                        for c in v.calls
                    ],
                    "error": v.error,
                }
                for v in sc.variants
            },
        }
        for sc in scenarios
    }
    lines.append(json.dumps(payload, indent=2, ensure_ascii=False))
    lines.append("```")

    body = "\n".join(lines)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(body, encoding="utf-8")
    return body


# ---------------------------------------------------------------------------
# The experiment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hermes_vs_raven(api_key: str, tmp_path: Path):
    cost: dict[str, float] = {}
    scenarios: list[ScenarioResult] = []

    # ================================================================
    # S1 — Pure conversation (no tools), 8 turns
    # ================================================================
    s1_questions = [
        f"Reply with only the word '{w}'." for w in ["OK", "YES", "DONE", "AFFIRM", "ACK", "READY", "CHECK", "END"]
    ]
    s1_variants = []
    for vname, strategy_label, cache_strat, disable_auto in [
        ("V1_baseline", "none", None, True),
        ("V2_raven", "tools+sys+rolling_tail", CacheOptimizer(max_breakpoints=4), True),
        ("V3_hermes", "sys+last_3", SystemAndTailCacheStrategy(), True),
    ]:
        v = await _run_variant(
            variant_name=vname,
            strategy_label=strategy_label,
            api_key=api_key,
            workspace_root=tmp_path,
            scenario_tag="S1",
            soul=_medium_soul(),
            questions=s1_questions,
            register_tools=None,
            cache_strategy=cache_strat,
            disable_provider_auto_cache=disable_auto,
            cost_so_far=cost,
        )
        s1_variants.append(v)
        await asyncio.sleep(2)

    scenarios.append(
        ScenarioResult(
            name="S1: Pure conversation (8 turns, no tools)",
            description=(
                "Medium SOUL.md (~600 tok). 8 single-token Q&A turns. No tools registered. "
                "Tests cross-turn history caching."
            ),
            variants=s1_variants,
        )
    )

    # ================================================================
    # S2 — Intra-turn tool chain (3 tools sequentially per turn, 3 turns)
    # ================================================================
    s2_questions = [
        "Investigate item_01.",
        "Investigate item_02.",
        "Investigate item_03.",
    ]
    chain_tools = [_ToolAlpha(), _ToolBeta(), _ToolGamma()]
    s2_variants = []
    for vname, strategy_label, cache_strat, disable_auto in [
        ("V1_baseline", "none", None, True),
        ("V2_raven", "tools+sys+rolling_tail", CacheOptimizer(max_breakpoints=4), True),
        ("V3_hermes", "sys+last_3", SystemAndTailCacheStrategy(), True),
    ]:
        v = await _run_variant(
            variant_name=vname,
            strategy_label=strategy_label,
            api_key=api_key,
            workspace_root=tmp_path,
            scenario_tag="S2",
            soul=_tool_chain_soul(),
            questions=s2_questions,
            register_tools=chain_tools,
            cache_strategy=cache_strat,
            disable_provider_auto_cache=disable_auto,
            cost_so_far=cost,
            max_iterations=8,
        )
        s2_variants.append(v)
        await asyncio.sleep(2)

    scenarios.append(
        ScenarioResult(
            name="S2: Intra-turn tool chain (3 tools × 3 turns)",
            description=(
                "Small SOUL.md (~300 tok). 3 registered tools (alpha/beta/gamma). "
                "System prompt instructs: call exactly one tool per LLM response, "
                "in order. Each turn = up to 4 LLM calls. Tests intra-turn prefix caching."
            ),
            variants=s2_variants,
        )
    )

    # ================================================================
    # S3 — Mixed: multi-turn, one tool per turn, 6 turns
    # ================================================================
    s3_questions = [f"Look up id 'item_{i:02d}' and confirm in one word." for i in range(1, 7)]
    s3_variants = []
    for vname, strategy_label, cache_strat, disable_auto in [
        ("V1_baseline", "none", None, True),
        ("V2_raven", "tools+sys+rolling_tail", CacheOptimizer(max_breakpoints=4), True),
        ("V3_hermes", "sys+last_3", SystemAndTailCacheStrategy(), True),
    ]:
        v = await _run_variant(
            variant_name=vname,
            strategy_label=strategy_label,
            api_key=api_key,
            workspace_root=tmp_path,
            scenario_tag="S3",
            soul=_single_tool_soul(),
            questions=s3_questions,
            register_tools=[_DataLookup()],
            cache_strategy=cache_strat,
            disable_provider_auto_cache=disable_auto,
            cost_so_far=cost,
            max_iterations=4,
        )
        s3_variants.append(v)
        await asyncio.sleep(2)

    scenarios.append(
        ScenarioResult(
            name="S3: Mixed — one tool per turn (6 turns)",
            description=(
                "Small SOUL.md (~300 tok). 1 tool (data_lookup). Each turn = 2 LLM calls "
                "(decide + respond). Tests the common real-world agent workload."
            ),
            variants=s3_variants,
        )
    )

    # Write report
    body = _write_report(scenarios)
    print(f"\nReport: {REPORT_PATH}\n")
    print(body[:5000])

    # ---- Assertions ----
    for sc in scenarios:
        for v in sc.variants:
            assert v.error is None, f"{sc.name}/{v.name}: {v.error}"
            assert v.n > 0, f"{sc.name}/{v.name}: no LLM calls"

        v1, v2, v3 = sc.variants
        assert v1.total_cache_read == 0, f"{sc.name}: baseline has cache reads"
        assert v2.total_cost < v1.total_cost, f"{sc.name}: Raven not cheaper than baseline"
        assert v3.total_cost < v1.total_cost, f"{sc.name}: Hermes not cheaper than baseline"
