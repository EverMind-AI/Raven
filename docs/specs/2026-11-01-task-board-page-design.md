# The Task Board page: every conversation's Task Groups on one page

Status: approved in conversation on 2026-11-01 (layout "A", board and list views both); built
2026-11-01, with the choices this spec left to the builder written into 5.6 to 5.8. The four
categories and their colours were settled the same day as the one set of types every surface
shows (2).
Builds on the host Task Board (`docs/specs/2026-10-29-task-board-design.md`): this page only
reads what that feature already keeps.

## 1. Intent

The sidebar's tasks tab shows one conversation's Task Groups. The owner runs several
conversations at once, each pushing several matters, and loses track of which ones wait for
him, which are running, and which have gone quiet. This page gathers the Task Groups of every
host conversation in one place and sorts them by what they need from him: an answer, nothing
yet (something is running), or nothing at all (idle). Finished ones stay out of the way.

## 2. Terms

- **Task** (任务) is a Task Group and **event** (事件) is a Progress Step, as `CONTEXT.md`
  maps them for Chinese surfaces. English identifiers keep Task Group / Progress Step.
- **Category**: a Task Group's type, one of four -- *Needs you* (需要你回答), *Running*
  (运行中), *Idle* (闲置), *Completed* (已完成) -- named after the four statuses the model
  writes, and the same four wherever a Task Group is shown: this page, the rail badge and the
  sidebar's tasks tab. Each has one colour: amber, sky (with the turning ring), green, grey.
  *Needs you* is a Task Group waiting on the user, for an answer or for an error only the user
  can resolve: one type for both.
- *Completed* means a Task Group whose status is `completed`, finished or dropped. It is not
  an archived conversation (`session.archive`), which is a separate feature (5.1).
- A **linked run** is a Task a step placed: a spawned node (`spawn:<node_id>`) or a DAG run
  (`dag:<run_id>`), with its descendant runs resolved by lineage as the sidebar does.

## 3. Goals

1. A module page, *Task board* (任务看板), with its own rail button beside *Connectors* (连接)
   and *Knowledge* (知识库).
2. Every host conversation's Task Groups, in the four categories; Completed hidden by default.
3. Two views of the same cards, switched in the page's toolbar: **Board** (columns) and
   **List** (grouped sections).
4. Each card says where it came from (the conversation), what it is (title and summary),
   where it stands (category, status and a time), and how big it is (events, sub-agents).
5. A card unfolds in place into its events and the questions it waits on; an event opens its
   conversation, with the Task Group expanded in the sidebar and the transcript at that
   event's reply.
6. The page stays current while it is open, including for conversations the page is not
   subscribed to.

## 4. Non-goals

- Dragging cards between columns, or editing, renaming or creating Task Groups on this page.
  An unfolded card carries the sidebar thread's own buttons (Ignore, Answer, Mark completed)
  and no others.
- Tasks of sub-agent or product-agent conversations: they keep no board.
- A server push channel for other conversations' board writes (5.3 polls instead; a push
  channel is later work).

Anti-goals -- failures even if everything technically works:

- A *Running* column holding Task Groups nothing runs for. A Task Group the model left
  `running` while no linked run is running is Idle, not Running: Running is ground truth, the
  same rule the sidebar's event markers follow.
- Cards that cannot be told apart across conversations: every card names its conversation.
- A poll that re-reads every session file each time and slows the gateway as sessions pile up.

## 5. Design

### 5.1 What the page lists

All host conversations the session list returns by default (`SessionManager.list_sessions`,
which already leaves sub-agent transcripts out) whose session directory holds a
`task_board.json`. Archived conversations (`metadata.archived`) are left out, as the rail
leaves them out. Every Task Group of every listed conversation is one card.

### 5.2 The category, the time and the counters -- decided on the server

One rule, applied in one place, for both views and the rail badge:

| Category | Rule, in this order | Time shown |
|---|---|---|
| Completed | status `completed` | how long it took: `updated_at - created_at` |
| Needs you | status `needs_you` | since `updated_at` |
| Running | any linked run running | since the earliest `started_at` among the running runs |
| Idle | everything else (`running` or `idle` with nothing running) | since `updated_at` |

- The model's `running` and `idle` are a hint the live runs correct, in both directions: a
  Task Group it left `running` after its run ended is Idle, and one it called `idle` while a
  run it linked still works is Running.

- "Running" for a linked run is what the sidebar's `tasks.list` says of it: the server builds
  the linked runs' rows with `tasks.list`'s own readers, so the two cannot disagree. The node
  registry alone cannot answer it -- every node of a run is claimed `running` at its start, and
  a run a restart cut off stays claimed -- so what this process is still executing is read on
  the event loop (the task index, the instance registry) and handed to the row builders.
  Descendant runs count for the run they continue.
