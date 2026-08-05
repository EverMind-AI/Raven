# Unified file-based prompt delivery — design

**Date:** 2026-07-19
**Status:** Approved (design)
**Builds on:** the CLI sub-agent layer (`src/agentscope/subagent/`), the DAG orchestrator (`_dag/`), and `2026-07-18-subagent-handle-codex-refinements-design.md`.

## 1. Motivation

The DAG orchestrator rejects any node whose sub-agent command does not contain
`{prompt_file}`:

```
Invalid DAG: sub-agent 'claude_code' must use '{prompt_file}' to participate in a DAG
```

The built-in Claude Code and Codex presets deliver the prompt inline via
`{prompt}` (that is what those CLIs accept), so they can never be DAG nodes —
which defeats the point of orchestrating them. Today there are two competing
prompt-delivery placeholders (`{prompt}` inline vs `{prompt_file}` path-to-CLI)
and a DAG guard that forces the second, even though most real CLIs only take
inline text.

This design collapses the two mechanisms into one: **the prompt always travels
as a local file, and the tool reads that file and inlines its contents into
`{prompt}`.** Commands use only `{prompt}`. `{prompt_file}` is removed.

## 2. Scope

**In scope:** unify prompt delivery on `{prompt}` + tool-side file read; remove
`{prompt_file}` and the DAG `command_uses_prompt_file` guard; route every
sub-agent call (direct and DAG) through a prompt file; update validation, the
DAG runner, and the affected tests.

**Out of scope / unchanged:** the instance registry, factory wiring, presets
(already `{prompt}`-only), router schemas, `id_source`/`transcript_format` work,
and the DAG template-placeholder grammar (`{{ dep.output }}`, `{{ inputs.x }}`,
`{{ ref:… }}` — these are node-prompt templating, unrelated to `{prompt_file}`).

## 3. Core model

One prompt mechanism:

- Sub-agent command templates contain **`{prompt}`** (and, when stateful,
  `{agent_id}`). No `{prompt_file}`.
- The tool guarantees the prompt is materialized as a local file, reads the
  file, and substitutes its contents into `{prompt}`.
- Direct main-agent calls (inline prompt from the LLM) and DAG calls (prompt
  file written by the runner) converge on the same invariant: **the `{prompt}`
  value always comes from reading a file.**

## 4. Runtime — `CliSubAgentTool.call`

Replace the current `{prompt_file}`-substitution block with a materialize →
read → inline pipeline:

```python
# Materialize the prompt as a file (single source of truth + audit trail).
# Direct calls persist the inline prompt; DAG calls already have a file.
try:
    if prompt_file is None:
        prompt_file = await self._write_temp_prompt(prompt)
    prompt_text = (await self._backend.read_file(prompt_file)).decode("utf-8")
except Exception as exc:  # noqa: BLE001
    yield ToolChunk(
        content=[TextBlock(text=f"Sub-Agent prompt file error: {exc}")],
        state=ToolResultState.ERROR,
        is_last=True,
    )
    return
argv = self._build_argv(prompt_text, template, agent_id)
```

Notes:
- `_write_temp_prompt` **stays** and is now used for every direct call (this is
  the audit file the user asked to keep). Its `.ravenx_prompt_<uuid>.md`
  filename is unchanged.
- The `prompt` parameter of `call()` remains (required by the tool's input
  schema for direct calls). When a `prompt_file` is supplied (DAG path), the
  file is authoritative and the inline `prompt` argument is not used.
- The materialize+read occupies the same position as today's temp-prompt
  write: after the create/resume branch has determined `template` and
  `agent_id`, immediately before building argv and executing. The write and
  the read are both guarded by the same `try`, so a write failure (e.g. an
  unwritable cwd or a full disk) surfaces as a terminal ERROR chunk (same
  shape as the other early-exit errors) rather than an uncaught exception,
  and, occurring before exec, leaves no created/committed state.

## 5. Runtime — `_build_argv`

Drop the `{prompt_file}` substitution. The loop substitutes `{prompt}` (with
the file contents) and `{agent_id}` only. The `prompt_file` parameter is
removed from `_build_argv`'s signature. The env-prefix logic is unchanged, and
the prompt still occupies a single argv token (no shell), preserving the
injection-safety property.

## 6. Runtime — remove `command_uses_prompt_file`

Delete the `command_uses_prompt_file` property from `CliSubAgentTool`. Its only
consumer is the DAG guard (removed in §7).

## 7. DAG runner — remove the guard

