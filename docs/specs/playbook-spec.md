# Playbook file format

> **Implementation status (2026-08-20, `refactor/unified_agent_registry`)**: node definitions are now one with dag's
> (`DagNodeSpec`; both camelCase and snake_case wire spellings kept). Three changes relative to this doc:
> node-level `confirm` **field deleted** (the gate exists only at graph level); `skills` / `mcps` become **playbook-only,
> engine-consumed** fields (the dag tool no longer has these params; `skills` folds into `promptTemplate` before dispatch),
> a three-state (omitted and `[]` both mean "nothing to recommend" / a list = point out these relevant ones in the prompt);
> `instance` **actually takes effect** now (built-in agents are always stateful), and the "non-head nodes of the same handle must
> not write skills" restriction is deleted with the old mechanism (`skills` now lands on the user message appended to every node).
> The `triggers` and top-level `confirm` responsibilities rework (group J) **has also landed**: funnel and LLM gating deleted;
> the entry is the single tool `load_playbook`; `confirm` sits on dag's graph-level parameter. Also new beyond this doc:
> `subagent` / `nodeSummary` / `promptTemplate` may be left blank, filled in by the caller with `fills`; see the end of section 5.

A playbook = one directory; inside it, a single `playbook.md`, split into three zones.

```
~/.raven/playbooks/
  competitor-scan/
    playbook.md
```

````markdown
---
name / description                     # identity envelope
---

Prose: the manual for humans; the machine does not parse it; identical across both modes

```yaml playbook-spec
all machine fields
```
````

**Recognition is by path, not by field**: anything under the `playbooks/` scan root is a playbook. The skill `SKILL.md` is not reused, so no marker like `metadata: '{"raven": {"playbook": true}}'` is needed -- two criteria for the same thing would breed two contradictions, "placed under skills yet declared a playbook" and "placed under playbooks yet missing the declaration", and neither rule needed to exist.

**Three zones instead of cramming everything into the frontmatter**, for reading order: the prose manual is usually a dozen-odd lines, while the machine fields, once `promptTemplate` is included, often run into the hundreds; short first, long after, reads smoothly.

Naming inside the block is camelCase.

---

## 1. frontmatter

Two fields, no more may be added. They sit here so the indexer can build its listing from the file head alone, without parsing the whole document.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Globally unique id, = directory name, `^[a-z0-9][a-z0-9-]*$` |
| `description` | string | yes | One-sentence intent, written as "when should I use me", <= 200 chars |

---

## 2. Block -- top-level fields

| Field | Type | Required/Default | Description |
|---|---|---|---|
| `taskSummary` | string | Required | One sentence on what running this playbook dispatches, shown to the user; it and `description` (section 1) answer two different questions -- `description` answers "when should I use me", this one answers "what does running it do" |
| `version` | integer | default `1` | The format version, not a content version |
| `mode` | enum | Required | Where the graph comes from: `dag` graph is fixed in `nodes`; `prompt` graph is assembled on the spot by the model from `prompts` |
| `confirm` | boolean | default `true` | Whether the user must confirm before dispatch. **It is a dag graph-level parameter**: the value written in this file is injected together with the nodes and locked (the model cannot change it); the gate is at the dispatch point, not on the discovery path -- the model picking a playbook is not the user agreeing to run it |
| `triggers` | object | Required | see section 3 |
| `params` | map | Optional | run-time inputs, see section 4 |
| `nodes` | list | required for dag, >= 1 / forbidden for prompt | see section 5 |
| `prompts` | string | required for prompt / forbidden for dag | Graph-assembly guidance: tells the model how to assemble a graph -- which agents, how many layers, who depends on whom |

`mode` and `nodes` / `prompts` are mutually validated; a violation fails loading.

**The two modes differ only at the "where does the graph come from" step; once the graph is in hand, it is exactly the same pipeline**: validation (section 8) -> confirm gate -> background async execution -> run_id receipt -> write-back on completion. So `confirm`, the `instance` rules, and the placeholders apply identically to both modes; no execution-shape field is defined.

A graph assembled in `prompt` mode must match the `nodes[]` structure of section 5; it passes the same validation before running, and if no valid graph can be assembled it errors rather than running anyway.

---

## 3. triggers

