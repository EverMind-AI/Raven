# Sub-Agent DAG Orchestration — Design Spec

- **Date:** 2026-07-17
- **Status:** Approved (design); pending implementation plan
- **Scope target:** `src/agentscope/subagent/` (single main-agent path)
- **Branch:** `feat/subagent-dag-orchestration`

## 1. Goal & context

RavenX today can only invoke sub-agents in a **star topology**: the main agent
calls one sub-agent at a time via `CliSubAgentTool`, and each result is piped
straight back into the main agent's context. There is no way for the main agent
to orchestrate a **DAG** of sub-agents, and no way for one sub-agent's output to
flow to another without passing through the main agent's context.

This spec introduces a **file-based message-passing DAG orchestrator** that lets
the main agent describe a graph of sub-agent tasks, runs that graph with a
deterministic ready-set scheduler, and passes messages between nodes through
**local files in the session workspace** (not through the main agent's context).

The design realizes the user's seven principles:

1. Sub-agent I/O is carried through local log files (reuse + reduced context).
2. The main agent writes each task prompt to a local file and invokes the
   sub-agent pointing at that file.
3. In the DAG, an upstream (producer) node writes its output to a log file; the
   system reads it automatically and feeds it to the downstream (consumer) node.
4. A node's input is a **template** (authored by the main agent during
   orchestration), supporting richer prompts and combining **multiple** upstream
   outputs.
5. Normally the DAG's terminal node output returns to the main agent, back into
   the session main loop for display (main agent is the graph's *sink*).
6. After the DAG finishes, the **full list of all sub-agent output files** is
   returned to the main agent.
7. A node's input may directly reference **prior-run** sub-agent output files,
   for efficient reuse across rounds.

### Terminology

- **Node** — one sub-agent invocation (a named `CliSubAgent` + a rendered prompt).
- **Edge** — a `depends_on` relation; upstream (producer) → downstream (consumer).
- **Terminal node** — a node with no downstream dependents (a graph sink).
- **Run** — one invocation of the orchestrator over one graph, with a `run_id`.

## 2. Non-goals (out of scope for v1)

- The `app/` multi-session team path (message-bus DAG). Deferred to a later spec.
- Main agent as a **literal** graph vertex ("path B"). v1 uses the sink model
  ("path A"): the main agent stays outside the scheduled graph.
- Runtime graph rewriting / failure-driven re-decomposition (CAMEL-style
  `RecoveryStrategy.DECOMPOSE`). v1 plans the graph once and does not mutate
  nodes/edges mid-run.
- Checkpoint / replay artifacts (OMA-style `PlanArtifact`). Nice-to-have later.
- **CLI-authored** output artifacts (a `{output_file}` placeholder passed *into*
  the CLI so the sub-agent writes its own result file). v1 always captures the
  sub-agent's **stdout**. (Distinct from §5.2's `output_file` *tool* parameter,
  which is merely where the tool persists the captured stdout on disk.) The tool
  API is designed to allow CLI-authored artifacts later.
- Any change to the ReAct loop in `agent/_agent.py`.

## 3. Chosen approach & rejected alternatives

**Chosen:** a new DAG orchestration sub-package under `subagent/` exposing a
single orchestration `ToolBase` that the main agent calls. It reuses the existing
`CliSubAgentTool` as the per-node executor, and passes all inter-node messages
through files on the **session workspace backend**. The main agent is the graph's
sink (path A).

**Rejected / deferred:**

- **Middleware that intercepts the ReAct loop to run DAGs** — rejected: violates
  the project convention that middlewares wrap phases rather than act as a
  scheduler; too invasive.
- **`app/` team message-bus DAG (multi-session)** — deferred by scope decision;
  larger blast radius.
- **Main agent as a real graph vertex (path B)** — deferred; requires a scheduler
  that owns inter-node edge traversal into the main agent.

## 4. Architecture

### 4.1 Artifacts & directory layout (principles 1/2/3)

All message files are written under the session `workspace.workdir`, in a
run-scoped directory, **through `backend.write_file` / `backend.read_file`** so
it works for Local / Docker / E2B / remote backends and the CLI sub-agents see
the same files via their `cwd`:

```
<workdir>/.ravenx_dag/<run_id>/
  graph.json          # the submitted DAG spec (audit / reuse)
  manifest.json       # node -> {status, subagent, prompt_file, output_file, deps, ts}
  <node_id>.prompt.md # orchestrator-rendered prompt (principle 2)
  <node_id>.out.md    # captured stdout, FULL / untruncated (principle 3)
```

`run_id = <UTC-timestamp>-<short-uuid>`. Files persist to enable cross-run reuse
(principle 7). A session-level `.ravenx_dag/index.json` appends one entry per run
so prior runs are discoverable.

### 4.2 DAG spec & template vocabulary (principles 2/4/7)

The main agent calls the orchestration tool (public name `run_subagent_dag`) with
a graph spec:

```jsonc
{
  "nodes": [
    {
      "id": "collect",
      "subagent": "researcher",
      "instance": "r1",                       // optional; stateful CLI handle
      "inputs": {"topic": "DAG orchestration"},
      "prompt_template": "Research {{inputs.topic}} and output key points"
    },
    {
      "id": "analyze",
      "subagent": "analyst",
      "depends_on": ["collect"],
      "inputs": {"spec": {"file": "docs/design.md"}},
      "prompt_template": "Per spec:\n{{ inputs.spec }}\nAnalyze:\n{{ collect.output }}"
    },
    {
      "id": "merge",
      "subagent": "writer",
      "depends_on": ["analyze", "collect"],
      "prompt_template": "Summarize:\n{{ analyze.output }}\nRaw file: {{ collect.output_path }}"
    }
  ]
}
```

**Node fields:** `id` (unique), `subagent` (must match a session
`CliSubAgentTool` name), `prompt_template` (required), `depends_on` (optional list
of node ids), `inputs` (optional map), `instance` (optional stateful handle).

