# Raven-X

Raven-X is a **Deep-Research-only agent build**. It does exactly one thing: answer a
research question by searching and reading the web (or a pinned corpus), then commit an
answer. Everything a general-purpose assistant needs — chat channels, proactivity,
persistent user memory, skills, a TUI — is either already gone or scheduled for removal.

It has **one delivery form: the DR Flow enabled.** The flow-off path exists solely as a
measurement instrument (see [Measurement anchor](#measurement-anchor)) and is not a
product configuration.

**New here? Read [QUICKSTART.md](QUICKSTART.md)** — clone to answered question in about ten
minutes, with runnable configs in [`examples/`](examples/). The rest of this file is what the
build *is*, not how to start it.

## Relationship to upstream Raven

Raven-X started from [EverMind-AI/Raven](https://github.com/EverMind-AI/Raven) at commit
`dbb1b0c` (2026-07-17) and has diverged since. **The two projects are unrelated going
forward**: upstream is maintained by other people, we do not track its releases, and we do
not contribute back. The Python package is still importable as `raven` for historical
reasons; a rename to `ravenx` is planned.

The upstream remote is kept fetch-only (`upstream`, push URL disabled) for exactly one
reason: comparison runs use an official upstream build as an external baseline, and
`git log dbb1b0c..upstream/main` is the only way to audit how much upstream drift is mixed
into an "ours minus theirs" number.

## Command-line usage

There is one entry point:

```bash
raven agent -m "<question>" --config <config.json>
```

Add `-s <channel>:<id>` and pass the same key again to keep talking — that is the whole
mechanism behind a conversation, and from `dr@3.0` a follow-up decides for itself whether it
needs new research:

```bash
raven agent -s cli:demo --config examples/dr_multi_turn.json -m "What does the Serper.dev API cost?"
raven agent -s cli:demo --config examples/dr_multi_turn.json -m "Compress that to three sentences."
```

Each invocation is a fresh process that reads the session from disk, so the second command is
not a continuation of the first process — it is the product path, and the reason a conversation
survives a restart. See [Multi-turn conversations](#multi-turn-conversations) for what the
per-turn decision covers and what it deliberately leaves alone. Without `-s`, every invocation
is its own single-turn session, which is what the benchmark harness relies on.

Launched as a subprocess, one per question, by the evaluation harness in
`raven_train/pipeline/04_rollout.py`. A conversation is still not an interactive session: there
is no REPL, no gateway and no TUI in scope — each turn is its own `raven agent` process, and the
only thing carried between them is the session file. `raven --help` still lists the commands for
the pruned front-ends, inherited from the
general-purpose assistant this build was carved out of and slated for removal. The Repo
layout table below marks which subpackages are `prune`. Start from
[`examples/`](examples/), not from `raven onboard`; `raven tui` fails outright, because the
front-end it needs is already pruned.

The model sees exactly two tools: `web_search` and `web_fetch`. This is enforced by
`drFlow.toolsAllowlist`, which unregisters everything else at assembly — an allowlist
rather than a denylist, because a plugin can inject a tool that a denylist cannot count.

### Options

| Option | Effect |
|---|---|
| `-m`, `--message` | The question. **Omitting it starts a multi-turn REPL instead** — a different code path (`interactive=True`), not the one any measurement uses. |
| `--config` | Config file path. Absolute, or relative to the current directory. |
| `-w`, `--workspace` | Where the agent's scratch files and the session log go. Default `~/.raven/workspace`. **Give each concurrent invocation its own**, or they share one session directory. |
| `--no-markdown` | Print the reply as plain text instead of rendering it. Use when a human is reading piped output; do not use it as a parsing strategy (see below). |
| `--logs` | Stream runtime logs to stderr. Off by default. |
| `-s`, `--session` | Write into a named session (`<channel>:<chat_id>`) instead of minting a fresh one. |
| `-c`, `--continue` / `-r`, `--resume` | Continue the most recent / a named session. Multi-turn; irrelevant to one-shot research. |
| `--fake-now` | Freezes `now` for the Sentinel stack, for that subsystem's own eval harness. Not a general clock override; leave unset. |
| `--wait-skill-extract`, `--flush-skill-buffer` | Belong to the pruned memory subsystem. Leave them alone. |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | The process completed. |
| `1` | Configuration or credential error — missing API key, unreadable config, invalid provider. All three sites are in `raven/cli/_helpers.py`. |

**Exit `0` does not mean an answer was produced.** Nothing in the run path maps an
answerless run to a non-zero code, by design: whether the agent committed an answer is a
measurement outcome, not a process failure. A caller that needs to know must read the
session log.

### Reading the result programmatically

**Do not parse stdout.** It is rendered for a human — spinners, progress lines, tool hints —
and `--no-markdown` only turns off the renderer, not the prose. The evaluation harness never
looks at it.

Read the session log instead:

```
<workspace>/sessions/cli/<YYYYmmdd_HHMMSS_xxxxxx>.jsonl
```

One JSON object per line. The first line is session metadata (`_type: "metadata"`); every
line after it is a message with `role` in `user` / `assistant` / `tool`.

| You want | Read |
|---|---|
| The final answer | The last row with `role == "assistant"` and `finish_reason == "stop"`. |
| Which build produced it | `flow_version` on any message row — `"<drFlow.version>/<package version>"`, e.g. `"dr@3.2/raven-0.1.5"`. |
| What the flow did | `observers` on the final assistant row. |
| Tool calls and their results | `tool_calls` on assistant rows; `role == "tool"` rows carry `name` and the result. |

`observers` is the flow's own account of the turn, and it is the reason to read this file
rather than the text. From a real single-question run:

```json
{"verify_gate":   {"reviews": 1, "rejects": 1, "revisions": 1, "accepted_after_revision": true},
 "force_finalize":{"terminal_hits": 1, "nudges": 1, "last_reason": "empty_visible_answer"},
 "fetch_floor":   {"searches": 1, "fetches": 2, "notes": 0},
 "budget":        {"max_context_used": 6937},
 "turn_end":      {"status": "completed", "iterations_used": 3, "llm_calls": 5, "overflows": 0, "elisions": 0},
 "invariants":    {"tool_calls": {"web_search": 1, "web_fetch": 2}, "ok": false, "violations": ["answer_shape_unaccounted"]},
 "containment":   {"enabled": false, "blocked_fetches": 0}}
```

Two of the five observers fired on that run: `ForcedFinalizeGate` injected a commit nudge
after a turn that reasoned without answering, and `DraftReviewerGate` rejected the first
draft once. Both injections are persisted as `user` rows, so the trajectory you read is the
trajectory the model saw.

Two traps in that payload:

- **`invariants.ok == false` is not a failure.** The invariant block is a counter, not a
  gate; it does not stop anything. The run above committed a 1,303-character answer while
  reporting `ok: false`.
- **`answer_chars: 0` with a real answer present** is the same thing seen from the other
  side. The accounting counts characters inside `<answer></answer>`. Since `dr@2.6` the DR
  contract does ask for those tags by default (`drFlow.finalShape.requireMarker`), so this
  usually reads as a non-zero count — but it is a request to a model, not a guarantee, and a
  turn cut off mid-reasoning carries no tags at all. Extract by `finish_reason`, and treat the
  tags as a convenience.

### Report shape vs benchmark shape

The DR contract is five rules of research discipline and says nothing about the shape of the
reply. That is deliberate: a benchmark answer takes its format from the task text ("organize
the results in one Markdown table with the following columns: ..."), which is how a real user
asks for one too, and a system prompt that pre-announced the format per benchmark would be
measuring the router rather than the agent. Format routing happens only at scoring time, on
the seed's `scorer` field; nothing in the rollout branches on it.

Two optional clauses are appended to the contract for the product surface, both on by default
and both pinned off in every benchmark profile:

| Knob | Adds |
|---|---|
| `drFlow.finalShape.requireMarker` | end the reply with `<answer>...</answer>`, reasoning kept above it |
| `drFlow.finalShape.reportStructure` | write the reply as a research report: direct answer, then the findings that decide it with the URL each came from, then what could not be established. Comparison and mechanism sections are asked for only when the question turns on them — an empty heading is worse than no heading |
| `drFlow.finalShape.processAppendix` | append a deterministic research trail: queries run, pages read, the reviewer's open points, and any cited URL that was never opened |

#### Setting the answer format

All three knobs live under `drFlow.finalShape` in the config file
(`~/.raven/config.json`, or whatever `--config` points at). They are independent, so there
are eight states; these four are the ones worth knowing.

```jsonc
{
  "drFlow": {
    "enabled": true,
    "finalShape": {
      "record": true,             // observer only, never changes the reply
      "requireMarker": true,      // <answer>...</answer> at the end
      "reportStructure": true,    // research-report body
      "processAppendix": true     // deterministic trail after the reply
    }
  }
}
```

| Want | `requireMarker` | `reportStructure` | `processAppendix` | Reply looks like |
|---|---|---|---|---|
| **Default (product)** | `true` | `true` | `true` | report body, then `<answer>..</answer>`, then the trail |
| **Just the answer** | `true` | `false` | `false` | whatever shape the question implies, ending in `<answer>..</answer>` |
| **Prose, no tags** | `false` | `true` | `false` | report body only, no marker anywhere |
| **Benchmark contract** | `false` | `false` | `false` | byte-identical to the contract every published reading was taken under |

Nothing else needs setting. The appendix used to also require `RAVEN_WEB_LEDGER`, which no
config could supply — with `processAppendix` on, a turn-scoped ledger is opened for you and
removed once the trail is rendered. Naming the variable yourself still wins and that file is
never deleted, because a batch's ledger is measurement data.

Two things are **not** how you set the format. The five DR contract rules say nothing about
reply shape, and there is no per-benchmark format router: a bench answer takes its format
from the task text, the same way a user asking for "one Markdown table with these columns"
does. If you want a specific shape for one question, ask for it in the question.

The appendix is **computed, not asked for**. A contract clause telling the model to
describe its own search strategy buys a self-report: unverifiable, competing with evidence
for the window, and free to be wrong in the flattering direction. The ledger already holds
the real trail, written as each event happened, so the body is the model's and the appendix
is derived — no tokens spent, nothing to invent.

It is distribution-neutral by construction: it runs after the turn's last generation and
attaches to the returned value, never to the persisted message, so no model reads it in this
turn or as history in the next. That is the one exemption to the ledger's "read by nobody at
run time" rule, and it is narrow on purpose.

One field is worth watching: `observers.process_appendix.citation_grounding_rate` — the share
of URLs cited in the answer that appear in a fetch record. A link the run never opened is a
fabricated citation, and this catches it deterministically, with no judge and no reward for
length. It is `null`, not `1.0`, when the answer cites nothing: an answer with no sources is
unmeasured, not perfectly grounded.

Three constraints travel with it and are not optional. It is **post-treatment and gameable**:
citing less raises it, so it must always be read beside `urls_cited` and the share of answers
where `cites_nothing` is true — the rate alone has a denominator the measured arm chooses. It
is an **intra-arm integrity guard, not a cross-arm scoreboard**: an arm that browses more has
more chances to mis-cite, so two arms citing at different rates are not comparable on it. And
its denominator is **cited URLs, not claims** — an answer can be fully grounded and entirely
wrong; this measures whether the links are real, not whether the reasoning is.

The two prompt-side clauses are **additive and asked for during generation**, never imposed
afterwards. Restructuring
a finished answer is the failure class measured twice here — an upstream framework's extraction
stage carried gold on 71/120 questions into 58/120 boxed fields, inventing nothing and dropping
10.83pp, and `dr@1.6`'s salvage seam failed the same way — so `final_shape`'s transform stays
pure, additive and non-shortening, and shape is requested rather than enforced.

Turning both off restores the benchmark contract byte for byte (`593c46c416c3f4cf`, 3,485
chars); `scripts/stamp_dr_segment.py` is the artifact that proves which one a run used, because
the system prompt never enters the trajectory.

#### Multi-turn conversations

Everything above describes one question and one answer, and until `dr@3.0` that was all DR
mode could be: the flow was chosen once when the loop was built, so a follow-up asking to
shorten a paragraph paid for the reviewer, the spin breaker and a research appendix over an
empty trail. `drFlow.conversation` makes the choice per turn. It is **off by default**, and
every published DR reading was taken with it off — the benchmark harness sends one message
per question, so no measured trajectory has ever reached a second turn.

```jsonc
{
  "drFlow": {
    "enabled": true,
    "conversation": {
      "enabled": true,
      "gate": "agentic",        // "always" keeps the pre-3.0 behaviour
      "researchMemo": true,     // carry what was searched and opened into later turns
      "identityScope": "turn"   // "topic" stops a follow-up re-reading the same pages
    }
  }
}
```

**The first turn always follows `drFlow.enabled`.** It is the question the user opened the
product to ask, and there is no conversation yet for a classifier to read. From the second
turn on, `gate: "agentic"` asks a small classifier whether the message needs new retrieval —
a fresh two-message call sharing nothing with the turn's own context, so the history it is
judging cannot steer it. Anything it asks for that the conversation does not already contain
is research; reformat, expand, translate, summarise and conversational turns are not. **Every
failure resolves to research** — a timeout, a truncated generation, an unparseable reply. A
needless research turn costs latency; a wrongly skipped one answers a factual question from
stale context in exactly the same confident voice.

What the gate cuts is **behaviour, never text**. The DR identity and contract stay
byte-identical on every turn even when the turn is answered from context, because the system
prompt is the head of the cached prefix and varying it per turn re-bills the whole
conversation at uncached rates. The iteration cap is left alone for a similar reason: it is a
ceiling, not a spend, and lowering it would only add a way for a mis-classified turn to be cut
off mid-answer.

`researchMemo` addresses a quieter problem. Tool results are truncated to 16k characters when
persisted and then dropped whole by the history trimmer, so by the third turn the pages an
answer rests on are gone while its claims remain. The memo is a short, capped record of the
URLs opened and the queries run, rebuilt from the client-side ledger — same source as the
process appendix, and for the same reason: asking the model to recall its own research buys an
unverifiable self-report. It is prepended to the current message, never persisted (it is
re-rendered each turn from session metadata, so persisting it would compound), and it carries
retrieval only — no findings, no integrity numbers. The appendix stays display-only; feeding it
back would put a metric the model can read into the context of the turn that produces the next
value of it.

`identityScope` decides what the web tools forget at a turn boundary. `"turn"`, the default and
the measured behaviour, clears the "already seen this" sets every turn — correct for a benchmark
item, where each turn is a separate question. `"topic"` keeps them across a session so a
follow-up does not re-search and re-open the previous turn's pages. Budgets and decisions still
clear either way, including the saturation rule's stop flag: carrying a decision across a turn
boundary would close search for a question nobody had asked yet. Its cost is stated rather than
hidden — a follow-up whose first searches legitimately re-find the previous turn's pages
accumulates a dry streak it did not earn, bounded to `k` searches by the streak reset.

`observers.conversation_gate` records the verdict per turn, and it distinguishes "the gate said
no" from "the gate could not run" (`gate`, `gate_error`, `gate_unparsed`, `config_always`,
`first_turn`). A single flag would report both as the same silence.

### Environment variables

| Variable | Read by | Effect |
|---|---|---|
| `SERPER_API_KEY` | `web_search` | Required for live-web search. No config equivalent is needed; `tools.web.search.apiKey` overrides it. |
| `JINA_API_KEY` | `web_fetch` | Required for live-web page reading. `tools.web.jinaApiKey` overrides it. |
| `RAVEN_WEB_LEDGER` | web tools | Name a file and both tools append a per-call JSONL ledger: every search with its **ordered** result URLs and whether the result was replayed from cache, every fetch with url / chars / ok / docid. Unset means off, **except** that `finalShape.processAppendix` opens a turn-scoped one under `<data dir>/ledger/` and deletes it after rendering the trail. Naming the variable always wins and that file is never removed. Write-only — it changes no behaviour and no output byte. |
| `RAVEN_TRACING`, `RAVEN_TRACING_DIR` | tracing | Tracing is **on by default**; `RAVEN_TRACING=0` makes it a no-op. `RAVEN_TRACING_DIR` moves the span log off `~/.raven/traces`. |
| `RAVEN_CLI_DEBUG` | CLI | Keeps loguru's default handler. Without it the CLI installs a WARNING-only stderr sink, so an INFO-level explanation of a failure is discarded before you see it. |
| `RAVEN_HOME` | tracing, TUI runtime **only** | Does **not** relocate the workspace, the config, or the session logs — those come from `Path.home()` directly (`raven/config/paths.py`). Use `--workspace` to move the run's output. |
| `http_proxy` / `https_proxy` | web tools | Inherited by default, because the fetch path constructs its client with `proxy=None` and leaves `trust_env` on. Set `tools.web.proxy` in the config to override per-arm. |

The LLM provider key is **not** read from the environment — `providers.<name>.apiKey` must be
present in the config, or the run exits 1.

### Invoking it in a batch

One process per question, each with its own workspace, is the only shape that has been
measured:

```bash
while read -r qid question; do
  raven agent --config arm.json --workspace "runs/$qid" -m "$question" \
    > "runs/$qid/stdout.txt" 2>&1
done < questions.tsv
```

Two constraints worth stating, both learned the hard way:

- **A shared workspace is a shared session directory.** Concurrent runs without `--workspace`
  write into one place and their trajectories interleave.
- **Search and fetch are separate vendors with separate quotas.** Probe both before a long
  run; a working search tells you nothing about whether pages can be read. A batch of 604
  runs once completed with every single fetch failing on a payment error, and the only gate
  watching reported a search-side number.

## DR Flow

`drFlow` in the config is the single versioned carrier for every behavioural knob. The
flow assembles five observers in a **contractually fixed order** (`raven/agent/flow/dr.py`):

| # | Observer | Job |
|---|---|---|
| 1 | `BudgetNoteObserver` | injects the remaining-iteration budget into persisted history |
| 2 | `FetchFloorObserver` | nudges when the agent searches without ever opening a page |
| 3 | `SpinEntryBreaker` | intercepts a restart — must run *before* the terminal gates see it |
| 4 | `ForcedFinalizeGate` | an answerless terminal turn is salvaged, never reviewed |
| 5 | `DraftReviewerGate` | reviews a draft that does have an answer |

Order 3-before-4-before-5 is load-bearing, not stylistic. Two tool-layer rewrites also
hang off the flow: `web_fetch` returns a digest, and `web_search` drops snippets and the
answer box and annotates a query repeated verbatim within the turn.

### Terminal-answer shaping — on by default, switchable

`final_answer` is the last non-empty assistant message, verbatim. Left alone, a turn tends to
*stop* rather than *end*: on a 302-question live-web batch only 15.6% of answers carried any
answer marker, and one sampled answer opened "This is very helpful. According to the Nagada
website: ..." — reasoning filed as the answer. Shaping makes the ending an explicit act, so
"what was the answer?" is answerable without re-reading the reasoning.

Two independently switchable pieces, both **on by default since `dr@2.6`**. For the full
answer-format picture, including `reportStructure` and `processAppendix`, see
[Setting the answer format](#setting-the-answer-format).

```jsonc
{
  "drFlow": {
    "enabled": true,
    "finalShape": {
      "record": true,          // read-only observer; costs nothing to leave on
      "requireMarker": true    // appends one clause to the DR contract
    }
  }
}
```

| Knob | What it does | Cost of leaving it on |
|---|---|---|
| `record` | Runs `shape_final_answer` on the terminal content and records the shaped form (`answer_tag` / `boxed` / `labeled` / `unmarked` / `empty`) beside the raw answer | None. It never touches `final_content`, the persisted messages, or anything the model reads, so the generated distribution is byte-identical either way |
| `requireMarker` | Appends one clause asking the model to close with `<answer>...</answer>` | It changes what the model reads, so it changes the distribution |

**When to turn them off.** Set `requireMarker` to `false` to compare against a reading taken
before `dr@2.6`: that restores the earlier contract byte for byte, which is the only honest way
to attribute a difference to something else. Benchmark arms should pin **both** values
explicitly rather than inherit them — an inherited default does not appear in the arm's own
config, so it can change what the arm measures without changing the file that describes it.

The shaper is additive by construction: it appends a canonical `Answer:` line and never
rewrites or drops the body. It refuses on an empty marker (`<answer></answer>`) and keeps the
original text, because *extraction that blanks an answer-bearing turn* is the specific failure
this was built against — a measured upstream pipeline carried a correct answer on 71/120
questions into a boxed field on 58/120, producing nothing new and dropping 10.83pp.

### Version labels

`drFlow.version` is **the key of the measurement ledger**, not a product name. Every
version changes the generated distribution, so the config validator rejects a superseded
label on a newer build. Current: `dr@3.2`.

Note that this is not the same number as `raven --version`, which reports the package
version inherited from upstream. `drFlow.version` is the one that identifies behaviour.

**Reproducing the published readings.** `main` carries the current label, which is ahead of
the one the published corpus-axis numbers were measured on. To reproduce those, check out the
tag `dr2.4-published` (`raven/` tree_sha `2db7aa62042eb97c`); the `dr@2.7` corpus and live-web
readings were measured from the working tree, so they reproduce only from each run's own
`_src_snapshot`, with `dr2.7-base` marking the commit they were cut from. Two consequences of `main`
being ahead, both intended: its `tree_sha` differs from the measured one, and a config still
labelled anything from `dr@2.4` through `dr@3.1` is rejected on it — the validator matches the base label,
so a profile suffix such as `dr@2.4-futurex` is rejected too. Update such configs to `dr@3.2`;
the error message names the value to use.

**There is no commit that "is" `dr@3.1`.** It was developed and measured, but never became its
own commit; it landed together with `dr@3.2`, behind the tag `dr3.0-base`, which marks `main`
as it stood at `dr@3.0`. Its two batches of readings are still reproducible, because provenance
here runs through each batch's own `_src_snapshot` plus the per-arm fingerprints in
`iso_manifest.json` — never through git granularity. Treat git as a convenience for reading
history, not as the measurement ledger; when a batch and a tag disagree, the batch is right.

| Label | The one thing it changed |
|---|---|
| `dr@1` | the five nodes, hook dispatch, rollback, crash-sidecar journal |
| `dr@1.1` | verify gate stops failing open for the whole run |
| `dr@1.2` | budget line derived from failure-mode quartiles |
| `dr@1.3` | context-window overflow no longer kills a turn silently |
| `dr@1.4` | tool surface pinned by allowlist; digest folds closed-tag reasoning |
| `dr@1.5` | a terminal turn with no closing think tag counts as answerless |
| `dr@1.6` | a verbatim-repeated search is annotated and served from cache |
| `dr@1.7` | a salvage reply is held to the same closing-tag bar as the answer it replaces |
| `dr@1.8` | the cheap commit nudge can reach an unclosed turn; fetch-floor streak counts since the last fetch |
| `dr@1.9` | the restart detector stops reading two idioms the identity segment itself taught the model |
| `dr@2.0` | the product identity segment is replaced by a DR one; the reviewer stops failing claims whose evidence was elided |
| `dr@2.1` | the dr@2.0 prompt bytes a refactor moved are restored; the elision trigger reads the whole context |
| `dr@2.2` | fixes and instruments at a score budget of 0: a committed salvage stops being miscounted as answerless, and the corpus retrieval log stops merging batches that share an arm directory name |
| `dr@2.3` | the instruments stop lying: `status` no longer reads "ok" on a run that hit the context window, every answerless run is attributed, and `answer_rate` becomes a pre-registered secondary endpoint |
| `dr@2.4` | the corpus SERP snippet is restored, deduplicated by docid, on the arm that measures candidate selection — per-config, so the live-web arms and the flow-off anchor do not move |
| `dr@2.5` | terminal-answer shaping arrives, off by default, two independently gated pieces: a read-only observer that records the shaped form beside the raw answer (distribution-free by construction), and one appended contract clause asking for an explicit `<answer>` marker (a distribution change, hence the bump) |
| `dr@2.6` | terminal-answer shaping is on by default, and every shipped arm pins the pair explicitly instead of inheriting it |
| `dr@2.7` | the reader path stops discarding pages it could have read: a local resolver failure no longer refuses a fetch the reader service performs on our behalf, and a transport failure that never produced a response is retried twice before the page is abandoned |
| `dr@2.8` | three changes, every one class-default-off and gated per arm, so the flow-off anchor cannot move: cross-query dedup with pagination on the corpus path (three call sites capped results at ten while the service clamps to fifty), the live-web result snippet restored together with its per-docid dedup, and a verify rejection that may buy retrieval instead of only a rewrite. Product surface only: the research-report body and the deterministic process appendix |
| `dr@2.9` | a wrap-up for turns killed by the completion budget rather than the tool-calling budget - **not** gated on the flow, because it repairs a defect the anchor suffers from most, which is why it carries its own label. Two ledger repairs in the same direction (a cancelled fetch records an `aborted` row instead of none; a write failure can no longer kill the run it measures). Product surface: the process appendix can finally render, because the trail no longer depends on an environment variable only a batch launcher sets |
| `dr@3.0` | the two observers that divide by the context window now divide by the window the turn actually runs on — both were handed the configured default, and on the served student that resolved to the same number, so the divergence was latent for the whole `dr@2.x` ladder. Also three criteria whose scope said "this turn" but were fed a whole conversation, all found by running one rather than by the suite: the fabricated-citation check now spans the conversation (reported as `opened_earlier`), its URL extractor stops at CJK punctuation instead of swallowing the rest of a Chinese clause, and `turn_invariants` is handed one turn. Everything else is default-off or product-only: multi-turn conversations, the search-saturation ladder, `dr@2.9`'s wrap-up moved behind a gate, and a reviewable shipping draft |
| `dr@3.1` | the replay cache key gains `page`. The key was `(query, n_requested, k)`, so once a turn had paged to 2, re-asking a query already asked that turn returned the page-1 entry: 206 of 289 page-2 retrievals were page-1 replays, 71.3%. The waste is the smaller half — a replay is observed as a dry search, so every fake page turn also pushed the turn one step toward `stopped`, and the broken rung was feeding the rung above it. The comment directly above the key had already written the rule down correctly, but enumerated the dimensions by hand and listed only `n_requested` and `k`; a rule that has to be re-applied by hand to each new parameter is a rule that will be missed. Also `sat_event`, non-empty only on a real tier transition, because `sat_action` is a sticky label stamped on every row and counting rows overstates `stopped` by 67x |
| `dr@3.2` | the context-window fallback doubles, 65536 to 131072, and the window is resolved **once** rather than at each consumer — `HistoryTrimmer`, `MemoryConsolidator` and `BudgetObserver` were still reading the unresolved configured value, and `BudgetObserver` is installed only on the flow-off branch, which made the divergence arm-correlated. On the served student all three agreed by accident, because the resolver returns `None` and every consumer fell back to the same number. Also `harness_text.py`, which splits one predicate into two that deliberately disagree: permissive where a false positive costs one item of look-back, strict where it would destroy a real answer. Instrument-only otherwise: `scored_by` stamps the scorer's caliber and file hash on every scored row, the ledger records `n_served`, and `renderedWidth` becomes a knob that is byte-identical at its default |

Human-facing name for the current build is **Raven-X 3.2**; every superseded label stays
in code so historical results remain indexable, and the validator matches the BASE label
so a profile suffix such as `-futurex` cannot smuggle a retired one onto a new build.

### The context window is a fallback, not a claim

`agents.defaults.context_window_tokens` is **131072** as of `dr@3.2`, raised from 65536. Read
it as "what we assume when we cannot find out", not as "this model has a 128k window": it is
consulted only when `resolve_context_window(model)` cannot answer, and on the student we serve
that is exactly what happens. Do not quote it as a capability.

Two consequences worth stating plainly, because both have bitten:

- **It moves the trim point on both arms**, so it is not a treatment — it is a new operating
  point, and it needs a fresh anchor pair. Readings taken either side of the change cannot be
  subtracted. This flow's primary mechanism since `dr@1.4` has been context-overflow
  prevention, so a wider window is expected to *shrink* the measured delta by rescuing the
  anchor too. That is the correct result, not a regression.
- **Raising it further is not free.** Rope extrapolation past what the checkpoint was trained
  for degrades quality silently rather than failing loudly, and doubling the window doubles KV
  per request, which lands hardest on the arm that is already the slower one.

## Measurement anchor

Every Flow change is judged by a same-batch A-minus-B against an arm with
`drFlow.enabled=false`. That arm is an **instrument**, not a shipped configuration:

- it never appears in an "us vs upstream" comparison;
- moving it invalidates the historical reference frame, so zero-bucket fixes are
  deliberately gated on `DRFlowConfig` and no-op when the flow is off;
- internal A-minus-B therefore contains a component of "we fixed a bug and the anchor did
  not", which must be accounted separately.

## Repo layout

This section is **normative**: `AGENTS.md` §3 defines a commit scope as a top-level
subpackage of `raven/`. Sizes are Python lines. `prune` marks what the DR-only pruning
campaign removes; `gated` marks code that is not dead — it feeds the per-turn token budget,
so deleting it moves the context trim point on both arms and is a Flow change, not a prune.

| `raven/` subpackage | Lines | Status |
|---|---|---|
| `agent/` | 10,677 | keep (loop, flow, hooks; subagent/personalizer/checkpoint prune) |
| `auth/` | 266 | prune |
| `channels/` | 7,681 | prune |
| `cli/` | 13,827 | keep `agent_commands` only; ~12.9k prune |
| `config/` | 4,529 | keep (`update*` modules prune) |
| `context_engine/` | 2,703 | keep (`segments/` gated) |
| `eval_engine/` | 772 | prune |
| `memory_engine/` | 6,048 | prune, except `skill_forge`/`skills` gated |
| `plugin/` | 2,074 | prune — it injects a tool no config declares |
| `proactive_engine/` | 14,563 | prune |
| `providers/` | 2,430 | keep (unused providers prune) |
| `routing/` | 912 | prune |
| `sandbox/` | 1,529 | undecided — needs its own closure pass |
| `security/` | 137 | keep |
| `session/` | 645 | keep in part — needs a closure pass |
| `skill_hub/` | 263 | prune |
| `spine/` | 995 | undecided |
| `templates/` | 0 (md only) | gated |
| `token_wise/` | 943 | keep pricing/catalog; strategies prune |
| `tracing/` | 1,643 | keep (no-op when `RAVEN_TRACING=0`) |
| `tui_rpc/` | 5,966 | prune |
| `utils/` | 564 | keep |

Top level: `tests/`, `docs/` (tracing API + sandbox only), `scripts/`, `LICENSES/`,
`bridge/` (packaged, closure unproven), `benchmarks/` and `ui-tui/` (both slated for
removal, pending decision).

## Development

`uv` is the only package manager — see `AGENTS.md` §4 for the forbidden list.

```bash
uv sync                      # after any directory move, use --reinstall
uv run pytest -q             # never bare pytest
uv run pytest tests/test_agent_flow_dr.py tests/test_agent_flow_finalize.py
```

`tests/test_agent_flow_dr.py` is the flow/anchor contract and must stay green through any
pruning step.

Verification rule: validate a change with **`.venv/bin/python` from this tree**. A
same-named `raven` package exists in other checkouts, and a patch applied to the wrong one
passes its own tests while never running.

## License and attribution

Derived from upstream Raven; see `LICENSE`, `LICENSES/`, and `NOTICES.md` for the upstream
license text and third-party notices. `NOTICES.md` is referenced by
`pyproject.toml` `license-files` and is part of the package metadata — do not delete it.
