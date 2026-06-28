"""TokenWise step 1+2 ablation experiment against the live OpenRouter API.

This test is a controlled experiment that answers one question:
    *Does CacheOptimizer + UsageTracker actually save money on a real
     multi-turn conversation, and by how much?*

To answer rigorously, we run the SAME 5-turn workload three times under
three configurations:

    V1  baseline                — no cache_control at all (lower bound)
    V2  provider built-in cache — LiteLLMProvider's default 2-breakpoint impl
    V3  TokenWise CacheOptimizer — strategy-driven 4-breakpoint placement

Per call we record (prompt, cache_read, cache_write, completion, cost), then
aggregate and dump everything to ``raven/token_wise/EXPERIMENT_REPORT.md``.

Skipped automatically when ``raven/key.env`` is missing.

Cost guard: the entire experiment is bounded to ~$0.30 on Sonnet 4.5.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from raven.config.raven import TokenWiseConfig
from raven.providers.litellm_provider import LiteLLMProvider
from raven.cli._token_wise_stack import install_from_config
from raven.token_wise.registry import StrategyRegistry

KEY_FILE = Path(__file__).resolve().parent.parent / "raven" / "key.env"
REPORT_PATH = (
    Path(__file__).resolve().parent.parent
    / "raven"
    / "token_wise"
    / "EXPERIMENT_REPORT.md"
)
MODEL = "anthropic/claude-sonnet-4-5"
TURNS = 6
COST_GUARD_USD = 0.50  # hard cap; abort if we go over

# OpenRouter routes Anthropic requests across multiple backends by default.
# Without affinity, each call lands on a fresh instance and the prompt cache
# is never reused (verified empirically: cache_write fires every call but
# cache_read stays 0). Pinning to the Anthropic backend restores normal
# cache semantics. This is OpenRouter-specific; direct Anthropic API users
# don't need it.
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


def _long_system_prompt() -> str:
    """Build a ~6 KB / ~1700-token system prompt — well over Sonnet's 1024 min."""
    sections: list[str] = []
    sections.append(
        "You are a meticulous code reviewer specialized in Python static "
        "analysis, security auditing, and concurrency correctness. "
        "Your responses are concise, surgical, and free of hedging.\n\n"
        "# Detailed style guide\n\n"
    )
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
    for i, r in enumerate(rules, 1):
        sections.append(f"## Rule {i}\n{r}\n\n")
        sections.append(
            "Rationale: subtle violations compound into hard-to-debug "
            "regressions during long-running services. The cost of enforcement "
            "is low; the cost of letting it slip is high. Reviewers should "
            "flag every deviation with a concrete suggested fix and a code "
            "snippet showing the corrected form. When uncertain, ask for the "
            "broader context before recommending a change.\n\n"
        )
    sections.append(
        "When asked a question, answer in exactly one sentence. Do not "
        "restate the question. Do not apologize. Do not preamble. Do not "
        "say 'as a code reviewer'."
    )
    return "".join(sections)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    turn: int
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    response_chars: int
    finish_reason: str


@dataclass
class VariantResult:
    name: str
    description: str
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def total_prompt(self) -> int:
        return sum(t.prompt_tokens for t in self.turns)

    @property
    def total_completion(self) -> int:
        return sum(t.completion_tokens for t in self.turns)

    @property
    def total_cache_read(self) -> int:
        return sum(t.cache_read_tokens for t in self.turns)

    @property
    def total_cache_write(self) -> int:
        return sum(t.cache_write_tokens for t in self.turns)

    @property
    def total_cost(self) -> float:
        return sum(t.cost_usd for t in self.turns)


# ---------------------------------------------------------------------------
# Variant runner
# ---------------------------------------------------------------------------


def _build_snapshot(response, model: str, session_key: str):
    """Wrapper around AgentLoop._build_usage_snapshot for the experiment."""
    from raven.agent.loop import AgentLoop
    return AgentLoop._build_usage_snapshot(response, model, session_key)


