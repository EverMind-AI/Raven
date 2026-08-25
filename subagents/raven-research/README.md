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
| Source | https://github.com/ZuyiZhou/Raven-X, branch `main`, commit `71abb5a6` |
| Local checkout | `./Raven-X` - the agent itself lives inside this folder |
| Local patches | **none** - the tree is that commit byte for byte, and the command that proves it is below |
| Package / version | `raven` 0.1.5, flow `dr@3.4` (updated 2026-08-21 from upstream `71abb5a6`; the label moves *down* because upstream folded `dr@3.5`-`dr@3.7` back into `dr@3.4` - see below. `dr@3.7` came from `ce225550` on 2026-08-20, `dr@3.3` from `e3edf28` on 2026-08-18, `dr@3.2` from `Raven-X-main.zip` on 2026-08-16, and the `dr@2.8` / `dr@2.9` steps from `b68085d` and `b1c12e4` on 2026-08-11) |
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
provider called `custom` here and one called `custom` there can be two different
endpoints, so picking by name would point this agent at a gateway its model is
not served on - which reads as a bad answer, not as an error.

Inheriting is a fallback, not an equivalence: a measurement taken on the pinned
model does not carry over to whatever the host happens to run. `launcher.log`
records which one was used on every turn, in the form
`llm: inherited from <path> (provider=... model=...); tuned for <recommended>`.

With no key here and no provider key in the host config either, the launcher
refuses rather than starting something that cannot answer.

## Updating to a newer upstream

The checkout carries **no local patches** - every adaptation lives beside it
(`run.py`, `config.json`, `subagent.json`, this file), so an update is a straight
replacement of `Raven-X/`.

That is true again rather than true by default. For one day it was not: the tree
was `a958b837` plus six locally patched files, carried because the `ask_user`
fixes had to run here before upstream had them. It is patch-free again because
upstream took five of the six back in `ed4a866` and declined the sixth for the
reason we had already written down against it - a test tightening, not a fix. So
the swap that brought the everos work also retired the patch set, and the check
below settles the whole question in one command again.

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
it kills it: the config fails to load, `raven` exits 1, and `run.py` reports a
credential-or-config error for what is really a stale label.

Earlier swaps kept the previous build beside the checkout as
`Raven-X-dr32-rollback.tar.gz` (tree only, no `.venv`) with a matching
`config.json.bak-dr32`, one pair per swap, because the source zip was archived
nowhere and there was no other way back. **That is no longer the reason it is
there.** The repo clones now, so the tree rolls back with
`git archive <old commit>`, and `config.json` is tracked in this repo, so its
previous value is in our own history. Both halves of the pair are gitignored and
neither was made for the `dr@3.7` swap; keep the old ones as long as the zips
they came from are the only copy of those builds.

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
| The provider package is realigned to upstream `5edcda9` - wire ids, auth, endpoints, rates, prompt cache, truncation - and a models.dev snapshot (MIT) is bundled | **Bypassed here**: our `custom` provider names an explicit `apiBase`, so the rewritten wire ids and the bundled registry are not on our path. The loaded provider entry does gain `endpoints` and `endpointStrategy: sticky`, both defaulted |
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

`observers.conversation_gate` in `launcher.log` is the record of the decision:

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

`drFlow.askUser` is on here with `mode: "first_turn"`, which means **turn one always
asks**: the agent answers with clarifying questions rather than starting research, and
the research it then does is aimed at the question it was told rather than the one it
guessed. `when_needed` reverts to asking only when the model judges it necessary, and
is byte-identical to the pre-mode clause.

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

A clarify handoff is a **third shape of committed reply**, and `run.py` had to learn it
because the two tests it already had both discard it. The gate short-circuits
`before_execute_tools` and the loop commits the questions with no `finish_reason`, so
the wrap-up rule drops them; and when the model writes its questions as ordinary prose
instead of calling the tool, the reply carries a perfectly normal `finish_reason ==
"stop"`, so the completed-answer rule would file a page of questions as a finished
report.

Both routes are recognised from `turn_end.awaiting_user`, which the flow stamps for
exactly this reader, with `turn_end.awaiting_user_source` saying which route it was.
`run.py` tests it **first**, before `finish_reason`, and logs the handoff louder than a
wrap-up, because this reply is not a thin answer - it is not an answer at all:

```
[run] reply is a clarify handoff, NOT a research answer - resume this session
      with the user's answers to continue
```

Resuming the same `--session` with the answers is what turns it into research. That
only works because the entry is registered **stateful** (see `Installed as`): the
gateway replays one `{agent_id}` across turns, so the clarify round and its answer are
one conversation. A caller that restates the whole question instead gets a fresh turn
one, which asks again.

The turn that answers the questions is classified as research from the commit marker
rather than by asking the conversation gate. That is deliberate and it is load-bearing:
a clarify answer ("the EU market, 2024") reads exactly like the shapes the gate is told
mean `research=false`, so consulting the gate there dropped the tools from the schema
and answered a never-researched question from memory, with nothing in the record saying
so. Under `mode: "first_turn"` that is not an edge case - turn one always asks, so turn
two is always this turn.

## Calling it

```bash
python3 run.py --session myquestion --question "..."
python3 run.py --session myquestion --question "and the second point?"   # same conversation
```

`run.py` wraps the CLI because two properties of the contract make a bare
registration useless:

- **stdout must not be parsed.** It is rendered for a human - spinners, progress
  lines, tool hints - and `--no-markdown` only turns off the renderer, not the
  prose. Their own evaluation harness never reads it. The answer comes from the
  turn's session log instead: `<workspace>/sessions/cli/<conversation>.jsonl`,
  one JSON object per line, first line `_type: "metadata"`. The answer is the
  last assistant row **past the lines the file already had** that either stopped
  normally or carries a wrap-up flag - see below. The line count is taken before
  the child starts, because a later turn appends to the file it continues and
  without the skip an answerless turn would report the previous turn's answer as
  its own.
- **exit 0 does not mean an answer was produced.** By design, nothing maps an
  answerless run to a non-zero code - whether the agent committed an answer is a
  measurement outcome, not a process failure. Exit 1 is the config-or-credential
  bucket - a missing key, an unreadable config, an invalid provider, and since
  the everos rewrite a memory backend with `require_service=true` that finds no
  usable service. That last one is unreachable on this config, where the knob is
  unset. So `run.py` decides success by finding a committed answer, and reports
  the exit code separately.

It also gives every *conversation* its own workspace under
`./runs/<conversation>/`. Not a decoration: this build files a session under the
workspace it ran in, so the workspace has to be a function of the conversation id
and nothing else - a per-process workspace would file every turn under a fresh
session and there would be no history for the next turn to read. Distinct ids
still get distinct workspaces, which is what keeps concurrent conversations from
interleaving. `RESEARCH_RUN_ROOT` moves that root; this machine points it at the
run tree that predates the change, which is also what keeps the folder free of
run artifacts.

The id is sanitised to one path segment before it is used, because it names a
directory and arrives from a CLI flag: `--session ../../elsewhere` must not
escape the run root. The surviving character set is a subset of what the build's
own `safe_filename` leaves alone, which is what lets `run.py` *name* the
transcript rather than pick the newest file in the directory - a guess that would
hand back the wrong conversation's answer the moment a workspace held two.

### Which row is the answer

`finish_reason == "stop"` alone is not enough, and `dr@2.9` is what made that
matter. Since `dr@3.0` the wrap-up is gated and **off by default**, so on this
config the rule below currently only ever fires its first row; the rest is kept
because the gate is one config line away and the cost of getting this wrong is a
real answer discarded. The loop writes a **wrap-up** when a budget cut the answer off -
the tool-iteration budget, or the completion budget, which strands a turn
mid-sentence on turn one having called no tool at all (measured upstream at 22 of
60 HLE anchor items). Both wrap-ups are appended through
`add_assistant_message` with no `finish_reason` at all, so the old rule discarded
a real answer and reported the run as answerless.

They are picked up by the flag the loop stamps on the message -
`synthesized_on_truncation` / `synthesized_on_exhaustion` - which it sets only
when the wrap-up produced real text rather than the static apology. The flag is
what keeps the rule from over-accepting, and it has to: two other kinds of
assistant row also carry no `finish_reason`, and both were seen here while
testing this update.

