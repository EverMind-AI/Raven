# ACP sub-agent elicitation pass-through - design

Date: 2026-08-25
Status: proposed

## Goal

Let an ACP sub-agent ask the user a question and get an answer back, rendered on
the surface the user is already looking at (TUI overlay, `ui/` sheet) through the
`clarify.request` contract both frontends already implement.

The protocol channel for this is `elicitation/create`. It is gated on a client
capability raven does not declare, so today no well-behaved ACP agent even tries.

## Why this is needed

This is not a pass-through of a question raven currently swallows. Raven's
`CLIENT_CAPABILITIES` (`raven/agent/acp/protocol.py:28-34`) declares only `fs`,
and by spec an omitted `elicitation` field means "this client cannot ask the user
anything". Adapters read that and take the capability away from the agent.

Measured in `@agentclientprotocol/claude-agent-acp@0.66.0`:

```js
// dist/acp-agent.js:4395
const disallowedTools = elicitationSupport.form ? [] : ["AskUserQuestion"];
```

So the `Coder` sub-agent does not have a suppressed question, it has no
`AskUserQuestion` tool at all. The same gate also withholds two other paths:
MCP-server elicitation forwarding (`:4460-4461`) and the refusal-fallback consent
dialog (`:4469`). Declaring the capability gives a sub-agent back an ability it
currently does not have.

`elicitation/create` is distinct from `session/request_permission`, which raven
already answers by auto-approving (`raven/agent/acp/permissions.py`). Permission
asks "may I run this tool call"; elicitation asks "I need input from the user".
This design changes nothing about permission handling.

## What was measured

ACP schema, identical in `@agentclientprotocol/sdk` 1.3.0 (bundled by the
claude-code adapter) and 1.4.0:

- Method `elicitation/create`; notification `elicitation/complete` (url mode only).
- Capability `elicitation: {form: {}, url: {}}`; omitted or `null` both mean not
  advertised. The two sub-capabilities are independently advertisable.
- Params: `message` (required) plus a `mode` discriminator. Form mode carries
  `requestedSchema`, and the scope is flattened into the same object: either
  `ElicitationSessionScope` (`sessionId` required, `toolCallId` optional) or
  `ElicitationRequestScope` (`requestId`, used before any session exists, e.g.
  during auth).
- Response: `{action: "accept", content}` | `{action: "decline"}` |
  `{action: "cancel"}` | a custom action.
- `content` values are closed to five wire types: `string`, `integer` (int64),
  `number` (double), `boolean`, `string[]`.

`sessionId` being on the wire is what makes per-session routing possible; a
pooled connection carries several concurrent runs.

Which configured agents emit elicitation:

| agent | install | ACP | emits elicitation |
| --- | --- | --- | --- |
| Coder (claude_code) | adapter 0.66.0 via npx | third-party adapter | yes, three paths, all gated on `elicitation.form` |
| Writer (codex) | adapter 1.1.14 via npx | third-party adapter | only under `collaboration_mode: plan` - see Risks |
| opencode | 1.18.21 local, config disabled and pinned to npx 1.18.16 | native `opencode acp` | yes (`elicitation/create` in the binary) |
| hermes | 0.20.0 | native `hermes acp` | no - `acp_adapter/` has zero references; its elicitation support is MCP-side only |
| openclaw | 2026.7.1-2 | native `openclaw acp` (gateway bridge) | no - `dist/acp*` has zero references; its elicitation code is the client direction |

Two of the configured agents exercise this feature as configured -- `Coder` and
the built-in `Raven` row over `raven acp`. `Writer` relays an elicitation only in
the collaboration mode we do not select, so it is reachable in principle and not
in practice. `opencode` needs enabling in config, and is the only native-ACP one
among them, so it would cover a different code path than the two npm adapters.

## Scope

In: form-mode elicitation, end to end, for spawned runs and DAG nodes alike.

Out: `url` mode - deliberately not advertised. It exists for out-of-band OAuth,
payment and credential-collection flows; advertising it would let a sub-agent
send the user to an arbitrary URL to enter credentials. Omitting one
sub-capability is a clean, spec-supported subset.

