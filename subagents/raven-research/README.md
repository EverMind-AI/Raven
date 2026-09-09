# raven-research

Raven-X, a **Deep-Research-only agent build**: it answers a research question by
searching and reading the web, then commits an answer. Everything a
general-purpose assistant needs (chat channels, proactivity, user memory,
skills, TUI) is pruned or being pruned. What the model sees is fixed by
`drFlow.toolsAllowlist`, which unregisters everything else at assembly: the two
web tools, and on this `-filetools` profile six local-file tools. `ask_user` is
the exception that needs no entry - the flow appends it to the allowlist itself
when `drFlow.askUser` is on, so the list and the switch cannot disagree.

Owner: ZuyiZhou. This folder is our caller-side record, not the agent itself.

## Where it comes from

| | |
|---|---|
| Source | https://github.com/ZuyiZhou/Raven-X, branch `main`, commit `a903a424` |
| Local checkout | `./Raven-X` - the agent itself lives inside this folder |
| Local patches | **seven**, recorded as `git apply`-able diffs under `./patches/` - see the `Local patches` section below |
| Package / version | `raven` 0.1.5, flow `dr@3.5` (updated 2026-09-02 from upstream `a903a424`, the tip of `main`; the base moved back onto `main` and the sufficiency-gate branch content now rides as `patches/dr_sufficiency_gate.patch`, because upstream has not merged that branch - see the `What the main port changed for us` section below. The step before took `0a09f07` on 2026-08-27, the tip of `feat/dr_sufficiency_gate` rather than `main`, which added the first-round sufficiency gate, verify stall observability, appendix grounding accuracy and a per-call provider timeout. The gate is off by default and takes no version bump - it removes no tools - so `config.json` still loads as `dr@3.5-filetools-askuser` and needed no new key. The step before it took `4447f4d4` on 2026-08-26, which added the `ask_user` broker round trip and the deep report template - see the section below; the same day's earlier step took `6b5ec31`, a bugfix-only re-vendor on the same flow: ACP cancel/prompt race, AgentLoop kwarg wiring, memory backend lifecycle, shell null-device guard. `dr@3.5` itself arrived 2026-08-25 from `7b603aa`, which also brought the ACP serve entry - see the `dr@3.5` section below. `dr@3.4` came from `71abb5a6` on 2026-08-21, when upstream folded `dr@3.5`-`dr@3.7` back into `dr@3.4`; the new `dr@3.5` is a fresh rung under upstream's launch convention, not the folded one back. `dr@3.3` from `e3edf28` on 2026-08-18, `dr@3.2` from `Raven-X-main.zip` on 2026-08-16, and the `dr@2.8` / `dr@2.9` steps from `b68085d` and `b1c12e4` on 2026-08-11) |
| Upstream ancestry | forked from EverMind-AI/Raven at `dbb1b0c` (2026-07-17), diverged since |
| Docs to read | `README.md`, `QUICKSTART.md`, `examples/README.md` in that checkout |

**The repo is reachable from this machine now.** It was private with no clone
path when this folder was set up, which is why the early steps arrived as zips;
as of 2026-08-20 `git ls-remote` lists its 9 branches and `main` fetches, so an
update no longer depends on someone mailing an archive. Fetch it read-only and
never add it as a push remote.

It declares itself unrelated to upstream Raven going forward, so do not expect
our fixes to apply, or theirs to arrive. The Python package is still importable as
`raven`, which is why it must stay in its own venv - see below.

## Install

`requires-python >= 3.12`; the venv resolved to 3.13.

```bash
cd Raven-X
uv sync
uv run pytest -q tests/test_agent_flow_dr.py     # no keys or network
```

Run the suite with an isolated `HOME`:

```bash
HOME=$(mktemp -d) .venv/bin/python -m pytest -q
```

The isolated `HOME` is the safe default rather than a current requirement: on this
build the whole suite passes under the real one (5402 passed, 72 skipped, 2026-08-21),
including `tests/test_cli_sentinel_commands.py::test_sentinel_status_runs`, which used
to fail on the host's `/root/.raven/config.json` carrying `subagents` fields the
schema forbids (`extra_forbidden`).

One leak of that kind is now closed *in the checkout* rather than worked around:
`render._language_directive()` resolves from that same host config at prompt-assembly
time, and this machine sets `"language": "zh"`, which prepends 101 measured characters
to the identity block and moved eight prompt-byte shas. `tests/conftest.py` pins it
empty for the suite. Note what it does not pin: a **run** still picks the directive up,
because the spawned CLI reads the host `HOME` too - that is production behaviour here,
not a test artifact.

`uv sync` bakes absolute-path shebangs into `.venv/bin/`, so **moving the
checkout breaks the console script** - confirmed the hard way when this folder
was relocated here: the shebang still pointed at the old path and
`.venv/bin/raven` failed outright. `uv sync --reinstall` rewrites them. `run.py`
resolves the checkout relative to itself, so it survives a move of the whole
folder; only the venv needs the reinstall.

## The LLM it runs on

`config.json` pins the model this agent is tuned for, and `subagent.json` records
the same choice as `recommendedLlm` so a reader does not have to infer it. Two
files naming one model can drift, so `install.sh` compares them and reports a
mismatch rather than preferring one.

Set this folder's api key and that pinned model is what runs. **Leave the key
blank and the agent inherits the host raven's LLM instead** - its `providers`
block, its `agents.defaults.provider` and `model`, and its `routing`. The rest of
`agents.defaults` stays this folder's: the token ceiling, the tool-iteration cap
and the timeouts are operating limits tuned for this agent's job, and they have
nothing to do with whose key is paying. The optional Serper and Jina keys fall
back the same way, each on its own.

The host's provider block is copied wholesale rather than matched by name. A
section of the same name here and there can address two different endpoints, so
picking by name would point this agent at a gateway its model is not served on -
which reads as a bad answer, not as an error.

Inheriting is a fallback, not an equivalence: a measurement taken on the pinned
model does not carry over to whatever the host happens to run. The launcher
says which one was used on stderr at startup (journaled by the host), in the
form `llm: inherited from <path> (provider=... model=...); tuned for
<recommended>` - once per server now rather than once per turn, since the
config is rendered at launch and the server lives across turns.

With no key here and no provider key in the host config either, the launcher
refuses rather than starting something that cannot answer.

## Research modes

Three research budgets over one checkout. `config.json` is the complete
baseline and *is* the medium profile; `modes/high.json` and `modes/max.json`
are diffs over it. One identity prompt, one provider block, one `.env` - the
overlays carry budget knobs and nothing else, which a unit test pins
(`test_the_shipped_overlays_carry_only_budget_knobs`).

**A mode is per session, not per connection** (changed 2026-08-28). `run.py`
no longer merges an overlay at render time; it writes the three of them into
the rendered config as `acp.modes`, and the agent composes a profile per
session from the baseline plus that mode's diff. So:

- `session/new` answers with the ACP spec's `modes` object - `currentModeId`
  plus the catalogue - and `session/set_mode` moves a live session between
  them. This is the spec's own session-mode surface, not the
  `session/set_config_option` channel the main repo's ACP agent uses for its
  model picker: that one exists there because `session/set_model` is *not* in
  the stable schema, and session modes are;
- `--mode {medium,high,max}` now only says which profile a session **starts**
  in, and lands as `acp.defaultMode`;
- diffs, not merged blocks, is load-bearing: a merged block would carry
  `identityOverride` - the whole identity prompt - once per mode, which is the
  drift the one-prompt-one-place rule exists to prevent. A launcher test pins
  that the rendered file holds it exactly once;
- a switch **never interrupts a running turn**. The engine is resolved once per
  turn, at its start, so the turn in flight keeps the profile it began with and
  the next one gets what was asked for. `AcpLoops` does it by comparing each
  session's mode against the one its resident engine was built under -
  releasing at the moment of the switch would take the tools out from under
  whatever was running;
- a mode is **not persisted with the transcript**. A stored session reopened
  later starts at `defaultMode` again: how much effort a question deserves is
  the caller's judgement at the time of asking, not a property of the
  conversation.

`session/set_mode` stays method-not-found when a deployment declares no
`acp.modes` - which is every other folder here - so nothing about them changed.

**One roster row, not three** (changed 2026-08-28). The modes were briefly
three entries - `Raven-Research`, `-Deep`, `-Ultra` - over this one folder.
They are one entry again now that the host can name a mode per call:

- the main agent picks with the `spawn` tool's `mode` parameter, whose enum is
  **measured, not declared**: the capability probe already opened a throwaway
  `session/new` to read the model list, and it reads the mode catalogue from the
  same reply. A row that named its own modes would drift the first time this
  folder gained or dropped one;
- a person picks with `subagents.instance.set_mode` on a direct chat, which is
  the case three rows could never serve: switching effort mid-conversation
  meant abandoning it and starting a new one against a different agent;
- three rows were three names for one agent, three connection pools against one
  provider quota, and three roster entries the dispatching model had to tell
  apart on description alone.

| | `medium` (default) | `high` | `max` |
|---|---|---|---|
| For | factual / single-topic questions | multi-faceted topics one pass of evidence does not settle | explicit requests for exhaustive research |
| `drFlow.maxIterations` | 20 | 30 | unset (falls back to the cap below) |
| `agents.defaults.maxToolIterations` | 40 | 60 | 150 |
| `budgetNote.warnRatio` | 0.3 | 0.8 | 0.8 |
| sufficiency gate | on, minSearches 1 / minFetches 2, timeouts 30/15 | on, minSearches 5 / minFetches 2, default timeouts | off |
| `verify` timeouts | 120/40 | default 360/120 | default 360/120 |
| `fetchGate` | on, k=15 | on, k=15 | on, k=15 |
| `search.saturation` | on, k=5, widen | on, k=10, widen | on, k=10, widen |
| `finalShape.reportDepth` | on | on | on |
| `finalShape.reportBounce` | off | on | on |
| `askUser` | on, `when_needed` | inherited | inherited |

