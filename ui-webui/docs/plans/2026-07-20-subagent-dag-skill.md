# `subagent-dag-orchestration` example skill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an AgentScope skill (a `SKILL.md` package) under `examples/agent_service` that steers the leader agent to prefer `run_subagent_dag` whenever a task spans two or more sub-agents, wire it into the reference service, and cover it with a discovery test.

**Architecture:** A skill is a directory with a `SKILL.md` (frontmatter `name`/`description` + markdown body). It reaches the leader agent by being seeded into the workspace via `LocalWorkspaceManager(skill_paths=[...])`, whose `list_skills()` feeds the leader Toolkit's `skills_or_loaders`; the `name`/`description` appear in the system prompt and the body is fetched on demand through the builtin `Skill` viewer tool. No `src/` code changes.

**Tech Stack:** Python 3.11+, `unittest` (`IsolatedAsyncioTestCase`)/`pytest`, `agentscope.skill.LocalSkillLoader`.

## Global Constraints

- **No `src/` changes.** Only `examples/agent_service/**` and `tests/**` are touched. No new dependencies.
- **Skill frontmatter is mandatory:** the `SKILL.md` MUST have both `name: subagent-dag-orchestration` and a non-empty `description`, or `LocalSkillLoader` silently drops it.
- **Faithful to the tool:** node fields (`id`, `subagent`, `prompt_template`, `depends_on`, `inputs`, `instance`) and placeholders (`{{ <id>.output }}`, `{{ <id>.output_path }}`, `{{ inputs.<key> }}`, `{{ inputs.<key>.path }}`, `{{ ref:<path> }}`, `{{ ref_path:<path> }}`) must match `src/agentscope/subagent/_dag/_tool.py` exactly.
- **Style/CI:** black (line length **79**), flake8, pylint, mypy, docstring checks. Run `pre-commit run --files <changed files>` before committing; fix the code, never disable checks. (Markdown files are not subject to the Python hooks, but run pre-commit on the whole change set anyway.) Backend tests: `conda run -n ravenx python -m pytest tests/<file> -v`.
- **Tests:** compare the whole `Skill` structure, using `AnyString`/`AnyValue` (from `tests/utils.py`) for nondeterministic fields; assert real behavior.
- **Commits:** `git add` only the files named in the task (never `git add -A`; never stage `.webapp_logs/*.pid`). End each commit message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Create the skill package and its discovery test

**Files:**
- Create: `examples/agent_service/skills/subagent-dag-orchestration/SKILL.md`
- Test: `tests/subagent_dag_skill_test.py`

**Interfaces:**
- Consumes: `agentscope.skill.LocalSkillLoader(directory, scan_subdir=False)` → `await .list_skills()` returns `list[Skill]`; `agentscope.skill.Skill` is a dataclass with fields `name`, `description`, `dir`, `markdown`, `updated_at`. `tests/utils.py` exposes `AnyString` and `AnyValue`.
- Produces: the loadable skill directory that Task 2 wires into `main.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_dag_skill_test.py` with exactly this content:

