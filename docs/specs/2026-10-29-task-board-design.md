# The host's Task Board - design

Date: 2026-10-29
Status: designed, awaiting owner approval
Base: `855a412fa` on `main`. Line anchors below were taken on that commit.

## Why

A host conversation routinely carries several matters at once -- eight in the session that
prompted this design: a bug fix, a release note, a PR review, a slide rework, a research
errand, a cron job, a dependency bump, an issue sweep. Each advances a little per turn,
interleaved with the others, and the reader loses the thread: what each matter is waiting
on, what was already done, which ones were quietly dropped.

The desk's tasks tab (`ui-web/src/features/tasks/`) cannot answer that today. It lists
**Tasks** in the `CONTEXT.md` sense -- one row per `spawn` call or `run_subagent_dag` run,
flat, newest first -- so it shows the delegated work units but not the matter each one
served, and it shows nothing at all for a turn that advanced a matter without delegating.

This design adds a **Task Board** to the Raven host: a per-session record, kept by the
host model itself after every turn, that groups the conversation into **Task Groups** (one
per matter) and records each turn's advance on a group as a **Progress Step**, with the
turn's Tasks hung under the step that started them. The tasks tab draws the board; the host
model can recall any group's thread through the same tool that maintains it.

## Goals

1. The tasks tab shows one row per Task Group, newest-created first. Clicking a row expands
   its progress thread in place, under the row; the other rows stay where they are.
2. A thread is the group's Progress Steps in time order, one per turn that advanced the
   group, unnumbered. A turn that advanced three groups writes one step under each.
3. Every `spawn` / `run_subagent_dag` / `load_playbook` dispatch a turn made is assigned to
   one of that turn's steps before the turn's board update is accepted -- none is left over
   for a later turn to place.
4. A group whose matter is finished -- done, or dropped by the user -- is **Completed**: it
   moves into a fold at the bottom of the list, collapsed by default and expandable, and it
   ranks lower in recall.
5. After every host turn, the host model updates the board in one extra call that sees the
   turn's full window, so grouping is decided with everything the turn itself knew.
6. The host model can recall a group's thread -- by id, or by a free-text query in either
   language -- during any turn.

## Non-goals

- **Anyone but the Raven host.** Product agents (`agents/raven-*`) and every sub-agent get
  no board, no tool, no post-turn call.
- **Surfaces other than the web page.** The TUI and IM channels do not draw the board. The
  host still keeps it and can still recall from it on those channels.
- **The board in the turn's context.** The board is not injected into the system prompt;
  the model reads it through recall when it needs it.
- **Groups that span sessions.** One board per session; a new session starts empty.
- **Hand-editing the structure.** The reader can mark a group completed or reopen it; titles,
  merges and step placement are the model's.
- **Semantic (embedding) retrieval.** Recall is BM25 plus listing and lookup by id.
- **Backfilling sessions that predate the board.** Their Tasks show as unassigned (see 6.4).
- **Any change to `spawn`, `run_subagent_dag`, `load_playbook` or the DAG orchestration
  skill.** The board only reads what their receipts already say.

## Anti-goals

The feature fails, however well it otherwise works, if:

- the reply to the user waits on the board, or the next turn waits on the last turn's update;
- a dispatched Task can end a turn without a place on the board, with nothing saying so;
- the board update leaves anything in the conversation the model reads on later turns;
- steps paraphrase the reply ("answered the user's question") instead of saying what moved,
  or a new group appears for every turn.

## Vocabulary

Three terms join `CONTEXT.md` (Raven Runtime) and are cross-referenced from
`ui-web/CONTEXT.md`, in the same change. **Task** keeps its meaning -- one `tasks.list` row --
and is what hangs under a step.

