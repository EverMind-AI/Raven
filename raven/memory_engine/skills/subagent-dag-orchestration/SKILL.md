---
name: subagent-dag-orchestration
description: Use when a task breaks into several distinct steps that a separate sub-agent could each carry out. A DAG node dispatches to an agent on the roster and cannot call your own tools, so work whose steps are reading files, editing code or running commands is never a DAG, however many steps it has - do that yourself. For work that does clear that bar, test three things before running the steps one at a time: are two or more steps independent (they can run at once), does a step hand a large artifact to the next (it can pass by file instead of through your context), do the steps want different specialists (the tool lists the roster). If any of the three holds, orchestrate the whole task as one run_subagent_dag call.
metadata: {"raven":{"emoji":"🕸️","always":true,"inject":"description","requires":{"tools":["run_subagent_dag"]}}}
---

# Sub-Agent DAG Orchestration

## When to use

The trigger is **a task that breaks into several distinct steps** — not a task you
have already decided needs sub-agents.

**First, the hard bound.** A DAG node dispatches only to a configured third-party
sub-agent; it cannot call your own tools. So work whose steps are reading files,
editing code, or running commands is **never** a DAG, however many steps it has and
however independent they are — do that yourself. Only work a sub-agent on the roster
could carry out gets as far as the tests below.

For work that clears that bound, test three things before running the steps one at a
time:

- **Independence** — can two or more steps run at the same time? Independent nodes are
  scheduled concurrently, up to the shared sub-agent cap
  (`max_concurrent_subagents` — `spawn` draws on the same allowance).
- **Artifact size** — does a step hand a large result to the next one? A node's output
  is written to a file the downstream node reads, so it never passes through your
  context.
- **Specialism** — do the steps want different sub-agents? The tool's own description
  lists the roster and what each one is for.

If any of the three holds, express the whole task as **one** `run_subagent_dag` call
rather than dispatching sub-agents one at a time. A fourth property comes free once
you do: every node's prompt and output is persisted, so intermediate steps stay
readable afterwards.

If none of the three holds, the graph buys you nothing — dispatch the work as a single
`spawn`, which is the right call exactly when the task is genuinely one sub-agent doing
one thing. The work you do yourself is the work that never cleared the bound above, not
this.

Every install can run a graph: raven's own agents are on the roster whether or not any
third-party agent is configured, so `run_subagent_dag` is always available. Which agents
exist is still the tool description's answer, not this file's — read the roster there.

## How it works

You submit a flat list of `nodes`. A scheduler runs every node whose dependencies are
met, as soon as they are met. Each node's prompt is rendered from its template,
dispatched to its sub-agent, and the reply is written to a file under the run
directory. Downstream nodes reference upstream outputs through template placeholders.
When the run finishes you get the terminal-node outputs plus every node's output-file
path — as a message once the run ends, or as the call's own result when you asked for
`background: false`.

A DAG run has no timeout — a long run is ended by hand (manual stop), not by a clock.

## Background by default

The call returns as soon as the graph is accepted, naming the run:

```
DAG run 20260729T031500Z-1a2b3c4d started in the background (3 nodes). I'll report the
result when it finishes -- keep working, and do not submit this graph again.
```

The graph keeps running after your turn ends, and its full result is delivered to you as a
new message when it finishes — the same way a `spawn` reports back. So:

- **Don't** re-submit the same graph, and don't call the tool again to check on it. There is
  nothing to poll; the result comes to you.
- **Do** carry on with whatever else the task needs while it runs.
- Tell the user the work is under way, without promising the outcome you have not seen yet.

Pass `background: false` only when you genuinely cannot continue without the outputs — for
instance when the very next thing you must do is read them. That blocks your turn until every
node is done and returns the summary below as the call's result.

A malformed graph is rejected in your own turn either way, before anything is dispatched, so
a call that returns "started" has already passed every check.

A stopped run reports nothing back — if the user cancels it, no announcement arrives.

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
| `subagent` | yes | Which agent runs this node. Use one of the names listed in the tool's own description — don't invent them. Raven's own agents (`research-raven`, `code-raven`, …) are on that list too, so a graph needs no third-party agent configured. |
| `prompt_template` | yes | Template rendered into the node's prompt. May contain the placeholders below. |
| `depends_on` | no | Upstream node ids that must finish before this node runs. May also name a node an earlier run in this conversation completed, which is already finished and so only records the dependency. |
| `inputs` | no | Object mapping a key to a literal string, to `{"file": "<path>"}`, or to `{"node": "<id>"}` for another node's output. |
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

### Node ids are unique across the conversation

**A node id may not repeat an id any earlier run in this conversation used.** The graph is
rejected if it does. That is what makes `{{ <id>.output }}` mean one thing, so pick ids that
say what the node produced — `pricing_research`, `deck_script_v2` — not `a`, `step1`, or a
generic `plan` you will want again. Re-doing work needs a new id; the earlier node's output
stays where it is and stays referenceable.

### Reading an earlier run in this conversation

Because ids are unique, **an earlier run's node is named by its id alone**:

```
{{ pricing_research.output }}        that node's output text, whichever run produced it
{{ pricing_research.output_path }}   its path
```

That node needs no `depends_on` — there is nothing to order, it has already finished.
Listing it anyway is accepted and changes nothing, so write the edge if it makes the graph
read better. `depends_on` is still *required* for a node of *this* graph, since that edge
is what makes the upstream node run first.

Only a node that **completed** can be named this way, in a placeholder or in `depends_on`
alike. A node that failed, was cancelled, was skipped, or belongs to a run still in flight
keeps its id — nothing else may take it — but has no output
to read, and naming it is refused before any node of your graph is dispatched. The refusal
says which of the four it is, because the fix differs: re-do failed, cancelled, or skipped
work under a **new** id, and for a run still in flight, submit again once it reports its
result.

Note what is *not* an option in any of the four: re-creating that node here. Its id is
taken, so a graph that repeats it is refused for the reuse instead. Re-running an upstream
step *as a node of your own graph* only works for one that does not exist yet — naming the
taken id in `depends_on` does not re-run anything.

A node id is all you ever need to name a node — there is no run qualifier on these forms,
because there is nothing left to disambiguate. Two other ways to say the same thing:

```
inputs: {"prev": {"node": "pricing_research"}}     then {{ inputs.prev }} / {{ inputs.prev.path }}
{{ ref:@runs/<run_id>/pricing_research.out.md }}   by file path
```

Use the `{"node": ...}` input form when one upstream feeds several placeholders. Use
`ref:@runs/<run_id>/...` when you have a path rather than an id — it reads the run
directory directly, so it also reaches files that are not a node's output
(`graph.json`, `<node>.prompt.md`) and runs recorded before ids were indexed. `<run_id>` is
the id reported when that run finished.

### Where a reference may point

`ref:` / `ref_path:` / `{"file": ...}` paths resolve inside two roots — the **session
working directory** (what a plain relative path is relative to) and **this conversation's
sub-agent history**, which may be named by absolute path. Anything outside both is
rejected, as is a `@runs/` path that climbs out of the run history.

The second root is this conversation's own record: its DAG runs, and the `spawn` calls
beside them. It stops there. The user's long-term memory, the installed skills, and every
*other* conversation's transcript sit outside it and are refused — so reference an earlier
run's outputs, or files the user pointed you at.

## Examples

The `subagent` values below are placeholders — substitute the names the tool reports
as available.

### Fan-out then aggregate

Two researchers run in parallel; a writer waits for both and reads their outputs by path:

```json
[
  {
    "id": "research_web",
    "subagent": "claude_code",
    "prompt_template": "Research recent web-framework benchmarks and report your findings."
  },
  {
    "id": "research_papers",
    "subagent": "claude_code",
    "prompt_template": "Summarize the latest papers on async runtimes."
  },
  {
    "id": "synthesize",
    "subagent": "claude_code",
    "depends_on": ["research_web", "research_papers"],
    "prompt_template": "Write a briefing merging these two sources.\nWeb findings: {{ research_web.output_path }}\nPaper summary: {{ research_papers.output_path }}"
  }
]
```

`research_web` and `research_papers` have no dependencies, so they run at the same
time. `synthesize` runs once both finish; as the terminal node, its output comes back
to you inline.

### Pipeline with a reused stateful instance

A draft is written, reviewed, then revised. The same session (`instance: "author"`)
carries context from the draft into the revision. This shape needs an agent tagged
`[stateful]`; on a `[stateless]` one the graph is rejected before anything runs:

```json
[
  {
    "id": "draft",
    "subagent": "claude_code",
    "instance": "author",
    "prompt_template": "Draft a 200-word introduction for a report on multi-agent systems."
  },
  {
    "id": "review",
    "subagent": "claude_code",
    "depends_on": ["draft"],
    "prompt_template": "Critique this draft for clarity and accuracy:\n{{ draft.output }}"
  },
  {
    "id": "revise",
    "subagent": "claude_code",
    "instance": "author",
    "depends_on": ["review"],
    "prompt_template": "Revise your earlier draft using this critique:\n{{ review.output }}"
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
- research_web [completed]: <run-dir>/research_web.out.md
- research_papers [completed]: <run-dir>/research_papers.out.md
- synthesize [completed]: <run-dir>/synthesize.out.md

Terminal outputs:
### synthesize
<synthesize's output text>
```

- `Node output files:` lists **every** node with its status and output path (failed or
  skipped nodes show `(no output file)` plus an `error:` line).
- Only terminal-node outputs are inlined. To review any other node's work, `read_file`
  its output path.
- The run dir also holds `<node>.prompt.md` (the rendered prompt), `graph.json`, and
  `manifest.json` — useful when a node's answer looks wrong and you need to see what it
  was actually asked.

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
  `depends_on` — an earlier run's node is the case that needs no edge.
- **Don't** guess `subagent` names — only names on the roster resolve.
- **Don't** share an `instance` handle across nodes to "keep them in order" on a
  `[stateless]` agent — order without shared context is what `depends_on` already gives.