| Row | `finish_reason` | Verdict |
|---|---|---|
| Normal final message | `"stop"` | the answer |
| Truncation / exhaustion wrap-up | absent, flag set | the answer, and `launcher.log` says which wrap-up produced it |
| A draft the verify reviewer rejected, before the revision replaced it | absent | not an answer |
| A turn whose whole 27k-character reply sat inside an unclosed think block | absent | not an answer - `turn_end.answerless` agrees |

`observers` is collected independently of the answer now, because an answerless
run is exactly when its counters are worth reading.

Invoke the console script, not `python -m raven.cli`: `raven.cli` is a package
with no `__main__` and cannot be executed as a module.

`run.py` records the flow's own `observers` block in `launcher.log`. Two traps
there, both documented upstream and both deliberately not treated as failures:
`invariants.ok == false` is a counter and gates nothing (a run can commit a
1,300-character answer while reporting it), and `answer_chars: 0` alongside a real
answer is the same accounting seen from the other side: the count is the reply's
length *after reasoning is folded away*, so it reads 0 when the whole reply parsed
as reasoning - a think block the generation never closed. It is not a count of
marker text; the `<answer></answer>` span has its own counter,
`final_shape.span_chars`.

On this profile `answer_chars: 0` is not even occasional. `finalShape.requireMarker`
is **off** here, so the model is never asked for the marker at all and
`final_shape` reads `form: "unmarked"` / `reason: "no_marker"` on every turn - a
permanently degenerate `record` payload, and a config choice rather than model
non-compliance. The three-section report template already puts the answer under a
mandated `## Answer` heading, which is what makes the marker redundant; note that
the profile suffix does not distinguish marker-on from marker-off, so two configs
can carry this label and read different prompts.

### The research trail is appended to the reply

**This reversed.** It used to be a decision that the trail stayed out: the appendix
rides on the value the CLI *returns*, never on the persisted message, and `run.py`
reads the persisted message, so the trail reached the CLI's stdout and stopped
there.

What made that untenable is the report template. It tells the model **not** to
close with a list of sources, on the promise that the reply is followed by the full
record of what was searched and opened - and on this path that record never arrived.
So the model was made to omit its sources in exchange for a substitute the caller
never got: measured at 17 pages read and 8 cited on one live turn, with the other 9
recorded nowhere.

The trail therefore reaches this launcher through `observers["research_trail"]`,
which is upstream's own key for exactly this reader, and `run.py` appends it to the
one string the host uses for **both** the recorded `out.md` and the reply it shows,
so the record and the reader see the same sources. It is derived, not generated: no
tokens are spent on it and there is nothing in it to invent. It is kept out of
`process_appendix`, which batch tooling reads as a numeric measurement payload, and
`run.py` pops it before logging the observers line - it is prose measured in
kilobytes and the rest of that payload is counters.

Leaving the knob on is worth it anyway, because its counters land in
`launcher.log`, and one of them is a real integrity check:

```
"process_appendix": {"emitted": true, "searches": 5, "distinct_queries": 5,
  "pages_opened": 10, "pages_ok": 3, "urls_cited": 2, "cited_not_opened": 0,
  "citation_grounding_rate": 1.0, "cites_nothing": false, ...}
```

`citation_grounding_rate` is the share of URLs cited in the answer that appear in
a fetch record - a link the run never opened is a fabricated citation. Upstream
attaches three conditions to reading it and they are not optional: never without
`urls_cited` and `cites_nothing` beside it (citing less raises the rate, so the
denominator is chosen by the thing being measured), only as an intra-run integrity
guard and never as a quality score, and its denominator is cited URLs rather than
claims - an answer can be perfectly grounded and entirely wrong.

## stdout discipline

Raven's CLI backend uses the child's **entire** output as the subagent's reply -
`combined = stdout + "\n" + stderr` when stderr is non-empty
(`cli_agent.py:288`). So anything a launcher prints becomes conversation text
attributed to the agent. Both launchers here therefore:

- print **only the result** on stdout (the answer / the deck paths), plus a
  one-line reason on failure so the caller is never left guessing;
- send progress, container narration and the flow's `observers` block to
  `launcher.log` in the run directory;
