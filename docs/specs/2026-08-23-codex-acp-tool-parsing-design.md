# codex-acp tool parsing - design

Date: 2026-08-23
Status: designed
Base: `feat/subagent_everos_memory_record` at `b7b05841`; `origin/main` at
`ffecc51a`.

Measured against `@agentclientprotocol/codex-acp@1.1.14` (`agentInfo.name`
`@agentclientprotocol/codex-acp`, `agentInfo.title` `Codex`), model
`gpt-5.6-sol`. Two frame captures back this spec: a real direct chat (323
frames, `~/.raven/traces/logs/acp-frames/2026-08-23/Writer-092353638638.jsonl`)
and a purpose-built probe (222 frames) driving a four-step task chosen to force
a plan, a patch and two shell commands.

## Goal

A direct chat with codex renders its tool calls incompletely. Name every codex
tool by what codex itself calls it, give each row a subject that says what the
call touched, and stop dropping the plan.

## Non-goals

- Renaming codex's tools into raven's vocabulary. The opposite is the point: a
  codex row should be recognisable as codex at a glance.
- Changing how claude-agent-acp, opencode, or the spec default are read. Two
  shared changes land in the base regardless: the `_revise_call` name guard
  (section 3) and the plan branch (section 5) -- `elif kind == "plan"` is
  dialect-independent, so any adapter's plan frame collapses to one row rather
  than one per snapshot. The row's *name* is still the dialect's own:
  `plan_tool_name` is `"plan"` on the base and inherited by claude-agent-acp,
  and only codex overrides it to `update_plan`. Everything else is codex-only.
- Rendering codex's `terminal` content stream. The intermediate
  `_meta.terminal_output_delta` frames are dropped as they are today; the
  completed frame carries the whole output.

## What the wire actually carries

Measured, not inferred. The probe's frames are the evidence for every row.

`codex-acp` reports a tool call three different ways depending on what it is,
and none of the three is codex's model-facing tool name:

- The ACP spec's `kind` enum (`execute`, `read`, `search`, `edit`, `other`).
- A `title` written for a human (`Read file '<path>'`, `Web search: <q>`,
  `Editing files`, `apply_patch`).
- Discriminators in `rawInput` and `_meta` that no other adapter sends.

Grepped across 222 probe frames plus the 323-frame session: `"shell"`,
`update_plan`, `view_image`, `"web_search"`, `exec_command`, `toolName` and
`tool_name` appear zero times. There is no field anywhere carrying codex's
model-facing tool name.

Two names survive anyway, because codex invokes those tools as shell commands
and the command string *is* on the wire:

```json
{ "sessionUpdate": "tool_call", "kind": "execute", "title": "apply_patch",
  "rawInput": { "command": "apply_patch", "cwd": "/tmp/codexprobe/ws" } }
```

`apply_patch` is argv[0] of a real command (`codex-rs/apply-patch/src/invocation.rs`),
and the patch body arrives on stdin. `mcpToolCall` and `dynamicToolCall`
likewise put the real tool name in `title`.

### The lossy re-badging

`createCommandActionEvent` in the adapter re-classifies one
`commandExecution` into four shapes using codex's own `ParsedCommand`
(`codex-rs/protocol/src/parse_command.rs`). Three of them **drop the command**:

| `ParsedCommand` | frame | carries |
|---|---|---|
| `Read` | `kind: "read"`, title `Read file '<p>'` | `locations` only |
| `ListFiles` | `kind: "read"`, title `List files in '<p>'` | nothing |
| `Search` | `kind: "search"`, title from query+path | nothing |
| `Unknown` | `kind: "execute"` | `rawInput.command` |

The command is recoverable, on a different frame. `session/request_permission`
carries `params.toolCall.rawInput.command` for the same `toolCallId`:

| frame | content |
|---|---|
| `tool_call` #42 | `kind: "read"`, title `Read file '.../calc.py'`, no `rawInput` |
| `request_permission` #43 | same `toolCallId`, `rawInput.command` = `sed -n '1,200p' calc.py` |

Raven answers that request in `raven/agent/acp/permissions.py` and discards the
params. The journal already records the frame, so this is a transcript gap, not
a capture gap.

