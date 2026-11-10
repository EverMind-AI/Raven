---
name: subagent-dag-orchestration
description: Use when delegated work breaks into several tasks that agents on the roster can carry out. The host's role decides what to delegate; this skill decides how to group that work. A DAG node dispatches to a roster agent, including the generic agent, and cannot call the host's tools. Choose from the tool's `subagent` parameter enum -- agent names on ordinary turns, worker labels when a Worker Table is bound. The roster describes underlying capabilities. Reading a brief or checking task status is host coordination, not a reason to create graph nodes. Before dispatching tasks one at a time, test three things: are two or more tasks independent (they can run at once), does a task hand its result to the next (a graph wires that handoff with no turn of yours in between), do the tasks want different specialists (the roster closes the tool list). If any of the three holds, orchestrate the whole task as one run_subagent_dag call. A step whose result decides what runs next is a decision node: give it routes, and guard what follows with when, all in one graph. Iteration is graphs in series, one graph per round, driven by you.
metadata: {"raven":{"emoji":"🕸️","always":true,"inject":"description","requires":{"tools":["run_subagent_dag"]}}}
---

# Subagent DAG Orchestration

## When to use

The three tests in this skill's description decide it: tasks that can run at once, a task
that hands its result to the next, or tasks that want different specialists. Persistence
is not a reason -- every node's and every `spawn`'s prompt and output is written to disk
either way. When none of the three holds, give the work to one agent: `spawn` if it is
offered, otherwise a one-node graph.

Independent nodes run at the same time, up to `max_concurrent_subagents`, a cap `spawn`
shares. A graph is acyclic and runs once, so iteration is graphs in series: one graph per
round, driven by you, each round reusing the same `instance` handles so every side keeps
its context.

## Write the brief as the owner gave it

A node sees only its prompt. What the owner asked for, and what the owner allowed the node
to change, reaches it only if the prompt carries it -- and the prompt is where a run's
search space gets narrowed without anyone deciding to. (Measured: briefs that turned the
owner's "training configuration, data mix, architecture, algorithm" into "gains can only
come from three classes" left untried the one training setting worth most of the gap to
the best known result.)

- **Quote the owner's request, as written, in every node's prompt** -- every round, not
  only the first. Your own steer is welcome, marked as a suggestion: "gains can only come
  from ..." is a boundary the owner never drew.
- **A list of tried things stays a list; it does not become a category.** Pass the list
  itself, and ask the node to check what is *not* on it before it picks directions.
- **Each role's brief states the whole of its role, every round,** not this round's
  chores. A duty left out of the brief is a duty the node takes to be someone else's.

## A complete call

The owner asked: "Recommend a web framework for the new service, from benchmarks and
recent papers on async runtimes." Two researchers run at once; a writer waits for both and
reads their outputs by path. `Raven-General` stands in for whatever the `subagent` enum
offers.

```json
{
  "task_summary": "Recommend a web framework",
  "nodes": [
    {"id": "benchmark_findings", "subagent": "Raven-General", "node_summary": "Research framework benchmarks",
     "prompt_template": "Owner's request, as written: \"Recommend a web framework for the new service, from benchmarks and recent papers on async runtimes.\"\nYour part: research recent web-framework benchmarks and report your findings."},
    {"id": "runtime_paper_summary", "subagent": "Raven-General", "node_summary": "Summarize async runtime papers",
     "prompt_template": "Owner's request, as written: \"Recommend a web framework for the new service, from benchmarks and recent papers on async runtimes.\"\nYour part: summarize the latest papers on async runtimes."},
    {"id": "framework_recommendation", "subagent": "Raven-General", "node_summary": "Write the recommendation",
     "depends_on": ["benchmark_findings", "runtime_paper_summary"],
     "prompt_template": "Owner's request, as written: \"Recommend a web framework for the new service, from benchmarks and recent papers on async runtimes.\"\nYour part: write the recommendation from these two sources.\nBenchmarks: {{ benchmark_findings.output_path }}\nPapers: {{ runtime_paper_summary.output_path }}"}
  ]
}
```

The two researchers have no dependencies, so they start together; the writer starts when
both finish, and as the terminal node its output comes back to you inline.

## Input format

The call's top level holds `task_summary` (the graph's title, under ten words: name the
goal), `nodes`, and optionally `background` (see `After you submit`) and `confirm`
(default `false`; `true` has the user approve the graph before any node runs -- for
effects outside this machine, such as publishing, sending or spending). Every other field
belongs to a node:

| Node field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Unique across the whole conversation, not just this graph; letters, digits, `_`, `-`. Name what the node produces (`pricing_research`, `deck_script_v2`), not `step1`. |
| `subagent` | yes | A value of the tool's `subagent` enum: an agent name, or, when a Worker Table is bound, a worker label -- the label itself, not the agent it runs on. |
| `node_summary` | yes | The node's title in the run, under ten words. |
| `prompt_template` | yes | The node's prompt, with the placeholders below. |
| `depends_on` | no | Nodes of this graph that must finish first. |
| `inputs` | no | Key to a literal string, `{"file": "<path>"}` or `{"node": "<id>"}` -- exactly one form per key. |
| `instance` | no | A session handle; see `Wiring rules`. |
| `routes`, `select`, `when` | no | See `Conditional branches`. |

| Placeholder | Resolves to |
| --- | --- |
| `{{ <id>.output }}` | That node's output **contents**. |
| `{{ <id>.output_path }}` | That node's output file **path**. |
| `{{ inputs.<key> }}` | The literal, or the **contents** of the file or node it names. |
| `{{ inputs.<key>.path }}` | That file's, or that node's output file's, **path**. |
| `{{ ref:<path> }}` | An existing file's **contents**. |
| `{{ ref_path:<path> }}` | That file's **path**. |

**Prefer the `_path` forms for anything large** -- a report, a log, a dataset: the subagent
opens the file itself instead of getting it pasted into its prompt. They need an agent
tagged `[local-files]` and a file that already exists; a `[no-local-files]` agent gets the
contents forms.

## Wiring rules

- **A node of this graph is read through an edge.** List it in `depends_on`; the edge is
  what makes it run first. That holds even for a node already upstream through another
  dependency: a step that reads both a draft and its review lists both.
- **Every `inputs` key is referenced** by a placeholder, and `{{ inputs.<key> }}` needs
  `<key>` in that node's own `inputs`. Use `{"node": "<id>"}` when one upstream feeds
  several placeholders.
- **References stay inside two roots:** the session working directory and this
  conversation's subagent history. `ref:@nodes/<id>.out.md` reaches a node's output by
  path; `<id>.prompt.md` and `<id>.memory.json` sit beside it.
- **`instance` shares one session.** Nodes with the same handle run one after another, in
  id order, in one session -- only on an agent tagged `[stateful]`; for a `[stateless]`
  one, pass what the later node needs through a placeholder. A handle spans runs: a later
  graph continues that conversation by naming the same handle with the same `subagent` --
  the handle never stands in for the agent, so every node still names its `subagent`. Use
  a fresh handle unless you mean to continue. A `[stateful]` node without a handle gets one
  of its own, reported in the summary, so it can be continued later.
- Raven appends each node's upstream memory-record paths to its prompt for you: no
  placeholder, input or waiting node is needed for them.

A draft, a critique, then a revision in the drafter's own session:

```json
{
  "task_summary": "Write the report introduction",
  "nodes": [
    {"id": "intro_draft", "subagent": "Raven-General", "node_summary": "Draft the introduction", "instance": "author",
     "prompt_template": "Owner's request, as written: \"Write a 200-word introduction to multi-agent systems for readers who know software but not agents.\"\nYour part: draft it."},
    {"id": "intro_critique", "subagent": "Raven-General", "node_summary": "Critique the draft", "depends_on": ["intro_draft"],
     "prompt_template": "Owner's request, as written: \"Write a 200-word introduction to multi-agent systems for readers who know software but not agents.\"\nYour part: critique this draft against that request, for clarity and accuracy:\n{{ intro_draft.output }}"},
    {"id": "intro_final", "subagent": "Raven-General", "node_summary": "Revise the draft", "instance": "author", "depends_on": ["intro_critique"],
     "prompt_template": "Owner's request, as written: \"Write a 200-word introduction to multi-agent systems for readers who know software but not agents.\"\nYour part: revise your earlier draft using this critique:\n{{ intro_critique.output }}"}
  ]
}
```

### Reading a task this conversation already finished

