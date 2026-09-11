---
name: subagent-dag-orchestration
description: Use when a task breaks into several distinct steps that a separate sub-agent could each carry out. A DAG node dispatches to an agent on the roster and cannot call your own tools, so steps that are just your own tool calls (reading files, small edits, running commands) are not a DAG for being many - do those yourself. The bound is about who can carry the work, not what kind of work it is: when the roster carries a specialist for it (a coding agent for code changes, an on-call agent for long runs) and the request assigns the work that way, it clears the bound like any other. For work that clears it, test three things before running the steps one at a time: are two or more steps independent (they can run at once), does a step hand its result to the next (a graph wires that handoff with no turn of yours in between), do the steps want different specialists (the tool lists the roster). If any of the three holds, orchestrate the whole task as one run_subagent_dag call. Iteration is graphs in series, one graph per round, driven by you.
metadata: {"raven":{"emoji":"🕸️","always":true,"inject":"description","requires":{"tools":["run_subagent_dag"]}}}
---

# Sub-Agent DAG Orchestration

## When to use

The trigger is **a task that breaks into several distinct steps** — not a task you
have already decided needs sub-agents.

**First, the hard bound.** A DAG node dispatches only to a configured third-party
sub-agent; it cannot call your own tools. So steps that are just your own tool calls
— reading files, small edits, running commands — are not a DAG for being many or
independent: do those yourself. The bound is about who can carry the work, not what
kind of work it is: when the roster carries a specialist for it (a coding agent for
code changes, an on-call agent for long runs) and the owner's request assigns the
work that way, it clears the bound like any other. Only work a sub-agent on the
roster could carry out gets as far as the tests below.

For work that clears that bound, test three things before running the steps one at a
time:

- **Independence** — can two or more steps run at the same time? Independent nodes are
  scheduled concurrently, up to the shared sub-agent cap
  (`max_concurrent_subagents` — `spawn` draws on the same allowance).
- **Handoff** — does a step hand its result to the next one? A node's output is written
  to a file the downstream node reads, and the graph wires that handoff itself. A `spawn`
  can hand its result on too — it takes a `node_id` and a later task names it the same way
  a node does — but only across a turn of yours; a graph needs none.
- **Specialism** — do the steps want different sub-agents? The tool's own description
  lists the roster and what each one is for.

If any of the three holds, express the whole task as **one** `run_subagent_dag` call
rather than dispatching sub-agents one at a time. Persistence is not one of the reasons:
every node's prompt and output is written to disk, and so is every `spawn`'s.

**Iteration is graphs in series, not a cycle in one graph.** A graph is acyclic and
runs once. When the work loops — code changes feeding experiment rounds feeding the
next code change — dispatch one graph per round and drive the loop yourself: each
graph's outputs come back to you, and the next round's nodes reuse the same
`instance` handles, so each side keeps its context across rounds.

If none of the three holds, the graph buys you nothing — dispatch the work as a single
`spawn`, which is the right call exactly when the task is genuinely one sub-agent doing
one thing. The work you do yourself is the work that never cleared the bound above, not
this.

Every install can run a graph: raven's own agents are on the roster whether or not any
third-party agent is configured, so `run_subagent_dag` is always available. Which agents
exist is still the tool description's answer, not this file's — read the roster there.

## Write the brief as the owner gave it

A node sees only its prompt. Whatever the owner asked for, and whatever the owner allowed
the node to change, reaches the node only if the prompt carries it -- and the prompt is where
a whole run's search space gets narrowed without anyone deciding to narrow it. Three rules,
each from a measured loss on 2026-09-08, where a two-round search left the one knob worth
most of the result untouched:

- **The owner's words and scope go in as written; your steer is marked as yours.** Quote
  the owner's request in every node's brief, not only the first round's. Your own reading of
  where the gains are is welcome as a suggestion, and it stays a suggestion: "gains can only
  come from ..." is a boundary the owner never drew. That day the owner allowed "training
  configuration, data mix, architecture, algorithm"; the round-one brief added "the
  hyperparameters are exhausted, so gains can only come from three classes outside the
  falsified list", and the round-two brief to the experiment runner opened with "you change
  no case". The runner changed nothing in 26 scored runs. One plain training setting, the
  kind the runner owned, was worth most of the gap to the best known result. It was never tried.
- **A list of tried things stays a list; it does not become a category.** When a handover
  names settings already swept, pass the list itself and ask the node to check what is *not*
  on it before it picks directions. Eighteen named hyperparameter groups became "the
  hyperparameters are exhausted"; the setting that mattered was on neither.
- **Each role's brief states the whole of its role, not this round's chores.** If the owner
  gave the experiment runner the training configuration, the runner's brief says so every
  round, whether or not you expect it to matter this round. A duty left out of the brief is
  a duty the node takes to be someone else's.

## How it works

You submit a flat list of `nodes`. A scheduler runs every node whose dependencies are
met, as soon as they are met. Each node's prompt is rendered from its template,
dispatched to its sub-agent, and the reply is written to a file of its own, flat and
keyed by node id rather than nested under the run. Downstream nodes reference upstream
outputs through template placeholders. When the run finishes you get the terminal-node
outputs plus every node's output-file path — as a message once the run ends, or as the
call's own result when you asked for `background: false`.

A node also leaves a **memory record**, `<node>.memory.json`, if its sub-agent runs on
a long-term memory backend: what that sub-agent distilled from the call into its own
memory, as `{"agent", "status", "memories": [{"type", "text"}]}`. This is separate from
its output file — the output is its answer to you, the record is what it concluded for
itself. Every node is told the paths of its upstream nodes' records automatically, so
you do not pass them: see `Upstream memory records` below.

A DAG run has no timeout — a long run is ended by hand (manual stop), not by a clock.

## Background by default

The call returns as soon as the graph is accepted, naming the run:

```
DAG run 20260729T031500Z-1a2b3c4d started in the background (3 nodes). I'll report the
result when it finishes -- keep working, and do not submit this graph again.
```

The graph keeps running after your turn ends, and its full result is delivered to you as a
new message when it finishes — the same way a `spawn` reports back. One other message can
reach you before that one: a node that could not do its job asking you what to do about it
(see below). So:

- **Don't** re-submit the same graph, and don't call the tool again to check on it. There is
  nothing to poll; the result comes to you.
- **Do** carry on with whatever else the task needs while it runs.
- Tell the user the work is under way, without promising the outcome you have not seen yet.

Pass `background: false` only when you genuinely cannot continue without the outputs — for
instance when the very next thing you must do is read them. That blocks your turn until the
graph finishes, or until a node reports it could not do its job, whichever comes first. In the
second case the call returns that node's report instead of the summary; see below for what to
do with it.

A malformed graph is rejected in your own turn either way, before anything is dispatched, so
a call that returns "started" has already passed every check.

A stopped run reports nothing back — if the user cancels it, no announcement arrives.

## When a node cannot do its job

Finishing is not the same as succeeding. Every node's answer is judged against the task it
was given, so a sub-agent that ran to the end and reported it could not do the work -- a
missing credential, a missing piece of the request, a tool that kept failing -- does not
count as done. Its output is withheld from the nodes that depend on it, and you get a
message naming the node, what is missing, and which downstream nodes are blocked behind it.

That message arrives mid-run, not at the end. The rest of the graph keeps going: only the
blocked branch waits.

Answer it by calling `resolve_dag_node` through `tool_call`: it is not in your tool list,
and `tool_call` is the only way to name it. The report you receive carries that tool's
complete schema, so take the field names from there and not from this page. What follows is
what the three decisions mean, not how to spell them.

- `continue` sends your message to the node and lets it try again, keeping the graph and
  everything it has already done.
- `abandon` gives up on that node. Its dependents are skipped; the other branches finish
  normally.
