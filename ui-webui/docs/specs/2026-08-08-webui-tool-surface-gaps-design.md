# Web UI tool surface — gap audit and design

**Date:** 2026-08-08
**Status:** Draft (design)
**Related:** `2026-07-23-p1-gateway-web-channel.md` (the web spine this audit
covers), `2026-07-23-p2-kernel-switch-ws-client.md` (the service-side event
translation), `2026-07-30-raven-deliver-files-tool-design.md` (the one tool that
was designed end to end for this surface)

## 1. Motivation

The web app's chat agent is a persistent `raven gateway` web channel: the service
is a WebSocket JSON-RPC client that translates spine events into AgentScope
events, and the browser renders those. That path was built for one shape of tool
call - the model calls a tool, the tool returns text, the text is shown. Every
tool that needs anything beyond that shape is degraded or broken on this surface,
and nothing in the code or the UI says so.

The concrete trigger: `ask_user` is registered unconditionally on every
`AgentLoop` (`raven/agent/loop/main.py:715`), the model reaches for it whenever a
task is genuinely ambiguous, and on the web channel it cannot work at all. A turn
that calls it stalls for ten minutes per question, shows the user a spinner with
no question text, refuses to accept an answer, and then proceeds as if the user
had declined to answer.

This document inventories every gap on that surface, records what was verified
against a live deployment, and fixes a design for each one. It changes no code.

## 2. How this was verified

Static reading of `origin/main`, then a live run of the full stack on
2026-08-08 (`raven gateway` + service on `:8000` + Vite on `:5173`, driving the
browser). Evidence is quoted inline per gap. The gaps that could only be settled
by running are marked **(observed)**; the rest are structural and hold by
inspection.

Two findings changed as a result of running it, and both are recorded here rather
than smoothed over:

- The `ask_user` question text was expected to be visible in the tool row's
  title. It is not: the service never reads `display`, and the default renderer
  shows no input at all. The user sees a bare tool name.
- `deliver_files` was expected to be the one fully working tool. Its backend and
  download route are fine, but rendering its inline card **crashes the whole chat
  route** on every live delivery.

## 3. Scope

**In scope**

- The tool surface of the gateway `web` channel: what reaches the browser between
  `turn.send` and `message.complete`, and what the browser can send back.
- The wire protocol between the gateway (`raven/rpc/spine.py` outlet, shared
  with the TUI) and the service.
- The service's event translation (`ui-webui/service/raven_gateway_agent.py`) and
  its REST surface.
- The frontend's tool rendering and any new interaction card.

**Out of scope**

- The AgentScope-native agent path. `ConfirmCard` / `SubagentHitlCard` /
  `PermissionPanel` serve that path's builtin tools; the gateway path has no
  permission model and this design does not give it one.
- IM channel behaviour. Every change here is additive to the channel spine, which
  keeps its current `ask_user` round trip unchanged.
- New tools. This is about the transport for the tools that already exist.
- Authentication on the service. Unrelated and already noted as local-only.

## 4. Gap inventory

Severity: **A** = the feature does not work or the page breaks; **B** = works but
the user is misinformed; **C** = works but is hard to read.

| id | Gap | Sev | Where |
|---|---|---|---|
| G1 | `ask_user` questions on the web channel are dropped | A | `raven/cli/gateway_commands.py:560-576` |
| G2 | The web dispatcher never registers `clarify.respond` | A | `raven/cli/gateway_commands.py:427-443` |
| G3 | The service ignores every frame that is not `method == "event"` | A | `ui-webui/service/raven_gateway_agent.py:148` |
| G4 | No answer UI, and the only inbound path rejects an answer with 409 | A | `ui-webui/service/agentscope/app/_router/_chat.py:154` |
| G5 | `deliver_files` inline card crashes the chat route | A | `ui-webui/frontend/src/components/chat/MessageBubble.tsx:462` |
| G6 | `MediaOut` is eaten, so tool-produced media never reaches the user | A | `raven/rpc/spine.py:254` |
| G7 | `Notice` is eaten, so mid-tool progress never reaches the user | C | `raven/rpc/spine.py:254` |
| G8 | `tool.complete` carries no success/failure signal | B | `raven/spine/events.py:64-81` |
| G9 | The service drops the tool's `display` label | B | `ui-webui/service/raven_gateway_agent.py:369-374` |
| G10 | The default renderer never shows call input | C | `ui-webui/frontend/src/components/chat/tool-renderers/DefaultRenderer.tsx:71` |
| G11 | The renderer registry is keyed on AgentScope tool names | C | `ui-webui/frontend/src/components/chat/tool-renderers/index.tsx:24-34` |
| G12 | Stop does not reach the gateway | A | `ui-webui/service/raven_gateway_agent.py:196-206` |
| G13 | `turn.send` is text-only, so uploads never reach the agent | B | `raven/rpc/models.py:552-557` |