Out: `session/request_permission` pass-through. It reverses an existing security
default and needs its own interaction design; the dispatcher this design
introduces is where it would land later.

## Design

### 1. Data model and module boundary

New pure module `raven/agent/acp/elicitation.py`. No transport, no broker, no
event loop, so all of it is unit-testable:

```
parse_request(params)   -> Ask | None        normalize the wire params: mode, message, schema, scope
fields(schema)          -> list[Field]       expanded in `properties` write order
coerce(field, text)     -> (ok, value)       str -> int/float/bool/list[str], enum match, pattern check
accept/decline/cancel()                      the three response builders
```

`Field` carries: name, prompt text (`title`, falling back to `description`),
type, enum options (from `enum`, or from `oneOf`/`anyOf` `const` values - and from
`items` for an array, where a multi-select keeps its enum), constraints
(`pattern`, `minLength`, `maxLength`, `minimum`, `maximum`), and whether it is
required. The schema's `default` is deliberately not carried: raven asks field by
field and has nowhere to pre-fill.

Property write order is the render order: it is what the agent wrote and what a
real form would show.

Because `content` values are closed to five types, `coerce` has a closed output
set and needs no open-ended type handling.

Not supported, both answered `decline`: `url` mode (not advertised, but an agent
may still send it) and `other`/unknown modes (the spec requires that a client
MUST NOT render an unknown mode as a known one).

### 2. Capability, dispatch and routing

**Capability.** `CLIENT_CAPABILITIES` gains `"elicitation": {"form": {}}` and no
`url`. That single line is the feature's switch.

**Registry.** `AcpConnection` gains an `elicitors` registry beside the existing
notification `router`, shaped after `_SessionRouter` (`pool.py:62-92`) including
its identity-checked `detach` (`:77-87`). That check matters here for the same
reason it matters there: an unconditional `pop` lets a finishing task tear down a
later task's routing, and the symptom is the later task silently going unserved.

**Dispatcher.** Today `on_request` is `auto_approver(name, observe=router.dispatch)`
(`pool.py:221`) - the observer exists because codex sends a shell command only on
the permission request and never on the matching `session/update`, so the
transcript needs it. The replacement keeps that parameter untouched and adds one
branch beside it:

| method | handler |
| --- | --- |
| `session/request_permission` | existing `permission_outcome`, unchanged |
| `elicitation/create` | look up `params["sessionId"]` in the connection's registry |
| anything else | `UNHANDLED` -> `-32601`, so `fs/*` behaviour is unchanged |

The dispatcher needs the registry, and the registry belongs to the connection.
The existing pattern already solves the ordering: `router` is built before
`launch` (`pool.py:206`), so the registry is built in the same place and passed
into the handler factory. No new mechanism.

**Attach lifecycle.** `acp_agent.run()` builds the elicitor next to the
collector, attaches it at `:570` and detaches it in the same `finally` at `:599`.
The elicitor must be constructed in the turn's own context, for the reason
documented at `acp_agent.py:126-131`: `__call__` runs on the connection's read
loop, whose ContextVar predates this run, and a pooled connection shared by two
runs would otherwise attribute one run's work to the other.

**Reaching the broker.** No new handle. `AskUserTool.ask_direct`
(`raven/agent/tools/ask_user.py:127-141`) already exists for host-side questions
outside a model tool call, with exactly the right contract: `None` means the
round-trip is structurally unavailable (no broker, no conversation), `""` means
timeout or cancellation (the broker never raises), anything else is the user's
answer. It already has a precedent caller - the graph-level confirm gate in front
of a whole DAG. Reusing it means sub-agent questions and raven's own `ask_user`
share one broker and one frontend contract.

**How the ACP layer reaches that tool.** Not by resolving it from a tool
registry: `AskUserTool` is registered per `AgentLoop`, and an ACP backend is
built from config in `backends/__init__.py` with no loop reference. Not by a new
process-wide setter either - the broker is late-bound at four transports, so a
parallel `set_asker` at each is four chances to miss one and the miss is silent.

