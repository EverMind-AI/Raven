# claude-agent-acp tool rendering - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A claude_code direct chat renders its tool calls under Claude Code's
own names, and a Bash row shows the model's own description of what it was for.

**Architecture:** Three layers, already built for codex and reused here. The
read boundary (`tool_vocabulary.py`) stops renaming Claude Code's names on the
way to a client. The TUI gains a verb table keyed by those names, exactly as
`codexTools.ts` is keyed by codex's. One new optional field carries the model's
own call description from the arguments JSON to the row.

**Tech Stack:** Python 3.12 + uv + pytest; TypeScript + vitest + Ink.

**Spec:** `docs/specs/2026-08-24-claude-code-acp-tool-rendering-design.md`

## Global Constraints

- Repo rules live in `CLAUDE.md` (AGENTS.md). Read it before the first edit.
- Comments: only where the logic is non-obvious or a *why* is needed. English.
  Match surrounding density -- these files comment heavily, so a real *why* is
  in keeping; a restatement of the code is not.
- Commits: Conventional Commits, English only, ASCII only in the whole message
  (no em-dash, curly quotes or ellipsis). Trailer
  `Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>`.
- **Do not commit unprompted beyond the commit step each task names, and never
  push.** The driver handles pushing and the MR.
- Python is run through uv only: `uv run pytest ...`, never bare `pytest`.
- `make lint-python` does NOT run at commit time in this repo (`core.hooksPath`
  points at a nonexistent directory), so run it by hand or CI goes red on
  formatting alone.
- Test file naming is fixed by AGENTS.md 5.1/5.2. Every test below extends an
  existing file; **do not create new test files.**
- The verb in a Claude Code table entry is the Claude Code name verbatim. Never
  substitute a Raven synonym -- that is the whole point of the change.

---

## Task 1: Stop renaming Claude Code's tool names

**Files:**
- Modify: `raven/agent/subagent/tool_vocabulary.py` (`RAVEN_NAME`, `ARGUMENT_KEY`)
- Test: `tests/test_subagent_tool_vocabulary.py`
- Test (migrate one assertion each): `tests/test_rpc_instances.py:512`, `tests/test_rpc_dag.py:451`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: rows reaching a client keep `Bash`, `Read`, `Glob`, `Grep`, `Write`,
  `Edit`, `NotebookEdit`, `LS`, `BashOutput`, `WebFetch`, `WebSearch`, `Task`.
  Tasks 3-5 render those names.

**Read first:** the spec's "Five existing assertions encode the old vocabulary"
table. Those five are stale by design. A red test at those five lines is
expected; a red test anywhere else is yours.

- [ ] **Step 1: Write the failing tests**

In `tests/test_subagent_tool_vocabulary.py`, replace
`test_a_claude_tool_name_becomes_ravens` (line 21) entirely with these two:

```python
def test_a_claude_tool_name_is_not_renamed() -> None:
    """A claude_code row keeps Claude Code's own name, so a direct chat reads as
    a Claude Code conversation rather than as a generic one. Same choice as
    codex, and for the same reason.
    """
    out = normalize_row(_call("Bash", {"command": "ls", "description": "list"}))
    fn = out["tool_calls"][0]["function"]
    assert fn["name"] == "Bash"
    assert json.loads(fn["arguments"]) == {"command": "ls", "description": "list"}


def test_every_claude_name_promotes_the_subject_its_raven_name_did() -> None:
    """Re-keying ``ARGUMENT_KEY`` under the claude names must change nothing but
    the name: each entry carries the value the old name resolved to.

    The subject has to be the *first* key, not merely present -- a reader that
    does not know the tool takes the first string value it finds.
    """
    cases = [
        ("Bash", {"command": "ls"}, "command"),
        ("BashOutput", {"bash_id": "b1"}, "command"),
        ("Read", {"file_path": "a.py"}, "path"),
        ("Write", {"file_path": "a.py", "content": "x"}, "path"),
        ("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, "path"),
        ("NotebookEdit", {"file_path": "n.ipynb"}, "path"),
        ("LS", {"path": "src/"}, "path"),
        ("Glob", {"path": "src/", "pattern": "*.ts"}, "pattern"),
        ("Grep", {"path": "src/", "pattern": "TODO"}, "pattern"),
        ("WebFetch", {"url": "https://example.com"}, "url"),
        ("WebSearch", {"query": "acp spec"}, "query"),
    ]
    for name, arguments, key in cases:
        fn = normalize_row(_call(name, arguments))["tool_calls"][0]["function"]
        assert fn["name"] == name, name
        assert next(iter(json.loads(fn["arguments"]))) == key, name
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_subagent_tool_vocabulary.py -k "is_not_renamed or promotes_the_subject" -v`