- **Task Board** (`raven/agent/task_board/`): one session's set of Task Groups, kept by the
  host model after every turn. _Avoid_: "task list" (that is `tasks.list`), "todo"
  (Raven-Code's Checklist).
- **Task Group**: one matter the conversation is pushing, spanning any number of turns --
  a bug fix, a release note. Carries a title, a status and its Progress Steps.
  _Avoid_: "task" for this.
- **Progress Step**: one turn's advance on one Task Group, in a sentence or two, with the
  Tasks that turn dispatched for it. _Avoid_: "subtask" (it is a record of progress, not a
  unit of work), "step" alone (`Step body` and the node record's steps are other things).

## Design

### 1. The board and where it lives

One JSON file per session, `<session_dir>/task_board.json`, where `session_dir` is
`SessionManager.session_dir(key)` (`raven/session/manager.py:702`) -- the directory that
already holds the session's sub-agent records beside its transcript. Written atomically
(temporary file + rename) by a single writer per session (2.4).

```json
{
  "version": 1,
  "groups": [
    {
      "id": "g_1a2b3c4d",
      "title": "Fix bug A: blank page after login",
      "status": "completed",
      "created_at": 1793253600000,
      "updated_at": 1793262600000,
      "steps": [
        {
          "id": "s_5e6f7a8b",
          "turn_id": "<the turn's id>",
          "turn_at": "2026-10-29T10:02:13.512345",
          "text": "Found the cause: the restored session has an empty token. Proposed three fixes.",
          "tasks": [{"kind": "spawn", "id": "locate_login_bug", "summary": "Locate the blank-page bug"}]
        }
      ]
    }
  ],
  "unassigned": []
}
```

- `id`s are minted by the store, never by the model.
- `turn_at` is the timestamp of the message that opened the turn, the ISO string exactly as
  the session log stores it. It is the step's turn key: the anchor the page scrolls the transcript to (5.3) and what
  `session.undo` matches against (6.3). `turn_id` rides along for tracing and may be absent.
- A step's `tasks` entries carry `{kind, id}` in exactly the address `tasks.list` uses -- a
  spawn's `node_id`, a DAG run's `run_id` (`raven/rpc/methods/tasks.py:264`, `:447`) -- and
  the `summary` the dispatching call gave, so recall and an inert chip (5.2) still have a
  name. The page joins them to its `TaskRow`s by the existing `taskKey`.
- `unassigned` holds dispatches whose turn's update failed outright (6.1). It is empty in
  the normal course.

**Statuses.** Set by the model on every step it writes, and by the reader's two actions.

| status | en label | zh label | dot | meaning |
|---|---|---|---|---|
| `active` | In progress | 推进中 | moss | Raven or its sub-agents are moving it |
| `waiting` | Waiting on you | 等你 | gold | the latest step asks the user to confirm or decide |
| `blocked` | Blocked | 卡住 | clay | it failed or is stuck |
| `parked` | Parked | 搁置 | hollow ring | the user put it off |
| `completed` | Completed | 已完成 | faint | finished or dropped; nothing further will move it |

### 2. The `task_board` tool

One tool, registered in the host's main registry only, with two actions. One schema serves
both on purpose: the post-turn call (3) must send the turn's own tools array byte for byte,
because prompt caching matches the request prefix in the order tools, system, messages -- a
second schema present only in the post-turn call would miss the cache from its first token.

**Registration.** Beside the orchestration tools in `_register_orchestration_tools`
(`raven/agent/loop/wiring.py:1498`), under the same `not is_subagent_process()` gate
(`:1358`), and only while `agents.defaults.taskBoard.enabled` is true (7). The host sets
`RAVEN_SUBAGENT` on every child it launches (`raven/home.py:82`, `subagent_role_env`), so a
product agent the host runs never sees the tool; the five product configs also set
`taskBoard.enabled: false`, so a product served standalone does not either.

**Schema** (model-facing; parameter descriptions stay short -- the schema rides in every
host call, and its cost is pinned like A2A's is in `tests/test_agent_loop_token_budget.py`):

```
task_board(
  action: "recall" | "update",
  # recall
  query?: string,          # free text, any language
  group_id?: string,
  include_turns?: boolean, # append each step's turn, verbatim, within a token budget
  # update
  steps?: [{
    group?: string,        # an existing group id, completed ones included
    new_title?: string,    # instead of group: open a new group
    title?: string,        # optional retitle of an existing group
    text: string,          # what this turn moved, one or two sentences
    status: "active" | "waiting" | "blocked" | "parked" | "completed",
    tasks?: string[]       # dispatch handles, e.g. "spawn:locate_login_bug", "dag:<run_id>"
  }],
  statuses?: [{group: string, status: ...}]   # a status change with no step
)
```

**recall**, during any turn:

- no `query` and no `group_id`: every group, one line each -- id, title, status, last step,
  age -- completed groups after open ones;
- `group_id`: that group's whole thread, each step with its time and, for each linked Task,
  the Task's current status and summary as the `tasks.list` readers report them;
- `query`: BM25 (`raven/utils/bm25.py`, CJK-aware) over each group's title, step texts and
  linked Task summaries; a completed group's score is multiplied by `0.5`; returns the top
  group's whole thread plus the ids and titles of up to two runners-up, so the model can
  re-ask by id when the top hit is the wrong matter;
- `include_turns`: appends, per step, the turn's opening message and final reply as the
  session log holds them, inside a token budget reported when it binds -- the same contract
  `context_retrieve` keeps (`raven/agent/tools/context_retrieve.py`). A turn the log no
  longer holds is reported as gone, not skipped.
- A linked run's descendants (3, "Descendant runs") are reported with it.

**update** is never executed as a tool call: during a turn it is refused ("the board is
updated for you after the turn ends"). The post-turn runner (3) reads the arguments of the
forced call itself and hands them to the store. Inside that call the converse holds: a
`recall` counts as a failed attempt, since the instruction already carries the board.

### 3. The post-turn call

**Where.** `_process_message` (`raven/agent/loop/turn_path.py:2723`), after the turn is
saved and `harness.memory.after_turn` has run (`:3446`), beside Step 4's post-action
learning (`:3465`): launched as a background task held in `_consolidation_tasks`, so the
reply has already gone out and shutdown drains it like the others.

**Input.** The message list `_run_agent_loop` returned -- the window of the turn's last call
plus its final answer -- the same tools array and system message that last call sent, and
one appended user-role instruction rendered from `raven/templates/prompts/{en,zh}/
task_board_update.md` through `raven.i18n.prompt`. It is the shape the early-exit synthesis
call already uses (`turn_path.py:755-775`: "the turn's own conversation plus one
instruction"). The instruction carries:

- the board as it stands: every open group's id, title, status and last two steps; every
  completed group's id and title;
- this turn's dispatches, each as a handle (`spawn:<node_id>`, `dag:<run_id>`) with its task
  summary, followed by any `unassigned` left by an earlier failed update (6.1);
- the rules: one step per group this turn advanced, in the conversation's language, saying
  what moved and what is next or awaited -- not restating the reply; open a group only for a
  matter no existing group covers, reopening a completed one instead of duplicating it; no
  step for a turn that advanced nothing; mark a group completed when it is done or the user
  dropped it; place every listed handle exactly once.

**The dispatch list.** The turn's own tool rows, read the way `session.history` already reads
them (`raven/rpc/methods/session.py:439-448`): a `run_subagent_dag` or `load_playbook` call
whose receipt names a run id is `dag:<run_id>`; a `spawn` call whose receipt carries the
manager's started id is `spawn:<node_id>`, the `node_id` taken from the call's arguments,
because that is the id `tasks.list` addresses a spawn by. A call refused before dispatch has
no such receipt and is not a dispatch. The receipt parsing moves into one helper both readers
call, so the two cannot come to disagree about what a turn dispatched.

**Descendant runs.** Some runs start with no tool call of their own: a replan's successor
(`TaskRow.replan.run_id`) and every round after the first of a stint (graphs carrying
`stint_id` / `round_index`, recorded in `raven/stint/record.py`). They are never in a turn's
dispatch list and the model never places them; they belong to the step that placed the run
they descend from, and the page and recall resolve them there by lineage.

**Call.** `provider.chat_with_retry` with the turn's model and fallbacks, `tool_choice`
forced to `task_board`, under `_llm_purpose("task_board")` so tracing and usage accounting
name it. Reasoning effort comes from `taskBoard.reasoningEffort` (default `low`; never
`minimal`, which Codex rejects).

**Validation, then write.** The store applies an update only when:

- every handle in this turn's dispatch list (and in `unassigned`) is placed exactly once;
- no handle outside that list appears;
- every `group` names an existing group, and every `new_title` is non-empty and capped;
- `steps` is empty only when there were no handles to place.

A rejected update returns the precise reason as the tool result -- "not placed:
`dag:2026...-3eb3b470`" -- and the runner asks again with the transcript of the attempt
appended, up to three attempts. An accepted update is written, then announced as
`tasks.board_updated` (4).

**What it leaves behind.** Nothing in the session's messages: the post-turn exchange is
neither appended to the turn nor persisted. It is traced as its own span and its usage is
added to the session's usage, so its cost is visible where every other call's is.

**Which turns.** Every completed host turn, whatever opened it -- a user message, a
returning sub-agent result, a cron firing. A turn that errored or was stopped runs the call
only if it dispatched something. A turn that dispatched nothing may return an empty update.

**Ordering.** One update in flight per session: a per-session lock in the board service
queues the next turn's update behind the last. The next turn itself never waits.

### 4. Wire surface

All three are declared in `rpc-schema/openrpc.json`, mirrored in `raven/rpc/models.py`, and
generated into `ui-web/src/rpc/generated` (`npm run gen`); `tests/test_rpc_schema_match.py`,
`tests/test_rpc_contract_shapes.py` and the page's `scripts/gates/` hold them equal.

1. `tasks.board(session_key)` -> `{board: TaskBoard, lineage: {<run_id>: <run_id>}}`: the
   file as stored (or an empty board), and each descendant run (3, "Descendant runs") mapped
   to the placed run it descends from, computed on the server from `graph.json`'s `replan`
   and the session's stint records.
2. `tasks.set_group_status(session_key, group_id, status)` -> `{board}`: the reader's two
   actions, `completed` and `active` (reopen). Takes the same per-session lock as an update.
3. Event `tasks.board_updated` `{session_key, board}`: after every accepted write, from
   either writer. Sent through the emitter the delivery sink already uses
   (`raven/rpc/bootstrap.py:388`); the TUI binding (`raven/cli/tui_commands.py:412`) drops it.

### 5. The page

The tasks tab's list view in `features/tasks/TasksPage.tsx` becomes the board. The rest of
the domain stays: the docked task pane with its graph and node record, the task strip above
the composer, and the tab's running-count badge.

5.1 **Rows.** Open groups first, by `created_at` descending. Each row: the status dot, the
title (one line, ellipsis), the latest step's text (one line, muted), and at the right either
a live badge -- "2 sub-agents running", "DAG 2/4", computed from the group's linked
`TaskRow`s that are running -- or the age of the latest step. Rows are buttons with
`aria-expanded`, operable from the keyboard.

5.2 **Thread.** Clicking a row expands its thread in place, below the row; clicking again
collapses it. Several threads may be open at once. A vertical timeline, no numbers. Each step
shows its text, its time (`HH:MM`, with the date when not today), and one chip per linked
Task and per descendant run of one -- kind, agent or run summary, status glyph. A chip opens
the existing task pane the way a list row does today (`desk.openDeskTask`). A chip whose
Task `tasks.list` no longer holds -- a branched session, a removed run -- renders dimmed and
inert, named by its stored summary. When `tasks.list` gains a dag row that neither a step nor
`lineage` places, the page re-reads `tasks.board` once, since a new round or successor may
have started.

5.3 **From a step to its turn.** Clicking a step's text scrolls the transcript to the
message whose timestamp is the step's `turn_at`.

5.4 **Completed.** Below the open groups, a fold `Completed - N` / `已完成 - N`, collapsed by
default. Expanded, it lists completed groups by `created_at` descending, dimmed, each
expandable to its thread like any other. Every row's overflow menu holds the reader's one
action: "Mark completed" on an open group, "Reopen" on a completed one.

5.5 **Unassigned.** A Task the session's `tasks.list` holds but no step links, directly or
by lineage -- a session from before the board, or an update that failed outright -- shows in
an `Unassigned` /
`未归类` row at the top, listing those Tasks as chips. With the board switched off this row
holds every Task, which is the old flat list in one fold.

5.6 **Live.** `state/session/stages.ts` arms `tasks.board_updated` the way it arms
`subagent.status`, replacing the open session's board; switching session or reconnecting
re-reads `tasks.board`.

Strings go through both catalogs (`ui-web/src/i18n/`); styles stay under the domain's `tk`
prefix and the page's existing colour variables (`--moss`, `--amber`, `--clay`, `--faint`).

### 6. Failure and edge cases

6.1 **The update cannot be had.** When the post-turn call fails after the provider's retries,
or three attempts all fail validation, the turn's handles are appended to `unassigned` with
the turn's id, a warning is logged, and the next turn's update must place them before its
own. Until then the page shows them under Unassigned (5.5).

6.2 **Forced tool choice refused.** Some reasoning models reject a forced `tool_choice`; the
Anthropic provider already downgrades to `auto` (`anthropic_messages_provider.py:506`). A
reply with no `task_board` call counts as a failed attempt under 3's limit.

6.3 **History edits.** A step never points at a turn the log no longer holds. `session.undo`
drops the steps whose `turn_at` belongs to a turn it removed, and groups left with no steps;
`session.clear`
empties the board; `session.branch` copies it with the transcript. Compaction leaves the
board alone -- surviving compaction is part of its purpose.

6.4 **Sessions from before the board.** No file: `tasks.board` answers an empty board and
the page shows the session's Tasks under Unassigned. No backfill.

### 7. Configuration

`agents.defaults.taskBoard` in `raven/config/schema.py` (`AgentDefaults`, `:182`):

```json
"taskBoard": {"enabled": true, "reasoningEffort": "low"}
```

`enabled: false` removes the tool and the post-turn call. Each `agents/*/config.json` sets
it false.

### 8. Tests

- Python, under `tests/` per AGENTS.md 5: the store (atomic write, ids, the five statuses,
  undo/clear/branch), validation (every refusal reason, the three-attempt limit, `unassigned`
  carried into the next update), recall (listing, lookup, BM25 with the completed weight,
  `include_turns` budget), registration (host only; absent under `RAVEN_SUBAGENT` and with
  `enabled: false`), the post-turn runner (no messages persisted, same tools array as the
  turn's last call, lock ordering, usage counted), the tool-surface token cost pinned, and
  the RPC methods and event against the schema.
- Page: `features/tasks` tests for ordering, the accordion, the Completed fold, Unassigned,
  chips opening the task pane, the step-to-turn scroll, the overflow actions, and the live
  event; the existing domain gates.
- One real-provider smoke under `tests/integration/`, on the live-test model, driving three
  turns over two matters with one spawn and checking the board it leaves.

## Risk

- **Cost.** Every host turn makes one more call carrying the full window. Where the provider
  caches by prefix (Anthropic, and OpenAI-compatible endpoints that cache) nearly all of its
  input is a cache read; elsewhere the turn's input cost roughly doubles. The call is visible
  in usage and the switch turns it off.
- **Latency.** None on the reply; the board settles a few seconds after it.
- **Rollback.** `taskBoard.enabled: false`. The page then shows every Task under Unassigned,
  which is the old list.