Coverage is high but not total: codex stops asking once a command pattern is
approved for the session (the probe's second `apply_patch` got no permission
frame). Recovery is therefore opportunistic, and the fallback is codex's own
path or title.

### What breaks today

Replaying the probe's real frames through `_TurnCollector` + `CodexDialect` +
`normalize_row`:

```
CALL read_file   {"path": ".../calc.py"}
CALL exec        {"command": "apply_patch", "cwd": "..."}   <- edited calc.py
CALL exec        {"command": "python3 -c \"import calc; print(calc.add(2,3))\""}
CALL exec        {"command": "apply_patch", "cwd": "..."}   <- edited notes.txt
```

1. All five `plan` frames are dropped. Four checklist entries and their
   progression leave no trace.
2. Two different edits render identically as `exec apply_patch`. The file is
   only in the result text.
3. Terminal output arrives CRLF-terminated.
4. `webSearch` and `mcpToolCall` are renamed to the fallback `tool_call` -- see
   section 3.
5. `fileChange` / `kind: "edit"` never fired in either capture; codex took the
   shell `apply_patch` path both times. Its handling below is derived from the
   adapter source and is explicitly unmeasured.

## Design

### 1. The names codex rows carry

The dialect reports codex's own name at the finest grain the wire supports,
preferring a model-facing name where one survives. Discriminators are checked
in this order; the first match wins.

| evidence on the frame | name | source |
|---|---|---|
| `rawInput.command` argv[0] == `apply_patch` | `apply_patch` | model-facing |
| `sessionUpdate: "plan"` | `update_plan` | model-facing, 1:1 with `plan_tool.rs` |
| `_meta.is_mcp_tool_call` | `mcp.<server>.<tool>` | model-facing, from `rawInput` |
| `kind: "execute"` + `rawInput.arguments`, no `command` | the `title` | model-facing, `dynamicToolCall` |
| `rawInput.type == "webSearch"` | `webSearch` | item type |
| `_meta.codex.collaboration` | `collabAgentToolCall` | item type |
| `_meta.codex.subagent` | `subAgentActivity` | item type |
| `_meta.contextCompaction` | `contextCompaction` | item type |
| `kind: "other"` + title `Image generation` | `imageGeneration` | item type |
| `kind: "read"` + title `View Image ` | `imageView` | item type |
| `kind: "edit"` | `fileChange` | item type, **unmeasured** |
| `kind: "read"` + title `List files` | `commandExecution.listFiles` | `ParsedCommand` |
| `kind: "read"` otherwise | `commandExecution.read` | `ParsedCommand` |
| `kind: "search"` otherwise | `commandExecution.search` | `ParsedCommand` |
| `kind: "execute"` | `commandExecution` | item type |
| none of the above | `kind` verbatim, else `tool_call` | the spec |

Three rows discriminate on `title` because nothing else on the frame separates
them: `imageView`, `commandExecution.listFiles` and `commandExecution.read` all
arrive as `kind: "read"`. That is a documented fragility, pinned to the version
measured, and a title the adapter rephrases upstream degrades to
`commandExecution.read` rather than to nothing.

### 2. The subject each row shows

The subject must be the first string value in the stored arguments, under a key
that names it truthfully, because a reader that does not know the tool takes the
first string it finds (`tool_vocabulary._promote`). `ARGUMENT_KEY` gets no
codex entries: the dialect supplies the key, so a promotion keyed on a raven
name cannot mislabel a codex field.

| name | subject | key |
|---|---|---|
| `apply_patch` | the files, from the patch envelope | `path` |
| `commandExecution` | `rawInput.command` | `command` |
| `commandExecution.read` / `.listFiles` / `.search` | recovered command, else `locations[0].path`, else title | `command` / `path` |
| `webSearch` | switches on `action.type` first: `openPage` takes `action.url`, `findInPage` takes `action.pattern`; otherwise `action.queries` joined, else `action.query`, else `query` | `query` |
| `mcp.<server>.<tool>` | first string in `rawInput.arguments`, else none | `argument` |
| the `title` (`dynamicToolCall`) | first string in `rawInput.arguments`, else none | `argument` |
| `update_plan` | the in-progress step, else `<n> steps` | `argument` |
| `imageView` | `rawInput.path` | `path` |
| `imageGeneration` | `rawOutput.savedPath` | `path` |
| `subAgentActivity` | leaf of `_meta.codex.subagent.path` | `argument` |
| `collabAgentToolCall` | `rawInput.prompt` | `prompt` |
| `fileChange` | the paths, from the `diff` content blocks | `path` |
| `contextCompaction` | none | -- |