Ids are unique across the conversation, so an earlier task -- an earlier run's node, or a
`spawn` given a `node_id` -- is named by its id alone, from either tool, and needs no
`depends_on`. Re-doing work takes a new id; the earlier output stays readable. Only a task
that completed and wrote an output can be named. One that failed, was cancelled, was
skipped, was not executed, or is still running keeps its id, and naming it is refused
before anything runs, saying which case it is: re-do failed, cancelled or skipped work
under a new id, and for a node of a run still in flight, submit again once that run
reports its result. That last one inverts when you are replanning: a node of the replaced
run that has not finished -- waiting to start, running, or suspended on a report -- is not
waited on; depending on one carries it into the new run, where it goes on and writes its
output. Only the node you are answering is given up, and anything your plan neither lists
nor depends on stops.

## Conditional branches

One graph can decide where it goes next. Reach for this when what runs next depends on
what a step concludes -- a triage that settles whether an issue needs a code fix, a design,
or only an answer. Without it the run has to finish, you read the result, and you submit
a second graph, which costs a turn and a charge on the hourly dispatch allowance.

- `routes` makes a node a **decision node**: route name to the condition, in plain words,
  under which its answer selects that route.
- `select` is `"one"` (the default) or `"many"`: how many routes one answer may select.
- `when` guards any node: a boolean expression over what the decision nodes before it
  chose.

**The judge picks the route, not the node.** Once a decision node finishes, Raven's judge
-- the same call that decides whether it accomplished its task -- reads its answer against
your conditions. The node never sees the conditions, and a route it names in its own text
is evidence like the rest of its answer, never the selection. Two rules no check enforces:

- **A decision node's prompt asks for the conclusion, explicitly** -- "Conclude in one
  sentence whether this is a defect, a feature request, or a usage question" -- not for a
  route name.
- **The conditions are mutually exclusive and decidable**, and a `select: "one"` decision
  node gets a catch-all route whose condition is "anything else". Without it, an answer
  that fits no condition lands on the nearest route, down a branch the answer does not
  support, or on none, which fails the node.

**Guards.** `when` names `<node>.<route>` -- that decision node selected that route -- or
`<node>.done`, which means the node settled, whatever it chose. Only a decision node of
this graph that declares `routes` and is in this node's `depends_on` may be named. The
atoms are joined by `not`, `and`, `or` and parentheses, in lowercase;
`not` binds tightest, then `and`, then `or`.
Spacing between tokens is free; an atom is one token, written without spaces.

**Every decision node in `depends_on` must appear in the node's `when`.** A node cannot
depend on a decision node and stay silent about it; `<node>.done` is how you say "whatever
it chose". This holds for a decision node that depends on another decision node too.

**Guard the entry node of a branch, and every node of the branch that also depends on
something outside it.** A node whose every in-graph dependency was not executed is not
executed either, so a branch node needs no guard beyond naming any decision node it
depends on. A node of the branch that also depends on something outside it, such as the
decision node itself or a node that runs whichever route is selected, is not left out that
way: unless its own `when` names the branch's route, it can run when the branch does not.
Name the route, as `<node>.<route>`, not `<node>.done`, which holds whichever route is
selected; and list the decision node in its `depends_on`, since a guard may only read a
dependency. A merge node that should run on every route, like `reply` in the example
below, keeps `<node>.done`.

**What a guard decides.** It is read once every dependency has settled, and a failed or
skipped dependency skips the node first. True runs the node.
False leaves it **not executed** with the reason `when_false`;
unknown leaves it not executed with `when_unknown`. Unknown is not false: a route of a
decision node that was never executed is *unknown*, so `not risk.high` does not hold when
no risk was ever judged. A node whose every in-graph dependency was not executed ends the
same way, reason `upstream_not_executed` -- that check comes first, so `<node>.done`, true
whatever the decision node chose, cannot revive a node with no input left on that path. A
node that is not executed is never dispatched and writes no output.

**A decision node whose route cannot be established fails**, with "The route could not be
determined: <reason>"; there is no fallback route. A host with no model to judge with
refuses such a graph at submission.

**A missing input has a fixed stand-in.** When a node reads a dependency of the same graph
that was not executed, every placeholder form renders this sentence, and Raven tells the
node which inputs may be missing:

```
<This task is not executed and the output is missing, please go on without this output.>
```

Your part is the titles -- one titled block per input that may be missing:

```
## Bug fix result
{{ fix.output }}

## Design proposal
{{ design.output }}
```

Reach a node that may not run with `{{ X.output }}`, **not** with `ref:@nodes/`, which is
refused. A node *may not run* when it has a `when` of its own, even `<node>.done`, which
holds whichever route is selected, or when it depends on nodes of this graph that all may
not run.

### A worked example

