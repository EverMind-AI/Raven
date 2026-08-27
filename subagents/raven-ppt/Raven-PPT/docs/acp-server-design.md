# raven-ppt as an ACP agent

The design record for serving ACP (Agent Client Protocol) from this checkout, so
that the raven-ppt sub-agent is driven as a protocol peer instead of as a CLI
process whose stdout is parsed.

Written before the code, and kept because the shape of this change is not
obvious from any one file: the translation it needs already exists in the tree
under a different wire vocabulary, and the reasons for every method that is
*not* served are the part a reader will otherwise re-litigate.

## 1. What is being replaced

Today the host raven dispatches this folder as `kind: "cli"`:

    {PYTHON} {SUBAGENT_DIR}/run.py --job {agent_id} --session cli:{agent_id} --prompt-file {prompt_file}

`run.py` forks `Raven-PPT/.venv/bin/raven agent -m <task>`, mirrors the child's
stdout to a log, and rebuilds the result from it afterwards: `verified_deck`
scans the captured tail for a `MEDIA:` line, opens the named `.pptx` as a zip to
confirm it carries slide parts, and prints three lines the caller reads as the
sub-agent's whole reply. Everything the agent did on the way there -- its
reasoning, its tool calls, the pages it rejected -- exists only as terminal text
in a log file.

Three costs, and they are what ACP buys back:

- **The reply is a scrape.** `maxOutputChars: 60000` of console output is the
  channel; a progress bar or a wrapped path is indistinguishable from content.
- **There is no live view.** A twenty-page deck takes tens of minutes and says
  nothing until it exits. The host's ACP backend
  (`raven/agent/subagent/backends/acp_agent.py`) renders `session/update`
  notifications as they arrive, which is the whole difference from the cli one.
- **A process per task.** The connection is pooled instead, so the handshake and
  the engine build are paid once.

## 2. The claim this design rests on, and its evidence

**A checkout that already translates spine events onto a wire needs a second
translator, not a port of the host's ACP subsystem.**

Evidence, all in this tree:

- `raven/spine/` is complete: `events.py` (`TurnStarted` / `TurnEnded` /
  `TurnFailed` / `ToolEvent` with `ToolPhase` / `Text` / `StreamDelta` /
  `Reasoning` / `MediaOut` / `Notice` / `Usage` / `EpisodeStart`),
  `delivery.py` (`Outlet`, `Capabilities`, `SupportsStreaming`, `DeliveryHub`),
  `scheduler.py` (`Scheduler`, `OriginPools`, `TurnHandle`), `runner.py`,
  `turn.py`.
- `raven/tui_rpc/spine.py` holds `TuiOutlet`, an object whose entire job is
  mapping those events onto one wire vocabulary, plus `build_tui`, the assembly
  that mounts it: `DeliveryHub()` -> `hub.register(outlet)` ->
  `Scheduler(runner, OriginPools(...), sink)`.
- `raven/cli/_repl_spine.py` holds `CliOutlet` / `build_repl`, the same pattern a
  second time for the console. So an outlet-plus-assembly per surface is the
  established shape here, not an invention.

The host's own ACP layer is *not* the model to copy structurally. It sits on
`build_rpc_stack`'s outbound `send_frame` and re-parses TUI wire events
(`token.delta` -> `agent_message_chunk`), and its module docstring says why:
`spine/delivery.py`'s `make_hub_sink` drops `TurnStarted` / `TurnFailed` /
`TurnEnded`, so an object registered only as an outlet never sees a turn end and
a suspended `session/prompt` would never be answered. It also states the cost:
"this parses a dict that was serialised one layer up, and whatever `RpcOutlet`
dropped is dropped for good".

Here there is no `raven/rpc/` to sit behind, so the double translation is not
available -- and not wanted. `AcpOutlet` reads the spine events directly, and the
lifecycle problem the host names is solved the way `build_tui` already solves it:
**a custom sink**, not `make_hub_sink`. `_make_tui_sink` intercepts `TurnEnded` /
`TurnFailed` before the hub and fires `message.complete` / `error` after the
render barrier. `_make_acp_sink` does the same and settles the prompt's future.