- keep stderr empty unless `--verbose` is passed, which is for running by hand
  only - stderr is folded into the reply too, so a spawned run must never use it.

This was a real defect: the first version streamed every container log line and
every `[run] ...` diagnostic to stdout, and those lines showed up verbatim at the
top of the agent's answer in the chat UI. Moving them to stderr would not have
helped, and `outputPattern` cannot rescue it either - the backend takes
`m.group(1)` from a non-DOTALL regex, so a capture group cannot span a multi-line
answer.

## Credentials

| Need | Status here |
|---|---|
| LLM provider key | `RESEARCH_API_KEY` in `.env`. Required - absent, the run exits 1 |
| Model | `openai/gpt-5.6-sol-pro` (1.05M context, accepts image input, $5/$30 per Mtok - five times terra's $1/$6). `temperature` was removed from the config: sol-pro does not list it among its supported parameters, and OpenRouter accepts the field then ignores it, so keeping it would assert a knob that does nothing. `reasoningEffort: high` is supported and stays |
| `RESEARCH_SERPER_API_KEY` | The search key, verified live against `google.serper.dev` |
| `RESEARCH_JINA_API_KEY` | **Deliberately empty** since 2026-08-16. It is optional rather than required: `web.py` adds the auth header only when a key exists, otherwise `r.jina.ai` is called unauthenticated at a lower rate limit. The key that used to sit here ran out of credit, and an exhausted key is worse than none, because the two paths do not fail alike - the same URL answers `402 Payment Required` with it and `200` without. The symptom was total rather than partial: `pages_ok: 0` against `pages_opened: 33` on the first `dr@3.2` verification run, every citation ungrounded, and an answer assembled entirely from search snippets. Deleting the field alone fixed it, same build and same launcher: `pages_ok: 6` of 9, `citation_grounding_rate: 1.0`, `cited_not_opened: 0`. Restoring a key means topping the account up first; adding a dead one silently disables page reading |

Search and fetch are separate vendors with separate quotas: a working search
says nothing about whether pages can be read. Upstream reports a batch of 604
runs completing with every single fetch failing on a payment error.

### Where the secrets live, and where the transcripts do not

Every secret is in `.env` (mode 600, never published); `config.json` holds none
and ships as-is. `.env.example` is the template - copy it, fill it, `chmod 600`.

The indirection is not decoration. Raven-X's config loader does **no**
environment-variable substitution and reads no key from the environment, so the
key has to be *in the config file* by the time the CLI loads it. `run.py`
therefore merges `.env` into a rendered copy at launch, hands the CLI that, and
deletes it in a `finally`. It is created with mode 600 via `os.open` **before** a
secret byte is written, rather than written and then `chmod`-ed, which would
leave a window.

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
moves that root; `RESEARCH_RUN_ROOT` moves the per-run workspaces under it.

The consequence worth stating: the sibling runtime directories move too. They
cannot be split from the transcripts, because all four hang off that one parent.
The project folder is left holding only the files that ship.

A `kill -9` is the one path that can strand a rendered file; the next run sweeps
anything older than a day.

The proxy matters for the LLM arm: OpenAI-backed models on OpenRouter refuse
requests from a China IP with `This model is not available in your region.`
Raven-X inherits `http_proxy` / `https_proxy` (its fetch client keeps
`trust_env` on), and the login shell here exports them, which is the env
`cli_agent` hands a spawned subagent. Nothing extra to configure.

## Installed as

A hand-written third-party subagent named `Raven-Research` (no `preset` field).

```bash
python3 install.py --dry-run   # print the resolved entry, change nothing
python3 install.py             # back up the current list, then register
```

`install.py` goes through `PUT /raven/subagents`, not the file-level helper -
see the note in `../raven-ppt/README.md`, which is where that lesson was
learned. The endpoint takes **every** entry, not just this one, so the script
reads the live list first, merges this entry into it by name, and writes a
timestamped backup beside itself before the PUT.

`subagent.json` ships with `{SUBAGENT_DIR}` and `{PYTHON}` unresolved so the
published file carries no path from the machine that built it. They are resolved
at install time and nowhere else, because the gateway substitutes only
`{agent_id}`, `{prompt}` and `{prompt_file}`, and spawns with the *session
workspace* as cwd - so neither a relative command nor the entry's `cwd` field can
stand in for the real path. Moving this folder means re-running `install.py`;
that is the whole migration.

Registered **stateful**, since `dr@3.2` (it was stateless until then - see
`Multi-turn` for what changed and what is still unmeasured). The entry carries a
`resumeCommand` identical to `command`, and both pass `--session {agent_id}`:
the gateway mints that id on an instance's first turn and replays it on every
later one, so one instance handle is one conversation.

The three fields are a single interlocking set, and the schema rejects every
partial version of it before anything is written:

| Field | Value | Why the schema insists |
|---|---|---|
| `command` | must contain `{agent_id}` | required when `idSource` is `provisioned`; without a `resumeCommand` the same token is *rejected*, because an unsubstituted `{agent_id}` reaches the CLI as a literal string and fails obscurely. The first install of the stateless entry failed on exactly that |
| `resumeCommand` | must contain `{agent_id}` | it is the mechanism statefulness is derived from - there is no separate `stateful: true` to set, and setting one that disagrees is an error |
| `idSource` | `provisioned` | raven mints the id rather than reading it back out of a transcript |

The rejection is safe: validation happens before the write, so a bad entry
leaves the existing list untouched.

`run.py` still mints its own conversation name when neither `--session` nor
`--job` is given, so a run by hand gets a private workspace instead of joining
someone else's conversation.

`subagent.json` is now the source this folder installs from, not a souvenir of
one past install. It used to be the latter and the two drifted badly: the file
still said `researcher_x` while the live entry had been renamed `Raven-Research`
in the web UI, so installing from it would have added a *second* entry rather
than updating one, and its description still promised an agent that "cannot read
local files" long after the `-filetools` profile gave it six file tools. Read the
live entry before assuming they agree:

```bash
curl --noproxy '*' -s http://127.0.0.1:8000/raven/subagents
```

The `description` is the field that matters most, because it is what the
dispatching model reads when deciding whether to hand work to this agent. The
capability boundary belongs in it - the two web tools plus the file tools, no
shell, stateful with a self-contained *first* question, several minutes for a
question that needs new evidence - since those are exactly the assumptions a
caller gets wrong. Statefulness is the one that has to be stated outright: a
caller who believes the agent is stateless restates the whole question every
turn, which is a research turn every turn and throws away the feature.

Since `askUser` went on, the description also has to say that **the first reply is
normally questions, not the report.** With `mode: "first_turn"` that is not an
occasional shape, it is every first call, and the two ways a caller gets it wrong
are both silent: relay the questions as the deliverable, or answer them by
restating the whole question, which starts a fresh round of questions instead of
the research. Neither reads as an error anywhere.

There is a name clash worth knowing: our config already has a `Researcher`
(kind `openai`, MiroThinker deep-research over an HTTP endpoint). This entry is a
different agent with a different mechanism, hence a distinct name.

## Files here

| File | | Published |
|---|---|---|
| `run.py` | Host-side launcher (own workspace, session-log extraction, answer-based verdict) | yes |
| `install.py` | Resolves the `subagent.json` placeholders and registers the entry over the RPC | yes |
| `config.json` | Raven-X run config. Holds **no** secrets | yes |
| `subagent.json` | The third-party subagent entry, with install-time placeholders | yes |
| `.env.example` | Template for the secrets and the two path knobs | yes |
| `README.md` `.gitignore` | This file, and the exclusion list below | yes |
| `.env` | The real secrets, mode 600 | **no** |
| `.config.rendered.*.json` | Transient merge of the two, mode 600, deleted after each run | **no** |
| `Raven-X/` | The agent itself. Ships as source; its `.venv` does not | yes |
| `Raven-X-dr*-rollback.tar.gz` `config.json.bak-*` | The builds this one replaced, and the configs that preceded it | **no** |
| `subagents-backup-*.json` | What `install.py` saved before a PUT - carries every other entry on this host | **no** |
| `runs/` `cache/` `cron/` `ledger/` | Runtime dirs the build derives from the config file's parent. `ledger/` is empty at rest (see above) | **no** |

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
