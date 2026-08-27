# spawn prompt templates, a shared file gate, and one delegation package - design

Date: 2026-08-26
Status: proposed

## Goal

Give `spawn` the same file-based prompt assembly `run_subagent_dag` already
has, put both surfaces behind one capability gate and one untrusted-content
rule, and collapse the two delegation packages into one flat
`raven/agent/subagent/`.

## Why this is needed

### A multi-step task cannot hand its own output forward

A session that dispatches step 1 through `spawn` has no framework-provided way
to give step 2 what step 1 produced. `spawn`'s parameters are `task_summary`,
`task`, `subagent`, `instance` (`raven/agent/tools/spawn.py:125-168`) - none of
them carries a context reference.

The upstream leg is already solved and is worth stating precisely, because it
narrows the work: the framework does land the output and does report the path.
`SpawnRecord` (`raven/agent/subagent_history.py:168`) writes `prompt.md` before
dispatch and `out.md`, `error.md`, `meta.json`, and `transcript.jsonl` on
completion, and `manager.py:1154` appends `Record: <dir>` to the announcement
that re-enters the host's context. What is missing is only the downstream leg:
nothing puts that path into the next task, and `spawn`'s description frames the
record as an audit directory rather than as a handoff channel.

Three routes exist today and each stops short:

- `instance` continues one conversation with one stateful sub-agent. It does
  not carry A to B.
- Pasting the `Record:` path into `task` works for a built-in Raven sub-agent
  (the fence is off by default, `raven/config/schema.py:956`, and when on,
  `backends/raven_loop.py:210` puts agent home in the roots, where the record
  tree lives). Nothing tells the model this is a legitimate move, and it is
  silently wrong against a `[no-local-files]` agent.
- A later DAG node can read a spawn record by absolute path, because
  `<session_dir>/subagents/` is deliberately one of the reference roots
  (`subagent_dag/_paths.py:44-50`). Undocumented on the `spawn` surface.

### The capability gate is installed on one surface only

`reads_local_files` is consumed by the DAG package and by the roster formatter,
and nowhere else. On the DAG surface, aiming a `_path` placeholder at a
`[no-local-files]` agent is refused before anything is dispatched, with a
corrective message naming the content form to use instead
(`subagent_dag/_capabilities.py:245`). On the `spawn` surface the identical
mistake - a path pasted into `task` for such an agent - has no gate: the path
arrives as meaningless text, the run completes, and the sub-agent answers from
the surrounding prose. The defect this produces is not "the model must restate
the upstream result"; it is a silent success where the other surface has a
pre-dispatch rejection.

### Sub-agent output reaches another sub-agent unfenced

CONTEXT.md requires that sub-agent-controlled text pass through
`wrap_untrusted` (`raven/security/trust.py`) before entering another
sub-agent's prompt, and notes that a path needs no wrapping because the path is
raven-minted while the file's contents are not. `wrap_untrusted` does not
appear anywhere in `raven/agent/subagent_dag/`: `{{ <node>.output }}` inlines an
upstream sub-agent's raw output into a downstream prompt today. Only the path
back to the host is fenced (`manager.py:1144`). Adding `ref` to `spawn` opens a
second instance of the same channel, so the rule has to be implemented rather
than inherited.

### Two packages, one grammar

Sharing the machinery rather than duplicating it also removes a package cycle.
Measured import edges between `loop`, `subagent`, `subagent_dag`, and `tools`:

```
before                                after
loop         -> subagent, dag, tools   loop     -> subagent, tools
subagent     -> loop, tools            subagent -> loop, tools
subagent_dag -> subagent, tools
tools        -> subagent  (spawn.py)
7 edges / 2 cycles                     4 edges / 1 cycle
```

`spawn.py` is the sole author of the `subagent <-> tools` cycle. The surviving
`subagent <-> loop` cycle is unchanged and still handled by the deferred import
at `backends/raven_loop.py:193`.

## Scope

In:

- `spawn` takes `prompt_template` and `inputs`; `task` is removed.
- A shared prompt-template layer used by both surfaces: grammar, root
  confinement, capability gate, renderer, file backend.
- The capability gate applied to `spawn`.
- Untrusted-content fencing for inlined file contents, on both surfaces.
- Unrecognized `{{ ... }}` bodies pass through verbatim instead of raising, on
  both surfaces.
- `raven/agent/subagent_dag/`, `raven/agent/tools/spawn.py`, and
  `raven/agent/subagent_history.py` move into a flat `raven/agent/subagent/`.

Out:

- Node references on the `spawn` surface (`{{ <id>.output }}`,
  `inputs: {k: {"node": id}}`). See D3.
- Making a `spawn` call addressable the way a DAG node id is (minting node-level
  ids for spawn calls, folding them into `SessionNodes`, enforcing
  conversation-wide uniqueness). That is the "framework-guaranteed spawn to
  spawn" feature and is a separate change.
- Renaming `DagValidationError`. See D5.
- Dropping the `_` prefix from the DAG's module names. See D5.
- Routing the DAG through the spine or `Origin.SUBAGENT`. See D5.

## Decisions

### D1 - `task` is replaced by `prompt_template`, not joined by it

`spawn` gets one prompt field, named as the DAG names it. A second field saying
the same thing would make the model choose on every call.

The old spelling is accepted from `**kwargs` and treated as the new one. This
follows the precedent set by the `agent` to `subagent` rename, whose reasoning
is recorded in `SpawnTool.execute`'s docstring: a renamed field left to fall
into `kwargs` is swallowed silently and the call runs under the wrong
assumption, so doing what the call plainly means is worth three lines.

### D2 - only shape-matching placeholder bodies parse; the rest pass through

`_parse_body` currently raises `DagValidationError` on any `{{ ... }}` body it
does not recognize, and no escape syntax exists anywhere in the codebase -
`{{{{` is not an escape, and there is no literal form. With `prompt_template`
always rendered, a prompt like "why does `{{ item.name }}` not render in this
Vue component" would be refused before dispatch, and the model would have no
escape available: its only route is to break the braces or paraphrase, so it
could not hand template-bearing code to a sub-agent at all. That input is
ordinary for a coding sub-agent.

So a body parses as a placeholder only when it matches a known shape
(`inputs.<k>`, `inputs.<k>.path`, `<id>.output`, `<id>.output_path`, `ref:`,
`ref_path:`); anything else is emitted verbatim.

The discrimination lands in the right place: `{{ item.name }}` matches no known
shape and passes through, while `{{ inputs.pln }}` still matches `inputs.<k>`,
still enters resolution, and still fails on the missing key. Typos are caught;
unrelated braces are let through.

Residual hole, stated so it is not discovered later: a misspelled *suffix*
(`{{ plan.otuput }}`) matches no shape and is emitted verbatim. An escape
syntax would close it but requires the model to learn a new form and remember
to use it, and the call where it does not remember is the hard rejection this
decision exists to avoid.

Applies to both surfaces: one grammar should not have two dialects.

### D3 - no node references on the `spawn` surface

`spawn` gets `{{ inputs.<k> }}`, `{{ inputs.<k>.path }}`, `{{ ref:<path> }}`,
`{{ ref_path:<path> }}`. It does not get `{{ <id>.output }}` or
`inputs: {k: {"node": id}}`.

Node references would not buy the handoff this design exists for. Spawn to
spawn already works by absolute path: `check_confined` accepts an absolute
reference that lands inside a root, and `<session_dir>/subagents/` is a root,
which is where spawn records live. The `ref` content form is read host-side and
inlined, so it works against a `[no-local-files]` agent too. What node
references would add is spawn to *DAG node*, at the cost of dragging
`SessionNodes` and `output_paths` into the spawn dispatch path - and spawn's own
products are keyed by `call_id`, absent from `SessionNodes`, so they would not
cover spawn to spawn either.

