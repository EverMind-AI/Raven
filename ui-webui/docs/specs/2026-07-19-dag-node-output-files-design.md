# DAG per-node output-file list to the main agent — design

**Date:** 2026-07-19
**Status:** Approved (design)
**Builds on:** the sub-agent DAG orchestrator (`src/agentscope/subagent/_dag/`).

## 1. Motivation

After a DAG run, `SubAgentDagTool.call` returns a result whose readable text
(`content`) shows only the run summary, the run directory, and the completed
**terminal (sink)** nodes' outputs. The full per-node file list lives only in
the tool result's `metadata`, which is not serialized into the model input
(formatters read `block.output`, never `block.metadata`). So the main agent's
LLM cannot see where each node's output was written and cannot browse or review
intermediate (non-terminal) nodes' outputs — even though every node's output is
already persisted on disk.

## 2. Scope

**In scope:** add a per-node output-file list to the DAG tool result's readable
text, so the main agent can `Read` any node's output for review.

**Out of scope / unchanged:** the runner, the store, the on-disk file layout,
the manifest, and the result `metadata` (it already carries `files`). No new
files are written — this only surfaces existing paths to the LLM.

## 3. Change

In `SubAgentDagTool.call` (`_dag/_tool.py`), after the `Files under: <dir>`
line and before the inline terminal-node outputs, add a section:

```
Node output files:
- <node> [<status>]: <output_file>
- ...
```

- Iterate `result.files` (already ordered by `spec.nodes`; each entry is
  `{"node", "status", "prompt_file", "output_file"}`).
- Render `- <node> [<status>]: <output_file>`. When `output_file` is falsy
  (a `failed`/`skipped` node has none), render `(no output file)` in its place.
- All nodes are listed, including `failed`/`skipped`, so their status is
  visible for review.
- The existing inline terminal-node outputs (`## <node>\n<text>`) are kept
  after this section.

`metadata` (including `files`) is unchanged. No change to `_runner.py`,
`_store.py`, the manifest, or the file layout.

Resulting text the LLM reads:

```
DAG run <id> complete: {'total': 4, 'completed': 4, 'failed': 0, 'skipped': 0}
Files under: <run-dir>

Node output files:
- A [completed]: <run-dir>/A.out.md
- B [completed]: <run-dir>/B.out.md
- C [completed]: <run-dir>/C.out.md
- D [completed]: <run-dir>/D.out.md

## D
<D's output text>
```

## 4. Testing

Extend `tests/subagent_dag_tool_test.py` (`test_runs_two_node_chain` or a new
test): assert the result `content` text now contains the per-node list — for a
two-node A→B chain, both `A`/`B` appear with `[completed]` and their
`output_file` path. The existing `metadata` assertions (`summary`,
`terminal_outputs`) stay unchanged.

## 5. Files touched

- `src/agentscope/subagent/_dag/_tool.py` — the `call` text assembly.
- `tests/subagent_dag_tool_test.py` — text-content assertion.

## 6. Decisions

1. **List fields = `status` + `output_file`** (not `prompt_file`) — the review
   need is "where is each node's output"; keep the line compact. (2026-07-19)
2. **Keep the inline terminal outputs; add the list** — the sink result stays
   immediately visible while the full file list enables browsing every node.
   (2026-07-19)
3. **Include failed/skipped nodes** with their status and `(no output file)` —
   review wants to see which nodes did not produce output. (2026-07-19)