The owner asked: "Handle issue 123 and draft the reply." What the issue needs depends on
the triage, and how closely a change is reviewed depends on what it touches. One graph,
two decision nodes, four guarded steps:

```json
{
  "task_summary": "Handle issue 123",
  "nodes": [
    {"id": "triage", "subagent": "Raven-General", "node_summary": "Classify the issue",
     "routes": {"bug": "the issue is caused by a defect in the code",
                "feature": "the issue asks for new behaviour",
                "question": "anything else: a usage question, or an issue that is neither a defect nor a request for new behaviour"},
     "prompt_template": "Owner's request, as written: \"Handle issue 123 and draft the reply.\"\nYour part: read issue 123. Conclude in one sentence whether it is a defect in the code, a request for new behaviour, or something else; give your reasoning after the conclusion."},
    {"id": "risk", "subagent": "Raven-General", "node_summary": "Judge the change risk",
     "routes": {"high": "a change would touch authentication, billing or a data migration",
                "low": "a change would touch none of those"},
     "prompt_template": "Owner's request, as written: \"Handle issue 123 and draft the reply.\"\nYour part: read issue 123 and conclude in one sentence whether a change would touch authentication, billing or a data migration."},
    {"id": "fix", "subagent": "Raven-General", "node_summary": "Fix the defect",
     "depends_on": ["triage"], "when": "triage.bug",
     "prompt_template": "Owner's request, as written: \"Handle issue 123 and draft the reply.\"\nYour part: fix the defect.\nTriage: {{ triage.output }}"},
    {"id": "design", "subagent": "Raven-General", "node_summary": "Write the design proposal",
     "depends_on": ["triage"], "when": "triage.feature",
     "prompt_template": "Owner's request, as written: \"Handle issue 123 and draft the reply.\"\nYour part: write the design proposal for the requested behaviour.\nTriage: {{ triage.output }}"},
    {"id": "deep_review", "subagent": "Raven-General", "node_summary": "Review the risky change",
     "depends_on": ["triage", "risk", "fix", "design"], "when": "(triage.bug or triage.feature) and risk.high",
     "prompt_template": "Owner's request, as written: \"Handle issue 123 and draft the reply.\"\nYour part: review the change for its impact on authentication, billing or a data migration.\n\n## Risk assessment\n{{ risk.output }}\n\n## Bug fix result\n{{ fix.output }}\n\n## Design proposal\n{{ design.output }}"},
    {"id": "reply", "subagent": "Raven-General", "node_summary": "Draft the reply",
     "depends_on": ["triage", "fix", "design"], "when": "triage.done",
     "prompt_template": "Owner's request, as written: \"Handle issue 123 and draft the reply.\"\nYour part: draft the reply to the reporter.\n\n## Triage\n{{ triage.output }}\n\n## Bug fix result\n{{ fix.output }}\n\n## Design proposal\n{{ design.output }}"}
  ]
}
```

`triage` and `risk` start together. At most one of `fix` and `design` runs, and neither on
`question`, the catch-all. `fix`, `design` and `deep_review` each open a branch, and each
carries that branch's guard. `deep_review` reads both branches but also depends on `triage`
and `risk`, which run on every route, so nothing leaves it out with the branches: its own
guard keeps it off the `question` path. `reply` is the merge node: `triage.done` runs it on
every route, and on `question` it reads the stand-in sentence in both titled blocks and
drafts the answer anyway. `fix` and `design` are only in its `depends_on`, since neither is
a decision node.

## Anti-patterns

- **Don't** ask a node to print a route marker for the graph to follow. The node's own text
  selects nothing; ask it for its conclusion instead.
- **Don't** repeat the entry guard on every node inside a branch. A node whose dependencies
  all lie inside the branch is left out with its entry, so a copy of the guard there says
  nothing. The exception is a node of the branch that also depends on something outside
  it, the decision node included: it needs the branch's route in its own `when`.

## Before you submit

- One call carries the whole graph. Each run, whatever its size, spends one unit of the
  hourly dispatch budget `spawn` also draws on (`max_subagent_spawns_per_hour`), so graphs
  submitted in a loop exhaust it.
- Every node has an `id` naming what it produces, its `subagent`, a `node_summary`, and a
  prompt that opens with the owner's request as written.
- Every node of this graph that a placeholder reads is in that node's `depends_on`, and
  every `inputs` key is referenced.
- Large upstream output reaches a `[local-files]` agent as a `_path` form.
- Every node a `when` names declares `routes`; every `select: "one"` decision node has a
  catch-all route and a prompt that asks for a conclusion.