- `replan` replaces what is left of the graph: this run stops and a new one starts from the
  nodes you supply. Nodes this run completed are referenced (`depends_on` plus
  `{{ <id>.output }}`), never re-declared; every other node needs a new id, including a redo
  of the node that failed.

**Choosing between them:** when you can supply what the report says is missing, `continue` --
and if only the user can supply it -- a credential, a decision, a fact about what they want --
ask them first and then continue the node with their answer, because that is the case this
whole mechanism exists for and abandoning instead throws away work one sentence could have
unblocked. When what is missing is the plan itself, `replan`. `abandon` is for work that turns
out not to be needed. Replanning because a node is hard is how a graph loops without
progressing -- each replan starts its nodes on a fresh attempt budget, and the only thing that
stops the loop is the session's hourly dispatch limit.

Two limits worth knowing. A node gets a small number of continuations before it fails for
good, so a message that does not actually change anything wastes one. And the run does not
wait forever -- if nobody answers, the node fails on its own and its dependents are skipped.

In a `background: false` call the report does not arrive as a message: it is the call's own
return value, and the graph keeps running while you read it. Answer it the same way, with
`resolve_dag_node` -- which, for a blocking run, waits and returns the next report or the
final summary, so keep calling it until you have the summary. While your turn is running the
graph waits for you without a deadline; ask the user first if only they can supply what is
missing. If you end your turn with a report unanswered, the run carries on as a background
run: the report is re-sent to you as a message and the usual deadline starts.

To stop the whole run rather than one node, call `cancel_dag` the same way, through
`tool_call`. That discards the run outright and starts nothing in its place -- it is not
`replan`, which keeps the completed work and chains a successor.

Each run costs one unit of the same per-hour budget `spawn` draws on
(`max_subagent_spawns_per_hour`), whatever its node count. Submitting graphs in a loop
exhausts it and the next call is refused, so express the work as one graph rather than
several.

## Input format

Call `run_subagent_dag` with `nodes`: a list of node objects. The optional `background`
flag (default `true`) is described above.

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Node id, unique across the whole conversation (not just this graph). Letters, digits, `_`, `-` only. |
| `subagent` | yes | Which agent runs this node. Use one of the names listed in the tool's own description — don't invent them. Raven's own in-process agent (`Raven`) is on that list too, so a graph needs no third-party agent configured. |
| `prompt_template` | yes | Template rendered into the node's prompt. May contain the placeholders below. |
| `depends_on` | no | Upstream node ids that must finish before this node runs. May also name a task this conversation already finished — an earlier run's node, or a `spawn` — which only records the dependency. |
| `inputs` | no | Object mapping a key to a literal string, to `{"file": "<path>"}`, or to `{"node": "<id>"}` for another node's output — exactly one of the three, with nothing else in the object. |
| `instance` | no | A stable handle (e.g. `researcher`). Nodes sharing it run sequentially in id order and reuse one sub-agent session — only on an agent the roster tags `[stateful]`. |

A handle reaches beyond one run: two graphs in the same conversation that name the same
handle share one sub-agent session, in whichever order they reach it. Reuse a handle across
runs only when you mean to continue that conversation; give the later graph a different one
otherwise. Omitting `instance` does not mean no session: one is assigned automatically per node
and reported in the run summary when it finishes, so that node can be continued later too.

### Sub-agent capability tags

The tool description tags every available sub-agent, and both tags are checked
before any node is dispatched — a graph that violates one is rejected whole, with
nothing run:

- `[stateful]` / `[stateless]` — only a `[stateful]` agent carries context across
  nodes that share an `instance` handle. Sharing a handle on a `[stateless]` agent
  is rejected: there it would only serialize the nodes while each still starts from
  scratch. Pass what the later node needs through a placeholder instead. `spawn`
  refuses an `instance` on a `[stateless]` agent for the same reason.
- `[local-files]` / `[no-local-files]` — a `[no-local-files]` agent runs where this
  filesystem is not, so handing it a path is rejected. Give it the *contents*
  (`{{ <dep>.output }}`, `{{ inputs.<k> }}`, `{{ ref:<path> }}`) instead of the
  `_path` forms.