```python
# -*- coding: utf-8 -*-
"""Discovery test for the example subagent-dag-orchestration skill."""
import os
from unittest.async_case import IsolatedAsyncioTestCase

from agentscope.skill import LocalSkillLoader, Skill

from utils import AnyString, AnyValue

_SKILL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "agent_service",
    "skills",
    "subagent-dag-orchestration",
)


class SubAgentDagSkillTest(IsolatedAsyncioTestCase):
    """The example skill loads with valid frontmatter and targets the tool."""

    async def test_skill_loads_and_targets_the_dag_tool(self) -> None:
        """The skill is discoverable and documents run_subagent_dag."""
        loader = LocalSkillLoader(_SKILL_DIR, scan_subdir=False)
        skills = await loader.list_skills()

        self.assertEqual(len(skills), 1)
        skill = skills[0]
        self.assertEqual(
            skill,
            Skill(
                name="subagent-dag-orchestration",
                description=AnyString(),
                dir=AnyString(),
                markdown=AnyString(),
                updated_at=AnyValue(),
            ),
        )
        self.assertTrue(skill.description.strip())
        self.assertIn("run_subagent_dag", skill.markdown)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `conda run -n ravenx python -m pytest tests/subagent_dag_skill_test.py -v`
Expected: FAIL — `assertEqual(len(skills), 1)` fails (the skill directory does not exist yet, so `list_skills()` returns `[]`).

- [ ] **Step 3: Create the skill file**

Create `examples/agent_service/skills/subagent-dag-orchestration/SKILL.md` with exactly this content:

````markdown
---
name: subagent-dag-orchestration
description: Use whenever a task needs two or more sub-agents, or any
  multi-step sub-agent work where one step's output feeds another. Orchestrate
  them as a single DAG via the run_subagent_dag tool instead of invoking
  sub-agents one by one.
---

# Sub-Agent DAG Orchestration

## When to use this skill

Prefer the `run_subagent_dag` tool for **any task that involves two or more
sub-agents**, or any task where one sub-agent's output feeds another. Express
the whole thing as **one** DAG call rather than invoking sub-agents one at a
time.

Call a single sub-agent tool directly only when the task is genuinely one
sub-agent doing one thing.

Why a DAG:

- **Concurrency** — independent nodes run in parallel automatically.
- **File-based passing** — a node's output is written to a file that
  downstream nodes read; large outputs never pass through your context.
- **Auditability** — every node's output is persisted to a file you can read
  and review, including intermediate steps.

## How it works

You submit a flat list of `nodes`. A scheduler runs every node whose
dependencies are met, as soon as they are met — independent nodes run
concurrently, up to a concurrency cap. Each node's prompt is rendered from its
template, delivered to its sub-agent, and the sub-agent's output is written to
a file under the run directory. Downstream nodes reference upstream outputs
through template placeholders. When the run finishes you receive the
terminal-node outputs inline, plus a list of every node's output file.

## Input format

Call `run_subagent_dag` with a single argument `nodes`: a list of node objects.

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Unique node id. Letters, digits, `_`, `-` only. |
| `subagent` | yes | Name of the sub-agent tool that runs this node. Use a name that exists in your toolkit. |
| `prompt_template` | yes | Template rendered into the node's prompt. May contain the placeholders below. |
| `depends_on` | no | List of upstream node ids that must finish before this node runs. |
| `inputs` | no | Object mapping a key to a literal string, or to `{"file": "<workdir-relative path>"}`. |
| `instance` | no | A stable, semantic handle (e.g. `researcher`) that reuses one stateful sub-agent conversation across nodes. |

### Template placeholders

Inside `prompt_template`:

| Placeholder | Resolves to |
| --- | --- |
| `{{ <id>.output }}` | The upstream node's output **contents**. |
| `{{ <id>.output_path }}` | The upstream node's output file **path**. |
| `{{ inputs.<key> }}` | The input's literal value, or the referenced file's **contents**. |
| `{{ inputs.<key>.path }}` | The input file's **path**. |
| `{{ ref:<path> }}` | The **contents** of an existing workdir file (e.g. an earlier run's output). |
| `{{ ref_path:<path> }}` | That file's **path**. |

Rules:

- **Prefer the `_path` forms for anything large.** They hand the sub-agent a
  path so it reads the file itself — the bytes never enter your context.
- A template may only reference ids listed in that node's `depends_on`, and
  keys listed in its `inputs`.
- All `ref:` / `file` paths must stay inside the session working directory.

## Examples

### Fan-out then aggregate

Two researchers run in parallel; a writer waits for both and synthesizes their
outputs by path:

```json
[
  {
    "id": "research_web",
    "subagent": "claude_code",
    "prompt_template": "Research recent web-framework benchmarks and write your findings to your output."
  },
  {
    "id": "research_papers",
    "subagent": "claude_code",
    "prompt_template": "Summarize the latest papers on async runtimes and write them to your output."
  },
  {
    "id": "synthesize",
    "subagent": "claude_code",
    "depends_on": ["research_web", "research_papers"],
    "prompt_template": "Write a briefing that merges these two sources.\nWeb findings: {{ research_web.output_path }}\nPaper summary: {{ research_papers.output_path }}"
  }
]
```

`research_web` and `research_papers` have no dependencies, so they run at the
same time. `synthesize` runs once both finish; as the terminal node, its output
comes back to you inline.

### Pipeline with a reused stateful instance

A draft is written, reviewed, then revised. The same stateful sub-agent
conversation (`instance: "author"`) carries context through the draft and the
revision:

```json
[
  {
    "id": "draft",
    "subagent": "claude_code",
    "instance": "author",
    "prompt_template": "Draft a 200-word introduction for a report on multi-agent systems."
  },
  {
    "id": "review",
    "subagent": "claude_code",
    "depends_on": ["draft"],
    "prompt_template": "Critique this draft for clarity and accuracy:\n{{ draft.output }}"
  },
  {
    "id": "revise",
    "subagent": "claude_code",
    "instance": "author",
    "depends_on": ["review"],
    "prompt_template": "Revise your earlier draft using this critique:\n{{ review.output }}"
  }
]
```

## Reading the result

The tool returns text like:

```
DAG run <id> complete: {'total': 3, 'completed': 3, 'failed': 0, 'skipped': 0}
Files under: <run-dir>

Node output files:
- research_web [completed]: <run-dir>/research_web.out.md
- research_papers [completed]: <run-dir>/research_papers.out.md
- synthesize [completed]: <run-dir>/synthesize.out.md

