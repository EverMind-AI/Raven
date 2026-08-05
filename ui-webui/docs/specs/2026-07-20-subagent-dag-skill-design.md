# `subagent-dag-orchestration` example skill — design

**Date:** 2026-07-20
**Status:** Approved (design)
**Builds on:** the sub-agent DAG orchestrator (`src/agentscope/subagent/_dag/`)
and the AgentScope skill mechanism (`src/agentscope/skill/`,
`tool/_builtin/_skill.py`).

## 1. Motivation

The leader agent in the reference service (`examples/agent_service`) can spawn
CLI sub-agents and can orchestrate them as a DAG via the `run_subagent_dag`
tool. But nothing tells the agent *to prefer the DAG* when a task spans
multiple sub-agents, nor teaches it the tool's input format. Left to itself the
agent tends to invoke sub-agents one at a time — losing the DAG's concurrent
scheduling, its file-based message passing (which keeps large outputs out of
the agent's context), and its per-node auditable outputs.

AgentScope already has a first-class **skill** mechanism: a directory holding a
`SKILL.md` (frontmatter `name`/`description` + a markdown body). A skill's
`name`/`description` are surfaced in the leader's system prompt; the agent
reads the full body on demand through the builtin `Skill` viewer tool. Skills
reach the leader Toolkit by being seeded into the workspace
(`LocalWorkspaceManager(skill_paths=[...])` → `workspace.list_skills()` →
Toolkit `skills_or_loaders`).

This design ships one such skill that encodes the prefer-DAG policy and the
`run_subagent_dag` input format, and wires it into the reference service so it
is live.

## 2. Scope

**In scope:**

- A new skill package `examples/agent_service/skills/subagent-dag-orchestration/SKILL.md`.
- Wiring it into `examples/agent_service/main.py` via `LocalWorkspaceManager`'s
  `skill_paths`, so the reference service actually surfaces it to the leader.
- A short note in `examples/agent_service/README.md`.
- A discovery test that the skill loads with valid frontmatter and targets the
  right tool.

**Out of scope / unchanged:** the DAG tool, runner, store, placeholders, and
any other `src/` code. No new dependencies (`frontmatter` and
`agentscope.skill.LocalSkillLoader` already exist).

## 3. Skill frontmatter

```yaml
name: subagent-dag-orchestration
description: Use whenever a task needs two or more sub-agents, or any
  multi-step sub-agent work where one step's output feeds another. Orchestrate
  them as a single DAG via the run_subagent_dag tool instead of invoking
  sub-agents one by one.
```

`name` is the exact handle the agent passes to the `Skill` viewer tool. Only
`name`/`description` are system-prompt-visible; they are the hook that makes the
agent decide to open the body.

## 4. Skill body (focused operational guide)

Sections, in order:

1. **When to use (prefer-DAG policy).** The mandate: any task needing **two or
   more sub-agents**, or any dependency chain where one sub-agent's output
   feeds another, should be expressed as **one** `run_subagent_dag` call.
   Invoke a single sub-agent tool directly only for a genuine one-sub-agent,
   one-shot task. Rationale: independent nodes run concurrently; outputs pass
   through files (large content stays out of the agent's context); every node's
   output is persisted and reviewable.

2. **How it works** (one paragraph). Submit a flat `nodes` list; a ready-set
   scheduler runs independent nodes concurrently; each node's output is written
   to a file under the run directory; downstream nodes pull upstream outputs via
   templates; the tool returns the terminal-node outputs plus a list of every
   node's output file.

3. **Node schema** — a table of the six fields, faithful to the tool schema:

   | Field | Required | Meaning |
   | --- | --- | --- |
   | `id` | yes | Unique node id, `^[A-Za-z0-9_-]+$`. |
   | `subagent` | yes | Name of the sub-agent tool that runs this node. |
   | `prompt_template` | yes | Template rendered into the node's prompt file. |
   | `depends_on` | no | Ids of upstream nodes that must finish first. |
   | `inputs` | no | Map key → literal string or `{"file": "<workdir-rel path>"}`. |
   | `instance` | no | Stable, semantic handle to reuse one stateful sub-agent conversation across nodes (e.g. `researcher`). |

4. **Template placeholders** — a table of all six, plus the rules:

   | Placeholder | Resolves to |
   | --- | --- |
   | `{{ <id>.output }}` | Upstream node's output **contents**. |
   | `{{ <id>.output_path }}` | Its output file **path**. |
   | `{{ inputs.<key> }}` | The literal, or the referenced file's **contents**. |
   | `{{ inputs.<key>.path }}` | The input file's **path**. |
   | `{{ ref:<path> }}` | **Contents** of a prior workdir file (e.g. an earlier run's output). |
   | `{{ ref_path:<path> }}` | Its **path**. |

   Rules: prefer the `_path` forms for large content (the sub-agent reads the
   file itself, keeping bytes out of context); only declared `depends_on` /
   `inputs` may be referenced; all `ref`/`file` paths must stay within the
   session workdir.

5. **Two copy-paste worked examples** (the literal JSON value for the `nodes`
   argument):

   - **Fan-out → aggregate:** `A` and `B` run in parallel (no `depends_on`);
     `C` has `depends_on: ["A", "B"]` and synthesizes with
     `{{ A.output_path }}` + `{{ B.output_path }}`.
   - **Pipeline with a reused stateful instance:** `draft → review → revise`,
     each `depends_on` the previous, referencing `{{ <prev>.output }}`, sharing
     one `instance` handle to demonstrate stateful reuse.

6. **Reading the result.** The tool returns a `Node output files:` list — one
   `- <node> [<status>]: <path>` line per node (including `failed`/`skipped`,
   which show `(no output file)`) — followed by the inline terminal outputs. To
   review any node's work, including intermediate (non-terminal) nodes, `Read`
   its output file.

7. **Rules / anti-patterns.** Don't loop single sub-agent calls when a DAG
   expresses the work; don't inline huge upstream content (use the `_path`
   forms); every id referenced in a template must appear in that node's
   `depends_on`; use the sub-agent names actually present in your toolkit — do
   not invent names.

The body refers to sub-agents generically and directs the agent to its own
available sub-agent tools, so it stays correct regardless of which sub-agent
presets a deployment provisions.

## 5. Wiring into the reference service

In `examples/agent_service/main.py`, add `skill_paths` to the existing
`LocalWorkspaceManager(...)` call, pointing at the skill directory next to
`main.py`:

```python
_SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "skills",
)

workspace_manager = LocalWorkspaceManager(
    basedir=...,
    isolation=IsolationPolicy.PER_SESSION,
    default_mcps=default_mcps,
    skill_paths=[
        os.path.join(_SKILLS_DIR, "subagent-dag-orchestration"),
    ],
)
```

On first workspace `initialize()`, the directory (which contains `SKILL.md`) is
copied into `<workdir>/skills`, indexed, and returned by `list_skills()`, which
the chat service feeds into the leader Toolkit's `skills_or_loaders`. A short
note is added to `examples/agent_service/README.md`.

## 6. Testing

The real failure mode is a `SKILL.md` with missing/malformed frontmatter, which
the loader **silently skips** — the skill would then never reach the agent. A
discovery test in `tests/subagent_dag_skill_test.py` guards exactly this:

- Load the skill directory with the public `agentscope.skill.LocalSkillLoader`.
- Assert exactly one `Skill` loads, comparing the whole `Skill` structure with
  `AnyString`/`AnyValue` (from `tests/utils.py`) for the nondeterministic fields
  (`dir`, `markdown`, `updated_at`), and the exact
  `name == "subagent-dag-orchestration"` plus a non-empty `description`.
- Assert the body documents `run_subagent_dag` (so the skill targets the right
  tool and hasn't drifted).

## 7. Files touched

- **New** `examples/agent_service/skills/subagent-dag-orchestration/SKILL.md`
- **Modify** `examples/agent_service/main.py` — `skill_paths` wiring.
- **Modify** `examples/agent_service/README.md` — one-line note.
- **New** `tests/subagent_dag_skill_test.py` — discovery test.

## 8. Decisions

1. **Co-locate under `examples/agent_service/skills/` and wire into `main.py`**
   (vs. a standalone example or a file left un-wired) — the reference service
   already has the DAG tool wired via `make_subagent_tool_factory`, so this is
   the natural, immediately-runnable home. (2026-07-20)
2. **Focused operational guide** (vs. minimal, vs. exhaustive) — enough to make
   the agent choose the DAG *and* build a valid one first try, without bloating
   the body with rarely-used detail. (2026-07-20)
3. **A discovery test, not a content-assertion test** — verify the skill loads
   and targets the right tool; do not brittle-assert on wording. (2026-07-20)
