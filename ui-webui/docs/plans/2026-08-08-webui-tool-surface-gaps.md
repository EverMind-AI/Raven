# Web UI Tool Surface Repair Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every tool the gateway `web` channel already exposes actually work on that channel: interactive tools can interact, produced media arrives, failures look like failures, call arguments are readable, and stop stops.

**Architecture:** Three layers, changed in that order per feature. The Raven core owns the spine events and the question round trip (`raven/tui_rpc/spine.py`, `raven/cli/gateway_commands.py`, `raven/agent/tools/`). The service translates spine events into AgentScope events and owns the REST surface the browser talks to (`ui-webui/service/`). The frontend renders events and raises interaction cards (`ui-webui/frontend/src/components/chat/`). Nothing here invents a new transport: the question round trip rides the existing `custom` event lane, media rides the existing deliverables download route, and the answer rides a new REST route that deliberately bypasses the chat front door.

**Tech Stack:** Python 3.12+ (`uv` / `pytest`, `asyncio_mode = "auto"`), aiohttp (gateway WS + HTTP), FastAPI (service), React 19 + TypeScript + Vite + i18next (frontend, `pnpm`).

**Specs:** `ui-webui/docs/specs/2026-08-08-webui-tool-surface-gaps-design.md` (tool surface, gaps `G*`) and `ui-webui/docs/specs/2026-08-08-webui-chat-interaction-design.md` (turn lifecycle, gaps `I*`). Phase 2 below implements the stop work from both, which is one change spanning the two specs.

## Global Constraints

- **Read `AGENTS.md` (repo root) before the first edit.** Its rules override anything here that conflicts.
- **Do not commit until the user says so** (`AGENTS.md` section 3.4). The commit step at the end of each task is the *intended* commit boundary, not authorization. Ask once, then honor the answer for the run.
- **Confirm the branch base with the user before cutting a branch** (`AGENTS.md` section 2.2). Base is `origin/main` per `.claude/skills/updating-raven-code`. Suggested branch: `fix/webui_tool_surface_gaps`. This plan is large enough that splitting it across several branches is reasonable - see "Suggested delivery split" at the end - but never split one phase across two merge requests.
- **Commit messages: Conventional Commits, all English, ASCII-only** (`AGENTS.md` sections 3.1, 3.1.1). No em-dash, no curly quotes. Trailer `Co-authored-by: Claude (<model-id>) <noreply@anthropic.com>`.
- **Code comments: English, and only where the logic is non-obvious** (`AGENTS.md` section 1). Do not annotate edits.
- **Python packages: `uv` only** (`AGENTS.md` section 4). No new dependency is needed by this plan.
- **Run tests as `uv run pytest ...`, never bare `pytest`.**
- **Test file naming per `AGENTS.md` section 5.** Add to the existing `tests/test_ask_user_tool.py`, `tests/test_cli_gateway_commands.py`, `tests/test_web_rpc_*.py` rather than creating phase-suffixed files.
- **Frontend gate (no JS unit-test runner):** `pnpm -C frontend lint` must report **0 errors** and `pnpm -C frontend build` must pass, run from `ui-webui/`.
- **Frontend formatting is Prettier with tabs, width 4, single quotes, semicolons, print width 100.** Match it; a stray reformat is diff noise.
- **i18n JSON: targeted text edits only.** Never rewrite `en.json` / `zh.json` wholesale.
- **The outlet is shared with the TUI.** `raven/tui_rpc/spine.py` feeds both surfaces. Any new event type must be ignorable by the TUI client *before* the gateway starts emitting it.
- **Broker invariant: a question always resolves to a string.** Every fail-safe path in `QuestionBroker` returns the default rather than raising, and no change here may introduce a path that raises into the agent loop.
- **Router invariant: an unknown channel falls back to the gateway broker, never to `None`.** `None` makes `ask_user` answer "not configured" instead of asking.

---

## File Structure

**New - Raven core**

| Path | Responsibility |
|---|---|
| `raven/tui_rpc/question_router.py` | `QuestionRouter`: holds one broker per channel plus a default, exposes the `await_question` / `pending_req` / `reply` / `cancel_all` surface the tools already use. No transport knowledge. |

**New - tests**

`tests/test_question_router.py`, `tests/test_web_rpc_clarify.py`, `tests/test_spine_media_events.py`

