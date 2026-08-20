# raven-research

Raven-X, a **Deep-Research-only agent build**: it answers a research question by
searching and reading the web, then commits an answer. Everything a
general-purpose assistant needs (chat channels, proactivity, user memory,
skills, TUI) is pruned or being pruned. The model sees exactly two tools,
`web_search` and `web_fetch`, enforced by `drFlow.toolsAllowlist` as an
allowlist that unregisters everything else at assembly.

Owner: ZuyiZhou. This folder is our caller-side record, not the agent itself.

## Where it comes from

| | |
|---|---|
| Source | https://github.com/ZuyiZhou/Raven-X, branch `main`, commit `ce225550` |
| Local checkout | `./Raven-X` - the agent itself lives inside this folder |
| Package / version | `raven` 0.1.5, flow `dr@3.7` (updated 2026-08-20 from upstream `ce225550`; `dr@3.3` came from `e3edf28` on 2026-08-18, `dr@3.2` from `Raven-X-main.zip` on 2026-08-16, and the `dr@2.8` / `dr@2.9` steps from `b68085d` and `b1c12e4` on 2026-08-11) |
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

With the real `HOME`, `tests/test_cli_sentinel_commands.py::test_sentinel_status_runs`
fails because the CLI falls back to the host's `/root/.raven/config.json`, whose
`subagents` entries carry fields this build's schema forbids (`extra_forbidden`).
That is our machine leaking into the test, not a defect in the checkout.

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
replacement of `Raven-X/`. Check that before replacing, rather than assuming it:
the base commit is whichever one whose tree the checkout matches, found by
diffing trees since no file records it, and a real local edit shows up as a file
that differs there. Compare **ASTs** when a file does differ - a formatter run
upstream reflows strings and moves blank lines, which `git diff
--ignore-all-space` reports exactly like a real change.

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

Keep `.venv` across the swap by moving it out and back. The install is editable
(`_editable_impl_raven.pth` points at this checkout), so the new source is live
immediately; `uv sync` is only needed when `pyproject.toml` or `uv.lock` changed,
which they did not for `dr@2.6 -> dr@2.8 -> dr@2.9 -> dr@3.2` but did for
`dr@3.3 -> dr@3.7` (see that section below - no package is added, so the sync is
optional in practice). `dr@2.9` also
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
  measurement outcome, not a process failure. Exit 1 means only one thing: a
  config or credential error. So `run.py` decides success by finding a committed
  answer, and reports the exit code separately.

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
answer is the same accounting seen from the other side, since the count only
covers text inside `<answer></answer>` tags that the model is asked - not
guaranteed - to emit.

### The research trail stays out of the reply

`processAppendix` renders a computed research trail - queries run, pages read,
reviewer's open points - and appends it to the value the CLI returns, never to the
persisted message. `run.py` reads the persisted message, so the trail reaches the
CLI's stdout and stops there: **the subagent reply never carries it, by decision,
and nothing needs to change to keep it that way.** Verified after the `dr@2.9`
update, when the appendix started rendering for real.

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