Expected: FAIL. The first asserts `Bash` and gets `exec`; the second fails on
its first case for the same reason.

- [ ] **Step 3: Drop the twelve rename entries**

In `raven/agent/subagent/tool_vocabulary.py`, delete the whole
claude-agent-acp block from `RAVEN_NAME` (the comment plus the twelve entries
from `"Bash": "exec",` through `"Task": "spawn",`), leaving the ACP spec-`kind`
entries above it untouched. Replace the deleted comment with:

```python
    # claude-agent-acp's own tool names are deliberately NOT here. A claude_code
    # row keeps them, so the transcript reads as a Claude Code conversation; the
    # TUI's table in `ui-tui/src/domain/claudeCodeTools.ts` is keyed by them.
```

- [ ] **Step 4: Re-key `ARGUMENT_KEY` under the claude names**

Append to `ARGUMENT_KEY`, after `"web_search": "query",`:

```python
    # The claude names, each mapped to the key its old RAVEN_NAME target
    # resolved to, so dropping the rename changes the displayed name and
    # nothing about the arguments. `Task` had no entry and gains none.
    "Bash": "command",
    "BashOutput": "command",
    "Read": "path",
    "Write": "path",
    "Edit": "path",
    "NotebookEdit": "path",
    "LS": "path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_subagent_tool_vocabulary.py -k "is_not_renamed or promotes_the_subject" -v`
Expected: PASS

- [ ] **Step 6: Migrate the four remaining stale assertions**

These are name assertions only. **Change nothing else in these tests** -- every
argument assertion must still pass as written, which is the proof that Step 4
was behaviour-preserving.

`tests/test_subagent_tool_vocabulary.py`, in
`test_the_tools_own_key_beats_the_generic_order`:

```python
    assert fn["name"] == "Grep"
```

`tests/test_subagent_tool_vocabulary.py`, in
`test_a_subject_under_an_unenumerated_key_still_reaches_the_tools_key`:

```python
    assert fn["name"] == "Bash"
```

`tests/test_rpc_instances.py:512` and `tests/test_rpc_dag.py:451`, both:

```python
    assert call["tool_calls"][0]["name"] == "Bash"
```

- [ ] **Step 7: Run the full affected suites**

Run: `uv run pytest tests/test_subagent_tool_vocabulary.py tests/test_rpc_instances.py tests/test_rpc_dag.py tests/test_acp_dialects.py -q`

Expected: all pass. `test_acp_dialects.py` is included because it asserts the
dialect still reports transport names -- it must stay green **without being
edited**. If it went red, the change reached further than intended.

- [ ] **Step 8: Lint and commit**

```bash
make lint-python
git add raven/agent/subagent/tool_vocabulary.py tests/test_subagent_tool_vocabulary.py tests/test_rpc_instances.py tests/test_rpc_dag.py
git commit -m "$(cat <<'EOF'
feat(agent): keep claude-agent-acp's own tool names on the wire

A claude_code row kept Claude Code's name in the record and lost it at the read
boundary, so a direct chat rendered generic raven verbs. Drop the twelve rename
entries and re-key ARGUMENT_KEY under the claude names, each mapped to the value
its old target resolved to, so only the displayed name changes.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: The plan row carries Claude Code's own name

**Files:**
- Modify: `raven/acp_client/acp_dialects/claude_code.py`
- Test: `tests/test_acp_dialects.py`
- Test data: `tests/acp_frames.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `ClaudeCodeDialect.plan_tool_name == "TodoWrite"`. Task 3's table
  needs a `TodoWrite` entry to match it.