**New - frontend**

| Path | Responsibility |
|---|---|
| `ui-webui/frontend/src/components/chat/ClarifyCard.tsx` | The question card: prompt, optional choices, free-form input, submit. |
| `ui-webui/frontend/src/components/chat/ClarifyContext.tsx` | Pending-question state per session, fed by the `clarify_request` custom event, cleared on answer / turn end. |

**Modified - Raven core**

| Path | Change |
|---|---|
| `raven/agent/tools/ask_user.py` | `set_context(conversation_id, channel)`; hold a router |
| `raven/agent/tools/deep_research.py` | pass `routing.channel` when asking |
| `raven/agent/loop/main.py` | pass `channel` into `ask_tool.set_context`; router type on `set_deep_research_broker` |
| `raven/agent/tools/registry.py` | surface the existing `Error`-prefix classification on `ToolOutput` |
| `raven/sandbox/direct_executor.py` | `start_new_session=True`; kill the process group on timeout and on cancellation |
| `raven/spine/events.py` | `ToolEvent.ok`; nothing else |
| `raven/tui_rpc/spine.py` | serialize `ok`; emit `media` and `progress` instead of eating `MediaOut` / `Notice` |
| `raven/tui_rpc/models.py` | `TurnSendParams.media` |
| `raven/tui_rpc/methods/turn.py` | carry `media` into the `TurnRequest` |
| `raven/cli/gateway_commands.py` | second broker for web, router, `register_question_methods` on the web dispatcher, per-turn `cancel_all` |
| `raven/cli/tui_commands.py` | wrap the TUI broker in a router (single entry) so both entry points share one type |

**Modified - service**

| Path | Change |
|---|---|
| `ui-webui/service/raven_gateway_agent.py` | dispatch clarify notifications; read `display`; map `ok` to `ToolResultState.ERROR`; `cancel()`; handle `media` / `progress`; send `media` on `turn.send` |
| `ui-webui/service/raven_config_routes.py` | `POST /raven/clarify/respond`, upload route for attachments |
| `ui-webui/service/agentscope/app/_router/_session.py` | interrupt calls the gateway cancel first |

**Modified - frontend**

| Path | Change |
|---|---|
| `ui-webui/frontend/src/components/chat/MessageBubble.tsx` | pass `t` into `DeliverFilesInlineCard`; render `ClarifyCard`; render media blocks |
| `ui-webui/frontend/src/components/chat/tool-renderers/DeliverFilesRenderer.tsx` | accept `t` as a prop |
| `ui-webui/frontend/src/components/chat/tool-renderers/index.tsx` | Raven tool-name aliases plus argument adapters |
| `ui-webui/frontend/src/components/chat/tool-renderers/DefaultRenderer.tsx` | prefer `display` for the header; show input in the body |
| `ui-webui/frontend/src/api/ravenConfig.ts` | clarify answer client |
| `ui-webui/frontend/src/i18n/locales/{en,zh}.json` | clarify card strings, error-state label |

---

## Phase 0: stop the bleeding

Two independent, self-contained fixes. Neither depends on anything else in this plan, and both are user-visible immediately.

### Task 0.1: Fix the deliver_files hook violation

**Files:**
- Modify: `ui-webui/frontend/src/components/chat/tool-renderers/DeliverFilesRenderer.tsx`
- Modify: `ui-webui/frontend/src/components/chat/MessageBubble.tsx`

**Steps:**
- [ ] Change `DeliverFilesInlineCard({ pair })` to `DeliverFilesInlineCard({ pair, t })`, typing `t: TFunction` from `./types`, and delete the `useTranslation()` call. Match `SubagentInlineCard`, which already has exactly this signature and is called from the same site.
- [ ] Remove the now-unused `useTranslation` import.
- [ ] At the `MessageBubble` call site, pass the `t` already in scope in that component.
- [ ] Grep the rest of `tool-renderers/` for any other function that is called rather than rendered while calling a hook. `renderHeader` / `renderBody` implementations are called by `renderToolCall`, so this class of bug can recur.

**Verification:**
- [ ] `pnpm -C frontend lint` reports 0 errors; the `react-hooks/rules-of-hooks` rule in particular must be clean.
- [ ] `pnpm -C frontend build` passes.
- [ ] Manual: with the stack up, prompt the agent to `deliver_files` a file. The card must render inline during streaming with no route error, and the download must still work.