The web-search subject reimplements the adapter's own `formatWebSearchTitle`
selection rather than taking the title, because the title is prefixed
(`Web search: `) and a prefix in a subject reads as part of the value.

`apply_patch`'s subject arrives *after* the call, in the result: the envelope
`*** Begin Patch` / `*** Update File:` / `*** Add File:` / `*** Delete File:`
(codex's own grammar, `codex-rs/apply-patch`). So the dialect gains one hook the
base does not have:

```
AcpDialect.subject_from_result(update, *, name=None) -> str | None
```

The caller supplies `name` from the call's opening frame, and an unnamed call returns `None` — the completed frame cannot be identified on its own.
Default `None`. `CodexDialect` implements it for `apply_patch`. The collector
calls it on a completed frame and back-fills the call's argument. This is the
same back-fill shape `_revise_call` already uses, on a different frame.

### 3. A kind-less update must not rename the call (base, all adapters)

`_revise_call` guards `title`, `argument` and `raw_input` with
`revised.x or previous.x`, but takes `name=revised.name` unguarded. codex's
completing frames do not repeat `kind`, so `tool_name()` falls back to
`_FALLBACK_TOOL` and overwrites the opening frame's correct name. Measured on
the real session: `kind: "search"` became `tool_call` for both web searches.

The base gains a predicate:

```
AcpDialect.names_call(update) -> bool     # default: bool(update["kind"])
```

`_revise_call` takes the revised name only when it returns True.
`CodexDialect` also returns True when any section-1 discriminator is present,
so a codex frame that names the call without a `kind` still counts.

This is the one change outside codex. claude-agent-acp repeats `kind` on its
updates (measured), so its behaviour is unchanged.

### 4. Recovering the command from the permission frame

`session/request_permission` carries `sessionId`, so the router that already
fans notifications out per session can carry it too.

- `auto_approver(name, observe=None)` gains an optional observer. `pool.py`
  wires `observe=router.dispatch`; `capabilities.py` leaves it unset.
- The observer runs after the outcome is computed and inside a `try`. An
  observer that fails must not delay or change the approval -- an unanswered
  permission request cancels the whole turn (the reason
  `permissions.py` exists).
- The collector handles `session/request_permission` as a **subject-only**
  revision. The command comes from `_meta.codex.params.commandActions[].command`
  -- codex's own parse -- rather than `toolCall.rawInput.command`, which is the
  argument to `bash -lc` and arrives wrapped in its own quotes. Deliberately not
  the frame's `kind`, which is `execute` even for a call the session update
  badged `read`; letting it rename the call would erase the `ParsedCommand`
  distinction section 1 just recovered.
- Frame order is `tool_call` then `request_permission`, so this is always a
  back-fill onto an existing call, never a new one.

No new data reaches disk: the journal already records these frames.

### 5. The plan becomes one `update_plan` row

`sessionUpdate: "plan"` is a whole-list snapshot, re-sent on every change (five
times in the probe for one four-entry plan). One row per frame would put five
near-identical rows in a transcript.

- The first plan frame of a turn opens one `update_plan` call, with a synthetic
  stable id, and breaks the message like any other call.
- Later plan frames revise that call in place and do **not** break the message,
  so narration around a moving plan is not fragmented.
- The result text is the checklist, rendered from `entries[].status`:

```
[x] Show the contents of calc.py with a shell command
[x] Fix add() using the patch tool
[>] Run the requested Python verification command
[ ] Append "verified" as one line in notes.txt
```

The opening frame sets the message break the way a `tool_call` does; a
revision does not. `plan` is therefore handled in its own branch rather than
added to `_BREAKING_UPDATES`, whose members break unconditionally.

### 6. CRLF

Codex's terminal output is CRLF-terminated. `CodexDialect.result` normalises
CRLF to LF and strips C0 control characters other than tab and newline, before
the `_RESULT_TEXT_CAP` slice.

### 7. Where the code goes

| file | change |
|---|---|
| `raven/acp_client/acp_dialects/codex.py` | grows from a result/argument patch into the full parser: sections 1, 2, 6 |
| `raven/acp_client/acp_dialects/base.py` | `names_call`, `subject_from_result` hooks (defaults only) |
| `raven/acp_client/acp_agent.py` | name guard, permission branch, plan branch |
| `raven/agent/acp/permissions.py` | `observe` parameter |
| `raven/agent/acp/pool.py` | wire `observe=router.dispatch` |
| `raven/agent/subagent/tool_vocabulary.py` | no change; codex names are absent from `RAVEN_NAME` and pass through |
| `ui-tui/src/domain/codexTools.ts` | new: codex's verb rules, so folding survives |
| `ui-tui/src/domain/episodeSummary.ts` | export `VerbRule`; consult `CODEX_VERBS` before the humanize fallback; apply the existing shell-program naming to `commandExecution` as it already does to `exec` |

`tool_vocabulary.py` needing no change is the check that section 1 is
consistent: codex's names are not raven's, so nothing renames them, and the
adapters that do send raven-mappable names are untouched.

## Testing

Frame fixtures come from the two captures, not from hand-written JSON, so a test
that passes describes something the adapter really sent.

| test | covers |
|---|---|
| `tests/test_acp_dialects.py` | section 1 name table, one case per row; section 2 subjects; CRLF; `names_call` |
| `tests/test_subagent_acp.py` | the name guard on a kind-less update; the permission back-fill; the plan row opening once and revising |
| `tests/test_subagent_tool_vocabulary.py` | codex names survive `normalize_row` unrenamed; subject is the first string value |
| `ui-tui/src/__tests__/episodeSummary.test.ts` | codex verbs resolve; a run of `commandExecution.read` folds by count |
| `ui-tui/src/__tests__/directEpisodes.test.ts` | a codex turn folds into episodes with the codex names intact |

Full-suite commands: `uv run pytest tests/test_acp_dialects.py
tests/test_subagent_acp.py tests/test_subagent_tool_vocabulary.py` and
`npm test --prefix ui-tui`. The TUI suite is run serially (it flakes above ~100
files under default worker parallelism), and `raven tui` loads the prebuilt
`ui-tui/dist/entry.js`, so a manual check needs
`npm run build --prefix ui-tui` first.

## Domain terms

New terms to define in `CONTEXT.md` (runtime) in the same change:

- **dialect discriminator** -- the field on an ACP frame that identifies which
  of one adapter's tools a call is, when `kind` cannot.
- **subject back-fill** -- setting a call's subject from a later frame: a
  `tool_call_update`, a completed result, or a permission request.

`ui-tui/CONTEXT.md` gains **codex verb rule** for the `CODEX_VERBS` table.

## Risks

- **Title-discriminated rows** (`imageView`, `commandExecution.listFiles`).
  An adapter that rephrases a title degrades them to
  `commandExecution.read`. Pinned to 1.1.14 and asserted from a captured frame,
  so an upgrade fails a test rather than silently mislabelling.
- **`fileChange` is unmeasured.** Its row is written from adapter source and
  never fired in either capture. Marked as such in the code comment; the first
  real frame that hits it is the confirmation.
- **Permission observer on the approval path.** A bug there could stall a
  permission request and cancel turns. Mitigated by computing the outcome first
  and wrapping the observer in a `try`, and by a test that an observer raising
  still yields the approval.
- **Codex names bypass raven's verb table.** Without `CODEX_VERBS` a codex
  conversation still folds by count; every row falls back to a humanized tool
  name and a generic `calls` unit, so a run reads `commandExecution.read 3
  calls` instead of `commandExecution.read 3 files`. The TUI table is part of
  this change, not a follow-up.