**Background:** `TodoWrite` never emits a `tool_call` at all. The adapter's
`shouldEmitToolCall` excludes it and the Task tools, routing their state to a
`sessionUpdate: "plan"` frame instead. The codex work already turns such a frame
into one row; only the name it carries is new.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_acp_dialects.py`:

```python
def test_the_claude_plan_row_carries_claude_codes_own_name() -> None:
    """`TodoWrite` never reaches a tool_call: the adapter's `shouldEmitToolCall`
    excludes it and routes its state to a `plan` frame. The row that frame
    becomes is named for the tool that produced it.
    """
    assert ClaudeCodeDialect().plan_tool_name == "TodoWrite"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_acp_dialects.py -k plan_row_carries -v`
Expected: FAIL, `assert 'plan' == 'TodoWrite'` (the base class default).

- [ ] **Step 3: Set the name on the dialect**

In `raven/acp_client/acp_dialects/claude_code.py`, inside
`class ClaudeCodeDialect`, directly under `key = "claude-agent-acp"`:

```python
    plan_tool_name = "TodoWrite"
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_acp_dialects.py -k plan_row_carries -v`
Expected: PASS

- [ ] **Step 5: Record the real frames this adapter sends**

Append to `tests/acp_frames.py`, after `CLAUDE_EXEC_UPDATE`:

```python
# A Bash call carrying the model's own description of it. `_meta.claudeCode`
# and `rawInput` hold the same string; the adapter keeps it out of ACP's
# `title`, which clients use as the shell-command preview.
CLAUDE_BASH_WITH_DESCRIPTION: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "toolu_019KgriiAGn9yJvTzSP6doNu",
    "kind": "execute",
    "status": "pending",
    "title": "cd /repo/ui-tui && ls i18n",
    "rawInput": {"command": "cd /repo/ui-tui && ls i18n", "description": "Locate messages.json and i18n dirs"},
    "_meta": {"claudeCode": {"toolName": "Bash", "title": "Locate messages.json and i18n dirs"}},
}

# A completing frame that names the tool but repeats no `kind`. 146 of the 350
# captured tool frames look like this, which is why `names_call` may not gate on
# `kind` alone for this adapter.
CLAUDE_UPDATE_WITHOUT_KIND: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "toolu_019KgriiAGn9yJvTzSP6doNu",
    "_meta": {
        "claudeCode": {
            "toolName": "Bash",
            "toolResponse": {"stdout": "messages.json\n", "stderr": "", "interrupted": False},
        }
    },
}
```

- [ ] **Step 6: Assert the dialect reads both frames**

`tests/test_acp_dialects.py` reaches these fixtures through a module import,
`from tests import acp_frames` at line 301, and refers to them as
`acp_frames.NAME`. Follow that -- do not add a `from tests.acp_frames import X`
line, and put both tests **after** line 301 so the import is in scope. `json` is
already imported at the top of the file.

```python
def test_a_claude_frame_without_kind_is_still_named_by_its_meta() -> None:
    """The opening frame carries `kind`; 146 of 350 captured tool frames do not,
    and `_meta.claudeCode.toolName` is the only name on those.
    """
    assert ClaudeCodeDialect().tool_name(acp_frames.CLAUDE_UPDATE_WITHOUT_KIND) == "Bash"


def test_a_claude_bash_call_keeps_both_its_command_and_its_description() -> None:
    """The row shows the description and the expanded block shows the command,
    so both have to survive into the stored arguments.
    """
    call = ClaudeCodeDialect().call(acp_frames.CLAUDE_BASH_WITH_DESCRIPTION)
    assert call.name == "Bash"
    assert call.argument == "cd /repo/ui-tui && ls i18n"
    assert json.loads(call.arguments_json())["description"] == "Locate messages.json and i18n dirs"
```

- [ ] **Step 7: Run the dialect suite**

Run: `uv run pytest tests/test_acp_dialects.py -q`
Expected: all pass.

If `test_a_claude_frame_without_kind_is_still_named_by_its_meta` fails, do not
change the test: `ClaudeCodeDialect.tool_name` already reads
`_meta.claudeCode.toolName` before falling back, so a failure means the frame
literal was mistyped.

- [ ] **Step 8: Document why the plan lane exists**

In the module docstring of `claude_code.py`, append a third bullet to the
existing list of "things this adapter does that the spec does not describe":

```
- It never emits a ``tool_call`` for ``TodoWrite`` or the Task tools. Their
  state goes to a ``sessionUpdate: "plan"`` frame instead, so the plan row is
  the only place they appear, and it is named for the tool that produced it.
