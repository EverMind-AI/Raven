# Task Board Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A module page beside Connectors and Knowledge that shows the Task Groups of every host conversation, in four categories, as a board or a list.

**Architecture:** One pure reader (`raven/agent/task_board/overview.py`) walks the sessions root for board files, decides each card's category, time and counters, and caches per session by file mtimes. A new RPC, `tasks.boards`, returns those cards. The page lives in the tasks domain of `ui-web`: a small store polls `tasks.boards` while the page is visible, two views render the same cards, and opening a card switches conversation and asks the sidebar's `BoardList` to expand that task.

**Tech Stack:** Python 3.12 (aiohttp JSON-RPC, pydantic models), React + TypeScript (vite, vitest, jsdom), OpenRPC codegen for ui-web and ui-tui.

**Spec:** `docs/specs/2026-11-01-task-board-page-design.md` (approved 2026-11-01). Read it beside this plan; section numbers below (5.2, 5.3, ...) are the spec's.

## Global Constraints

- AGENTS.md: English source; CJK only in the i18n catalogue's `zh` values (an exemption zone) and `*.md`.
- TDD per task: the failing test runs red for the stated reason before any implementation.
- No commit and no push without the owner's word (AGENTS.md 3.4); every task ends at a green checkpoint.
- Python: `nice -n 19 uv run --frozen --python 3.12 --all-extras pytest ...`; the full suite at most `-n 4`.
- ui-web tests: `nice -n 19 node_modules/.bin/vitest run <path>` from `ui-web/`; new classes carry the tasks domain's `tk` prefix.
- Categories, in rule order (5.2): `archived` = status `completed`; `needs_you` = status `waiting` or `blocked`; `running` = any linked run running; `idle` = the rest.
- Poll: every 10 s while the page is visible; the badge alone every 60 s otherwise (5.3).
- Strings: every user-facing string is an i18n key with `en` and `zh`; Chinese terms 任务 / 事件 (CONTEXT.md).
- Step summaries never reach the page.

## Review Focus

1. A session whose transcript is missing, unreadable, or whose first line is not metadata: the page still answers with every other session's cards. (Task 1)
2. A card linking a run the node registry no longer holds (pruned history): it counts once as a sub-agent and does not make the card Running. (Task 1)
3. Dozens of conversations with boards: a poll with nothing changed re-reads no board file. (Task 1)
4. The gateway unreachable while the page is open: the cards already shown stay, a quiet error line appears, and polling carries on. (Task 3)
5. Opening a card whose conversation was deleted or whose task is gone by the time the sidebar loads: the focus request is dropped, never re-applied to a later board. (Task 7)

---

### Task 0: Branch and baseline

- [ ] **Step 1: Ask the owner which branch and base this goes on** (AGENTS.md 2.2). Recommendation: a new branch `feat/task_board_page` cut from `feat/host_task_board` once that branch's uncommitted work is committed, because the page reads `task_board.json`, `board_to_wire` and the `tk` styles that branch adds. Do not cut anything before the answer.
- [ ] **Step 2: Baseline.** In the worktree: `nice -n 19 uv run --frozen --python 3.12 --all-extras pytest tests/test_task_board_*.py tests/test_rpc_tasks_board.py -q -n 0` -> all pass; `cd ui-web && nice -n 19 node_modules/.bin/vitest run src/features/tasks` -> all pass.

---

### Task 1: The overview reader

**Files:**
- Create: `raven/agent/task_board/overview.py`
- Test: `tests/test_task_board_overview.py`

**Interfaces:**
- Consumes: `load_board(session_dir) -> Board` (`raven/agent/task_board/store.py`); `board_lineage(session_dir, board) -> dict[str, str]` (`lineage.py`); `session_history_root(session_dir)` (`raven.agent.subagent.history`); `REGISTRY_FILENAME` (`raven.agent.subagent.dag_store`); `SessionManager.key_from_path(path)` and `SUBAGENT_CHANNEL = "sub"` (`raven/session/manager.py`).
- Produces:
  - `CATEGORIES: tuple[str, ...] = ("needs_you", "running", "idle", "archived")`
  - `@dataclass(frozen=True) class Card` with fields `session_key: str, session_title: str, group_id: str, title: str, summary: str, status: str, created_at: int, updated_at: int, category: str, running_since_ms: int | None, events: int, sub_agents: int`
  - `def category_of(status: str, running: bool) -> str`
  - `def list_cards(sessions_dir: Path, *, include_archived: bool) -> dict[str, Any]` returning `{"cards": [card as dict], "archived_count": int, "needs_you_count": int}`; cards sorted by `updated_at` descending.

