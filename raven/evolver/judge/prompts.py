"""LLM-judge prompt templates.

Two top-level templates are exposed:

- ``JUDGE_SYSTEM_PROMPT``: the role + rubric + output schema text. Sent
  as the system message of the judge LLM call.
- ``JUDGE_USER_TEMPLATE``: parametrized with the specific trajectory
  data; sent as the user message.

The system prompt teaches the L1/L2/L3 rubric, the (WHERE, WHY) labels,
and the JSON output schema. Decoupling system + user lets us reuse the
heavy rubric across many trajectories without re-paying tokens.

Building blocks:

- ``build_judge_messages`` returns the (system, user) pair given a
  trajectory record — feed directly to any chat-style LLM provider.
- ``WHERE_DESCRIPTIONS`` / ``WHY_DESCRIPTIONS`` document each enum value
  in human terms; they are injected into the prompt so the judge model
  has a concrete signpost (not just the enum string).

All prompt text is intentionally explicit about the cross-field
invariants enforced by ``JudgeResult.__post_init__``: L1 → no patch,
L2/L3 → patch_where + patch_why required, ``other`` → must carry a
sub-name. Saying it both in the prompt and in code reduces wasted
LLM calls that produce invalid JSON.
"""

from __future__ import annotations

from .schema import PatchWhere, PatchWhy


# --- Human-readable descriptions injected into the prompt ----------------


WHERE_DESCRIPTIONS: dict[PatchWhere, str] = {
    PatchWhere.system_prompt_template:
        "Raven's system prompt templates (raven/templates/*.md: SOUL, AGENTS, "
        "TOOLS, etc). Use when the agent's identity / behavioural guidelines need "
        "wording changes.",
    PatchWhere.task_wrapper_prompt:
        "external scorer task domain prompt (src/domains/<domain>/prompt.md). "
        "This is the user-message wrapper telling agent what the task is and what "
        "rules to follow. Common L2 fixes live here.",
    PatchWhere.judge_prompt:
        "Raven's internal turn-judge prompt (eval_engine/prompts/*.py, L-B "
        "layer). Affects memory updates, NOT the benchmark verifier; safe to "
        "evolve.",
    PatchWhere.tool_description:
        "A tool's description string or safety pattern in agent/tools/*.py. Use "
        "when the model misuses a tool because the description is vague / misleading.",
    PatchWhere.hook_new:
        "Add a new lifecycle hook file in agent/hook/<name>.py. Hooks fire at "
        "phases (before_iteration / before_execute_tools / after_iteration / "
        "after_send) and can inject nudges or short-circuit the loop. Best for "
        "runtime behaviour interventions (repetition, test cadence, budget).",
    PatchWhere.hook_modify:
        "Tune an existing hook's parameters in eval_engine/hooks/*.py.",
    PatchWhere.skill:
        "Add / edit / retire a skill in memory_engine/skills/. Skills are "
        "retrievable procedural recipes — use when a class of tasks needs a "
        "reusable how-to.",
    PatchWhere.memory:
        "Add / edit / retire a memory entry in memory_engine/everos/. Memory "
        "holds factual knowledge (e.g. 'Django ≥4 uses async views').",
    PatchWhere.tool_new:
        "Add a new Tool subclass in agent/tools/<name>.py. Rare — only when no "
        "existing tool plus prompt nudge can express the needed operation.",
    PatchWhere.loop_override:
        "Scoped override to agent/loop/main.py or subsidiary loop logic (code "
        "class). Use for instrumentation, conditional logic, or parameter patches "
        "that don't merit a full hook. Must include activation_beacon().",
    PatchWhere.context_override:
        "Scoped override to context management (code class). Patches short-lived "
        "state structures that vary per iteration. Must include activation_beacon().",
    PatchWhere.tool_override:
        "Scoped override to tool execution or selection logic (code class). Use for "
        "tool-routing or execution-time patches. Must include activation_beacon().",
    PatchWhere.config:
        "Tune a yaml / json default value, threshold, or feature flag.",
    PatchWhere.control:
        "Control arm - no mechanism; measures infra runtime-neutrality. Not a real patch surface; never propose patches here.",
}


