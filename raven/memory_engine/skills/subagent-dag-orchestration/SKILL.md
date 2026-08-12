---
name: subagent-dag-orchestration
description: Use whenever a task needs two or more sub-agents, or a multi-step pipeline where one sub-agent's output feeds another. Orchestrate the whole graph as a single run_subagent_dag call (parallel fan-out, file-based passing) instead of dispatching sub-agents one at a time.
metadata: {"raven":{"emoji":"🕸️","always":true,"inject":"description","requires":{"tools":["run_subagent_dag"]}}}
---

# Sub-Agent DAG Orchestration

## When to use

Prefer the `run_subagent_dag` tool for **any task that needs two or more sub-agent
runs**, or where one sub-agent's output feeds another. Express the whole thing as
**one** DAG call rather than dispatching sub-agents one at a time.

Call a single sub-agent (`spawn`) directly only when the task is genuinely one
sub-agent doing one thing.

Why a DAG:

- **Concurrency** — independent nodes run in parallel automatically (up to 5 at a time).
- **File-based passing** — a node's output is written to a file downstream nodes read;
  large outputs never pass through your context.
- **Auditability** — every node's prompt and output is persisted, including intermediate
  steps you can read afterwards.

`run_subagent_dag` is only registered when third-party sub-agents are configured
(`subagents.third_party`). If the tool is absent, there is nothing for DAG nodes to
dispatch to — fall back to `spawn`.

## How it works

You submit a flat list of `nodes`. A scheduler runs every node whose dependencies are
met, as soon as they are met. Each node's prompt is rendered from its template,
dispatched to its sub-agent, and the reply is written to a file under the run
directory. Downstream nodes reference upstream outputs through template placeholders.
When the run finishes you get the terminal-node outputs inline plus every node's
output-file path.

A DAG run has no timeout — a long run is ended by hand (manual stop), not by a clock.

## Input format

Call `run_subagent_dag` with a single argument `nodes`: a list of node objects.

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Unique node id. Letters, digits, `_`, `-` only. |
| `subagent` | yes | Name of a configured third-party sub-agent. Use one of the names listed in the tool's own description — don't invent them. |
| `prompt_template` | yes | Template rendered into the node's prompt. May contain the placeholders below. |
| `depends_on` | no | Upstream node ids that must finish before this node runs. |
| `inputs` | no | Object mapping a key to a literal string, or to `{"file": "<workspace-relative path>"}`. |
| `instance` | no | A stable handle (e.g. `researcher`). Nodes sharing it run sequentially in id order and reuse one sub-agent session — only on an agent the roster tags `[stateful]`. |

### Sub-agent capability tags

The tool description tags every available sub-agent, and both tags are checked
before any node is dispatched — a graph that violates one is rejected whole, with
nothing run:

- `[stateful]` / `[stateless]` — only a `[stateful]` agent carries context across
  nodes that share an `instance` handle. Sharing a handle on a `[stateless]` agent
  is rejected: there it would only serialize the nodes while each still starts from
  scratch. Pass what the later node needs through a placeholder instead. `spawn`
  refuses an `instance` on a `[stateless]` agent for the same reason.
- `[local-files]` / `[no-local-files]` — a `[no-local-files]` agent runs where this
  filesystem is not, so handing it a path is rejected. Give it the *contents*
  (`{{ <dep>.output }}`, `{{ inputs.<k> }}`, `{{ ref:<path> }}`) instead of the
  `_path` forms.

### Template placeholders

Inside `prompt_template`:

| Placeholder | Resolves to |
| --- | --- |
| `{{ <id>.output }}` | The upstream node's output **contents**. |
| `{{ <id>.output_path }}` | The upstream node's output file **path**. |
| `{{ inputs.<key> }}` | The input's literal value, or the referenced file's **contents**. |
| `{{ inputs.<key>.path }}` | The input file's **path**. |
| `{{ ref:<path> }}` | The **contents** of an existing workspace file (e.g. an earlier run's output). |
| `{{ ref_path:<path> }}` | That file's **path**. |

Rules:

- **Prefer the `_path` forms for anything large.** They hand the sub-agent a path so it
  reads the file itself — the bytes never enter your context. Only for an agent tagged
  `[local-files]`; a `[no-local-files]` one has to get the contents form.
- A template may only reference ids listed in that node's `depends_on`, and keys listed
  in its `inputs`.
- `ref:` / `ref_path:` / `{"file": ...}` paths must be relative and stay inside the
  session working directory; absolute or escaping paths are rejected.

## Examples

The `subagent` values below are placeholders — substitute the names the tool reports
as available.

### Fan-out then aggregate

Two researchers run in parallel; a writer waits for both and reads their outputs by path:

```json
[
  {
    "id": "research_web",
    "subagent": "claude_code",
    "prompt_template": "Research recent web-framework benchmarks and report your findings."
  },
  {
    "id": "research_papers",
    "subagent": "claude_code",
    "prompt_template": "Summarize the latest papers on async runtimes."
  },
  {
    "id": "synthesize",
    "subagent": "claude_code",
    "depends_on": ["research_web", "research_papers"],
    "prompt_template": "Write a briefing merging these two sources.\nWeb findings: {{ research_web.output_path }}\nPaper summary: {{ research_papers.output_path }}"
  }
]
```

`research_web` and `research_papers` have no dependencies, so they run at the same
time. `synthesize` runs once both finish; as the terminal node, its output comes back
to you inline.

### Pipeline with a reused stateful instance

A draft is written, reviewed, then revised. The same session (`instance: "author"`)
carries context from the draft into the revision. This shape needs an agent tagged
`[stateful]`; on a `[stateless]` one the graph is rejected before anything runs:

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
DAG run 20260729T031500Z-1a2b3c4d finished: 3 completed, 0 failed, 0 skipped (of 3).
Run dir: <session history>/mas_dag/20260729T031500Z-1a2b3c4d

Node output files:
- research_web [completed]: <run-dir>/research_web.out.md
- research_papers [completed]: <run-dir>/research_papers.out.md
- synthesize [completed]: <run-dir>/synthesize.out.md

Terminal outputs:
### synthesize
<synthesize's output text>
```

- `Node output files:` lists **every** node with its status and output path (failed or
  skipped nodes show `(no output file)` plus an `error:` line).
- Only terminal-node outputs are inlined. To review any other node's work, `read_file`
  its output path.
- The run dir also holds `<node>.prompt.md` (the rendered prompt), `graph.json`, and
  `manifest.json` — useful when a node's answer looks wrong and you need to see what it
  was actually asked.

## Anti-patterns

- **Don't** dispatch sub-agents one at a time in a loop when a DAG expresses the work.
- **Don't** inline large upstream content with `{{ <id>.output }}` when you only need to
  pass it along; use `{{ <id>.output_path }}` — unless the agent is `[no-local-files]`.
- **Don't** reference an id in a template without listing it in that node's `depends_on`.
- **Don't** guess `subagent` names — only configured ones resolve.
- **Don't** share an `instance` handle across nodes to "keep them in order" on a
  `[stateless]` agent — order without shared context is what `depends_on` already gives.
