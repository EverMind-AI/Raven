# Claude Code Background Settle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code ACP run returns once, with the agent's final answer, after the background work that the run started has ended.

**Architecture:** A pure state class (`BackgroundSettle`) reads Claude Code's raw SDK messages and answers "is the run's work over". The Claude Code dialect opts into those messages at session open. The turn collector feeds the state class and marks wake turns. `AcpAgentBackend.run` waits for the state class after the prompt answers, inside the session lock, before its teardown.

**Tech Stack:** Python 3.11+, asyncio, pytest (`uv run pytest`), the ACP stub server in `tests/acp_stub_server.py`.

**Spec:** `docs/specs/2026-11-24-claude-code-background-settle-design.md`

## Global Constraints

- All source, comments, logs and test fixtures in English, ASCII only (AGENTS.md section 1.3).
- Comments only for a non-obvious why (AGENTS.md section 1.1); every new file has an English module docstring.
- Tests run with `uv run pytest ...`, never bare `pytest`; no `-n 8` (owner rule: keep test runs light on CPU).
- Do not commit unless the owner says so (AGENTS.md section 3.4). Each task ends at "report"; the commit commands are for when the owner says commit.
- The opt-in `_meta` is identical on `session/new`, `session/load` and `session/resume` (spec, Constraints).
- Ceiling `3600` s from the prompt answer; unwoken-end bound `15` s; alive stamp every `60` s (spec, Design 4-5).
- Agents other than claude-agent-acp, and the Mirasim shim, keep today's behaviour exactly (spec, Non-goals).

## Review Focus

1. A Claude Code run on an older CLI that sends no `session_state_changed`: it must return on the prompt answer, not wait for the ceiling. (Task 4, `test_no_state_message_returns_on_the_answer`.)
2. The adapter reconnects or dies during settle: the run must return with what it has, not hang until the ceiling. (Task 4, `test_connection_lost_during_settle_returns_the_reply`.)
3. Two runs on one instance, the first leaving a task running past its ceiling: the second must not wait for that task. (Task 1, `test_a_task_from_an_earlier_run_does_not_hold`.)
4. A Monitor whose `task_started` arrives before its `tool_call` frame names it: the Monitor check is made when the rule is checked, not when the task starts. (Task 1, `test_monitor_named_after_its_task_started`.)
5. A raw `result` that arrives before the prompt answer (the subagent case): it is counted once in usage, and the prompt answer's counts are not added on top. (Task 1, `test_usage_sums_results_not_the_answer`.)

---

### Task 1: The settle state

**Files:**
- Create: `raven/acp_client/background_settle.py`
- Modify: `tests/acp_frames.py` (append two fixtures)
- Test: `tests/test_acp_background_settle.py` (create)

**Interfaces:**
- Produces, in `raven/acp_client/background_settle.py`:
  - `RAW_MESSAGE_METHOD = "_claude/sdkMessage"`
  - `RAW_MESSAGE_FILTER: list[dict[str, str]]` = the four filters in spec Design 1, in that order.
  - `SETTLE_CEILING_S = 3600.0`, `UNWOKEN_END_S = 15.0`, `ALIVE_EVERY_S = 60.0`
  - `class BackgroundSettle(is_monitor_call: Callable[[str], bool])`
    - `prompt_answered() -> None`
    - `apply(message: dict[str, Any], now: float) -> list[str] | None` - returns `None` unless this message is a `running` that starts a wake turn after the prompt answered; then returns the descriptions of this run's tasks that left the live set since the previous turn (empty list when none did).
    - `reporting: bool` (property) - a `session_state_changed` arrived in this run.
    - `state: str | None` (property) - last state seen.
    - `done(now: float) -> bool` - the four-condition rule of spec Design 3.
    - `running_tasks() -> list[str]` - descriptions of this run's live tasks, Monitor and ambient excepted.
    - `usage_counts() -> dict[str, int] | None` - the summed `usage` of every raw `result` of the run, in ACP field names `inputTokens`, `outputTokens`, `cachedReadTokens`, `cachedWriteTokens`, `totalTokens` (total = the sum of the other four); `None` when no `result` arrived.
  - `wake_marker(agent: str, ended: list[str]) -> str` - `f"[{agent} resumed on its own: {', '.join(ended)} ended]"`, or `f"[{agent} resumed on its own: a background event]"` when `ended` is empty.