```

- [ ] **Step 9: Lint and commit**

```bash
make lint-python
git add raven/acp_client/acp_dialects/claude_code.py tests/test_acp_dialects.py tests/acp_frames.py
git commit -m "$(cat <<'EOF'
feat(agent): name the claude-agent-acp plan row for the tool behind it

TodoWrite never reaches a tool_call: the adapter routes its state to a plan
frame. Name that row TodoWrite, symmetric with codex's update_plan, and record
the two frame shapes the tests read.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Claude Code's verb table in the TUI

**Files:**
- Create: `ui-tui/src/domain/claudeCodeTools.ts`
- Modify: `ui-tui/src/domain/episodeSummary.ts` (`ruleFor` only)
- Modify: `ui-tui/src/domain/directEpisodes.ts` (module docstring)
- Test: `ui-tui/src/__tests__/episodeSummary.test.ts`

**Interfaces:**
- Consumes: the names Task 1 now lets through.
- Produces: `claudeRule(name: string): VerbRule | undefined` and
  `CLAUDE_VERBS: Record<string, VerbRule>`, both exported from
  `./claudeCodeTools.js`. Tasks 4 and 5 do not import them; only
  `episodeSummary.ts` does.

- [ ] **Step 1: Make the worktree able to run TUI tests**

A fresh worktree has no `node_modules`, so the suite cannot start. The lockfile
is identical to the main checkout's (verified), so linking is safe:

```bash
ln -s /Evermind/sh_evermind/xuedizhan/Raven/node_modules node_modules
ln -s /Evermind/sh_evermind/xuedizhan/Raven/ui-tui/node_modules ui-tui/node_modules
ln -s /Evermind/sh_evermind/xuedizhan/Raven/ui-tui/packages/hermes-ink/node_modules ui-tui/packages/hermes-ink/node_modules
```

Confirm the baseline is green before changing anything:

```bash
npm test --prefix ui-tui -- --no-file-parallelism
```

Expected: all pass. `--no-file-parallelism` is not optional -- the suite flakes
under default worker parallelism once it exceeds ~100 files, and a flake here
would be misread as your change breaking something.

These symlinks are gitignored; do not add them.

- [ ] **Step 2: Write the failing tests**

Add to `ui-tui/src/__tests__/episodeSummary.test.ts`, inside the final
`describe` block. `claudeRule` lives in a different module from everything the
file already imports, so this is a **new import line**, placed after the
existing `import { ... } from '../domain/episodeSummary.js'`:

```ts
import { claudeRule } from '../domain/claudeCodeTools.js'
```

```ts
  it('folds a run of claude reads under the table verb and a files unit, not the generic calls fallback', () => {
    const reads = [tool('Read', 'a.ts'), tool('Read', 'b.ts'), tool('Read', 'c.ts')]

    expect(toolsPhrase(reads)).toBe('Read 3 files')
  })

  it('names an mcp tool by the whole name claude code gave it', () => {
    const rule = claudeRule('mcp__playwright__browser_click')

    expect(rule?.verb).toBe('mcp__playwright__browser_click')
  })

  it('leaves a claude tool outside the table to the generic humanised rule', () => {
    expect(claudeRule('SendMessage')).toBeUndefined()
  })
```

- [ ] **Step 3: Run them to verify they fail**

Run: `npm test --prefix ui-tui -- --no-file-parallelism episodeSummary`
Expected: FAIL. The import of `claudeRule` does not resolve.

- [ ] **Step 4: Create the table**

Create `ui-tui/src/domain/claudeCodeTools.ts`:

```ts
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Claude Code's own tool names, and how a run of them folds.
//
// Same shape and same reason as `codexTools.ts`. A claude_code direct chat keeps
// Claude Code's names so the transcript reads as a Claude Code conversation, and
// `OVERRIDES` in `episodeSummary.ts` is keyed by Raven's vocabulary -- so these
// names need a table of their own. With no entry every call falls to the generic
// rule and a run of six reads stops collapsing to one line.
//
// The verb is the Claude Code name verbatim, never a Raven synonym. Only `style`
// and `unit` are ours.
//
// The entries are the names with evidence behind them: `Bash` and `Read` were
// measured on the wire, the rest are named explicitly in the adapter's own
// `toolInfoFromToolUse` switch or were already carried by `RAVEN_NAME`. A name
// outside this table is not a gap -- `ruleFor` humanises it, which is the right
// answer for a tool nobody here has seen.

import type { VerbRule } from './episodeSummary.js'

export const CLAUDE_VERBS: Record<string, VerbRule> = {
  Bash: { verb: 'Bash', unit: 'commands', style: 'target' },
  BashOutput: { verb: 'BashOutput', unit: 'commands', style: 'target' },
  Read: { verb: 'Read', unit: 'files', style: 'count' },
  Write: { verb: 'Write', unit: 'files', style: 'target' },
  Edit: { verb: 'Edit', unit: 'files', style: 'target' },
  NotebookEdit: { verb: 'NotebookEdit', unit: 'files', style: 'target' },
  Glob: { verb: 'Glob', unit: 'patterns', style: 'count' },
  Grep: { verb: 'Grep', unit: 'patterns', style: 'count' },
  LS: { verb: 'LS', unit: 'dirs', style: 'count' },
  WebFetch: { verb: 'WebFetch', unit: 'urls', style: 'target' },
  WebSearch: { verb: 'WebSearch', unit: 'queries', style: 'target' },
  Agent: { verb: 'Agent', unit: 'subagents', style: 'count' },
  Task: { verb: 'Task', unit: 'subagents', style: 'count' },
  TodoWrite: { verb: 'TodoWrite', unit: '', style: 'target' },
  ExitPlanMode: { verb: 'ExitPlanMode', unit: '', style: 'target' },
  AskUserQuestion: { verb: 'AskUserQuestion', unit: 'questions', style: 'target' },
  ReportFindings: { verb: 'ReportFindings', unit: 'findings', style: 'target' },
  TaskCreate: { verb: 'TaskCreate', unit: 'tasks', style: 'target' },
  TaskUpdate: { verb: 'TaskUpdate', unit: 'tasks', style: 'target' },
  TaskList: { verb: 'TaskList', unit: '', style: 'target' },
  TaskGet: { verb: 'TaskGet', unit: '', style: 'target' }
}

// An MCP call is named `mcp__<server>__<tool>`, one name per configured tool, so
// it cannot have a row of its own. The separator differs from codex's `mcp.`,
// which is why this is a second rule rather than a shared one.
export const claudeRule = (name: string): VerbRule | undefined =>
  CLAUDE_VERBS[name] ?? (name.startsWith('mcp__') ? { verb: name, unit: 'calls', style: 'target' } : undefined)
```

- [ ] **Step 5: Consult the table from `ruleFor`**

In `ui-tui/src/domain/episodeSummary.ts`, add the import beside the codex one:

```ts
import { claudeRule } from './claudeCodeTools.js'
```

and replace `ruleFor` and the comment above it with:

```ts
// The rule for any tool: its override if we have one, else an adapter's own
// table, else a generic rule built from the humanized name. The adapter tables
// are consulted after OVERRIDES rather than merged into it because the three are
// keyed by different vocabularies -- OVERRIDES by Raven's names, CODEX_VERBS by
// codex's, CLAUDE_VERBS by Claude Code's.
const ruleFor = (name: string): VerbRule =>
  OVERRIDES[name] ??
  codexRule(name) ??
  claudeRule(name) ?? { verb: humanize(name) || name, unit: 'calls', style: 'target' }
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `npm test --prefix ui-tui -- --no-file-parallelism episodeSummary`
Expected: PASS

- [ ] **Step 7: Correct the docstring that now states the opposite of the truth**

`ui-tui/src/domain/directEpisodes.ts` says the runtime hands over calls already
named in Raven's vocabulary. Task 1 made that false for two adapters. Replace
that paragraph (the one beginning "The runtime hands over calls already named")
with:

```ts
// The runtime hands over each call under its own transport's name: raven's own
// tools keep raven's vocabulary, and codex and claude_code keep theirs, so a
// direct chat reads as a conversation with that agent. `ruleFor` in
// `episodeSummary.ts` consults one verb table per vocabulary, and humanises a
// name that no table claims.
```

- [ ] **Step 8: Run the whole TUI suite and commit**

```bash
npm test --prefix ui-tui -- --no-file-parallelism
npm run type-check --prefix ui-tui
git add ui-tui/src/domain/claudeCodeTools.ts ui-tui/src/domain/episodeSummary.ts ui-tui/src/domain/directEpisodes.ts ui-tui/src/__tests__/episodeSummary.test.ts
git commit -m "$(cat <<'EOF'
feat(tui): fold a run of claude code calls under claude code's own verbs