### Task 0.2: Show what the tool was actually called with

**Files:**
- Modify: `ui-webui/service/raven_gateway_agent.py`
- Modify: `ui-webui/frontend/src/components/chat/tool-renderers/DefaultRenderer.tsx`

**Steps:**
- [ ] In the `tool.start` branch, read `payload.get("display")` alongside `arguments`. Carry it to the frontend on the tool call - prefer an explicit field over smuggling it into the serialized input, so the raw arguments stay intact for the renderers that parse them.
- [ ] In `defaultRenderHeader`, print the display label after the tool name when present.
- [ ] In `defaultRenderBody`, render the call input above the result, in the same framed box `subagentRenderBody` uses for its prompt. Keep the existing behaviour of returning `null` only when there is neither input nor result.
- [ ] Add the i18n key for the input box label to `en.json` and `zh.json`.

**Verification:**
- [ ] `pnpm -C frontend lint` / `build` clean.
- [ ] Manual: a turn calling `exec` and two different `read_file` paths renders three rows that are distinguishable from each other without expanding, and each expanded row shows its arguments.

---

## Phase 1: make ask_user work

The five gaps here are a chain; the feature is dead until all of them are closed, so verify end to end only at the last task.

### Task 1.1: QuestionRouter

**Files:**
- Create: `raven/tui_rpc/question_router.py`
- Create: `tests/test_question_router.py`

**Steps:**
- [ ] Implement `QuestionRouter` holding `{channel: QuestionBroker}` plus a `default` broker, and exposing `await_question(conversation_id, *, channel, prompt, choices, default, timeout_s)`, `pending_req(conversation_id, *, channel)`, `reply(key, answer)` and `cancel_all()`.
- [ ] `reply` must try every registered broker (the caller has a key, not a channel) and return `True` on the first that accepts.
- [ ] An unknown channel resolves to the default broker. Log once at debug, not at warning - non-web channels are the common case.
- [ ] Tests: routing by channel; unknown channel falls back; `reply` finds a pending question on a non-default broker; `cancel_all` releases across every broker.

**Verification:**
- [ ] `uv run pytest tests/test_question_router.py -x`

### Task 1.2: Thread the channel to the asking tools

**Files:**
- Modify: `raven/agent/tools/ask_user.py`, `raven/agent/tools/deep_research.py`, `raven/agent/loop/main.py`
- Modify: `tests/test_ask_user_tool.py`, `tests/test_deep_research_tool.py`

**Steps:**
- [ ] `AskUserTool.set_context(conversation_id, channel)` - store both in the existing `ContextVar` (a frozen dataclass, matching `MessageTool._MsgTurn`, keeps it task-isolated and copy-on-write).
- [ ] `AskUserTool.execute` passes the channel through to the router.
- [ ] In `AgentLoop`, pass the turn's channel at the `ask_tool.set_context(key)` call site; `_set_tool_context(channel, chat_id, ...)` is called immediately above, so the value is already in hand.
- [ ] `_ask_search_mode` takes the channel; `DeepResearchTool` reads it from `self._routing.get().channel`, which already exists. No public signature change there.
- [ ] Keep `set_broker` accepting a plain `QuestionBroker` as well as a router, so a caller that wires only one transport (tests, `raven agent`) is unaffected.
- [ ] Tests: a web-channel turn asks on the web broker; an IM-channel turn asks on the gateway broker; no broker at all still returns the "not configured" string rather than raising.

**Verification:**
- [ ] `uv run pytest tests/test_ask_user_tool.py tests/test_deep_research_tool.py -x`

### Task 1.3: Build and wire the web broker

**Files:**
- Modify: `raven/cli/gateway_commands.py`, `raven/cli/tui_commands.py`
- Modify: `tests/test_cli_gateway_commands.py`