## 3. Event mapping

`session/update` carries exactly five `sessionUpdate` values in the reference
translator (`raven/acp/updates.py`), and each has a spine source here.

| spine event | ACP `sessionUpdate` | reference |
|---|---|---|
| `StreamDelta.delta` (streaming reply) | `agent_message_chunk`, `content: {type:"text"}` | `updates.py:189-193` (`token.delta`) |
| `Text.content` (non-streamed reply) | `agent_message_chunk` | `updates.py:189-193` (a `Text` reaches the TUI as one `token.delta`, `tui_rpc/spine.py:178-183`) |
| `Reasoning.content` | `agent_thought_chunk` | `updates.py:195-199` |
| `ToolEvent(phase=START)` | `tool_call` | `updates.py:358-397` |
| `ToolEvent(phase=COMPLETE)` | `tool_call_update` | `updates.py:400-436` |
| `MediaOut.media` | one `agent_message_chunk` per file, `content: {type:"resource_link"}` | `updates.py:237-304` |
| `Usage` (via `TurnEnded.usage`) | `usage_update` | `updates.py:330-355` |
| `Notice` | eaten | `updates.py:493-507` keeps only `action_blocked`, which has no spine `NoticeKind` |
| `EpisodeStart` | eaten | `updates.py:224-234`, "a TUI collapsing boundary with no ACP counterpart" |
| `TurnEnded` | `session/prompt` result `{"stopReason": "end_turn"}` | `updates.py:210-216` |
| `TurnFailed(cancelled=True)` | `{"stopReason": "cancelled"}` | `updates.py:520-521` |
| `TurnFailed(cancelled=False)` | one explanatory `agent_message_chunk`, then `{"stopReason": "end_turn"}` | `updates.py:510-529` |

Field-level decisions carried over verbatim rather than re-derived:

- `tool_call` uses `status: "in_progress"`, never `pending` -- by the time the
  event exists the call is running, and a pending row that never changes reads as
  a hang (`updates.py:358-371`).
- `tool_call` omits `rawInput`. It would carry the whole `exec` command line into
  a transcript the client persists (`updates.py:366-370`). `title` and
  `locations` carry what a client needs to draw the row; both come from a local
  copy of `raven/acp/tool_kinds.py`, so `kind`, the absolute-path requirement and
  the `MAX_LOCATIONS` cap match.
- `tool_call_update` status is `completed` / `failed`, and the result preview is
  a `{"type": "content", "content": {"type": "text", ...}}` block capped at
  `MAX_RESULT_PREVIEW` with a `\n[truncated]` marker (`updates.py:400-436`).
  Spine's `ToolEvent` has no `ok` field, so the same convention the loop uses is
  read off the preview: a model-facing failure starts with `Error`.
- `usage_update` needs `used` and `size` and is omitted unless both are real; an
  update of zeroes is not the same statement as no update (`updates.py:330-355`).
  Spine's three-field `Usage` has no window size, so the size comes from the
  loop's configured `context_window_tokens`.
- A prompt is **never** answered with a JSON-RPC error. Measured on codex-acp
  from the other direction: an error in reply to a turn-shaped request makes
  clients tear down the whole turn (`methods.py:17-21`). A refused turn still
  gets a `stopReason`, with the reason as message content.

## 4. Where `AcpOutlet` mounts

`raven/acp/spine.py`, as the twin of `raven/tui_rpc/spine.py`:

    build_acp(agent_loop, emit, channel="acp")
      -> hub = DeliveryHub(); hub.register(AcpOutlet(channel, emit, sessions))
      -> Scheduler(AgentTurnRunner(agent_loop, stream=True, inline_tool_stream=True),
                   OriginPools(user=1, system=1),
                   _make_acp_sink(hub, outlet, channel, sessions))

`stream=True` (as the TUI does, not as `build_repl` does) because live delivery
is the reason to be on this transport at all.