async def _run_variant(
    *,
    name: str,
    description: str,
    api_key: str,
    sys_prompt: str,
    user_questions: list[str],
    disable_auto_cache: bool,
    registry: StrategyRegistry,
    cost_so_far: dict[str, float],
) -> VariantResult:
    """Run the same multi-turn workload under one configuration."""
    provider = LiteLLMProvider(
        api_key=api_key,
        api_base="https://openrouter.ai/api/v1",
        default_model=MODEL,
        provider_name="openrouter",
        disable_auto_cache_control=disable_auto_cache,
        extra_body=_OPENROUTER_PIN,
    )

    result = VariantResult(name=name, description=description)
    messages: list[dict] = [{"role": "system", "content": sys_prompt}]

    for turn_idx, q in enumerate(user_questions, 1):
        messages = messages + [{"role": "user", "content": q}]

        # Cost guard: never exceed the hard cap.
        if sum(cost_so_far.values()) > COST_GUARD_USD:
            pytest.fail(
                f"Cost guard tripped at ${sum(cost_so_far.values()):.4f} "
                f"(cap=${COST_GUARD_USD}). Aborting."
            )

        msgs, tools, model_chosen = await registry.before_llm_call(messages, None, MODEL)
        resp = await provider.chat_with_retry(messages=msgs, tools=tools, model=model_chosen)
        if resp.finish_reason == "error":
            pytest.fail(f"Variant {name} turn {turn_idx} failed: {resp.content}")

        # Build the snapshot through the same code path the real agent loop uses,
        # so the test catches any extraction/pricing bugs end-to-end.
        snap = _build_snapshot(resp, MODEL, name)

        result.turns.append(TurnResult(
            turn=turn_idx,
            prompt_tokens=snap.input_tokens,  # fresh-only after normalization
            completion_tokens=snap.output_tokens,
            cache_read_tokens=snap.cache_read_tokens,
            cache_write_tokens=snap.cache_write_tokens,
            cost_usd=snap.estimated_cost_usd,
            response_chars=len(resp.content or ""),
            finish_reason=resp.finish_reason,
        ))
        cost_so_far[name] = result.total_cost

        # Notify strategies (so UsageTracker captures stats for V3).
        await registry.after_llm_call({"usage": resp.usage}, snap)

        # Append assistant reply to history so the next turn shares the prefix.
        messages = messages + [{"role": "assistant", "content": resp.content or ""}]

    return result


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def _write_report(variants: list[VariantResult], baseline_name: str) -> str:
    """Render results to Markdown and write to EXPERIMENT_REPORT.md.

    Returns the report body (also useful for assertions in the test).
    """
    baseline = next(v for v in variants if v.name == baseline_name)
    lines: list[str] = []
    lines.append("# TokenWise Step 1+2 — Ablation Experiment Report\n")
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat()} UTC_\n")
    lines.append("")
    lines.append("## Setup\n")
    lines.append(f"- Model: `{MODEL}` (via OpenRouter)")
    lines.append(f"- Turns per variant: {TURNS}")
    lines.append("- System prompt: ~6 KB / ~1700 tokens (well over Sonnet's 1024 cache minimum)")
    lines.append("- Workload: identical user questions across all variants; conversation grows turn-by-turn")
    lines.append(f"- Cost guard: ${COST_GUARD_USD:.2f} (hard abort)")
    lines.append("")
    lines.append("## Variants\n")
    for v in variants:
        lines.append(f"- **{v.name}** — {v.description}")
    lines.append("")
    lines.append("## Aggregate results\n")
    lines.append("| Variant | Prompt (fresh) | Cache write | Cache read | Completion | Total cost | vs baseline |")
    lines.append("|:--------|---------------:|------------:|-----------:|-----------:|-----------:|------------:|")
    for v in variants:
        if baseline.total_cost > 0:
            delta_pct = (v.total_cost - baseline.total_cost) / baseline.total_cost * 100
            delta_str = f"{delta_pct:+.1f}%"
        else:
            delta_str = "n/a"
        lines.append(
            f"| {v.name} | {v.total_prompt:,} | {v.total_cache_write:,} | "
            f"{v.total_cache_read:,} | {v.total_completion:,} | "
            f"${v.total_cost:.6f} | {delta_str} |"
        )
    lines.append("")
    lines.append("## Per-turn detail\n")
    for v in variants:
        lines.append(f"### {v.name}\n")
        lines.append("| Turn | Prompt | Cache R | Cache W | Completion | Cost (USD) |")
        lines.append("|-----:|-------:|--------:|--------:|-----------:|-----------:|")
        for t in v.turns:
            lines.append(
                f"| {t.turn} | {t.prompt_tokens:,} | {t.cache_read_tokens:,} | "
                f"{t.cache_write_tokens:,} | {t.completion_tokens:,} | "
                f"${t.cost_usd:.6f} |"
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
            ratio = v.total_cache_read / max(1, v.total_cache_read + v.total_prompt)
            lines.append(
                f"- `{v.name}`: {ratio*100:.1f}% of input tokens served from cache"
            )
    lines.append("")
    lines.append("## Raw data (JSON)\n")
    lines.append("```json")
    payload = {
        v.name: {
            "description": v.description,
            "turns": [
                {
                    "turn": t.turn,
                    "prompt_tokens": t.prompt_tokens,
                    "completion_tokens": t.completion_tokens,
                    "cache_read_tokens": t.cache_read_tokens,
                    "cache_write_tokens": t.cache_write_tokens,
                    "cost_usd": t.cost_usd,
                    "response_chars": t.response_chars,
                    "finish_reason": t.finish_reason,
                }
                for t in v.turns
            ],
            "totals": {
                "prompt": v.total_prompt,
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
async def test_ablation_experiment(api_key: str, tmp_path: Path):
    """Run V1/V2/V3 against the same workload, write a report, assert savings."""
    sys_prompt = _long_system_prompt()
    # Workload designed to grow the conversation history each turn so the
    # extra V3 breakpoints (mid, before-last) can pay off vs V2's system-only.
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

    # V1 — baseline: no cache at all.
    v1_registry = StrategyRegistry([])
    v1 = await _run_variant(
        name="V1_baseline",
        description="No cache_control. Provider auto-cache disabled. No TokenWise.",
        api_key=api_key, sys_prompt=sys_prompt, user_questions=questions,
        disable_auto_cache=True, registry=v1_registry, cost_so_far=cost_so_far,
    )

    # Brief pause so the cache write from V2 (if any) doesn't bleed into V3.
    await asyncio.sleep(2)

    # V2 — provider's built-in 2-breakpoint cache, no TokenWise.
    v2_registry = StrategyRegistry([])
    v2 = await _run_variant(
        name="V2_provider_auto",
        description="LiteLLMProvider built-in cache_control (system + last tool). No TokenWise.",
        api_key=api_key, sys_prompt=sys_prompt, user_questions=questions,
        disable_auto_cache=False, registry=v2_registry, cost_so_far=cost_so_far,
    )

    await asyncio.sleep(2)

    # V3 — TokenWise CacheOptimizer + UsageTracker (the actual product).
    cfg = TokenWiseConfig(enabled=True, cache_optimization=True, usage_tracking=True, max_cache_breakpoints=4)
    v3_registry = install_from_config(cfg, telemetry_dir=tmp_path)
    v3 = await _run_variant(
        name="V3_tokenwise",
        description="TokenWise CacheOptimizer (4 breakpoints) + UsageTracker. Provider auto-cache disabled.",
        api_key=api_key, sys_prompt=sys_prompt, user_questions=questions,
        disable_auto_cache=True, registry=v3_registry, cost_so_far=cost_so_far,
    )

    # Sanity: UsageTracker recorded V3's calls and persisted them.
    tracker = v3_registry.get("usage_tracker")
    assert tracker is not None
    snap = tracker.snapshot("V3_tokenwise")
    assert snap.input_tokens > 0
    assert snap.output_tokens > 0
    files = list(tmp_path.glob("usage-*.jsonl"))
    assert len(files) == 1
    rows = [r for r in files[0].read_text().splitlines() if r.strip()]
    assert len(rows) == TURNS, f"expected {TURNS} jsonl rows, got {len(rows)}"

    # Write the report.
    body = _write_report([v1, v2, v3], baseline_name="V1_baseline")
    print(f"\nReport written to: {REPORT_PATH}\n")
    print(body)

    # ---- Assertions on the experiment outcome ----
    # V1 should have no cache hits at all (no markers placed).
    assert v1.total_cache_read == 0, "V1 baseline must have zero cache reads"
    assert v1.total_cache_write == 0, "V1 baseline must have zero cache writes"
    # V2 + V3 must reuse a cache. (cache_write may be 0 if the prefix was
    # already cached by a previous variant in this run — both V2 and V3 use
    # the same system prompt, so V3 inherits V2's warm cache. The existence
    # of cache_read > 0 is the real proof the system works.)
    assert v2.total_cache_read > 0, (
        f"V2 (provider auto-cache) had no cache hits; system_prompt cache "
        f"may not have been created. v2.turns={v2.turns}"
    )
    assert v3.total_cache_read > 0, (
        f"V3 had no cache hits across {TURNS} turns. v3.turns={v3.turns}"
    )
    # Note: cache_write may legitimately be 0 if a previous test run already
    # populated Anthropic's ephemeral cache for this exact prefix (5-min TTL).
    # The presence of cache_read > 0 is the real proof the system works end-to-end.

    # The big claim: V3 must save cost vs no-cache baseline.
    assert v3.total_cost < v1.total_cost, (
        f"V3 (${v3.total_cost:.6f}) is not cheaper than V1 baseline "
        f"(${v1.total_cost:.6f}); caching is providing no savings."
    )
    # Quantify minimum savings — be conservative for stable CI.
    v3_savings_pct = (1 - v3.total_cost / v1.total_cost) * 100
    assert v3_savings_pct >= 50, (
        f"V3 saved only {v3_savings_pct:.1f}% vs V1; expected >= 50% on this "
        f"workload (long stable system prompt should yield 70-90% savings)."
    )
    # V2 should also save vs baseline (provider auto-cache works, just not as well).
    v2_savings_pct = (1 - v2.total_cost / v1.total_cost) * 100
    assert v2_savings_pct > 0, (
        f"V2 (provider auto-cache) showed no savings vs V1 (${v2.total_cost:.6f} "
        f"vs ${v1.total_cost:.6f})"
    )
    # V3 should be at least as good as V2 (more breakpoints can only help).
    assert v3.total_cost <= v2.total_cost * 1.05, (  # 5% tolerance for noise
        f"V3 (${v3.total_cost:.6f}) noticeably more expensive than V2 "
        f"(${v2.total_cost:.6f}); TokenWise CacheOptimizer is regressing vs "
        f"the simpler provider auto-cache."
    )