**Steps:**
- [ ] Inside the `web_cfg.enabled` branch, build a second `QuestionBroker` whose `send_frame` emits to `web_emitter` as `{"type": "custom", "name": "clarify_request", "payload": {...}}` keyed by the frame's `conversation_id`. Payload: `conversation_id`, `request_id`, `question`, `choices`.
- [ ] Build the `QuestionRouter` with `{"web": web_broker}` and the existing channel broker as default; pass the router (not a bare broker) to `ask_tool.set_broker` and `agent.set_deep_research_broker`.
- [ ] Register `clarify.respond` on the web dispatcher via `register_question_methods(web_dispatcher, question_broker=web_broker)`.
- [ ] Wrap the TUI's broker in a router at its single call site too, so both entry points hand the tools the same type.
- [ ] Update the stale comment at the proactive-target block ("P1.2 does not bridge ask_user to the web client yet") - it is about to be false.
- [ ] Tests: with the web channel enabled, an `ask_user` on a web conversation reaches the web emitter and not the channel hub, and the reverse for an IM conversation; `clarify.respond` is present on the web dispatcher.

**Verification:**
- [ ] `uv run pytest tests/test_cli_gateway_commands.py -x`

### Task 1.4: Release pending questions on cancel and turn end

**Files:**
- Modify: `raven/cli/gateway_commands.py`
- Modify: `tests/test_cli_gateway_commands.py`

**Steps:**
- [ ] On `turn.cancel` for a conversation, and on the turn-end path, resolve any question pending for that conversation to its default so no card outlives its turn.
- [ ] Prefer a targeted release over `cancel_all()`, which would also drop a question belonging to a concurrent conversation.
- [ ] Test: a cancelled turn leaves no pending request for its conversation, and a concurrent conversation's pending question survives.

**Verification:**
- [ ] `uv run pytest tests/test_cli_gateway_commands.py -x`

### Task 1.5: Service plumbing for the question and the answer

**Files:**
- Modify: `ui-webui/service/raven_gateway_agent.py`, `ui-webui/service/raven_config_routes.py`

**Steps:**
- [ ] `clarify_request` arrives as a `custom` event, so `_publish_custom` already carries it to the session SSE - confirm that end to end rather than assuming it, and only add a branch if it does not.
- [ ] Add `POST /raven/clarify/respond` proxying `{conversation_id | request_id, answer}` to the gateway's `clarify.respond`. It must not go through `/chat/`: that path spawns a run and 409s while one is active, which is exactly the state a pending question is in.
- [ ] Return the gateway's `{ok}` verbatim so the UI can tell "answered" from "expired".

**Verification:**
- [ ] With the stack up: `curl` the route with a bogus `request_id` and get `{"ok": false}` rather than a 500.

### Task 1.6: The question card

**Files:**
- Create: `ui-webui/frontend/src/components/chat/ClarifyCard.tsx`, `ui-webui/frontend/src/components/chat/ClarifyContext.tsx`
- Modify: `ui-webui/frontend/src/components/chat/MessageBubble.tsx`, `ui-webui/frontend/src/api/ravenConfig.ts`, `ui-webui/frontend/src/i18n/locales/{en,zh}.json`

**Steps:**
- [ ] `ClarifyContext` subscribes to the `clarify_request` custom event and keeps the pending question per session. Follow `SubagentInstancesContext` / `DagRunsContext`, which already do this for other custom events.
- [ ] `ClarifyCard` shows the question, renders `choices` as selectable options when present, always allows a free-form answer, and submits through the API client. Reuse `ConfirmCard`'s keyboard handling (arrow keys plus Enter) so the two interaction cards behave the same.
- [ ] Clear the pending question on a successful answer, on `ok: false`, and on turn end.
- [ ] Render the card in `MessageBubble` at the same level as `SubagentHitlCard`.
- [ ] Add the card's strings to `en.json` and `zh.json` with targeted edits.

**Verification:**
- [ ] `pnpm -C frontend lint` / `build` clean.
- [ ] **End-to-end, the acceptance test for the whole phase:** ask the agent something that makes it call `ask_user` with two questions. Both must appear as cards, answering the first must let the turn proceed to the second within seconds (not 600 s), and the final answer must reflect both replies. The gateway log must contain no "has no live source" warning.
- [ ] Cancel a turn while a question is pending; the card must disappear and the session must accept a new message.

---

## Phase 2: make stop stop

Three independent defects sit behind one button (`I1`, `I3`, `I4`), plus two that
decide whether the fix reports the truth (`I5`, `I6`). Landing only task 2.1 would
produce a stop that looks like it worked and leaves the user's command running -
strictly worse than today, because today at least the UI keeps spinning. Do all
four tasks in one change.