WHY_DESCRIPTIONS: dict[PatchWhy, str] = {
    PatchWhy.repetition_breaker:
        "Agent loops on near-identical (tool, args) sequences without making "
        "progress (observed in 72% of 244-paired failed trajectories).",
    PatchWhy.test_starvation_remedy:
        "Agent spends most turns reading code, runs tests too infrequently to "
        "iterate (PASS trajectories run tests at 25% of turns vs FAIL at 12%).",
    PatchWhy.budget_awareness:
        "Agent has no sense of how much budget is left and over-explores until "
        "max_iter (100% of max_iter failures hit the ceiling).",
    PatchWhy.tool_clarity:
        "A registered tool is unused or misused because its description / "
        "documentation is missing / vague / misleading.",
    PatchWhy.env_contract_clarify:
        "The environment's interface contract is inaccurately described — e.g. "
        "a prompt forbids using a tool that the env actually supports, or a tool "
        "is documented as host-side when it's container-side.",
    PatchWhy.skill_gap_fill:
        "A recurring task type (e.g. Django pytest setup) has no skill in the "
        "library, so the agent re-discovers the same recipe in every trajectory.",
    PatchWhy.memory_recall_fix:
        "Agent repeats the same lookup / verification within a single trajectory "
        "(re-reads same file range, re-runs same python -c). Short-term memory "
        "is leaking.",
    PatchWhy.reasoning_visibility:
        "Agent does not externalize its reasoning before tool calls or final "
        "answer. Trajectory shows long stretches of tool calls without "
        "narrative explanation, making it hard to follow intent or diagnose "
        "failures. Patch typically adds a prompt nudge to 'explain your "
        "reasoning before each significant tool call' or similar visibility-"
        "improving instruction. Promoted from B2 dry-run 2026-05-30 as the "
        "dominant `other` extra (reasoning_visibility_improvement, "
        "communication_traceability, explanatory_text_nudge, ...).",
    PatchWhy.empty_response_recovery:
        "Agent enters a streak of completely empty turns (content=None, no tool "
        "calls) and does not self-recover. Patch typically adds a hook or prompt "
        "nudge that detects the streak and injects a recovery instruction.",
    PatchWhy.method_lock_in_remedy:
        "Agent commits too early to one approach (e.g., a single file path or "
        "algorithm) and stops exploring alternatives even when early attempts fail. "
        "Patch introduces a method-diversity nudge or conditional branch check.",
    PatchWhy.infra_neutrality_control:
        "Control-arm bookkeeping: this node carries no real patch; it exists to "
        "measure baseline infra runtime-neutrality across rounds. Not a real "
        "pathology category — never use for actual patch proposals.",
    PatchWhy.other:
        "None of the above categories fit. You MUST specify a free-form "
        "sub-name in `patch_why_extra` (e.g. 'plan_action_disconnect'). Reserve "
        "for genuinely novel pathologies; do not abuse to avoid picking an "
        "existing category.",
}


# --- Main prompt -----------------------------------------------------------


def _render_where_block() -> str:
    return "\n".join(
        f"  - `{w.value}`: {WHERE_DESCRIPTIONS[w]}" for w in PatchWhere
    )


def _render_why_block() -> str:
    return "\n".join(
        f"  - `{w.value}`: {WHY_DESCRIPTIONS[w]}" for w in PatchWhy
    )