Layout facts the reader relies on: a session's directory is `sessions/<group>/<name>/` beside its transcript `sessions/<group>/<name>.jsonl`; the transcript's first line is `{"_type": "metadata", "key": ..., "metadata": {"title": ..., "archived": ...}}`; the node registry is `session_history_root(session_dir) / REGISTRY_FILENAME`, whose `nodes` map rows `{kind, status, started_at_ms, ended_at_ms, run_id?}`.

- [ ] **Step 1: Write the failing tests** (build sessions on disk under `tmp_path / "sessions"` with a helper that writes the transcript's metadata line, a `task_board.json` via `save_board`, and an optional `nodes.json`):
  - `test_category_follows_the_rule_order`: `category_of("completed", True) == "archived"`, `category_of("waiting", True) == "needs_you"`, `category_of("blocked", False) == "needs_you"`, `category_of("active", True) == "running"`, `category_of("parked", True) == "running"`, `category_of("active", False) == "idle"`, `category_of("parked", False) == "idle"`.
  - `test_a_running_spawn_makes_its_card_running_since_its_start`: a group whose step links `spawn:n1`, registry row `n1` = `{"kind": "spawn", "status": "running", "started_at_ms": 1000}` -> card `category == "running"`, `running_since_ms == 1000`.
  - `test_a_dag_run_runs_while_any_node_or_descendant_runs`: link `dag:R1`; registry nodes `a` `{run_id: "R1", status: "completed"}`, `b` `{run_id: "R2", status: "pending", started_at_ms: 2000}`; `R2` is a replan successor of `R1`: write `dag_root(session_dir) / "R1" / "graph.json"` as `{"replan": {"run_id": "R2"}}` (`dag_root` from `raven.agent.subagent.history`, the shape `tests/test_rpc_tasks_board.py` already uses) -> `running`, `running_since_ms == 2000`.
  - `test_counters_count_events_and_each_sub_agent_once`: two steps; step 1 links `spawn:n1`, step 2 links `dag:R1` whose registry holds nodes `a`, `b` -> `events == 2`, `sub_agents == 3`; a link to `spawn:gone` with no registry row adds 1 (Review Focus 2) and leaves the category not running.
  - `test_archived_cards_come_only_when_asked_and_counts_always`: one `completed`, one `waiting`, one `active` group -> `list_cards(..., include_archived=False)` holds 2 cards, `archived_count == 1`, `needs_you_count == 1`; with `include_archived=True` 3 cards.
  - `test_only_host_conversations_with_a_board_are_read`: a session with no board, a `sub:` session, and one whose metadata says `"archived": true` contribute nothing; the host session does.
  - `test_a_broken_session_is_skipped_not_fatal` (Review Focus 1): a board whose transcript is missing, and one whose first line is `not json`, are skipped; a good session still answers.
  - `test_an_unchanged_session_is_not_read_again` (Review Focus 3): call `list_cards` twice; monkeypatch `overview.load_board` with a counting wrapper -> one read for the first call, zero for the second; touching the board file (new mtime) reads it again.
  - `test_cards_carry_title_and_summary_but_never_a_step_summary`: the card dict's keys are exactly the `Card` fields; no key named `steps`.
  - `test_an_untitled_session_is_named_by_its_first_message`: metadata without `title`, first user message `"fix the login page"` -> `session_title == "fix the login page"` (cut at 80 characters).

- [ ] **Step 2: Run them to see them fail**

Run: `nice -n 19 uv run --frozen --python 3.12 --all-extras pytest tests/test_task_board_overview.py -q -n 0`
Expected: collection error, `ModuleNotFoundError: raven.agent.task_board.overview`.

- [ ] **Step 3: Implement `overview.py`**

Walk `sessions_dir.glob("*/*/task_board.json")`; the transcript is `board.parent.parent / (board.parent.name + ".jsonl")`. Read only the transcript's first line for the key and metadata (and, for an untitled session, scan to the first `"role": "user"` line). Skip `sub:` keys and `metadata.archived`. A run is running when its registry rows (a spawn's own row; a DAG run's rows whose `run_id` is the run or one of its descendants from `board_lineage`) include `running` or `pending`; `running_since_ms` is the smallest `started_at_ms` among those rows. Cache per session directory in a module dict keyed by the tuple `(board mtime_ns, registry mtime_ns or 0, transcript mtime_ns)`; a hit reuses that session's cards. Every per-session failure is caught, logged at debug, and skips that session.

- [ ] **Step 4: Run the tests to see them pass**

Run: `nice -n 19 uv run --frozen --python 3.12 --all-extras pytest tests/test_task_board_overview.py -q -n 0`
Expected: 10 passed. Then `ruff check` and `ruff format` on both files.

- [ ] **Step 5: Checkpoint** -- green; no commit (Global Constraints).

---

### Task 2: The `tasks.boards` RPC and the generated clients

**Files:**
- Modify: `raven/rpc/methods/tasks.py` (beside `tasks_board`, `register_tasks_methods`)
- Modify: `raven/rpc/models.py` (beside `TasksBoardParams` / `TasksBoardResult`, and the method table entry `"tasks.board": (TasksBoardParams, TasksBoardResult)`)
- Modify: `rpc-schema/openrpc.json` (a method entry beside `tasks.board`, a `TaskBoardCard` component)
- Regenerate: `ui-web/src/rpc/generated.ts` (`npm run gen`), `ui-tui/src/rpc/generated.ts` (`npm run gen:rpc`)
- Test: `tests/test_rpc_tasks_board.py`

**Interfaces:**
- Consumes: `list_cards(sessions_dir, *, include_archived)` (Task 1).
- Produces: RPC `tasks.boards` with params `{include_archived?: boolean}` and result `TasksBoardsResult {cards: TaskBoardCard[], archived_count: int, needs_you_count: int}`; `TaskBoardCard` has exactly the `Card` fields of Task 1, `category` an enum of the four categories, `running_since_ms` nullable. Python: `async def tasks_boards(params: dict[str, Any], *, agent_loop_factory=None) -> dict[str, Any]`, registered as `"tasks.boards"`. TS: `TasksBoardsResult`, `TaskBoardCard` in the generated clients.

- [ ] **Step 1: Write the failing tests** in `tests/test_rpc_tasks_board.py` (reuse its `workspace` fixture and `_board` helper):
  - `test_boards_lists_every_conversation_s_cards`: two sessions saved with boards -> `await tasks_boards({})` returns both conversations' cards, `TasksBoardsResult.model_validate(answer)` passes.
  - `test_boards_hides_archived_until_asked`: a completed group -> absent without `include_archived`, present with `{"include_archived": True}`; `archived_count == 1` both times.
- [ ] **Step 2: Run them to see them fail**: `ImportError: cannot import name 'tasks_boards'`.
- [ ] **Step 3: Implement** `tasks_boards` (resolve the session manager the way `_session_dir` in `raven/rpc/methods/subagent.py` does: `manager_for(_safe_invoke_factory(agent_loop_factory), load_config())`, then `list_cards(mgr.sessions_dir, include_archived=bool(params.get("include_archived")))`); register it; add the pydantic models (`TaskBoardCard`, `TasksBoardsParams`, `TasksBoardsResult`, all `_Strict`) and the method-table entry; add the OpenRPC method (summary "Every host conversation's Task Groups as cards, sorted for the Task Board page.") and the `TaskBoardCard` component with the same fields, required list and enum.
- [ ] **Step 4: Run** `pytest tests/test_rpc_tasks_board.py tests/test_rpc_schema_match.py tests/test_rpc_registration.py -q -n 0` -> pass. Then `cd ui-web && npm run gen` and `cd ui-tui && npm run gen:rpc`; `git diff --stat` shows both `generated.ts` files changed; `npm run type-check` in both packages exits 0.
- [ ] **Step 5: Checkpoint.**

---

### Task 3: The page store: fetch, poll, persist

**Files:**
- Create: `ui-web/src/features/tasks/boards.ts`
- Modify: `ui-web/src/features/tasks/types.ts` (`TasksSource` gains `boards?`; re-export `TaskBoardCard`, `TasksBoardsResult`)
- Modify: `ui-web/src/features/tasks/source.ts` (`boards: (includeArchived) => gateway().call('tasks.boards', { include_archived: includeArchived })`)
- Modify: `ui-web/src/rpc/fixtures/tasks.ts` (answer `tasks.boards` from the fixture boards already there)
- Test: `ui-web/src/features/tasks/boards.test.ts`

**Interfaces:**
- Consumes: `TasksSource.boards?: (includeArchived: boolean) => Promise<TasksBoardsResult>`; `state/page.ts` `get()` / `subscribe()`; `document.visibilityState`.
- Produces (all exported from `boards.ts`):
  - `type BoardsView = 'board' | 'list'`
  - `interface BoardsState { cards: TaskBoardCard[]; archivedCount: number; needsYouCount: number; view: BoardsView; showArchived: boolean; filter: string | null; error: boolean; loaded: boolean }`
  - `get(): BoardsState`, `subscribe(fn): () => void`, `_resetForTests(): void`
  - `refresh(): Promise<void>` -- one `tasks.boards` call with `include_archived = showArchived`; on failure keep `cards`, set `error: true`.
  - `start(): void` / `stop(): void` -- the page's 10 s poll; `startBadge(): void` -- the 60 s badge poll, idempotent, paused while the page poll runs.
  - `setView(v: BoardsView)`, `setShowArchived(on: boolean)` (refreshes), `setFilter(sessionKey: string | null)` -- the first two persist to `localStorage` keys `raven.tasks.board.view` and `raven.tasks.board.archived`.
  - `type Category = TaskBoardCard['category']`; `visible(cards?: TaskBoardCard[]): TaskBoardCard[]` -- the cards after `filter`; `countsOf(cards): Record<Category, number>`.
  - `onBoardEvent(): void` -- called from the tasks source's `onBoardUpdated` (wired in `ui-web/src/app/install.ts` beside the existing `onBoardUpdated`); refreshes at once while the page is open (5.3: "at once on a `tasks.board_updated` it does receive").
  - `PAGE_ID = 'tasksBoardPage'` (the module page id of Task 4).

- [ ] **Step 1: Write the failing tests** (fake timers; a stub source counting calls):
  - `start polls every 10 seconds and stop ends it`: `start()` -> 1 call; advance 10 000 ms -> 2; `stop()`; advance 30 000 -> still 2.
  - `the badge polls every 60 seconds while the page is closed`: `startBadge()` -> advance 60 000 -> 1 call more; while `start()` runs, the badge timer adds none.
  - `a failed poll keeps the cards and says so` (Review Focus 4): first call resolves 2 cards, second rejects -> `cards.length === 2`, `error === true`; third resolves -> `error === false`.
  - `the view and the archived switch persist`: `setView('list')`, `setShowArchived(true)` -> `localStorage` holds them; `_resetForTests()` then a fresh read restores both.
  - `the filter narrows the cards and their counts`: cards from two sessions; `setFilter('web:a')` -> `visible()` holds only `web:a` cards and `countsOf(visible())` counts only those.
  - `turning archived on asks for archived cards`: `setShowArchived(true)` -> the last call's argument is `true`.
  - `a board event refreshes the open page at once`: `start()` (1 call), then `onBoardEvent()` -> 2 calls without advancing time; with the page closed, `onBoardEvent()` adds none.
- [ ] **Step 2: Run** `node_modules/.bin/vitest run src/features/tasks/boards.test.ts` -> fails: cannot resolve `./boards`.
- [ ] **Step 3: Implement `boards.ts`** with `makeStore` (`state/store`), the source from `state/sources` (`sources.tasks`), timers via `setInterval`; the page poll runs only while `page.get() === PAGE_ID` and the document is visible (a `visibilitychange` listener calls `stop()` / `start()`).
- [ ] **Step 4: Run** the test file -> pass; `npm run type-check` -> 0.
- [ ] **Step 5: Checkpoint.**

---

### Task 4: The page in the shell: page row, rail button, badge, strings

**Files:**
- Modify: `ui-web/src/state/pages.ts` (`DECLARED` gains a row)
- Modify: `ui-web/src/state/escapeOrder.ts` (`CLOSERS` gains `tasksBoardPage: () => page.show(null)`)
- Modify: `ui-web/src/chrome/Rail.tsx` (the nav array gains an entry after Knowledge, with a badge)
- Modify: `ui-web/src/features/tasks/manifest.ts` (`page: 'tasksBoardPage'`, `root: TasksBoardApp`)
- Create: `ui-web/src/features/tasks/BoardsPage.tsx` (only `TasksBoardApp`, an empty shell, in this task)
- Modify: `i18n/messages.json` (keys below); regenerate ui-tui's i18n (`npm run gen:i18n` in `ui-tui`)
- Modify: `ui-web/scripts/__golden__/boot-stub.txt`, `boot-live-noserver.txt` (regenerated)
- Test: `ui-web/src/features/tasks/BoardsPage.test.tsx`

**Interfaces:**
- Consumes: Task 3's store (`start`, `stop`, `startBadge`, `get`, `subscribe`, `PAGE_ID`).
- Produces: page row `{ id: 'tasksBoardPage', bodyId: 'tasksBoardBody', navButtons: ['boardsBtn'], escapeRank: 4, head: 'gui.page.tasks_board', aria: 'gui.page.tasks_board' }`; rail button id `boardsBtn`; `export function openBoards(): void` in `boards.ts` (`page.show(PAGE_ID)` then `start()`); `export function TasksBoardApp(): JSX.Element`.
- i18n keys (en | zh): `gui.nav.tasks_board` "Task board" | 任务看板; `gui.page.tasks_board` "Task board" | 任务看板; `gui.tasks.page.badge` "{n} waiting on you" | {n} 个等你回答 (the badge's accessible label).

- [ ] **Step 1: Write the failing tests**:
  - `the rail has a task board button after Knowledge`: render the rail -> the button ids in order contain `knowledgeBtn`, then `boardsBtn`, then `importedBtn`.
  - `the badge shows the needs-you count and hides at zero`: store `needsYouCount = 3` -> the button holds a badge with text `3` and `aria-label` `gui.tasks.page.badge {"n":3}`; `0` -> no badge element.
  - `opening the button shows the page and starts the poll`: click `boardsBtn` -> `page.get() === 'tasksBoardPage'`; the stub source was called once.
- [ ] **Step 2: Run them** -> fail (no `boardsBtn`).
- [ ] **Step 3: Implement**: the page row, the closer, the nav entry (`button: 'boardsBtn', key: 'gui.nav.tasks_board', open: () => openBoards()`, an existing kanban-like glyph from the icon set), the badge from `boards.get().needsYouCount`, `startBadge()` once the rail mounts, the manifest fields, the i18n keys.
- [ ] **Step 4: Run** the test file and the gates: `node_modules/.bin/vitest run src/features/tasks/BoardsPage.test.tsx scripts/gates` -> pass (domain registration, domain shape, rail-nav registry, class namespace, import direction). Build: `npm run build && ../.venv/bin/python3 build.py` -> the boot snapshot fails with a diff of exactly the new button and the new section; regenerate both goldens:
  `node scripts/boot-snapshot.mjs dist/index.html --url "http://127.0.0.1:18792/?stub=1" --golden scripts/__golden__/boot-stub.txt --update` and the same with `--url "http://127.0.0.1:18792/" --golden scripts/__golden__/boot-live-noserver.txt --update`; rerun `build.py` -> both `boot-snapshot: OK`.
- [ ] **Step 5: Checkpoint.**

---

### Task 5: The board view

**Files:**
- Modify: `ui-web/src/features/tasks/BoardsPage.tsx`
- Modify: `ui-web/src/features/tasks/styles.css`
- Modify: `i18n/messages.json`
- Test: `ui-web/src/features/tasks/BoardsPage.test.tsx`

**Interfaces:**
- Consumes: `boards.get()`, `visible()`, `countsOf()`; `formatDuration` and `ago` (`ui-web/src/lib/ago.ts`) for times.
- Produces: `function BoardView({ cards, showArchived, open }: { cards: TaskBoardCard[]; showArchived: boolean; open: (c: TaskBoardCard) => void }): JSX.Element`; `function CardFace({ card }): JSX.Element` (shared with Task 6). Class names the tests address: `.tkbcol` (a column, `data-cat` = category), `.tkbcard` (a card, a button), `.tkbconv` (its conversation line), `.tkbbang` (the blocked mark), `.tkbring` (the running ring, `data-st="run"`).
- i18n keys (en | zh): `gui.tasks.page.col_needs_you` Needs you | 需要你回答; `gui.tasks.page.col_running` Running | 运行中; `gui.tasks.page.col_idle` Idle | 闲置; `gui.tasks.page.col_archived` Archived | 已归档; `gui.tasks.page.waiting` "Waiting on you · {t}" | "等你回复 · {t}"; `gui.tasks.page.stuck` "Stuck · {t}" | "卡住 · {t}"; `gui.tasks.page.idle_for` "Idle {t}" | "闲置 {t}"; `gui.tasks.page.took` "Took {t}" | "用时 {t}"; `gui.tasks.page.events` "events {n}" | "事件 {n}"; `gui.tasks.page.sub_agents` "sub-agents {n}" | "子代理 {n}"; `gui.tasks.page.empty_col` "Nothing here" | "暂无".

- [ ] **Step 1: Write the failing tests**:
  - `the board shows three columns, archived only with the switch`: column heads read `col_needs_you`, `col_running`, `col_idle` with counts; `showArchived` -> a fourth, `col_archived`.
  - `each card names its conversation, title, summary and counters`: one card -> `.tkbconv` text = `session_title`, title, summary, `gui.tasks.page.events {"n":2}`, `gui.tasks.page.sub_agents {"n":3}`.
  - `the footer speaks the category`: needs_you `waiting` -> `gui.tasks.page.waiting ...`; `blocked` -> a `.tkbbang` mark and `gui.tasks.page.stuck ...`; running -> a ring element (`data-st="run"`) and an elapsed time from `running_since_ms`; idle -> `gui.tasks.page.idle_for ...`; archived -> `gui.tasks.page.took ...` from `updated_at - created_at`.
  - `cards in a column are newest first`: two cards, `updated_at` 1 and 2 -> order 2, 1.
  - `an empty column says so`: no running cards -> the running column holds `gui.tasks.page.empty_col`.
- [ ] **Step 2: Run them** -> fail.
- [ ] **Step 3: Implement** the columns and the card face (the ring reuses the event marker's look: a sky ring with a gap, `tkrunspin`, static under reduced motion); styles in `styles.css`, every class `tk`-prefixed; columns stack under the builder's breakpoint.
- [ ] **Step 4: Run** the test file and `scripts/gates/check-class-namespace.test.mjs` -> pass.
- [ ] **Step 5: Checkpoint.**

---

### Task 6: The list view

**Files:**
- Modify: `ui-web/src/features/tasks/BoardsPage.tsx`, `styles.css`, `i18n/messages.json`
- Test: `ui-web/src/features/tasks/BoardsPage.test.tsx`

**Interfaces:**
- Consumes: `CardFace` pieces (Task 5), `countsOf`.
- Produces: `function ListView({ cards, showArchived, open }: same as BoardView): JSX.Element`.
- i18n keys: `gui.tasks.page.all` All | 全部.

- [ ] **Step 1: Write the failing tests**:
  - `the list chips count each category and All leaves archived out`: chips read `all` (needs_you + running + idle), each category, and `col_archived` only with the switch.
  - `a chip filters the list to its category`: click `col_running` -> only running rows.
  - `rows sit in sections in column order`: section heads in order needs_you, running, idle.
  - `the archived section starts collapsed`: with the switch on, the archived section's toggle has `aria-expanded="false"` and no rows until clicked.
- [ ] **Step 2: Run them** -> fail.
- [ ] **Step 3: Implement** the chips (local state), the sections and the rows (status mark, title with the conversation beside it, one-line summary, time and counters on the right).
- [ ] **Step 4: Run** -> pass.
- [ ] **Step 5: Checkpoint.**

---

### Task 7: Toolbar and opening a card

**Files:**
- Modify: `ui-web/src/features/tasks/BoardsPage.tsx`, `styles.css`, `i18n/messages.json`
- Modify: `ui-web/src/features/tasks/store.ts` (the focus request)
- Modify: `ui-web/src/features/tasks/BoardList.tsx` (apply it)
- Test: `ui-web/src/features/tasks/BoardsPage.test.tsx`, `ui-web/src/features/tasks/BoardList.test.tsx`

**Interfaces:**
- Consumes: `switchTo(s: SessRow)` and `refreshList(): Promise<void>` (`state/session/registry.ts`), `sess(id)` (`state/session/rows.ts`), `openDeskTab('tasks')` (`features/desk/store.ts`), Task 3's `setView`, `setShowArchived`, `setFilter`.
- Produces: in `store.ts`: `requestFocus(sessionKey: string, groupId: string): void`, `focusFor(sessionKey: string): string | null`, `clearFocus(): void`.
- i18n keys: `gui.tasks.page.all_conversations` All conversations | 全部会话; `gui.tasks.page.show_archived` "Show archived {n}" | "显示已归档 {n}"; `gui.tasks.page.view_board` Board | 看板; `gui.tasks.page.view_list` List | 列表; `gui.tasks.page.error` "Could not refresh; showing the last answer" | "刷新失败，显示的是上一次的结果".

- [ ] **Step 1: Write the failing tests**:
  - BoardsPage: `the toolbar switches views and archived, and filters by conversation` -- the segmented control flips `view`; the switch label carries `archivedCount`; picking a conversation narrows the cards.
  - BoardsPage: `opening a card switches conversation, opens the tasks tab and asks for focus` -- mocked `switchTo`, `openDeskTab`, `refreshList`: click a card -> `switchTo` called with `sess(key)`; when the list does not hold it, `refreshList()` runs once and `sess(key)` is asked again, and only if it is still absent `switchTo` gets `{ id: key, title: session_title }` (5.8: "fetched when the rail's list does not hold it"); then `openDeskTab('tasks')`; `focusFor(key) === group_id`. Enter on the focused card does the same.
  - BoardList: `a focus request expands its group once and clears` -- `requestFocus('s1', 'a')` then draw -> group `a` has `aria-expanded="true"`, `scrollIntoView` called once, `focusFor('s1') === null`.
  - BoardList: `a focus request for a group the board lacks is dropped` (Review Focus 5) -- `requestFocus('s1', 'gone')` then draw a board without it -> `focusFor('s1') === null`, no group expanded.
- [ ] **Step 2: Run them** -> fail.
- [ ] **Step 3: Implement** the toolbar (title, conversation filter from the cards' distinct conversations, the switch, the segmented control, the error line when `error`), the card buttons (accessible name: title, category word, conversation), the open flow, and in `BoardList` an effect that, once the current session's board is loaded, applies `focusFor(current session)` -- expand, `scrollIntoView({ block: 'nearest' })`, `clearFocus()` -- or clears it when the group is absent.
- [ ] **Step 4: Run** both test files -> pass.
- [ ] **Step 5: Checkpoint.**

---

### Task 8: Terms and spec status

**Files:**
- Modify: `CONTEXT.md` (the Task Board entries), `docs/specs/2026-11-01-task-board-page-design.md`

- [ ] **Step 1:** Add **Board category** to CONTEXT.md beside Task Group: the page's four buckets, their rule order, and that *Archived* there means a completed Task Group, never an archived conversation; Chinese surfaces: 需要你回答 / 运行中 / 闲置 / 已归档.
- [ ] **Step 2:** Mark the spec's status line with what shipped and any deviation found while building (none expected).
- [ ] **Step 3: Checkpoint.**

---

### Task 9: Whole-branch verification and the owner's look

- [ ] **Step 1:** `nice -n 19 uv run --frozen --python 3.12 --all-extras pytest -q -n 4` -> only the known local baselines fail (the eight listed in the board branch's notes).
- [ ] **Step 2:** `cd ui-web && nice -n 19 node_modules/.bin/vitest run` -> all pass; `npm run type-check` -> 0; `cd ../ui-tui && npm run type-check` -> 0.
- [ ] **Step 3:** `ruff check` / `ruff format --check` on every changed Python file; the source-language gate on a snapshot (`SNAP=$(git stash create) && uv run python scripts/check_source_language.py "origin/main...$SNAP"`) -> exit 0.
- [ ] **Step 4:** `npm run build && ../.venv/bin/python3 build.py` -> both boot snapshots OK.
- [ ] **Step 5:** Real browser: headless Chromium screenshots of the page in board view and list view, with and without archived, at 1440 px and at a narrow width; compare with the approved mockup (`/root/scratch/taskboard/design/.superpowers/brainstorm/*/content/task-board-page.html`, version A).
- [ ] **Step 6:** Restart the live instance from the worktree with the cookie handed over (`/root/scratch/taskboard/restart_raven_web.py <supervisor pid>`), confirm `cookie kept: True` and the page answers, and hand it to the owner to try.
- [ ] **Step 7:** Run the pr-review-patterns sweep before any push.

### Task 10: Commit and push -- only on the owner's word

- [ ] **Step 1:** Show the commit message (Conventional Commits, ASCII, no trailer per the owner's ruling) and wait for "commit".
- [ ] **Step 2:** On "push": fetch, rebase if needed, rerun the task's tests, `git push --force-with-lease` only after a rebase.