### Task 2.1: The gateway agent yields a terminal event on cancellation

**Files:**
- Modify: `ui-webui/service/raven_gateway_agent.py`
- Modify: `ui-webui/service/tests/` (add a `reply_stream` cancellation test)

**Steps:**
- [ ] Wrap `reply_stream`'s event loop so `asyncio.CancelledError` closes any open thinking/text block and yields `ReplyEndEvent(finished_reason=ReplyEndReason.INTERRUPTED)`, then re-raises.
- [ ] Do not swallow the cancellation: `_run_impl` must still see it, or the run is treated as a normal completion.
- [ ] Do not await anything blocking in the cleanup - the task is already cancelled.
- [ ] Cover both arrival points the framework distinguishes (`_service/_chat.py:584-592`): cancellation at `__anext__`, and cancellation re-armed from the publish side.

**Verification:**
- [ ] The new test asserts a terminal `ReplyEndEvent` with `INTERRUPTED` for both cancellation points.
- [ ] Manual: stop a turn; the bubble switches from spinner to finished and the elapsed timer freezes.
- [ ] Manual: reload afterwards; the bubble is still finished (this is what proves `finished_at` was persisted).

### Task 2.2: Cancel the turn upstream

**Files:**
- Modify: `ui-webui/service/raven_gateway_agent.py`, `ui-webui/service/agentscope/app/_router/_session.py`

**Steps:**
- [ ] Add `GatewayClient.cancel(session_key)` calling `turn.cancel`.
- [ ] Call it from the interrupt path *before* cancelling the local run, so the gateway ends the turn instead of being orphaned.
- [ ] Treat an RPC timeout as success-in-progress, not failure: `turn_cancel` awaits the full unwind (`raven/tui_rpc/methods/turn.py:255-257`) while `GatewayClient.call` gives up after 30 s (`I5`). Log it; never surface it as a failed stop.
- [ ] A failed cancel must not block the local cancel.
- [ ] Release any question pending for that conversation in the same handler (companion spec decision 5) so an `ask_user` card cannot outlive its turn.

**Verification:**
- [ ] Manual: after a stop, the next message is accepted normally instead of returning `session '...' already has an active turn`.

### Task 2.3: The executor kills its process group

**Files:**
- Modify: `raven/sandbox/direct_executor.py`
- Modify: `tests/test_direct_executor.py` (or the existing executor test file)

**Steps:**
- [ ] Start the child with `start_new_session=True` so the tool's children form their own process group.
- [ ] Signal the **group** on both the timeout path and a new cancellation path, escalating `SIGTERM` then `SIGKILL`.
- [ ] The cancellation path re-raises after killing; it must not convert a cancellation into a normal `ExecResult`.
- [ ] Check `BoxliteExecutor` for the same shape while here - its per-exec path was not audited, only its init/stop.
- [ ] Test with a shell wrapper that spawns a grandchild (`bash -c 'sleep 30; ...'`), not a bare command: the pid-only kill this replaces passes a bare-command test.

**Verification:**
- [ ] `uv run pytest tests/ -k executor -x`
- [ ] Manual, the audit's probe: `exec` a `bash -c 'sleep 90; echo X > /tmp/stoptest.txt'`, stop it, then confirm with `ps` that no `sleep` survives and that `/tmp/stoptest.txt` never appears.
- [ ] Regression: a normal `exec` and a timing-out `exec` still behave as before, on the TUI as well as the web UI.

### Task 2.4: The client stops guessing

**Files:**
- Modify: `ui-webui/frontend/src/hooks/useMessages.ts`

**Steps:**
- [ ] Clear `currentReplyRef` when the interrupt fallback timer fires, not just the phase (`I6`), so a late event cannot land on a dead reply.
- [ ] Keep the 10 s fallback as a safety net, but with task 2.1 in place the terminal event should always win; if the fallback is what ends an interrupt, that is a bug worth a `console.warn`.
- [ ] Either surface the hook's `error` state or delete it - it is currently written and never read.
- [ ] Do not add a client-side "assume dead after N minutes" heuristic for `finished_at` (companion spec decision 2): it would hide the next lost terminal event.