**`inputs` entry** is either an inline literal string **or** a file reference
`{"file": "<path>"}` (read via `backend.read_file`, path resolved against
cwd/workdir). This is the main agent's natural "feed a file" entry point
(principle 2's file-passing extended to inputs).

**Template placeholders** (rendered by the orchestrator before dispatch, written
to `<node>.prompt.md`):

| Placeholder | Expands to |
|---|---|
| `{{ inputs.<key> }}` | inline literal, or **contents** of a file input |
| `{{ inputs.<key>.path }}` | the **path** of a file input (CLI reads it itself) |
| `{{ <dep>.output }}` | **contents** of dependency `<dep>`'s output file |
| `{{ <dep>.output_path }}` | **path** of dependency `<dep>`'s output file |
| `{{ ref:<path> }}` | inline **contents** of any workdir file (incl. prior run) |
| `{{ ref_path:<path> }}` | the **path** itself (incl. prior run) |

Combining multiple upstream outputs (principle 4): list several ids in
`depends_on` and reference each — `{{ a.output }} ... {{ b.output }}`.

**Default-deny (borrowed from the research):** `{{ <dep>.output }}` is only valid
when `<dep>` is declared in this node's `depends_on`. Referencing an undeclared
dependency, or a missing file, is a validation error.

**Content vs path injection** is the main context-economy lever: `*.output`
inlines text (downstream CLI sees it directly); `*.output_path` inlines only the
path (downstream CLI reads the file itself — cheapest). The main agent chooses
per placeholder when authoring the template.

### 4.3 Execution engine (ready-set scheduler; borrowed from OMA)

Inside the orchestration tool:

1. **Validate** the graph: unique ids; every `depends_on` resolves; the graph is
   **acyclic** (topological check — reject cycles); every `subagent` exists among
   the session's resolved `CliSubAgentTool`s; every referenced sub-agent's
   command template contains `{prompt_file}`; every template placeholder is
   well-formed and obeys default-deny.
2. **Create** `.ravenx_dag/<run_id>/` via the backend; write `graph.json`.
3. **Ready-set loop:** `ready` = nodes whose dependencies are all `completed`.
   For each ready node (bounded by concurrency limits): render its prompt →
   `write_file(<node>.prompt.md)` → call the sub-agent tool with
   `prompt_file=<path>` and `output_file=<path>` → the tool writes full stdout to
   `<node>.out.md` → mark `completed`, update `manifest.json`. Recompute `ready`
   until none remain.
4. **Concurrency:** a global `asyncio.Semaphore(max_concurrency)` caps in-flight
   CLI processes; a per-instance `asyncio.Semaphore(1)` serializes reuse of the
   same stateful `instance` (avoids resume races).
5. **Failure:** a node error → status `failed`; **cascade** its transitive
   dependents to `skipped` (reason `upstream_failed`) so nothing hangs;
   independent branches continue (graceful partial completion). No runtime graph
   rewrite.

### 4.4 Information flow, context economy & main-agent sink (principles 1/3/5/6)

- **Node → node:** upstream output is written to a file; the orchestrator renders
  the downstream prompt by injecting either the file's contents or its path per
  the template. **Intermediate node outputs never enter the main agent's
  context** — the concrete realization of principle 1, versus today's per-call
  stdout dump into context.
- **Main agent as sink (principles 5/6):** when the DAG completes, the tool
  returns to the main agent:

  ```jsonc
  {
    "run_id": "...",
    "dir": "<workdir>/.ravenx_dag/<run_id>",
    "terminal_outputs": [{"node": "merge", "text": "<FULL, untruncated>"}],
    "files": [{"node": "collect", "status": "completed",
               "prompt_file": "...", "output_file": "..."}, ...],
    "summary": {"total": 3, "completed": 3, "failed": 0, "skipped": 0}
  }
  ```

  `terminal_outputs` carries the **full, untruncated** text of each terminal
  (sink) node, read directly from its `<node>.out.md`, so the main agent can
  display the final result in the session main loop. `files` is the full list of
  every node's output file (principle 6). Intermediate outputs are referenced by
  path only, keeping them out of context.

### 4.5 Cross-run reuse (principle 7)

Output files persist under `.ravenx_dag/<run_id>/`. Because the main agent
received the file list (principle 6), a later DAG can reference any prior file via
`{{ ref:<run_id>/<node>.out.md }}` or feed it as a file `input`. `index.json`
makes prior runs discoverable.

## 5. Code changes & integration points

### 5.1 New sub-package `src/agentscope/subagent/_dag/`

All files `_`-prefixed; public names exposed via `subagent/__init__.py`.

- `_graph.py` — the `SubAgentDagSpec` / node models (pydantic), `from_dict`
  parsing, and structural validation (unique ids, dependency resolution,
  acyclicity via topological sort, placeholder well-formedness, default-deny).
- `_render.py` — template rendering implementing the §4.2 placeholder vocabulary,
  reading files via the backend; default-deny enforcement.
- `_store.py` — run directory creation, prompt/out file writes, `manifest.json`
  and `index.json` read/write, `run_id` generation — all via the backend.
- `_runner.py` — the ready-set scheduler: concurrency semaphores, dispatch,
  stdout→file capture wiring, failure cascade, manifest updates.
- `_tool.py` — `SubAgentDagTool(ToolBase)` with public tool name
  `run_subagent_dag`; `input_schema` = the graph spec; `call()` drives the runner
  and yields a terminal `ToolChunk` (a text summary + terminal outputs; structured
  result in `metadata`).

### 5.2 Extend `src/agentscope/subagent/_tool.py` (`CliSubAgentTool`)

Additive, backward-compatible:

- `_build_argv` — support a `{prompt_file}` placeholder token (alongside
  `{prompt}` / `{agent_id}`).
- `call()` — add optional `prompt_file: str | None` and `output_file: str | None`:
  - When the command template uses `{prompt_file}`: substitute `prompt_file` if
    given; otherwise write the `prompt` string to a temp file under `cwd` via the
    backend and substitute that path.
  - When `output_file` is given: write the **full, untruncated** stdout to that
    file via `backend.write_file`.
  - The in-context returned `ToolChunk` text is still bounded by
    `_MAX_OUTPUT_CHARS`.
- `_MAX_OUTPUT_CHARS`: **30000 → 128000** (bumps the in-context return cap for all
  sub-agent calls; the on-disk `.out.md` is always full regardless).
- Existing `{prompt}`-only configs and call sites are untouched.

### 5.3 Wire in `src/agentscope/subagent/_agent_tools.py`

`make_subagent_tool_factory` builds the `CliSubAgentTool` list as today, then also
constructs and appends a `SubAgentDagTool`, passing it the `{name: CliSubAgentTool}`
map, the resolved `backend`, and the `session_workdir`. No ReAct-loop or `app/`
team changes.

### 5.4 Config / backward compatibility

- `CliSubAgentConfig`'s command validators (`_base.py:103-189`) currently *require*
  `{prompt}`. They are relaxed to accept **`{prompt}` or `{prompt_file}`** (at least
  one prompt-delivery placeholder), so a `{prompt_file}`-only DAG config is valid
  while every existing `{prompt}` config still passes. The DAG runner separately
  enforces that a DAG node's sub-agent command contains `{prompt_file}`.
- All existing single-Call Sub-Agent behavior is preserved (additive-only).

### 5.5 Backend file-I/O constraint (verified)

`BackendBase` exposes `read_file` / `write_file` / `join_path` / `file_exists` /
`is_dir` / `list_dir` / `delete_path`
(`src/agentscope/tool/_builtin/_backend.py:288-482`). The orchestrator uses these
exclusively (never local `open()`), so message files live in the same filesystem
the CLI sub-agents see via `cwd`, across all backend types.

## 6. Testing strategy (TDD; whole-structure assertions)

- **Unit — validation** (`_graph.py`): cycle detection, unknown `subagent`,
  missing dependency, undeclared-dependency reference (default-deny), missing
  `{prompt_file}` in a template, malformed placeholder.
- **Unit — rendering** (`_render.py`): every placeholder form (`inputs.*`,
  `inputs.*.path`, `<dep>.output`, `<dep>.output_path`, `ref:*`, `ref_path:*`),
  file inputs, combining multiple upstream outputs, default-deny rejection.
- **Unit — store** (`_store.py`): prompt/out/manifest/index read-write over a
  `LocalBackend` in a tmp workdir; `run_id` uniqueness.
- **Unit — scheduler** (`_runner.py`): ready-set ordering, bounded concurrency,
  per-instance serialization, failure cascade to dependents (partial completion).
- **Unit — `CliSubAgentTool`**: `{prompt_file}` substitution; `output_file` writes
  full untruncated stdout; 128k in-context cap; `{prompt}` back-compat unchanged.
- **Integration:** a fake CLI sub-agent (e.g. a tiny script that reads
  `{prompt_file}` and echoes) driven through a diamond DAG (A → B, C → D);
  assert the **whole** result structure (`run_id`/timestamps via `AnyString` /
  `AnyValue` from `tests/utils.py`), and assert the on-disk files exist with full
  content.

Assertions compare whole data structures per project convention.

## 7. Conventions & risks

- **Additive-only / back-compat:** all changes are additive; existing sub-agent
  call paths are unchanged (`{prompt}` path intact).
- **Encapsulation:** internals `_`-prefixed; public surface via
  `subagent/__init__.py`.
- **Lazy imports:** no new third-party deps expected; if any are added they go to
  the correct `pyproject.toml` extra and are imported at point of use.
- **Docstrings:** English, strict `Args:` / `Returns:` template with backtick
  types.
- **Determinism:** `run_id` uses a timestamp + uuid, so runs are not bit-for-bit
  reproducible; tests use `AnyString` for such fields and assert structure.
- **Security / concurrency:** prompts are delivered by file (no shell-metachar
  injection, consistent with the current no-shell argv design); per-instance
  serialization prevents stateful-resume races; the global semaphore caps
  in-flight processes.

## 8. Open questions

None blocking. Future work: cooperative `{output_file}` artifacts, checkpoint /
replay, the `app/` team path, and main-agent-as-literal-node (path B).
