# claude-agent-acp tool rendering - design

Date: 2026-08-24
Status: designed
Base: `feat/claude_code_tool_rendering` cut from `origin/main` at `3c060830`.

Measured against `@agentclientprotocol/claude-agent-acp@0.66.0` (`agentInfo.name`
`@agentclientprotocol/claude-agent-acp`). Evidence is 224 captured frames from
real direct chats under `~/.raven/traces/logs/acp-frames/2026-08-20/`, plus the
adapter's own `dist/tools.js` and `dist/acp-agent.js`, which ship unminified and
commented.

This extends the codex work in
`docs/specs/2026-08-23-codex-acp-tool-parsing-design.md`. Read that first: the
dialect hooks, the read boundary and the TUI verb-table pattern all come from
it, and this spec only adds a second adapter to them.

## Evidence: what is measured and what is derived

Separating these is the point of this section, not a formality. The codex round
produced exactly one blocking review finding, and its cause was a claim that
described behaviour nobody had run. Anything below marked *derived* is a
prediction from adapter source, and a test has to close it before it counts.

**Measured** (from the 224 frames):

| Fact | Evidence |
|---|---|
| `_meta.claudeCode.toolName` is on every tool frame | 350 occurrences |
| `_meta.claudeCode.title` carries the model's own Bash `description` | 64 occurrences |
| `_meta.claudeCode.toolResponse` carries `stdout`/`stderr`/`interrupted` | 74 occurrences |
| Only `Bash` (312) and `Read` (38) were exercised | tool-name census |
| `rawInput` key sets: `Bash{command,description[,timeout]}`, `Read{file_path[,limit]}` | key-order census |
| A `tool_call_update` may carry `toolName` with no `kind` | Bash 128, Read 18 |
| Statuses reaching a row include `failed` | 1 Bash |
| No completed call loses its output to the `toolResponse` frame | 68 of 68 carry text elsewhere |
| No frame carried `interrupted: true` | 0 of 224 |

**Derived** (adapter source only, no capture exercised them): the `kind`,
`title` and `locations` the adapter builds for `Glob`, `Grep`, `Write`, `Edit`,
`WebFetch`, `WebSearch`, `TodoWrite`, `Agent`/`Task`, `ExitPlanMode`,
`AskUserQuestion`, `ReportFindings` and `TaskCreate`/`TaskUpdate`/`TaskList`/
`TaskGet`; the routing of `TodoWrite` and the Task tools to the plan lane
(`shouldEmitToolCall`); and the naming of MCP tools as `mcp__<server>__<tool>`.
Every one of those shapes is declared in one place: `toolInfoFromToolUse`, the
switch in `dist/tools.js:16` of `@agentclientprotocol/claude-agent-acp@0.66.0`
(its `Glob` case reads `input.path` and `input.pattern`; its `Grep` case reads
`input.pattern`, `input.path`, `input.glob`, `input.type`, `input.head_limit`,
`input.output_mode` and the `-i`/`-n`/`-A`/`-B`/`-C` flags). The adapter ships
that source, so a derived shape is testable, not merely predicted, and the
argument-promotion cases in `tests/test_subagent_tool_vocabulary.py` are
written to exactly what the switch reads. *Derived* bounds one thing: that no
capture observed this frame on the wire -- it says nothing about whether our
code's handling of the shape is verified.

## Goal

A claude_code direct chat renders its tool calls under Claude Code's own names,
so the transcript reads as a Claude Code conversation, and every row shows the
most informative subject the wire offers.

## Non-goals

- Rebuilding the web UI's tool rendering to match this design. Its
  `Bash`/`Read`/`Glob` renderers arrived with the original agentscope commit
  `9ea5b336`, keyed by names that happen to collide with Claude Code's own
  vocabulary. That collision means a claude_code row is not unreached:
  `ChatViewport.tsx` feeds a direct chat's messages through the same
  `ChatContent` -> `MessageBubble` -> `renderToolCall` path as the main
  session, so its `Bash`/`Read`/`Write`/`Edit`/`Glob`/`Grep` calls do hit
  these renderers. Read/Write/Edit broke as a result: their shared
  `tryGetFilePath` (`_shared.tsx`) read only `file_path`, and the read
  boundary now promotes a claude_code row's subject onto `path`, so the path
  vanished from the row. This branch adds `path` there as a fallback rather
  than leaving that regression for a separate fix. Reworking these renderers
  to speak Claude Code's vocabulary the way the TUI's verb tables do stays
  out of scope.