`emit` is a *synchronous* one-frame writer, the same choice the reference makes
(`outbound.py:70-75`): the frame writer is a write plus a flush with no
suspension point, which is what keeps the order of frames on the wire equal to
the order they were produced in.

`AcpOutlet` needs the conversation-id -> session-id map, because every
`session/update` frame is addressed. Spine's `conversation_id` is the raven
session key; the ACP `sessionId` **is** that key (the reference's choice at
`methods.py:294-299`: "one identity rather than two"), so the map is the identity
function and the lookup exists only to refuse a frame for a session this
connection has released.

## 5. Methods

Served:

| method | behaviour |
|---|---|
| `initialize` | `protocolVersion` via `negotiated_version` (tolerant of `"1"` / `1.0`), `agentCapabilities`, `authMethods: []`, `agentInfo`. Re-initialising is allowed. |
| `session/new` | Validates `cwd`, refuses a non-empty `mcpServers`, mints `acp:<chat_id>`, builds this session's engine, answers `{"sessionId": ...}`. |
| `session/prompt` | Reads content blocks, stages material, runs one turn, answers `{"stopReason": ...}`. |
| `session/cancel` | Cancels the lane, then settles the pending prompt as `cancelled` -- in that order, and unconditionally (`methods.py:620-643`). |
| `session/close` | Cancels a running turn, settles its prompt, and releases the session's engine. |
| `session/update` | Outbound notification only. |

The launcher's one fatal condition softens into a turn-level one. `run.py` exits
with "no source material"; here that is an `agent_message_chunk` naming the fenced
block and a `stopReason: end_turn`, because a prompt is never answered with an
error. What it is *not* is silent: a first turn with nothing to ground a deck in
cannot produce one, and the reply says so.

Refused with `-32601`, and declared as unsupported in `initialize` so no client
routes work into them:

| method | why |
|---|---|
| `session/load` | `loadSession: false`. Replaying a transcript as `session/update` notifications needs a transcript renderer this checkout does not have; declaring it true would show a person an empty history for a conversation that has one. |
| `session/resume` | `sessionCapabilities` omits `resume`. This also makes `AcpAgentBackend.is_stateful` false, so the host will not bind an instance handle to a session it cannot reopen -- which is the honest answer, because a deck's workspace is the session and a resumed one would have to find it again. |
| `session/list`, `session/delete` | Not declared. Each declared capability is a method that must then work. |
| `session/set_mode`, `session/set_config_option` | No modes; the model is fixed by the rendered config, not switchable per session. |
| `session/request_permission` (outbound) | Not sent. There is no interactive approver behind this process, so a request would be a promise nobody keeps. `ExecTool` without an `ApprovalResponder` fails closed, which is exactly what `raven agent -m` does today -- unchanged behaviour, stated rather than discovered. |
| `elicitation/create` (outbound) | Not sent, and `ask_user` is left as it is: with no `QuestionBroker` it returns `"Error: ask_user not configured"`, a tool error the turn survives. |
| `authenticate` | `authMethods: []` is a positive statement that none is needed; a client calling it anyway is told what it declared rather than given a method-not-found it would read as a version mismatch (`methods.py:229-237`). |

The rule behind the whole table is the one the upstream commit
`06a96eea feat(agent): route an acp sub-agent's question to the user` was written
for: a capability declared and not served is worse than one absent. Measured
there, from the client side -- raven not declaring `elicitation` made
claude-agent-acp 0.66.0 put `AskUserQuestion` into `disallowedTools` and
codex-acp 1.1.14's `handleUserInput` return `{answers: {}}`. The declaration is
read, and a false one silently disables tools.

Nothing is answered before `initialize`, including `session/new`: the client's
capabilities decide how a question would be routed, and a session built without
them would have to guess (`methods.py:220-228`).

## 6. How material arrives

The host's ACP backend sends exactly one content block:

    {"sessionId": ..., "prompt": [{"type": "text", "text": task}]}