Instead, a turn-scoped ContextVar, which is how `ExecTool` already solves the
identical problem for shell approvals and is bound from exactly one place:
`raven/rpc/spine.py`, beside `start_approval_turn`. ContextVars copy into the
background task a sub-agent run happens on, so the run inherits its turn's
asker.

That site also supplies the rule this design would otherwise have had to invent:
the shell responder is bound **only for `Origin.USER`**, so CRON and other
background turns fail closed as non-interactive. Elicitation follows it exactly -
a background turn whose sub-agent asks a question declines immediately instead of
waiting 600s for a reader who does not exist - and it needs no separate
presence check.

The conversation key is that site's `cid` (`_conversation_id(req)`), which is
what makes the "main `session_key`, not the per-instance lane" decision below
fall out rather than being enforced separately.

### 3. Question decomposition and validation

One property is one `clarify.request`, asked in `properties` order.

**The pair rule**, in `raven/agent/subagent/acp_dialects/claude_code.py`: an enum
property `X` plus an optional free-text property `X_custom` renders as a single
question - choices from `X`'s enum, free-text box writing to `X_custom`. This is
the fixed convention documented in the adapter's `dist/elicitation.d.ts`: each
`AskUserQuestion` becomes `question_<n>` (single-select `oneOf`, or an array with
an `anyOf` item enum for multi-select) plus `question_<n>_custom` mirroring the
CLI's per-question "Other" box. Without the rule a two-question ask becomes four
prompts; with it, two. `acp_dialects/` is where the repo already keeps
per-adapter conventions.

How each schema type lands on the clarify sheet (choices plus free text):

| schema | shown | read back |
| --- | --- | --- |
| `string` with `enum`/`oneOf` | choices are the labels; `oneOf` `description` as secondary text | matched against the enum |
| bare `string` | free-text box only | as typed |
| `boolean` | choices yes / no | coerced to bool |
| `integer` / `number` | free-text box | coerced, re-asked on failure |
| `array` (`anyOf` item enum, multi-select) | choices offered, with a hint that the box takes a comma-separated list | split, each item matched against the enum |

**Required and skipped.** A skipped optional field (Esc, or the sheet's close
button) means its key is omitted from `content`. A skipped required field is
re-asked once; if it is skipped again the whole elicitation answers `decline`,
because a `content` missing a required key does not match the schema and an
honest decline beats an invalid accept.

Validation retries are bounded at two per field, then `decline`, so a `pattern`
that never matches degrades into a decline rather than an endless re-ask.

Two facts from the adapter make skipping cheap in the dominant case: the form
generated for `AskUserQuestion` marks nothing required, and `decline` there means
the model is told the user skipped and the turn continues - only `cancel` aborts
the tool call.

**Sequential asking is forced, not chosen.** `QuestionBroker` allows at most one
pending question per conversation key, because a turn is serial.

### 4. Frontend and attribution

The two frontends fail in opposite directions:

- TUI (`ui-tui/src/app/createGatewayEventHandler.ts:521-527`) ignores
  `conversation_id` entirely and patches a single global `clarify` overlay slot.
  It shows everything, attributes nothing, and a second question overwrites the
  first.
- `ui/` (`src/features/composer/clarify.ts:42,133`) files the sheet under
  `p.conversation_id || sheetSession()` and buckets by it, so a question filed
  under a conversation the reader is not viewing is invisible until they switch.

**Conversation key: the main `session_key`, not the per-instance
`direct_lane`.** The whole point is that a human answers; a per-instance key
would hide the question inside a bucket nobody is looking at, and the DAG case is
worse - N nodes, N lanes, none of them the visible conversation.

**Attribution travels in the text, not the routing.** The prompt is prefixed with
the asking agent and instance, separated by a colon: `Coder(api-refactor): <message>`.
A bare separator rather than a phrase, because there is no backend i18n for
user-facing strings and the frontends are bilingual - any wording here would
hardcode one language into both of them. This is backend-only, renders correctly
in both frontends today, and needs no frontend change. The cost is that
attribution is prose rather than a structured field; a structured `asked_by` param
would need both frontends changed and is listed as a follow-up.