### G1 - ask_user questions are dropped on the web channel

The gateway builds exactly one `QuestionBroker`, whose `send_frame` is
`_question_to_channel`. That callback looks the conversation up in `gw_sources`
and dispatches a `Text` to the IM/CLI hub. `gw_sources` is populated only by
`GatewayTurnRunner` (`raven/cli/_gateway_spine.py`); the web channel runs a
different spine (`build_web` -> `build_tui`), so a web conversation id is never
in that map. The callback logs and returns; the broker then waits out its
600 s fail-safe and resolves to the empty default.

The code says so itself at `raven/cli/gateway_commands.py:468-469`: *"P1.2 does
not bridge ask_user to the web client yet."*

**(observed)** A live web session asked three questions in one `ask_user` call:

```
07:50:07 WARNING ask_user question for web:82dc59e2... has no live source - dropping
08:00:07 WARNING ask_user question for web:82dc59e2... has no live source - dropping
08:10:07 WARNING ask_user question for web:82dc59e2... has no live source - dropping
```

Exactly 600 s apart - the questions are asked serially, so a batch of N costs
N x 600 s of silence before the turn continues with "user did not answer".

`deep_research` shares this broker for its deep-vs-regular prompt
(`AgentLoop.set_deep_research_broker`), so that prompt fail-safes to `regular` on
the web channel, and `DeepResearchOfferTool`, whose entire behaviour is two
broker prompts, is inert there.

### G2 - the web dispatcher never registers clarify.respond

`register_question_methods` is called only from the TUI method umbrella
(`raven/rpc/methods/__init__.py:173`). The gateway builds the web dispatcher
by hand and registers system, turn and config methods only. Even a client that
knew the contract has no method to call.

### G3 - the service ignores non-event frames

`GatewayClient._read_loop` routes responses by `id` and events by
`method == "event"`. A `clarify.request` notification would fall off the end of
the loop silently.

### G4 - no answer UI, and 409 on the only inbound path

The browser's only way to send text is `POST /chat/`, which spawns a run through
`chat_run_registry` and raises 409 when the session already has one. A turn
blocked inside `ask_user` is still an active run.

**(observed)** Typing an answer into a session blocked on `ask_user` renders the
bubble locally and returns `409 Conflict`; the answer never leaves the browser.
So even with G1-G3 fixed, the answer needs a path that is not `/chat/`.

### G5 - deliver_files crashes the chat route

`MessageBubble` calls `DeliverFilesInlineCard({ pair: block.call })` as a plain
function, but that function calls `useTranslation()` on its first line
(`DeliverFilesRenderer.tsx:23`). The hook therefore joins `MessageBubble`'s own
hook sequence, and it only joins once a deliverable block exists - which, while
streaming, is a later render than the first.

**(observed)** On a live `deliver_files` call the page is replaced by the route
error boundary:

```
Something went wrong - Rendered more hooks than during the previous render.
  at useTranslation (src/i18n/useI18n.ts:3:9)
  at DeliverFilesInlineCard (src/components/chat/tool-renderers/DeliverFilesRenderer.tsx:25:16)
  at renderBlock (src/components/chat/MessageBubble.tsx:470:17)
```

(Line numbers in that trace are the running checkout's; on `main` the call site
is `MessageBubble.tsx:462` and the hook `DeliverFilesRenderer.tsx:23`. The defect
is identical on both.)

Retry remounts and renders correctly, and the download works (a delivered file
was fetched end to end). So the damage is confined to the live render, but every
delivery costs the user the page.

`SubagentInlineCard` in the same directory is called the same way and does *not*
crash, because it takes `t` as a prop instead of calling the hook.

### G6 - MediaOut is eaten

`RpcOutlet.deliver` handles `Reasoning`, `ToolEvent`, `Text` and `EpisodeStart`,
and drops `Notice` and `MediaOut` with the comment *"no wire event today ... a
known gap, deferred"*. The `message` tool's `media` argument becomes a `MediaOut`
(`raven/agent/loop/main.py:2826-2846`), and that is also the documented route for
the media generation tools, which save a file and return its path for the model
to forward (`raven/agent/tools/media_gen.py:28-30`).