| Field | Type | Required | Description |
|---|---|---|---|
| `keywords` | list[string] | yes, >= 1 | Substring match after normalization: lowercase, full/half-width folding, whitespace compression; Chinese is not word-segmented. Long words and short phrases both go here |

`keywords` decides **which playbook is placed in front of the model this round** -- it is a retrieval cue, not a trigger. A large library uses top-K recall (copying the skill-retrieval approach), so keywords serve to raise recall, not to guarantee use. A hit does not mean it will run:
the playbook, together with its description and parameter table, enters the optional list of the `load_playbook` tool; whether to use it is decided by the model with the entire session context,
the same role keywords play in skill retrieval. With a small library, everything is listed (`playbooks.router.topK` defaults to 5; a library at or below that lists everything),
and keywords only affect ordering and recall.

**The two costs are handled separately**: the `enum` of `name` covers **the whole library** (a name is a few tokens; even if recall misses it, the user can still call it by name),
while the description + parameter table + blank-field list **renders only top-K** (that is the expensive part; fixed with K, it does not grow with the library).

**Both modes share the same discovery path.** No branching by `mode` -- otherwise, though they look the same as playbooks to the user, one would be auto-triggered and the other only findable by retrieval, a directly perceivable behavioral difference.

---

## 4. params.&lt;key&gt;

Declares the inputs that vary per run. Referenced in `promptTemplate` / `prompts` as `${params.<key>}`.

| Field | Type | Required/Default | Description |
|---|---|---|---|
| `type` | enum | default `string` | `string` / `integer` / `number` / `boolean` / `enum` / `path` |
| `required` | boolean | default `false` | When true and no value is present, ask; the one asked is the model (which then asks the user) |
| `default` | any | Optional | With a default value, never ask |
| `enum` | list | required and non-empty when `type: enum` | The value table. Gives the model a multiple-choice question, more accurate than fill-in-the-blank |
| `description` | string | Required | The parameter description; also used as the wording for the missing-parameter follow-up |

Three consumers: **the parameter table at the execution entry** (this whole block renders into the `load_playbook` tool description; the model relies on it to know which keys to pass -- without the render-out, the model can only guess key names, and wrongly guessed keys are silently dropped), the missing-parameter follow-up, and compile-time substitution of `${params.x}`.

`type: path` passes `check_confined` at compile time and may not escape the session working directory.

A playbook with no variable inputs (e.g. "pull an issue triage once a week") omits the entire block.

---

## 5. nodes[]

| Field | Type | Required/Default | Description |
|---|---|---|---|
| `id` | string | Required | `^[A-Za-z0-9_-]+$`, unique within the graph, becomes the artifact file name |
| `subagent` | string | Required | The `name` in the agent registry. The access method (cli / acp / in-process), credentials, and capability metadata all live in the registry; the playbook does not redeclare them |
| `nodeSummary` | string | Required | One sentence on what this step does, written before the `promptTemplate`; it is this step's line in the run, for the people watching the graph, not the model's own memo |
| `promptTemplate` | string | Required | This step's task brief; placeholders in section 6 |
| `dependsOn` | list[string] | default `[]` | The ids of the nodes it depends on; also the reference whitelist |
| `skills` | list[string] | Optional | The skills **relevant** to this step, for the agent running it to consider first (only pickable from the directories already present on this machine). **Playbook-only, engine-consumed**: folded into this step's `promptTemplate` before dispatch; the dag tool has no such parameter. Neither "can only see these" nor "must use them" |
| `mcps` | list[string] | Optional | The mcps this step wants to attach. **Playbook-only, and not in effect today**: subagent has no mcp channel; dispatch explicitly reports them ignored and does not write them into the prompt (writing them in would be a fake instruction the agent has no tools to execute) |
| `instance` | string | Optional | The session handle; nodes with the same handle share one agent session |
| `inputs` | map | Optional | Each key is a literal string, `{file: path}` or `{node: id}` -- **only these three, no run qualifier** (to pin a specific run, use the file form `{{ ref:@runs/<run_id>/... }}`, see section 6). The value is **structure**, not a unique capability: a key referenced by `{{ inputs.k }}` must be declared first (default-deny; inline `{{ ref: }}` cannot be validated at this level), and reading the node definition tells you what it reads without going through the prompt |

**This table is the dag tool's node schema, not a separate set defined by the playbook.** Per-node configuration is a dag capability itself;
once a playbook is loaded, its nodes are injected directly into the dag tool, so the model can write these fields when calling dag directly too.

