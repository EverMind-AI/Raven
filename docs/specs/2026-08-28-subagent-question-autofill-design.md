# Sub-agent question autofill - design

Date: 2026-08-28
Status: proposed
Scope: `raven/agent/acp`, `raven/agent/tools/ask_user.py`, `raven/agent/loop/main.py`,
`raven/rpc/spine.py`, `raven/config/raven.py`, `CONTEXT.md`

## Problem

A sub-agent knows only the task string it was handed. The host knows the whole turn:
what the user asked a moment ago, the arguments of the spawn call that created the
sub-agent, and whatever everos recalls about the user. So a sub-agent routinely asks
for something the user has already said in this very turn, and raven -- sitting
directly between the two -- forwards the question unchanged.

Both routes a sub-agent's question can take end at the same `clarify.request` surface,
and neither tries to answer first:

- `Elicitor._elicit` decomposes a form into one question per schema property and puts
  every one of them to the user, one at a time (`elicitor.py:146`, `:202`).
- `AskUserResponder._ask` does the same for the single question a `session/update`
  carries (`ask_user.py:184`).

Both hold a per-conversation lock across the whole exchange (`elicitor.py:132`,
`ask_user.py:167`). A form nobody is answering therefore also blocks every other
agent's question in that conversation for up to `LOCK_WAIT_SECONDS` (600s).

The material that answers many of these questions is in the host's hands at the moment
the question arrives. Nothing reads it.

## Decisions taken