**A concurrency defect this design must not introduce.** With every sub-agent
filing under the main `session_key`, two DAG nodes asking at once collide on one
broker key. The broker's existing collision behaviour
(`raven/rpc/question_broker.py:73`) logs an error and fail-safes the *stale*
question to its default - here `""`, which this design reads as "skipped". Node
A's question would be silently dropped, never seen by the user.

The fix is an `asyncio.Lock` per conversation key, held for the whole elicitation
(all fields of one request), not per field. One agent's multi-field form is then
never interleaved with another's, "one form at a time" holds, and a second asker
queues, bounded by the broker's own timeout. The lock lives in a module-level
dict in the elicitation glue.

### 5. Failure, timeout and the read-loop constraint

**The constraint that would otherwise break everything.** `_read_stdout` ->
`_dispatch` (`client.py:480`) -> `_answer_request` (`:564`) -> `on_request` is
awaited inline. That is harmless today because `auto_approver` is a pure function
that awaits nothing real. The moment an elicitation awaits a human for up to
600s, the entire pooled connection's read loop stops: no `session/update` reaches
any session on that connection, and no `session/prompt` future is ever resolved.
The symptom would read as "two sub-agents at once hangs everything", with no
obvious link to a question.

Fix: `_dispatch` spawns `_answer_request` as a tracked task instead of awaiting
it inline. This is sound because JSON-RPC requests each carry their own id and
are independent - nothing requires answering in arrival order - while response
resolution (`_resolve`) and notifications stay on the existing fast path. The
task set hangs off the connection and is cancelled in `close()`, beside the
existing `_fail_pending` teardown.

Every fallback path, all of which answer rather than error:

| situation | answer |
| --- | --- |
| `mode: url` (not advertised, sent anyway) | `decline` |
| `mode: other` or unknown | `decline` |
| `ElicitationRequestScope` (`requestId`, no session) | `decline` |
| `sessionId` present but no elicitor registered (run already ended) | `decline` |
| `ask_direct` returns `None` (no broker or no conversation, e.g. a headless run) | `decline` |
| `ask_direct` returns `""` (600s timeout, or `cancel_all` on connection EOF) | field treated as skipped: optional key omitted, required -> `decline` |
| coercion or `pattern` failure past the retry limit | `decline` |
| malformed schema (not an object, `properties` not a dict, unknown property `type`) | `decline` |
| raven's own turn cancelled mid-elicitation | `cancel` - the turn really was aborted, which is what `cancel` means |

**Invariant: every `elicitation/create` gets a response.** The reason is written
at `client.py:567` - an unanswered request leaves the agent's turn pending
for the life of the session. There is a specific trap: `_answer_request`'s
exception path answers `-32601 method not found` (`:582-584`). For a capability
raven has declared, that is a lie the agent cannot act on. So the elicitor must
contain every exception itself and never raise.

Timeout stays the broker's existing 600s per question, matching raven's own
`ask_user`; the human is the bottleneck either way. No additional whole-form
budget: once the read loop is unblocked, a pending form only holds its own
session.

## Testing

| file | covers |
| --- | --- |
| `tests/test_acp_elicitation.py` (new) | the pure module: field order, coercion for all five types, `enum`/`oneOf`, pattern retry limit, `build_content`, the pair rule, malformed schema -> decline. No transport, no broker |
| `tests/test_subagent_acp.py` (extend) | dispatch and routing: `sessionId` reaches the right elicitor, identity-checked detach, unknown session -> decline, `requestId` scope -> decline |
| `tests/test_subagent_acp.py` (extend) | read-loop regression: a new `tests/acp_stub_server.py` mode sends `elicitation/create` then keeps pushing `session/update`; assert the updates still arrive while the elicitation is unanswered |
| `tests/test_subagent_acp.py` (extend) | the lock: two elicitations on one conversation key, second queues, both get answered - versus today's broker collision which fail-safes the first |
| `tests/test_acp_dialects.py` (extend) | the `claude_code` pair rule |
| either file | assert `CLIENT_CAPABILITIES` contains `elicitation.form` and not `url`, pinning the security decision |