"The same set" refers to the field set and the semantics; two shape differences do not count as a separate set: the model-facing JSON Schema uses snake_case (`prompt_template` / `depends_on`) while this table is the camelCase written in the file; and at dispatch the `id` gets a `<playbook name>-<random 6 chars>-` prefix, with `dependsOn` and `{{ id.output }}` rewritten in sync -- because ids on the dag side are **unique within a session**, and without the rewrite, running the same playbook a second time in one session would be rejected by the uniqueness rule. The prefix lives only at run time; it never enters the file.

`instance` manages exactly one thing: **whether to carry the context forward**. `promptTemplate` is that injected prompt;
no extra field exists, and a new prompt can still be injected when the context is carried forward.

`skills` folds into this step's `promptTemplate` (the user message appended to every node), so writing it on any node of an `instance` chain
takes effect -- the old "only allowed on the head node" restriction was deleted together with the old mechanism, see validation rule 9.

Configuration hangs off nodes, not roles, because the same agent can run several steps in one graph, each with a different task --
that is just two tasks, two contexts, using the same agent and the same calling convention:

```yaml
nodes:
  - {id: a1, subagent: research-raven, dependsOn: [],   skills: [market-research]}
  - {id: b,  subagent: code-raven,     dependsOn: [a1]}
  - {id: a2, subagent: research-raven, dependsOn: [b],  skills: [code-audit], mcps: [github]}
```

**Loading is a single tool `load_playbook(name, params, fills?)`; `mode` decides what happens after loading** (the model does not need to distinguish modes; the list mixes both kinds together). The two execution surfaces guarantee different strengths:

| mode | After loading | Who presses the dispatch button | What can be locked |
|---|---|---|---|
| `dag` | The engine fills params and injects nodes, straight into the dag pipeline; missing required `params` or blank fields come back to ask the model one round first | **engine** | **Nodes and gates fully locked**. The model can pass only `params` and `fills`; there is no syntax to express "edit an already-written field" -- a structural guarantee, not enforced by validation |
| `prompt` | Returns the graph-assembly guidance with `${params.x}` filled in | **model** (it writes its own `nodes` from it and calls `run_subagent_dag`) | Buys **flexibility**: the same guidance assembles different graphs per the situation at hand -- that is its reason to exist. The cost: the top-level `confirm` and the steps have no attachment point (there is no original graph to compare), so the guidance text must make them explicit |

The two modes are a trade-off pair, not "one complete, one crippled": for **determinism** (the same graph a hundred times, gate and steps locked) write `dag`; for **flexibility** (the process itself decided case by case) write `prompt`. Patching `prompt` mode's flexibility as if it were a defect would turn it into a worse `dag` mode.

### 5.1 Blanks and `fills` (implemented)

`subagent`, `nodeSummary` and `promptTemplate` may be left blank, meaning "this one I leave undecided; the caller writes it from context". Only these three count as **gaps** --
omitting `skills` means "use this agent's own menu", which is a complete answer; treating it as a gap would make every well-written playbook in the library
pop a question first.

| | Behavior |
|---|---|
| How gaps are reported | Missing `params` and blank node fields are reported **together**, with **zero dispatch**. Reporting in two rounds makes the caller spend two round trips to learn one thing |
| How to fill them | `fills = {"<the node id the author wrote>": {"promptTemplate": "..."}}`. The key is the **id in the file**, not the one with the run prefix -- the prefix is added afterwards; the caller has never seen it |
| Hard rule | **`fills` pointing at an already-written field rejects everything**. Without this rule, `fills` is a general-purpose field editor: change any prompt, point a node at another agent, strip the skills -- the file in git would no longer describe what actually ran |
| `skills: []` still does not count as blank | For `fills` it is a **written** field, so the caller cannot fill it. Semantically it is already synonymous with omitting (see next section), but "the author wrote it" and "the author did not" are two different things: treating it as a blank lets the caller stuff values into a field the author deliberately touched |
| Round-trip cap | The same (session, playbook) reports gaps at most 2 rounds; beyond that, a termination message is returned. "Cannot fill -> ask again -> still cannot fill" is a loop the caller could idle away round after round |
| CLI | There is no model to fill, so `raven playbook run` gained `--fill NODE.FIELD=VALUE`; without it, it reports "this playbook needs values filled in at run time" and does not silently run a graph with missing fields |