## synthesize
<synthesize's output text>
```

- `Node output files:` lists **every** node with its status and output-file
  path (failed or skipped nodes show `(no output file)`).
- The terminal-node outputs are inlined after the list.
- To review any node's work — including intermediate nodes that are not
  inlined — `Read` its output file.

## Rules and anti-patterns

- **Don't** invoke sub-agents one at a time in a loop when a DAG expresses the
  work — build the DAG.
- **Don't** inline large upstream content with `{{ <id>.output }}` when you
  only need to pass it along; use `{{ <id>.output_path }}` so the sub-agent
  reads it directly.
- Every id you reference in a template **must** appear in that node's
  `depends_on`.
- Use `subagent` names that actually exist in your toolkit — don't invent them.
````

- [ ] **Step 4: Run the test to verify it passes**

Run: `conda run -n ravenx python -m pytest tests/subagent_dag_skill_test.py -v`
Expected: PASS — the skill loads as exactly one `Skill` named `subagent-dag-orchestration` with a non-empty description, and its body contains `run_subagent_dag`.

- [ ] **Step 5: Pre-commit + commit**

```bash
pre-commit run --files examples/agent_service/skills/subagent-dag-orchestration/SKILL.md tests/subagent_dag_skill_test.py
git add examples/agent_service/skills/subagent-dag-orchestration/SKILL.md tests/subagent_dag_skill_test.py
git commit -m "$(cat <<'EOF'
feat(examples): add subagent-dag-orchestration skill + discovery test

A SKILL.md guiding the leader agent to prefer run_subagent_dag whenever a
task spans two or more sub-agents, with the node schema, placeholder
reference, two worked examples, and result-reading guidance. Covered by a
discovery test asserting the skill loads and targets the tool.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Wire the skill into the reference service and document it

**Files:**
- Modify: `examples/agent_service/main.py` (the `LocalWorkspaceManager(...)` call)
- Modify: `examples/agent_service/README.md`

**Interfaces:**
- Consumes: the skill directory created in Task 1; `LocalWorkspaceManager.__init__(..., skill_paths: list[str] | None = None)` — skill directories seeded into new workspaces.
- Produces: the reference service surfaces the skill to its leader agent.

- [ ] **Step 1: Add the skills-dir constant and wire `skill_paths`**

In `examples/agent_service/main.py`, the current `workspace_manager` is built like this:

```python
workspace_manager = LocalWorkspaceManager(
    basedir=os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "workspaces",
    ),
    isolation=IsolationPolicy.PER_SESSION,
    # The default MCP servers that will be added into the workspace
    default_mcps=default_mcps,
)
```

Replace it with (add the `_SKILLS_DIR` constant immediately above the call, and the `skill_paths` argument):

```python
# Skill directories seeded into every session workspace, so the leader
# agent can discover and read them via the builtin Skill viewer tool.
_SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "skills",
)

workspace_manager = LocalWorkspaceManager(
    basedir=os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "workspaces",
    ),
    isolation=IsolationPolicy.PER_SESSION,
    # The default MCP servers that will be added into the workspace
    default_mcps=default_mcps,
    # Seed the sub-agent DAG orchestration skill so the leader agent is
    # guided to prefer run_subagent_dag for multi-sub-agent tasks.
    skill_paths=[
        os.path.join(_SKILLS_DIR, "subagent-dag-orchestration"),
    ],
)
```

- [ ] **Step 2: Verify `main.py` still parses**

Run: `conda run -n ravenx python -m py_compile examples/agent_service/main.py`
Expected: exit 0, no output (the module compiles; a full import needs Redis, so do not run it).

- [ ] **Step 3: Add a README section**

In `examples/agent_service/README.md`, insert this new section immediately before the `## What Next` line:

```markdown
## Sub-agent DAG skill

This service seeds one example [skill](./skills/subagent-dag-orchestration/SKILL.md)
into every session workspace via the workspace manager's `skill_paths`. It
guides the leader agent to prefer the `run_subagent_dag` tool — orchestrating
sub-agents as a single DAG with file-based message passing — whenever a task
spans two or more sub-agents, instead of invoking them one at a time. Drop more
skill directories under `skills/` and add them to `skill_paths` in `main.py` to
extend the leader agent's guidance.

```

- [ ] **Step 4: Confirm Task 1's test still passes**

Run: `conda run -n ravenx python -m pytest tests/subagent_dag_skill_test.py -v`
Expected: PASS (unchanged — the skill directory and its path are consistent with the wiring).

- [ ] **Step 5: Pre-commit + commit**

```bash
pre-commit run --files examples/agent_service/main.py examples/agent_service/README.md
git add examples/agent_service/main.py examples/agent_service/README.md
git commit -m "$(cat <<'EOF'
feat(examples): seed the subagent-dag-orchestration skill in the service

Wire the skill directory into LocalWorkspaceManager.skill_paths so the leader
agent discovers it, and document it in the README.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] Run the discovery test once more: `conda run -n ravenx python -m pytest tests/subagent_dag_skill_test.py -v` — green.
- [ ] `conda run -n ravenx python -m py_compile examples/agent_service/main.py` — exit 0.
- [ ] `git diff --stat main` shows only: the spec, this plan, `SKILL.md`, `main.py`, `README.md`, and the test file.

## Notes for the executor

- Do NOT change any `src/` file. This is an example skill plus its wiring and a test.
- Do NOT import `main.py` in a test (it calls `create_app`, which needs Redis); `py_compile` is the syntactic gate.
- Do NOT stage `.webapp_logs/*.pid`. Never `git add -A`.