Dropping that branch is what keeps the shared renderer's dependencies to
`backend`, `cwd`, and `roots`: it is exactly the `{"node": ...}` branch of
`_input_value` that needs the DAG store.

### D4 - inlined file contents are fenced by where the file lives

**Reversed during implementation. See Revisions, below.**

A resolved reference under the sub-agent history root (`<session_dir>/subagents/`,
`@runs/` included) is sub-agent-written, so its contents are wrapped with
`wrap_untrusted(..., source="subagent")` before substitution. A reference under
the session working directory is user and host material and is substituted
as-is.

The test is lexical and reuses the same `_within` comparison as
`check_confined`, so the fence decision and the confinement decision cannot
disagree about where a path landed. Fencing everything was rejected because it
would stamp a user's own spec document as untrusted external input, changing
what it means to the sub-agent reading it.

This is applied to the DAG's `{{ <node>.output }}` and `{"node": ...}`
resolutions as well, closing the pre-existing gap described above. That is a
behaviour change to existing DAG prompts and is deliberate: one grammar, one
rule.

### D5 - one flat package

`raven/agent/subagent_dag/` and `raven/agent/tools/spawn.py` move into
`raven/agent/subagent/` as sibling modules. No `dag/` subpackage.

Flattening collides three filenames that exist on both the DAG side and the
shared side (`_render.py`, `_capabilities.py`, `backend.py`), so the flat layout
carries a prefix convention: `dag_*` for graph-specific modules, `prompt_*` for
the shared template layer.

`_errors.py` becomes `prompt_errors.py`, not `dag_errors.py`: the shared grammar
raises from it, and a shared module importing a `dag_*` module would invert the
dependency. `DagValidationError` keeps its class name in this change - it is in
the DAG package's public `__all__` and `raven/playbook/validate.py` imports it.
The name becomes a misnomer on the spawn surface; renaming it is separate churn
and is out of scope.

The DAG's `_`-prefixed module names are kept. Moving, renaming, and editing in
one diff is not reviewable; de-underscoring is its own change or none.

A flat layout has no `dag` package to re-export from, and the DAG names are
deliberately *not* added to `subagent/__init__.py`. That file eagerly imports
`manager`, so re-exporting the DAG there would make `import
raven.agent.subagent` - which every `SubagentManager` consumer does - pull in
the graph model, the run store, and the runner. Keeping them out is what
preserves the optionality half of the decision this change is otherwise
reversing.

The cost of that is near zero, because no consumer relies on the re-export
today. All 17 `raven/`-side imports name a submodule directly
(`raven.agent.subagent_dag.tool`, `._graph`, `._store`, `.live`, `._resume`,
`._reader`, `._errors`, `.control_tools`), so each is a one-line path
substitution to `raven.agent.subagent.dag_<module>`. Only two test files import
from the package root (`tests/test_subagent_dag_runner.py:18` and
`tests/test_subagent_dag_core.py:14`); they are repointed at the specific
modules.

This reverses the letter of a decision recorded in
`subagent_dag/__init__.py`, which says the package is "NOT fused into
`raven/agent/subagent/` and NOT routed through `Origin.SUBAGENT` / the spine
scheduler". That docstring binds two separate claims. Only the directory claim
is reversed: the DAG keeps its own graph model, store, runner, and scheduler,
stays an optional tool, and is still not routed through the spine. The
docstring is rewritten to record the new decision rather than left contradicting
the tree.

### D6 - one merge request, with the move as its own first commit

The move is mechanical and its correctness has a property no other part of this
change has: the test suite should pass unchanged except for import paths. That
property is preserved at commit granularity - the move plus the extraction lands
as the first commit, with the full suite green there, and every behaviour change
lands on top. The squash collapses this on `main`, but a reviewer can check out
that commit and verify the move alone.

## Architecture

### Shared layer, and what stays graph-specific