1. **The decision is made once per form, not once per question.** A form's fields are
   frequently answered by one sentence the user wrote ("push it to `feat/x` and let
   chandler review"), and a per-question decision cannot see that: it would judge
   "which branch" without knowing "which reviewer" was settled by the same clause. One
   call per form also costs one model call instead of N, and lets a fully answered form
   skip the conversation lock entirely.

2. **The decision rides the turn's own conversation rather than a bespoke context.**
   The autofill step is a continuation of the turn that spawned the sub-agent, so it is
   shown what raven itself is looking at. This is not only cheaper to specify -- it is
   the only version that works. `_save_turn` runs after the turn completes, so the
   persisted session holds nothing from the current turn; the *live* message list
   holds both the user's message and the assistant message carrying the spawn call and
   its arguments, because `add_assistant_message` runs before the tools execute
   (`main.py:3264`).

3. **One additional recall, keyed on the question.** The recall already in the
   assembled context was keyed on the user's message
   (`context_engine/segments/memory.py`), which is the wrong query for "who should
   review this". A real turn recalls on its new message; this step does the same, under
   the same 5s budget and the same degrade-to-no-hits rule.

4. **Three outcomes per question, not two.** A question can be partially reachable --
   raven knows the branch but not the reviewer. `partial` still asks the user, but the
   question carries what raven knows.

5. **Raven never rewrites or splits a sub-agent's question.** The user must be
   answering the question that was actually asked. What raven knows is *appended* as a
   parenthetical note; the sub-agent's wording is passed through byte for byte.

6. **A question asking for approval of an action is never auto-answered**, however
   clearly the context supports it. See section 4.

7. **Every failure path defers to the user.** Deliberately the opposite of
   `dag_verdict.judge`, which fails open: a failed verdict costs one extra node, a
   failed autofill makes a decision on the user's behalf.

8. **The step is rendered as a synthetic tool call, and written back into the
   conversation at the `drain` seam.** Both frontends already render an unknown tool
   name generically, so this needs no wire-schema, i18n, or frontend change.

9. **One switch, and no model knob.** Pinning a cheaper model for this call would mean
   a different conversation, which contradicts decision 2.

## Terminology

One term enters `CONTEXT.md` alongside this change, per AGENTS.md section 6, in the
Subagent cluster beside the existing **Elicitation Pass-Through** and **Ask-User Round
Trip** entries:

- **Question Autofill** -- the step in which raven answers a sub-agent's question from
  the turn's own context instead of putting it to the user. It sits in front of both
  question routes, decides per form, and defers anything it cannot support from what
  the host already knows. `_Avoid_`: it is not a *default* for a question -- the
  broker's `default` is what a timeout returns, and autofill deliberately never sets
  it (section 3).

## 1. Where the decision happens

Not as an `Asker` decorator. The `Asker` boundary is per question (`elicitor.py:202`),
and decision 1 requires the whole form.

The resolver is reached from the two call sites that hold a whole exchange:

```
Elicitor._elicit                                  (elicitor.py:111-185)
  1. resolve(fields)                              <- one call, before the lock
  2. for each `answer`: coerce against the field schema
       coerce fails -> downgrade that field to `defer`, log
  3. nothing left to ask -> accept(content); the conversation lock is never taken
  4. otherwise -> take the lock, ask the leftovers as one batch

AskUserResponder._ask                             (ask_user.py:159-190)
  same call with a single-element list; that route carries one question per frame
  and has nothing to split
```

Both are constructed in `backends/acp_agent.py` and have no loop reference, which is
the problem `asker.py` already solves for the asker: a turn-scoped ContextVar, set in
`RpcTurnRunner.run` where the loop, the emit callable, and `req` are all in scope.
`start_ask_turn` gains a second binding and `current_ask` returns it, so `spine.py:195`
remains the single wiring point and the `web_rpc` surface inherits it unchanged
(`web_rpc/spine.py` reuses `build_rpc_spine`).

The binding follows the asker's own origin gate: it is set only for `Origin.USER`
turns. A CRON or otherwise background turn has no reader, its asker is `None`, and its
questions already resolve without a popup -- there is nothing there for autofill to
save.

## 2. What the resolver is shown

One model call, whose messages are:

| Block | Source |
|---|---|
| the turn so far | read-only snapshot of the loop's live message list |
| what raven has already auto-answered this turn | the turn's autofill ledger |
| recalled memory for this question | `backend.recall(query=<question text>, user_id=..., top_k=...)` |
| the questions | fenced with `wrap_untrusted(source="subagent")` |

The snapshot needs a new read-only accessor on `AgentLoop`: `messages` in
`_run_agent_loop` is a local that is reassigned each iteration (`main.py:3264` and
others), so a stored reference goes stale. A per-session holder refreshed at the top of
each iteration is enough -- the question arrives during tool execution, one statement
after the holder was last written.

The recall needs `self.memory_config` on `AgentLoop`. It is a constructor parameter
today (`main.py:486`) but is only forwarded to the context engine (`:698`), never
stored, so `user_id` and `memory_top_k` are not reachable from anywhere else.

The sub-agent's question is untrusted input -- it can try to talk raven into answering
-- so it is fenced, and the model reports only by calling a tool. Prose output would be
a route for a forged decision; a tool argument is not. This follows
`dag_verdict.verdict_tool_schema`.

**Cache.** The prefix is what the loop just sent, so the call should be cache-warm --
but `litellm_provider.py` places the breakpoint on the last tool, and this call
replaces the tool list with its own. Whether the shorter prefix still hits is
provider-dependent and is to be measured, not assumed. It does not change the design:
continuity is chosen for correctness.

## 3. The three outcomes

```python
@dataclass(frozen=True)
class Question:
    key: str            # schema property name; "" for the single-question route
    prompt: str         # the sub-agent's own wording, never modified
    options: list[str]
    required: bool

@dataclass(frozen=True)
class Resolution:
    status: Literal["answer", "partial", "defer"]
    answer: str = ""    # status == "answer"
    known: str = ""     # status == "partial" -- appended to the prompt as a note
```

`answer` is admitted only when the answer is either stated in the turn's own text or
uniquely determined by a recalled long-term preference. Anything needing more than one
step of inference, or admitting more than one reasonable answer, is `defer`. An answer
constrained by `options` must be one of them.

`partial` reaches the user as the sub-agent's question plus one appended line:

```
raven-code(a1b2): Which branch, and should I name a reviewer?
(raven knows: branch = feat/x, from what you said this turn; reviewer unknown)
```

**The broker's `default` is left empty.** `default` is what `await_question` returns on
timeout (`question_broker.py:81`), so putting a draft there would turn "the user did
not look" into "raven answered for them" -- the exact failure mode decision 6 exists to
prevent. A timeout stays a skip.

## 4. The irreversible-action veto

A question whose answer *authorises an action* -- push, delete, send, pay, overwrite,
merge -- is deferred whatever the context says.

The two kinds are not distinguishable by how answerable they are, which is what makes
the veto necessary rather than redundant. "Which branch" is a fact: answered wrongly,
the sub-agent goes the wrong way, the user sees a wrong result, and it is re-run.
"Should I push now" is an authorisation: answered wrongly, the push has happened. In
this repo that also spends the merge request's approval, and `main` is protected.

The veto is a rule in the resolver's instruction, so it is soft and can misjudge. The
asymmetry is what makes that acceptable: misjudging a fact as an authorisation costs
one popup the user was going to see anyway, while the reverse costs an action. It fails
in the same direction as every other fallback here.

## 5. Rendering and write-back

Autofill is rendered as a tool call named `answer_for_user`, in three parts:

| When | What | Gets |
|---|---|---|
| at the moment of the decision | `emit(ToolEvent(START))` then `emit(ToolEvent(COMPLETE))` | a row in both frontends, live |
| same moment | append to the turn's autofill ledger | continuity for later questions in the same turn |
| top of the next loop iteration, beside `drain` (`main.py:3095`) | flush the ledger into the message list as `assistant(tool_calls=[answer_for_user])` + `tool(result)` | persistence via `_save_turn`, replay after reload, and the main model knowing what was answered for it |

**Why the write-back cannot happen where the decision does.** The question arrives while
the spawn or `run_subagent_dag` tool is executing -- that is, after the assistant
message carrying `tool_calls` is in the list and before its `tool` results are. Splicing
a message into that window breaks the invariant `main.py` documents at the
`add_assistant_message` call site: every `tool_calls` entry must be followed by the tool
messages answering each `tool_call_id`. The flush therefore runs on the loop's own task,
at the top of the next iteration, which is where `drain` already merges externally
arrived content for exactly this reason. A tool call is always followed by another
iteration, so the flush always happens; the one exception is a turn that ends on
`max_tool_iterations`, which loses the write-back and is logged.

Details:

- **One row per form**, not per question. `arguments` carries the agent, the instance
  and the questions; `result_preview` carries one line per question saying where it
  went.
- **No row when nothing was autofilled.** A form that fully defers must not add a row
  saying so, or every sub-agent question grows a second row and the feature becomes
  noise.
- **`answer_for_user` is never registered in the `ToolRegistry`.** Registering it would
  hand the model an interface for claiming it had answered on the user's behalf. It is
  only ever synthesised.
- `ToolEvent.conversation_id` is set: the emit happens off the turn's task.

The precedent for a purely synthesised tool event is `spine.py:218-227`, which emits a
`tool.complete` for the message tool that the model never "called". The precedent for
rendering an unknown name is both frontends themselves: `ui/src/live/050-turn.js:145`
does `st.tool(p.name || 'tool', ...)` and `createGatewayEventHandler.ts:490` does
`recordToolStart(id, name ?? 'tool', ...)`. Neither has a tool-name allowlist.

## 6. Batching what is left

`ask_direct` (`tools/ask_user.py:215`) passes only `prompt`, `choices` and `timeout_s`
to the broker, dropping the `header`, `recommended`, `index`, `total` and `batch`
parameters `await_question` accepts. Every sub-agent question is therefore rendered as
a standalone "1 of 1" today, even when it is field 3 of a 5-field form.

Since the resolver now computes the leftover set before anything is asked, that set is
sent as one batch: `Asker.ask` is widened with keyword-only `index`, `total` and
`batch`, `_AskViaTool` forwards them, and `ask_direct` passes them through.

`ui-tui` renders these (`components/prompts.tsx:137`). `ui/`'s `ClarifyRequest`
(`features/composer/clarify.ts`) does not declare them and ignores unknown keys, so the
web UI keeps its current one-at-a-time rendering. Improving that is a separate change
to `ui/`.

## 7. Configuration

A new extension block on `RavenConfig`, modelled on `SubagentDagConfig`
(`config/raven.py:1425`):

```json
{ "subagentQuestions": { "autofillEnabled": true } }
```

| field | default | meaning |
|---|---|---|
| `autofillEnabled` | `true` | Off restores the previous behaviour exactly: every question goes straight to the user. |
| `autofillTimeoutSeconds` | `20.0` | Wall clock for one resolver call. Past it, every question in the form defers. |

Wired through the three entry points that already pass `subagent_dag_config`
(`cli/gateway_commands.py`, `cli/tui_commands.py`, `cli/agent_commands.py`).

## 8. Failure modes

| Case | Behaviour |
|---|---|
| `autofillEnabled` false | No resolver call, no ledger, no row. Byte-identical to today. |
| Resolver call times out, raises, or answers without calling the tool | Every question in the form defers |
| An `answer` fails `elicitation.coerce` against its field schema | That field alone downgrades to `defer`; the retry loop in `_one` then re-asks the *user*, never the resolver |
| An `answer` is not among a field's `options` | Treated as no answer; defers |
| No memory backend, or recall exceeds its budget | Resolves on the conversation alone |
| Run cancelled mid-form | Unchanged: `Elicitor._cancelled` and `_retracted` are checked between fields as they are today |
| Turn ends on `max_tool_iterations` before the flush | The row was already rendered live; the write-back is lost and logged |
| No live message snapshot (an unwired host, a test) | Defers; autofill needs the turn to have one |

## 9. Testing

Per AGENTS.md section 5.1, one new file matching the module it covers, plus extensions
to the two existing route tests:

- `tests/test_acp_autofill.py` -- **new**: the three outcomes; a fully answered form
  never takes the conversation lock and never reaches the broker; a partially answered
  form asks only the leftovers, as one batch; the appended `known` note leaves the
  sub-agent's wording untouched; `default` stays empty; an approval-shaped question
  defers even when the context answers it; timeout, exception and no-tool-call all
  defer; the switch off is byte-identical to the current path.
- `tests/test_acp_elicitation.py` -- coerce failure downgrades one field rather than
  burning the retry budget.
- `tests/test_acp_ask_user.py` -- the single-question route resolves through the same
  path.

The resolver runs against a mock provider throughout, following
`test_subagent_dag_verdict.py`. No test calls a real model. A test asserting that
injected text inside a fenced question cannot move an outcome belongs with the three
outcome tests.

## Out of scope

- **Autofill for background turns.** CRON and other non-`Origin.USER` turns have no
  asker bound, so their questions never reach a user today; answering them instead of
  declining them is a change to background behaviour and a separate piece of work.
- **Batch rendering in `ui/`.** The batch fields are sent; the web UI ignores them
  until its `ClarifyRequest` declares them.
- **Rewriting or splitting a sub-agent's question** (decision 5), including the case
  where one field genuinely bundles two questions. Those get a `partial` note and go to
  the user whole.
- **A hard, non-model check for the irreversible-action veto.** Section 4 is a prompt
  rule. A keyword gate over question text was considered and rejected: it cannot see
  intent, and its false negatives would read as a guarantee this design does not make.