Rows now arrive under Claude Code's names, which OVERRIDES is not keyed by, so
every call fell to the generic rule and a run of six reads stopped collapsing to
one line. Add the table, keyed by those names, alongside codex's.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Shape the rows

**Files:**
- Modify: `ui-tui/src/domain/episodeSummary.ts` (`PATHY`, `QUOTED`, and the shell branch in `target`)
- Test: `ui-tui/src/__tests__/episodeSummary.test.ts`

**Interfaces:**
- Consumes: `CLAUDE_VERBS` from Task 3 (only so the verbs in the expected
  strings exist).
- Produces: nothing new. Task 5 modifies the same `target` function, so read its
  final shape here before editing it there.

**Why this is required, not polish:** `execLabel` -- the thing that turns a
100-character pipeline into `curl -> python3` -- fires only for `exec` and
`commandExecution`. Task 1 renamed `Bash` away from `exec`, so without this a
Bash row falls to a 40-character clip of the raw command. That is strictly worse
than before the change.

- [ ] **Step 1: Write the failing tests**

Add to `ui-tui/src/__tests__/episodeSummary.test.ts`, in the same final
`describe` block:

```ts
  it('names the programs a claude Bash command ran, not the whole pipeline', () => {
    const detail = toolParts(tool('Bash', 'cat x | python3 -c "import sys"')).detail

    expect(detail).toContain('python3')
    expect(detail).not.toContain('import sys')
  })

  it('keeps the filename of a long claude Read path, the way raven paths do', () => {
    const long = `/repo/${'nested/'.repeat(12)}memory/agent_memory.go`
    const { detail } = toolParts(tool('Read', long))

    expect(detail.startsWith('…')).toBe(true)
    expect(detail.endsWith('agent_memory.go')).toBe(true)
  })

  it('quotes the needle a claude Grep looked for', () => {
    expect(toolParts(tool('Grep', 'DeviceFlow')).detail).toBe('"DeviceFlow"')
  })
```

- [ ] **Step 2: Run them to verify they fail**

Run: `npm test --prefix ui-tui -- --no-file-parallelism episodeSummary`
Expected: FAIL on all three -- the Bash detail is the raw command, the Read
detail is right-clipped, the Grep detail is unquoted.

- [ ] **Step 3: Add the claude names to the two sets**

In `ui-tui/src/domain/episodeSummary.ts`, `PATHY` gains four entries:

```ts
const PATHY = new Set([
  'read_file',
  'write_file',
  'edit_file',
  'list_dir',
  'apply_patch',
  'fileChange',
  'imageView',
  'commandExecution.read',
  'commandExecution.listFiles',
  'Read',
  'Write',
  'Edit',
  'NotebookEdit'
])
```

and `QUOTED` gains three:

```ts
const QUOTED = new Set(['grep', 'find', 'web_search', 'Grep', 'Glob', 'WebSearch'])
```

- [ ] **Step 4: Give the shell branch a set of its own**

Still in `episodeSummary.ts`, add this beside the other sets, above `target`:

```ts
// Tools whose subject is a shell command, so the row names the programs it ran
// rather than printing the pipeline.
const SHELL = new Set(['exec', 'commandExecution', 'Bash', 'BashOutput'])
```

and in `target`, replace

```ts
  if (tool.name === 'exec' || tool.name === 'commandExecution') {
    return execLabel(raw)
  }
```

with