### Template placeholders

Inside `prompt_template`:

| Placeholder | Resolves to |
| --- | --- |
| `{{ <id>.output }}` | That node's output **contents**. |
| `{{ <id>.output_path }}` | That node's output file **path**. |
| `{{ inputs.<key> }}` | The input's literal value, or the **contents** of the file or node it names. |
| `{{ inputs.<key>.path }}` | That file's, or that node's output file's, **path**. |
| `{{ ref:<path> }}` | The **contents** of an existing file. |
| `{{ ref_path:<path> }}` | That file's **path**. |

Rules:

- **Prefer the `_path` forms for anything large.** They hand the sub-agent a path so it
  reads the file itself — the bytes never enter your context. Only for an agent tagged
  `[local-files]`; a `[no-local-files]` one has to get the contents form.
- A `_path` form must name a file that already exists. One that does not fails the node
  before its sub-agent is dispatched, rather than handing over a path nothing can open.
- `{{ inputs.<key> }}` needs `<key>` declared in that node's own `inputs`. The key is
  never a node id; what may name a node is the *value*.
- And the other direction: every key declared in `inputs` must be referenced by a
  placeholder. Material reaches the sub-agent only where a placeholder puts it, so a
  declared key nothing references would not arrive at all — the graph is rejected rather
  than run with material that silently went nowhere.

### Node ids are unique across the conversation

**A node id may not repeat an id anything in this conversation already used** — an earlier
run's node or a `spawn`, which takes its `node_id` out of this same namespace. The graph is
rejected if it does. That is what makes `{{ <id>.output }}` mean one thing, so pick ids that
say what the node produced — `pricing_research`, `deck_script_v2` — not `a`, `step1`, or a
generic `plan` you will want again. Re-doing work needs a new id; the earlier node's output
stays where it is and stays referenceable.

### Reading a task this conversation already finished

Because ids are unique, **an earlier task is named by its id alone** — and it does not
matter which tool ran it:

```
{{ pricing_research.output }}        that task's output text, whichever run or spawn produced it
{{ pricing_research.output_path }}   its path
```

The same works in the other direction: a `spawn` takes a `node_id` and can name a node this
graph leaves behind, with the same three forms.

That task needs no `depends_on` — there is nothing to order, it has already finished.
Listing it anyway is accepted and changes nothing, so write the edge if it makes the graph
read better. `depends_on` is still *required* for a node of *this* graph, since that edge
is what makes the upstream node run first.

Only a task that **completed and wrote an output** can be named this way, in a placeholder
or in `depends_on` alike. One that failed, was cancelled, was skipped, or is still running
keeps its id — nothing else may take it — but has no output
to read, and naming it is refused before any node of your graph is dispatched. The refusal
says which case it is, because the fix differs: re-do work that failed, was cancelled or
was skipped under a **new** id; where the run recorded no outcome for the node at all, read
its file directly; and for a node of a run still in flight, submit again once that run
reports its result.

That last one inverts when you are replanning. A replan discards what is left of the run it
replaces, so a node of that run which has not finished — one still waiting to start, or one
suspended on a report of its own — will never write an output, and waiting for it is waiting
for something the replan itself cancels. Those refusals say so, and ask for a new id: the
same answer this guide already gives for every node of the replaced run except the ones it
completed.

Note what is *not* an option in any of these: re-creating that node here. Its id is
taken, so a graph that repeats it is refused for the reuse instead. Re-running an upstream
step *as a node of your own graph* only works for one that does not exist yet — naming the
taken id in `depends_on` does not re-run anything.

A node id is all you ever need to name a node — there is no run qualifier on these forms,
because there is nothing left to disambiguate. The id also reaches beyond this tool: a
`spawn` takes a `node_id` out of the same namespace, so a graph may name a task a spawn
finished and a spawn may name a node this graph leaves behind. Two other ways to say the
same thing:

```
inputs: {"prev": {"node": "pricing_research"}}     then {{ inputs.prev }} / {{ inputs.prev.path }}
{{ ref:@nodes/pricing_research.out.md }}           by file path
```

Use the `{"node": ...}` input form when one upstream feeds several placeholders. Use
`ref:@nodes/<node_id>.<ext>` when you have a path rather than an id — it also reaches a
node's other files, not just its output (`<node_id>.prompt.md`, `<node_id>.memory.json`).
`graph.json` and `manifest.json` stay run-scoped rather than moving into `nodes/`, and
anything written before this conversation's history was flattened stays where it was
written — both need the full absolute path described below, not a prefix.

### Where a reference may point

`ref:` / `ref_path:` / `{"file": ...}` paths resolve inside two roots — the **session
working directory** (what a plain relative path is relative to) and **this conversation's
sub-agent history**, which may be named by absolute path. Anything outside both is
rejected, as is a `@nodes/` path that climbs out of this session's node artifacts.

The second root is this conversation's own record: every task it has run, from either
tool, since a `spawn` writes into the same `nodes/` root a graph does and is named the same
way. It stops there. The user's long-term memory, the installed skills, and every
*other* conversation's transcript sit outside it and are refused — so reference an earlier
run's outputs, or files the user pointed you at.

### Upstream memory records

Every node whose sub-agent can open local paths gets this appended to its rendered
prompt, listing each **transitive** upstream and the path of that node's record:

```
## Upstream memory records

- research: <session history>/nodes/research.memory.json
- audit: <session history>/nodes/audit.memory.json
```

Three things follow, and getting any of them wrong wastes a node's turn:

- **You do not write this.** It is appended for you. There is no placeholder for it and
  no `inputs` key to set; adding one duplicates what is already there.
- **Paths, not contents.** The node opens them itself with its own file tool. A
  sub-agent the roster tags `[no-local-files]` gets no block at all, because a path it
  cannot open is noise.
- **A listed file often does not exist yet.** A record is written after its node
  finished, by a poller that waits for the memory backend to finish distilling, while
  the scheduler starts the next node as soon as its dependencies complete. So absence is
  the normal case, not a fault: a node that finds the file missing, or carrying
  `"status": "pending"`, should carry on without it. Do not add a node whose only job is
  to wait for one, and do not treat a missing record as a failed upstream.

## Examples