`tests/test_subagent_acp.py` is where ACP behaviour tests already live, including
the `permission_outcome` tests, so per AGENTS.md 5.4 these extend it rather than
adding a parallel file.

No frontend change means no frontend tests - a concrete benefit of the
text-prefix attribution choice.

No automated integration test: provoking a real `AskUserQuestion` depends on the
model deciding to ask, which would make a CI case randomly red. Instead a manual
verification step in the MR description: enable `opencode`, give `Coder` a
genuinely ambiguous task, confirm the sheet appears and the agent continues with
the answer.

## Follow-ups

- Structured `asked_by` on `clarify.request`, rendered by both frontends, so
  attribution is a field rather than a text prefix.
- `session/request_permission` pass-through, landing in the dispatcher this
  design introduces.
- TUI `clarify.request` currently ignores `conversation_id` and has one overlay
  slot; a queue there would let it show who is asking and stop a second question
  overwriting the first. Questions from one turn are already serial -- every
  sub-agent spawned in a turn inherits that turn's conversation id, so they share
  the elicitor's per-conversation lock -- but two conversations (two sessions, two
  direct-chat lanes, or a spawn beside a direct chat) hold different locks, and
  there the later request replaces the earlier one on screen while the earlier
  stays pending until it times out.
- A codex dialect for `pair_fields`. codex pairs a free-text companion with the
  `__other` suffix and marks it `_meta.codex.{questionId, isOtherAnswer}`, and
  inverts the `required` sense claude-code uses: a question that has an other-box
  is left out of `required` (measured: `required: []` for a one-question form).
  Not written now because the path is unreachable while `collaboration_mode` is
  `default` -- there would be no live shape to test it against.
- codex's user-input request carries two fields this design ignores. `isSecret`
  marks an answer that should not be echoed, and a clarify sheet renders and
  records it like any other -- so a credential asked for that way would be
  visible in the transcript. `autoResolutionMs` is the asking side's own deadline
  for giving up, which nothing here honours.
- `clarify.closed` is not in the ACP server's `SIDE_CHANNEL_METHODS`, so a close
  arriving at that sink lands on its `dropped` tally. The useful behaviour is
  cancelling the outstanding `elicitation/create` with `elicitation/complete`,
  which is a protocol addition rather than a listing.

## Risks

- Advertising the capability changes sub-agent behaviour: `Coder` gains
  `AskUserQuestion`, so runs that previously guessed may now stop and ask. That
  is the intent, but it is a behaviour change for existing tasks. Rollback is the
  one line in `CLIENT_CAPABILITIES`.
- The read-loop change alters when agent-initiated requests are answered relative
  to other frames on the same connection. The stub-server test is the guard.
- Measured live on 2026-08-26, which is what this risk asked for.
  `Coder` (claude-agent-acp) asks and is answered end to end: two round trips in
  the frame journal, three questions merged into one `accept` carrying
  `question_0..2` and no `_custom` key, answered in 6.9s and 2.6s. The built-in
  `Raven` row asks too, over its own `raven acp`. `Writer` (codex-acp 1.1.14)
  does not, and the reason is not this design: `handleUserInput` requires
  `clientCapabilities.elicitation.form != null` -- which this change satisfies --
  and then the codex engine only issues a user-input request when the session's
  `collaboration_mode` config option is `plan`, not the default. Setting it to
  `plan` in a raw-ACP probe produced an `elicitation/create` on the first turn;
  the same probe under `default` produced none. Raven has no client-side
  `session/set_config_option`, so it cannot flip that switch today, and the
  chosen configuration keeps `agent-full-access` plus `default`. Note the gate is
  the collaboration mode and not `INITIAL_AGENT_MODE`, which the preset does set
  and which the wire confirms took effect (`currentModeId: agent-full-access`).