**Verification:**
- [ ] `pnpm -C frontend lint` / `build` clean.
- [ ] Manual: stop, then immediately send; the composer either accepts the message and it reaches the agent, or refuses it - never accepts it into a void.

---

## Phase 2b: stop lying about how a turn ended

Same family as Phase 2, separable review-wise: these are the non-interrupt ways a
turn ends where the UI is told the wrong thing (`I8`, `I9`, `I10`, plus `I7`).

### Task 2b.1: A rejected message leaves no phantom bubble

**Files:**
- Modify: `ui-webui/frontend/src/hooks/useMessages.ts`
- Modify: `ui-webui/service/agentscope/app/_router/_chat.py`
- Modify: `ui-webui/frontend/src/i18n/locales/{en,zh}.json`

**Steps:**
- [ ] On a failed `chatApi.trigger`, remove the optimistically-appended user message and show a specific, translated message. Today the bubble stays, the text is lost on reload (`I7`), and the toast carries `ChatRunRegistry`'s internal wording.
- [ ] Give the 409 a caller-facing detail that says a reply is still in progress, instead of exposing the registry's phrasing.
- [ ] Do not add a retry queue (companion spec decision 6) - dropping the bubble with a clear message is the whole fix.

**Verification:**
- [ ] Manual: force a 409 (send during a run via a second tab); the bubble disappears and the toast explains why.

### Task 2b.2: Distinguish submit failures

**Files:**
- Modify: `ui-webui/service/raven_gateway_agent.py`, `ui-webui/frontend/src/i18n/locales/{en,zh}.json`

**Steps:**
- [ ] Split `reply_stream`'s connect/submit `except` into: no socket, `-32003` (turn already running), `-32008` (no model), other. Emit a machine-readable reason; let the frontend render the text (`I10` decision 9).
- [ ] Remove the hardcoded Chinese strings (`连接 gateway 失败`, `[gateway 连接中断,本轮结束]`) - they are written into reply content and persisted.
- [ ] Note in the code why `-32003` should be unreachable after Phase 2, so a future reader knows it is a symptom, not a normal state.

**Verification:**
- [ ] Manual: with the gateway stopped, sending shows a connection error; with a turn artificially in flight, sending shows the turn-in-progress message. The two are distinguishable.

### Task 2b.3: The idle clock and the disconnect end turns honestly

**Files:**
- Modify: `ui-webui/service/raven_gateway_agent.py`

**Steps:**
- [ ] On the 900 s idle timeout, end the reply with `INTERRUPTED` (not `COMPLETED`) and call the Phase 2.2 cancel so client and gateway agree the turn is over (`I9`).
- [ ] Same for `__disconnected__` (`I10`).
- [ ] Do not raise the timeout instead - any finite clock has this failure; the label and the missing cancel are the bug.

**Verification:**
- [ ] Manual (or with the timeout temporarily lowered): a turn that outlives the clock is marked interrupted, and the next message is accepted.

---

---

## Phase 3: let produced media through

Both tasks change the shared outlet. Do task 3.0 first or the TUI may throw on an event it has never seen.

### Task 3.0: Confirm the TUI ignores unknown event types

**Files:**
- Inspect: `ui-tui/src/` event handling; modify only if it does not already ignore unknown types.

**Steps:**
- [ ] Find the wire-event switch and confirm the default branch is a no-op.
- [ ] If it is not, make it one before anything emits `media` or `progress`.

**Verification:**
- [ ] A TUI session runs normally against a gateway emitting the new events (revisit after task 3.1).

### Task 3.1: A media wire event

**Files:**
- Modify: `raven/tui_rpc/spine.py`, `raven/cli/gateway_commands.py`
- Modify: `ui-webui/service/raven_gateway_agent.py`, `ui-webui/frontend/src/components/chat/MessageBubble.tsx`
- Create: `tests/test_spine_media_events.py`