## After you submit

The call returns as soon as the graph is accepted. A malformed graph is refused in your
own turn, before anything runs, so "started" means it passed every check. The run carries
on after your turn ends, and its result arrives as a new message.

- Carry on with the rest of the task, and tell the user the work is under way without
  promising an outcome you have not seen.
- Do not call `run_subagent_dag` again to check on it, and do not re-submit it: the result
  comes to you, and a second call runs the whole graph again.
- Pass `background: false` only when your very next step is reading the outputs. It blocks
  your turn until the run finishes or a node reports a problem, and the run finishes
  either way.

A run has no timeout: it ends when its nodes do, or by a manual stop, and a stopped run
reports nothing back.

## When a node cannot do its job

Finishing is not succeeding: a judge reads every node's answer against its task. A node
that could not do the work -- a missing credential, a missing piece of the request, a tool
that kept failing -- has its output withheld, and you get a report mid-run naming what is
missing and which nodes wait behind it, while the rest of the graph keeps going. Answer it
with `resolve_dag_node` through `tool_call`; the report carries the exact invocation and
that tool's schema, so take the spelling from there. In a `background: false` call the
report is the call's return value. What the four decisions mean:

- `continue` when you can supply what is missing. If only the user can -- a credential, a
  decision, a fact about what they want -- ask them, then continue with their answer: that
  is the case this mechanism exists for. Continuations are few, so a message that changes
  nothing wastes one.
- `reassign` when the subagent lacks what the task needs -- a tool, access to the files, a
  skill, or the strength for it. Choose by how well the roster's descriptions and tags fit
  the task; an agent that already tried the node is not excluded.
- `replan` when what is missing is the plan itself: a new run starts from the nodes you
  supply, and the work in progress your plan lists or depends on is carried into it without
  stopping. The node you are answering is given up -- redo its work under a new id -- and a
  finished node is read through `depends_on` and `{{ <id>.output }}`, never re-declared.
  Not because a node is hard: each replan starts its nodes on a fresh attempt budget, so
  replanning a hard node loops until the hourly dispatch limit stops it.
- `abandon` for work that turns out not to be needed: its dependents are skipped, and
  everything else finishes.

**Keep what should go on.** A replan carries only what its plan reaches. Depend on, or list,
every node in progress and every not-started node whose work is still wanted -- including the
leaf of every branch the replan is not about; the report lists both kinds. Everything else that
has not finished stops with the old run. A node in progress or queued is carried only as it is:
to change its work, add a node under a new id and leave that one out. A replan races the run --
one that redefines a node that has started meanwhile is refused whole, and you prepare it again.
After a replan the run to watch is the successor, whose id the result names; an answer to an
earlier report may still name the old run, and is routed to the run that holds the node now.

A `reassign` keeps its id, its task and its place in the graph; it spends a continuation,
like `continue`, and the attempt budget is not reset. The new agent's first prompt carries
a handover of the visible conversation -- what the node was asked and answered, as plain
text, with no tool calls, no tool output and no private context of the old agent -- so say
what went wrong in your message. On an `instance`, the run's other unfinished nodes on that
instance move with it, the handover is the instance's whole conversation, and whichever
moved node reaches the new agent first plants it, not necessarily the node you answered; a
target that takes more than one node must be `[stateful]`. Expect three refusals or
notices: a target that already has a conversation under the node's named `instance` is
refused -- choose another agent, or replan with a new instance name; a run submitted with
`confirm: true` asks the user again first; and when either agent runs on a registered
machine, the files earlier attempts wrote stay where they were made, so your message has to
carry what the new agent needs.

```json
{"run_id": "<run id>", "node_id": "<node id>", "decision": "reassign",
 "agent": "<roster name>", "message": "<what the new subagent should do>"}
```

To stop the whole run instead, call `cancel_dag` through `tool_call`: it discards the run
and starts nothing in its place.

## Reading the result

The summary -- a background run's announcement or a blocking call's return value -- lists
every node with its status and output path, plus any `error:` line, a `reason:` line for a
node a branch left out, and a decision node's `selected_routes:` (`(none)` is a real
selection under `select: "many"`). Only terminal outputs are inlined, and a completed node
whose dependents were all not executed counts as terminal. `read_file` any other output
path; the node's rendered prompt sits beside it as `<id>.prompt.md`, worth reading when an
answer looks wrong.