- The module imports nothing from `raven.acp_client` (the dialects import it in Task 2).

- [ ] **Step 1: Add the probe fixtures**

Append to `tests/acp_frames.py` two lists, `CLAUDE_SETTLE_BASH_MONITOR` and `CLAUDE_SETTLE_SUBAGENT`, of `(t, method, params)` tuples, copied from the live probe's recorded frames (the bash-plus-Monitor session and the background-subagent session). Keep, in order: raw `_claude/sdkMessage` notifications trimmed to the fields the settle reads, `tool_call` frames trimmed to `toolCallId` and `_meta.claudeCode.toolName`, `agent_message_chunk` text, and the prompt answer as method `"answer"`. Times are seconds from the prompt send.

Precede them with a comment naming the source: claude-agent-acp 0.108.0, live probe 2026-11-24, sonnet low; `t` is seconds from the prompt send; method `"answer"` marks the prompt answer.

- [ ] **Step 2: Write the failing tests**

In `tests/test_acp_background_settle.py`, a helper `_replay(frames, until)` drives a `BackgroundSettle` from a fixture: `session/update` tool_call frames fill a `{toolCallId: toolName}` dict that backs `is_monitor_call`; `"answer"` calls `prompt_answered()`; `_claude/sdkMessage` calls `apply(params["message"], t)`. It returns the settle and the list of `(t, done(t))` after each frame. Tests:

```python
def test_bash_run_is_done_only_after_the_last_idle():
    settle, checks = _replay(CLAUDE_SETTLE_BASH_MONITOR)
    first_done = next(t for t, done in checks if done)
    assert first_done == pytest.approx(110.68, abs=0.01)  # last idle; the answer came at 35.71

def test_monitor_task_does_not_hold_the_run():
    settle, _ = _replay(CLAUDE_SETTLE_BASH_MONITOR)
    assert settle.running_tasks() == []  # the Monitor task is still in the live set here

def test_subagent_run_is_done_at_the_answer_after_the_idle():
    settle, checks = _replay(CLAUDE_SETTLE_SUBAGENT)
    assert next(t for t, done in checks if done) == pytest.approx(116.11, abs=0.01)

def test_wake_after_the_answer_reports_the_ended_task():
    # Bash fixture: the only idle->running after the answer (the Monitor event, t 45.02)
    # returns []; the later wake turns run back to back, so apply returns None for them.
    # Synthetic: answer, idle, t1 leaves the level, then running -> returns ["<t1 description>"].

def test_task_ending_while_idle_waits_for_the_wake():
    # Synthetic: answer, idle, task t1 live; level [] at 10.0; done(10.04) is False;
    # running at 10.044, idle at 12.0 -> done(12.0) is True.

def test_task_ending_while_idle_with_no_wake_is_done_after_15s():
    # Same without the running: done(24.9) False, done(25.1) True (UNWOKEN_END_S after 10.0).

def test_a_task_from_an_earlier_run_does_not_hold():
    # Level lists t-old (no task_started in this run), state idle, answered -> done.

def test_monitor_named_after_its_task_started():
    # task_started for t2 with tool_use_id c2 arrives while is_monitor_call("c2") is False;
    # it becomes True before the check -> done.

def test_requires_action_is_not_done():
    # answered, no live tasks, last state requires_action -> not done.

def test_ambient_task_does_not_hold():
    # live task of this run marked ambient -> done.

def test_no_state_message_means_not_reporting():
    # Only task_started and level messages -> reporting is False.

def test_usage_sums_results_not_the_answer():
    settle, _ = _replay(CLAUDE_SETTLE_SUBAGENT)
    counts = settle.usage_counts()
    assert counts["outputTokens"] == 173 + 11 + 0
    assert counts["totalTokens"] == sum(counts[k] for k in ("inputTokens", "outputTokens", "cachedReadTokens", "cachedWriteTokens"))

def test_wake_marker_text():
    assert wake_marker("Claude Code", ["Sleep 40s then print marker"]) == "[Claude Code resumed on its own: Sleep 40s then print marker ended]"
    assert wake_marker("Claude Code", []) == "[Claude Code resumed on its own: a background event]"
```