JUDGE_SYSTEM_PROMPT = f"""You are an expert LLM-agent harness analyst.

You read a (compressed) trajectory of an LLM agent attempting a coding task
(SWE-bench / Terminal-Bench style) and produce a structured diagnosis. Your
output drives an automated harness self-evolution loop, so it must be
precise, machine-readable, and conservative.

# Step 1 — classify the issue type

You MUST pick exactly one of:

- **L1 — Infrastructure bug** (model / network / container / tool-implementation
  failure). Triggers (any single one is enough):
    * assistant **completely silent** turn (content=None or empty string
      AND **no** tool_calls) + finish_reason="stop" appears in >30% of turns.
      NOTE: a tool-using turn (content=None but tool_calls present) is the
      NORMAL agent pattern (model acts instead of explaining); it is NOT
      an L1 signal. Only count turns that have neither text nor tool calls.
    * tool errors like `docker daemon error`, `connection refused`, `read
      timeout`, `OOM`, `pool exhausted`
    * same prompt+input produces wildly different behaviour across trials
      (variance suggests transient infrastructure failure)
    * agent terminated suddenly at turn<10 with no final assistant text
    * trajectory shows the agent was writing code but the run aborted from
      a system-level error
  Action for L1: kind="human_review_needed". Do NOT propose any patch.

  # L1 vs L2 — finish_reason discriminator (read this before triggering L1
  # on any empty-content turn):

    - empty content + finish_reason="stop"           → L1 (model truly silent;
                                                            provider returned a
                                                            terminated stream
                                                            with no output)
    - empty content + finish_reason="length"         → L2 (max_tokens budget too
                                                            small for this model
                                                            — config issue, not
                                                            infra failure).
                                                            Patch: bump max_tokens
                                                            for this call path,
                                                            or disable thinking
                                                            for sub-LLM calls.
    - empty content + finish_reason="content_filter" → L1 (safety abort = infra-
                                                            side decision)
    - non-empty but malformed JSON / partial output  → L2 (the model IS producing
                                                            tokens; the harness
                                                            prompt + parser
                                                            combination is too
                                                            strict for this
                                                            model's capability).
                                                            Patch: tighten the
                                                            format prompt, add
                                                            a JSON5/markdown-
                                                            strip parser, or use
                                                            structured-output
                                                            mode if the backend
                                                            supports it.

  NOTE: "graceful fallback" patterns in tool results (e.g. "Failed to parse
  rewrite response — defaulting to retrieval") are L2 signals, NOT L1. The
  agent is still running fine; the harness is just degraded. Look at the WHY
  not the symptom.

- **L2 — Harness config error** (documents / configs / prompts are inaccurate,
  contradictory, or stale). Triggers (any one):
    * prompts contradict each other (e.g. one says "use X" another says
      "NEVER use X")
    * a tool is registered but its documentation tells the agent not to use it
    * tool description is misleading; the agent picks the wrong tool
    * config defaults are inappropriate for the benchmark
  Action for L2: kind="patch_proposal" with full WHERE/WHY/target_file fields.

- **L3 — Harness capability gap** (system runs cleanly but behaviour can be
  smarter). Triggers (any one):
    * agent failed to retrieve a relevant skill that exists in the library
    * memory is missing a known failure-pattern that would have helped
    * a useful nudge is missing from the prompt (e.g. "run tests after every
      edit")
    * a skill exists but its content is low quality
  Action for L3: kind="patch_proposal" with full WHERE/WHY/target_file fields.

If multiple types could apply, prefer L1 > L2 > L3 (errors block evolution
first).

# Step 1.5 — L2 vs L3 tiebreaker (when both could apply)

If you are leaning L3 but the issue could also be framed as L2, ask:

  "Could a static edit to an existing config / prompt / tool description
  have prevented this *before* the agent run started?"

  - YES → L2 (something is misconfigured / stale / contradictory)
  - NO  → L3 (config is fine; agent needs more capability — new skill,
              new memory, new prompt nudge)

Examples:
  - Agent picked wrong tool because two tool descriptions overlap → L2
    (description needs rewrite to disambiguate; static fix)
  - Agent picked wrong tool because no skill explains when to pick which → L3
    (need new skill; not a description bug)
  - Prompt says "use pytest" but env has pytest-xdist conflict → L2
    (prompt is stale; update it)
  - Prompt is consistent and tool docs are fine but agent forgot to run
    tests after edits → L3 (add a nudge skill / memory entry)

Default tilt when uncertain: **prefer L2** — fixing config is cheaper and
more general than adding new skills/memory.

# Step 2 — for L2 / L3, choose WHERE the patch goes

Pick exactly one structural location:

{_render_where_block()}

# Step 3 — for L2 / L3, choose WHY this patch is being made

Pick exactly one pathology category. If none fit, use `other` and provide
a free-form sub-name in `patch_why_extra`:

{_render_why_block()}

# Step 4 — emit JSON

Output exactly the following JSON object — nothing else, no surrounding
prose, no Markdown code fences (the parser strips fences but emitting
clean JSON is faster and cheaper).

```
{{
  "trajectory_id": "<echo the id given to you>",
  "issue_type": "L1" | "L2" | "L3",
  "confidence": <float 0.0 to 1.0>,
  "signal_description": "<one short sentence describing the observed failure mode>",
  "evidence_turn_range": [<start_turn>, <end_turn>],
  "proposed_action": {{
    "kind": "human_review_needed" | "patch_proposal",
    "reasoning": "<overall reasoning across the whole patch (or, for L1, why human review)>",

    // The fields below are required when kind=patch_proposal,
    // and MUST be omitted (null/[] is acceptable) when kind=human_review_needed.
    "patch_where": "<PatchWhere enum value>",
    "patch_why": "<PatchWhy enum value>",
    "patch_why_extra": "<sub-name; required iff patch_why=other; null otherwise>",
    "components": [
      {{
        "component_id": "comp_1",
        "target_file": "<repo-relative path of the file to edit>",
        "summary": "<one short sentence describing what to change in this file>",
        "depends_on": []
      }}
      // ...add additional component objects for structurally-coupled edits
    ]
  }}
}}
```

# CRITICAL — cross-field consistency (the parser will reject your output
# if violated):

  - If issue_type == "L1":
      kind MUST be "human_review_needed"
      patch_proposal MUST be null
  - If issue_type == "L2" or "L3":
      kind MUST be "patch_proposal"
      patch_proposal MUST be a full object with WHERE / WHY / target_file

Double-check this constraint BEFORE emitting JSON. If you are tempted to
output L2/L3 with kind="human_review_needed", you are confused — re-read
Step 1 and either downgrade to L1 (if truly infra) or commit to producing
a patch_proposal.

# Multi-component patches

`components` is ALWAYS a non-empty list (for L2/L3). For most patches use
exactly ONE component (single-file fix). Use MULTIPLE components only when
the change is structurally coupled — i.e., no single component is useful
on its own. Examples:

- New hook FILE + register the hook in CONFIG → 2 components, where the
  config one `depends_on: ["comp_1"]`.
- Add a new TOOL + reference it in the SYSTEM_PROMPT → 2 components.
- Add a SKILL + update MEMORY index pointing to it → 2 components.

Rules for `components`:

- `component_id` must be unique within this `components` array
  (e.g. "comp_1", "comp_2", ...).
- `depends_on` is a list of sibling `component_id`s that must be applied
  first. It MUST only reference siblings within THIS components array.
  Use `[]` (empty) when independent.
- If you find yourself wanting to bundle ≥3 unrelated file edits into one
  patch, STOP — emit only the highest-priority one. The evolver will
  propose other patches in later iterations.

# Cross-field rules (will be re-validated downstream)

Emitting invalid JSON wastes a round-trip:

1. L1 → kind must be `human_review_needed`; patch_where / patch_why /
   patch_why_extra / components all null or `[]`.
2. L2 / L3 → kind must be `patch_proposal`; patch_where + patch_why
   populated; `components` is a non-empty list with all required
   fields per item.
3. `patch_why=other` → `patch_why_extra` non-empty.
4. `confidence` ∈ [0.0, 1.0]; report your true uncertainty, not 1.0 by default.
5. `evidence_turn_range` is the inclusive interval of turn indices that
   justify your judgement. If a single turn suffices, use [n, n].
"""