`params` and blank fields are two different variation points: `params` is a **value** (declared once, referenced in many places, with type and requiredness validation); a blank is **a whole field left unwritten** (the model fills it freely from context).

**Not every field present is the norm; three layers each own a segment.** You need not write every field when producing a playbook:

| Who provides | Content | What if missing |
|---|---|---|
| This file | The orchestration of `nodes[]` and the per-step configuration | The field set is a subset of the dag node fields, but **requiredness is looser**: `subagent` / `nodeSummary` / `promptTemplate` may be left blank for the model to fill. The other five are optional; **"not written" is not "empty"**, see the table below |
| The model (reading context) | The values of `params` + the `fills` for blank fields | `required: true` without `default` must have a value; when missing, `load_playbook` returns structurally "still needs these" with zero dispatch, and the model calls again after filling them. **The model can pass only `params` and `fills`** -- it has no syntax to express "edit an already-written field" |
| The caller | Run-level parameters such as `background` | dag-only; this file never declares them |

| Field | What omitting equals |
|---|---|
| `skills` | No skills mentioned in the prompt. If written, the prompt points out these relevant ones for priority consideration. `[]` is synonymous with omitting (see below) |
| `instance` | At run time a handle is automatically forged with an independent context, reported back in the run summary, and resumable later |
| `mcps` | No extra mcp attached |
| `dependsOn` | A start node, running concurrently with the other start nodes |
| `inputs` | No declared inputs (`{{ ref: }}` in `promptTemplate` can still read files directly) |

So "which skills this step gets, left for the model to pick from context" needs no special syntax -- omitting `skills` is exactly that.

**`skills` is a suggestion, not a gate.** It folds into this step's `promptTemplate` and is handed, together with the task description, to the agent
running this step, saying "these are relevant to the job, consider them first". It does **not** express "can only see these" (the menu is no longer filtered;
the other skills stay in front of it), nor "must use a particular one" (which one is always decided on the spot by that agent from context).

So `skills: []` carries no meaning anymore -- it is neither "see zero" (a restriction) nor "forbidden to use" (a command); it is simply "nothing
to recommend", synonymous with omitting. **But not silently**: dispatch explicitly reports it, because `skills: []` written in the file looks like
a requirement was stated. To really express "no skills for this step", write it in `promptTemplate`.

`mode: prompt` is the extreme case of this layering: the file holds no nodes at all; all node fields are produced by that run-time compose call, then pass the same validation as `mode: dag` (rule 12).

**There is only one gate level**, the top-level `confirm`: it governs "should this playbook run". No node-level gate -- approving a graph is complete semantics in itself; what the reviewer sees is the whole graph.

In implementation it is dag's graph-level `confirm`: the value written in this file is injected together with the nodes, and the gate sits at the
dag tool's dispatch point (after validation, before billing -- a graph the user cancelled should not cost budget). This parameter is the **precondition
for deleting the funnel**: before it existed, `confirm`'s only execution point was inside the funnel; deleting the funnel would have made every playbook "run the moment the model calls it".

Since picking the playbook is the model's decision, this gate level is the **user's** only point of intervention, so the accompanying requirement is mandatory, not optional: the confirmation dialog must list which steps have external side effects (publishing, filing tickets, sending messages), making the "yes" an informed one. The criterion is derivable from the registry -- whether the step's agent has write operations among its mcp / tools.

---

## 6. Placeholders

| Syntax | Time | Where written | Meaning |
|---|---|---|---|
| `${params.x}` | compile time | `promptTemplate` / `prompts` | The parameter value |
| `{{ dep.output }}` | run time | `promptTemplate` | The full output text of the dependency node |
| `{{ dep.output_path }}` | run time | `promptTemplate` | The file path of the dependency node's output |
| `{{ inputs.k }}` | run time | `promptTemplate` | The value of the node's `inputs` (file form takes the content) |
| `{{ inputs.k.path }}` | run time | `promptTemplate` | The file path of the node's `inputs` (file form only) |
| `{{ ref:path }}` | run time | `promptTemplate` | The content of a workspace file |
| `{{ ref_path:path }}` | run time | `promptTemplate` | The path of a workspace file |

Compile time substitutes only `${...}`; `{{...}}` is passed through verbatim to run time.

