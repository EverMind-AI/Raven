# Claude Code runs end when their background work ends - design

Date: 2026-11-24
Status: approved by the owner 2026-11-25
Base: `origin/main` at `5aa749f36`

## Problem

A Claude Code dispatch (spawn, DAG node, direct chat) is reported as finished
when `session/prompt` answers. For Claude Code that answer does not mean the
work is over. When the agent starts a command with `run_in_background` and then
ends its model turn, the adapter answers the prompt at once. The command goes
on, Claude Code wakes the model again when it ends, and each of those later
turns reaches the host as a separate Unprompted Turn report.

Incident, 2026-11-23 UTC, instance `claude-code-1373-979836` (PR #1373 review):

| Time | Event |
|---|---|
| 16:02:24 | Agent starts a mutation run in the background (task `b8pydmz1h`) and a Monitor (`b1fwp07cx`) |
| 16:07:45 | Prompt answers `end_turn`; Raven reports the spawn as done, with the text "M1-M5 done, M6-M14 need about 20 more minutes" |
| 16:07:57 - 16:23:03 | Four Unprompted Turn reports reach the host; the last one (11 KB) is the real review, delayed by the 300 s debounce |
| 16:14:56 | The mutation run ends (`<task-notification>` in the Claude Code transcript) |
| 16:18:04 | The agent finishes the review |

The owner wants one dispatch to give one result: the run ends when the work it
started has ended.

## Measured facts

Source read: `@agentclientprotocol/claude-agent-acp` 0.108.0 (the pinned
version, SDK 0.3.326) and 0.110.1 (SDK 0.3.330). Live probe on 2026-11-24
against 0.108.0, sonnet, effort low, two sessions on one connection. The
frames the tests replay are copied from that probe (see Tests).

1. `session/new`, `session/load` and `session/resume` accept
   `_meta.claudeCode.emitRawSDKMessages`: `true`, or a list of filters
   `{type, subtype?, origin?}`. The adapter then sends each matching SDK message
   as the notification `_claude/sdkMessage` with `{sessionId, message}`. The
   option is not limited to AIR clients. It is part of the adapter's session
   fingerprint: a later load with a different value rebuilds the session.
2. `system/background_tasks_changed` carries the full set of live background
   tasks (replace semantics). Tasks that a subagent starts are in it too.
3. `system/session_state_changed` carries `idle`, `running` or
   `requires_action`.
4. Background command: the prompt answered at 40.7 s and `idle` followed at
   once, while `sleep 40` ran on. At 66.8 s the task ended in the order
   `task_updated`, `task_notification`, `background_tasks_changed`. Three queued
   wake turns then ran back to back with no `idle` between them. The final
   line came at 111.9 s and `idle` at 115.6 s.
5. Background subagent: the adapter held the prompt until the woken main turn
   had said its final line (544.2 s), and the CLI sent no `idle` while the
   subagent ran. This path is already correct today.
6. A Monitor task is not marked `ambient`. It is `task_type: local_bash`,
   `is_backgrounded: true`, the same as a real background command. Only
   `task_started.tool_use_id` tells them apart: it names the ACP tool call
   whose `_meta.claudeCode.toolName` is `Monitor`. The adapter separates them
   the same way.
7. A Monitor that outlives the work still wakes the model: on expiry it is
   killed, `task_notification` reports `stopped`, and the model says one more
   line. Monitor events produce no `task_*` message, only `running`.
8. A task can end while the session is idle. Then the set empties first and
   `running` follows: measured 44 ms later (627.65 s and 627.69 s, a Monitor
   expiry). For that short gap the session reads idle with nothing live.
9. Each turn ends with a raw `result` whose `usage` counts that turn only. The
   prompt answer's `usage` equals the first `result` of the run. In the subagent
   case the woken main turn's `result` arrived before the prompt answer, and a
   zero-usage `result` came after it.
10. Scheduled wakes (`ScheduleWakeup`, `CronCreate`) are not in the stream. They
   appear only in the Stop hook input (`session_crons`).

## Intent

A dispatch to Claude Code returns once, with the agent's final answer, after the
background work it started has ended. Progress keeps streaming to the pane while
it waits, as it does during the prompt.

## Goals

- A Claude Code run returns when the prompt has answered and the session is idle
  with no background work left, Monitor tasks excepted.
- The returned reply is the agent's last message, after its last wake turn.
- No Unprompted Turn report is produced for wake turns inside that wait.
- Questions and permission requests asked during the wait reach the user as
  they do during a prompt.
- The usage ledger counts the wake turns inside the wait.

## Non-goals

- Agents other than claude-agent-acp. Their runs end on the prompt answer, as
  today.
- The Mirasim shim (`raven.acp.mirasim`, agent "Claude Code-Mirasim"). It does
  not run claude-agent-acp and shows the same symptom through a different stack;
  it needs its own probe of what the Mirasim host reports.
- Monitor activity after the run has returned. Owner's decision: its events and
  its expiry keep going through the Unprompted Turn path unchanged.
- Scheduled wakes (`ScheduleWakeup`, `CronCreate`). A run does not wait for them.
- Stopping background tasks that are still running when the run returns.

## Anti-goals

- A run that never returns. The wait always has a ceiling.
- A guess from silence where the stream gives a fact. The quiet-period fallback
  of the Unprompted Turn recorder is not reused here; the only quiet period
  (rule condition 4) applies after a task end that no wake turn follows.
- Claiming to be a JetBrains AIR client to get task updates.

## Constraints

- AGENTS.md section 6: the new concept is a domain term and goes into
  `CONTEXT.md` (see "Term" below).
- The `_meta` value must be identical on `session/new`, `session/load` and
  `session/resume`, or the adapter rebuilds a loaded session (fact 1).
- Remote machines must keep working: the notifications travel on the same ACP
  connection, so no file on this computer is read.
- DAG and spawn timing changes need the owner's sign-off. The owner signed off
  on this design, both DAG behaviours included, on 2026-11-25.

## Approaches considered

| | Approach | Why not chosen |
|---|---|---|
| **A** | **Subscribe to the raw SDK messages (chosen)** | - |
| B | Scan the Claude Code transcript for open background tasks | Private file format; does not work for a run on another machine |
| C | Declare the AIR `asyncTasks` capability | Impersonates JetBrains AIR and changes other adapter behaviour with it |
| D | Inject a Stop hook that reports `background_tasks` | Writes into the user's Claude Code configuration |
| E | Wait for a quiet period after the prompt | Fails for background work that prints nothing for a long time |
| F | Tell the agent in the prompt not to end its turn early | Unreliable; conflicts with Claude Code's own "do not poll or sleep" rule |

The owner chose A on 2026-11-24.

## Design

### 1. Opt in at session open

`AcpDialect` gets a hook that returns the `_meta` a session open carries. The
spec dialect returns nothing. `ClaudeCodeDialect` returns:

```json
{"claudeCode": {"emitRawSDKMessages": [
  {"type": "system", "subtype": "background_tasks_changed"},
  {"type": "system", "subtype": "session_state_changed"},
  {"type": "system", "subtype": "task_started"},
  {"type": "result"}
]}}
```

`AcpAgentBackend._open_session` sends it on `session/new`, `session/load` and
`session/resume` alike. A session opened before this change is rebuilt once by
the adapter on its first load with the new value. That is accepted: it costs one
CLI restart with resume, not the conversation.

### 2. Read the stream in the turn collector

The session router already routes any notification by `params.sessionId`, so
`_claude/sdkMessage` reaches the run's `_TurnCollector`. Today the collector
ignores every method except `session/update` and the permission request. It
starts to keep, for its session:

- `state`: the last `session_state_changed` value, and whether one was seen in
  this run at all;
- `own`: the tasks whose `task_started` arrived during this run, with their
  `tool_use_id`. Tasks that a subagent of this run starts are included, because
  their `task_started` also arrives during the run. Tasks left by an earlier run
  are not: this run does not wait for them;
- `live`: the last `background_tasks_changed` set;
- `monitors`: the tasks in `own` whose `tool_use_id` names a call that the
  turn's events recorded under the tool name `Monitor`;
- `last_end`: when a task in `own` last left `live`;
- `last_idle`: when `idle` last arrived;
- `results`: the `usage` of every raw `result` of this run.

The Unprompted Turn recorder keeps ignoring `_claude/sdkMessage`. It sees the
session only after the run has detached, which is the current boundary.

### 3. The rule

The run's work is over when all of these are true:

1. the prompt has answered;
2. the last state is `idle`;
3. no task of `own` is in `live`, Monitor tasks and `ambient` tasks excepted;
4. `last_idle` is later than `last_end`, or `last_end` is more than 15 s ago
   and no `running` came after it.

Condition 4 covers fact 8: when the last task ends while the session is idle,
the run waits for the wake turn that follows, and the 15 s bound covers a task
whose end wakes nobody. The rule is checked when the prompt answers, on every
raw message after it, and on a timer while condition 4 waits. `requires_action`
is not idle: a pending question keeps the run open. Facts 4 and 5 show that the
last `idle` lands after the last wake turn's final line.

### 4. Background Settle

When the prompt answers and the rule is not yet true, the run enters Background
Settle instead of returning:

- It stays inside the session lock, so another prompt to the same session waits,
  as it would behind a running prompt.
- Everything the run attached stays attached: the collector (so text keeps
  streaming to the pane through `on_delta` and the live transcript), the
  elicitor, the ask_user responder and the steer offer. A steer sent while no
  wake turn runs is answered `no_turn`, as the agent reports it.
- Each wake turn starts a new message: a `running` after the prompt answered
  is a message break, as a tool call is today, and so is each raw `result`
  after it (wake turns queued back to back share one `running`), so a wake
  turn that only speaks is not glued onto the text before it. The transcript gets a `user` row before
  the wake turn, a stated fact in the manner of the Unprompted Turn marker:
  `[Claude Code resumed on its own: <description> ended]`, naming the tasks of
  `own` that left `live` since the last turn, or `a background event` when none
  did (a Monitor event).
- While a task of `own` (Monitor tasks excepted) is live, the run is stamped
  alive once a minute (`activity.note_alive`), so a quiet background command
  does not trip the DAG stall notice (`STALL_NOTICE_S`). The ceiling bounds it.
- When the rule becomes true, the run continues exactly where it continues
  today after the prompt answers: frames, steps, usage, the empty-turn checks,
  the first-turn binding and the reply. The reply is `collector.reply`, which is
  the last message, so it is the agent's final answer.
- The frame range recorded for the call extends to the end of the settle.

Settle is entered only if at least one `session_state_changed` arrived in this
run. If none did (an older CLI, or the option was dropped), the run returns on
the prompt answer as today.

### 5. Bounds and interruptions

- Ceiling: a module constant, 3600 s from the prompt answer. When
  `_prompt_deadline` gives a deadline (the brief's, or the row's `timeout`), it
  bounds the prompt and the settle together, measured from the prompt send. At
  the ceiling or the deadline the run returns successfully with the reply it
  has, not with a timeout error: the agent did answer, and failing would throw
  that answer away. A closing notice names what was still running, for
  example:
  `[raven] Claude Code still had background work running after 60 min: Sleep 40s then print marker. Anything it reports later arrives as its own report.`
  Later frames then reach the Unprompted Turn path, as today.
- Connection lost during settle: the run returns the reply it has, with a notice
  that the connection closed before the background work ended.
- Cancel during settle (stop button, `/stop`, shutdown): send `session/cancel`
  if the last state is `running`, do not wait for it, and propagate the cancel.
  The exception is the one the prompt path already makes: a run on a registered
  machine while this process is leaving (`remote.leaving()`) sends no cancel and
  keeps its record, as `mark_left` does today. Its pick-up after a restart
  returns the answered prompt's reply, as today; the settle is not resumed, and
  later wake turns reach the Unprompted Turn path.
  Whatever the answered prompt established must not be lost: the first-turn
  binding, the frames, the steps and the usage booked so far. Background tasks
  are not stopped; ACP gives a non-AIR client no way to stop them.

### 6. Usage

When the run saw at least one raw `result`, the ledger books the sum of their
`usage` and does not read the prompt answer's counts (fact 9: the answer equals
the first result, and a woken turn's result can arrive before the answer). When
it saw none, the answer's counts are booked as today. The cost stays as today: `usage_update.cost` is cumulative for the
session, so the difference taken at the end of the settle includes the wake
turns.

### 7. What the user sees

| Before | After |
|---|---|
| Spawn reported done at the prompt answer, with a progress sentence as its result | Spawn reported done once, with the final answer as its result |
| Later turns arrive as Unprompted Turn reports, 300 s apart | Later turns stream into the same run's pane |
| A DAG node completes before its background work ends | The node completes after it |

A Monitor that outlives the run still produces Unprompted Turn reports for its
events and its expiry (owner's decision).

## Term

Add to `CONTEXT.md`:

**Background Settle** (`raven/acp_client/acp_agent.py`): the part of an ACP
Subagent run between the prompt answer and the end of the background work the
turn started, for an agent whose stream reports that work (claude-agent-acp,
through `emitRawSDKMessages`). The run keeps its session and its sinks, streams
the wake turns, and returns the last message once the session is idle with no
background task left, Monitor tasks excepted, or at its ceiling.
_Avoid_: "held wake" - that is `raven.holdTurn`, where a Raven agent keeps its
own prompt open on the server side; "hold" alone, for the same reason.

## Tests

The probe frames are copied verbatim into a test fixture, in the manner of
`tests/acp_frames.py`. The stub ACP server gets a mode that replays them on
cue. Cases:

1. Background command: the run returns after the last `idle`, with the final
   line as its reply, and no Unprompted Turn is recorded.
1b. The last task ends while idle (fact 8): the run does not return in the gap
   before `running`, and does return 15 s later when no wake follows.
1c. A task left live by an earlier run does not hold this run open.
1d. A wake turn that only speaks becomes its own message, after the marker row.
2. Background command plus Monitor: the Monitor does not keep the run open.
3. Background subagent: unchanged result (the prompt already waits).
4. No `session_state_changed` in the run: the run returns on the prompt answer.
5. Ceiling reached: the reply carries the notice naming the running task.
6. Cancel during settle: `session/cancel` sent when running, the cancel
   propagates, the first-turn binding is committed.
7. `requires_action` during settle: the question reaches the elicitor; the run
   stays open until `idle`.
8. Usage: booked counts equal the sum of the raw results, including one that
   arrives before the prompt answer.
9. `_meta` is the same on `session/new`, `session/load` and `session/resume`.
10. Another dialect: no `_meta` is sent and no settle happens.

## Risks and rollback

- Spawns and DAG nodes that use Claude Code take longer to report, by the
  length of their background work, up to one hour.
- A direct chat or a second spawn to the same instance waits behind a settling
  run.
- Each existing Claude Code session is rebuilt once by the adapter at its first
  load after the change.
- The pool's idle close (`idleCloseS`) treats a connection as in use while a run
  holds one of its sessions, so a background command that prints nothing for
  longer than the idle budget does not end the settle early. Background work
  still running after the run returns is not protected; the idle close can end
  it, as before.
- Rollback: make the dialect hook return nothing. No `session_state_changed`
  then arrives, and every run returns on the prompt answer as today.

## Decisions taken with the owner

- Approach A, the raw SDK messages (2026-11-24).
- A live probe before the design (2026-11-24; cost 1.78 USD).
- Monitor activity after the run returns: no special handling (option a).
- The design, with both DAG behaviours, approved (2026-11-25).

## Calls made by the author

- The ceiling is 3600 s and a constant, not a configuration key. A key can come
  later if a real run needs another value.
- Scheduled wakes do not keep a run open: the stream does not report them, and
  a wake can be hours away.
- The Mirasim shim is out of scope (see Non-goals).
- Everything the run attached stays attached during the settle, the steer offer
  included.
- Wake-turn token counts come from the raw `result` messages, summed over the run.
- A run waits only for tasks it started (including its subagents' tasks), not
  for tasks an earlier run left running.
- Condition 4's bound is 15 s.
- Wake turns get a marker row and a message break.
- At the ceiling or the deadline the run succeeds with a notice. For a DAG node
  this means the node completes, not fails. Signed off by the owner.
- A run with live background work is stamped alive once a minute, so the DAG
  stall notice does not fire during the settle. Signed off by the owner.
- A remote run left behind by a host shutdown during the settle is picked up as
  today, without the settle.

## Left to the builder

- How `AcpAgentBackend.run` is restructured so that the settle sits inside the
  session lock and before the teardown, and how the cancel during settle is kept
  apart from the cancel of an in-flight prompt.
- The shape of the fixture and of the stub server mode.
- Log lines for entering and leaving the settle.