- The `interrupted` flag on `_meta.claudeCode.toolResponse`. Zero occurrences in
  224 frames, so its behaviour on a real interruption is unknown. Writing
  handling for it would be writing untested code against a guessed frame shape.
- Recovering output from the `toolResponse` frame. Suspected, then measured: no
  output is lost.
- The `cli` backend. `cli_agent.py` emits no tool rows at all, so the vocabulary
  change cannot reach it.

## What the wire carries, and what today's code does with it

### The adapter names the tool precisely, and Raven renames it back

`ClaudeCodeDialect.tool_name` already reads `_meta.claudeCode.toolName`, so the
stored record holds `Bash`, `Read`, `Glob`. `tool_vocabulary.RAVEN_NAME` then
maps twelve of those into Raven's vocabulary on the way to a client, so the TUI
sees `exec`, `read_file`, `find`.

That mapping is not lossy the way codex's was: `Bash` and `exec` name the same
thing. It is a display-vocabulary choice, and the chosen answer is the same as
codex's, for the same reason. Knowing which agent you are talking to is worth
more than uniformity across transports, and codex and claude_code following
opposite rules is the confusing outcome.

### The model's own intent is on the wire and is discarded

The adapter deliberately keeps Claude's `description` out of ACP's `title`,
which clients use as the shell-command preview, and puts it in
`_meta.claudeCode.title` instead. Measured, the two differ sharply:

| ACP `title` (rendered today) | `_meta.claudeCode.title` (unread) |
|---|---|
| `cd /Evermind/sh_evermind/xuedizhan/Raven/.claude/worktrees/f` | `Locate messages.json and i18n dirs` |
| `cd /Evermind/sh_evermind/xuedizhan/Raven/.claude/worktrees/f` | `Grep for messages.json references` |

`episodeSummary.ts` says of `execLabel`: "This is the stopgap for `exec` having
no `description` parameter: once the backend can pass the model's own intent
through `display_call`, that wins and this stays as the fallback." Claude Code
has been passing it since before that comment was written.

The same value also reaches the client already, inside the arguments JSON:
`rawInput` is `{command, description}` on 64 of 312 Bash frames, and
`arguments_json` serialises it. `callSubject` scans `SUBJECT_KEYS` by name,
finds `command` first, and never looks at `description`.

## Design

### 1. Stop renaming the twelve

Drop `Bash`, `BashOutput`, `Read`, `Write`, `Edit`, `NotebookEdit`, `Glob`,
`Grep`, `LS`, `WebFetch`, `WebSearch` and `Task` from `RAVEN_NAME`. The ACP
spec-`kind` entries above them stay: the base dialect still reports `kind`, and
opencode is read through it.

No migration is needed, and not by luck. The record has always stored `Bash`;
the rename happens on the way out, in `normalize_row`, called from
`rpc/methods/{dag,instances,subagent}.py`. A direct chat folds both its settled
and its live turns through `foldDirectTurns` (`directChatSync.ts:108`), so the
two change together and cannot disagree.

`Task` loses its mapping to `spawn`, so a nested Claude Code subagent no longer
borrows the `delegated` verb. `DISPATCH_TOOLS` in `chatStream.ts` is consulted
on the host chat stream, not on the direct-chat fold, so nothing downstream
breaks.

### 2. Keep the subject correct without the rename

`ARGUMENT_KEY` is keyed by the name *after* renaming, so dropping the twelve
also drops subject promotion for all of them. The rule that restores it is
mechanical: **re-key each name's existing entry under the Claude name.**

| Claude name | renamed to (today) | its `ARGUMENT_KEY` | new entry |
|---|---|---|---|
| `Bash`, `BashOutput` | `exec` | `command` | `command` |
| `Read`, `Write`, `Edit`, `NotebookEdit`, `LS` | `read_file`/`write_file`/`edit_file`/`list_dir` | `path` | `path` |
| `Glob`, `Grep` | `find`/`grep` | `pattern` | `pattern` |
| `WebFetch` | `web_fetch` | `url` | `url` |
| `WebSearch` | `web_search` | `query` | `query` |
| `Task` | `spawn` | none | none |

