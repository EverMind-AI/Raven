"""TokenWise scenario sweep — does V3 actually beat V2 outside the 'big system / no history' edge case?

The earlier ``test_token_wise_agentloop_experiment.py`` showed V3 only
~0.2% cheaper than V2 because the workload made V2 look good (huge
stable system prompt, almost no history). This file tests two workloads
that should expose V3's structural advantage:

    Scenario A  — medium system + long history
        - SOUL.md sized to ~2 KB / ~600 tokens
        - 16 turns of synthetic history pre-seeded into the session
          (~3000 tokens of cacheable conversation prefix)
        - Then 6 fresh turns through AgentLoop.run_turn

    Scenario B  — frequent tool calls, tool results accumulate
        - SOUL.md sized to ~1 KB / ~300 tokens
        - One custom tool returning ~500 tokens of deterministic data
        - 6 user messages each forcing a tool_lookup call
        - Each turn = 2 LLM calls (decide → respond), so cumulative
          tool_result history grows by ~600 tok per turn

Variants in both scenarios:
    V1  baseline             — no cache_control anywhere
    V2  provider auto-cache  — 1 breakpoint at system end
    V3  TokenWise            — 4 breakpoints incl. history

The test writes a combined report at
``raven/token_wise/EXPERIMENT_REPORT_WORKLOADS.md``.

Skipped automatically when ``raven/key.env`` is missing.
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
from raven.token_wise.usage_tracker import UsageTracker

KEY_FILE = Path(__file__).resolve().parent.parent / "raven" / "key.env"
REPORT_PATH = Path(__file__).resolve().parent.parent / "raven" / "token_wise" / "EXPERIMENT_REPORT_WORKLOADS.md"
MODEL = "anthropic/claude-sonnet-4-5"
COST_GUARD_USD = 1.50
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
    """A ~2 KB SOUL.md — small enough that history can dominate the prompt."""
    return (
        "# Soul\n\n"
        "I am Raven, a careful assistant. I reply concisely and never invent facts.\n\n"
        "## Working principles\n\n"
        "- Be precise about what you know vs what you assume.\n"
        "- Prefer direct answers over hedged ones.\n"
        "- When asked about prior context, refer to it accurately.\n"
        "- Ground every claim in the conversation history when relevant.\n"
        "- Never repeat what the user just said back to them.\n\n"
        "## Output protocol\n\n"
        "When asked a yes/no question, reply with one word.\n"
        "When asked for a single token (e.g. 'OK', 'YES'), output that token only.\n"
        "Otherwise reply in one short sentence.\n\n"
        "## Domain hints\n\n"
        "- Topics may include: software engineering, math, language, daily tasks.\n"
        "- Code answers prefer Python 3.11+ idioms unless asked otherwise.\n"
        "- Time-related answers respect the user's stated timezone.\n"
    )


def _small_soul() -> str:
    """A ~1 KB SOUL.md — for the tools scenario where history grows fast."""
    return (
        "# Soul\n\n"
        "I am Raven, a tool-using assistant.\n\n"
        "## Tool usage\n\n"
        "When the user gives an item id, immediately call the ``data_lookup`` "
        "tool with that id. After receiving the result, reply with one short "
        "sentence summarizing what was retrieved.\n\n"
        "## Output protocol\n\n"
        "Be terse. Never repeat the raw tool output. Never re-call a tool you "
        "already called this turn.\n"
    )


def _seed_workspace(workspace: Path, soul: str) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "SOUL.md").write_text(soul, encoding="utf-8")
    (workspace / "AGENTS.md").write_text("# Agent\nTerse responses.\n", encoding="utf-8")
    (workspace / "USER.md").write_text("# User\nDeveloper.\n", encoding="utf-8")
    (workspace / "TOOLS.md").write_text("# Tools\nNo notes.\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Custom fixed-response tool for Scenario B
# ---------------------------------------------------------------------------


_LOOKUP_FIXED_BLOB = "Item record:\n" + "\n".join(
    f"  field_{i:02d}: value_{i:02d}_aaaaaaaa_bbbbbbbb_cccccccc_dddddddd" for i in range(40)
)


class _DataLookupTool(Tool):
    """Returns a deterministic ~500-token blob whose content does not depend on the input.

    Determinism matters: the assistant's reply is the same across variants
    so completion_tokens stays constant and we isolate the cache effect.
    """

    name = "data_lookup"
    description = "Look up structured data for a given item id. Returns a block of fields."
    parameters = {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "Identifier of the item to look up"},
        },
        "required": ["item_id"],
    }

    async def execute(self, item_id: str = "", **kwargs: Any) -> str:
        return f"Lookup result for {item_id}\n{_LOOKUP_FIXED_BLOB}"


# ---------------------------------------------------------------------------
# Recording tracker — same idea as in the prior experiment
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
# Per-call / per-variant data structures
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
    description: str
    sys_prompt_chars: int
    history_chars_seed: int
    calls: list[CallResult] = field(default_factory=list)
    error: str | None = None

    @property
    def n_calls(self) -> int:
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
    workload: str
    variants: list[VariantResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Variant runner — scenario A (long conversation)
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


async def _run_long_conversation(
    *,
    name: str,
    description: str,
    api_key: str,
    workspace_root: Path,
    install_cache_optimizer: bool,
    disable_provider_auto_cache: bool,
    cost_so_far: dict[str, float],
    seed_turns: int = 16,
    fresh_turns: int = 6,
) -> VariantResult:
    workspace = workspace_root / f"sceneA_{name}"
    _seed_workspace(workspace, _medium_soul())

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
    if install_cache_optimizer:
        strategies.append(CacheOptimizer(max_breakpoints=4))
    strategies.append(tracker)
    registry = StrategyRegistry(strategies)

    loop = AgentLoop(
        provider=provider,
        workspace=workspace,
        model=MODEL,
        max_iterations=4,
        context_window_tokens=200_000,
        mcp_servers={},
        channels_config=None,
        strategies=registry,
    )
    loop.tools._tools.clear()  # no tools in this scenario

    sys_prompt = loop.context.build_system_prompt()
    sys_chars = len(sys_prompt)

    # Pre-seed the session with synthetic history. Each pair adds ~180 chars,
    # so 16 turns ≈ 2900 chars ≈ ~750 tokens of cacheable conversation prefix.
    session_key = f"sceneA:{name}"
    session = loop.sessions.get_or_create(session_key)
    seed_topics = [
        "What's the time complexity of merging two sorted lists of size n?",
        "Why does Python's GIL prevent true parallelism for CPU-bound threads?",
        "Summarize the Liskov Substitution Principle in one sentence.",
        "What's the difference between a thread and a process on Linux?",
        "Give me the formula for compound interest.",
        "Explain monads to a Python developer in one sentence.",
        "What does HTTP 429 mean and how should clients respond?",
        "When is binary search inappropriate?",
    ]
    for i in range(seed_turns):
        topic = seed_topics[i % len(seed_topics)]
        session.add_message("user", f"Background turn {i + 1}: {topic}")
        session.add_message(
            "assistant",
            f"Reply {i + 1}: a deliberately moderate-length answer that contains "
            f"enough text to add ~120 tokens to the session history when serialized "
            f"into the prompt. Topic recap index {i}.",
        )
    seed_chars = sum(len(m.get("content", "")) for m in session.messages)
    loop.sessions.save(session)

    result = VariantResult(
        name=name,
        description=description,
        sys_prompt_chars=sys_chars,
        history_chars_seed=seed_chars,
    )
    questions = [
        "Reply with only the word OK.",
        "Now reply with only the word YES.",
        "Now reply with only the word DONE.",
        "Now reply with only the word AFFIRM.",
        "Now reply with only the word ACK.",
        "Final turn — reply only with the word END.",
    ][:fresh_turns]

    try:
        for q in questions:
            if sum(cost_so_far.values()) > COST_GUARD_USD:
                pytest.fail(f"Cost guard tripped at ${sum(cost_so_far.values()):.4f}")
            await _run_user_turn(loop, q, session_key=session_key, chat_id=name)
            snap = tracker.history[-1]
            result.calls.append(
                CallResult(
                    fresh_prompt=snap.input_tokens,
                    cache_read=snap.cache_read_tokens,
                    cache_write=snap.cache_write_tokens,
                    completion=snap.output_tokens,
                    cost_usd=snap.estimated_cost_usd,
                )
            )
            cost_so_far[name] = result.total_cost
    except Exception as e:
        result.error = repr(e)

    return result


# ---------------------------------------------------------------------------
# Variant runner — scenario B (tool result accumulation)
# ---------------------------------------------------------------------------


async def _run_tool_accumulation(
    *,
    name: str,
    description: str,
    api_key: str,
    workspace_root: Path,
    install_cache_optimizer: bool,
    disable_provider_auto_cache: bool,
    cost_so_far: dict[str, float],
    fresh_turns: int = 6,
) -> VariantResult:
    workspace = workspace_root / f"sceneB_{name}"
    _seed_workspace(workspace, _small_soul())

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
    if install_cache_optimizer:
        strategies.append(CacheOptimizer(max_breakpoints=4))
    strategies.append(tracker)
    registry = StrategyRegistry(strategies)

    loop = AgentLoop(
        provider=provider,
        workspace=workspace,
        model=MODEL,
        max_iterations=4,
        context_window_tokens=200_000,
        mcp_servers={},
        channels_config=None,
        strategies=registry,
    )
    # Strip default tools; install ONLY our deterministic data_lookup.
    loop.tools._tools.clear()
    loop.tools.register(_DataLookupTool())

    sys_prompt = loop.context.build_system_prompt()
    sys_chars = len(sys_prompt)

    result = VariantResult(
        name=name,
        description=description,
        sys_prompt_chars=sys_chars,
        history_chars_seed=0,
    )
    session_key = f"sceneB:{name}"

    try:
        for i in range(1, fresh_turns + 1):
            if sum(cost_so_far.values()) > COST_GUARD_USD:
                pytest.fail(f"Cost guard tripped at ${sum(cost_so_far.values()):.4f}")
            await _run_user_turn(
                loop,
                f"Look up id 'item_{i:02d}' and confirm in one word.",
                session_key=session_key,
                chat_id=name,
            )
        # Each fresh turn should have produced (decide → respond) = 2 LLM calls.
        for snap in tracker.history:
            result.calls.append(
                CallResult(
                    fresh_prompt=snap.input_tokens,
                    cache_read=snap.cache_read_tokens,
                    cache_write=snap.cache_write_tokens,
                    completion=snap.output_tokens,
                    cost_usd=snap.estimated_cost_usd,
                )
            )
        cost_so_far[name] = result.total_cost
    except Exception as e:
        result.error = repr(e)

    return result


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def _write_report(scenarios: list[ScenarioResult]) -> str:
    lines: list[str] = []
    lines.append("# TokenWise Workload Sweep — Where does V3 beat V2?\n")
    lines.append(
        "_Two complementary workloads run through ``AgentLoop.run_turn`` "
        "to test the claim that ``CacheOptimizer`` (4 breakpoints, history-aware) "
        "structurally outperforms the provider's built-in single-breakpoint "
        "cache when history is large._\n"
    )
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat()} UTC_\n")
    lines.append(f"\nModel: `{MODEL}` (via OpenRouter, pinned to Anthropic backend)\n")

    for sc in scenarios:
        lines.append(f"\n---\n\n## Scenario {sc.name} — {sc.description}\n")
        lines.append(f"**Workload:** {sc.workload}\n")
        baseline = sc.variants[0]

        lines.append("### Aggregate\n")
        lines.append(
            "| Variant | LLM calls | Fresh prompt | Cache write | Cache read | Completion | Total cost | vs baseline |"
        )
        lines.append(
            "|:--------|----------:|-------------:|------------:|-----------:|-----------:|-----------:|------------:|"
        )
        for v in sc.variants:
            if baseline.total_cost > 0:
                delta_pct = (v.total_cost - baseline.total_cost) / baseline.total_cost * 100
                delta_str = f"{delta_pct:+.1f}%"
            else:
                delta_str = "n/a"
            lines.append(
                f"| {v.name} | {v.n_calls} | {v.total_fresh:,} | {v.total_cache_write:,} | "
                f"{v.total_cache_read:,} | {v.total_completion:,} | "
                f"${v.total_cost:.6f} | {delta_str} |"
            )

        lines.append("\n### Per-call detail\n")
        for v in sc.variants:
            lines.append(
                f"#### {v.name} (sys={v.sys_prompt_chars:,} chars, seeded history={v.history_chars_seed:,} chars)\n"
            )
            if v.error:
                lines.append(f"**ERROR**: `{v.error}`\n")
            lines.append("| Call | Fresh | Cache R | Cache W | Completion | Cost (USD) |")
            lines.append("|-----:|------:|--------:|--------:|-----------:|-----------:|")
            for i, c in enumerate(v.calls, 1):
                lines.append(
                    f"| {i} | {c.fresh_prompt:,} | {c.cache_read:,} | "
                    f"{c.cache_write:,} | {c.completion:,} | ${c.cost_usd:.6f} |"
                )
            lines.append("")

        # Conclusions per scenario
        v3 = next((v for v in sc.variants if v.name.startswith("V3")), None)
        v2 = next((v for v in sc.variants if v.name.startswith("V2")), None)
        v1 = next((v for v in sc.variants if v.name.startswith("V1")), None)
        if v1 and v2 and v3 and v1.total_cost > 0:
            v2_pct = (1 - v2.total_cost / v1.total_cost) * 100
            v3_pct = (1 - v3.total_cost / v1.total_cost) * 100
            v3_vs_v2 = (1 - v3.total_cost / v2.total_cost) * 100 if v2.total_cost > 0 else 0
            lines.append("### Conclusions\n")
            lines.append(f"- V2 vs V1: **{v2_pct:.1f}%** savings")
            lines.append(f"- V3 vs V1: **{v3_pct:.1f}%** savings")
            lines.append(f"- **V3 vs V2: {v3_vs_v2:+.1f}%** (negative = V3 cheaper)\n")

    lines.append("\n---\n\n## Raw data (JSON)\n")
    lines.append("```json")
    payload = {
        sc.name: {
            "description": sc.description,
            "workload": sc.workload,
            "variants": {
                v.name: {
                    "description": v.description,
                    "sys_prompt_chars": v.sys_prompt_chars,
                    "history_chars_seed": v.history_chars_seed,
                    "n_calls": v.n_calls,
                    "totals": {
                        "fresh_prompt": v.total_fresh,
                        "cache_write": v.total_cache_write,
                        "cache_read": v.total_cache_read,
                        "completion": v.total_completion,
                        "cost_usd": v.total_cost,
                    },
                    "calls": [
                        {
                            "fresh": c.fresh_prompt,
                            "cache_read": c.cache_read,
                            "cache_write": c.cache_write,
                            "completion": c.completion,
                            "cost_usd": c.cost_usd,
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workload_scenarios(api_key: str, tmp_path: Path):
    """Run scenarios A and B end-to-end and emit a combined report."""
    cost_so_far: dict[str, float] = {}

    # ---- Scenario A: medium system + long pre-seeded history ----
    a_v1 = await _run_long_conversation(
        name="V1_baseline",
        description="No cache_control. Provider auto-cache OFF.",
        api_key=api_key,
        workspace_root=tmp_path,
        install_cache_optimizer=False,
        disable_provider_auto_cache=True,
        cost_so_far=cost_so_far,
    )
    await asyncio.sleep(2)
    a_v2 = await _run_long_conversation(
        name="V2_provider_auto",
        description="Provider built-in cache_control (system + last tool only).",
        api_key=api_key,
        workspace_root=tmp_path,
        install_cache_optimizer=False,
        disable_provider_auto_cache=False,
        cost_so_far=cost_so_far,
    )
    await asyncio.sleep(2)
    a_v3 = await _run_long_conversation(
        name="V3_tokenwise",
        description="TokenWise CacheOptimizer (4 breakpoints incl. history).",
        api_key=api_key,
        workspace_root=tmp_path,
        install_cache_optimizer=True,
        disable_provider_auto_cache=True,
        cost_so_far=cost_so_far,
    )

    scenario_a = ScenarioResult(
        name="A",
        description="medium system + long pre-seeded history",
        workload=(
            "SOUL.md ~2 KB; 16 turns of synthetic Q&A pre-seeded into the "
            "session before measurement; then 6 fresh single-token turns."
        ),
        variants=[a_v1, a_v2, a_v3],
    )

    await asyncio.sleep(2)

    # ---- Scenario B: tool result accumulation ----
    b_v1 = await _run_tool_accumulation(
        name="V1_baseline",
        description="No cache_control. Provider auto-cache OFF.",
        api_key=api_key,
        workspace_root=tmp_path,
        install_cache_optimizer=False,
        disable_provider_auto_cache=True,
        cost_so_far=cost_so_far,
    )
    await asyncio.sleep(2)
    b_v2 = await _run_tool_accumulation(
        name="V2_provider_auto",
        description="Provider built-in cache_control (system + last tool only).",
        api_key=api_key,
        workspace_root=tmp_path,
        install_cache_optimizer=False,
        disable_provider_auto_cache=False,
        cost_so_far=cost_so_far,
    )
    await asyncio.sleep(2)
    b_v3 = await _run_tool_accumulation(
        name="V3_tokenwise",
        description="TokenWise CacheOptimizer (4 breakpoints incl. history).",
        api_key=api_key,
        workspace_root=tmp_path,
        install_cache_optimizer=True,
        disable_provider_auto_cache=True,
        cost_so_far=cost_so_far,
    )

    scenario_b = ScenarioResult(
        name="B",
        description="frequent tool calls — tool results accumulate in history",
        workload=(
            "SOUL.md ~1 KB; one custom data_lookup tool returning a fixed "
            "~500-token blob per call; 6 user messages each forcing a tool "
            "call; each turn is therefore (decide → respond) = 2 LLM calls."
        ),
        variants=[b_v1, b_v2, b_v3],
    )

    body = _write_report([scenario_a, scenario_b])
    print(f"\nReport written to: {REPORT_PATH}\n")
    print(body[:4000])  # print head only; tail is dominated by raw JSON

    # ---- Hard assertions ----
    for sc in [scenario_a, scenario_b]:
        for v in sc.variants:
            assert v.error is None, f"Scenario {sc.name} variant {v.name} crashed: {v.error}"
            assert v.n_calls > 0, f"Scenario {sc.name} variant {v.name} produced no LLM calls"

    for sc in [scenario_a, scenario_b]:
        v1, v2, v3 = sc.variants[0], sc.variants[1], sc.variants[2]
        # No cache markers ⇒ no cache activity.
        assert v1.total_cache_read == 0, f"{sc.name}/{v1.name}: cache reads on baseline"
        assert v1.total_cache_write == 0, f"{sc.name}/{v1.name}: cache writes on baseline"
        # V2 + V3 must hit cache.
        assert v2.total_cache_read > 0, f"{sc.name}/{v2.name}: zero cache hits"
        assert v3.total_cache_read > 0, f"{sc.name}/{v3.name}: zero cache hits"
        # Both must save vs V1.
        assert v3.total_cost < v1.total_cost, (
            f"{sc.name}: V3 (${v3.total_cost:.6f}) not cheaper than V1 (${v1.total_cost:.6f})"
        )
        assert v2.total_cost < v1.total_cost, (
            f"{sc.name}: V2 (${v2.total_cost:.6f}) not cheaper than V1 (${v1.total_cost:.6f})"
        )

    # The structural claim: V3 must beat V2 on these workloads, by at least 5%.
    for sc in [scenario_a, scenario_b]:
        v2, v3 = sc.variants[1], sc.variants[2]
        margin_pct = (1 - v3.total_cost / v2.total_cost) * 100 if v2.total_cost > 0 else 0
        assert margin_pct >= 5, (
            f"{sc.name}: V3 vs V2 margin only {margin_pct:.1f}% — expected >= 5% on this "
            f"workload. v2.total=${v2.total_cost:.6f} v3.total=${v3.total_cost:.6f}"
        )
