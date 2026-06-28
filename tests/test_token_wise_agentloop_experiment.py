"""TokenWise step 1+2 ablation — driven through the real ``AgentLoop``.

Unlike ``test_token_wise_integration_openrouter.py`` which calls
``LiteLLMProvider.chat_with_retry`` directly, this experiment goes through
the **full agent stack**:

    AgentLoop.run_turn → _process_message → _run_agent_loop
      → strategies.before_llm_call → provider.chat_with_retry
      → strategies.after_llm_call → save session

so it exercises the integration path that production traffic actually
takes (session manager, context builder, message hooks, system prompt
assembly, etc.). Everything that happens to the request before it leaves
the box is in scope.

Variants:
    V1  baseline             — no cache_control anywhere
    V2  provider auto-cache  — LiteLLMProvider's built-in 1-breakpoint impl
    V3  TokenWise            — CacheOptimizer (4 breakpoints) + UsageTracker

Workload: 6 turns of trivial Q&A using a deliberately oversized SOUL.md
that pushes the assembled system prompt above Anthropic's 1024-token
cache minimum.

Skipped automatically when ``raven/key.env`` is missing.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.litellm_provider import LiteLLMProvider
from raven.token_wise.cache_optimizer import CacheOptimizer
from raven.token_wise.registry import StrategyRegistry
from raven.token_wise.usage_tracker import UsageTracker

KEY_FILE = Path(__file__).resolve().parent.parent / "raven" / "key.env"
REPORT_PATH = (
    Path(__file__).resolve().parent.parent
    / "raven"
    / "token_wise"
    / "EXPERIMENT_REPORT.md"
)
MODEL = "anthropic/claude-sonnet-4-5"
TURNS = 6
COST_GUARD_USD = 0.50
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
# Workspace fixture — produces a tmp workspace whose SOUL.md is large enough
# that the assembled system prompt comfortably clears Sonnet's 1024-token
# cache minimum.
# ---------------------------------------------------------------------------


def _seed_workspace(workspace: Path) -> None:
    """Write the bootstrap files the AgentLoop's ContextBuilder will load."""
    rules = [
        "Prefer dataclasses over plain dicts when fields are known.",
        "Use ``from __future__ import annotations`` for forward references.",
        "Avoid mutable default arguments — they leak between calls.",
        "Prefer asyncio.gather over sequential awaits when independent.",
        "Use loguru for logging in this codebase, never print().",
        "Type-annotate every public function signature; mypy is non-negotiable.",
        "Catch exceptions narrowly; bare except is forbidden in production code.",
        "Treat warnings as errors in CI configurations to prevent rot.",
        "Validate inputs at boundaries; trust internal callers within a module.",
        "Use pathlib.Path over os.path string operations for new code.",
        "Prefer composition over inheritance for plug-in extension points.",
        "Document non-obvious invariants inline near the code they constrain.",
        "Use enum.Enum instead of magic strings for closed tag spaces.",
        "Make tests deterministic — no time.sleep, no real network in unit tests.",
        "Pin third-party dependencies with upper bounds in pyproject.toml.",
        "Prefer dependency injection over module-level singletons.",
        "Never log secrets; redact tokens, API keys, and PII at the boundary.",
        "Use frozen dataclasses for value objects to enforce immutability.",
        "Document the unit of every numeric quantity (ms, MB, USD).",
        "Prefer explicit empty checks over truthy checks for collections.",
    ]
    soul_lines: list[str] = ["# Soul\n", "I am Raven, a precise, terse code reviewer.\n"]
    soul_lines.append("## Detailed style guide\n")
    for i, r in enumerate(rules, 1):
        soul_lines.append(f"### Rule {i}\n{r}\n")
        soul_lines.append(
            "Rationale: subtle violations compound into hard-to-debug regressions "
            "during long-running services. The cost of enforcement is low; the "
            "cost of letting it slip is high. Reviewers should flag every "
            "deviation with a concrete suggested fix and a code snippet showing "
            "the corrected form. When uncertain, ask for the broader context "
            "before recommending a change.\n"
        )
    soul_lines.append(
        "## Output protocol\nReply in exactly one short sentence. Do not "
        "preamble. Do not apologize. Do not say 'as a code reviewer'."
    )
    (workspace / "SOUL.md").write_text("\n".join(soul_lines), encoding="utf-8")
    # Keep AGENTS.md, USER.md, TOOLS.md tiny so SOUL.md dominates the prompt.
    (workspace / "AGENTS.md").write_text("# Agent\nTerse responses only.\n", encoding="utf-8")
    (workspace / "USER.md").write_text("# User\nDeveloper.\n", encoding="utf-8")
    (workspace / "TOOLS.md").write_text("# Tools\nNo extra notes.\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Variant data structures
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    turn: int
    fresh_prompt: int
    cache_read: int
    cache_write: int
    completion: int
    cost_usd: float
    response_chars: int


@dataclass
class VariantResult:
    name: str
    description: str
    sys_prompt_chars: int
    turns: list[TurnResult] = field(default_factory=list)
    error: str | None = None

    @property
    def total_fresh(self) -> int:
        return sum(t.fresh_prompt for t in self.turns)

    @property
    def total_cache_read(self) -> int:
        return sum(t.cache_read for t in self.turns)

    @property
    def total_cache_write(self) -> int:
        return sum(t.cache_write for t in self.turns)

    @property
    def total_completion(self) -> int:
        return sum(t.completion for t in self.turns)

    @property
    def total_cost(self) -> float:
        return sum(t.cost_usd for t in self.turns)


# ---------------------------------------------------------------------------
# The variant runner — builds a real AgentLoop, drives 6 turns
# ---------------------------------------------------------------------------


class _RecordingTracker(UsageTracker):
    """UsageTracker subclass that also keeps the last-call snapshot in order.

    We need per-turn data, not just session totals, so we capture each call
    individually. Persistence is disabled to keep the experiment self-contained.
    """

    name = "usage_tracker"

    def __init__(self):
        super().__init__(persist=False)
        self.history: list = []

    async def after_llm_call(self, response, usage):
        self.history.append(usage)
        await super().after_llm_call(response, usage)


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
    name: str,
    description: str,
    api_key: str,
    workspace_root: Path,
    questions: list[str],
    install_cache_optimizer: bool,
    disable_provider_auto_cache: bool,
    cost_so_far: dict[str, float],
) -> VariantResult:
    """One variant = a fresh workspace, fresh AgentLoop, six run_turn calls."""
    # Per-variant workspace so each variant has its own session/memory.
    workspace = workspace_root / name
    workspace.mkdir(parents=True, exist_ok=True)
    _seed_workspace(workspace)

    # Provider — pin to Anthropic backend so OpenRouter doesn't shuffle and
    # break the cache between calls within this variant.
    provider = LiteLLMProvider(
        api_key=api_key,
        api_base="https://openrouter.ai/api/v1",
        default_model=MODEL,
        provider_name="openrouter",
        disable_auto_cache_control=disable_provider_auto_cache,
        extra_body=_OPENROUTER_PIN,
    )

    # Strategy registry — every variant gets a recording UsageTracker so we
    # can compare apples to apples; only V3 also gets the CacheOptimizer.
    tracker = _RecordingTracker()
    strategies: list = []
    if install_cache_optimizer:
        strategies.append(CacheOptimizer(max_breakpoints=4))
    strategies.append(tracker)
    registry = StrategyRegistry(strategies)

    # The real AgentLoop. We disable everything that would inject side
    # effects we don't want in the experiment (MCP, channels).
    loop = AgentLoop(
        provider=provider,
        workspace=workspace,
        model=MODEL,
        max_iterations=4,                      # we expect zero tool turns
        context_window_tokens=200_000,         # disable consolidator triggering
        mcp_servers={},
        channels_config=None,
        strategies=registry,
    )
    # Strip default tools so the assistant has nothing to invoke and the
    # tools schema doesn't add noise/cost variance to the experiment.
    loop.tools._tools.clear()

    # Capture the assembled system prompt size so the report can show what
    # actually went over the wire (not just what we put in SOUL.md).
    sys_prompt = loop.context.build_system_prompt()
    sys_chars = len(sys_prompt)

    result = VariantResult(name=name, description=description, sys_prompt_chars=sys_chars)
    session_key = f"experiment:{name}"

    try:
        for turn_idx, q in enumerate(questions, 1):
            if sum(cost_so_far.values()) > COST_GUARD_USD:
                pytest.fail(
                    f"Cost guard tripped at ${sum(cost_so_far.values()):.4f} "
                    f"(cap=${COST_GUARD_USD})."
                )
            await _run_user_turn(loop, q, session_key=session_key, chat_id=name)

            # Pull this turn's usage out of the tracker history.
            assert len(tracker.history) == turn_idx, (
                f"expected tracker.history len={turn_idx}, got {len(tracker.history)}"
            )
            snap = tracker.history[-1]
            result.turns.append(TurnResult(
                turn=turn_idx,
                fresh_prompt=snap.input_tokens,
                cache_read=snap.cache_read_tokens,
                cache_write=snap.cache_write_tokens,
                completion=snap.output_tokens,
                cost_usd=snap.estimated_cost_usd,
                response_chars=0,  # session manager retains response; tracker doesn't
            ))
            cost_so_far[name] = result.total_cost
    except Exception as e:
        result.error = repr(e)

    return result


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def _write_report(variants: list[VariantResult], baseline_name: str) -> str:
    baseline = next(v for v in variants if v.name == baseline_name)

    lines: list[str] = []
    lines.append("# TokenWise Step 1+2 — Ablation Experiment Report\n")
    lines.append(
        "_Driven through the real ``AgentLoop.run_turn`` so the "
        "experiment exercises session management, context assembly, "
        "TokenWise hooks, and provider invocation as a complete stack._\n"
    )
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat()} UTC_\n")
    lines.append("")
    lines.append("## Setup\n")
    lines.append(f"- Model: `{MODEL}` (via OpenRouter, pinned to Anthropic backend)")
    lines.append(f"- Turns per variant: {TURNS}")
    lines.append("- Driver: ``AgentLoop.run_turn`` (real production code path)")
    lines.append("- Workspace: per-variant tmp dir with seeded SOUL.md / AGENTS.md / USER.md / TOOLS.md")
    lines.append("- Default tools cleared on the loop (no tool-call noise)")
    lines.append("- Memory consolidator threshold raised to 200K tokens (won't trigger)")
    lines.append("- MCP / channels: disabled")
    lines.append(f"- Cost guard: ${COST_GUARD_USD:.2f} (hard abort)")
    lines.append("")

    lines.append("## Variants\n")
    for v in variants:
        lines.append(f"- **{v.name}** — {v.description}")
        lines.append(f"  - Assembled system prompt: {v.sys_prompt_chars:,} chars")
        if v.error:
            lines.append(f"  - **ERROR:** `{v.error}`")
    lines.append("")

    lines.append("## Aggregate results\n")
    lines.append("| Variant | Fresh prompt | Cache write | Cache read | Completion | Total cost | vs baseline |")
    lines.append("|:--------|-------------:|------------:|-----------:|-----------:|-----------:|------------:|")
    for v in variants:
        if baseline.total_cost > 0:
            delta_pct = (v.total_cost - baseline.total_cost) / baseline.total_cost * 100
            delta_str = f"{delta_pct:+.1f}%"
        else:
            delta_str = "n/a"
        lines.append(
            f"| {v.name} | {v.total_fresh:,} | {v.total_cache_write:,} | "
            f"{v.total_cache_read:,} | {v.total_completion:,} | "
            f"${v.total_cost:.6f} | {delta_str} |"
        )
    lines.append("")

    lines.append("## Per-turn detail\n")
    for v in variants:
        lines.append(f"### {v.name}\n")
        lines.append("| Turn | Fresh | Cache R | Cache W | Completion | Cost (USD) |")
        lines.append("|-----:|------:|--------:|--------:|-----------:|-----------:|")
        for t in v.turns:
            lines.append(
                f"| {t.turn} | {t.fresh_prompt:,} | {t.cache_read:,} | "
                f"{t.cache_write:,} | {t.completion:,} | ${t.cost_usd:.6f} |"
            )
        lines.append("")

    lines.append("## Conclusions\n")
    cheapest = min(variants, key=lambda v: v.total_cost)
    lines.append(f"- **Cheapest variant: `{cheapest.name}`** at ${cheapest.total_cost:.6f}")
    if baseline.total_cost > 0 and cheapest.name != baseline.name:
        savings_pct = (1 - cheapest.total_cost / baseline.total_cost) * 100
        lines.append(
            f"- **Savings vs `{baseline.name}`: {savings_pct:.1f}%** "
            f"(${baseline.total_cost - cheapest.total_cost:.6f} absolute)"
        )
    for v in variants:
        if v.total_cache_read > 0:
            covered = v.total_cache_read / max(1, v.total_cache_read + v.total_fresh)
            lines.append(
                f"- `{v.name}`: {covered*100:.1f}% of input tokens served from cache"
            )
    lines.append("")
    lines.append(
        "## Caveat — OpenRouter routing affinity\n\n"
        "OpenRouter's default routing distributes Anthropic requests across "
        "multiple backend instances, which empirically destroys the prompt "
        "cache: ``cache_write`` fires every call but ``cache_read`` stays 0. "
        "The variant runner pins the backend with "
        "``provider={'order': ['Anthropic'], 'allow_fallbacks': False}`` (passed "
        "through ``LiteLLMProvider.extra_body``) to restore normal cache "
        "semantics. Direct Anthropic API users do not need this.\n"
    )
    lines.append("## Raw data (JSON)\n")
    lines.append("```json")
    payload = {
        v.name: {
            "description": v.description,
            "sys_prompt_chars": v.sys_prompt_chars,
            "error": v.error,
            "turns": [
                {
                    "turn": t.turn,
                    "fresh_prompt": t.fresh_prompt,
                    "cache_read": t.cache_read,
                    "cache_write": t.cache_write,
                    "completion": t.completion,
                    "cost_usd": t.cost_usd,
                }
                for t in v.turns
            ],
            "totals": {
                "fresh_prompt": v.total_fresh,
                "cache_write": v.total_cache_write,
                "cache_read": v.total_cache_read,
                "completion": v.total_completion,
                "cost_usd": v.total_cost,
            },
        }
        for v in variants
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
async def test_agentloop_ablation_experiment(api_key: str, tmp_path: Path):
    """Run V1/V2/V3 through ``AgentLoop.run_turn`` with the same workload."""
    questions = [
        "Reply with only the word OK.",
        "Now reply with only the word YES.",
        "Now reply with only the word DONE.",
        "Now reply with only the word AFFIRM.",
        "Now reply with only the word ACK.",
        "Final turn — reply only with the word END.",
    ]
    assert len(questions) == TURNS

    cost_so_far: dict[str, float] = {}

    v1 = await _run_variant(
        name="V1_baseline",
        description="No cache_control. AgentLoop with empty CacheOptimizer; provider auto-cache OFF.",
        api_key=api_key, workspace_root=tmp_path, questions=questions,
        install_cache_optimizer=False, disable_provider_auto_cache=True,
        cost_so_far=cost_so_far,
    )
    await asyncio.sleep(2)

    v2 = await _run_variant(
        name="V2_provider_auto",
        description="LiteLLMProvider built-in cache_control (system + last tool); no CacheOptimizer.",
        api_key=api_key, workspace_root=tmp_path, questions=questions,
        install_cache_optimizer=False, disable_provider_auto_cache=False,
        cost_so_far=cost_so_far,
    )
    await asyncio.sleep(2)

    v3 = await _run_variant(
        name="V3_tokenwise",
        description="TokenWise CacheOptimizer (4 breakpoints) installed in the AgentLoop's StrategyRegistry; provider auto-cache OFF.",
        api_key=api_key, workspace_root=tmp_path, questions=questions,
        install_cache_optimizer=True, disable_provider_auto_cache=True,
        cost_so_far=cost_so_far,
    )

    body = _write_report([v1, v2, v3], baseline_name="V1_baseline")
    print(f"\nReport written to: {REPORT_PATH}\n")
    print(body)

    # ---- Assertions on observable behavior ----
    for v in [v1, v2, v3]:
        assert v.error is None, f"variant {v.name} crashed: {v.error}"
        assert len(v.turns) == TURNS, f"{v.name} did not complete all turns"
        assert v.sys_prompt_chars > 4_000, (
            f"{v.name} system prompt too short ({v.sys_prompt_chars} chars) — "
            f"may not exceed Anthropic's cache minimum"
        )

    # V1: no cache markers placed → zero cache activity must be observed.
    assert v1.total_cache_read == 0, "V1 baseline must have zero cache reads"
    assert v1.total_cache_write == 0, "V1 baseline must have zero cache writes"

    # V2 and V3: the system prompt was big enough to qualify for caching, so
    # repeated turns within each variant must have reused the cache.
    assert v2.total_cache_read > 0, (
        f"V2 had no cache reads across {TURNS} turns — provider auto-cache "
        f"is broken end-to-end. v2.turns={v2.turns}"
    )
    assert v3.total_cache_read > 0, (
        f"V3 had no cache reads across {TURNS} turns — TokenWise "
        f"CacheOptimizer is not flowing to the provider via the AgentLoop. "
        f"v3.turns={v3.turns}"
    )

    # Savings claims.
    assert v3.total_cost < v1.total_cost, (
        f"V3 (${v3.total_cost:.6f}) is not cheaper than V1 baseline "
        f"(${v1.total_cost:.6f})"
    )
    v3_pct = (1 - v3.total_cost / v1.total_cost) * 100
    assert v3_pct >= 50, (
        f"V3 saved only {v3_pct:.1f}% vs V1; expected >= 50% on this workload"
    )
    assert v2.total_cost < v1.total_cost, (
        f"V2 (provider auto-cache) showed no savings vs V1 (${v2.total_cost:.6f} "
        f"vs ${v1.total_cost:.6f})"
    )
    assert v3.total_cost <= v2.total_cost * 1.05, (
        f"V3 (${v3.total_cost:.6f}) noticeably more expensive than V2 "
        f"(${v2.total_cost:.6f}); CacheOptimizer is regressing vs the "
        f"provider's default."
    )