```ts
  if (SHELL.has(tool.name)) {
    return execLabel(raw)
  }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `npm test --prefix ui-tui -- --no-file-parallelism episodeSummary`
Expected: PASS

- [ ] **Step 6: Run the whole TUI suite and commit**

```bash
npm test --prefix ui-tui -- --no-file-parallelism
npm run type-check --prefix ui-tui
git add ui-tui/src/domain/episodeSummary.ts ui-tui/src/__tests__/episodeSummary.test.ts
git commit -m "$(cat <<'EOF'
fix(tui): keep claude bash rows a label instead of a raw command

execLabel fired on the raven name, so renaming Bash off exec dropped a row from
two program names to a 40-char clip of the pipeline. Give the branch a set, and
put the claude path and search tools in PATHY and QUOTED beside their raven
equivalents.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Show the model's own description of a call

**Files:**
- Modify: `ui-tui/src/types.ts` (`EpisodeTool`)
- Modify: `ui-tui/src/domain/episodeFold.ts` (new `callIntent`, row construction)
- Modify: `ui-tui/src/domain/episodeSummary.ts` (`target`, `toolParts`)
- Test: `ui-tui/src/__tests__/episodeSummary.test.ts`, `ui-tui/src/__tests__/episodeFold.test.ts`

**Interfaces:**
- Consumes: `SHELL` and the final `target` from Task 4.
- Produces: `EpisodeTool.intent?: string`; `callIntent(argumentsJson: string): string`
  exported from `./episodeFold.js`.

**The trap:** two functions build a row's visible subject. `phraseFor` builds the
collapsed label and `toolParts` builds the expanded one, and both reach it
through `target()`. Putting the preference in `toolParts` alone makes one row
show the intent folded and the command expanded. The last test in Step 1 is what
catches that.

- [ ] **Step 1: Write the failing tests**

Add to `ui-tui/src/__tests__/episodeSummary.test.ts`:

```ts
  it('shows the model own description of a Bash call instead of the derived label', () => {
    const call: EpisodeTool = {
      id: 'c1',
      intent: 'Locate messages.json and i18n dirs',
      name: 'Bash',
      ok: true,
      summary: 'cd /repo/ui-tui && ls i18n'
    }

    expect(toolParts(call).detail).toBe('Locate messages.json and i18n dirs')
  })

  it('keeps the whole command in the expanded argument when the row shows a description', () => {
    const call: EpisodeTool = {
      id: 'c1',
      intent: 'Locate messages.json and i18n dirs',
      name: 'Bash',
      ok: true,
      summary: 'cd /repo/ui-tui && ls i18n'
    }

    expect(toolArgument(call)).toBe('cd /repo/ui-tui && ls i18n')
  })

  it('folds and expands one call to the same subject', () => {
    const call: EpisodeTool = { id: 'c1', intent: 'List the repo', name: 'Bash', ok: true, summary: 'cd /repo && ls' }

    expect(toolsPhrase([call])).toBe('Bash List the repo')
    expect(toolParts(call).detail).toBe('List the repo')
  })
```

Add to `ui-tui/src/__tests__/episodeFold.test.ts`. `callIntent` joins the
module the file already imports, so widen that existing line rather than adding
a new one:

```ts
import { callIntent, foldRowsIntoEpisodes } from '../domain/episodeFold.js'
```

```ts
describe('callIntent', () => {
  it('reads the description claude code sends beside a command', () => {
    expect(callIntent('{"command":"ls","description":"List the repo"}')).toBe('List the repo')
  })

  it('is empty for a call that carries no description', () => {
    expect(callIntent('{"command":"ls"}')).toBe('')
  })

  it('is empty for arguments that are not an object', () => {
    expect(callIntent('not json')).toBe('')
    expect(callIntent('"a bare string"')).toBe('')
  })
})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `npm test --prefix ui-tui -- --no-file-parallelism episodeSummary episodeFold`
Expected: FAIL -- `intent` is not a property of `EpisodeTool`, and `callIntent`
does not resolve.

- [ ] **Step 3: Add the field**

In `ui-tui/src/types.ts`, inside `EpisodeTool`, after `summary: string`:

```ts
  // The model's own one-line description of what a call is for, when the
  // transport sends one -- claude-agent-acp puts Claude's Bash `description`
  // in the call's arguments. Preferred over a derived label because it names
  // the intent rather than the programs; absent for every transport that sends
  // none, so a row without one is unaffected.
  intent?: string