| Shared (`prompt_*`) | Graph-specific (`dag_*`) |
|---|---|
| `{{ ... }}` grammar, shape matching | `output` / `output_path` resolution |
| root confinement, `@runs/` prefix | `SessionNodes`, cross-run node ids |
| `_path` form vs `[no-local-files]` gate | graph parse, cycle check, ready-set order |
| `ref` / `ref_path` / `inputs` resolution | node capability notices (skills, mcps) |
| untrusted fencing by resolved root | run store, projection, reader |
| local file backend | |

`dag_render.render_prompt` keeps its signature and composes the shared resolver
with its own `output` resolution, so the DAG's callers are untouched.

### spawn dispatch order

`SpawnRecord.open` writes `prompt.md` before the sub-agent is dispatched, so
rendering has to happen before `manager.spawn`:

1. `_reject_useless_instance` - unchanged.
2. Capability gate: a `_path` form against a `[no-local-files]` `subagent`
   returns a corrective string.
3. Render `prompt_template` with `inputs`; a `DagValidationError` is caught and
   returned as a corrective string.
4. `manager.spawn(task=<rendered>, ...)`.

Failures return strings rather than raising, matching `_reject_useless_instance`
and the rest of the tool's contract.

Roots are the two the DAG uses: `workdir.current()` and
`session_history_root(session_dir)`, the latter reached through the manager,
which already owns `_session_dir`. `@runs/` comes along with the shared paths
module, which lets a spawn read an earlier DAG node's file at no extra cost.

### Two prompt artifacts, deliberately different

`prompt.md` holds the *rendered* text - what the sub-agent actually saw, and
what the DAG stores as `<node_id>.prompt.md`.

The announcement's `Task:` line holds the *template*. `manager.py:1155`
concatenates it verbatim with no truncation, so an inlined file would be
re-injected into the host's context in full. The DAG has no equivalent exposure
because a node's rendered prompt never returns to the host - only its output
does.

Rather than thread a second value through `_run_subagent` and
`_run_subagent_inner` to four `_announce_result` call sites, the template travels
in the `origin` bag that already carries `instance`, `workspace`, and `agent`,
and is read once in `_announce_result`, falling back to `task` when absent. The
proactive spawn in `raven/proactive_engine/sentinel/executor/spawn.py` renders
nothing, takes the fallback, and needs no change.

## Files

| Path | Origin | Change |
|---|---|---|
| `subagent/spawn_tool.py` | `tools/spawn.py` | move; `prompt_template` + `inputs`; gate; render |
| `subagent/history.py` | `agent/subagent_history.py` | move only |
| `subagent/prompt_placeholders.py` | `subagent_dag/_placeholders.py` | move; D2 shape matching |
| `subagent/prompt_paths.py` | `subagent_dag/_paths.py` | move only |
| `subagent/prompt_capabilities.py` | `subagent_dag/_capabilities.py` (gate only) | generalize to `(template, subagent)` |
| `subagent/prompt_render.py` | `subagent_dag/_render.py` (`ref`/`input` branches) | extract; D4 fencing |
| `subagent/prompt_backend.py` | `subagent_dag/backend.py` | move only |
| `subagent/prompt_errors.py` | `subagent_dag/_errors.py` | move only |
| `subagent/dag_*.py` (9) | `subagent_dag/` remainder | move; compose shared resolver |
| `subagent/dag_tool.py` | `subagent_dag/tool.py` | move only |
| `subagent/dag_control_tools.py` | `subagent_dag/control_tools.py` | move only |
| `subagent/manager.py` | - | `origin` carries the template; `_announce_result` reads it |
| `agent/loop/main.py` | - | import paths |
| `raven/playbook/{types,validate}.py`, `raven/cli/playbook_commands.py`, `raven/web_rpc/methods_config.py`, `raven/rpc/methods/{dag,instances,subagent}.py` | - | import paths |
| `ui-tui/src/lib/toolArgs.ts` | - | `PREVIEW_KEYS` gains `prompt_template` |
| `CONTEXT.md` | - | define the prompt-template layer and the fencing rule |

## Testing

- `tests/test_subagent_prompt_template.py` - new. Grammar and shape matching
  (including the D2 pass-through and its residual hole), root confinement, the
  capability gate, and the D4 fence decision on both sides of the root
  boundary.