**(observed)** The model called

```
'name': 'message', 'arguments': '{"content": "...", "channel": "web", "chat_id": "default", "media": ["/etc/hostname"]}'
```

and the browser showed the text with no attachment of any kind.

This gap is shared with the TUI; fixing it at the outlet fixes both surfaces.

### G7 - Notice is eaten

Same site, same reason. `PROGRESS` and `TOOL_HINT` notices never reach either
surface, so a long tool shows no intermediate state.

### G8 - no success/failure signal on tool.complete

`ToolEvent` carries `result_preview`, `truncated` and `metadata` but no status,
so the service hardcodes `ToolResultState.SUCCESS`. The information does exist
one layer down: `ToolRegistry.execute` already classifies a result as an error by
`model_text.startswith("Error")` in order to decide whether to append its retry
hint (`raven/agent/tools/registry.py:106`). It simply never leaves that function.

**(observed)** A failing `read_file` and two successful calls render with byte
identical status icons; only expanding the row and reading the text reveals
`Error: File not found`.

### G9 - display is dropped

`tool.start` carries a tool-authored label (`ToolEvent.display`, fed by
`Tool.display_call`) precisely so the UI can show *what* was asked rather than an
argument blob. The service builds its `ToolCallDeltaEvent` from `arguments` only
and never reads `display`.

### G10 - the default renderer shows no input

`defaultRenderHeader` prints "Call tool" plus the tool name; `defaultRenderBody`
returns the result and nothing else. For any tool without a dedicated renderer
the call arguments are unreachable from the UI.

### G11 - the renderer registry is keyed on the wrong names