Both modes use all seven: `prompt` mode first substitutes `${params.x}` into `prompts` and hands it to the model; the graph the model assembles still carries `{{...}}`, resolved by the runner at run time.

The `output` vs `output_path` choice: local agents receive the path (they read it themselves; large artifacts do not occupy context); remote API agents can only receive content. An agent whose `readsLocalFiles` in the registry is false is stopped by the pre-check from using `output_path`.

---

## 7. Graph language

`dependsOn` is the entire graph language; parallelism is implicit -- no dependency means parallel, no `parallel` syntax needed.

| Spelling | Meaning |
|---|---|
| `dependsOn: []` or omitted | A start of the graph, starts immediately |
| `dependsOn: [a]` | Waits for a to finish |
| `dependsOn: [a, b]` | Waits for both a and b to finish, merging |
| Two nodes with the same upstream | No dependency between them; parallel, forking |

```yaml
nodes:
  - {id: scan_market, dependsOn: []}
  - {id: scan_tech,   dependsOn: []}
  - {id: merge,       dependsOn: [scan_market, scan_tech]}
  - {id: selfcheck,   dependsOn: [merge]}
```

```
scan_market -+
              +-> merge --> selfcheck
scan_tech ---+
```

`dependsOn` is also reference authorization: the `x` in `{{ x.output }}` must be listed in this node's `dependsOn`, otherwise a compile-time error (default-deny).

Graphs assembled in `prompt` mode are bound by the same rules; it is not an escape hatch.