The `subagent` values below are placeholders — substitute the names the tool reports
as available. Every prompt opens with the owner's request as written (see `Write the
brief as the owner gave it`); the node's own part follows it.

### Fan-out then aggregate

The owner asked: "Pick a web framework for the new service. Compare the candidates on
benchmarks and on what recent papers say about async runtimes, and recommend one." Two
researchers run in parallel; a writer waits for both and reads their outputs by path:

```json
[
  {
    "id": "research_web",
    "subagent": "Raven",
    "prompt_template": "Owner's request, as written: \"Pick a web framework for the new service. Compare the candidates on benchmarks and on what recent papers say about async runtimes, and recommend one.\"\nYour part: research recent web-framework benchmarks and report your findings."
  },
  {
    "id": "research_papers",
    "subagent": "Raven",
    "prompt_template": "Owner's request, as written: \"Pick a web framework for the new service. Compare the candidates on benchmarks and on what recent papers say about async runtimes, and recommend one.\"\nYour part: summarize the latest papers on async runtimes."
  },
  {
    "id": "synthesize",
    "subagent": "Raven",
    "depends_on": ["research_web", "research_papers"],
    "prompt_template": "Owner's request, as written: \"Pick a web framework for the new service. Compare the candidates on benchmarks and on what recent papers say about async runtimes, and recommend one.\"\nYour part: write the recommendation, merging these two sources.\nWeb findings: {{ research_web.output_path }}\nPaper summary: {{ research_papers.output_path }}"
  }
]
```

`research_web` and `research_papers` have no dependencies, so they run at the same
time. `synthesize` runs once both finish; as the terminal node, its output comes back
to you inline. Each node sees the whole request, so the writer knows a recommendation
is wanted and the researchers know what it is for.

### Pipeline with a reused stateful instance

The owner asked: "Write the introduction for our report on multi-agent systems: about
200 words, for readers who know software but not agents." A draft is written, reviewed,
then revised. The same session (`instance: "author"`) carries context from the draft
into the revision. This shape needs an agent tagged `[stateful]`; on a `[stateless]`
one the graph is rejected before anything runs:

```json
[
  {
    "id": "draft",
    "subagent": "Raven",
    "instance": "author",
    "prompt_template": "Owner's request, as written: \"Write the introduction for our report on multi-agent systems: about 200 words, for readers who know software but not agents.\"\nYour part: draft it."
  },
  {
    "id": "review",
    "subagent": "Raven",
    "depends_on": ["draft"],
    "prompt_template": "Owner's request, as written: \"Write the introduction for our report on multi-agent systems: about 200 words, for readers who know software but not agents.\"\nYour part: critique this draft against that request, for clarity and accuracy:\n{{ draft.output }}"
  },
  {
    "id": "revise",
    "subagent": "Raven",
    "instance": "author",
    "depends_on": ["review"],
    "prompt_template": "Owner's request, as written: \"Write the introduction for our report on multi-agent systems: about 200 words, for readers who know software but not agents.\"\nYour part: revise your earlier draft using this critique:\n{{ review.output }}"
  }
]
```

## Reading the result

Whether it arrives as the announcement of a background run or as a foreground call's return
value, the summary has the same shape:

```
DAG run 20260729T031500Z-1a2b3c4d finished: 3 completed, 0 failed, 0 cancelled, 0 skipped (of 3).
Run dir: <session history>/mas_dag/20260729T031500Z-1a2b3c4d

Node output files:
- research_web [completed]: <session history>/nodes/research_web.out.md
- research_papers [completed]: <session history>/nodes/research_papers.out.md
- synthesize [completed]: <session history>/nodes/synthesize.out.md

Terminal outputs:
### synthesize
<synthesize's output text>
```

- `Node output files:` lists **every** node with its status and output path (failed or
  skipped nodes show `(no output file)` plus an `error:` line).
- Only terminal-node outputs are inlined. To review any other node's work, `read_file`
  its output path.
- Each node's `<node>.prompt.md` (the rendered prompt) sits beside its output under
  `<session history>/nodes/` — useful when a node's answer looks wrong and you need to
  see what it was actually asked. The run dir itself holds only `graph.json` and
  `manifest.json`.
- A node may also leave `<node>.memory.json` in that same location (see `Upstream
  memory records` below). These are **not** listed in the summary, because a record is
  written asynchronously after its node finished and so may not exist when the summary
  is composed. `read_file` one directly if you want to know what a sub-agent took away
  from its step.

## Anti-patterns

- **Don't** dispatch sub-agents one at a time in a loop when a DAG expresses the work.
- **Don't** build a DAG for steps only your own tools can do — nodes reach third-party
  sub-agents, not your tools. Many steps alone is not a reason.
- **Don't** call the tool again to check on a background run, and don't re-submit its graph —
  the result is delivered to you; a second call runs the whole thing a second time.
- **Don't** reach for `background: false` to "make sure it finishes". It finishes either way;
  blocking only costs you the turn.
- **Don't** inline large upstream content with `{{ <id>.output }}` when you only need to
  pass it along; use `{{ <id>.output_path }}` — unless the agent is `[no-local-files]`.
- **Don't** reference a node of *this* graph in a template without listing it in that node's
  `depends_on` — a task this conversation already finished is the case that needs no edge.
- **Don't** guess `subagent` names — only names on the roster resolve.
- **Don't** share an `instance` handle across nodes to "keep them in order" on a
  `[stateless]` agent — order without shared context is what `depends_on` already gives.