**Steps:**
- [ ] Stop eating `MediaOut` in `TuiOutlet.deliver`; emit `{"type": "media", "payload": {"files": [{path, name, mime, kind, token}]}}`.
- [ ] Mint a download token per file through the existing `DeliverableStore`, so media reuses the one download trust boundary instead of adding a second. The outlet needs the store; pass it in from the gateway rather than importing a global.
- [ ] Never put bytes in the payload. A generated video would exceed the RPC frame limit that motivated the deliverables route.
- [ ] Service: translate `media` into content blocks the frontend already knows how to render, or into a custom event if that turns out cleaner - decide against the actual AgentScope block types, do not guess here.
- [ ] Frontend: render images inline, everything else as a download row (reuse `DeliveredFileRow`).
- [ ] Tests: `MediaOut` produces one event with one entry per file; the token resolves through the existing download route; no bytes appear in the event.

**Verification:**
- [ ] `uv run pytest tests/test_spine_media_events.py -x`
- [ ] Manual: ask the agent to call `message` with a `media` argument. The attachment must appear and download.
- [ ] Manual, if a media model is configured: `image_generate` produces a visible image.
- [ ] Manual: a TUI session is unaffected.

### Task 3.2: A progress wire event

**Files:**
- Modify: `raven/tui_rpc/spine.py`, `ui-webui/service/raven_gateway_agent.py`, `ui-webui/frontend/src/components/chat/MessageBubble.tsx`

**Steps:**
- [ ] Emit `Notice` as `{"type": "progress", "payload": {"kind": "progress" | "tool_hint", "text": ...}}`.
- [ ] Render under the in-flight tool row; discard on that tool's completion so progress lines do not accumulate in history.

**Verification:**
- [ ] Manual: a long tool shows intermediate progress and leaves nothing behind once it completes.

---

## Phase 4: tell success from failure

### Task 4.1: Carry the error classification to the UI

**Files:**
- Modify: `raven/agent/tools/registry.py`, `raven/spine/events.py`, `raven/agent/loop/main.py`, `raven/tui_rpc/spine.py`
- Modify: `ui-webui/service/raven_gateway_agent.py`, `ui-webui/frontend/src/components/chat/tool-renderers/_shared.tsx`
- Modify: `tests/test_tool_registry.py` (or the existing registry test file)

**Steps:**
- [ ] `ToolRegistry.execute` already computes `model_text.startswith("Error")` to decide whether to append its retry hint. Surface that boolean on `ToolOutput` instead of discarding it.
- [ ] Add `ToolEvent.ok: bool = True` (COMPLETE only) and populate it at the `on_tool_event("complete", ...)` site.
- [ ] Serialize `ok` on `tool.complete`.
- [ ] Service: `ToolResultState.ERROR` when `ok` is false.
- [ ] Frontend: the row's state icon reflects the error state. Check what `ToolCallRow` already does with `result.state` before adding anything new.
- [ ] Document in the code comment that the criterion is the `Error` prefix, not a semantic failure signal - a tool that fails without that prefix stays green.
- [ ] Tests: an `Error`-prefixed result sets `ok=False`; a normal result does not; a tool timeout sets `ok=False`.

**Verification:**
- [ ] `uv run pytest tests/ -k "registry or tool_event" -x`
- [ ] Manual: `read_file` on a missing path renders visibly as an error without expanding the row.

---

## Phase 5: readable tool rows

### Task 5.1: Raven tool-name aliases

**Files:**
- Modify: `ui-webui/frontend/src/components/chat/tool-renderers/index.tsx` and the individual renderers as needed

**Steps:**
- [ ] Add registry entries for `exec`, `read_file`, `write_file`, `edit_file`, `grep`, `find`, `list_dir`, pointing at the existing `Bash` / `Read` / `Write` / `Edit` / `Grep` / `Glob` renderers.
- [ ] Each aliased renderer reads argument names from the AgentScope tool. Check each Raven tool's parameter schema and adapt: the names differ (for example `exec` takes `command`, `read_file` takes `path`). An adapter per alias is preferable to branching inside the renderer.
- [ ] Do not create parallel renderer components; the rendered output is the same.

**Verification:**
- [ ] `pnpm -C frontend lint` / `build` clean.
- [ ] Manual: a turn using `exec`, `read_file`, `write_file` and `grep` renders each row with its own arguments, and an `edit_file` shows its diff preview.

---

## Phase 6: attachments into the agent

### Task 6.1: Accept media on turn.send

**Files:**
- Modify: `raven/tui_rpc/models.py`, `raven/tui_rpc/methods/turn.py`, `raven/tui_rpc/openrpc.json`
- Modify: `ui-webui/service/raven_gateway_agent.py`, `ui-webui/service/raven_config_routes.py`
- Modify: `tests/test_tui_rpc_turn.py` (or the existing turn-method test file), plus the schema-parity test