Fill in the bodies of the synthetic tests with the exact times in their comments. The two fixture times above (110.68, 116.11) are the fixture's own: check them against the generated fixture and correct the literal if the extraction rounds differently, never the rule.

- [ ] **Step 3: Run the tests to see them fail**

Run: `uv run pytest tests/test_acp_background_settle.py -q`
Expected: collection error, `ModuleNotFoundError: raven.acp_client.background_settle`.

- [ ] **Step 4: Implement `background_settle.py`**

The rule (spec Design 3), with `own` = tasks whose `task_started` arrived after construction, keyed by `task_id` with their `tool_use_id` and `description`:

```
done(now) =
    answered
    and state == "idle"
    and not any(t in live for t in own if not monitor(t) and not ambient(t))
    and (last_end is None or last_idle > last_end
         or (now - last_end > UNWOKEN_END_S and last_running <= last_end))
```

`last_end` is updated when a task of `own` (Monitor and ambient excepted) leaves `live` between two `background_tasks_changed`. `apply` returns the wake list only for a `running` whose previous state was not `running` and that comes after `prompt_answered`. The module docstring states what a settle is and that it is fed by `emitRawSDKMessages`.

- [ ] **Step 5: Run the tests to see them pass**

Run: `uv run pytest tests/test_acp_background_settle.py -q`
Expected: all pass.