Every `drFlow` cell of that table is held against the measured arm by
`tests/test_agents_research_flow_parity.py` in the trunk's own suite (the one
CI runs): it builds the catalogue the launcher writes, composes each mode
through the fork's `build_session_modes`, and compares effective values with
`profiles/student_sglang_web_dr.json` field by field, with a written reason for
every difference - the 20 and 30 caps are named as the exhaustion window they
open, not excused as bounds. The fork's own `tests/test_shipped_flow_parity.py`
covers the fork's profiles and examples, not this folder's `config.json`, which
is the config the launcher actually serves. The same file holds the trunk
twin's **class defaults** and its retired-label tables against this checkout's,
and since 2026-09-07 the relation is "the twin may lead, never lag", not
equality: this checkout is kept as the record of upstream `a903a424` and is no
longer re-vendored, while the twin takes upstream's changes directly (the first
was `ea19b948`, dr@3.7). Every default the twin has moved past the record is
named in that file's `TWIN_LEADS` table with the upstream commit and reason; a
difference with no entry fails, a twin-only field with no entry fails, and an
entry whose values have stopped differing fails, so the allowance cannot go
stale. The retired-label check requires every retirement this checkout makes
to be present in the twin unless its base is one the twin has since retired.
Intentional differences live in that table; anything else is drift. The slice
comparison in `tests/test_agents_research_launcher.py` cannot see a default (a
written value hides it), which is why the class defaults are compared at all.

`agents.defaults.requestTimeoutSeconds` is deliberately **not** on that list.
It is the only knob the three profiles used to differ on that a session cannot
own: it is consumed once, at provider construction (`cli/_helpers.py`), and the
provider is process-wide. It is uniform at 600s now - which costs a medium turn
nothing, because it is a hang guard on a single request rather than a budget:
what bounds a medium turn is `maxIterations` and the sufficiency gate.

Points a reader should not have to re-derive:

- **The default changed behaviour on 2026-08-27.** The single `Raven-Research`
  entry used to run what is now the max budget; existing callers now get the
  medium profile - 20 DR iterations, the sufficiency judge consulted from the
  first search - unless they name a heavier mode. What they do *not* give up is
  the report: `reportDepth` is on in all three, so the deep template is the
  product's one report shape and never a reason to pick a mode.
- **Sufficiency floors are trigger floors, not strictness.** `minSearches: 5`
  on high means the judge is not even consulted before five searches have been
  invested; it makes the gate *later*, not harsher. On max the gate is off:
  exhaustive is the request. Fail-open direction is always "keep researching".
- **The hygiene breakers (`fetchGate`, `saturation`) are on in all three
  modes**, max included: they cut pathologies (search-without-fetch spirals,
  saturated query families), not depth - `onSaturate: widen` broadens instead
  of stopping. Their pricing numbers were measured on the bench arms, not on
  this model/profile; watch the `fetch_gate` / `saturation` ledger counters
  before retuning `k`.
- **No mode is a benchmark entry.** The sufficiency gate is unmeasured
  (upstream requires its own A/B arm), and the breakers change the measured
  distribution. A benchmark wants its own config, pinned byte-for-byte,
  not any of these three.
- **`askUser` is identical in all three modes on purpose**, so the overlays
  carry no delta for it and the identity prompt's "you may ask once" promise
  holds everywhere. `when_needed` asks only when the question is genuinely
  undecidable without the user, so a clear factual question on the medium mode
  pays no clarify round trip.
- One `everos.agentId` for every mode: one research memory, whichever budget
  retrieved it. This was already true when the modes were separate rows, and it
  is why collapsing them lost nothing.
- One pooled ACP connection for the whole folder, launched lazily. Every mode
  is served by it, so the sessions of a medium call and a max call share one
  process, one provider credential and one `acp.userPool` - which is the point:
  three rows were three uncoordinated pools against one provider quota.

Routing lives in `owns`, and routes the agent rather than the mode: the identity
segment renders every owning agent, and there is one of those now instead of
three equal claimants. Which mode a call runs in is the spawn tool's `mode`
argument, whose menu is built from what the probe measured - so the guidance for
choosing between them rides on each mode's own description (`MODE_LABELS` in
`run.py`) rather than on a roster line that exists from the moment the folder is
installed, probe or no probe.

## Updating to a newer upstream

**As of 2026-09-07 this checkout is not updated.** It is kept as the record of
upstream `a903a424` plus the seven patches, and upstream's later commits are
ported into the trunk twin under `agents/raven-research/plugins/research-flow`
instead (see the parity note above). The procedure below is retained for the
day that decision is reversed; nothing in it has been exercised since.

The checkout carries **seven local patches**, all under `./patches/`, so an update
is a replacement of `Raven-X/` followed by re-applying them. Everything else
adapts from beside the checkout (`run.py`, `config.json`, `subagent.json`, this
file) and needs no touching.

Patch-free is the state to get back to, not the state to assume. It has been both:
the tree was `a958b837` plus six locally patched files during the `ask_user` step,
went patch-free when upstream took five of the six back in `ed4a866` and declined
the sixth for the reason we had written down against it, and then picked up the
`web_search` gate. The check below is what settles which it is - read it rather
than this paragraph, because a paragraph goes stale and a diff does not.

Check it before replacing, rather than assuming it: the base commit is whichever
one whose tree the checkout matches, found by diffing trees since no file records
it, and a real local edit shows up as a file that differs there.

```bash
mkdir /tmp/up && git -C <upstream clone> archive <commit> | tar -x -C /tmp/up
diff -rq --no-dereference -x .venv -x __pycache__ -x .ruff_cache -x .pytest_cache \
    /tmp/up subagents/raven-research/Raven-X          # expect: no output at all
```

Three ways to misread that diff, all live in this tree. `--no-dereference` is not
optional: `Raven-X/CLAUDE.md` is a **symlink** to `./AGENTS.md` (mode 120000) here
and upstream alike, and a comparison that follows the link reports a 275-line edit
that does not exist - compare link text, or compare blob modes. Compare **ASTs**
when a `.py` file does differ, because a formatter run upstream reflows strings and
moves blank lines exactly like a real change (`raven-code`'s 2026-08-20 update
found seven such files hiding one real patch). And an empty diff against the
*wrong* commit is not a pass, so name the SHA you archived rather than a branch:
during the `ask_user` step two upstream branches sat one character apart
(`feat/add_ask_user_too` and `...tool`, three commits apart), both fetched, and
neither errored.

If a swap ever does have to carry a patch again, record it as a `git apply`-able
diff beside the checkout **in the same change**, not as a paragraph here. This
folder has already lost one the other way: the everos backend's `_timestamp_ms`
coercion was carried here, dropped in an upstream swap, and survived only as a row
in the `dr@3.3` table below, because there was no patch file to re-apply it from.
Upstream has since rebuilt it (`backend.py:321`, now parsing ISO-8601 as well),
which closes that particular hole but not the one in the process.

## Local patches