```

- [ ] **Step 4: Read it at the fold**

In `ui-tui/src/domain/episodeFold.ts`, add after `callSubject`:

```ts
/** The model's own description of a call, when its arguments carry one.
 *
 * claude-agent-acp sends this both in `_meta.claudeCode.title` and in the call's
 * arguments; the arguments are the copy that already reaches a client, so that
 * is the one read here.
 */
export const callIntent = (argumentsJson: string): string => {
  let parsed: unknown

  try {
    parsed = JSON.parse(argumentsJson)
  } catch {
    return ''
  }

  if (parsed === null || typeof parsed !== 'object') {
    return ''
  }

  const value = (parsed as Record<string, unknown>).description

  return typeof value === 'string' ? value.trim() : ''
}
```

and in the row construction (the `calls.map` that sets `summary: callSubject(call.arguments)`):

```ts
    const tools = calls.map((call): EpisodeTool => {
      const intent = callIntent(call.arguments)
      const tool: EpisodeTool = {
        done: true,
        id: call.id,
        name: call.name,
        ok: true,
        summary: callSubject(call.arguments),
        ...(intent ? { intent } : {}),
        ...(row.atMs ? { startedAt: row.atMs } : {})
      }

      pending.set(call.id, tool)

      return tool
    })
```

- [ ] **Step 5: Prefer it in both subject builders**

In `ui-tui/src/domain/episodeSummary.ts`, at the very top of `target`, before
`const raw = tool.summary.trim()`:

```ts
  // The model's own description of the call, when the transport sent one. It
  // names the intent, which every derived label below can only approximate.
  if (tool.intent) {
    return clip(tool.intent, budget)
  }

```

and in `toolParts`, change the `arg` line so a row with an intent does not take
the path branch (an intent is prose, and left-clipping prose loses its head):

```ts
  const arg = PATHY.has(tool.name) && !tool.intent ? clipPath(raw, 60) : target(tool, 60)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `npm test --prefix ui-tui -- --no-file-parallelism episodeSummary episodeFold`
Expected: PASS

- [ ] **Step 7: Run everything and commit**

```bash
npm test --prefix ui-tui -- --no-file-parallelism
npm run type-check --prefix ui-tui
npm run lint --prefix ui-tui
git add ui-tui/src/types.ts ui-tui/src/domain/episodeFold.ts ui-tui/src/domain/episodeSummary.ts ui-tui/src/__tests__/episodeSummary.test.ts ui-tui/src/__tests__/episodeFold.test.ts
git commit -m "$(cat <<'EOF'
feat(tui): show the model's own description of a claude code command

claude-agent-acp sends Claude's Bash description beside the command, and the row
showed a label derived from the pipeline instead. Carry it as EpisodeTool.intent
and prefer it in target(), which both the folded and the expanded subject reach,
so the two cannot disagree. The expanded block still shows the whole command.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Verify the whole change end to end

**Files:** none. This task only runs things and reports.

- [ ] **Step 1: The full Python suite**

Run: `uv run pytest -q`

Expected: green. `tests/test_subagent_direct_chat.py` has been seen to SIGSEGV
intermittently on pristine `origin/main`; if it crashes, re-run that file alone
to confirm it is the known pre-existing flake, and say so rather than
attributing it to this branch.

- [ ] **Step 2: The full TUI suite, serially**

Run: `npm test --prefix ui-tui -- --no-file-parallelism`
Expected: green.

- [ ] **Step 3: Lint both sides**

```bash
make lint-python
npm run lint --prefix ui-tui
npm run type-check --prefix ui-tui
```

If `make lint-python` reports files this branch never touched, check whether it
fails identically at the merge base before treating it as yours.

- [ ] **Step 4: Confirm the diff is only what the plan describes**

```bash
git diff --stat origin/main...HEAD
```

Expected exactly: the spec, this plan, `tool_vocabulary.py`, `claude_code.py`,
`claudeCodeTools.ts`, `episodeSummary.ts`, `episodeFold.ts`, `directEpisodes.ts`,
`types.ts`, and the five test files. Anything else is unplanned and must be
explained or reverted.

- [ ] **Step 5: Report**

Report to the driver: the four commands' results, the diffstat, and any
assertion that had to be migrated beyond the five the spec names. **Do not push
and do not open an MR** -- the driver does that.