**Steps:**
- [ ] Add `media: list[str] | None = None` to `TurnSendParams`. The model is `_Strict`, so an undeclared key is rejected rather than ignored - the field must be declared, not smuggled.
- [ ] Carry it into the `TurnRequest` the way the channel path already carries inbound media; do not invent a second representation.
- [ ] Update `openrpc.json` and whatever cross-language parity test covers `turn.*`.
- [ ] Service: an upload route that stores bytes under the session workspace and passes paths; `_extract_text` gains a sibling that collects media blocks.
- [ ] Frontend: the existing attachment UI sends through the new route.

**Verification:**
- [ ] `uv run pytest tests/ -k "turn or openrpc" -x`
- [ ] Manual: attach an image in the browser and ask the agent to describe it.

---

## Phase 7: stream robustness

Lowest priority: neither defect bites in a healthy local deployment.

### Task 7.1: Reconnect the SSE

**Files:**
- Modify: `ui-webui/frontend/src/hooks/useMessages.ts`, `ui-webui/frontend/src/api/session.ts`

**Steps:**
- [ ] When the stream ends while the component is still mounted, retry with capped backoff instead of exiting the loop silently (`I11`).
- [ ] Show a reconnecting indicator; distinguish an abort (session switch / unmount) from an unexpected end.
- [ ] Replay makes this safe - the server replays the current run's buffered events and the log is dropped at turn end - so no dedup logic is needed. Confirm that rather than assuming it.

**Verification:**
- [ ] Manual: restart the service mid-turn; the UI shows reconnecting and then recovers to the live turn.

### Task 7.2: Clean up after a WS reconnect

**Files:**
- Modify: `ui-webui/service/raven_gateway_agent.py`

**Steps:**
- [ ] Drop stale queues wherever `_subs` is cleared, so `_queues` does not grow one entry per session per reconnect (`I12`).
- [ ] Best-effort `turn.unsubscribe` before discarding a subscription, so the gateway's emitter does not accumulate registrations.

**Verification:**
- [ ] Bounce the gateway a few times with a session open and confirm `_queues` does not grow without bound.

---

## Known artifact this plan does not fix

Replies interrupted **before** Phase 2 ships are persisted with no `finished_at`,
so they keep rendering as running (with a live timer) in every future page load -
see `I2`. Task 2.1 stops new ones from being created but does not repair stored
ones.

**Decided 2026-08-08: no migration.** Existing sessions keep their spinning
bubbles; the artifact is cosmetic, confined to sessions that were stopped before
the fix, and clears itself as those sessions age out. Do not add a client-side
"assume dead" heuristic to hide them either - that is the same trap companion spec
decision 2 rejects, and it would mask the next lost terminal event.

## Suggested delivery split

Squash is server-enforced, so each merge request lands as one commit. These group cleanly and each is independently useful:

| MR | Contents | Rationale |
|---|---|---|
| 1 | Phase 0 | Two small fixes, one of which stops a page crash. Ship first, review in minutes. |
| 2 | Phase 2 | Stop actually stops. Four tasks that must not be split - shipping a subset produces a stop that lies. Spans core (`direct_executor`) and both web layers. |
| 3 | Phase 2b | The remaining "turn ended" lies plus the lost-message fix. Depends on Phase 2's cancel call. |
| 4 | Phase 1 | One feature (`ask_user` works), six tasks that are meaningless apart. |
| 5 | Phase 3 | Touches the shared outlet; deserves its own review and its own TUI regression pass. |
| 6 | Phase 4 + Phase 5 | Both are "the row tells the truth"; small together. |
| 7 | Phase 6 + Phase 7 | Independent; lowest priority. |

Phase 2 is promoted ahead of Phase 1 because it is the only phase that leaves
permanent artifacts in stored history, and because Phase 1's cancel handling
(releasing a pending question) builds on Phase 2.2.

## Out of scope

- A permission / confirmation model for gateway tools.
- `episode.start` grouping in the web UI.
- Rendering multimodal tool *results* (the `TODO` in `DefaultRenderer.tsx`).
- Anything on the AgentScope-native agent path.
