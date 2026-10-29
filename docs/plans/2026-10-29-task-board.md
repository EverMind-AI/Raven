# Host Task Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Raven host keeps a per-session Task Board -- Task Groups holding one Progress Step per turn that advanced them, every spawn / DAG dispatch placed under a step -- updated by one post-turn call after every host turn, recallable through a `task_board` tool, and drawn by the web page's tasks tab as an in-place accordion with a bottom "Completed" fold.

**Architecture:** A new inner package `raven/agent/task_board/` owns the board (pure model, file store, dispatch parsing, lineage, recall, the tool, the post-turn runner and a per-session service). `_process_message` launches the runner in the background beside Step 4, reusing the turn's own window and tools array so the call reads from the prompt cache. Two RPC methods and one event carry the board to `ui-web`, whose tasks tab swaps its flat list for the board.

**Tech Stack:** Python 3.12 (asyncio, dataclasses, pydantic config, pytest), OpenRPC JSON schema with generated TypeScript clients, React + vitest (`ui-web`), Ink TUI type-check (`ui-tui`).

**Spec:** `docs/specs/2026-10-29-task-board-design.md`. Section numbers below ("spec 3") point into it.

## Global Constraints

- Host only: everything here exists only when `not is_subagent_process()` (`raven/home.py:85`) and `agents.defaults.taskBoard.enabled` is true. Each `agents/*/config.json` sets `"taskBoard": {"enabled": false}`.
- Untouched (ring 4, owner sign-off zone): the `spawn`, `run_subagent_dag` and `load_playbook` tools and the DAG orchestration skill. The board only reads their receipts.
- Board file `<session_dir>/task_board.json`, `"version": 1`; `session_dir` is `SessionManager.session_dir(key)`.
- Statuses, in this order: `active`, `waiting`, `blocked`, `parked`, `completed`. The reader's actions set only `completed` and `active`.
- Handles: `spawn:<node_id>` and `dag:<run_id>`. Completed recall weight `0.5`; runners-up `2`; update attempts `3`; open-group steps shown in the instruction `2`; `TITLE_MAX = 80`, `TEXT_MAX = 400` characters (longer input is cut, not refused).
- Config: `"taskBoard": {"enabled": true, "reasoningEffort": "low"}`; `"minimal"` is refused at load (Codex rejects it).
- Wire names: methods `tasks.board`, `tasks.set_group_status`; event `tasks.board_updated` with payload `{session_key, board, lineage}`.
- Web strings (key: en / zh): `gui.tasks.board.completed`: "Completed - {n}" / "已完成 - {n}"; `gui.tasks.board.unassigned`: "Unassigned" / "未归类"; `gui.tasks.board.st_active`: "In progress" / "推进中"; `gui.tasks.board.st_waiting`: "Waiting on you" / "等你"; `gui.tasks.board.st_blocked`: "Blocked" / "卡住"; `gui.tasks.board.st_parked`: "Parked" / "搁置"; `gui.tasks.board.st_completed`: "Completed" / "已完成"; `gui.tasks.board.mark_completed`: "Mark completed" / "标为已完成"; `gui.tasks.board.reopen`: "Reopen" / "重新打开"; `gui.tasks.board.running`: "{n} sub-agents running" / "{n} 个子代理在跑"; `gui.tasks.board.dag`: "DAG {done}/{total}" / "DAG {done}/{total}"; `gui.tasks.board.empty`: "No tasks yet" / "还没有任务".
- Terms (AGENTS.md 6): **Task Board**, **Task Group**, **Progress Step**, defined in `CONTEXT.md` (Task 1) and cross-referenced in `ui-web/CONTEXT.md` (Task 9). **Task** keeps its `tasks.list` meaning.
- Source language: English everywhere; CJK only in `i18n/messages.json` (already a carrier), `raven/templates/prompts/zh/` (exempt zone) and the regenerated `ui-tui/src/i18n/messages.generated.ts`. Every new Python file opens with an English module docstring; comments only where they say why (AGENTS.md 1).
- `rpc-schema/openrpc.json` is the contract source; `raven/rpc/models.py` mirrors it by hand; `ui-web/src/rpc/generated*` and `ui-tui/src/rpc/generated.ts` are regenerated, never edited.
- `raven/agent/**` never imports `raven/rpc/**` (import-linter "inner layers know no surface").
- Commands: `$WT` is this worktree. Python: `cd $WT && nice -n 19 uv run --frozen --python 3.12 --all-extras pytest <paths> -q -p no:randomly`, never bare `pytest`, never `-n` above 4. Vitest: `cd $WT/ui-web && node node_modules/vitest/vitest.mjs run <paths>`, never `npx`. Before the first page task, `npm ci --prefix $WT/ui-web` and `npm ci --prefix $WT/ui-tui` (the worktree gets its own `node_modules`; never link the main checkout's).
- No commit and no push without the owner's explicit word (AGENTS.md 3.4). Each task ends by staging its files; the commit is Task 10, one commit, Conventional Commits, no trailer.

## Review Focus

1. **A dispatch refused before it started** (a `spawn` whose `node_id` is taken, a graph that fails validation): its receipt names no id, so the board must not demand it be placed. Pinned in Task 2.
2. **A provider that downgrades the forced tool choice and answers in prose**: counts as a failed attempt; after three, the turn's handles land in `unassigned` instead of vanishing. Pinned in Task 6.
3. **The reader switches conversation while `tasks.board` is in flight**: the answer must not be drawn into the conversation now open. Pinned in Task 8.
4. **A long session** (60 groups, 400 steps): the update instruction stays bounded -- two steps per open group, completed groups as id and title only. Pinned in Task 6.
5. **Two turns of one session ending back to back**: the second update must see the first one's write. Pinned in Task 6.

---

### Task 1: Board model and file store

**Files:**
- Create: `raven/agent/task_board/__init__.py`, `raven/agent/task_board/board.py`, `raven/agent/task_board/store.py`
- Modify: `CONTEXT.md` (three terms, beside **Task** at `:705`)
- Test: `tests/test_task_board_store.py`

**Interfaces:**
- Produces (`board.py`):
  - `STATUSES: tuple[str, ...]`, `TITLE_MAX = 80`, `TEXT_MAX = 400`
  - `@dataclass(frozen=True) class Dispatch: handle: str; summary: str`
  - `@dataclass class TaskLink: kind: str; id: str; summary: str`
  - `@dataclass class ProgressStep: id: str; turn_at: str; turn_id: str | None; text: str; tasks: list[TaskLink]`
  - `@dataclass class TaskGroup: id: str; title: str; status: str; created_at: int; updated_at: int; steps: list[ProgressStep]`
  - `@dataclass class Pending: handle: str; summary: str; turn_at: str; turn_id: str | None`
  - `@dataclass class Board: groups: list[TaskGroup]; unassigned: list[Pending]; version: int = 1`
  - `class UpdateRejected(ValueError)` -- `str(exc)` is the model-facing reason
  - `new_id(prefix: str) -> str` -- `f"{prefix}_{secrets.token_hex(4)}"`
  - `board_from_dict(data: Mapping[str, Any]) -> Board`, `board_to_dict(board: Board) -> dict[str, Any]` -- the spec 1 JSON, field for field
  - `apply_update(board: Board, args: Mapping[str, Any], dispatches: Sequence[Dispatch], *, turn_at: str, turn_id: str | None, now_ms: int, mint: Callable[[str], str] = new_id) -> Board`
  - `set_group_status(board: Board, group_id: str, status: str, *, now_ms: int) -> Board`
  - `drop_turns(board: Board, removed: Set[str]) -> Board` -- the steps of the turns an undo removed
- Produces (`store.py`): `BOARD_FILENAME = "task_board.json"`; `load_board(session_dir: Path) -> Board`; `save_board(session_dir: Path, board: Board) -> None`; `reset_board(session_dir: Path) -> None`; `copy_board(src: Path, dst: Path) -> None`

- [ ] **Step 1: Write the failing tests** in `tests/test_task_board_store.py`:

```python
SPAWN = Dispatch("spawn:locate_login_bug", "Locate the blank-page bug")
DAG = Dispatch("dag:20261029T100213Z-3eb3b470", "Review fixes")

def test_new_title_opens_a_group_holding_the_step():
    b = apply_update(Board([], []), {"steps": [{"new_title": "Fix bug A", "text": "Found it",
        "status": "waiting", "tasks": [SPAWN.handle]}]}, [SPAWN], turn_at="T1", turn_id=None, now_ms=5)
    g = b.groups[0]
    assert (g.title, g.status, g.created_at, g.updated_at) == ("Fix bug A", "waiting", 5, 5)
    assert g.steps[0].turn_at == "T1"
    assert g.steps[0].tasks == [TaskLink("spawn", "locate_login_bug", "Locate the blank-page bug")]

def test_every_dispatch_is_placed_exactly_once():  # three refusals, by message prefix
    # nothing placed -> "not placed: spawn:locate_login_bug, dag:20261029T100213Z-3eb3b470"
    # one handle on two steps -> "placed more than once: spawn:locate_login_bug"
    # a handle this turn did not dispatch -> "not dispatched this turn: spawn:other"

def test_unassigned_is_placed_with_the_turn_and_then_cleared(): ...
def test_a_step_on_a_completed_group_reopens_it_unless_it_says_completed(): ...
def test_steps_may_be_empty_only_when_nothing_was_dispatched():
    # [] with dispatches -> "steps may be empty only when nothing was dispatched"; [] without -> board unchanged
def test_group_and_new_title_are_exclusive_and_known():
    # both or neither -> "a step needs exactly one of group and new_title"; "unknown group: g_x";
    # "unknown status: done"; blank new_title -> "new_title is empty"
def test_long_title_and_text_are_cut_to_their_caps(): ...   # len == TITLE_MAX / TEXT_MAX
def test_statuses_apply_after_steps(): ...
def test_set_group_status_completes_and_reopens(): ...      # updated_at moves; unknown id -> KeyError
def test_drop_turns_drops_undone_steps_and_groups_left_empty(): ...
def test_save_round_trips_and_leaves_no_temporary_file(tmp_path): ...
def test_a_corrupt_file_loads_as_an_empty_board(tmp_path, caplog): ...   # and logs a warning
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd $WT && nice -n 19 uv run --frozen --python 3.12 --all-extras pytest tests/test_task_board_store.py -q -p no:randomly`
Expected: FAIL, `ModuleNotFoundError: raven.agent.task_board`

- [ ] **Step 3: Implement `board.py` and `store.py`**

`apply_update` works on a deep copy and raises before touching it. Validation order: shape of each step, then group/status lookups, then the placement set -- required handles are `{d.handle for d in dispatches} | {p.handle for p in board.unassigned}`, placed handles are the steps' `tasks` concatenated. A placed pending handle takes its summary from the `Pending`; a step's `turn_at` / `turn_id` are the call's even for a pending handle. `save_board` writes `task_board.json.tmp` beside the file, then `os.replace`. `load_board` answers `Board([], [])` for a missing file and for one that does not parse (logged with `logger.warning`).

- [ ] **Step 4: Add the three terms to `CONTEXT.md`** -- Task Board, Task Group, Progress Step, with the definitions and `_Avoid_` lists of spec "Vocabulary", placed after **Task** (`:705`).

- [ ] **Step 5: Run the tests to see them pass** -- same command; Expected: all pass.

- [ ] **Step 6: Stage** -- `git -C $WT add raven/agent/task_board tests/test_task_board_store.py CONTEXT.md`

---

### Task 2: The turn's dispatch list

**Files:**
- Create: `raven/agent/task_board/dispatches.py`
- Modify: `raven/rpc/methods/session.py:304-310` (drop `_DAG_RUN_ID_RE` / `_SPAWN_TASK_ID_RE`), `:439-448` (call the helpers)
- Test: `tests/test_task_board_dispatches.py`

**Interfaces:**
- Consumes: `Dispatch` (Task 1)
- Produces:
  - `DAG_RUN_ID_RE = re.compile(r"\bDAG (?:run )?(\d{8}T\d+Z-[0-9a-f]+)")`
  - `SPAWN_TASK_ID_RE = re.compile(r"\bstarted \(id: ([0-9a-f]{8})\)")`
  - `dag_run_id_of(content: str) -> str | None` (first match), `spawn_task_id_of(content: str) -> str | None` (last match, as `session.py:443-447` explains)
  - `turn_dispatches(messages: Sequence[Mapping[str, Any]]) -> list[Dispatch]`

- [ ] **Step 1: Write the failing tests** in `tests/test_task_board_dispatches.py`. Build turns as the session log stores them: an assistant row with `tool_calls=[{"id", "function": {"name", "arguments": <json str>}}]` and a `role: "tool"` row with `tool_call_id`, `name`, `content`.

```python
def test_a_started_spawn_is_its_node_id():
    # receipt "Subagent [Locate it] started (id: 1a2b3c4d). ..." with args node_id="locate_login_bug",
    # task_summary="Locate it" -> [Dispatch("spawn:locate_login_bug", "Locate it")]
def test_a_refused_spawn_is_not_a_dispatch():          # Review Focus 1
    # receipt "Error: node_id 'locate_login_bug' is already taken ..." -> []
def test_graph_and_playbook_receipts_name_their_run():
    # "DAG run 20261029T100213Z-3eb3b470 started in the background (3 nodes). ..." (run_subagent_dag)
    # "DAG 20261029T100500Z-0badf00d: started 'review-fix' (4 steps); ..." (load_playbook, summary = args["name"])
def test_dispatches_keep_their_first_order_without_duplicates(): ...
def test_history_still_stamps_a_background_run():
    # raven.rpc.methods.session's history row for a run_subagent_dag tool row carries dag_run_id as before
```

- [ ] **Step 2: Run them to see them fail** -- `... pytest tests/test_task_board_dispatches.py ...`; Expected: FAIL, module missing.

- [ ] **Step 3: Implement `dispatches.py`; point `session.py` at the two helpers.** Tool contents that are lists are read as the concatenation of their `text` parts. `session.py` keeps applying the DAG helper to `run_subagent_dag` rows only, so the one change to `session.history` is that a foreground run's receipt (`"DAG <run_id>: ... -- outputs in ..."`, `dag_tool.py:1256`) is now stamped too.

- [ ] **Step 4: Run the new tests and the history tests** -- `... pytest tests/test_task_board_dispatches.py tests/test_rpc_session.py tests/test_rpc_tasks.py ...`; Expected: all pass.

- [ ] **Step 5: Stage** -- `git -C $WT add raven/agent/task_board/dispatches.py raven/rpc/methods/session.py tests/test_task_board_dispatches.py`

---

### Task 3: Lineage and recall

**Files:**
- Create: `raven/agent/task_board/lineage.py`, `raven/agent/task_board/recall.py`
- Test: `tests/test_task_board_recall.py`

**Interfaces:**
- Consumes: `Board`, `TaskGroup`, `TaskLink` (Task 1); `raven.agent.subagent.history.dag_root`, `session_history_root`; `raven.agent.subagent.dag_store.REGISTRY_FILENAME`; `raven.stint.record.StintStore`, `STINTS_DIRNAME`; `raven.utils.bm25.BM25Okapi`, `tokenize`
- Produces:
  - `descendants(session_dir: Path, placed_runs: Iterable[str]) -> dict[str, str]` -- descendant run id to the placed run it descends from
  - `task_status(session_dir: Path, link: TaskLink) -> str` -- `"unknown"` when the registry has no row
  - `COMPLETED_RECALL_WEIGHT = 0.5`, `RUNNERS_UP = 2`
  - `list_groups(board: Board, *, now_ms: int) -> str`
  - `thread_text(board: Board, group_id: str, *, status_of: Callable[[TaskLink], str], lineage: Mapping[str, str]) -> str` -- `KeyError` for an unknown id
  - `search(board: Board, query: str, *, status_of: Callable[[TaskLink], str], lineage: Mapping[str, str]) -> str`
  - `turn_texts(messages: Sequence[Mapping[str, Any]], turn_ats: Sequence[str], *, max_tokens: int = 4000) -> str`

- [ ] **Step 1: Write the failing tests** in `tests/test_task_board_recall.py`:

```python
def test_listing_puts_open_groups_before_completed_ones(): ...
def test_a_thread_names_each_task_with_its_live_status(tmp_path):
    # nodes.json {"nodes": {"locate_login_bug": {"kind": "spawn", "status": "completed"}}} -> "completed" in text
def test_search_finds_a_matter_in_chinese_and_in_english():
    # group "修复登录白屏" found by "登录 白屏"; group "Release notes 0.26" found by "release notes"
def test_a_completed_group_ranks_below_an_open_one_on_equal_text(): ...
def test_search_names_up_to_two_runners_up_by_id(): ...
def test_replan_successors_and_later_stint_rounds_resolve_to_the_placed_run(tmp_path):
    # graph.json of run A carries {"replan": {"run_id": "B"}}; a stint record whose rounds are A, C, D
    # -> descendants(tmp_path, ["A"]) == {"B": "A", "C": "A", "D": "A"}
def test_turn_texts_report_a_turn_the_log_no_longer_holds(): ...    # "gone" for a missing turn_at
def test_turn_texts_say_when_the_budget_binds(): ...
```

- [ ] **Step 2: Run them to see them fail** -- Expected: FAIL, module missing.

- [ ] **Step 3: Implement.** `descendants` follows each `<dag_root>/<run>/graph.json` `replan.run_id` chain, and reads `StintStore(dag_root(session_dir) / STINTS_DIRNAME).list()` -- the root `SubAgentDagTool.stints_root` resolves (`dag_tool.py:1033`) -- mapping every round's `run_id` after the first to the first round's, when the first is placed (directly or through a replan). `task_status` reads `session_history_root(session_dir) / REGISTRY_FILENAME`: a spawn's row status, or for a run the tally of its nodes' statuses read as running > failed > completed. `search` indexes one document per group (title repeated twice, step texts, link summaries), multiplies a completed group's score by `COMPLETED_RECALL_WEIGHT`, answers the top group's `thread_text` plus `RUNNERS_UP` id-and-title lines, and says so plainly when nothing scores. `turn_texts` gives, per `turn_at`, the message carrying that timestamp and the last assistant text before the next user message, cut by `raven.utils.tokens.estimate_message_tokens` with the budget reported when it binds.

- [ ] **Step 4: Run the tests to see them pass.**

- [ ] **Step 5: Stage** -- `git -C $WT add raven/agent/task_board/lineage.py raven/agent/task_board/recall.py tests/test_task_board_recall.py`

---

### Task 4: The `task_board` tool

**Files:**
- Create: `raven/agent/task_board/tool.py`, `raven/agent/task_board/service.py`
- Test: `tests/test_task_board_tool.py`

**Interfaces:**
- Consumes: Tasks 1-3; `raven.contracts.tool.Tool`; `SessionManager.session_dir`, `SessionManager.peek`
- Produces (`service.py`):
  - `class TaskBoardService` -- `__init__(self, sessions: SessionManager)`; `session_dir(self, session_key: str) -> Path`; `lock(self, session_key: str) -> asyncio.Lock` (one per key); `set_sink(self, sink: Callable[[str, dict], Awaitable[None]] | None) -> None`; `load(self, session_key: str) -> Board`; `lineage(self, session_key: str, board: Board) -> dict[str, str]`; `async write(self, session_key: str, board: Board) -> None` (save, then emit `{"type": "tasks.board_updated", "payload": {"session_key", "board": board_to_dict(board), "lineage"}}` fire-and-forget the way `SubagentManager._emit_event` does); `async set_status(self, session_key: str, group_id: str, status: str) -> Board` (under `lock`; only `completed` / `active`, else `UpdateRejected`)
- Produces (`tool.py`): `TOOL_NAME = "task_board"`; `class TaskBoardTool(Tool)` with `__init__(self, service: TaskBoardService)`, `set_context(self, channel: str, chat_id: str, session_key: str | None = None) -> None` (a `ContextVar`, as `context_retrieve.py:56-60`), `async execute(self, action: str, query: str | None = None, group_id: str | None = None, include_turns: bool = False, **_: Any) -> str`; `REFUSED_UPDATE = "The board is updated for you after this turn ends; use action=recall to read it."`

- [ ] **Step 1: Write the failing tests** in `tests/test_task_board_tool.py`:

```python
def test_the_schema_offers_both_actions_and_the_five_statuses(): ...
def test_update_during_a_turn_is_refused():   # execute(action="update", steps=[...]) == REFUSED_UPDATE, file unchanged
def test_recall_reads_only_the_bound_sessions_board(tmp_path): ...
def test_recall_on_a_session_without_a_board_says_it_is_empty(): ...
def test_recall_with_include_turns_reads_the_session_log(): ...
async def test_set_status_writes_and_announces_once(): ...      # sink called with type "tasks.board_updated"
async def test_set_status_refuses_other_statuses(): ...
```

- [ ] **Step 2: Run them to see them fail.**

- [ ] **Step 3: Implement.** The schema is spec 2's, with one short description: "This conversation's task board: one group per matter, one step per turn that advanced it. Updated for you after every turn. action=recall reads it: no arguments lists every group, group_id returns a group's whole thread, query finds a matter by its words." Parameter descriptions stay one clause each (the schema rides in every host call).

- [ ] **Step 4: Run the tests to see them pass.**

- [ ] **Step 5: Stage.**

---

### Task 5: Configuration, registration and the host gate

**Files:**
- Modify: `raven/config/schema.py:148-206` (`TaskBoardConfig`, `AgentDefaults.task_board`), `raven/agent/loop/bundles.py:54` (`EngineWiring.task_board_config`), `raven/core/runtime.py:308` (pass `config.agents.defaults.task_board`), `raven/agent/loop/main.py:213,289` (keep it as `self._task_board_config`; `self.task_board: TaskBoardService | None = None`), `raven/agent/loop/wiring.py:1498` (`_register_orchestration_tools`) and `:2099-2124` (`_set_tool_context` name lists), `agents/{raven-code,raven-design,raven-oncall,raven-ppt,raven-research}/config.json`
- Test: `tests/test_task_board_registration.py`, `tests/test_agent_loop_token_budget.py`

**Interfaces:**
- Produces: `class TaskBoardConfig(Base): enabled: bool = True; reasoning_effort: str = "low"` (a validator refuses `"minimal"`); `AgentDefaults.task_board: TaskBoardConfig` (alias `taskBoard`); `AgentLoop.task_board: TaskBoardService | None` -- set only where the tool is registered.

- [ ] **Step 1: Write the failing tests** in `tests/test_task_board_registration.py`, building loops the way `tests/test_agent_loop_token_budget.py` does:

```python
def test_the_host_registers_the_board_and_its_service(): ...      # tools.get("task_board") and loop.task_board
def test_a_sub_agent_process_has_neither(monkeypatch):            # RAVEN_SUBAGENT=1
def test_a_disabled_board_has_neither(): ...
def test_every_product_config_switches_the_board_off():           # agents/*/config.json -> enabled is False
def test_minimal_effort_is_refused_at_load(): ...
def test_the_turn_binds_the_board_to_its_session(): ...           # _set_tool_context reaches TaskBoardTool.set_context
```

and in `tests/test_agent_loop_token_budget.py`, beside `test_what_enabling_a2a_costs_the_tool_surface`, `test_what_the_task_board_costs_the_tool_surface` pinning the measured token cost of the schema.

- [ ] **Step 2: Run them to see them fail.**

- [ ] **Step 3: Implement.** Inside `_register_orchestration_tools` (already behind `not is_subagent_process()`), when `self._task_board_config.enabled`: build `self.task_board = TaskBoardService(self.sessions)` and register `TaskBoardTool(self.task_board)`, before the closing `list_last("run_subagent_dag", "spawn")` so that ordering stays. Add `"task_board"` to both name lists in `_set_tool_context`.

- [ ] **Step 4: Run the new tests and the budget file**, then measure the cost, write it into the pin, run again. Expected: pass.

- [ ] **Step 5: Stage.**

---

### Task 6: The post-turn call

**Files:**
- Create: `raven/agent/task_board/runner.py`, `raven/templates/prompts/en/task_board_update.md`, `raven/templates/prompts/zh/task_board_update.md`
- Modify: `raven/agent/loop/_shared.py:258` (`LoopOutcome.last_tools: list[dict] | None = None`), `raven/agent/loop/turn_path.py` (set `last_tools` from the final iteration's `tool_defs` where the outcome is built, `:2714`; keep the window at `:3253` and `:3279`; launch the runner beside Step 4, `:3465`)
- Test: `tests/test_task_board_runner.py`, `tests/test_task_board_turn.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4; `raven.i18n.prompt`; `raven.contracts.llm_provider.LLMResponse`
- Produces:
  - `MAX_ATTEMPTS = 3`, `OPEN_STEPS_SHOWN = 2`
  - `build_instruction(board: Board, dispatches: Sequence[Dispatch]) -> str` -- `prompt("task_board_update").format(board=..., dispatches=...)`
  - `async run_post_turn(service: TaskBoardService, *, session_key: str, call: Callable[[list[dict]], Awaitable[LLMResponse]], window: list[dict], dispatches: list[Dispatch], turn_at: str, turn_id: str | None, now_ms: Callable[[], int]) -> Board | None`

- [ ] **Step 1: Write the failing runner tests** in `tests/test_task_board_runner.py`, with `call` a scripted fake returning `LLMResponse`s:

```python
async def test_an_accepted_update_is_written_and_announced(): ...
async def test_a_rejected_update_is_asked_again_with_its_reason():
    # 2nd call's messages end with the assistant tool call and a tool row whose content starts "Rejected: not placed:"
async def test_prose_three_times_parks_the_handles_in_unassigned():          # Review Focus 2
async def test_a_recall_inside_the_post_turn_call_counts_as_a_failure(): ...
async def test_a_provider_error_parks_the_handles(): ...                      # finish_reason "error", and a raised exception
async def test_two_updates_of_one_session_run_in_order():                     # Review Focus 5
    # the second call's instruction lists the first update's group
async def test_the_instruction_stays_bounded_on_a_long_session():             # Review Focus 4
    # 60 groups x ~7 steps: each open group shows OPEN_STEPS_SHOWN steps; completed ones only "id: title"
async def test_the_session_log_is_left_alone(): ...                           # session file bytes unchanged
async def test_nothing_dispatched_and_an_empty_update_writes_nothing(): ...
async def test_unassigned_handles_are_listed_before_the_turns_own(): ...
```

- [ ] **Step 2: Write the failing turn tests** in `tests/test_task_board_turn.py`, driving `agent._process_message(...)` with the `ScriptedProvider` pattern of `tests/test_agent_loop_usage_sink.py` (`:504`, `:124`):

```python
async def test_a_host_turn_calls_once_more_with_its_window_and_tools():
    # the extra request's tools == the turn's last request's tools; its messages == the turn's final
    # window + one user instruction; tool_choice == {"type": "function", "function": {"name": "task_board"}}
async def test_the_reply_goes_out_before_the_board_call_returns(): ...
async def test_the_step_is_keyed_by_the_turns_received_at():
    # step.turn_at == the timestamp the session log stores on the turn's opening message
async def test_an_errored_turn_that_dispatched_nothing_makes_no_call(): ...
async def test_the_call_is_counted_in_session_usage_under_its_purpose(): ...  # purpose "task_board"
```

- [ ] **Step 3: Run both files to see them fail.**

- [ ] **Step 4: Write the two templates.** English: the instruction of spec 3 -- the board snapshot under `{board}`, the handles under `{dispatches}`, then the rules in spec 3's order. Chinese: the same text translated, same placeholders.

- [ ] **Step 5: Implement `runner.py`.** One attempt: `call(window + [{"role": "user", "content": instruction}] + retries)`. A `task_board` call with `action == "update"` goes to `apply_update`; on `UpdateRejected` the retry rows are the assistant's tool call and a tool row `"Rejected: <reason>. Call task_board again with action=update."`. Prose, a recall, `finish_reason == "error"` or a raised exception is a failed attempt. Accepted: `await service.write(...)` under `service.lock(session_key)`, re-reading the board inside the lock. After `MAX_ATTEMPTS`, or at once on a provider error: append a `Pending` per handle not already pending, write, `logger.warning`.

- [ ] **Step 6: Wire `turn_path.py`.** Bind the window instead of discarding it at `:3253` / `:3279` (`final_content, _, window, outcome = ...`). Beside Step 4, when `self.task_board is not None` and (`outcome.status == "completed"` or the turn dispatched something): compute `turn_dispatches(live["log"].messages())` -- the turn's own record, which no window pass rewrites -- build `call` as a closure that runs `self.strategies.before_llm_call(messages, outcome.last_tools, routed_model)` and then `self.provider.chat_with_retry(messages=..., tools=..., model=..., fallback_models=fallback_models, tool_choice={"type": "function", "function": {"name": "task_board"}}, reasoning_effort=self._task_board_config.reasoning_effort)` inside `_llm_purpose("task_board")` and `usage_context.bind(key)`, and schedule `run_post_turn(..., turn_at=turn_received_at, ...)` as a task in `self._consolidation_tasks`, the way `_t4` is.

- [ ] **Step 7: Run both files to see them pass**, then the loop's neighbours: `... pytest tests/test_agent_loop_token_budget.py tests/test_task_board_*.py -q ...`.

- [ ] **Step 8: Stage.**

---

### Task 7: Wire surface and history edits

**Files:**
- Modify: `rpc-schema/openrpc.json` (methods `tasks.board`, `tasks.set_group_status`; schemas `TaskBoard`, `TaskGroup`, `ProgressStep`, `TaskLinkRef`, `PendingDispatch`, `TaskGroupStatus`, `TasksBoardResult`, `TasksBoardUpdatedPayload`, `TasksBoardUpdatedEvent`; the event joins `TurnEvent.oneOf`), `raven/rpc/models.py` (mirrors; the method map at `:7127`; the `TurnEvent` union at `:1573`), `raven/rpc/methods/tasks.py:528` (handlers, registration), `raven/rpc/bootstrap.py:388` (bind the board's sink to `emitter.emit`), `raven/rpc/methods/session.py:1167,1199,1452` (clear, undo, branch), `ui-tui/src/app/chatStream.ts:533` (a no-op case for `tasks.board_updated`), regenerated `ui-tui/src/rpc/generated.ts`
- Test: `tests/test_rpc_tasks_board.py`; `tests/test_rpc_schema_match.py`, `tests/test_rpc_contract_shapes.py` unchanged and passing

**Interfaces:**
- Consumes: Tasks 1, 3, 4
- Produces: `async tasks_board(params: dict, *, agent_loop_factory=None) -> dict` (`{"board", "lineage"}`; reads the file directly when no loop or no service is up); `async tasks_set_group_status(params: dict, *, agent_loop_factory=None) -> dict` (`{"board", "lineage"}`; through `loop.task_board.set_status`; `RpcError` when there is no service, for an unknown group, and for a status other than `completed` / `active`)

- [ ] **Step 1: Write the failing tests** in `tests/test_rpc_tasks_board.py`:

```python
async def test_an_unknown_session_has_an_empty_board(): ...
async def test_the_board_comes_with_its_lineage(): ...
async def test_set_group_status_writes_and_emits_the_event(): ...
async def test_set_group_status_refuses_anything_but_completed_and_active(): ...
async def test_clear_empties_the_board(): ...
async def test_undo_drops_the_undone_turns_steps():
    # kept = {m["timestamp"] for m in session.messages} after undo_last_turn
async def test_branch_copies_the_board_to_the_child(): ...
```

- [ ] **Step 2: Run them to see them fail.**

- [ ] **Step 3: Implement.** Schema first (descriptions in the house style of `tasks.list`), then `models.py`, then the handlers, then the three session hooks (`reset_board`, `drop_turns` + `save_board`, `copy_board`), then the sink binding in `bootstrap.py` only -- the TUI path (`raven/cli/tui_commands.py:412`) binds none. Regenerate the TUI client: `npm run gen:rpc --prefix $WT/ui-tui`.

- [ ] **Step 4: Run** the new tests with `tests/test_rpc_schema_match.py tests/test_rpc_contract_shapes.py`, then `npm run lint:rpc --prefix $WT/ui-tui` and `npm run type-check --prefix $WT/ui-tui`. Expected: pass.

- [ ] **Step 5: Stage.**

---

### Task 8: Page data -- client, source, store, live event, turn seam

**Files:**
- Regenerate: `ui-web/src/rpc/generated*` (`npm run gen --prefix $WT/ui-web`)
- Modify: `ui-web/src/features/tasks/types.ts` (re-exports; `TasksSource` gains `board`, `setGroupStatus`, `onBoardUpdated?`), `ui-web/src/features/tasks/source.ts:26`, `ui-web/src/features/tasks/store.ts`, `ui-web/src/state/session/stages.ts` (an `arm('tasks.board_updated', ...)` beside `subagent.status` at `:316`, and its `stageOf` case at `:411`), `ui-web/src/features/transcript/source.ts`, `ui-web/src/features/transcript/TurnNav.tsx:71` (export `goTo`)
- Test: `ui-web/src/features/tasks/board.test.ts`, `ui-web/src/features/transcript/source.test.ts`

**Interfaces:**
- Consumes: Task 7's generated `TaskBoard`, `TaskGroup`, `ProgressStep`, `TasksBoardResult`, `TasksBoardUpdatedPayload`
- Produces:
  - `TasksSource.board(sessionKey: string): Promise<TasksBoardResult>`; `TasksSource.setGroupStatus(sessionKey: string, groupId: string, status: 'completed' | 'active'): Promise<TasksBoardResult>`; `TasksSource.onBoardUpdated?: (p: TasksBoardUpdatedPayload) => void`
  - store state `board: TaskBoard | null`, `lineage: Record<string, string>`, `boardLoaded: boolean`; `refreshBoard(): Promise<void>`; `onBoardUpdated(p): void`; `openGroups(board: TaskBoard): TaskGroup[]` and `completedGroups(board: TaskBoard): TaskGroup[]` (both `created_at` descending); `linkedRows(step: ProgressStep, rows: TaskRow[], lineage: Record<string, string>): { link: TaskLinkRef; row: TaskRow | null }[]` (descendants after their ancestor); `unplacedRows(rows: TaskRow[], board: TaskBoard | null, lineage: Record<string, string>): TaskRow[]`
  - `revealTurnAt(turnAt: string): boolean` in `features/transcript/source.ts` -- finds the shown lane's opening ask whose `at` equals the transcript's own `msOf(turnAt)` and scrolls to it with `goTo`

- [ ] **Step 1: Write the failing tests** in `board.test.ts`:

```ts
it('drops a board answer that lands after the reader switched conversation')   // Review Focus 3
it('replaces the board on tasks.board_updated for the open session only')
it('orders open and completed groups by created_at, newest first')
it('counts a lineage-placed row as placed')
it('re-reads the board once when tasks.list gains a dag row nothing places')
```

and in `transcript/source.test.ts`: `it('reveals the turn whose opening ask carries that timestamp')`, `it('answers false for a turn the lane does not hold')`.

- [ ] **Step 2: Run them to see them fail** -- `cd $WT/ui-web && node node_modules/vitest/vitest.mjs run src/features/tasks/board.test.ts src/features/transcript/source.test.ts`

- [ ] **Step 3: Implement**, following `refresh()`'s session guard (`store.ts:115-130`) for `refreshBoard`.

- [ ] **Step 4: Run them to see them pass**, then `npm run gen:check --prefix $WT/ui-web` and the page's gates (`node node_modules/vitest/vitest.mjs run scripts/gates` from `ui-web/`). Expected: pass.

- [ ] **Step 5: Stage.**

---

### Task 9: Page UI -- the board in the tasks tab

**Files:**
- Create: `ui-web/src/features/tasks/BoardList.tsx`, `ui-web/src/features/tasks/BoardList.test.tsx`
- Modify: `ui-web/src/features/tasks/TasksPage.tsx:211-251` (`TasksApp` renders `BoardList`; `List` / `Group` / `Row` go if nothing else uses them), `ui-web/src/features/tasks/TasksPage.test.tsx` (its list-view cases move to `BoardList.test.tsx`; pane cases untouched), `ui-web/src/features/tasks/styles.css` (`tk` prefix), `i18n/messages.json` (the Global Constraints keys), `ui-web/CONTEXT.md` (**Task Board** beside **Task** at `:378`, pointing at `CONTEXT.md`), regenerated `ui-tui/src/i18n/messages.generated.ts` (`npm run gen:i18n --prefix $WT/ui-tui`)
- Test: `BoardList.test.tsx`

**Interfaces:**
- Consumes: Task 8's store functions and `revealTurnAt`; `desk.openDeskTask(row)` as `Row` uses it today
- Produces: `export function BoardList(): JSX.Element`

- [ ] **Step 1: Write the failing tests** in `BoardList.test.tsx`:

```tsx
it('lists open groups newest first, then a collapsed Completed fold with its count')
it('expands a group in place, aria-expanded true, steps without numbers')
it('keeps several groups open at once')
it('toggles a group from the keyboard with Enter and Space')
it('opens the task pane from a chip, and leaves a chip with no row inert and dimmed')
it('shows a running badge from the group s running rows, else the age of its last step')
it('marks a group completed and reopens it from the row menu')
it('shows Unassigned only when a row is placed nowhere')
it('scrolls the transcript to a step s turn when its text is clicked')
it('renders the dot colour of each of the five statuses')
```

- [ ] **Step 2: Run it to see it fail.**

- [ ] **Step 3: Implement `BoardList`** to spec 5: rows are `<button aria-expanded>` with the status dot (`data-st`), the title and the latest step, the badge or age at the right; a thread is a list of steps (text, `HH:MM` or date + time, chips); the fold is a `<button aria-expanded>` labelled `gui.tasks.board.completed`; Unassigned sits first. Dots reuse `--moss`, `--amber`, `--clay`, `--faint`, and a ring for `parked`.

- [ ] **Step 4: Add the i18n keys, regenerate the TUI catalogue, update `ui-web/CONTEXT.md`.**

- [ ] **Step 5: Run** `BoardList.test.tsx` and `TasksPage.test.tsx`, then the whole page suite (`npm test --prefix $WT/ui-web`; judge any red against an `origin/main` run before blaming this change). Expected: pass.

- [ ] **Step 6: Stage.**

---

### Task 10: Real-provider smoke, gates, the commit

**Files:**
- Create: `tests/integration/test_task_board_real_llm.py`

- [ ] **Step 1: Write the smoke**: three turns on one session over two matters, one of them spawning a sub-agent; then assert the board holds two groups, three steps, the spawn's handle under the step of the turn that dispatched it, and `unassigned == []`. Same `pytestmark` as `tests/integration/test_agent_harness_real_llm.py` (`real_llm`, skipped without an OpenRouter key), with `MODEL = "openrouter/z-ai/glm-5.3-flash"` -- live tests never run on a premium model.

- [ ] **Step 2: Run it** -- `... pytest tests/integration/test_task_board_real_llm.py -q ...`; Expected: pass.

- [ ] **Step 3: Run the gates** -- `make lint`, `make check-large-files`, `make check-source-language` (against the three-dot range, with new files added `-N` first), `npm run gen:check --prefix $WT/ui-web`, `npm run lint:rpc --prefix $WT/ui-tui`, the full Python suite at `-n 4` under `nice -n 19`, and the page suite. Expected: all green, or reds shown identical on `origin/main`.

- [ ] **Step 4: Run the `pr-review-patterns` pre-submit sweep** over `git diff origin/main...HEAD` and fix what it finds.

- [ ] **Step 5: Commit, on the owner's word only** -- one commit, `feat(*): keep a task board of each conversation's matters` (or the owner's subject), no trailer.