Eleven entries. Because each is the same value the old name resolved to, the
promoted arguments are **byte-identical to today's** and only the displayed name
changes. That is what makes the change reviewable: any argument difference in a
test is a bug, not an expected consequence.

Skipping this would be a real regression, not a cosmetic one. `Glob` and `Grep`
send both `pattern` and `path`, and `callSubject` scans
`['command', 'path', 'pattern', 'query', 'url', 'file_path']` in that order, so
those rows would show the directory searched and lose the needle -- the failure
`_promote`'s own docstring names: "`grep` with a `path` scope reports the
directory it searched and loses the pattern." Separately, `callSubject` has no
`filePath` in its list, so an adapter using that spelling would fall through to
the generic sweep and could surface `old_string` as the subject of an edit.

`Agent`/`Task` keeps no entry and falls through the named keys to
`description`, which is the subject a reader wants.

### 3. Claude Code's names get a verb table

New `ui-tui/src/domain/claudeCodeTools.ts`, mirroring `codexTools.ts`:
`CLAUDE_VERBS` keyed by Claude Code's own names, with the verb equal to the key
verbatim and only `unit` and `style` chosen by us. `ruleFor` in
`episodeSummary.ts` consults it after `codexRule`.

Without a table, every Claude Code call falls to the generic rule and a run of
six reads stops collapsing to one line, exactly as it would have for codex.

MCP tools cannot have a row each: Claude Code names them
`mcp__<server>__<tool>`, one name per configured tool. A prefix rule handles
them, as `mcp.` does for codex. Note the separator differs -- codex uses `mcp.`,
Claude Code uses `mcp__` -- so this is a second rule, not a shared one.

### 4. Shape the rows

- `Bash` and `BashOutput` join the `execLabel` branch in `target()`. Without
  this the change is a regression: `execLabel` fires on `tool.name === 'exec'`,
  so renaming `Bash` away from `exec` drops a row from `cd -> ls` to a 40-char
  clip of the raw command.
- `Read`, `Write`, `Edit` and `NotebookEdit` join `PATHY`, so a path keeps its
  basename instead of its head.
- `Grep`, `Glob` and `WebSearch` join `QUOTED`, so the needle is quoted.

### 5. The row shows the model's intent; the detail keeps the command

`EpisodeTool` gains `intent?: string`, filled in `episodeFold.ts` from the
arguments JSON's `description` when one is present. `toolArgument` keeps
returning `summary`, so the expanded block still shows the whole command.

The preference goes inside `target()`, not in `toolParts`. Two different
functions produce a row's visible subject -- `phraseFor` builds the collapsed
label and `toolParts` builds the expanded one -- and both reach it through
`target()`. Putting the preference in `toolParts` alone would show the intent
when a row is expanded and the command when the same row is folded.

A second field rather than a replacement, because `toolArgument` is
`tool.summary`: promoting the description into `summary` would put prose where
the expanded block must show the command.

Optional, and absent for every transport that sends no description, so a row
that has none behaves exactly as it does today. Both the live and the settled
fold call the same function, so this cannot make live and resume disagree.

### 6. The plan lane already works

`TodoWrite` never emits a `tool_call`: `shouldEmitToolCall` excludes it and the
Task tools, and the adapter routes their state to `sessionUpdate: "plan"`. The
codex work already turns a plan frame into one row through `plan_rows` and
`_PLAN_CALL_ID`.

Only `ClaudeCodeDialect.plan_tool_name = "TodoWrite"` is new, symmetric with
codex's `update_plan`, so the plan row carries Claude Code's own name too.

### 7. Where the code goes

| File | Change |
|---|---|
| `raven/agent/subagent/tool_vocabulary.py` | drop 12 `RAVEN_NAME` entries; re-key 11 `ARGUMENT_KEY` entries |
| `raven/agent/subagent/acp_dialects/claude_code.py` | `plan_tool_name`; docstring |
| `ui-tui/src/domain/claudeCodeTools.ts` | new: `CLAUDE_VERBS`, `claudeRule` |
| `ui-tui/src/domain/episodeSummary.ts` | consult `claudeRule`; `PATHY`, `QUOTED`, `execLabel` branch; `intent` in `target()` |
| `ui-tui/src/domain/episodeFold.ts` | populate `intent` |
| `ui-tui/src/types.ts` | `EpisodeTool.intent?` |
| `ui-tui/src/domain/directEpisodes.ts` | docstring: it states the runtime hands over Raven's vocabulary, which stops being true |