The registry maps `Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `TaskCreate` -
the AgentScope builtin tool names. Raven's core tools are `exec`, `read_file`,
`write_file`, `edit_file`, `find`, `grep`, `list_dir`. Only `deliver_files` and
`run_subagent_dag` are keyed on names the gateway actually emits.

**(observed)** Three calls in one turn rendered as `Call tool exec`,
`Call tool read_file`, `Call tool read_file` - the two `read_file` rows
indistinguishable.

### G12 - stop does not reach the gateway

`GatewayClient` exposes `subscribe` and `send_turn` and nothing else; the session
interrupt cancels the local reply stream. `turn.cancel` is registered on the web
dispatcher and never called.

**(observed)** An `exec` running `sleep 100; echo ... > /tmp/stoptest.txt` was
stopped from the UI. The composer returned to idle, the process was still alive
40 s later, and the file appeared on schedule. The message's "Running..." timer
never stopped, because the service had stopped reading the queue - the same
frozen-UI failure the timeout comment at `raven_gateway_agent.py:322-328` warns
about, arriving through the interrupt path instead.

### G13 - turn.send is text-only

`TurnSendParams` is a `_Strict` model with `content: str` and no media field, and
`_extract_text` collects text blocks only. Files attached in the browser cannot
reach the agent.

## 5. Decisions

1. **One broker per transport, selected by channel** (2026-08-08) - the gateway
   builds a second `QuestionBroker` for the web channel and puts a small
   `QuestionRouter` in front of both. The router is what the tools hold; it picks
   a broker from the turn's `source.channel`, defaulting to the gateway spine
   broker for every non-web channel.

   Rejected: making `AskUserTool` hold a dict and key it by conversation id. Web
   conversation ids (`session_key`) and channel conversation ids
   (`conversation` or `channel:chat_id`) are not reliably distinguishable as
   strings, and a prefix convention would be a silent trap the first time a
   channel names a conversation like a session key.

   The channel is already available at both call sites: `AgentLoop` calls
   `_set_tool_context(channel, chat_id, ...)` immediately before
   `ask_tool.set_context(key)`, and `DeepResearchTool._Routing` already carries
   `channel`. So `set_context` gains a `channel` argument and `deep_research`
   needs no signature change at all.

2. **The question reaches the browser on the existing custom-event lane**
   (2026-08-08) - `clarify.request` is emitted to the web emitter as
   `{"type": "custom", "name": "clarify_request", "payload": {...}}`, the same
   shape DAG progress and injected skills already use
   (`gateway_commands.py:445-460`). The service publishes it on the message bus
   through `_publish_custom`, so it reaches the session SSE without entering the
   reply's content blocks.

   Rejected: a new top-level wire event type. It would need a matching branch in
   the TUI client, an `openrpc.json` entry and cross-language schema parity, for
   a payload the custom lane already carries. The confirm/question pair is
   deliberately outside that contract today (`raven/rpc/methods/question.py`
   docstring) and stays outside it.

3. **The answer travels on its own REST route, not through /chat/**
   (2026-08-08) - a `POST /raven/clarify/respond` route on the service proxies to
   the gateway's `clarify.respond`. It must not go through the chat front door:
   the session has an active run by construction (that is what is blocked), so
   `/chat/` will always 409 (G4).

   The route is idempotent by the broker's own contract - an unknown or already
   resolved key returns `{ok: false}` rather than raising.

4. **The web dispatcher registers `clarify.respond` with its own broker**
   (2026-08-08) - reusing `register_question_methods`, which already takes the
   broker as a keyword. No new RPC method is invented; the web dispatcher simply
   stops being the only surface that omits it.

5. **Cancel releases pending questions** (2026-08-08) - `turn.cancel` and the
   turn-end path must call `cancel_all()` on the web broker for that
   conversation, so a stopped turn does not leave a card the user can still
   answer into a dead future. The gateway already does this on shutdown
   (`gateway_commands.py`), never per turn.

6. **`ok` is derived where the classification already happens** (2026-08-08) -
   `ToolRegistry.execute` already decides `model_text.startswith("Error")` to
   append its retry hint. That boolean is surfaced on `ToolOutput`, carried to
   `ToolEvent.ok` (default `True`, COMPLETE only), serialized on `tool.complete`,
   and mapped by the service to `ToolResultState.ERROR`.

   Rejected: having the service pattern-match `result_preview` for a leading
   `Error`. It would duplicate a core policy in a client, in a different
   language, against a string that is already truncated to 200 chars and may be
   a display string rather than the model text.

   Note the criterion is presentational, not semantic - a tool that fails without
   the `Error` prefix stays green. Widening it is a separate change to the core's
   error convention and is out of scope here.

7. **`display` wins over `arguments` for the call label; `arguments` stays
   visible in the body** (2026-08-08) - the service passes `display` through, and
   the frontend prefers it for the trigger line. The raw input moves into the
   expandable body of the default renderer, so nothing that is visible today is
   lost and the curated label is what the user reads first.

8. **A new `media` wire event, emitted by the shared outlet** (2026-08-08) -
   `RpcOutlet.deliver` stops eating `MediaOut` and emits
   `{"type": "media", "payload": {"files": [{path, mime, kind}]}}`. Bytes do not
   travel on the wire: the payload is a reference, and the browser fetches
   content through the deliverable download route, which already exists and is
   already proxied.

   That means a media reference needs a download token, which today only
   `deliver_files` mints. The outlet therefore registers the referenced paths in
   the same `DeliverableStore` before emitting. This keeps exactly one download
   trust boundary rather than adding a second, weaker one.

   Rejected: inlining base64 in the event. A generated video would cross the
   1 MiB RPC frame limit that motivated the deliverables route in the first
   place.

9. **`Notice` becomes a `progress` wire event** (2026-08-08) - same site, same
   shape, rendered under the in-flight tool row and discarded when the tool
   completes. Kept separate from decision 8 so the media work is not blocked on
   it.

10. **The frontend renderer registry gains Raven aliases; no new renderer
    components** (2026-08-08) - `exec` -> `BashRenderer`, `read_file` ->
    `ReadRenderer`, and so on, with a per-renderer argument adapter where the
    Raven tool's parameter names differ from the AgentScope tool's. Writing
    parallel components would double the maintenance for identical output.

11. **`DeliverFilesInlineCard` takes `t` as a prop** (2026-08-08) - matching
    `SubagentInlineCard`, which is called from the same site in the same way and
    is correct. Converting the call to `<DeliverFilesInlineCard />` also fixes
    the hook violation, but the caller uses the `undefined` return value to
    decide whether to fall through to the collapsible renderer, and a component
    element is always truthy - so that route needs the fallback logic reworked
    too. The prop is the smaller, more local fix.

12. **Stop calls `turn.cancel` before cancelling the local stream**
    (2026-08-08) - `GatewayClient` gains `cancel(session_key)`, and the agent
    cancels upstream first so the gateway stops the turn rather than being
    orphaned. If the RPC fails the local cancel still runs; a stop must never be
    blocked by a broken socket.

13. **Attachments extend `turn.send` with an explicit `media` field**
    (2026-08-08) - `TurnSendParams` is `_Strict`, so an extra key is rejected
    rather than ignored; the field has to be declared. The service stores the
    uploaded bytes under the session workspace and sends paths, matching how
    every other file reaches the agent.

## 6. Wire protocol changes

| Event | Change | Compatibility |
|---|---|---|
| `tool.complete` | new `ok: bool` (default `true`) | additive; the TUI ignores unknown keys |
| `tool.start` | none (the service starts reading the existing `display`) | none |
| `custom` / `clarify_request` | new `name` on an existing event type | additive |
| `media` | new event type | the TUI must ignore unknown types (verify) |
| `progress` | new event type | as above |
| `clarify.respond` | existing method, newly registered on the web dispatcher | none |
| `turn.send` | new optional `media: list[str]` | additive on a `_Strict` model |

`openrpc.json` and the cross-language schema parity tests cover `turn.*`, so
decision 13 touches them; the clarify pair and the new outlet events do not
belong to that contract (see decision 2).

## 7. Per-tool outcome

"Now" is the audited state; "After" is this design fully applied.

| Tool | Now | After |
|---|---|---|
| `ask_user` | unusable (G1-G4, G9, G10) | question card, answer, cancel |
| `deep_research` (real) | runs; its prompt is skipped (G1) | prompt works |
| `deep_research` (offer) | inert (G1) | works |
| `deliver_files` | works, but crashes the page on every delivery (G5) | works |
| `message` | text only; attachments vanish (G6) | attachments arrive |
| `image_generate` / `speech_generate` / `video_generate` | product unreachable (G6) | product arrives |
| `exec`, `read_file`, `write_file`, `edit_file`, `grep`, `find`, `list_dir` | run; unreadable rows (G9-G11) | argument-aware rows |
| `web_search`, `web_fetch`, `read_skill`, `use_skill`, `cron`, `tool_search`, MCP, plugin tools | run; generic rows (G9, G10) | label plus input visible |
| `spawn` | works | unchanged |
| `run_subagent_dag` | works | unchanged |
| any failing tool | renders as success (G8) | renders as error |
| stop button | does not stop anything (G12) | stops the turn |
| uploads | never reach the agent (G13) | reach the agent |

## 8. Risks

- **Two brokers on one `AgentLoop`.** The gateway serves IM channels and the web
  channel from one loop, and `set_broker` is a single-slot setter today. The
  router (decision 1) is the whole mitigation; if a turn arrives with a channel
  the router does not know, it must fall back to the gateway broker rather than
  to `None`, or the tool silently reports "not configured".
- **A question outliving its turn.** Decision 5 is what prevents an answered card
  from resolving a future nobody is waiting on. Worth a test that cancels mid
  question.
- **The `media` event's download tokens** (decision 8) extend the deliverables
  trust boundary to files the user never explicitly asked for. The store's
  existing token rules apply unchanged, but the review should confirm that a
  generated file landing in the workspace is an acceptable thing to mint a token
  for.
- **`ok` is a prefix convention** (decision 6). It will mark some genuine
  failures green. That is strictly better than marking all of them green, and the
  doc says so rather than implying the signal is exact.
- **Shared outlet.** Decisions 8 and 9 change `RpcOutlet`, which the TUI also
  uses. The TUI must tolerate the new event types before they are emitted.

## 9. Deferred

- `episode.start` is emitted by the outlet and ignored by the service, so the web
  UI has no episode grouping. Cosmetic; no decision taken.
- A permission / confirmation model for gateway tools. The frontend has the
  components, the gateway path has no policy engine, and inventing one here would
  be a much larger design.
- Rendering multimodal tool *results* (the `TODO` at `DefaultRenderer.tsx:92`).
  Decision 8 covers media the agent sends, not images returned inside a tool
  result.

## 10. Test strategy

- **Core (pytest).** Router selection per channel including the unknown-channel
  fallback; `clarify.respond` registered on a web dispatcher; `ok` set for an
  `Error`-prefixed result and unset otherwise; `MediaOut` and `Notice` producing
  their wire events; `turn.send` accepting and rejecting `media`; cancel
  releasing a pending question.
- **Service.** The clarify route resolving and its unknown-key path;
  `turn.cancel` reaching the gateway on interrupt; the read loop dispatching a
  clarify notification.
- **Frontend.** No unit runner exists, so the gate is `pnpm -C frontend lint`
  (0 errors) plus `pnpm -C frontend build`, and a manual pass covering: a live
  `deliver_files` call without a crash, an `ask_user` question answered from the
  card, a failing tool rendered as an error, and a stop that actually stops.
- **Regression.** The TUI must be exercised after the outlet changes - it shares
  `RpcOutlet` and must ignore the new event types rather than throwing.
