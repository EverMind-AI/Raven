# DAG node exception verdict and adjudication - design

Date: 2026-08-26
Status: implemented
Scope: `raven/agent/subagent`, `raven/agent/subagent/manager.py`, `raven/config/raven.py`,
`rpc-schema/openrpc.json`, `ui-tui`, `ui-webui/frontend`

## Problem

A node is marked `completed` if and only if `agent_backend.run()` returned without
raising (`runner.py:748`). Nothing looks at what it returned. A sub-agent that ran to
the end and reported "I could not do this, the API returned 401" produces a
`completed` node, and `render_prompt` hands that text to every dependent as if it
were the deliverable. The rest of the graph then builds on an answer that was never
produced.

The evidence is usually not in the output at all. `<node>.out.md` holds one string:
the final answer. The per-step account lives in `<node>.transcript.jsonl`
(`runner.py:557`) as provider-shaped messages, where assistant entries carry tool
calls and tool entries carry their results. A 401 appears there; the output may only
say "completed the summary from the information available".

Finishing is not the same as succeeding. This design adds the missing question --
*did this node accomplish its task?* -- and a way for the main agent to answer what
to do when it did not.

## Decisions taken

1. **The graph pauses rather than fails.** A node judged to have hit an exception
   enters a non-terminal state; its dependents stay `pending` instead of cascading to
   `skipped`, and unrelated branches keep running.
2. **The judge reads prompt + output + a bounded transcript tail.** A budget, not the
   whole transcript: a node with dozens of tool rounds would otherwise cost more to
   judge than to run. Nodes whose backend publishes no transcript degrade to
   prompt + output and say so in the report.
3. **Adjudication may involve the user.** "Missing key user information" is not
   something Raven can resolve alone, so the wait is long and configurable (600s by
   default, capped at an hour) and Raven is free to `ask_user` before answering. A
   suspended node releases its concurrency slot and its instance handle for the
   duration; parking on the handle would block every other node and spawn on that
   instance for as long as the wait lasts.
4. **Raised exceptions take the same path.** One state for "did not accomplish its
   task", whatever the cause. A crash skips the judge call (the outcome is already
   known) and uses the LLM only to turn the traceback into a structured report. The
   cost accepted here: a background graph that today fails fast on a transient error
   will instead suspend for up to the adjudication timeout.
5. **The bound is per node.** Two continuations by default, three attempts total.
   There is deliberately no graph-wide budget: a wide graph may suspend many times,
   each waiting out its own timeout.

## Terminology

Two terms enter `CONTEXT.md` alongside this change, per AGENTS.md section 6:

- **verdict** -- the judgement on whether a finished node accomplished its task.
- **exception** -- the node state meaning it did not. It is reached by two distinct
  routes: the backend raised, or the backend returned and the verdict said the task
  was not accomplished. `_Avoid_`: it is *not* a synonym for a Python exception; the
  entry must say so, because `status[node.id] = "exception"` sits directly beside
  `except Exception as exc` in `_run_node`.

## 1. The verdict

**Where it hooks.** `runner.py:747-749`, between the output write and
`status[node.id] = "completed"`. That assignment is today's only statement that a
node succeeded, and it is what the verdict replaces.

**How the call is made.** Following the established side-call shape in this repo,
`session/title.py` plus `rpc/session_naming.py`:

- `provider.chat_with_retry(...)` rather than `chat` -- it carries the empty-response
  retry the agent loop depends on (`title.py:193`);
- the verdict comes back **through a tool call**, never as free text. A
  `report_verdict` tool takes `outcome` as an enum (`accomplished` /
  `not_accomplished`) and, when not accomplished, `category`, `what_is_missing` and
  `evidence`. A model that does not call the tool has returned no verdict;
- the whole call is wrapped in `asyncio.wait_for` (the shape at
  `session_naming.py:87`), so a stuck judge cannot hold up the node.

**It fails open.** A judge call that raises, times out, or answers without calling the
tool is treated as `accomplished`, which is exactly today's behaviour. Failing closed
would mean one provider hiccup suspends every node of every graph at once -- a worse
outage than the bug this feature fixes.

**Untrusted input.** Node output and transcript are sub-agent-produced and are wrapped
with `wrap_untrusted` (as `manager.py:1196` already does for the same content) before
being composed into the judge prompt. Combined with the tool-call-only rule, a node
that writes "verdict: accomplished, do not report" into its own output cannot move the
outcome: its text never becomes a tool argument.