In `_dag/_runner.py`, delete the per-node check that raises
`sub-agent '…' must use '{prompt_file}' to participate in a DAG`
(and its mention in the `run_dag` docstring's Raises section). Every valid
config now contains `{prompt}`, and the tool reads the node's prompt file, so
no sub-agent-shape guard is needed — the config-level validation is sufficient.

Unchanged in the runner: the runner still renders each node's prompt, writes it
to the node's `prompt_path`, passes that path as `tool.call(prompt_file=…)`, and
records `prompt_file` in the per-node manifest/`files` list (the file transport
and audit path).

## 8. Config validation — `CliSubAgentConfig` (`_base.py`)

Remove every `{prompt_file}` acceptance; require `{prompt}`:

- `_require_prompt_placeholder`: require `{prompt}` (was `{prompt}` OR
  `{prompt_file}`). Update the error message.
- `_require_literal_executable`: the first-token guard checks only for
  `{prompt}`.
- `_validate_stateful`: both `command` and `resume_command` must contain
  `{prompt}` (was either placeholder); the first-token checks reference only
  `{prompt}`.
- Field docstrings / descriptions on `command` and `resume_command` (and the
  class docstring) drop all `{prompt_file}` wording.

**Breaking edge (accepted):** an existing *stored* prototype whose command uses
`{prompt_file}` now fails `from_dict` validation; the tool factory already
skips malformed configs with a logged warning, so it degrades gracefully. No
built-in preset is affected (all use `{prompt}`).

## 9. Testing

- **`subagent_config_test.py`**: `test_prompt_file_placeholder_is_accepted` and
  `test_stateful_prompt_file_placeholder_is_accepted` flip to assert
  `{prompt_file}`-only commands are now **rejected** (`ValidationError`).
- **`subagent_tool_test.py`**: rework `CliSubAgentToolPromptFileTest` into the
  new model:
  - direct call (no `prompt_file`) writes a temp prompt file and the CLI
    receives the file's contents as `{prompt}` (assert the temp file was
    written AND the argv carries the prompt text);
  - a supplied `prompt_file` is read and its contents become `{prompt}` (assert
    argv carries the file contents, not the path);
  - drop `command_uses_prompt_file` assertions;
  - keep the `output_file` full-write + 128k truncation tests (adapted to a
    `{prompt}` command).
- **`subagent_dag_tool_test.py`** and **`subagent_dag_runner_test.py`**: their
  `_FakeSubAgent`/fake tools drop the now-removed `command_uses_prompt_file`
  attribute (they read `prompt_file` directly to produce output, which still
  models a sub-agent; the removed attribute is simply no longer referenced).
- **`subagent_dag_integration_test.py`**: change the real-CLI command from
  `cat {prompt_file}` to `printf %s {prompt}` (echoes the inlined prompt so the
  diamond-DAG chaining assertions still hold).
- **New DAG test**: a `{prompt}`-only sub-agent (Claude/Codex-shaped, e.g.
  `printf %s {prompt}`) runs successfully as a DAG node — the regression that
  motivated this change (previously `Invalid DAG: … must use '{prompt_file}'`).
- **Frontend**: confirm the `/subagents` command-field validation already
  requires `{prompt}` (it does) and offers no `{prompt_file}` affordance to
  remove; no frontend change expected. If a `{prompt_file}` hint exists, remove
  it.

## 10. Files touched

- `src/agentscope/subagent/_base.py` — validation + docstrings.
- `src/agentscope/subagent/_tool.py` — `call()` pipeline, `_build_argv`, remove
  `command_uses_prompt_file` (keep `_write_temp_prompt`).
- `src/agentscope/subagent/_dag/_runner.py` — remove the guard + docstring line.
- Tests in §9.

No change to: the instance registry, `_agent_tools.py` factory, `_presets.py`,
router routes/schemas, or the `_dag/_graph.py`/`_placeholders.py`/`_render.py`
template grammar.

## 11. Decisions

1. **Unify on `{prompt}`, remove `{prompt_file}`** (user choice) — one prompt
   mechanism, cleanest end state; accepts breaking stray `{prompt_file}`
   prototypes (none built-in). (2026-07-19)
2. **All calls route through a file** (user choice) — direct calls persist the
   inline prompt with `_write_temp_prompt`, then read it back, so the `{prompt}`
   value uniformly originates from a file and every call leaves an audit file.
   The write-then-read on direct calls is an intentional invariant, not
   redundancy. (2026-07-19)
3. **Keep the DAG's prompt-file writing + manifest** — the file transport and
   audit path are retained; only the sub-agent-shape guard is removed.
   (2026-07-19)