**Three things that cannot be expressed**: cycles (conditional fallback, e.g. "review fails, fall back to rewriting"), conditional skipping (no `when:`), per-playbook concurrency caps (governed by the runner's global config).

Note that `mode: prompt` **cannot** bypass these three. It is a graph generator, not a run-time orchestrator -- a graph is fixed once assembled; mid-run it cannot be changed based on intermediate results. So loops like "stop after two consecutive rounds without new findings" are beyond both modes; `prompts` can only spell "how to assemble this graph", not "how to judge once it runs".

---

## 8. Validation rules

All run at load time; anything failing goes to quarantine.

| # | Rule |
|---|---|
| 1 | `mode: dag` -> `nodes` non-empty and no `prompts`; `mode: prompt` -> has `prompts` and no `nodes` |
| 2 | `frontmatter.name` = the directory name, and the directory sits under the `playbooks/` scan root |
| 3 | `nodes[].subagent` must be in the registry; when missing, report "X must be registered first", not blow up when that step runs |
| 4 | The graph is acyclic, and every node is reachable from the starts |
| 5 | The `x` in `{{ x.output }}` must be within this node's `dependsOn` |
| 6 | The `x` in `${params.x}` must be declared in `params` |
| 7 | `instance` is available only for agents marked `stateful` in the registry. **This rule is a dead letter today**: only the generator and `raven playbook validate` run it; hand-written input run directly is silently dropped (see unified registry plan, section 9) |
| 8 | Nodes sharing an `instance` must have a dependency chain between them -- a shared session cannot run concurrently; they would stomp each other's context |
| 9 | **Deleted**. Its rationale was "after a session starts the system prompt cannot be swapped, so only the head node's `skills` take effect"; but `skills` now folds into `promptTemplate`, which is the user message appended to every node, so writing it on any node of the chain takes effect. Keeping it would be a false rule that rejects valid graphs |
| 10 | Members of the same `instance` must be **the same agent** -- different agents sharing a handle share no session at all (existing rule, carried over) |
| 11 | `{{ dep.output_path }}` is usable only when the registry marks that agent `readsLocalFiles` |
| 12 | A graph assembled in `mode: prompt` passes all of rules 4-11 before execution; if invalid, reassemble; if still invalid, error out -- no degraded run |

---

## 9. Example -- mode: dag

````markdown
---
name: competitor-scan
description: Research one competitor's market and tech sides in separate tracks, then merge into a report with cited sources. Matches when you want a quick read on a competitor.
---

# Quick competitor scan

The market and tech sides are researched in two parallel tracks; after merging into a document, self-check once.
Every conclusion must carry a source; mark anything uncertain as "to be verified".

```yaml playbook-spec
mode: dag
confirm: true
taskSummary: Concurrently research one competitor's market and tech sides, merge into a sourced report, and self-check to finalize

triggers:
  keywords: [competitor, rival, benchmark, competitive landscape, help me look at this company]

params:
  target:
    type: string
    required: true
    description: Which competitor to scan

nodes:
  - id: scan_market
    subagent: research-raven
    nodeSummary: Research the market side: positioning, pricing, customer structure, competitive landscape
    dependsOn: []
    skills: [web-search, source-credibility-check]
    mcps: [exa]
    promptTemplate: |
      Research ${params.target}'s market side: positioning, pricing, customer structure, competitive landscape.
      At most 3 bullet points per dimension, each with a source. Do not touch technical details.

  - id: scan_tech
    subagent: research-raven
    nodeSummary: Research the tech side: technical approach, open-source ecosystem, engineering maturity
    dependsOn: []
    skills: [web-search, repo-analysis]
    mcps: [exa, github]
    promptTemplate: |
      Research ${params.target}'s tech side: technical approach, open-source ecosystem, engineering maturity.
      Do not touch the business side.

  - id: merge
    subagent: content-raven
    nodeSummary: Merge both tracks' findings into a conclusion-first report, every conclusion cited
    dependsOn: [scan_market, scan_tech]
    instance: w1
    promptTemplate: |
      Merge both tracks' findings into one report, conclusions first, every conclusion carrying its source.
      Market side: {{ scan_market.output }}
      Tech side: {{ scan_tech.output }}

  - id: selfcheck
    subagent: content-raven
    nodeSummary: Self-check to finalize by checklist: sources, no speculation, no duplication
    dependsOn: [merge]
    instance: w1
    promptTemplate: |
      Self-check by checklist: every conclusion has a source, no unmarked speculation, no duplication.
      After fixing, output the final version.
```
````

`scan_market` and `scan_tech` are both start nodes, running in parallel; `merge` waits for both; `merge` and `selfcheck` share `instance: w1`, running in the same content session, so the self-check remembers what was just written.

---

## 10. Example -- mode: prompt

````markdown
---
name: due-diligence
description: Run due diligence on a target company, with depth adjusted dynamically by findings. Matches background checks before investing, acquiring, or partnering.
---

# Due diligence

The investigation scope varies with focus; the graph shape is not fixed, so instead of hard-coding nodes, give graph-assembly rules.

```yaml playbook-spec
mode: prompt
confirm: false

triggers:
  keywords: [due diligence, dd, background check, check this company out]

params:
  target:
    type: string
    required: true
    description: The target company name
  focus:
    type: enum
    enum: [tech, market, team, finance]
    default: market
    description: The area of emphasis

prompts: |
  Assemble a three-layer due-diligence graph for ${params.target}, emphasizing ${params.focus}.

  Layer one, one node:
    subagent: research-raven, skills: [web-search]
    The task is a broad scan, producing the three columns "known / unknown / questionable".

  Layer two, spread by focus, all dependsOn layer one, parallel to each other:
    - Always one research-raven node checking team background and public risk records
    - focus includes tech: add a code-raven node, mcps: [github], scanning open-source repos and tech blogs
    - focus includes market or finance: add a data-raven node doing comparable-company and market-size estimates
    Each node takes the scan results via {{ first-layer-node-id.output }} and digs only into the "questionable" items.
    Do not give these nodes the same instance -- each keeps an independent context, so early wrong conclusions do not amplify across branches.

  Layer three, one content-raven summary node, dependsOn all of layer two's nodes.

  Every node's promptTemplate must state: any "the company claims X" must be tagged with its source and credibility,
  not mixed in with verified facts; write "not disclosed" for what cannot be found, mark "to be verified" when unsure.
```
````

`prompts` spells out **how to assemble this graph** -- how many layers, which agent pairs with which skills/mcps at each layer, who dependsOn whom, how data flows between nodes. Do not write "how to judge once it runs": the graph is fixed once assembled; there are no decision points at run time.

---

## 11. Full field index

```
frontmatter   name | description

block top-level taskSummary | version | mode | confirm | triggers | params | nodes | prompts
  triggers    keywords
  params.<key> type | required | default | enum | description
  nodes[]     id | subagent | nodeSummary | promptTemplate | dependsOn |
              skills | mcps | instance | inputs

placeholders  ${params.x}
              {{ dep.output }} | {{ dep.output_path }}
              {{ inputs.k }} | {{ inputs.k.path }}
              {{ ref:path }} | {{ ref_path:path }} (@runs/<run_id>/... pins a specific run)
```