- [ ] **Step 6: Report** (commit only on the owner's word)

```bash
git add raven/acp_client/background_settle.py tests/acp_frames.py tests/test_acp_background_settle.py
git commit -m "feat(acp_client): read claude code background work from its raw sdk messages"
```

---

### Task 2: Opt in at session open

**Files:**
- Modify: `raven/acp_client/acp_dialects/base.py` (`AcpDialect`)
- Modify: `raven/acp_client/acp_dialects/claude_code.py` (`ClaudeCodeDialect`)
- Modify: `raven/acp_client/acp_agent.py` (`_open_session` and its one caller in `run`)
- Modify: `tests/acp_stub_server.py` (log the `_meta` of session opens)
- Test: `tests/test_acp_dialects.py`, `tests/test_acp_background_settle.py`

**Interfaces:**
- Consumes: `RAW_MESSAGE_FILTER` (Task 1).
- Produces:
  - `AcpDialect.session_open_meta(self) -> dict[str, Any] | None` - `None` in the base.
  - `ClaudeCodeDialect.session_open_meta()` -> `{"claudeCode": {"emitRawSDKMessages": RAW_MESSAGE_FILTER}}` (a fresh copy per call).
  - `AcpAgentBackend._open_session(..., session_meta: dict[str, Any] | None = None)` - when not `None`, the params of `session/new`, `session/load` and `session/resume` all carry `"_meta": session_meta`.
  - Stub: on `session/new`, `session/load` and `session/resume`, when the params carry `_meta`, write `stub: <method> meta=<json.dumps(_meta, sort_keys=True)>` to stderr.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_acp_dialects.py
def test_only_claude_code_opts_into_raw_sdk_messages():
    assert ClaudeCodeDialect().session_open_meta() == {"claudeCode": {"emitRawSDKMessages": RAW_MESSAGE_FILTER}}
    assert AcpDialect().session_open_meta() is None
    assert CodexDialect().session_open_meta() is None
```

In `tests/test_acp_background_settle.py`, add the stub helpers this and later tasks use: `_settle_backend(tmp_path, scenario, **env)` builds a backend with `build_third_party_backend` from a `ThirdPartyAcpSubagentConfig` pointing at `tests/acp_stub_server.py`, `env={"ACP_STUB_MODE": "settle", "ACP_STUB_SETTLE": scenario, "ACP_STUB_AGENT_NAME": "@agentclientprotocol/claude-agent-acp", **env}`, plus the autouse fixture that clears `_SESSION_STATE` and awaits `close_pool()` (as `tests/test_subagent_acp.py` does). Then:

```python
async def test_new_load_and_resume_carry_the_same_meta(tmp_path):
    # Run twice on one instance handle (stateful stub): the first run opens with
    # session/new, the second reopens with session/load. Both stderr lines carry
    # meta={"claudeCode": {"emitRawSDKMessages": [...]}} with identical JSON.
    # Repeat with ACP_STUB_SETTLE=plain and mode resume_only behaviour for session/resume
    # if the stub's settle mode can combine with it; otherwise call _open_session
    # directly on a fake client that records params.

async def test_another_agent_sends_no_meta(tmp_path):
    # ACP_STUB_AGENT_NAME left as stub-agent: no "meta=" line in stderr.
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/test_acp_dialects.py tests/test_acp_background_settle.py -q -k "meta or raw_sdk"`
Expected: FAIL (`AttributeError: session_open_meta`, and no `meta=` line).

- [ ] **Step 3: Implement**

The dialect hook as above. In `run`, compute `session_meta = dialect_for(connection.initialize).session_open_meta()` and pass it to `_open_session`; there, add it to the `reopen` dict and to the `session/new` params. The stub gets mode `settle` (for now it behaves like `ok`) and the stderr line.

- [ ] **Step 4: Run to see them pass**

Run: `uv run pytest tests/test_acp_dialects.py tests/test_acp_background_settle.py -q`
Expected: all pass.

- [ ] **Step 5: Report** (commit only on the owner's word)

```bash
git add raven/acp_client/acp_dialects/base.py raven/acp_client/acp_dialects/claude_code.py raven/acp_client/acp_agent.py tests/acp_stub_server.py tests/test_acp_dialects.py tests/test_acp_background_settle.py
git commit -m "feat(acp_client): ask claude code for its raw sdk messages on every session open"
```

---

### Task 3: The collector reads the stream

**Files:**
- Modify: `raven/acp_client/acp_agent.py` (`_TurnCollector`)
- Test: `tests/test_acp_background_settle.py`

**Interfaces:**
- Consumes: `BackgroundSettle`, `wake_marker`, `RAW_MESSAGE_METHOD` (Task 1).
- Produces on `_TurnCollector`:
  - constructor keyword `agent: str = ""` (the run passes `self.name`), used for the marker.
  - `settle: BackgroundSettle` - built in the constructor; `is_monitor_call(call_id)` is true when an event of `self.events` with `t == "call"` and that id has `call.name == "Monitor"`.
  - `settle_changed: asyncio.Event` - set on every raw message.
  - `prompt_answered() -> None` - forwards to `settle.prompt_answered()`.
  - On a raw `result` after `prompt_answered`: set `_tool_ran = True`, so each wake turn's text is its own message (wake turns queued back to back share one `running` and get no marker of their own).
  - On a raw message whose `settle.apply` returns a list: append `{"t": "user", "text": wake_marker(agent, ended), "at": now}` to the events, set `_tool_ran = True` (so the wake's first words start a new message, as after a tool call), republish the transcript (`activity.set_transcript`).
  - `turn_counts(answered)` returns `settle.usage_counts()` when that is not `None`, else today's result.
  - Every raw message calls `activity.note_alive(self._run)`.

- [ ] **Step 1: Write the failing tests**

Feed a `_TurnCollector(on_delta=sink, dialect=ClaudeCodeDialect(), agent="Claude Code")` the bash fixture (session/update frames as `__call__("session/update", {"update": ...})`, raw frames as `__call__(RAW_MESSAGE_METHOD, params)`, `"answer"` as `prompt_answered()`):

```python
async def test_collector_reply_is_the_last_wake_turn():
    assert collector.reply == "final: BG_DONE"
    assert collector.closing_text == "final: BG_DONE"

async def test_each_wake_turn_gets_a_marker_row():
    users = [e["text"] for e in collector.events if e["t"] == "user"]
    assert users == ["[Claude Code resumed on its own: a background event]"]

async def test_a_speaking_wake_is_not_glued_to_the_text_before_it():
    assert "started\n\nevent: PING_1\n\nevent: PING_2" in "".join(seen)  # marker and result breaks

async def test_turn_counts_come_from_the_raw_results():
    assert collector.turn_counts({"outputTokens": 999})["outputTokens"] == 249 + 8 + 8 + 169

async def test_a_raw_message_before_any_answer_adds_no_marker():
    # subagent fixture: no "user" event at all.
```

Correct the two marker lists and the token sum against the fixture if it differs; they come from it.

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/test_acp_background_settle.py -q -k collector or marker or glued or turn_counts`
Expected: FAIL (`TypeError` on `agent=`, then assertion errors).

- [ ] **Step 3: Implement** as in Interfaces. The raw-message branch goes before the `session/update` parsing in `__call__`, next to the permission branch.

- [ ] **Step 4: Run to see them pass**

Run: `uv run pytest tests/test_acp_background_settle.py tests/test_subagent_acp.py -q -k "background_settle or two_messages or reply or closing"`
Expected: all pass, the existing reply tests unchanged.

- [ ] **Step 5: Report** (commit only on the owner's word)

```bash
git add raven/acp_client/acp_agent.py tests/test_acp_background_settle.py
git commit -m "feat(acp_client): mark claude code wake turns and count their usage"
```

---

### Task 4: The run waits for the settle

**Files:**
- Modify: `raven/acp_client/acp_agent.py` (`AcpAgentBackend.run`, new `AcpAgentBackend._settle`)
- Modify: `tests/acp_stub_server.py` (the `settle` scenarios)
- Modify: `CONTEXT.md` (term **Background Settle**, after **Unprompted Turn**)
- Test: `tests/test_acp_background_settle.py`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces:
  - `AcpAgentBackend._settle(self, *, client: Any, session_id: str, collector: _TurnCollector, prompt_sent_at: float, deadline_s: float | None, remote: bool) -> str | None` - returns `None` when the work ended (or the run is not reporting), else the notice text without the `[raven] ` prefix and the trailing period (the caller's notes loop adds both).
  - Notice texts (exact):
    - ceiling: `f"{name} still had background work running after {int(SETTLE_CEILING_S // 60)} min: {tasks}. Anything it reports later arrives as its own report"`
    - deadline: `f"{name} still had background work running at its deadline: {tasks}. Anything it reports later arrives as its own report"`
    - connection: `f"the connection to {name} closed before its background work ended"`
    where `tasks = "; ".join(collector.settle.running_tasks())`.
  - Stub scenarios (`ACP_STUB_SETTLE`), each emitting raw messages as `_claude/sdkMessage` notifications from a thread with the given delays, after the prompt answer unless noted:
    - `bash`: before the answer: `running`, `task_started` t1 (tool_use_id c1, "sleep job"), level [t1], text "started", `result`; answer `end_turn`; `idle`. Then +0.3 s: level [], `running`, text "final: done", `result`, `idle`.
    - `bash_monitor`: as `bash`, plus a Monitor tool_call c2 and its task t2 that never leaves the level.
    - `idle_end`: as `bash`, but the level [] comes while idle and `running` follows 0.5 s later.
    - `idle_end_nowake`: as `idle_end` with no `running` after the level [].
    - `forever`: t1 never ends; state `running` after the answer.
    - `no_state`: no raw message at all; text "pong"; answer.
    - `dies`: as `forever`, then the process exits 0.5 s after the answer.

- [ ] **Step 1: Write the failing tests**

```python
async def test_bash_run_returns_the_final_answer_once(tmp_path):
    out = await _settle_backend(tmp_path, "bash").run("go", task_id="t", workspace=tmp_path, executor=None)
    assert out == "final: done"
    # and the session's instance log holds no `unprompted` turn (instance_log read as in
    # tests/test_acp_unprompted.py)

async def test_monitor_does_not_hold_the_run(tmp_path):   # bash_monitor -> returns "final: done" within 5 s

async def test_task_ending_while_idle_waits_for_the_wake(tmp_path):   # idle_end -> "final: done"

async def test_unwoken_task_end_returns_after_the_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(background_settle, "UNWOKEN_END_S", 0.5)   # idle_end_nowake -> "started"

async def test_no_state_message_returns_on_the_answer(tmp_path):   # no_state -> "pong" within 2 s

async def test_ceiling_returns_with_a_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(background_settle, "SETTLE_CEILING_S", 1.0)
    # forever -> reply ends with "[raven] <name> still had background work running after 0 min: sleep job. Anything it reports later arrives as its own report."

async def test_deadline_bounds_prompt_and_settle_together(tmp_path):
    # backend config timeout=2; forever -> returns within 3 s with the "at its deadline" notice, no exception

async def test_connection_lost_during_settle_returns_the_reply(tmp_path):   # dies -> "started" + connection notice

async def test_cancel_during_settle_sends_cancel_and_keeps_the_binding(tmp_path):
    # forever on a stateful instance; cancel the run task 0.5 s after the answer;
    # CancelledError propagates; stub stderr shows the cancel (log it in handle_cancel for
    # the settle mode); backend._registry.lookup(...) returns the session id.

async def test_quiet_background_work_stamps_the_run_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(background_settle, "ALIVE_EVERY_S", 0.2)
    # forever + ceiling 1.0: activity.note_alive called >= 3 times during the settle (spy).

async def test_another_agent_returns_on_the_answer(tmp_path):
    # scenario forever with ACP_STUB_AGENT_NAME=stub-agent -> returns at the answer.
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/test_acp_background_settle.py -q`
Expected: the new tests FAIL (the run returns at the answer).

- [ ] **Step 3: Implement `_settle` and call it from `run`**

- In `run`, record `prompt_sent_at = time.monotonic()` just before `client.request("session/prompt", ...)`, and pass `agent=self.name` to `_TurnCollector`.
- Call `settle_notice = await self._settle(...)` inside the `try`, right after `answered = True`. A `CancelledError` from it then takes the existing `except asyncio.CancelledError` branch, which already commits a first turn's binding and writes frames and steps. `deadline_s` is `_prompt_deadline(self.timeout)`, measured from `prompt_sent_at`.
- Add `settle_notice` to the `notes` list next to `self._mcp_note(grant, lent)`.
- `_settle`: call `collector.prompt_answered()`; return `None` at once unless `collector.settle.reporting`; then loop: return `None` when `settle.done(now)`; return the connection notice when `client.closed` or not `client.alive`; return the ceiling or deadline notice when either budget is spent; call `activity.note_alive(collector._run)` every `ALIVE_EVERY_S` while `settle.running_tasks()` is non-empty; wait on `collector.settle_changed` for at most `min(1.0, budget left)` and clear it. On `CancelledError`: when `settle.state == "running"` and not `(remote and remote_mod.leaving())`, send `client.notify("session/cancel", {"sessionId": session_id})` (errors swallowed), then re-raise. Log one line on entering and one on leaving the settle.
- Add the **Background Settle** entry to `CONTEXT.md` with the text from the spec's "Term" section.

- [ ] **Step 4: Run to see them pass**

Run: `uv run pytest tests/test_acp_background_settle.py -q`
Expected: all pass.

- [ ] **Step 5: Run the neighbouring suites**

Run: `uv run pytest tests/test_subagent_acp.py tests/test_acp_unprompted.py tests/test_acp_dialects.py tests/test_subagent_acp_remote.py tests/test_acp_elicitation.py tests/test_acp_ask_user.py -q -p no:randomly`
Expected: same pass/fail set as on `origin/main` at `5aa749f36` (record the baseline first with the same command in the main checkout).

- [ ] **Step 6: Gates**

Run: `uv run ruff check raven/acp_client tests/test_acp_background_settle.py && uv run ruff format --check raven/acp_client tests/test_acp_background_settle.py && make check-source-language`
Expected: exit 0.

- [ ] **Step 7: Report** (commit only on the owner's word)

```bash
git add raven/acp_client/acp_agent.py tests/acp_stub_server.py tests/test_acp_background_settle.py CONTEXT.md docs/specs/2026-11-24-claude-code-background-settle-design.md docs/plans/2026-11-24-claude-code-background-settle.md
git commit -m "feat(acp_client): end a claude code run when its background work ends"
```

---

## Acceptance outside the test suite

After Task 4, one live run against the real adapter: spawn the `Claude Code` agent through a scratch Raven config with the bash-plus-Monitor prompt from the probe, on `sonnet` and effort `low`. Pass when the spawn reports once, with `final: BG_DONE`, and no Unprompted Turn reaches the host before the Monitor expires. Expected cost about 1 USD. Ask the owner before running it, and run it without user-level Claude Code hooks, which otherwise record the probe prompts.