(`acp_agent.py:572-577`). So the text channel is the only one a real client uses
today, and the fenced-JSON contract in `subagent.json` keeps working unchanged.
The prompt reader accepts the three block types ACP defines for material anyway
-- `text`, `resource_link` (Zed's shape for an `@`-mention), and `resource` with
inline text -- because they cost one branch each and are the natural channel the
moment a client offers one. `promptCapabilities` declares only what is honoured:
`image: false`, `audio: false`, `embeddedContext: true`.

`cwd` is the second, better channel, and it is what the transport adds:
`AcpAgentBackend` passes `cwd = self.cwd or str(workspace)` (`acp_agent.py:517`),
so with no `cwd` in the manifest the session's directory **is the host's live
session workspace**. That is the same directory `run.py`'s `deliver()` copies the
finished deck into today, via `Path.cwd()` -- but now it is a protocol parameter
rather than an inherited process property, so it is known at `session/new` and
can be reported back.

Staging happens at `session/prompt`, not `session/new`, because the paths are in
the prompt. That is a gain over the launcher: a second turn on the same session
can add material.

The layout is unchanged. Each session gets

    <instance data dir>/acp_jobs/<session chat id>/
        materials/      staged copies of the named sources
        deck/           the ppt tool's own tree (Project.root)
        out/            where the deck is published

The instance data dir is the config file's own parent, which is how every other
runtime directory here is placed (`get_runtime_subdir`) -- so the launcher, which
renders its config under the state root, gets its jobs there too and there is no
second path to keep in step. `raven acp --jobs-root` overrides it.

and **that directory is the session's engine workspace**. This is why the engine
is per session rather than per process: `Project.root` is `workspace / "deck"`
and its docstring is explicit -- "a workspace is one task: the launcher makes a
directory per spawn and the agent is fenced inside it". Two concurrent sessions
sharing one workspace would share one `deck/`. `readyTimeoutMs` is raised in the
manifest because `session/new` is bounded by that same budget
(`acp_agent.py:527`, `706`) and now does the engine build.

After the turn, the same verification the launcher does runs here: find the
`.pptx` written under this session's `out/` (preferring the one a `MEDIA:` line
names), confirm it opens as a zip carrying slide parts, count the slides, copy it
to the session's `cwd`. It is reported as two updates -- a text
`agent_message_chunk` carrying the same three lines the launcher prints, and a
`resource_link` chunk naming the file. The text matters because the host builds
the sub-agent's reply from `agent_message_chunk` text alone
(`_ANSWER_UPDATES = ("agent_message_chunk",)`, `acp_agent.py:60`) and raises
`AcpEmptyTurnError` for a turn that produced none.

## 7. Gaps in this tree

Stated so they are decisions rather than surprises.

1. **No framing module.** The host imports `raven.agent.acp.protocol` for
   `encode` / `decode`; that package is not vendored. A local `protocol.py`
   reproduces it -- newline-delimited JSON-RPC 2.0, `ensure_ascii=False`, one
   object per line -- plus the agent-direction additions the host keeps in
   `raven/acp/protocol.py`: a string-or-int `id`, `data` on an error, the
   ACP-assigned codes (`-32000`, `-32002`, `-32800`), and
   `negotiated_version`.
2. **No `raven.agent.workdir.validate_override`.** `cwd` validation is local and
   narrower: absolute, existing, a directory. The refusals the host's version
   adds (the agent home and its ancestors, the memory / skills / sessions
   subtrees) exist to stop a per-turn `git add -A` checkpoint from committing
   provider keys; this surface runs with `interactive=False`, so no checkpoint
   runs, and `cwd` here is only a delivery destination.
3. ~~**`discover_vendored_rows` cannot see an acp manifest.**~~ **Resolved in the
   host; this folder shipped the workaround anyway.** When this was written the
   scan validated every `*/subagent.json` as `ThirdPartyCliSubagentConfig`, whose
   `kind` is `Literal["cli"]`, so a lone acp manifest was skipped with a warning.
   The fix was called out here as "a one-line edit in the *host*
   (`raven/agent/subagent/vendored_agents.py`) and out of scope", and the
   workaround was a second file, `subagent.acp.json`, registered by
   `install.py --acp`.

   That edit has since landed -- the scan now picks the schema from the
   manifest's own `kind` -- and `raven-code` and `raven-research` both ship a
   single `subagent.json` declaring `kind: "acp"`. The workaround outlived the
   constraint it was for: this folder carried two manifests, two descriptions
   that drifted apart, and an installer flag to choose between them. It is one
   manifest now, like the other three.
