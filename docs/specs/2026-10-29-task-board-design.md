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
- **Task Group** (任务 on Chinese surfaces): one matter the conversation is pushing,
  spanning any number of turns -- a bug fix, a release note. Carries a title, a summary of
  where it stands, a status and its Progress Steps. _Avoid_: "task" for this in English.
- **Progress Step** (事件 on Chinese surfaces): one turn's advance on one Task Group -- a
  title and a summary -- with the Tasks that turn dispatched for it. _Avoid_: "subtask" (it is a record of progress, not a
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
      "summary": "Fixed and merged; the user confirmed the page loads.",
      "status": "completed",
      "created_at": 1793253600000,
      "updated_at": 1793262600000,
      "steps": [
        {
          "id": "s_5e6f7a8b",
          "turn_id": "<the turn's id>",
          "turn_at": "2026-10-29T10:02:13.512345",
          "title": "Found the cause, proposed three fixes",
          "summary": "Asked the user which of three fixes to apply. The blank page comes from the restored session carrying an empty token.",
          "questions": ["Which of the three fixes should I apply?"],
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
  the session log stores it. It is the step's turn key: what `session.undo` matches against
  (6.3), and how the page finds a turn read back from history (5.3). `turn_id` is the id the
  turn's opening frame named, by which the page finds a turn opened on the open page (5.3);
  it may be absent.
- A step's `tasks` entries carry `{kind, id}` in exactly the address `tasks.list` uses -- a
  spawn's `node_id`, a DAG run's `run_id` (`raven/rpc/methods/tasks.py:264`, `:447`) -- and
  the `summary` the dispatching call gave, so recall and an inert chip (5.2) still have a
  name. The page joins them to its `TaskRow`s by the existing `taskKey`.
- A step's `summary` is what recall reads back. On the step that leaves its group
  `needs_you` it opens with what the user is asked about the matter, and the page shows it
  under that step while the group waits (5.2). That step also lists, in `questions`, each
  question the reply asked about the matter -- required there, refused on any other step --
  which the page frames on the card, with Answer and Ignore (5.1). `needs_you` is set on a
  step only, never through `groups`, so the step the page reads is the one that asked.
- `unassigned` holds dispatches whose turn's update failed outright (6.1). It is empty in
  the normal course.

**Statuses.** Set by the model on every step it writes, and by the reader's two actions.

| status | en label | zh label | colour | meaning |
|---|---|---|---|---|
| `needs_you` | Needs you | 需要你回答 | amber | the reply asks the user to answer, confirm or decide about this matter -- an error only the user can resolve included -- even while a run it started still works |
| `running` | Running | 运行中 | sky, a turning ring | a run it started is still working |
| `idle` | Idle | 闲置 | moss (green) | nothing is in flight and nothing is asked |
| `completed` | Completed | 已完成 | faint (grey) | finished or dropped; nothing further will move it |

Any other value is refused. What the page shows of a group is its Board category
(`CONTEXT.md`), the same four: `needs_you` and `completed` stand as written, and between
`running` and `idle` the live runs decide -- Running while a Task the group links runs, Idle
otherwise -- so a run the model believed still going cannot leave a ghost behind.

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
`taskBoard.enabled: false`, so a product served standalone does not either. The tool is
resident under tool-search compaction (`DEFAULT_ALWAYS_VISIBLE`,
`raven/agent/tools/tool_search.py`): the post-turn call forces it by name.

**Schema.** One description and six bare properties: `action` (`"recall" | "update"`),
`query`, `group_id`, `include_turns`, and `steps` / `groups` as arrays of objects with no
nested fields. The schema rides in every host call: nested, the update's shape cost about 514
tokens a call; bare, about 240, pinned as an increment beside A2A's in
`tests/test_agent_loop_token_budget.py`. The shape is stated instead by the post-turn
instruction (3), once per turn. The description also tells the model never to call `update`
itself -- a live model did, mid-turn, when it only said the board was kept for it.

The update's shape, as the post-turn instruction states it:

```
task_board(
  action: "recall" | "update",
  # recall
  query?: string,          # free text, any language
  group_id?: string,
  include_turns?: boolean, # append each step's turn, verbatim, within a token budget
  # update
  steps?: [{               # the turn's events, one per group it advanced
    group?: string,        # an existing group id, completed ones included
    new_title?: string,    # instead of group: open a new group, with
    new_summary?: string,  #   its summary
    title: string,         # what moved -- all the page shows of the step
    summary: string,       # what moved and what is next, any length; with needs_you, the ask first
    status: "needs_you" | "running" | "idle" | "completed",  # the group's, after the turn
    questions?: string[],  # with needs_you, and only then: each question asked about this matter
    tasks?: string[]       # dispatch handles, e.g. "spawn:locate_login_bug", "dag:<run_id>"
  }],
  groups?: [{              # changes to an existing group's own fields, only those that change
    group: string,
    title?: string,        # a clearly better name
    summary?: string,      # where the matter stands now, whenever the turn changed it
    status?: ...           # only for a group with no step this turn, never needs_you: the step sets its group's
  }]
)
```

**Lengths.** Counted the way a Chinese word count reads text (`word_count`): each CJK
character is one, each word of any other script is one, so one limit reads as characters to a
Chinese writer and as words to an English one. A title -- a group's or a step's -- is at most
20; a group's summary and each question at most 50; a step's summary has no limit. An update over a limit is
refused with the measured length and the start of the text, and the model rewrites it within
the same three attempts (6.1); nothing is cut silently.

**recall**, during any turn:

- no `query` and no `group_id`: every group, one line each -- id, title, status, summary,
  age -- completed groups after open ones;
- `group_id`: that group's whole thread, its summary, and each step with its time, title and
  summary and, for each linked Task, the Task's current status and summary as the
  `tasks.list` readers report them;
- in both, a group that waits on the user (`needs_you`) closes with its unanswered questions:
  its latest step's, numbered as the page numbers them. One the user set aside has left the
  step already; an earlier step's were answered by the turns after it, and a group no longer
  waiting asks nothing;
- `query`: BM25 (`raven/utils/bm25.py`, CJK-aware) over each group's title and summary, its
  steps' titles and summaries, and linked Task summaries; a completed group's score is multiplied by `0.5`; returns the top
  group's whole thread plus the ids and titles of up to two runners-up, so the model can
  re-ask by id when the top hit is the wrong matter;
- `include_turns`: appends, per step, the turn's opening message and final reply as the
  session log holds them, inside a budget of 4000 tokens that cuts each turn rather than
  leaving turns out: a turn that fits an even share stays whole, the long ones split what is
  left evenly, and a turn's question and reply share its part the same way, each cut marked.
  Only when even 100 tokens a turn will not fit are the oldest turns left out, and the
  reading opens by saying how many. A turn the log no longer holds is reported as gone, not
  skipped.
- A linked run's descendants (3, "Descendant runs") are reported with it.

**update** is never executed as a tool call: during a turn it is refused ("the board is
updated for you after the turn ends"). The post-turn runner (3) reads the arguments of the
post-turn call itself and hands them to the store. Inside that call the converse holds: a
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

- the board as it stands: every open group's id, title, status, summary and the titles of
  its last two steps; every completed group's id and title;
- this turn's dispatches, each as a handle (`spawn:<node_id>`, `dag:<run_id>`) with its task
  summary, followed by any `unassigned` left by an earlier failed update (6.1);
- the rules: one step per group this turn advanced, in the conversation's language, its title
  saying what moved and its summary what moved and what is next or awaited -- not restating
  the reply; the lengths above; rewrite a group's summary whenever the turn changed where the
  matter stands; a reply that asks the user something about a matter makes that group
  `needs_you`, even while a run it started still works, the step's summary opens with the
  ask and its `questions` list each question about the matter -- a question about another
  matter belongs to that matter, and leaves this one as it is; open a group only for a
  matter no existing group covers, reopening a completed one instead of duplicating it; no
  step for a turn that advanced nothing; mark a group completed when it is done or the user
  dropped it; place every listed handle exactly once.

Both blocks sit between `[BEGIN ...]` / `[END ...]` markers under a sentence calling them
records to read as data: they hold text earlier calls wrote, sometimes carried over from tool
output, and the instruction is a user-role message. The rules also say what a group is: the
matter the user pursues, titled by the matter rather than by one action inside it, with every
request a matter even when the turn finished it.

**The dispatch list.** The turn's own tool rows -- what the turn added (`TurnLog.added`),
never the history it opened with, where every earlier turn's spawns still sit -- read the way
`session.history` already reads
them (`raven/rpc/methods/session.py:439-448`): a `run_subagent_dag` or `load_playbook` call
whose receipt names a run id is `dag:<run_id>`; a `spawn` call whose receipt carries the
manager's started id is `spawn:<node_id>`, the `node_id` taken from the call's arguments,
because that is the id `tasks.list` addresses a spawn by. A call refused before dispatch has
no such receipt and is not a dispatch. The receipt parsing moves into one helper both readers
call, so the two cannot come to disagree about what a turn dispatched. A call made through
the `tool_call` meta-tool -- how `run_subagent_dag`, never resident, is reached under
tool-search compaction -- is read as the call it forwards.

**Descendant runs.** Some runs start with no tool call of their own: a replan's successor
(`TaskRow.replan.run_id`) and every round after the first of a stint (graphs carrying
`stint_id` / `round_index`, recorded in `raven/stint/record.py`). They are never in a turn's
dispatch list and the model never places them; they belong to the step that placed the run
they descend from, and the page and recall resolve them there by lineage.

**Call.** `provider.chat_with_retry` under `_llm_purpose("task_board")` so tracing and usage
accounting name it, with the turn's own `tool_choice` -- nothing forces the tool. A forced
choice would cost the cache it is meant to share: on Anthropic a `tool_choice` change keeps
the tools and system tiers but invalidates the messages tier, the whole history. And several
providers refuse one outright: the newest Claude models answer a forced `tool`/`any` with a
400, and some OpenRouter routes take only `auto`, `none` or `required`. The instruction asks
for the call; an answer in text is asked again (6.2). It runs at the main model's settings, none of them configured for the board: the
model, fallbacks and effort the turn's last call ran at (`LoopOutcome.last_model` /
`last_effort`; no effort is passed when the turn passed none). Taken from the turn rather
than re-read from config, because a conversation can pin its own model and effort, the router
can pick another model per message, a turn can have fallen back mid-way, and Anthropic voids a cached message prefix when the thinking
settings change between two calls. The call is skipped when the turn's tools array did not
carry `task_board` -- a deploy that lists it in `disabledTools` -- since the model would have
nothing to answer the instruction with.

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

**Ordering.** Two locks per session in the board service. The update queue holds a
post-turn update across its model call, so the next turn's update is built on this one's
result; the next turn itself never waits. The write lock covers only reading, changing and
writing the file, for every writer -- the update's last step on a fresh read, the reader's
two actions, clear and undo -- so none of those waits on a model call. A clear moves the
session's generation and an undo marks the turns it removed; an update still on its way
checks both before it writes, and writes nothing for a turn that is gone.

### 4. Wire surface

All four are declared in `rpc-schema/openrpc.json`, mirrored in `raven/rpc/models.py`, and
generated into `ui-web/src/rpc/generated` (`npm run gen`); `tests/test_rpc_schema_match.py`,
`tests/test_rpc_contract_shapes.py` and the page's `scripts/gates/` hold them equal.

1. `tasks.board(session_key)` -> `{board: TaskBoard, lineage: {<run_id>: <run_id>}}`: the
   file as stored (or an empty board), and each descendant run (3, "Descendant runs") mapped
   to the placed run it descends from, computed on the server from `graph.json`'s `replan`
   and the session's stint records.
2. `tasks.set_group_status(session_key, group_id, status)` -> `{board, lineage}`: the
   reader's two actions, `completed` and `idle` (reopen, and Ignore on a group that needs the
   user). Takes the write lock only.
   `invalid_params` for an unknown group, for another status, and on a runtime that keeps
   no board.
3. `tasks.ignore_question(session_key, group_id, step_id, question)` -> `{board, lineage}`:
   the reader sets one question aside; the question leaves its step, and a group that waits
   on the user goes `idle` once its latest step asks nothing more. Takes the write lock only;
   `invalid_params` for an unknown group or step and on a runtime that keeps no board; a
   question the step no longer lists changes nothing.

   Both set-asides -- a question, and Ignore on a group that waits without naming one -- are
   told to the host at its next turn. The reply that asked is still in its history and
   nothing there says the user declined, so it would ask again and the post-turn update would
   put the question back. Each set-aside is kept as a note beside the board
   (`task_board_notes.json`, `raven/agent/task_board/notes.py`) and shown as a line of the
   runtime context ("the user set aside ... without answering it; it is declined, not open")
   on every turn until one that showed it has run. A note made while a turn runs waits for
   the next one. Marking completed and reopening are not set-asides. Clearing the
   conversation drops the notes, and a branch takes them along with the board.
4. `tasks.move_on(session_key, group_id)` -> `{noted}`: the reader's next message moves this
   group on (5.1.1). Takes the write lock only; `invalid_params` for an unknown group and on a
   runtime that keeps no board.
5. Event `tasks.board_updated` `{session_key, board}`: after every accepted write, from
   either writer. Sent through the emitter the delivery sink already uses
   (`raven/rpc/bootstrap.py:388`); the TUI binding (`raven/cli/tui_commands.py:412`) drops it.

### 5. The page

The tasks tab's list view in `features/tasks/TasksPage.tsx` becomes the board. The rest of
the domain stays: the docked task pane with its graph and node record, the task strip above
the composer, and the tab's running-count badge.

5.1 **Cards.** Open groups first, by `created_at` descending, one card each. The card's head
is a button with `aria-expanded`, operable from the keyboard: the group's type on a tinted
pill -- its Board category, the Task Board page's four words and colours (1, Statuses) -- with,
at the right, either a live badge -- "2 sub-agents running", "DAG 2/4", computed from the
group's linked `TaskRow`s that are running -- or the age of the latest step; then the title
(bold), the group's summary (muted, two lines; no line while a group has none) and, while the
card is closed, a foot under the head and outside it, counting its events and sub-agents the
way the Task Board page counts them, with Move on at its right (5.1.1). While a group needs the user, the card lists the questions its latest step asked at
its bottom -- under the head while it is folded, after that latest step while it is open --
each in a box of its own with an Ignore that sets that one question aside
(`tasks.ignore_question`: it leaves its step, so it leaves the card and the answer draft, and
the last one set aside leaves the group `idle`). One Answer button puts a draft in the open
conversation's composer -- the group named, each question still listed numbered with an
answer slot under it, the cursor in the first slot, anything already typed kept above it.
Open, the card's last row holds Mark completed at the left, and Move on and Answer at the
right. A group that waits on the user without naming a question keeps a single Ignore, which
sets it `idle`.

5.1.1 **Move on.** Every group, folded or open, offers Move on: the reader's next message is
about this matter. It puts a bar at the top of the composer card (`MoveOnBar`) -- the group's
title with its id, then its summary, one line each, and a cross that puts it away -- and the
reader in the composer. The bar is the page's alone until the message goes: the send first
tells the server (`tasks.move_on`, below) and then sends the message, and the bar goes with
it. The server keeps a note beside the board, as it keeps a set-aside, and the turn the message
opens reads, in its runtime context, which group it moves on, by id, title and where it
stands; the turn's own board update reads the same line. A message sent while a turn runs
waits for a turn of its own rather than joining that one, whose context was read before it. A
later move-on replaces one not yet shown. The Task Board page offers it on every card and row,
opening the conversation first, as an event does.

5.2 **Thread.** Clicking a card's head expands its thread inside the card, under the head;
clicking again collapses it. Several threads may be open at once. A vertical timeline, no
numbers, each step marked by the Tasks it started: a sky turning ring while one runs, a clay
"!" when one failed or was interrupted, a moss tick otherwise -- a step that started nothing is done -- read by the
same rule the tab's Task rows use, with the state word kept for screen readers. Each step
shows its title and, on the latest step of a group that still needs the user, its summary --
which opens with what the user is asked -- under the title on a tinted block clamped to five
lines, beside the timeline turned amber under that step: every vertical line of the thread
stands in the marks' column; then its time
(`HH:MM`, with the date when not today), and one chip per linked
Task and per descendant run of one -- kind, agent or run summary, status glyph. A chip opens
the existing task pane the way a list row does today (`desk.openDeskTask`). A chip whose
Task `tasks.list` no longer holds -- a branched session, a removed run -- renders dimmed and
inert, named by its stored summary. When `tasks.list` gains a dag row that neither a step nor
`lineage` places, the page re-reads `tasks.board` once, since a new round or successor may
have started.

5.3 **From a step to its turn's reply.** Clicking a step's title, or its summary, scrolls
the transcript to what Raven said in that turn: the last answer between the turn's opening and
the next turn's, or the opening itself while the turn has no answer on the stage. A turn opens
with a question someone asked or with a delegated result re-entering (its delivered row); a
message merged into a running turn opens nothing. The turn is the one whose opening carries
the step's `turn_id` -- an opening drawn on the open page is filed under the id its frame named
(`message.start`, `turn.started`) -- or else the untagged opening nearest the step's `turn_at`,
within five minutes: a turn read back from history carries no id, but carries that very
timestamp. Time alone would miss a turn opened on the open page whenever the browser's time
zone is not the server's: `turn_at` is the server's clock with no zone, that opening the
browser's.

5.4 **Completed.** Below the open groups, a fold `Completed - N` / `已完成 - N`, collapsed by
default. Expanded, it lists completed groups by `created_at` descending, dimmed, each
expandable to its thread like any other. The reader's one action sits at the foot of an
expanded thread: "Mark completed" on an open group, "Reopen" on a completed one.

5.5 **Unassigned.** A Task the session's `tasks.list` holds but no step links, directly or
by lineage -- an update that failed outright, say -- shows in an `Unassigned` / `未归类`
fold at the top, collapsed by default, drawn the way the old flat list draws a Task. A
conversation whose board holds no group -- one from before the board, or with the board
switched off -- draws the old flat list itself, so nothing about it changes.

5.6 **Live.** `state/session/stages.ts` arms `tasks.board_updated` the way it arms
`subagent.status`, replacing the open session's board; switching session or reconnecting
re-reads `tasks.board`.

Strings go through both catalogs (`ui-web/src/i18n/`); styles stay under the domain's `tk`
prefix and the page's existing colour variables (`--amber`, `--sky`, `--moss`, `--clay`,
`--faint`).

### 6. Failure and edge cases

6.1 **The update cannot be had.** When the post-turn call fails after the provider's retries,
or three attempts all fail validation, the turn's handles are appended to `unassigned` with
the turn's id, a warning is logged, and the next turn's update must place them before its
own. Until then the page shows them under Unassigned (5.5).

6.2 **An answer in text.** With the tool choice left to the model, a reply may carry text, or
another tool's call, instead of `task_board`. It counts as a failed attempt under 3's limit:
the reply is kept as text -- another tool's call is never run -- and the model is told to
answer with the call.

6.3 **History edits.** A step never points at a turn the log no longer holds. `session.undo`
drops the steps whose `turn_at` belongs to a turn it removed, and groups left with no steps;
`session.clear`
empties the board; `session.branch` copies it with the transcript. Compaction leaves the
board alone -- surviving compaction is part of its purpose.

6.4 **Sessions from before the board.** No file: `tasks.board` answers an empty board and
the page draws the session's Tasks as the old flat list (5.5). No backfill.

### 7. Configuration

`agents.defaults.taskBoard` in `raven/config/schema.py` (`AgentDefaults`, `:182`):

```json
"taskBoard": {"enabled": true}
```

The switch is all there is. `enabled: false` removes the tool and the post-turn call. Each `agents/*/config.json` sets
it false. A loop built without the config at all -- an embedding, a test, a trajectory
replay -- keeps no board: the runtime is what wires it, so only a runtime-built host makes
the extra call per turn.

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
- **Rollback.** `taskBoard.enabled: false`. With no group on any board, the page draws the
  old flat list.