## Testing

### Six existing assertions encode the old vocabulary

These are stale by design, not broken by the change, and the plan must name each
one so an implementer does not read a red test as their own bug. The codex round
lost a task to exactly this omission.

| Location | Input | Asserts today | Becomes |
|---|---|---|---|
| `tests/test_subagent_tool_vocabulary.py:23` | `Bash` | `exec` | `Bash` |
| `tests/test_subagent_tool_vocabulary.py:64` | `Grep` | `grep` | `Grep` |
| `tests/test_subagent_tool_vocabulary.py:143` | `Bash` | `exec` | `Bash` |
| `tests/test_rpc_instances.py:512` | `Bash` | `exec` | `Bash` |
| `tests/test_rpc_dag.py:451` | `Bash` | `exec` | `Bash` |
| `tests/test_rpc_subagent_calls.py:402` | `Bash` | `exec` | `Bash` |

The sixth was missed when this table was first written and found by a full-suite
run during Task 1. Its docstring also states the rationale this design reverses
("the wire keeps raven's because every renderer's verb table is keyed by it"),
so the docstring is corrected with the assertion. Enumerating by grepping for
`== "exec"` is what missed it: the same file has an unrelated earlier hit, and
checking that one and moving on skipped this test entirely.

Name assertions only. Every argument assertion in those tests, including
`test_the_tools_own_key_beats_the_generic_order` and
`test_an_unrelated_field_sharing_the_subjects_value_survives`, must pass
**unchanged** -- that is the check that the re-keyed `ARGUMENT_KEY` really is
behaviour-preserving. An implementer who finds an argument assertion failing has
found a bug in the re-keying, not a stale test.

Also checked and *not* affected, so they must stay green untouched:
`tests/test_acp_dialects.py` (asserts the dialect reports transport names, which
does not change), and every other `== "exec"` / `== "read_file"` assertion in the
suite, all of which feed an already-Raven name as input.

### New tests

Python:

- `tests/acp_frames.py` gains real claude frames taken from the captures: a
  `Bash` call with a `description`, a `Read`, and a `tool_call_update` carrying
  `toolName` with no `kind`.
- `normalize_row` leaves `Bash` and `Read` named as sent.
- `normalize_row` promotes `pattern` over `path` for `Glob` and `Grep`. This is
  the test that closes a *derived* claim, so it asserts on a payload shaped like
  the adapter's source says it sends, and the spec says so.
- The dialect reports `TodoWrite` as the plan tool name.

TUI:

- `claudeRule` returns the verbatim name for a table entry, handles `mcp__`, and
  returns undefined otherwise.
- A run of `Read` calls collapses to one line under the table's `files` unit,
  rather than the generic `calls` fallback.
- A `Bash` row shows `execLabel` output when no description is present, and the
  description when one is.
- `toolsPhrase` and `toolParts` agree on the same `Bash` call: the folded label
  and the expanded row show the same subject. This is the assertion that would
  have caught putting the preference in `toolParts` alone.
- `toolArgument` still returns the full command on a row showing an intent.

Commands: `uv run pytest`, `npm test --prefix ui-tui`, `make lint-python`.
`ui-tui` tests are run serially: the suite flakes above 100 files under default
worker parallelism.

## Domain terms

No new domain term. `intent` is a field name, not a concept: it is the model's
own `description` for a call, and the spec calls it that everywhere else.

## Risks

- **The derived set is most of the design.** Only `Bash` and `Read` were ever
  captured, and they are the two tools that need the least help. `Glob`,
  `Grep`, `TodoWrite`, `Agent` and MCP naming rest on adapter source. The
  mitigation is that each derived claim has a test asserting the predicted frame
  shape, so a wrong prediction fails a test rather than shipping.
- **A row can get longer.** A description is prose and `execLabel` output is two
  program names. Clipping already applies at 60 chars in `toolParts`.
- **`_meta.claudeCode.title` and `rawInput.description` are the same value.**
  The design reads the arguments JSON, which is the path that already reaches
  the client. If a future adapter version sends the meta without the rawInput
  field, the row silently falls back to `execLabel`, which is the current
  behaviour and not a regression.