Seven, each `-p1` from the `Raven-X/` root, and since the 2026-09-02 re-partition
they are mutually **disjoint at the file level** - every changed file is carried
whole by exactly one patch - so their relative order is documentation rather
than a constraint (the old rule that `product_surface_audit` must apply after
`acp_per_session_loops` died with the re-partition: the `ACP_INTEGRATION_PLAN.md`
deletion is now the audit patch's alone, and nothing else touches that file).
Each patch is the **whole** base-to-checkout diff of its files, so a lone patch
still applies to the pristine base and the set reproduces the tree in any order:

| Patch | Files | What it is |
|---|---|---|
| `patches/acp_per_session_loops.patch` | 6 source, 8 test | Serves each ACP session from its own `AgentLoop` (`raven/acp/loops.py`), so two sessions against one agent process stop sharing an engine. Since 2026-09-01 also carries the per-session MCP surface that grew on the same seam: a session's `mcpServers` stanzas connect into a registry overlay that session owns (`SESSION_MCP_CAPABILITY` in `raven/acp/protocol.py`, the `ContextVar` overlay in `raven/agent/tools/registry.py`), and the replay/live update frames carry the tool's own name as `_meta` `raven.toolName`. Since 2026-09-02 also carries `DRFlowConfig._SUPERSEDED_PROFILES` in `raven/config/raven.py` (the file was already this patch's) and its test in `tests/test_config_raven_loader.py`: the whole-label refusal of a retired profile label, see the label row below |
| `patches/web_search_providers.patch` | 6 source, 14 test, 4 doc | The pluggable web search and fetch providers, and with them the gate that withholds `web_search` unless a provider key or a corpus endpoint resolves. That gate is the trunk invariant `web-search-gated-on-its-key`, which `scripts/check_vendored_invariants.py` asserts on this fork every CI run - so this one is not optional, and a swap that drops it fails the build rather than reaching a user. (The `fetch_result_ok` `raw_decode` fix this patch used to carry landed upstream in `ba06912` and is no longer ours) |
| `patches/prompt_cache_provider_probe.patch` | 6 source, 3 test | Gives the provider objects the `supports_prompt_caching` method the three `AgentLoop` construction sites already read off them. Without it every site resolved to `None` and `CacheOptimizer` fell back to the id-only lookup, silently, because the fallback answers |
| `patches/acp_session_modes.patch` | 6 source, 4 test | Reads `acp.modes` from the config, advertises them on every session response, and answers `session/set_mode`, with `AcpLoops` rebuilding a session's engine at the next turn boundary when its mode moves. What makes medium, high and max one agent process rather than three roster rows |
| `patches/dr_sufficiency_gate.patch` | 10 source, 8 test, 1 doc | Upstream's own `feat/dr_sufficiency_gate` branch (its PR #14, tip `0a09f07`), carried as a patch because `main` has not merged it: the first-round sufficiency gate (`raven/agent/flow/sufficiency.py`), verify stall observability, the appendix citation recovery (`_match_form` for truncated and comma-tail citations), a per-call provider timeout, the `examples/dr_shallow.json` product profile, and the gate branch's pause-defer test in `tests/test_agent_fetch_gate.py` (dropped unrecorded in the 2026-09-02 revendor, restored on review). Our `config.json` and the high mode set the gate's floors, so this one cannot be dropped until the branch merges. Drop it the moment it does. Also carries the reviewer trail on `verify.py`: `DraftReviewerGate` writes the model it ran on onto its `installed` row and every verdict row |
| `patches/product_surface_audit.patch` | 2 source, 2 test, 1 deletion | What is left of the 2026-08-31 audit fixes after upstream took the bulk back in `ba06912`: `cited_never_surfaced` is computed through `_match_form` rather than bare set membership, so a truncated citation of a listed link reads as unopened, not fabricated (`raven/agent/process_appendix.py`); `looks_failed` treats the registry-wide bare `Error` prefix as failed, which `WebSearchTool._failed` (colon-only) does not (`raven/cli/_progress_line.py`). Also deletes `ACP_INTEGRATION_PLAN.md` - internal-only planning doc, not shipped here on purpose. (`unwrap_untrusted`'s scan-from-the-end and the base `cited_never_surfaced` split are upstream's own now and left this patch.) Since 2026-09-02 also carries two appendix fixes from the product-feedback review of the 31m24s run: `cited_fence_tags`, a disclosure counter for answers that cite the security fence's `web_fetch #...` nonce instead of a URL (the observed run wrote 75 of its 87 citation handles that way and the grounding check said "nothing to check" - a false green; the nonce never reaches the ledger, so this is disclosure on the `cited_schemeless` model, not resolution), and an unreviewed banner that leads the trail block whenever the shipped answer carries no reviewer verdict (`budget_spent`, `unavailable`, or a salvage) instead of hiding as one word in the head line. Since 2026-09-03 the trunk twin (`agents/raven-research/plugins/research-flow/research_flow/support/process_appendix.py`) carries this module's whole body - these two fixes and upstream's 08-28/09-01 appendix work - with the fork's regression tests, and `tests/test_agents_research_process_appendix.py` pins the two module bodies equal with docstrings and comments stripped. Also carries the reviewer trail's read side: the appendix takes the judge model off the last verdict and renders `reviewer: pass via <model>`, with `verify_model` beside `verify_outcome`. Since 2026-09-06 also carries `_fold_path`, which folds the parts of an arXiv or forge address that do not name the document before the grounding checks read it - four arXiv path prefixes and a version stamp are one paper, and a forge resolves an owner and a repository case-insensitively, so a run that opened either form read the page the answer cites. The fold stops at the naming segments: a path inside a repository is case-sensitive, and folding it would let a fabricated deep link absolve itself against a real one |
| `patches/ask_user_gate.patch` | 1 source | `AskUserGate` treats a hook rollback's re-sample as past the turn boundary (`after_rollback`) and revokes its standing grant whenever it withholds the tool. Without it a reviewer reject at iteration 1 - a memo-answered follow-up turn is the common shape - rolled back to the same iteration number, re-offered `ask_user`, and the model put a question to the user after the review. The rollback count it reads (`ctx.metadata["hook_rollbacks"]`) is written by the loop, whose one-line change rides in `web_search_providers.patch` because `raven/agent/loop/main.py` already belongs to that patch; the gate's and the loop's tests ride there too for the same reason (`tests/test_agent_flow_ask_user.py`, `tests/test_agent_loop_hook_dispatch.py`). `clean_questions` also drops a repeated question text, so a duplicate neither renders twice nor crowds out a distinct third question |

Four are ours (`acp_session_modes`, `product_surface_audit`, `ask_user_gate`, and the
`dr_shallow.json` `PER_CONFIG` reconciliation riding inside
`dr_sufficiency_gate`'s copy of `test_shipped_flow_parity.py`; the appendix
files themselves are carried whole by `product_surface_audit` per the table
below, wherever a given refinement in them originated);
`dr_sufficiency_gate` itself is upstream's unmerged branch, and the other three
are the trunk's, carried here because this checkout is downstream of them.
`prompt_cache_provider_probe` is in flight on the trunk side and should be
dropped from here the moment upstream carries it. None has been offered
upstream yet, though session modes are stable ACP and so are offerable.

**Fifteen files mix more than one of those concerns**, and each is carried whole
by exactly one patch rather than split across them - a split cannot be re-applied
independently, and pretending otherwise is how a patch set silently stops
reproducing its tree:

| File | Carried by | Also changed by |
|---|---|---|
| `raven/agent/loop/main.py` | `web_search_providers` | per-session loops |
| `CONTEXT.md` | `web_search_providers` | per-session loops |
| `examples/README.md` | `web_search_providers` | sufficiency gate |
| `raven/cli/agent_commands.py` | `prompt_cache_provider_probe` | web search providers |
| `raven/cli/gateway_commands.py` | `prompt_cache_provider_probe` | web search providers |
| `raven/providers/base.py`, `endpoint_rotor.py`, `litellm_provider.py` | `prompt_cache_provider_probe` | sufficiency gate (the per-call timeout) |
| `raven/cli/acp_commands.py` | `acp_session_modes` | the other acp patches |
| `raven/config/schema.py` | `acp_session_modes` | per-session loops, web search providers |
| `raven/config/raven.py` | `acp_per_session_loops` | sufficiency gate |
| `raven/agent/process_appendix.py` | `product_surface_audit` | sufficiency gate, the reviewer trail |
| `tests/test_agent_process_appendix.py` | `product_surface_audit` | sufficiency gate, the reviewer trail |
| `raven/agent/flow/verify.py` | `dr_sufficiency_gate` | the reviewer trail |
| `tests/test_agent_flow_verify_ledger.py` | `dr_sufficiency_gate` | the reviewer trail |

So dropping a patch because upstream took its feature does not remove that
feature from these files. Check them by hand when a patch is retired.

Re-applying after a swap, and proving the result:

```bash
mkdir /tmp/up && git -C <upstream clone> archive <commit> | tar -x -C /tmp/up
rm -rf subagents/raven-research/Raven-X && mkdir subagents/raven-research/Raven-X
cp -a /tmp/up/. subagents/raven-research/Raven-X/     # replace, do not overlay:
                                                      # an overlay keeps files the
                                                      # swap deleted upstream
cd subagents/raven-research/Raven-X
git apply -p1 ../patches/acp_per_session_loops.patch
git apply -p1 ../patches/web_search_providers.patch
git apply -p1 ../patches/prompt_cache_provider_probe.patch
git apply -p1 ../patches/acp_session_modes.patch
git apply -p1 ../patches/dr_sufficiency_gate.patch
git apply -p1 ../patches/product_surface_audit.patch
git apply -p1 ../patches/ask_user_gate.patch
uv sync          # the replacement took .venv/ with it; seconds to rebuild
```

The order above is alphabetical-by-story, not a constraint: the six are disjoint
at the file level, and the 2026-09-02 verification applied them in two different
orders to prove it. One caution from this machine: run `git apply` on an
**out-of-repo copy** when verifying - from inside this repository it resolves
paths against the enclosing worktree, skips every vendored path with exit 0,
and a broken patch verifies green.

Then run the `diff -rq` above: it must name **nothing at all**. The patches carry
every file this fork changes, so a path it names is drift no patch records - the
state this folder was in when the patch set claimed three patches and the tree
carried four concerns, and again on 2026-09-02 when it claimed five and the tree
carried six (the per-session MCP surface had grown into `spine.py`, `methods.py`,
`server.py` and four siblings with no patch recording it; the re-partition from
the true base diff is what caught it).

Prove it both ways, because a forward-only check passes on a patch set that has
quietly grown a second copy of a hunk:

```bash
# forward: pristine + all seven == this checkout
# reverse: this checkout - all seven == pristine
git apply --reverse -p1 ../patches/ask_user_gate.patch
git apply --reverse -p1 ../patches/product_surface_audit.patch
git apply --reverse -p1 ../patches/dr_sufficiency_gate.patch
git apply --reverse -p1 ../patches/acp_session_modes.patch
git apply --reverse -p1 ../patches/prompt_cache_provider_probe.patch
git apply --reverse -p1 ../patches/web_search_providers.patch
git apply --reverse -p1 ../patches/acp_per_session_loops.patch
```

Now that the repo clones, the swap comes from its objects rather than an archive:

```bash
cd <repo root>
git fetch --no-tags https://github.com/ZuyiZhou/Raven-X.git \
    'refs/heads/*:refs/remotes/zuyizhou/*'
git log --oneline <base>..zuyizhou/main          # what you are taking

cd subagents/raven-research
rm -rf Raven-X && mkdir Raven-X
git -C <repo root> archive zuyizhou/main | tar -x -C Raven-X
git add -A .
```

Then confirm the staged tree differs from upstream in nothing but the
policy-excluded assets:

```bash
git diff --name-status "zuyizhou/main^{tree}" \
    $(git write-tree --prefix=subagents/raven-research/Raven-X)
```

Keep `.venv` across the swap. Clearing only the tracked files leaves it in place,
which is simpler than moving it out and back:

```bash
cd subagents/raven-research/Raven-X
git ls-files -z . | xargs -0 rm -f
find . -depth -type d -empty -not -name .venv -not -path './.venv/*' -delete
find . -type d -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
git -C <upstream clone> archive <commit> | tar -x -C .
```

Clear the stale `__pycache__` as that does: bytecode for a module the new tree no
longer has is still importable. The install is editable
(`_editable_impl_raven.pth` points at this checkout), so the new source is live
immediately; `uv sync` is only needed when `pyproject.toml` or `uv.lock` changed,
which they did not for `dr@2.6 -> dr@2.8 -> dr@2.9 -> dr@3.2`, did for
`dr@3.3 -> dr@3.7` (no package added, so optional in practice), and did not again
for `dr@3.7 -> dr@3.4` - the two files are byte-identical across that swap. `dr@2.9` also
brings a `bench/` tree - the evaluation engine - and exempts it from lint in both
`pyproject.toml` and `.pre-commit-config.yaml`; nothing here calls it.

**One step is not optional: bump `drFlow.version` in `config.json` in the same
change.** `drFlow.version` is the key of upstream's measurement ledger, so the
validator rejects every superseded label on a newer build - `dr@2.4` through
`dr@3.1` are all refused by this one. It matches the **base** label, so our
`-filetools` profile suffix survives and only the number moves
(`dr@2.9-filetools` -> `dr@3.2-filetools`). Skipping it does not degrade the run,
it kills it: the config fails to load and `raven acp` exits at launch, so the
host reports the agent unreachable with the validator's message in the stderr
tail - a stale label reads as a broken agent.

Earlier swaps kept the previous build beside the checkout as
`Raven-X-dr32-rollback.tar.gz` (tree only, no `.venv`) with a matching
`config.json.bak-dr32`, one pair per swap, because the source zip was archived
nowhere and there was no other way back. **That is no longer the reason it is
there.** The repo clones now, so the tree rolls back with
`git archive <old commit>`, and `config.json` is tracked in this repo, so its
previous value is in our own history. Both halves of the pair are gitignored and
neither was made for the `dr@3.7` swap; keep the old ones as long as the zips
they came from are the only copy of those builds.

### What the port to `main` changed for us

The 2026-09-02 swap (`0a09f07` -> `a903a424`) is unlike every earlier one: the
two commits sit on **diverged lines**. `feat/dr_sufficiency_gate` forked from
`main` at `4447f4d4` and upstream has not merged it (their PR #14 is open), while
`main` took ten commits of its own past the fork point. So this step was a real
three-way merge (merge base `4447f4d4`), not a fast-forward: the base recorded
above moved back onto `main`, and the gate branch's five commits now ride as
`patches/dr_sufficiency_gate.patch` until upstream merges them. Six files
conflicted, all of them the same story - upstream's product-surface audit
(`ba06912`) had taken our `product_surface_audit` fixes back in its own wording
while the gate branch refined the same functions - and the resolutions kept the
finer implementation wherever the two disagreed (the `_match_form` citation
matching over `main`'s bare set membership) and took `main`'s side wherever only
the prose differed.

| Change | Effect here |
|---|---|
| **`ba06912` took the audit fixes upstream** | `trust.py`'s scan-from-the-end, the base `cited_never_surfaced` split, `looks_failed` borrowing `WebSearchTool._failed`, and `fetch_result_ok`'s `raw_decode` are all upstream's own now. `patches/product_surface_audit.patch` shrank to the two refinements upstream does not have (see the patch table) plus the plan-doc deletion, and `web_search_providers.patch` dropped its `raw_decode` rider |
| **The no-key search message split in two** (`ba06912`) | The model-facing return is now a generic "web search is unavailable on this run - do not retry" with **no config path in it** (a tool return value gets narrated back to whoever is watching); the provider's name, config path and env var go to the operator on stderr. Kept, with the log line parameterised over our provider `spec` rather than hardcoding Serper, and `test_the_unconfigured_error_names_the_selected_provider` now asserts the split rather than the old single string |
| **`cdb9c1e` flipped eight class defaults on** | Six of the eight pins `config.json` adds land at values this profile already ran under; two do not. The old base predates `cdb9c1e`, so `search.includeSnippets` and `search.snippetDedupByDocid` were effectively **off** before this swap and are **on** after it - a model-visible change to every search result, and one of the two reasons the flow label moves (see the label row below; the first published revision of this row claimed zero effect, which an MR review corrected). What the pins themselves change is the meaning of *unpinning* one: that now keeps it on |
| **`profiles/` and the flow-parity gate arrived** (`14a02f2`, `2773e35`, `92971ac`) | The measured-arm configs now ship in the tree, and `tests/test_shipped_flow_parity.py` compares every shipped config with `drFlow.enabled` field-by-field against the arm that produced the published numbers. The gate branch's `examples/dr_shallow.json` had never met that test; it needed a `PER_CONFIG` allowlist entry whose reasons come from its own five-delta table in `examples/README.md` - recorded inside `dr_sufficiency_gate.patch` |
| **The appendix gained `span_seconds` and `cited_schemeless`** (`49411cb`, `eb7b8e1`) | Both merged in beside the gate branch's finer citation matching: the research wall-clock (first ledger row to last, deliberately *not* the turn's clock) and the disclosure list of scheme-less references `_URL_RE` structurally cannot see. The fetch ledger rows also carry `info_to_extract` now, write-only |
| **The untrusted fence tag is named** (`7e74677`) | So a model that mentions the fence stops being able to cite it as a source. Upstream's change, taken as-is |
| **No dependency delta** | `pyproject.toml` and `uv.lock` are byte-identical from `0a09f07` through the merge, so an existing venv carries across; this worktree's checkout had none and needed one `uv sync` |
| **The flow label moves by suffix, and the old one is refused** | `main`'s validator default is still `dr@3.5` and `_SUPERSEDED_VERSIONS` matches base labels only (adding `dr@3.5` would reject upstream's own measured profiles), so the bump lives in the suffix: `dr@3.5-filetools-askuser` is superseded by `dr@3.5-filetools-askuser-derive` (AGENTS.md 0.2 - the identity gained the derive and recommend reply rules, and search snippets turned on, so the old label names a different generated distribution). A README row is not the refusal 0.2 asks for, so `DRFlowConfig._SUPERSEDED_PROFILES` (`raven/config/raven.py`, in `acp_per_session_loops.patch`) matches the whole label and refuses the old one on an enabled flow, naming the successor; the trunk launcher's `FlowConfig` carries the same table. Retiring the next profile label is one entry in each |

The swap also caught **unrecorded drift**: the vendored tree carried a
per-session MCP surface (session-scoped `mcpServers`, the registry overlay,
`raven.toolName` on replay frames) across seven files that no patch recorded -
the state the reverse-apply check exists to catch, and it did. The whole local
delta was re-derived from the true base diff and re-partitioned into the six
patches above; forward and reverse application were verified file-identical
against a pristine `main` archive, in an out-of-repo copy (see the caution in
the re-apply section).

Verified on this build 2026-09-02: the vendored tree is byte-identical to
pristine `a903a424` plus the six patches (both directions), `config.json` loads
as `dr@3.5-filetools-askuser-derive` with `askUser.delivery: "tool"` and the
sufficiency gate on, `scripts/check_vendored_invariants.py` passes on the
swapped tree, and the checkout's own suite is **5902 passed, 84 skipped** in its
own venv under an isolated `HOME`.

### What the `ask_user` round trip and the deep report changed for us

The 2026-08-26 swap (`6b5ec31` -> `4447f4d4`) is 6 upstream commits over 31
files, with **no dependency delta** - `pyproject.toml` and `uv.lock` are
byte-identical across it, so the venv carried over untouched and `uv sync` was
not needed. The flow label does not move: `dr@3.5` is still the base this build
accepts, so `dr@3.5-filetools-askuser` stays as it is. Two new knobs are what
the swap exists for, and this folder turns both on.

| Change | Effect here |
|---|---|
| **`drFlow.askUser.delivery: "tool"`** | Asking now happens INSIDE the turn: the questions leave over the question broker, the answers come back as the tool's return value, and the same turn researches on them. The old `handoff` - questions as the turn's reply, answers as the next prompt - stays the fallback. The prompt clause, the tool description and the schema all switch with the knob, and the outline goes with them: the broker round trip carries questions and answers only, so `outline: true` is effective-off under this delivery |
| **Both sides must opt in, and our host does not yet** | Upstream arms the broker only when the ACP client declares `clientCapabilities._meta.raven.askUser` at `initialize`, and answers over the `_raven/clarify_respond` extension method. Raven's own ACP client (`raven/agent/acp/protocol.py`) declares `fs` and `elicitation.form` and nothing else, so today the tool reads `round_trip_ready` False and the handoff short circuit is still the transport that runs. That degradation is by design - a question frame must never go to a client with no UI to answer it, because the broker's 600s fail-safe would answer every mandated clarify with its default - and what it costs meanwhile is one paragraph of prompt: the model is told to ask by calling and never to repeat the questions into its reply, while the reply it gets handed back IS the rendered questions. `render_handoff` writes that text, not the model, so the caller sees exactly what it saw before |
| **`drFlow.finalShape.reportDepth: true`** | Selects the deep report clause in place of the `dr@3.4` template. Same three sections, `## Findings` upgraded from a findings list to an argued report: causal narrative, per-datum source and as-of date, disagreements adjudicated in the open, established facts separated from forward-looking judgments, tracking signals inside `## Limitations`. Upstream ships it off and unmeasured - a bundle of prompt commitments priced together - and says product profiles that want it should set it explicitly rather than inherit a default, which is what this file now does |
| **The rest of `finalShape` is pinned explicitly** | `reportStructure`, `reportFormatOverride`, `reportReminder` and `processAppendix` all matched their defaults and were being inherited silently; `reportBounce` is turned **on** against its default, in the high and max modes only - the extra generation it spends is exactly the kind of cost the medium mode exists to decline. It re-samples a terminal draft that is missing a template section, once, deterministically - upstream leaves it off until someone prices it, and `record: true` here is what makes that possible (`report_shape_gate.bounces` against `report_shape_gate.shipped_malformed`). Pinning the other four costs nothing and stops a future default flip from moving this profile without a commit saying so |
| **`identityOverride`'s reply rule went back to answer-first** | Upstream applies four of its five no-user rewrites under `delivery: "tool"` and deliberately skips the fifth, because under the round trip the reply shape never changes. Our override owns the identity, so nothing applies those rewrites for us - the reconciliation is by hand, and the sentence "If you are asking the user, the questions are the whole reply" now contradicts the clause it sits next to. Reverted to "One message, plain text. First line: the answer itself and nothing else." |

Verified on this build 2026-08-26: the vendored tree is byte-identical to
`4447f4d4`, `config.json` loads as `dr@3.5-filetools-askuser` with the eight
`finalShape` knobs and `delivery: "tool"` as written, the assembled contract
renders the tool-delivery clause and the deep report template with no
`identityOverride` warning, and the checkout's own suite is **5636 passed, 78
skipped** in 150s in its own venv.

### What `dr@3.5` and the ACP transport changed for us

The 2026-08-25 swap (`71abb5a6` -> `7b603aa`) brought two coupled things: the
flow label moved to `dr@3.5` (upstream's launch convention supersedes `dr@3.4`
the moment its batch finished, so the old label is now *rejected* on this
build), and the checkout grew `raven acp` - a serve entry that speaks the Agent
Client Protocol on stdio (`raven/acp/`, upstream's plan and pitfalls recorded at
the time in its `ACP_INTEGRATION_PLAN.md`, removed 2026-08-31 by
`patches/product_surface_audit.patch` - internal-only planning doc, not carried
here). This folder switched its registration to it in the
same change: `subagent.json` is now `kind: "acp"`, and `run.py` shrank from a
686-line per-turn wrapper to a launcher that renders the config and execs the
server.

| Change | Effect here |
|---|---|
| **`drFlow.version` -> `dr@3.5-filetools-askuser`** | The never-optional step, forwards again this time. Verified on this build: the new label loads, and `dr@3.4-filetools-askuser` is rejected with "predates this build's flow semantics". Under the acp transport a stale label no longer masquerades as a credential error: `raven acp` dies at launch and the host shows the validator's own message in the stderr tail |
| **One server per connection, not one process per turn** | The host raven keeps a pooled ACP connection; a task is a `session/prompt` on it. Statefulness is no longer declared through `resumeCommand` - the handshake reports `loadSession: true` and `sessionCapabilities.resume`, the host measures it (verified: `can_resume=True`, `can_load=True`, ~3s), and a follow-up on the same instance handle is a `session/load` + prompt on the same server |
| **The whole answer-extraction layer died with the CLI mode** | Everything `Calling it` used to document - session-log parsing, the wrap-up/clarify row classification, `--session` sanitising, per-conversation workspaces, `launcher.log` - is protocol business now. The reply is the turn's `agent_message_chunk` stream (non-streaming inside the loop, so it is the one committed reply, wrap-ups and clarify handoffs included); progress arrives as `session/update` tool-call frames the host journals |
| **The research trail arrives natively** | The appendix rides on the value `run_turn` returns, which is exactly what the acp spine emits as the closing text - so the trail reaches the host with no `observers["research_trail"]` re-derivation. What is lost is `compose_reply`'s budgeting: the host tail-truncates the reply at the entry's `maxOutputChars` (30000), so a report that saturates the cap loses its trail silently instead of shortening the answer to keep it |
| **`cwd` is pinned in the manifest** | New field, load-bearing: the host's connection pool keys the launch on `(command, cwd, env)`, and an acp entry with no `cwd` falls back to the calling task's workspace - every new workspace would relaunch the server and kill the sessions the old one was serving. `{SUBAGENT_DIR}` resolves at discovery/install time, same as the command |
| **The rendered config outlives the process that wrote it** | The exec hands `run.py`'s pid to the server, and the host tears servers down with `SIGKILL` to the process group, so the old delete-in-`finally` can never run. Each launch now sweeps rendered files whose pid is dead instead; the age-based day sweep went with it |
| **`--wait-skill-extract` / `--flush-skill-buffer` have no acp addressee** | Both were per-turn CLI flags. The server runs turns through the same loop (`drain_backend_stores` still blocks on the detached everos writes), but nothing flushes the skill buffer per prompt any more - the promote-on-last-turn problem the old launcher documented is moot in the form it had, and unsolved in a new one: a long-lived server never sees a "last turn" either |

Verified on this build 2026-08-25: the vendored tree is byte-identical to
`7b603aa`, the editable venv carried across with a plain `uv sync` (5 packages
moved), `config.json` loads as `dr@3.5-filetools-askuser`, the full chain
`run.py` -> `raven acp` answers `initialize` + `session/new` with
`agentInfo.name: raven-x-research`, and the checkout's own suite is **5563
passed, 84 skipped** in 136s under an isolated `HOME`.

### What the `dr@3.4` fold, `ask_user` and everos v2 changed for us

7 upstream commits, 55 files, +12495/-324. `pyproject.toml` and `uv.lock` are
**byte-identical** to `ce225550`, so the editable venv carried straight across
with no `uv sync` - only the tree was replaced. Two of the seven commits are our
own work coming home; the other five are the everos rewrite.

| Change | Effect here |
|---|---|
| **The build label moves *down*, `dr@3.7` -> `dr@3.4`** | The one step that is not optional, and this time it runs backwards. Upstream folded `dr@3.5`-`dr@3.7` into `dr@3.4` - same flow semantics, one name - and the loader now **rejects** all three folded labels rather than aliasing them, so the old `dr@3.7-filetools` would not load at all. Verified on this build: `drFlow.version` is `dr@3.4-filetools-askuser` and loads; setting `dr@3.7-filetools-askuser` fails validation with "names a label that was folded into 'dr@3.4' ... set drFlow.version to 'dr@3.4', keeping any profile suffix". Per the rule above that is `raven` exit 1, which `run.py` reports as a credential-or-config error - so a swap that forgets this row looks like a missing key |
| **The agent asks before it researches** (`drFlow.askUser`, on here) | The behavioural change a caller will notice first. See `Asking first` below for what it does to the reply shape and to `run.py`'s verdict |
| **Our six local patches came home** (`ed4a866`) | Upstream took five of the six, so the checkout is patch-free again - see `Updating to a newer upstream`. The two that carry behaviour were already live here: the turn answering a clarify is classified as research from the commit marker instead of the conversation gate, and `observers["research_trail"]` carries the rendered trail where this launcher can reach it. The sixth (a `test_provider_rates.py` tightening) was declined on the grounds we had already recorded against it, and is dropped rather than re-applied |
| **The everos write path is rebuilt** (`77d51b8`, `39d2908`) | Deferred capture, dedup, and the 422s fixed. The shape that matters here is the *cost*: a turn's write is detached after `_STORE_TURN_BUDGET_S` (5s) rather than paid for in full, at most 4 are in flight, and teardown drains them within 15s. So a slow everos costs the turn 5s on the WRITE path instead of its own timeout. Read that narrowly: recall is a different lane and is still fully synchronous on prompt assembly, on this folder's `timeout_s` of 360 - so a service that is reachable but stalled still stalls every turn for minutes, and the 5s bound does not help there |
| **`--flush-skill-buffer` is live again** | It was inert on the pruned build ("leave it alone"); it now promotes this session's captured turns and **blocks on one request** bounded by the adapter's `flush_timeout_s` (default 960s), inside `run.py`'s own `--timeout` (2400s). `run.py` still passes it on every turn, deliberately: a lone `-m` turn trips no boundary, so the alternative is not "promote later" but "never". The accepted cost is that turn two of a conversation promotes an unfinished trajectory, because the gateway spawns one process per turn and never says which is the last - upstream's advice to promote on the last `-m` only has no addressee here. `memory.flush_on_task_end` is the config-side equivalent and is off; the flag is the only trigger |
| **Plugin config slices are validated, and an identity mismatch is reported** (`2694acb`) | Both new checks pass clean on our slice, verified by running them: the five keys we set (`mode`, `base_url`, `user_id`, `agent_id`, `timeout_s`) are all declared now - two of them were undeclared and therefore silently inert before - and `memory.userId` / `memory.agentId` agree with the slice's `user_id` / `agent_id`, which is what the mismatch check exists for. A mismatch there is the silent kind: EverOS routes by sender, so writes land under one owner and reads look under another and recall comes back permanently empty with no error on either side. The check warns and never raises, so it cannot kill a run |
| **A remote everos, with the token out of the config file** (`bd8b4e4`) | **Not our path, and worth keeping that way.** `base_url` points at the host raven's own local everos (`localhost:18791`), so there is no token, no TLS decision and no gateway header to get wrong. If that ever changes, supply the bearer through `EVEROS_API_KEY` in the environment rather than `api_key` in `config.json` - this folder's `config.json` is published and holds no secrets, which is the whole reason `.env` exists |
| `require_service`, a new exit-1 site | Unset here, so unreachable: a missing everos still degrades to `everos not found` plus an empty recall, and the run answers normally. It widens what exit 1 can mean, which is why the note under `Calling it` no longer says "only one thing" |
| **The suite stops reading the developer's `~/.raven/config.json`** (`46cdb1a`) | Directly ours, and it fixes a real failure on this machine: `render._language_directive()` resolves lazily at assembly time from the host's config, which here sets `"language": "zh"`, and measured 101 characters prepended to the identity block. Eight prompt-byte tests asserted shas against that ambient value and failed. An autouse fixture in `tests/conftest.py` now pins it empty, which is the premise those tests already stated |
| `_timestamp_ms` is back (`backend.py:321`) | The coercion this folder lost in an earlier swap, rebuilt upstream and now stricter than what we had - it parses ISO-8601 as well as an integer, with a per-message fallback. Latent either way here (our store payloads carry no `timestamp` key), but the day one does, the whole write no longer dies on a `422 INVALID_INPUT` |

Verified on this build 2026-08-21: the vendored tree is byte-identical to
`71abb5a6`, `raven` 0.1.5 imports from it, `.venv/bin/raven` runs without a
`uv sync`, `config.json` loads as `dr@3.4-filetools-askuser`, and the checkout's
own suite is **5402 passed, 72 skipped** in 158s under the real `HOME`.

### What `dr@3.4` .. `dr@3.7` changed for us

19 upstream commits, 136 files, +18891/-1739. `pyproject.toml` and `uv.lock` did
move this time, but **no package is added**: `litellm` goes from a range to an
exact `==1.85.0` (the venv here already had 1.85.0, so nothing to install),
`oauth-cli-kit` is dropped as a dependency entirely, and `raven/providers/data/*.json`
joins the package data. Nothing here authenticates by OAuth, so losing that dep
costs us nothing.

| Change | Effect here |
|---|---|
| **The final answer gets a fixed report shape, and it is on by default** (`finalShape.reportStructure`, dr@3.5 through dr@3.7) | **The one change a reader will see in the output.** We do not set `reportStructure`, so we inherit the new default `true`: dr@3.5 turns the closing clause into a fixed three-section template, dr@3.6 lets that outrank a format the question itself asks for, and dr@3.7 adds a per-turn reminder on the user message (stripped before persist) plus `ReportShapeGate`, a bar that bounces a draft missing a section once. The gate is off until priced; the template and the reminder are not. Set `finalShape.reportStructure: false` to keep the pre-dr@3.5 clause - upstream's own bench profiles pin it off so their anchors cannot move |
| **Flow hooks are scoped to the turn** (dr@3.4) | Ours: `fetchFloor` and `verify` are both on here, and they now measure against this turn's base rather than the whole conversation. Multi-turn is on (`conversation.enabled`), which is exactly the shape this fixes |
| The closing-tag bar is waived for out-of-band reasoning | **Does not reach us**: `thinkClosingTagRequired` is already `false` here. Worth knowing why it landed, though - a channel-separated stack returns reasoning in `reasoning_content`, leaving `content` answer-only, and the bar then erased every complete answer, 4 of 4 live-web turns |
| `DRFlowConfig.context_window_tokens`, a DR-arm-only window override | New, unset here, and upstream verified it changes no behaviour while unset. It exists so a DR arm's window can be moved without dragging the flow-off anchor with it |
| The provider package is realigned to upstream `5edcda9` - wire ids, auth, endpoints, rates, prompt cache, truncation - and a models.dev snapshot (MIT) is bundled | **On our path**: the provider is declared `openrouter`, the section the endpoint actually is, so requests go out under the gateway wire id (`openrouter/openai/gpt-5.6-sol-pro`) and the bundled registry answers for them. Declaring it `custom` instead used to bypass all of that, and cost more than the wire id: `custom` reads as a self-hosted inference server, which switched on the orphan-`</think>` normalisation and let a report mentioning that tag lose everything before it. The loaded provider entry also gains `endpoints` and `endpointStrategy: sticky`, both defaulted |
| Native runtimes are stopped instead of segfaulting at exit | Removes a false signal rather than changing a contract. A live watchfiles runtime used to segfault interpreter finalization on its own - exit 139, 3 of 3 runs with `raven` never imported - masking the command's real exit code. `run.py` decides success by finding a persisted answer, not by exit code, so it was never fooled; a human reading the log was |

### What `dr@3.3` changed for us

`pyproject.toml` and `uv.lock` are byte-identical to `dr@3.2`, so the editable
venv carried straight across with no `uv sync`. Upstream added three modules
(`agent/flow/turn_task.py`, `agent/fetch_gate.py`, `agent/flow/fetch_gate.py`)
and no file exists only on our side.

| Change | Effect here |
|---|---|
| **The task a draft is judged against is now this turn's question**, not the conversation's first user message (`flow/turn_task.py`) | Directly ours, and the reason to take this build. The draft reviewer and the salvage both used to walk `ctx.messages` for the first `role == "user"` row, which in a conversation is turn one's question. Upstream measured it on 2026-08-17: turn two asked a follow-up about a different product, the model researched it correctly, and the reviewer rejected the draft with `0 unsupported claim(s)` because it did not answer the question it was shown - and the rewrite then answered turn one again. Multi-turn is on here, so this surface is ours; we were shielded only by `verify.strictRejectOnly`, which degrades a claimless reject to a pass |
| `fetchGate`, a search-without-fetch guard at the action-space layer | New, default off, **left off**. It withholds `web_search` from the iteration after `k` (15) searches with no fetch, where `fetchFloor` - which we do have on - only appends an advisory sentence. Upstream's own note is that the advisory measured real but an order of magnitude too small |
| The everos memory backend dropped its timestamp coercion | Upstream removed `_timestamp_ms`; the message entry is now `m.get("timestamp") or now_ms` again. Latent rather than live: the store payloads built here carry no `timestamp` key, so `or now_ms` always fires. It would matter the day a message arrives carrying Raven's own ISO-8601 string, which `MessageItemDTO.timestamp` (an `int`) rejects with `422 INVALID_INPUT` for the whole write |

Verified on this build 2026-08-18: `raven` 0.1.5 imports from the swapped tree,
the config loads with `drFlow.version = dr@3.3-filetools`, upstream's own
`test_agent_fetch_gate` / `test_agent_turn_task` / `test_provider_routing` /
`test_config_raven_loader` / `test_agent_flow_conversation` pass (87), and one
real two-part run (local file + web) returned `exit=0` in 98s with
`flow_version: dr@3.3-filetools/raven-0.1.5`, `read_file` ok 1, `web_search` ok 2,
`web_fetch` ok 3, `citation_grounding_rate: 1.0`, `cited_not_opened: 0`.

### What `dr@3.0` .. `dr@3.2` changed for us

| Change | Effect here |
|---|---|
| The context window is resolved **once**, into `AgentLoop.context_window_tokens` | The big one. Until `dr@3.1` that attribute held our configured `contextWindowTokens` (65536) and only a few call sites resolved the real window; now `HistoryTrimmer`, `MemoryConsolidator` and `_make_token_budget` all read the resolved value. `resolve_context_window("openai/gpt-5.6-sol-pro")` answers **1050000** through OpenRouter's live table, so our 65536 pin is now inert and the trim point moved by ~16x. Expect longer retained histories and a higher per-call input bill; there is no knob to force the old number back, because the resolved value wins unconditionally |
| The truncation / exhaustion wrap-up moved behind a gate, **default off** | `run.py`'s wrap-up branch is now dormant rather than wrong - see `Which row is the answer`. Upstream gated it because ungated it moved their anchor, and because it converts a detectable failure (empty answer) into an undetectable one (an answer that is present and wrong): measured 21 firings for zero correct answers on the treated arm |
| The process appendix's URL extractor stops at CJK punctuation | Directly ours: our output surface is Chinese, and an ASCII-only stop set let a cited URL swallow the rest of the clause, so a page the run really had opened was counted as a fabricated citation. `citation_grounding_rate` in `launcher.log` is trustworthy on Chinese answers for the first time |
| Multi-turn conversations | New, default off upstream, **on here** - this is what `Multi-turn` below turns on and the reason the entry is no longer stateless |
| The search-saturation ladder, the reactive completion clamp | New, default off, and left off: neither is reached by a run on this config |

`dr@2.8` turned two `finalShape` clauses on by default, and both reach us because
`config.json` does not pin them:

| Knob | Effect here |
|---|---|
| `reportStructure` | The reply is asked for as a research report - answer first, then the findings that decide it with their URLs, then what could not be established. It is a contract clause, so it lands in the committed answer and reaches the caller |
| `processAppendix` | Was inert at `dr@2.8` for want of a ledger; `dr@2.9` opens a per-turn one under the state root (see below), so it now renders. It still does **not** reach the subagent reply, and that is intended - see the note under `Calling it` |

Pin either to `false` in `config.json` to get the older reply shape back.

### The ledger directory

`dr@2.9` fixed the defect that made `processAppendix` unreachable off a bench:
the loop now opens a per-turn ledger file. `get_data_dir()` is the config file's
own parent, so it lands beside `cache/` and `cron/` under

```
~/.raven/workspace/subagent_sessions/raven-research/ledger/
```

which is where `run.py` renders the config, and deliberately **not** in this
folder - see `Where the secrets live`. It is still isolated from the host Raven's
own `~/.raven/` state, which was the point of the original note; only the address
changed. The file is deleted after the appendix reads it, so the directory is
empty at rest; a process killed mid-turn leaves a few kilobytes behind that
nothing reads.

## Multi-turn

`drFlow.conversation` is upstream's product surface for conversations, added in
`dr@3.0` and off by default there. It is on here.

Read its own module docstring (`raven/agent/flow/conversation.py`) before
changing any of this; the short version is that **none of it has ever run under
measurement**. Every DR reading in upstream's ladder is turn one of a fresh
session, because their harness sends one message per question, and every knob
below is read on turn two or later. So this is a product decision, not a measured
one, and turn one is byte-identical to what it was before.

| Knob | Here | Why |
|---|---|---|
| `enabled` | `true` | The feature itself |
| `gate` | `agentic` | From turn two on, a classifier decides per turn whether the research machine runs. `always` would put a follow-up like "expand the third point" through the verify gate, the fetch floor and a full search - minutes, for a reformat. Turn one is not covered by either setting: it follows `drFlow.enabled` |
| `gateModel` | unset (the loop's model) | A small fast model is what the knob is for, but it would be a second model in this config to keep working, and the gate sits in front of every turn's first token either way |
| `gateMaxTokens` | `4096` | **Not cosmetic.** The gate does not pass a reasoning effort, so it inherits `agents.defaults.reasoningEffort: high`, and upstream's default of 256 tokens is a budget a reasoning model spends before emitting a verdict. A generation that ends on `length` is read as no verdict and fails open to a research turn - the gate would cost a call per turn and decide nothing |
| `gateTimeoutSeconds` | `90` | Same reason as above, from the other side: upstream's 20s is sized for a small model, and a timeout is also a fail-open |
| `researchMemo` | `true` (upstream default) | A compact record of what was searched and opened, carried into later turns. The alternative is not "no memo": tool results are truncated at 16k when persisted and then dropped whole by the trimmer, so a later turn asking about a cited source would be answered from whatever happened to survive |
| `identityScope` | `turn` (upstream default) | `topic` would stop a follow-up re-searching and re-opening the previous turn's pages, but it carries the saturation rule's identity set across turns too, so a follow-up whose first searches legitimately re-find those pages accumulates a dry streak it did not earn and can close search early. Not worth it for the re-fetching it saves |

The fail-open direction is "research" everywhere: a classifier that errors, times
out or returns something unparseable yields a research turn. That asymmetry is
deliberate - a needless research turn costs latency and quota, a wrongly skipped
one answers a factual question from stale context in the same confident voice.

`identityOverride` in `config.json` had to change with it. It told the model it
answers "one hard question ... in a single turn" with "no stored memory", which
on turn two is a flat contradiction of its own context. It now states the
conversation, the memo, and that a source the memo names may be cited without
re-fetching. Note what the gate deliberately does **not** do: it never varies the
system prompt per turn, because that text is the head of the cached prefix and
re-billing the whole conversation at uncached rates costs more than the verify
call it would save. So the identity has to read correctly on a research turn and
on a chat turn alike.

### What a turn reports

`observers.conversation_gate` is the record of the decision, stamped on the
turn's persisted assistant message
(`$RESEARCH_STATE_ROOT/workspace/sessions/acp/<session>.jsonl` - the old
`launcher.log` went with the per-turn wrapper):

```
turn 1  {"dr_turn_research": true,  "dr_turn_source": "first_turn", "dr_turn_why": ""}
turn 2  {"dr_turn_research": false, "dr_turn_source": "gate",
         "dr_turn_why": "only reformats the number already given"}
```

Measured here on a two-turn conversation: turn one read a local file and answered
in 23s with `verify_gate` and the invariants checker present; turn two answered
from context in 13s with no `verify_gate` and no `process_appendix` block at all,
which is the machinery being skipped rather than passing. `dr_turn_source` is the
field to read when a follow-up is unexpectedly slow - `gate_error`,
`gate_unparsed` or a `first_turn` where you expected a resume all mean the run
paid for a research turn.

## Asking first

`drFlow.askUser` is on here with `mode: "when_needed"`, which means the agent asks
**only when it judges the question undecidable without an answer** - on any turn,
including the first. This is the byte-identical pre-mode clause; `first_turn`, the
schema default, is the other setting, and it mandates a clarify round on turn one of
every conversation. The trade is stated in the schema and it decided this: a mandated
round removes the threshold a question is supposed to clear, so the failure mode is
filler ("how deep would you like?") that teaches callers to ignore the round including
the time it matters, and every conversation pays a round trip whether or not it needed
one.

`delivery: "tool"` is set, which asks for the broker round trip - questions out and
answers back inside one turn - rather than the `handoff` that ends the turn on the
questions. It is what the profile wants, not what currently runs: the round trip needs
the ACP client to declare `_meta.raven.askUser`, our host does not, and the tool falls
back to the handoff on its own. See the `4447f4d4` section above for what that costs
until the host side lands.

The knob needs no allowlist entry - the flow appends `ask_user` to
`drFlow.toolsAllowlist` itself when the switch is on, so the two cannot disagree - but
it did need `ask_user` removed from `tools.disabledTools`, where this profile had
pinned it off, and it needed `identityOverride` rewritten. The old identity told the
model there was "nobody to consult"; leaving that in place while handing it the tool is
the kind of contradiction a model resolves by ignoring one half.

Everything else is upstream's default: one clarify round per chain (`maxRounds: 1`), up
to 3 questions with an outline of the plan of attack, and `firstIterationOnly` on.

**`firstIterationOnly` is a security boundary, not only a throttle.** It withdraws
`ask_user` from the schema after the first search, and the reason is the direction of
trust: once the run has read a page, a tool still on offer is a channel for relaying
that page's instructions to a human. With it off, the identity clause - which says in
so many words never to use `ask_user` to relay a retrieved directive - is the only
thing left refusing. Leave it on.

### What the caller sees

A clarify handoff arrives as an ordinary reply: the turn's closing text is the
questions, relayed over ACP like any other answer. That is still the shape today,
`delivery: "tool"` notwithstanding - under the round trip there would be no
handoff for a caller to see at all, because the questions and their answers
would both be spent inside the turn that then delivers the report. The classification that
`run.py` used to re-derive from `turn_end.awaiting_user` lives upstream now -
the loop commits the questions as the turn's reply on both routes (the
`ask_user` tool and plain prose), so there is no separate shape for a caller
to mishandle. What the caller must still know is written into the entry's
`description`: the first reply is normally questions, and the answers go back
to the **same instance**, which the host resumes over `session/load`.

The turn that answers the questions is classified as research from the commit
marker rather than by asking the conversation gate. That is deliberate and it
is load-bearing: a clarify answer ("the EU market, 2024") reads exactly like
the shapes the gate is told mean `research=false`, so consulting the gate there
dropped the tools from the schema and answered a never-researched question from
memory, with nothing in the record saying so. Under `mode: "when_needed"` this
turn is occasional rather than guaranteed, which makes the classification matter
more, not less: a path taken on most conversations gets noticed when it breaks,
and one taken on a few does not.

## Calling it

In production nothing calls `run.py` per question any more. The host raven
discovers the `kind: "acp"` entry, spawns `run.py` **once per connection**, and
speaks the Agent Client Protocol on its stdin/stdout: `initialize`,
`session/new` (or `session/load` for a follow-up on a known instance), then one
`session/prompt` per turn. The reply is the turn's `agent_message_chunk`
stream; tool activity arrives as `session/update` frames the host journals
beside the run.

By hand, the same thing in two lines:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/tmp","mcpServers":[]}}' \
  | python3 run.py
```

For an interactive question-and-answer session, skip the protocol and use the
checkout's own CLI against a rendered config - but remember that a config you
render by hand holds the key and is yours to delete.

Two contract properties the old per-turn wrapper existed to absorb are solved
by the protocol itself, which is why the wrapper could die:

- **stdout no longer needs parsing.** The CLI's stdout was rendered for a human
  and the answer had to be dug out of the session JSONL, wrap-up flags,
  clarify markers and all. Over ACP the loop runs non-streaming and emits one
  closing text per turn - the committed reply, whatever produced it (a normal
  stop, a budget wrap-up, a clarify handoff) - and the server relays exactly
  that. The classification lives upstream in `run_turn`, where it belongs.
- **success no longer hides in an exit code.** One process is one connection,
  not one turn, so there is no exit status per question to misread. A turn that
  commits nothing is a protocol-visible empty turn (`stopReason: "end_turn"`
  with no message chunk), which the host raises as an error naming the stderr
  tail, rather than an exit-0 silence a wrapper has to disbelieve.

Sessions are minted and named by the server (`acp:<timestamp>_<nonce>`), filed
under one workspace pinned beneath `RESEARCH_STATE_ROOT` - the per-conversation
workspace machinery went with the wrapper, and with it `RESEARCH_RUN_ROOT` and
the `--session` path-escape sanitising: no caller-supplied string names a
directory any more.

### The research trail is still appended to the reply

The trail rides on the value `run_turn` returns - upstream appends the appendix
to the returned string, never to the persisted message - and the returned
string is exactly what the acp spine emits as the turn's closing text. So the
record of what was searched and opened reaches the host natively now, with no
`observers["research_trail"]` re-derivation; the report template's promise
(no source list in the prose, the full record follows) is kept by the
transport itself.

One budgeting nicety is gone: the old launcher shortened the *answer* to keep
the trail under the host's 30000-char reply cap, with a notice saying so. The
acp backend tail-truncates instead, so a report that saturates
`maxOutputChars` loses its trail silently. Accepted: the cap is sized so that
a saturating report is already an anomaly worth reading in the journal.

`process_appendix` and its counters (`citation_grounding_rate` above all) are
unchanged flow business; they ride `observers` on the persisted assistant
message, now under
`$RESEARCH_STATE_ROOT/workspace/sessions/acp/<session>.jsonl`. Read
`citation_grounding_rate` only with `urls_cited` and `cites_nothing` beside
it, only as an intra-run integrity guard, never as a quality score.

## stdout discipline

Over ACP the discipline is stricter and enforced upstream rather than here:
**every byte on stdout must be a frame.** The serve entry claims fd 1 before
anything else loads (`claim_stdout`), so a stray `print` anywhere in the
import graph lands on stderr as log noise instead of breaking the client's
decoder. `run.py` keeps the same rule for the window it owns - everything it
says goes to stderr, which the host journals beside the run and shows in the
tail of an empty-turn error. Nothing folds stderr into the reply any more; the
old CLI backend's `combined = stdout + stderr` trap died with the transport.

## Credentials

| Need | Status here |
|---|---|
| LLM provider key | `RESEARCH_API_KEY` in `.env`. Required - absent, the run exits 1 |
| Model | `openai/gpt-5.6-sol-pro` (1.05M context, accepts image input, $5/$30 per Mtok - five times terra's $1/$6). `temperature` was removed from the config: sol-pro does not list it among its supported parameters, and OpenRouter accepts the field then ignores it, so keeping it would assert a knob that does nothing. `reasoningEffort: high` is supported and stays |
| `RESEARCH_SERPER_API_KEY` | The search key, verified live against `google.serper.dev` |
| `RESEARCH_TAVILY_API_KEY`, `RESEARCH_EXA_API_KEY`, `RESEARCH_BRAVE_API_KEY`, `RESEARCH_FIRECRAWL_API_KEY` | Unset. Slots for the alternative search backends the checkout offers (Brave is search-only; the other three also serve `web_fetch` off the same key). Only needed when `config.json` selects one of them under `tools.web.search.provider` / `tools.web.fetch.provider` |
| `RESEARCH_JINA_API_KEY` | **Deliberately empty** since 2026-08-16. It is optional rather than required: `web.py` adds the auth header only when a key exists, otherwise `r.jina.ai` is called unauthenticated at a lower rate limit. The key that used to sit here ran out of credit, and an exhausted key is worse than none, because the two paths do not fail alike - the same URL answers `402 Payment Required` with it and `200` without. The symptom was total rather than partial: `pages_ok: 0` against `pages_opened: 33` on the first `dr@3.2` verification run, every citation ungrounded, and an answer assembled entirely from search snippets. Deleting the field alone fixed it, same build and same launcher: `pages_ok: 6` of 9, `citation_grounding_rate: 1.0`, `cited_not_opened: 0`. Restoring a key means topping the account up first; adding a dead one silently disables page reading |

Search and fetch are separate vendors with separate quotas: a working search
says nothing about whether pages can be read. Upstream reports a batch of 604
runs completing with every single fetch failing on a payment error.

### Where the secrets live, and where the transcripts do not

Every secret is in `.env` (mode 600, never published); `config.json` holds none
and ships as-is. `.env.example` is the template - copy it, fill it, `chmod 600`.

The indirection is not decoration. Raven-X's config loader does **no**
environment-variable substitution and reads no key from the environment, so the
key has to be *in the config file* by the time the server loads it. `run.py`
therefore merges `.env` into a rendered copy at launch and execs `raven acp`
on it. It is created with mode 600 via `os.open` **before** a secret byte is
written, rather than written and then `chmod`-ed, which would leave a window.
The file lives as long as the server does - the host tears servers down with
`SIGKILL` to the process group, so nothing inside can delete it on the way out;
each launch instead sweeps rendered files whose pid (baked into the filename,
and preserved by the exec) is no longer alive.

**Where that rendered file goes is what decides where everything else goes.**
`get_data_dir()` is the config file's own parent, and there is no separate knob
for the session directory - so `sessions/`, `cache/`, `cron/` and `ledger/` all
follow the config wherever it is written. Rendering it under

```
~/.raven/workspace/subagent_sessions/raven-research/
```

is therefore what keeps conversation transcripts out of this folder, and the
only way to do it without patching the checkout - which would not survive, since
the checkout is replaced wholesale on every upstream zip. `RESEARCH_STATE_ROOT`
moves that root; the workspace is pinned at `<state root>/workspace` unless
`config.json` names one (`RESEARCH_RUN_ROOT` and the per-conversation
workspaces went with the per-turn wrapper).

The consequence worth stating: the sibling runtime directories move too. They
cannot be split from the transcripts, because all four hang off that one parent.
The project folder is left holding only the files that ship.

Every teardown is a `kill -9` now, so a stranded rendered file is the normal
case, not the exception; the next launch's pid-liveness sweep is what collects
them.

The proxy matters for the LLM arm: OpenAI-backed models on OpenRouter refuse
requests from a China IP with `This model is not available in your region.`
Raven-X inherits `http_proxy` / `https_proxy` (its fetch client keeps
`trust_env` on), and the login shell here exports them, which is the env
`cli_agent` hands a spawned subagent. Nothing extra to configure.

## Installed as

One third-party subagent over this folder, `kind: "acp"`: `Raven-Research`
(`subagent.json`). The effort level is not a second entry - it is the `mode`
the caller names on the call, see `Research modes` above.

On a host that carries the `subagents/` tree nothing needs installing: the
host raven discovers every folder's `subagent.json` at startup, resolves
`{SUBAGENT_DIR}` / `{PYTHON}` (and, new with the acp entry, the same
placeholder in `cwd`), and puts the row on the table - enabled only when the
venv is built and a credential is reachable. A discovered row bakes in no path
and follows the folder, so a move or upgrade heals it.

`install.py` also covers one migration this transport switch created: a
stored row from an earlier install that still says `kind: "cli"` overrides
the discovered acp row, because config beats discovery by name. Re-run it (or
delete the stored row) on any host that ever registered this agent by hand.

```bash
python3 install.py --dry-run   # print the resolved entry, change nothing
python3 install.py             # register it
```

Prefer discovery: a stored row bakes in this folder's absolute path and
outranks the discovered row, so `install.py` is for a host that cannot see
this tree, not a step of normal setup.

The cli entry's interlocking declaration set (`command` + `resumeCommand` +
`idSource`, all agreeing about `{agent_id}`) is gone, and nothing replaced it:
an acp entry *declares* no behaviour. Statefulness is measured, not written -
the handshake reports `loadSession: true` and `sessionCapabilities.resume`,
the host verifies it on startup (or on the operations page's Test) and
snapshots the result, and the roster's `stateful` tag comes from that
snapshot. A schema field that disagreed with the handshake was the failure
mode the design removes.

What the entry does carry:

| Field | Value | Why |
|---|---|---|
| `command` | `{PYTHON} {SUBAGENT_DIR}/run.py` | Starts a *server*, once per connection - no `{prompt_file}`, no `{agent_id}`; a task placeholder here would be passed through literally and fail the handshake |
| `cwd` | `{SUBAGENT_DIR}` | Pinned because the host's connection pool keys the launch on `(command, cwd, env)`; unset, it falls back to the calling task's workspace and every new workspace relaunches the server, killing the sessions the old one was serving |
| `readyTimeoutMs` | `60000` | The `initialize` budget. The real handshake measures ~3s (a full engine import plus config render); 60s is headroom, not hope |
| `timeout` | `null` | No ceiling on a `session/prompt`: a long run is ended by a manual stop, not a timer. `run.py` defines no watchdog of its own, so this field was the only clock on the path |
| `maxOutputChars` | `30000` | The host tail-truncates the reply here - see the research-trail note above for what that costs |
| `owns` | routes the agent, not the mode | The identity segment renders every owning agent, and one row now owns research - see `Research modes`. Mode guidance deliberately lives on the modes themselves, because this line is read from the moment the folder is installed while the `mode` argument appears only once the probe has measured them |

`run.py` still mints nothing and names nothing: sessions are the server's own
(`acp:<timestamp>_<nonce>`), and one instance handle maps to one of them in the
host's registry.

The `description` is the field that matters most, because it is what the
dispatching model reads when deciding whether to hand work to this agent. The
capability boundary belongs in it - the two web tools plus the file tools, no
shell, stateful with a self-contained *first* question, several minutes for a
question that needs new evidence - since those are exactly the assumptions a
caller gets wrong. Statefulness is the one that has to be stated outright: a
caller who believes the agent is stateless restates the whole question every
turn, which is a research turn every turn and throws away the feature.

Since `askUser` went on, the description also has to say that **a reply can be
questions rather than the report.** Under `mode: "when_needed"` that is an
occasional shape rather than every first call, which makes it easier to miss and
no less costly: the two ways a caller gets it wrong are both silent - relay the
questions as the deliverable, or answer them by restating the whole question,
which starts a fresh round instead of the research. Neither reads as an error
anywhere, and a caller that never met the shape in testing is the one who meets
it in production.

There is a name clash worth knowing: our config already has a `Researcher`
(kind `openai`, MiroThinker deep-research over an HTTP endpoint). This entry is a
different agent with a different mechanism, hence a distinct name.

## Files here

| File | | Published |
|---|---|---|
| `run.py` | Host-side launcher: renders the config (mode catalogue, then `.env` secrets), then execs `raven acp` | yes |
| `install.py` | Resolves the manifest placeholders and registers the entry over the RPC | yes |
| `config.json` | Raven-X run config, complete baseline = the medium profile. Holds **no** secrets | yes |
| `modes/high.json` `modes/max.json` | Budget-knob diffs, declared to the agent as `acp.modes` and composed per session | yes |
| `subagent.json` | The subagent entry, with install-time placeholders | yes |
| `.env.example` | Template for the secrets and the state-root knob | yes |
| `README.md` `.gitignore` | This file, and the exclusion list below | yes |
| `.env` | The real secrets, mode 600 | **no** |
| `.config.rendered.*.json` | Merge of the two, mode 600, lives as long as its server; swept by pid-liveness at the next launch | **no** |
| `Raven-X/` | The agent itself. Ships as source; its `.venv` does not | yes |
| `Raven-X-dr*-rollback.tar.gz` `config.json.bak-*` | The builds this one replaced, and the configs that preceded it | **no** |
| `subagents-backup-*.json` | What `install.py` saved before a PUT - carries every other entry on this host | **no** |
| `runs/` `cache/` `cron/` `ledger/` | Runtime dirs the build derives from the config file's parent - all under `RESEARCH_STATE_ROOT` now, listed here because the `.gitignore` still guards this folder against a misconfigured root. `ledger/` is empty at rest (see above) | **no** |

## Publishing

`.gitignore` is the enforceable form of the right-hand column; read it as the
manifest. Three things in it are worth stating outright rather than leaving to a
pattern match:

**The checkout ships, but not its `.venv`.** `Raven-X/` is published as source,
so a recipient gets a launcher and an agent together. `.venv/` is excluded: it is
~700 MB of third-party wheels whose console scripts carry this machine's absolute
paths baked into their shebangs, so a copied one fails outright where `uv sync`
in the checkout rebuilds it correctly. The Install section above is the whole
setup story for a recipient.

Note for whoever signs off on the release: `Raven-X` is a separate upstream
project with its own owner (ZuyiZhou) and licence, obtained here as a zip from a
**private** repository, and its `LICENSE` and `NOTICES.md` travel with it.
Publishing it is the owner's call to have made, not this folder's.

**The runtime directories are work data, not samples.** `runs/`, `cache/` and
`ledger/` record real questions asked, pages fetched and answers committed.
Nothing in them is safe to ship on the grounds of being small.

**`.env` and the rendered config are the only files that ever hold a key.** If
either reaches a published tree, treat the keys as leaked: rotate first, then
work out how it got there. Deleting the commit does not undo it.