- `tests/test_subagent_manager.py` - `spawn`'s new contract: `prompt_template`
  required, `task` accepted as the old spelling, `inputs` forms, gate refusals
  returned as strings, `prompt.md` rendered while the announcement carries the
  template.
- `tests/test_subagent_dag_runner.py:2194` - must change: it asserts the
  unrecognized-placeholder rejection that D2 removes.
- The files importing `subagent_dag` and `subagent_history`: path substitution
  only, no assertion changes, except the two test files named in D5 that import
  from the DAG package root. A green suite at the move commit is the evidence
  for D6.
- `ui-tui` vitest for `toolArgs`, run serially - the suite flakes above ~100
  files under default worker parallelism.

## Revisions

Four decisions above did not survive implementation. They are left as written, with
what replaced them, because the reasoning that was wrong is more useful to the next
reader than a document that was never wrong.

**D4 is reversed: the fence follows the kind of reference, not the file's location.**
The original rule fenced a read whose resolved path landed under the sub-agent history
root and left every other read bare, on the grounds that fencing a user's own spec
document would mislabel it. That reads the working directory as the user's material,
and it is not reliably: a repository checked out there is exactly where text that must
not be taken as instructions arrives. A file's contents are now fenced as `file`
wherever the file sits, another node's output as `subagent`, and a literal input not at
all -- the author typed that one into the call. The location signal is gone, and with
it the parameter that carried it through four functions.

**Confinement is decided physically, not lexically.** The same section argued that
reusing one textual comparison for both the fence and the confinement check kept them
from disagreeing. It also meant a symlink inside a root could name a target outside
every root, and the read followed it: reproduced end to end, with the contents of a
file the roots exist to exclude arriving in a third-party sub-agent's prompt. Both the
reference and the roots now go through `realpath`, which also keeps a root that is
itself reached through a link from refusing its own files.

**`inputs` arrived on `spawn` from the other direction while this was in flight**, as a
parameter whose entries are read and prepended to the task under a heading, framed per
block with the key name and the path each was read from. Material travels only through
placeholders here, so that machinery is withdrawn rather than carried: placement is the
template's, and naming the material and its provenance is the dispatching model's job
inside the template it writes. Two things were kept from it -- refusing the call before
dispatch when an input cannot be read, and carrying the authored template beside the
rendered prompt so the completion announcement never reads an inlined file back into
the host's context.

**A declared input that no placeholder references is refused.** Nothing is prepended,
so such a key was material that silently did not arrive while the run still read as
finished -- the failure the parameter exists to prevent. The refusal lives in the shared
layer, so both surfaces answer the same way.

One refusal was also deliberately reversed in the other direction: `@runs/` resolves on
the `spawn` surface rather than being refused there. A spawn has no run history of its
own, but it is dispatched from a conversation that may have one, and that root already
sits inside the sub-agent history a reference may reach.

## Risks

**The D4 fence changes existing DAG prompt text.** Every downstream node that
reads an upstream's output now sees it inside an untrusted fence. Sub-agent
behaviour may shift on prompts that were tuned without it. This is the intended
correction, but it is the one change here that alters what an existing,
working graph sends.

**D2 weakens a validation the DAG has today.** A misspelled placeholder suffix
now reaches the sub-agent as literal text instead of being refused. Accepted
for the reason in D2; the alternative is a hard rejection with no escape.

**The flat package is 35 modules at one level.** Navigability rests on the
`dag_*` / `prompt_*` prefixes rather than on directory structure. Chosen
deliberately; the prefixes are load-bearing and new modules must follow them.

**`DagValidationError` is raised on the spawn surface.** A refused `spawn` call
reports an error class named for the other surface. Cosmetic, and renaming it
touches `raven/playbook/validate.py`.

**One merge request covers a move, a grammar change, a security change, and a
tool-contract change.** D6's first-commit discipline preserves the move's
verifiability but removes the enforced review breakpoint between the four.