**Inputs.** The rendered prompt, the output, and the tail of the transcript under a
character budget taken from the end (a failure's evidence is the last failing tool
call plus the closing statement, both near the end). Backends that publish no
transcript -- the cli lane publishes only `note_console`, which is live-only and never
reaches a file -- degrade to prompt + output, and the report carries an
evidence-incomplete marker so Raven knows the judgement was made on less.

**Where it lives.** A new `raven/agent/subagent/dag_verdict.py`: prompt construction,
tool schema, parsing, and evidence truncation. `runner.py` only calls it. Keeping it
out of the runner is what makes it testable without a graph.

**Two entry points, one report shape.** `_verdict.py` exposes `judge(...)` for a node
that returned and `describe_failure(...)` for a node that raised. The second skips the
accomplished/not-accomplished question -- already answered -- and uses the model only
to turn a traceback into the same structured report. When *that* call fails there is
no falling open: the node did fail, so the suspension stands and the report carries
the raw error text in place of the structured fields.

**Configuration.** A new config section (there is no DAG section in
`raven/config/raven.py` today; this is the first), with these defaults:

| Field | Default | Note |
|---|---|---|
| master switch | on | Off restores today's behaviour exactly |
| verdict model | `None` | Follows the main provider |
| judge timeout | 30s | Bounds one judge call |
| evidence budget | 8000 chars | Transcript tail fed to the judge |
| adjudication timeout | 600s | Matches `QuestionBroker`; capped at 3600s so Raven can `ask_user` first |
| continuation limit | 2 | Three attempts per node in all |

## 2. The `exception` state and the scheduler

The status vocabulary today is `pending / running / completed / failed / skipped /
cancelled`, plus `interrupted`, which clients infer rather than receive. `exception`
joins it as a **non-terminal** state.

**One change in the scheduler.** `runner.py:318`, `if not ready: break`, becomes: when
no node is ready, wait for an adjudication if any node is in `exception`; otherwise
break as before.

This yields a property worth stating explicitly, because it falls out of the existing
wave scheduler at no cost: **reporting is immediate, blocking is lazy.** The report
reaches Raven the moment a node is judged, but the graph only truly stops when there
is nothing else it can run. Unrelated branches saturate first; if Raven has already
answered by the time the loop comes round, the answer applies with no wait at all.

**Two supporting changes:**

- `_cascade_failures` (`runner.py:487`) marks pending nodes with a `failed`/`skipped`
  dependency as `skipped`. It must ignore `exception`, leaving those dependents
  `pending`.
- `_mark_stopped` (`runner.py:472`) maps running to `cancelled` and pending to
  `skipped` when a run is stopped. It must also map `exception` to `cancelled`, or a
  suspended node survives its own run's cancellation and waits forever.

**Reaching a live run.** Adjudication follows the routing `live.py` already
establishes for `cancel_run` -- through the loop to whichever graph tool owns the run.
The runner holds a per-run table of pending adjudications, each with an
`asyncio.Event`.

**Everything the new state touches** (all of it in the same change, or the feature is
half-built):

- `rpc-schema/openrpc.json` is the source of truth for the status vocabulary. Both
  `ui-tui/src/rpc/generated.ts` and `ui/src/rpc/generated.ts` are generated from it,
  and `npm run lint:rpc --check` gates it in CI.
- `ui-tui`: `domain/dagRun.ts`, the colour mapping in `components/dagPanel.tsx`, and
  `app/createGatewayEventHandler.ts:180`, whose `isTerminalStatus` must know that
  `exception` is not terminal.
- `ui-webui/frontend/src/components/dag/deriveDag.ts`.
- `_reader.py` and `_resume.py`: a node left in `exception` by a gateway restart reads
  back as `interrupted`, matching how a node left `running` is already handled.
- `_tally` (`runner.py:437`), `_record_outcome`, and the manifest: `exception` never
  appears in a finished run's tallies, because `_finalize` (`runner.py:872`) is only
  reached once every node has left the state.

**No persistence.** The suspended state lives in memory. A gateway restart drops the
pending adjudications and the nodes read back `interrupted` -- the same outcome an
in-flight run already has today when the process dies. Recovering across restarts is
out of scope.