4. **`session/update` for a turn nobody prompted.** The host's translator
   correlates a turn id so a runtime-submitted turn's ending cannot answer this
   prompt (`updates.py:540-550`). Nothing here submits background turns -- no
   cron service, no sentinel injection on this surface -- so correlation is one
   flag per session (a prompt is in flight or it is not) and a second concurrent
   `session/prompt` on one session is refused, as the reference refuses it.

## 8. Shutdown, in three steps

Written after the fact, because the order was wrong first and a test caught it.

    sessions.settle_all()   # answer every pending prompt; touch no engine
    await _drain(tasks)     # let the handlers return through their own code
    await sessions.aclose() # now tear the engines down

Each step is where it is for a reason that only shows up when it is moved:

- **Settling cannot tear down.** A suspended `session/prompt` resumes into code
  that still reads its session's engine -- the deck report needs the outlet. With
  the release folded into step 1, that handler woke up holding `engine is None`
  and died with an `AttributeError`, taking its reply with it.
- **Draining cannot come last.** EOF can arrive while a `session/new` handler is
  still awaiting its engine build. Closing the sessions before that handler
  returns finds an empty table, and the engine it goes on to register is left with
  nothing to release it -- so its MCP subprocesses outlive the process.
- **Draining cannot come first either**, which is what the settle is for: a
  handler suspended on a turn's future would otherwise sit out the whole grace
  period.
- **And a settle sweep is not enough on its own.** A prompt frame read before EOF
  whose handler has not run yet would open its turn *after* the sweep passed, and
  wait for a settle that is never coming. So `settle_all` also latches
  (`AcpSession.closing`), and `begin_turn` on a closing session hands back an
  already-answered future. This is the same latch the reference implementation
  describes on `UpdateTranslator.close`.

The deck report is gated on `stopReason == "end_turn"` for the same family of
reasons: a cancelled turn was interrupted mid-render, and announcing that file as
published would hand the caller a deck the agent never finished reviewing.

## 9. What shipped

| Phase | Contents |
|---|---|
| 1 | This document. |
| 2 | `protocol.py`, `capabilities.py`, `tool_kinds.py`, `session.py`, `spine.py` (`AcpOutlet` + `build_acp`), `materials.py`, `methods.py`. |
| 3 | `stdio.py`, `server.py`, `engine.py`, `raven/cli/acp_commands.py` (`raven acp`), `subagents/raven-ppt/run.py --acp`, `subagents/raven-ppt/subagent.json` (`kind: "acp"`). |
| 4 | `outbound.py`, `questions.py`: the agent-to-client direction, which nothing above had. `ask_user` is answerable over ACP because of it. |

Tests: `tests/test_acp_{spine,methods,materials,server,stdio}.py` and
`tests/test_cli_acp_commands.py`. None starts a subprocess or opens a socket: the
frame loop is driven over an in-memory `StreamReader`, the engine arrives through
the `engine_factory` seam, and the stdout-claiming tests move real descriptors
onto temp files and restore them.

Verified beyond the unit tests:

- `subagent.json` validated against the host's own `ThirdPartyAcpSubagentConfig`,
  its write-path rejector, the discriminated union the roster is built from, and
  `build_third_party_backend` -- which yields an `AcpAgentBackend` with `cwd=None`
  (so the host's session workspace is used) and `stateful=False` (so no instance
  handle is bound to a session that cannot be reopened).
- `discover_vendored_rows` run over the folder tree, which now returns
  `Raven-PPT kind=acp enabled=True` beside `Raven-Code` and `Raven-Research`.
- A real `raven acp` process, handshaked over a pipe: two clean frames on stdout,
  diagnostics on stderr, exit 0 -- and the same through `run.py --acp`, which
  rendered the config and exec'd into it.
