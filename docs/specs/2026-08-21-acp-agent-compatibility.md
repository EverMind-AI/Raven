# Raven as an ACP agent: what works, what does not, and why

`raven acp` serves the [Agent Client Protocol](https://agentclientprotocol.com)
over stdio, so an editor can spawn Raven the way it spawns any other coding
agent. This document is the compatibility matrix: every surface of the protocol,
with the honest state of it.

The reason it exists in this shape is that the worst failure mode available to an
ACP agent is a *declared* capability with nothing behind it. The client routes
work to a method that answers with an error, and the turn stalls on a promise
nobody will keep. So the rule followed throughout is that a capability is
declared only when something honours it, and everything else is written down here
rather than left for a user to find.

Spec baseline: **schema 1.20.0**, vendored at `tests/fixtures/acp/schema-v1.json`
with its sha256 pinned in `VERSION.json`. Every outbound frame the unit tests
build is validated against it.

## Handshake

| Surface | State | Notes |
|---|---|---|
| `initialize` | **Supported** | Tolerant by construction: unknown params ignored, and a `protocolVersion` of the wrong type is read for intent (`"1"`, `1.0`) rather than rejected. An unreadable one gets version 1 back, which is what lets the client decide whether to disconnect. |
| `protocolVersion` | **1** | Not 2: `schema/v2` is a draft, no shipping agent answers 2, and the official guidance is to gate a v2 path behind both version negotiation and a feature flag. |
| `agentInfo` | **Supported** | Read from the installed distribution, so it cannot claim a version this build is not. |
| `authenticate` / `authMethods` | **Not needed** | `authMethods: []` is a positive statement: no authentication is required, so a client may go straight to `session/new`. A client that calls `authenticate` anyway is told that, rather than given a method-not-found it would read as a version mismatch. |
| `logout` | **Not supported** | `agentCapabilities.auth` is empty, so it is never called. There is no session to end. |

## Sessions

| Surface | State | Notes |
|---|---|---|
| `session/new` | **Supported** | `cwd` is checked the way Raven checks its own working directories: it must be absolute, and it may not be the agent home, an ancestor of it, or inside the memory / skills / sessions subtrees. The last is not arbitrary -- the per-turn checkpoint runs `git add -A` over the working directory, so a session rooted at `~` would commit provider keys into a shadow repository. Each refusal is `-32602` with the reason attached. |
| `session/new` `mcpServers` | **Refused when non-empty** | MCP is connected once per process, lazily, and nothing scopes a server to one session. Accepting the field silently would leave a client believing its tools are available for the rest of the session. An empty array is the normal value and is accepted. |
| `session/new` `additionalDirectories` | **Not declared** | The capability is an object in the schema, so declaring it means implementing it. |
| `sessionId` | The Raven session key | One identity rather than two (`acp:<chat_id>`). A second id space would need a map that survives a restart to be worth anything. |
| `session/load` | **Supported** | Not a getter: the transcript is replayed as `session/update` notifications *during* the request, so a resumed session is drawn by the same client code as a live one. The working directory comes from the client rather than from storage, because a project moves. Reloading a session this connection already holds reuses its stream instead of opening a second one. |
| `session/load` for an unknown id | **`-32002`** | `session.resume` is forgiving in a way the protocol is not: an unknown id makes it mint a fresh session and answer with that. The id it returns is compared against the one asked for, which is what turns the fallback into an error -- a client silently handed a new session shows a person an empty transcript for a conversation that had one. |
| What a replay loses | Attachments | `_map_to_wire` flattens a multimodal user message to the text of its text blocks, so an image attached three turns ago replays as the words around it. Recovering it means reading the raw messages and then re-reading files that may no longer exist. |
| A replayed tool call's status | `pending`, then `completed` | The only place `pending` is used. On a live call the status is `in_progress` because the work is running; on a replay the work is over and the entry carrying its outcome follows. A row left `in_progress` would show a spinner for a call that finished last week, and `completed` on the announcement would claim an outcome before the entry that carries it. |
| A replayed diff | Text, not a `diff` block | The stored record is a rendering and the file's contents at the time are gone, so a structured block would need a `newText` that would have to be invented. |
| Replay size | 500 messages, 16 KB each | Truncated from the front, so what is dropped is what a scrollback would have dropped -- and the truncation is announced in the transcript, because a history that appears to begin mid-thought reads as corruption. |
| `session/list` | **Supported** | Newest activity first. Read from the session manager rather than through the internal `session.list`, because `SessionInfo.cwd` is required by the schema and that method's wire shape does not carry it. Not paginated: every match is in the response and `nextCursor` is omitted, which says "there is no more" rather than "ask again". |
| A session with no recorded directory | **Omitted from the listing** | `cwd` is required, and inventing one would tell a client the session ran somewhere it did not. The number omitted is logged, because a listing quietly shorter than the session directory is not something anyone notices until they go looking for a conversation. |
| `session/list` without an engine | **Empty** | A fresh `SessionManager` caches nothing, so a listing built from one would describe an object about to be discarded. Also true of a session minted and never used: sessions are lazy and write no file until their first turn, so a brand-new session does not appear until it has said something. |
| `session/resume` / `close` / `delete` | **Not supported** | All three are stable in the schema, all three need their own capability declaration, and Zed uses none of them. `sessionCapabilities` declares `list` and nothing else. |
| Two prompts on one session | **Refused** | A session's updates carry no request correlation, so two prompts in flight produce a single interleaved stream that cannot be split apart. Raven's own ACP *client* documents this as a certainty from the other side; the mirror holds here. |
| Several sessions on one connection | **Supported** | Each has its own subscription and its own turn slot. |

## Prompts

| Content block | State | Notes |
|---|---|---|
| `text` | **Supported** | |
| `resource_link` | **Supported** | The block an editor sends for every file mention, and it is gated by no capability at all -- `promptCapabilities` covers only `audio` and `embeddedContext`. A `file://` URI is turned back into a path (percent-decoding undone) so the agent's own file tools can act on it; anything else is passed through as a URI. Named in the prompt rather than read here: reading it would make a mention a silent file read, and the agent's read goes through the tool that reports it. |
| `resource` (embedded text) | **Supported** | `embeddedContext: true` rests on this case. |
| `resource` (embedded blob) | **Named, not inlined** | Base64 in a prompt is tokens spent on nothing. |
| `image` | **Supported** | Written into `<workspace>/uploads/` and handed to the turn as a workspace-relative media path -- the same spelling every file tool takes, and one that survives a deployment with `restrict_to_workspace` on. 20 MB ceiling, matching `fs.upload`. |
| `audio` | **Not supported** | `promptCapabilities.audio: false`. There is no audio path on the prompt side. If one arrives anyway it is named in the prompt rather than dropped, so a person who spoke learns the words did not get through. |
| A block Raven cannot read | **Costs only itself** | One unusable attachment never fails the prompt. |

## Turn output

| Surface | State | Notes |
|---|---|---|
| `agent_message_chunk` | **Supported** | Both paths: the streamed reply and the non-streamed one. |
| `agent_thought_chunk` | **Supported** | |
| `tool_call` | **Supported** | Initial status is `in_progress`, not `pending`: pending means "not started", and a pending row that never changes reads as a hang. `kind` is mapped for every tool Raven registers; anything else, including the `mcp_<server>_<tool>` names an MCP server brings at runtime, is `other`, because a wrong icon is worse than a generic one. |
| `tool_call.locations` | **Supported** | Resolved to absolute against the session's working directory, as the spec requires. A path that cannot be made absolute is dropped rather than sent relative. |
| `tool_call.rawInput` / `rawOutput` | **Not sent** | It carries a tool's arguments verbatim, and for `exec` that is the whole command line. The title carries what a client needs to draw the row, and it is redacted. |
| `tool_call_update` status | **Always `completed`** -- known inaccuracy | `ToolEvent` carries no success flag, and the preview it does carry comes from `display_text or model_text`, so a tool that writes a friendly message on failure is indistinguishable from one that succeeded. Guessing from the text would mislabel both directions. The fix is a status field at the emit site, which is a change to the spine's event vocabulary rather than to this mapping. |
| `ToolCallContent` `diff` | **Supported** | Structured `{path, newText, oldText?}` carried from the write tools, which hold both versions of the file. `oldText` is omitted only for a file that did not exist -- the schema defines its absence as "new file", so omitting it for a file whose previous content is merely unavailable would render every line of a rewrite as an addition. |
| `ToolCallContent` `terminal` | **Not supported** | See `terminal/*` below. |
| `stopReason` | `end_turn`, `cancelled` | `max_tokens` and `max_turn_requests` have no source in Raven and are never sent. |
| `refusal` as a stop reason | **Not sent** | It would come from the runtime's `action_blocked` notice, and that notice carries no turn id: `_run_turn` has none to give it, and the runtime shares a lane with the client's turn (see `_owns_lane`), so a refusal seen on this session cannot be shown to belong to this prompt. Latching it anyway made a *foreign* turn's block the reported outcome of a turn that completed normally, which is a false statement about this turn; reporting `end_turn` is merely a less specific true one. The refusal itself is not lost -- it is delivered as message content, which is what the person reads. Restoring it needs a turn id on the notice, which means threading one through the agent loop. |
| A failed turn | **Explained, then ended** | A prompt is never answered with a JSON-RPC error. Measured from the other direction on codex-acp: an error in reply to a turn-shaped request makes clients tear down the whole turn. The failure is surfaced as message content and the turn ends with a stop reason. |
| `plan` | **Not sent** | `PlanEntry.priority` is required (`high`/`medium`/`low`) and Raven's DAG has no source for it. A plan would have to be invented. |
| `usage_update` | **Supported** | `used` and `size` are the context-window numbers, `cost` is the estimated dollar figure. Sent on the turn's completion, before the prompt is answered, so the client has it while the turn still exists. Withheld entirely when the window numbers are not real: a `size` of zero has a client drawing a full bar or dividing by it, and an update of zeroes is not the same statement as no update. A cost of zero, by contrast, is reported -- a cached reply cost nothing, which is different from not knowing. |
| `session_info_update` | **Not sent yet** | The title is available (it is what `session/list` reports), but nothing changes it mid-session today. |
| `available_commands_update` | **Not supported** | The only catalogue Raven has reflects Typer CLI verbs. The vocabulary is wrong, and a wrong command list is worse than none. |
| `current_mode_update` / `session/set_mode` | **Not supported** | Raven has no mode concept. Declaring a mode with no state behind it is worse than not declaring one. |
| Files the reply carried | **Supported** | Each file arrives as one `agent_message_chunk` whose content is a `resource_link`, one chunk per file because a chunk carries a single content block. A link and not an `image` or `audio` block even for a picture: those carry base64 `data`, which would mean reading the file inside a translator that is pure on purpose, and a client that spawned this agent can open the path itself. `mimeType` is forwarded exactly as the emit site declared it, which today is `application/octet-stream` for every file -- the RFC default for "unknown", not a detection result, so a client that needs the real type should sniff the extension. Deriving one here would be the translator claiming knowledge the event does not carry, and a wrong type sends a client to the wrong viewer. Capped at 32 files per turn. A path that cannot be made absolute is named in text instead of linked: a relative `file://` URI resolves against the client's own directory, so it opens nothing or the wrong file. |
| A sub-agent's output | **Untagged** | The tag exists in the translator and nothing sets it here: this build has no direct sub-agent chat, so every turn on a session's subscription is the main agent's and there is no second speaker to distinguish. The `_meta["raven.target"]` path stays because the alternative is a translator that silently drops the tag once a lane can carry one. |

## Cancellation

| Surface | State | Notes |
|---|---|---|
| `session/cancel` | **Supported** | Cancels the work first, then answers the pending prompt with `cancelled` -- last, so a late event cannot settle it with a different reason after the client has been told. Answered unconditionally, including when there was nothing to cancel: a cancel arriving before the scheduler accepted the turn still has a prompt to answer. |
| Tool-level cancellation | **Supported** | A cancelled turn kills the shell's whole process group, not just the shell. Measured: with the group kill, a child process stops writing the instant the turn is cancelled; without it, the child keeps writing to the workspace for the life of the agent. |
| `$/cancel_request` | **Ignored, safely** | Protocol-level and explicitly optional; the spec says a receiver MAY act on it. |
| Client leaves mid-turn | **Answered, then unwound** | On EOF the pending prompts are settled as `cancelled` and the handlers return through their own code, releasing their turn slots before the engine is torn down. |

## Permissions

| Surface | State | Notes |
|---|---|---|
| `session/request_permission` | **Supported** | Sent for the command families listed below. |
| Option kinds offered | `allow_once`, `reject_once` | Not `allow_always` / `reject_always`. Raven has no persistent policy store -- the approval broker takes allow or deny, and its denial memory is cleared at every turn boundary. Offering an "always" a client would render as a saved preference is a lie. |
| A refusal | `selected` with a reject option id | `RequestPermissionOutcome` has exactly two variants, `cancelled` and `selected`. There is no `denied`. |
| A client that does not answer | **Refusal after 5 minutes** | Not 35 seconds, which is the terminal broker's ceiling because a terminal overlay owns a visible countdown. A person reading a diff in an editor is not that. |
| A client that answers with an error | **Refusal** | |
| A client that answers with an unknown option id | **Refusal** | Options are minted per request and the answer is checked against that set, so a stale id, an invented one, or a synthesised `allow_always` is refused rather than believed. |
| A client that answers `cancelled` | **Refusal** | The one that is not misbehaviour: a client cancelling a turn MUST answer every pending permission this way. |
| Command families that ask | publish, install, remote exec, credential, destructive VCS, fetch-with-side-effect | `git push`, `npm install`, `ssh`, `gh auth`, `git reset --hard`, `curl -o` and their relatives, including behind `sudo`, `env` and `sh -c`. |
| Command families that do not ask | everything else | A build, a test run, a formatter, a file edit and a plain `curl` of a documentation page all run unannounced. The line is "hard to undo from outside this directory", not "dangerous": a prompt on every command trains the reader to approve without looking, which costs more than it buys. |
| **A sub-agent's commands** | **Refused, not asked** | The families are declared per surface rather than per tool, so a sub-agent's own `ExecTool` inherits them: a delegated `git push` no longer runs unannounced. What it gets is a refusal with a reason, because that tool has no approval responder and a tool that cannot ask fails closed. Asking on a sub-agent's behalf needs a lane's conversation id routed into a task that outlives its turn, which is its own change; until then, refusing beats acting in silence. |
| **A sandboxed session** | **Asks about the same families** | A microVM contains what a command does to files, so a sandboxed turn skips the deny list and the contained families (a delete, a power-off) -- that is what running one is for. It does not contain a push, an install or a connection to another machine, so those still ask. The classification used to be skipped whole on the sandbox flag, which made the safer configuration prompt LESS than the plain one for exactly the operations the sandbox has no say over. |
| `ask_user` with `elicitation` declared | **`elicitation/create`, form mode** | The route that fits: a message plus a one-field schema, answered with a value. The field is an enum when the question has choices and a plain string otherwise, which makes this the only route that can carry an answer nobody listed in advance. Branching on `elicitation.form` and not on the group: a client can declare the group and support only `url` mode, which is for sending somebody to a web page. |
| `ask_user` without it, with choices | **`session/request_permission`** | A worse fit, used because `toolCall` is required and a bare question has none. The synthesised one is marked in `_meta` (`raven.synthesisedToolCall`) rather than disguised, its `kind` is `other` because the kinds describe tools and this is not one, and every option is `allow_once` because the kinds describe authorisation and none is being given. Capped at 8 choices. |
| `ask_user` without it, free text | **Shown, then defaulted** -- known limitation | A permission response carries an option id and nothing else, so there is no channel for typed text. The question is put on the wire as an ordinary agent message, so the person sees it and can answer in their next prompt, and the tool falls back to its default. Stated here because a silently defaulted question reads as an agent that did not listen. |
| An unanswered question | **Defaulted, not hung** | Every path answers the runtime's broker, including every failure path. Left unanswered it treats the question as "wait longer" and falls back on its own after ten minutes -- during which the tool call is blocked and a client shows a spinner, then a reply that ignores what it asked. |

## Configuration

| Surface | State | Notes |
|---|---|---|
| `session/set_config_option` | **Supported, `category: "model"` only** | This is the stable channel. `session/set_model` does not exist in the schema and neither does `models.availableModels`; both appear in older material, and an agent waiting for either would never be asked to switch a model. |
| `configOptions` on `session/new` | **Sent when there is one** | So a client can put a model picker in the session menu without a second round trip. Absent rather than empty when nothing is configured and nothing is running: an empty list is a menu that opens onto nothing. |
| Scope of a model change | **Process-wide** -- said in the option's own description | Raven writes `agents.defaults.model` and reassigns the live loop; there is no per-session model, so a connection with two sessions open changes both. The protocol's shape says otherwise, so the caveat goes in `description`, which the schema declares as text for the client to display -- a person reads it where they make the choice. |
| A model change during a turn | **Not refused here** | `config.set` in this build has no busy guard, so a switch mid-turn is accepted and takes effect on the next model call rather than being rejected. A typed refusal from `config.set` does travel out intact when there is one -- a client can act on a code, and flattening it to an internal error leaves a person retrying something that will keep failing -- but there is no in-turn refusal to travel. |
| Which models are offered | Authenticated providers, plus the one in use | The second half is not a courtesy. `authenticated` reports whether raven's own config holds a credential, and a working installation can be running on one from the environment -- measured on a machine where every provider reported false while `anthropic/claude-opus-4-5` was answering. Filtering on that flag alone hid every model that worked. Capped at 40 per provider. |
| Other configuration | **Not exposed** | Raven has more hot-changeable config, but a selector for each would put a settings panel in a session menu. The ones worth being there are the ones a person changes mid-conversation. |

## Credentials in the payload

An ACP payload is rendered in an editor and often kept in its transcript, so a
tool title, a permission prompt and an error message are publishing surfaces.

Every row below names the surface as it behaves in the live translator, not as
the redactor behaves in isolation. That distinction is not pedantic: the first
version of this table described a redactor that the publishing path never
called, so the document was right about the module and wrong about the wire.

| Channel | Handling |
|---|---|
| A command line in a prompt title | Redacted (14 patterns, capture group only, so the shape survives and the row stays readable). |
| A `tool_call` title on the wire | Redacted in `_tool_call`, which is the last point before the frame leaves. |
| A `tool_call_update` result preview | Redacted **before** the 64 KiB cut, so a credential that straddles the cut cannot leave its head behind as ordinary text. |
| A terminating error's message and detail | Redacted. |
| A blocked action's notice detail | Redacted; a refusal quotes what was refused, which is often the command line. |
| A tool's arguments | Not sent at all -- see `rawInput` above. |
| An internal error's `data` | The traceback tail is stripped (twelve lines of absolute paths, sometimes of argument values) and whatever survives is redacted. |
| An `mcpServers` `env` dict | Refused with the field. |
| A `Diff` block's `oldText` / `newText` | **Not redacted**, deliberately. The client draws or applies this content, so a redacted diff is a wrong diff -- it would show and could write `[redacted]` into the file. A diff of a credential file therefore publishes it. Recorded rather than fixed because the fix is to not send diffs for such files, which needs a rule about which files those are. |
| Model and reasoning text | Not redacted. It is the answer the person asked for, and a redactor cannot tell a quoted secret from a discussion of one. |
| **What is not caught** | A high-entropy string with no label and no vendor prefix. It is indistinguishable from a hash, a build id or a commit sha, and redacting those would break every row that legitimately shows one. This is not a secret scanner; containment is the sandbox's job. |

## Filesystem and terminal

| Surface | State | Notes |
|---|---|---|
| `fs/read_text_file` / `fs/write_text_file` | **Not called** | Declining to call a client capability is entirely conformant. The reason not to is the dirty-buffer problem below. |
| `terminal/*` | **Not called** | Raven runs commands through its own sandbox executor, which is where the approval check and the process-group kill live. Routing them through the client would move both outside Raven's control. |

## The dirty-buffer problem

This one has no fix, and is written down rather than worked around.

`rpc/files.py` states Raven's invariant as *"the viewer may render exactly what
the agent may read"*. Against an editor that invariant does not hold: the model
reasons about the version on disk, and the person is looking at a modified
buffer. Symptoms:

* `edit_file`'s exact `old_text` match fails on an edit that obviously should
  apply, because the text the person can see was never saved;
* `write_file` silently overwrites unsaved work, and the diff it reports is
  computed against the disk version, so the "before" shown is not the before the
  person had.

Reading through `fs/read_text_file` would fix the first symptom and make the
second worse: the agent would then read the buffer and write the file, so its
own read and its own write would disagree about what the file is.

## Multiple windows

An editor spawns one process per window, and each builds its own engine against
the same agent home. Several engines then run their own cron services and
compete for the browser profile lock. This is a consequence of the process
topology -- an ACP agent *is* a stdio child of the editor, so there is nothing to
mount onto -- and it is not solved.