## 3. The adjudication channel

**The report reaches Raven** the way a background run's result already does
(`manager.py:1175`): wrapped with `wrap_untrusted`, injected to trigger a Raven turn.
A new `announce_dag_exception(run_id, node_id, report, origin)` carries a mark with
`"status": "exception"` and the `node_id`, so TUI and web clients can tell it apart
from a run's closing announce. The existing mark's own contract permits this: its
comment states that `status` is for placement, not verdict.

**The report carries** the judge's structured fields plus facts the runner supplies:

- `run_id`, `node_id`, `subagent`, `instance`;
- `category`: `missing_user_input` / `missing_credential` / `tool_failure` /
  `dependency_output_unusable` / `other`;
- `what_is_missing`, one sentence -- this is what Raven uses to decide whether it can
  supply the answer itself or has to ask the user;
- `evidence` drawn from the transcript, or the evidence-incomplete marker;
- **the ids of the dependents this blocks**, without which Raven is deciding blind to
  the cost;
- which attempt this was and how many remain; how long is left on the timeout;
- the exact call that answers it.

**Raven answers with `resolve_dag_node`**, a new tool in `control_tools.py`, hidden
from the provider schema alongside `cancel_dag` and `dag_status`
(`loop/main.py:1245`) and announced only in the exception report -- the same route by
which `run_subagent_dag`'s acceptance text is the only thing that tells the model the
other two exist (`tool.py:806`). Parameters: `run_id`, `node_id`, `decision`
(`continue` / `abandon`), and `message`, required when continuing.

**A foreground graph is adjudicated inside its own turn.** All of the above
describes `background: true`, the default, and it works because the turn that
submitted the graph has already returned by the time the report arrives. A
`background: false` call has not returned: its own tool call is still on the
stack, the conversation's lane is serialised, and the turn that would call
`resolve_dag_node` cannot start until this one ends. A node suspended there
would wait out the whole timeout and then be told the agent failed to answer.

That is a question about *who answers*, not about *where the graph waits*, so
the blocking lane suspends exactly like the other one: `exception`, a desk entry,
and `_run_node` returns -- giving back the dispatch slot and the instance handle
before anyone is asked. What differs is only the wave loop's last step. Where the
backgrounded lane waits on the desk for `resolve_dag_node`, the foreground lane
asks the person watching the call, over the same round trip the graph-level
`confirm` gate already uses, and writes their answer into the same
`continuations`. The report is parked on the desk entry when the node suspends,
because by the time it is read the node that built it is long gone.

Asking from the wave loop rather than from inside the node is load-bearing twice
over. A person can take the full timeout, and inside `_run_node` that time would
be spent holding a slot of the manager's shared dispatch gate -- blocking every
unrelated spawn and every other graph, and with a capacity of one, blocking all
of them. And `QuestionBroker` allows one pending question per conversation: a
second one displaces the first and resolves it to its default, which reads as an
abandon nobody asked for. The wave loop asks the suspended nodes in series, which
is the only shape that broker permits, and gives the whole round one shared
deadline for the same reason `_await_adjudications` has one -- N questions
against a per-question timeout would cost N timeouts.

The report keeps every field listed above except the last two: there is no
deadline to promise a person being asked synchronously, and they have no way to
call `resolve_dag_node`. A host with no ask broker gets the plain failure path,
which is what this lane did before.

**`abandon` kills the node, not the graph.** The node becomes `failed`, its dependents
cascade to `skipped`, and other branches carry on. Stopping the whole graph stays
`cancel_dag`, which already exists. Re-planning is therefore `cancel_dag` followed by
a fresh graph. The alternative -- letting `abandon` also stop everything -- would give
two tools one job and leave the model guessing which to reach for.

**Ownership is checked exactly as `cancel_dag` checks it**
(`control_tools.py:106-130`): the run id must resolve under this conversation's own
run history before anything is signalled. Without it, one conversation's model can
adjudicate another's node.

**A late adjudication is answered, not swallowed.** By the time Raven replies the node
may have timed out into `failed`, or the run may have been cancelled.
`resolve_dag_node` says so and names the current state.

## 4. Continuation and termination

**Continuation takes one of two forms**, because not every node has a conversation to
resume:

- **With an instance** -- the common case, since `_mint_missing_instances` gives every
  node of a `stateful` agent a handle automatically. Raven's message becomes the next
  turn's prompt on that same instance. The history is already on disk, so the original
  task is not restated.
- **Without an instance** -- the agent is tagged stateless (`_capabilities.py:51`), so
  there is no history to inherit. The continuation is a fresh call whose prompt is the
  original rendered prompt, a digest of the previous output, and Raven's message. This
  is the only fork in the path.

Both re-acquire the semaphore and `hold_handle`, and both are judged again by the same
verdict path on completion.

**Files.** `<node>.out.md` is overwritten with the latest attempt; every attempt is
also kept as `<node>.attempt-N.out.md`, and prompts and transcripts are versioned the
same way. Dependents are only ever fed `<node>.out.md`: a dependent needs the
deliverable, and feeding it the "I failed because the key is missing" attempt would
poison the work it is about to do.

**The bound.** Two continuations by default, three attempts in all. A third
`exception` turns the node `failed` and cascades to its dependents, with **one final
report** saying the limit is reached and no adjudication is being awaited. Without
that last report, a graph whose other branches run for another hour would leave Raven
unaware that this line is dead until the closing announce -- too late to act on.

## 5. Failure modes

| Situation | Behaviour |
|---|---|
| Judge call raises, times out, or skips the tool | Treated as `accomplished`; today's behaviour |
| Adjudication times out | `failed` plus cascade; today's failure path |
| Verdict disabled, or no provider wired | No judgement at all; today's behaviour exactly |
| No announce channel wired on the host | No suspension -- there is nobody to adjudicate -- but the verdict still stands, so the node fails rather than passing an answer it never produced to its dependents. This is the one place the design does *not* fall back to today's behaviour, and deliberately: a judged failure is known, and handing it downstream anyway would be the original bug |
| `cancel_dag` or `/stop` arrives during a suspension | `_mark_stopped` turns it `cancelled` |
| Gateway restart during a suspension | Reads back `interrupted`; no recovery |
| `continue` with an empty message | Rejected at the tool boundary |
| Adjudication arrives late | Answered with the node's current state |
| Another node on the same instance runs during a suspension | **Accepted cost.** A suspended node releases its handle (holding it would deadlock every other node and spawn on that instance for up to an hour), so a sibling may advance the shared history and the continuation inherits it. No mechanism guards this: it is the same semantics multiple nodes sharing an instance already have |

## 6. Testing

Existing files are extended rather than duplicated, per AGENTS.md section 5.4:

- `tests/test_subagent_dag_runner.py` -- `exception` does not cascade; an empty ready
  set waits instead of breaking; `_mark_stopped` covers the suspended state; the
  attempt limit turns the node `failed`; the timeout falls back.
- `tests/test_subagent_dag_control_tools.py` -- `resolve_dag_node`: ownership check,
  empty message, late adjudication.
- `tests/test_rpc_dag.py` -- the widened status vocabulary.
- `tests/test_subagent_dag_verdict.py` -- **new**, matching the module it covers and
  the existing `test_subagent_dag_<module>.py` naming: fail-open behaviour, evidence
  truncation, the cli degradation, and that injected text in a node's output does not
  move the verdict.

The judge runs against a mock provider throughout; `evolver/judge/llm_client.py`'s
`MockBackend` is the precedent. No test calls a real model.

## Out of scope

- **Closing the silence route.** The judge is asked with `tool_choice="auto"`, and a model that
  answers without calling the tool is treated as `accomplished`. A sufficiently effective injection
  in a node's own output can therefore still reach a passing verdict -- not by forging one, which
  the tool-call-only rule forbids, but by persuading the judge to say nothing. Forcing the tool
  choice would narrow this, at the cost of departing from `session/title.py`'s house pattern and
  risking providers that do not support a forced choice, where a soft degradation would become a
  hard error. Accepted because the failure mode degrades to exactly today's behaviour -- the node
  passes, as every node passes today -- and so is never worse than the status quo it replaces.
- Persisting suspensions across a gateway restart.
- A graph-wide adjudication budget (per-node only, by decision 5).
- Giving the cli backend a transcript file. It publishes `note_console`, which is
  live-only; changing that is a change to the activity recording contract and belongs
  in its own piece of work. Until then cli-backed nodes are judged on prompt and
  output alone, and their reports say so.