JUDGE_USER_TEMPLATE = """trajectory_id: {trajectory_id}
task_description:
\"\"\"
{task_description}
\"\"\"

trajectory (compressed):
\"\"\"
{trajectory_text}
\"\"\"

Output the JSON analysis now."""


def build_judge_messages(
    trajectory_id: str,
    task_description: str,
    trajectory_text: str,
) -> list[dict[str, str]]:
    """Assemble the (system, user) message pair for one judge call.

    Returns the list shape most chat-style LLM SDKs expect:
    ``[{"role": "system", "content": ...}, {"role": "user", "content": ...}]``.

    ``trajectory_text`` should already be compressed to the judge's context
    budget — typically 5-15K tokens of "Agent Debugger" style event summary
    (spec §11.4.5). Raw multi-million-token trajectories are NOT supported;
    upstream must compress first.

    No string interpolation happens in the system prompt — its content is
    fixed across calls, so most providers will cache it on subsequent calls
    (significant cost savings during a long evolution run).
    """
    user = JUDGE_USER_TEMPLATE.format(
        trajectory_id=trajectory_id,
        task_description=task_description.strip(),
        trajectory_text=trajectory_text.strip(),
    )
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


PASS_FAIL_SYSTEM_PROMPT = (
    "You are grading whether an AI agent ACCOMPLISHED a task, from its trajectory. "
    "There is no automated verifier — your verdict is the score. Judge only "
    "whether the task's goal was actually achieved (correct final answer / the "
    "requested action completed and verified), not effort or intent. Be strict: "
    "an unfinished, guessed, or unverified result is a fail. Respond with ONLY a "
    'JSON object, no prose, no code fences: {"passed": true|false, "reasoning": '
    '"<one sentence>"}.'
)


def build_pass_fail_messages(
    task_description: str, trajectory_text: str, *, trajectory_id: str = ""
) -> list[dict[str, str]]:
    """Assemble the (system, user) pair for a no-benchmark pass/fail verdict."""
    user = (
        f"Task:\n{task_description.strip()}\n\n"
        f"Agent trajectory:\n{trajectory_text.strip()}\n\n"
        "Did the agent accomplish the task? Return the JSON verdict."
    )
    return [
        {"role": "system", "content": PASS_FAIL_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


__all__ = [
    "JUDGE_SYSTEM_PROMPT",
    "JUDGE_USER_TEMPLATE",
    "WHERE_DESCRIPTIONS",
    "WHY_DESCRIPTIONS",
    "build_judge_messages",
    "PASS_FAIL_SYSTEM_PROMPT",
    "build_pass_fail_messages",
]