- **events** = the Task Group's Progress Steps. **sub-agents** = the spawned nodes it linked
  plus the nodes of the DAG runs it linked, counted off those runs' rows (each sub-agent that
  worked on the Task Group once); a linked run no row describes any more counts once. My call:
  the mockup labelled this 子代理, and a DAG run is several sub-agents, not one.

### 5.3 One new RPC: `tasks.boards`

```
tasks.boards(include_completed?: boolean = false) -> {
  cards: [{
    session_key, session_title,          // the title the rail shows
    group_id, title, summary, status,    // the Task Group's own fields
    created_at, updated_at,              // ms, the Task Group's own
    category: "needs_you" | "running" | "idle" | "completed",
    running_since_ms,                    // ms; set only on a Running card (5.2)
    events, sub_agents
  }],
  completed_count,                       // always, so the switch can show it
  needs_you_count                        // always, for the rail badge
}
```

- The client turns these into 5.2's time: `updated_at` for Needs you and Idle,
  `running_since_ms` for Running, `updated_at - created_at` for Completed.
- Completed cards are returned only with `include_completed`; the counts always come back.
- Step summaries never leave the server here either (the board's wire rule).
- Cost: the boards are found by walking the sessions root for `task_board.json` files -- a
  directory walk, never a read of every session file -- and only those sessions' metadata
  (title, archived) is read. A session whose board, node registry and session file are all
  unchanged since the last answer is not parsed again (the builder picks the cache, 7). The
  linked runs' rows are read on every answer, since what is running is not on disk; a session
  whose board links no run is never asked for them. It goes in `rpc-schema/openrpc.json`, mirrored in `raven/rpc/models.py`, with the
  generated clients regenerated (ui-web `npm run gen`, ui-tui `gen:rpc`) and an offline
  fixture for the page.

**Freshness.** Events reach a client only for the sessions it subscribed to
(`raven/rpc/subscriptions.py`, `SubscriptionEmitter`), so the page cannot wait for other
conversations' `tasks.board_updated`. It asks `tasks.boards` when it opens, again every 10
seconds while it is the visible page (no polling when it is hidden or another page is open),
and at once on a `tasks.board_updated` it does receive. The rail badge reads
`needs_you_count` from the same answer; while the page is not visible, the badge alone asks
again every 60 seconds (my call).

### 5.4 Where it lives in the page

- Rail: a third module button after Knowledge (before *Import conversation*), labelled
  任务看板 / "Task board", with a small badge holding `needs_you_count`, hidden at zero.
- Page: a module page row in `ui-web/src/state/pages.ts` (`DECLARED`), owned by the tasks
  domain -- `features/tasks/manifest.ts` gains `page` and `root`. Its classes carry the
  domain's `tk` prefix. It fills the pane, as the knowledge page does, rather than sitting in
  the reading column every other page keeps (`.wrap`'s 1080px cap): the columns are the
  content.

### 5.5 Toolbar

Title (任务看板), a conversation filter ("All conversations" / 全部会话, then the conversations
that have cards), a "Show completed {n}" switch (显示已完成), and a Board / List segmented
switch. The filter narrows every view and every count on the page, the badge excepted. The
view and the completed switch persist across reloads (my call).

### 5.6 Board view

- Columns in this order: Needs you, Running, Idle; Completed appears as a fourth column while
  the switch is on. A column head carries its icon, name and count. An empty column says so in
  one quiet line.
- A card, top to bottom: the conversation (icon, its title on one line, ellipsis); the Task
  Group's title (bold); its summary (two lines, ellipsis); a footer with the status and time on
  the left and the counters on the right ("events n | sub-agents n" / 事件 n | 子代理 n).
- Footer status: the category's mark, then its name and its time, the same words for all
  four -- "Needs you · {since}" (需要你回答), "Running · {elapsed}" (运行中),
  "Idle · {since}" (闲置), "Completed · took {duration}" (已完成 · 用时). The mark is an amber
  dot, the sky ring the sidebar turns for a running event, a green dot and a grey dot.
- Within a column, the most recently updated card first.
- Narrow widths stack the columns one per row. The board measures its own width, not the
  window's, since the rail beside the page can be dragged wider: four columns pair up under
  920px, and every column stacks under 680px.
- Times: Needs you and Idle say how long the state has held, as a span ("1d", "1 天"), not how
  long ago it began; Running and Completed use the elapsed spelling the rest of the page uses
  ("6m 12s", "1h 12m").

### 5.7 List view

- Category chips with counts: All (every category but Completed), Needs you, Running, Idle,
  Completed. A chip filters the list to its category.
- Sections in the column order, one row per Task Group: the status mark, the title with the
  conversation beside it, the summary on one line, and the status, time and counters at the
  right. With the completed switch on, a Completed section closes the list, collapsed until
  opened. A row unfolds as a card does (5.8), its thread under the title.
- A category with no Task Groups has no section; when nothing at all is listed, one quiet line says
  so. The Completed chip lists the completed rows directly, without the fold, and a chip the
  switch takes away falls back to All.

### 5.8 Unfolding a card, and going to an event

A card's face is a button (Enter and Space work), named by the title, the category and the
conversation. Pressing it unfolds the card in place -- the page stays, and so does the
conversation on screen -- into the Task Group's thread, the sidebar's own (`BoardList.tsx`
`Thread`): the events in time order with their marks, the latest event's summary and the
questions it asked while the group needs the reader, then Mark completed, Ignore and Answer.
Pressing it again folds the card. The thread is the conversation's board (`tasks.board`) and its
Tasks (`tasks.list`, for the event marks), read when the card unfolds and again with every
refresh while the page is open; the badge's poll reads neither. Both views unfold the same
cards. The thread names no Task, since a Task's pane opens only for the conversation on screen.

The thread's buttons act on the card's conversation, which need not be the one on screen:

- **An event** goes to that conversation:
  1. switches to it (`state/session/registry.ts` `switchTo`, with that conversation's session
     row -- fetched when the rail's list does not hold it);
  2. opens the right panel's tasks tab (`state/ws.ts` `pick('tasks')`, which is the desk's
     `openDeskTab`), after the switch, since the desk keys what it opens by the conversation on
     screen;
  3. expands that Task Group in the sidebar's board and scrolls it into view: a one-shot focus
     request the tasks store holds for `BoardList`, cleared once applied. The request names its
     conversation, so it waits out the switch rather than landing on the board being left; a
     completed group opens the Completed fold too; a group the board no longer has drops it;
  4. scrolls the transcript to the reply the event's turn ended on (`transcript/source.ts`
     `revealTurnAt`); the switch has drawn the history by the time it answers.

  The page steps aside first, and an event of the conversation already open skips step 1. An
  open another switch overtook stops after the switch: no tab, no scroll, and its focus request
  taken back if it is still its own.
- **Answer** goes to the conversation the same way, then drafts the answers in its composer.
- **Ignore** and **Mark completed** write to the conversation (`tasks.ignore_question`,
  `tasks.set_group_status`) and stay on the page. The board they answer with becomes the
  card's thread, and the sidebar's when that conversation is the one on screen; the cards are
  read again for where the group now stands.
- **Move on** sits outside the face, with the counters -- in a card's foot, at a row's end --
  so it is a press apart from the one that unfolds. It goes to the conversation as an event
  does, then holds the group for the conversation's next message: the bar over the composer
  and the note its turn reads (the sidebar spec, 5.1.1). An open another switch overtook holds
  nothing.

### 5.9 Language and accessibility

All strings are i18n keys with English and Chinese (`i18n/messages.json`, under
`gui.tasks.page.*`, the rail label under `gui.nav.*`). Status is said in words as well as by
colour; the ring respects reduced motion.

## 6. Testing

- Python: `tasks.boards` -- every category rule (each status; a running spawn, a running DAG
  node, a running descendant run; a failed run), `since_ms` per category, both counters, the
  conversations listed (no board, sub-agent transcript, archived conversation each left out),
  `include_completed`, the two counts, and the unchanged-session cache answering the same.
  Models and OpenRPC stay in parity (the existing schema-match tests).
- Page (vitest): the store's polling starts with the page and stops when it hides; board
  columns and their order; Completed hidden until the switch; list sections and chips; the card
  footer per category; a card unfolding in place and folding again, its thread read again with each open-page
  refresh, an event's open flow (switch, tab, focus applied once, the turn's reply), Answer, and
  the thread's writes landing in the card's own conversation; the rail badge; the
  persisted view. The ui-web gates: domain registration, rail-nav registry, class namespace,
  import direction, the boot-snapshot golden (the rail grows a button), offline fixtures.
- A real-browser screenshot of both views beside the approved mockup before review.

## 7. Left to the builder

- The cache behind `tasks.boards` and where it lives, within 5.3's rule.
- Component split, file names inside `features/tasks/`, glyphs from the existing icon set,
  CSS inside the `tk` namespace, the narrow-width breakpoint.
- Time formatting (reuse `ui-web/src/lib/ago.ts` where it fits).
- How the focus request reaches `BoardList` (a store field or a subscription), as long as it
  is applied once.

## 8. Calls made without the owner

1. Archived conversations' Task Groups are left out (5.1).
2. sub-agents counts each sub-agent once, DAG nodes included (5.2).
3. Polling every 10 seconds while visible, plus the events the page does receive; the badge
   alone every 60 seconds otherwise (5.3).
4. Completed cards fetched only when the switch is on; both counts always returned (5.3).
5. The rail button sits after Knowledge; its badge hides at zero (5.4).
6. The view and the completed switch persist across reloads (5.5).
