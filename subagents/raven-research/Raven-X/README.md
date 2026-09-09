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
| `--flush-skill-buffer` | Promote this session's captured turns before exit, so a deferred-capture memory backend derives episodes / cases / skills from them. Blocks on one request. Measurement runs leave it off and promote out of band (`scripts/everos_flush_batch.py`), which keeps extraction cost out of every question's wall-clock. |
| `--wait-skill-extract` | Inert; kept for call-site compatibility. Leave it alone. |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | The process completed. |
| `1` | Configuration or credential error — missing API key, unreadable config, invalid provider, or a memory backend that opted into `require_service=true` and found no usable service. Every site is in `raven/cli/_helpers.py`. |

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
| Which build produced it | `flow_version` on any message row — `"<drFlow.version>/<package version>"`, e.g. `"dr@3.3/raven-0.1.5"`. |
| What the flow did | `observers` on the final assistant row. |
| Tool calls and their results | `tool_calls` on assistant rows; `role == "tool"` rows carry `name` and the result. |

`observers` is the flow's own account of the turn, and it is the reason to read this file
rather than the text. From a real single-question run:

```json
{"verify_gate":   {"reviews": 1, "rejects": 1, "revisions": 1, "accepted_after_revision": true},
 "force_finalize":{"terminal_hits": 1, "nudges": 1, "last_reason": "empty_visible_answer"},
 "fetch_floor":   {"searches": 1, "fetches": 2, "notes": 0},
 "budget":        {"max_context_used": 6937},
 "turn_end":      {"status": "completed", "iterations_used": 3, "llm_calls": 5, "overflows": 0,
                   "elisions": 0, "effective_context_window": 131072,
                   "context_window_source": "config_fallback", "salvage_seam": null,
                   "answerless_corrected": false, "loop_nudges": 0, "prefill_retries": 0},
 "invariants":    {"tool_calls": {"web_search": 1, "web_fetch": 2}, "ok": false, "violations": ["answer_shape_unaccounted"]},
 "containment":   {"enabled": false, "blocked_fetches": 0}}
```

`ForcedFinalizeGate` and `DraftReviewerGate` both fired on that run — a commit nudge after a
turn that reasoned without answering, and one rejected draft. Both injections are persisted as
`user` rows, so the trajectory you read is the trajectory the model saw.

**The namespace set varies per row, so a missing one is not a signal.** Seven are effectively
always present (`turn_end`, `invariants`, `containment`, `truncation_wrapup`, `final_shape`,
`report_shape`, `budget` — 99.7–99.8% of 875 rows on the last web batch); the rest appear only
when they fire (`fetch_floor` 98.4%, `verify_gate` 90.9%, `spin_breaker` 32.0%,
`force_finalize` 11.9%, `loopscan` 0.8%, `dup_query` 0.2%), and a harness-error row carries
none at all. So **0% is not evidence that an observer did not fire** — it reads identically to
one that was never exported, which is exactly how `fetch_gate` stayed invisible until `dr@3.5`
added it to `terminal_state()`. Count a new namespace's occurrence rate against those figures
before trusting a zero.

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
| `drFlow.finalShape.reportStructure` | write the reply as a research report with a fixed three-section template — `## Answer`, `## Findings` (each finding with the URL it came from), `## Limitations` — exact headings, all three present every time, no other headings; since `dr@3.4` the template also overrides any formatting instructions in the question itself, behind its own sub-switch `finalShape.reportFormatOverride` (default on; off restores the `dr@3.4` clause byte for byte — the template introduced without the override; through `dr@3.4` this was an ordered-prose request with optional per-question sections) |
| `drFlow.finalShape.reportReminder` | `dr@3.4`, on: repeat the template as a short block on each turn's user message, stripped before persist. The clause is in the system prompt, which loses to the model's own recent replies on exactly the turns that reformat one |
| `drFlow.finalShape.reportBounce` | `dr@3.4`, **off**: bounce a draft missing a section back once for a rewrite. A deterministic markdown check — no model call — so it is the one DR observer that also runs on non-research turns. Off until its cost is priced; see the ladder |
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
      "reportStructure": true,    // fixed three-section report body
      "reportFormatOverride": true, // template beats the question's format asks
      "reportReminder": true,     // restate the template next to the question
      "reportBounce": false,      // rewrite a draft that is missing a section
      "processAppendix": true     // deterministic trail after the reply
    }
  }
}
```

| Want | `requireMarker` | `reportStructure` | `processAppendix` | Reply looks like |
|---|---|---|---|---|
| **Default (product)** | `true` | `true` | `true` | `## Answer` / `## Findings` / `## Limitations`, then `<answer>..</answer>`, then the trail — with the `dr@3.4` reminder restating the template each turn |
| **Just the answer** | `true` | `false` | `false` | whatever shape the question implies, ending in `<answer>..</answer>` |
| **Prose, no tags** | `false` | `true` | `false` | the three-section report body only, no marker anywhere |
| **Benchmark contract** | `false` | `false` | `false` | byte-identical to the contract every published reading was taken under |

Nothing else needs setting. The appendix used to also require `RAVEN_WEB_LEDGER`, which no
config could supply — with `processAppendix` on, a turn-scoped ledger is opened for you and
removed once the trail is rendered. Naming the variable yourself still wins and that file is
never deleted, because a batch's ledger is measurement data.

With `reportStructure` **on**, the precedence flips (since `dr@3.4`): the template beats a
formatting instruction in the question, so a question asking for one word, a JSON object or a
table is still answered as the three-section report, with the requested shape satisfied inside
it. To honour per-question format asks, set `finalShape.reportFormatOverride: false` — the
template then applies only where the question is silent — or turn `reportStructure` off.

The appendix is **computed, not asked for**. Asking the model to describe its own search
strategy buys a self-report: unverifiable, competing with evidence for the window, and free to
be wrong in the flattering direction. The ledger already holds the real trail, so the body is
the model's and the appendix is derived. It is distribution-neutral by construction — it runs
after the turn's last generation and attaches to the returned value, never to the persisted
message, so no model reads it this turn or as history next turn.

One field is worth watching: `observers.process_appendix.citation_grounding_rate` — the share
of cited URLs that appear in a fetch record, so a link the run never opened is caught
deterministically, with no judge and no reward for length. It is `null`, not `1.0`, when the
answer cites nothing: an answer with no sources is unmeasured, not perfectly grounded. Three
constraints are not optional. It is **post-treatment and gameable** — citing less raises it, so
read it beside `urls_cited` and the share of answers where `cites_nothing` is true. It is an
**intra-arm integrity guard, not a cross-arm scoreboard**: an arm that browses more has more
chances to mis-cite. And its denominator is **cited URLs, not claims** — an answer can be fully
grounded and entirely wrong.

The two prompt-side clauses are **additive and asked for during generation**, never imposed
afterwards. Restructuring a finished answer is the failure class measured twice here — an
upstream framework's extraction stage carried gold on 71/120 questions into 58/120 boxed
fields, inventing nothing and dropping 10.83pp, and `dr@1.6`'s salvage seam failed the same way
— so `final_shape`'s transform stays pure, additive and non-shortening. Turning both off
restores the benchmark contract byte for byte (`593c46c416c3f4cf`, 3,485 chars);
`scripts/stamp_dr_segment.py` proves which one a run used, because the system prompt never
enters the trajectory.

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

What the gate cuts is **behaviour, never text**: the DR identity and contract stay
byte-identical on every turn, because the system prompt heads the cached prefix and varying it
re-bills the whole conversation at uncached rates. The iteration cap is left alone for a
related reason — it is a ceiling, not a spend, and lowering it only adds a way for a
mis-classified turn to be cut off mid-answer.

`researchMemo` addresses a quieter problem: tool results are truncated to 16k characters when
persisted and then dropped whole by the history trimmer, so by the third turn the pages an
answer rests on are gone while its claims remain. The memo is a short, capped record of the
URLs opened and queries run, rebuilt from the client-side ledger rather than from the model —
asking it to recall its own research buys an unverifiable self-report. Prepended to the current
message and never persisted (it is re-rendered each turn, so persisting would compound), and it
carries retrieval only: feeding the appendix's integrity numbers back would put a metric the
model can read into the context of the turn that produces the next value of it.

`identityScope` decides what the web tools forget at a turn boundary. `"turn"` (default, and the
measured behaviour) clears the "already seen this" sets every turn — correct for a benchmark item,
where each turn is a separate question; `"topic"` keeps them so a follow-up does not re-open the
previous turn's pages, at the cost of a dry streak it did not earn, bounded to `k` by the streak
reset. Budgets and *decisions* clear either way, including the saturation stop flag: carrying a
decision across a turn boundary would close search for a question nobody had asked yet.

`observers.conversation_gate` records the verdict per turn, and it distinguishes "the gate said
no" from "the gate could not run" (`gate`, `gate_error`, `gate_unparsed`, `config_always`,
`first_turn`). A single flag would report both as the same silence.

#### Asking the user first (`drFlow.askUser`)

A research turn can begin by asking the user one round of questions instead of guessing. The
model calls `ask_user`; the flow turns that call into the turn's reply and ends the turn, and the
user's next message arrives carrying the answer — nothing blocks on a human, the tool call is the
signal and the turn boundary is the transport. Product surface only, and it needs the multi-turn
surface above.

```jsonc
{
  "drFlow": {
    // A batch that publishes a number pins "version": "dr@3.5-askuser" here — a
    // profile suffix, not a new base label. The shipped example deliberately does
    // NOT pin it: an example has to inherit the label or it breaks on the next bump.
    "toolsAllowlist": ["web_search", "web_fetch", "ask_user"],
    "conversation": { "enabled": true },
    "askUser": {
      "enabled": true,
      "mode": "first_turn",        // default: turn one always asks. "when_needed" reverts
      "outline": true,             // ask for the plan of attack alongside the questions
      "maxRounds": 1,              // per clarify CHAIN, not per session
      "firstIterationOnly": true,  // withdrawn from the schema after the first search.
                                   // A SECURITY boundary, not only a throttle: once the
                                   // run has read a page, a tool still on offer is a
                                   // channel for relaying that page's instructions to
                                   // the user. Off, the identity clause is the only
                                   // thing left refusing.
      "brief": false               // OFF by default and unproven: at maxRounds 1 the
                                   // question and the reply are the two most recent
                                   // messages already. Read its docstring first
    }
  },
  "tools": { "disabledTools": ["exec", "spawn", "..."] }   // must NOT list "ask_user"
}
```

**Two keys turn it on**, and a third can keep it off. The feature is unreachable from a benchmark
arm because four separate things hold it shut, but only two of them are yours to open:

| # | What holds it shut | Who opens it |
|---|---|---|
| 1 | `drFlow.askUser.enabled` is `false` | **you** |
| 2 | it resolves to `askUser.enabled and conversation.enabled`, and `conversation.enabled` is `false` | **you** |
| 3 | `drFlow.toolsAllowlist` does not name `ask_user`, and everything unnamed is unregistered | `build_dr_flow`, once 1 and 2 are on — naming it yourself is harmless but unnecessary. An **empty** allowlist means "no slimming" and is left empty |
| 4 | `tools.disabledTools` listing `"ask_user"` — it runs **before** the allowlist | **you**, by removing the entry. Nothing lists it by default, but every shipped bench example pins it |

So from a fresh config it is two keys; from a copy of a bench profile it is those two plus
deleting one array entry. If you set both and the model still never asks,
`observers.ask_user.tool_absent` says the tool was missing from the schema — that is cause 4.
`examples/dr_ask_user.json` opens all four; it is a sibling of `examples/dr_multi_turn.json`
rather than an edit to it, so a reading from one is still comparable with the other, and like
its sibling it leaves `thinkClosingTagRequired` at `false` (the model it ships with emits no
think tags, and the bar would erase every answer). `observers.ask_user` is written on **every**
turn, asked or not: "it asked nothing" and "it was not installed" are the numerator and the
missing denominator of the only number that could ever justify defaulting this on.

**`mode` decides when the contract asks for a round.** `first_turn` (default) always asks on
turn one and later asks only when something is genuinely undecidable; `when_needed` applies that
condition to every turn and renders the clause byte-for-byte as before the knob existed, which
is what makes it an exact revert. The clause text does **not** vary per turn — it states both
regimes in one fixed block and the model reads its turn number off the history, because the
system prompt heads the cached prefix and varying it re-bills the whole conversation at uncached
rates (measured 4.3x on this stack).

⚠️ **`first_turn` is a request, not a guarantee, and its cost is real.** Nothing can make a
model emit a tool call, so `observers.ask_user.asked` on a mandated turn is a *compliance
rate*; the namespace carries `first_turn` and `ask_required` to keep those turns separable.
Enforcement — bouncing a first turn that did not ask — is deliberately absent: it re-samples
turns, the strongest kind of distribution change, and the compliance rate has to be known
first. Two prices ride along: filler questions ("how deep would you like?") teach users to
ignore the round entirely, and `dr@3.4` measured replies whose history carries an outline as
well-formed 2/9 against 8/14 elsewhere, which this mode pays on every conversation. Ask-rate data on questions that are *not* underspecified is what
should decide between the two modes.

**A clarify written as prose is a third outcome, and it is counted.** Seen live: the model
produced questions as ordinary reply text without calling the tool, so the terminal gates read
it as a draft and the reviewer's bounce sent it off to research an ambiguous question under an
assumption. `ClarifyExemptHook` stands the three terminal gates down on exactly one response
(mandated turn, first iteration, no tool call, no report section, a question mark) and
`observers.ask_user.prose_clarify` / `clarify_chars` record it, because `asked: false` reads the
same for "used the wrong channel" and "chose not to ask". The round budget is scoped to the
clarify **chain**, not the session. Both are spelled out in `raven/agent/flow/ask_user.py`.

Two things to know before the first run:

| Scenario | How to run it | Why |
|---|---|---|
| Interactive, or a two-turn check by hand | first turn plain `-m` (a session is minted for you), second turn `-c` | no key to invent; `-c` resolves the most recent `cli` session |
| Parallel batches | an explicit `-s`, and **never** `-c` | `-c` takes the most recent `cli` session globally, so parallel runs would fight over one |

⚠️ `-s` names prefix-collide. `SessionManager.resolve_key` matches exactly first and then by
*unique prefix*, so `-s q1` resolves to `cli:q12` when only `q12` exists, and the second question
lands silently in the first one's session. Use fixed-length ids (`raven session create`), not
`q1 / q2 / ...`.

⚠️ Turning this on **supersedes the personalizer's clarifying question**
(`enable_personalization`) build-wide, warning once per turn when both are on: two owners cannot
ask in one turn, and the personalizer's question never enters the agent loop so it cannot carry
an outline. That is a pre-existing regression rather than part of this design; the fix, when both
must live, is to move the personalizer's clarify onto this same turn-boundary seam.

### Environment variables

A `.env` file **beside the config file** is merged into the process environment
before the config is read, so credentials stay out of a config you can publish:

```bash
# ~/.raven/.env   (or next to whatever --config points at).  chmod 600 it.
SERPER_API_KEY=sk-...
JINA_API_KEY=sk-...
ANYSEARCH_API_KEY=sk-...      # one key, both web tools
```

It only seeds the environment - it adds no second way to resolve a setting, so
precedence stays **config file > environment > `.env`**. A real `export` always
beats the file. The directory is the config's own, the same anchor this runtime
already uses for `sessions/`, `cache/` and `ledger/`, so the same command reads
the same `.env` wherever it is launched from. `RAVEN_DOTENV=0` turns it off (the
test suite sets this). Not to be confused with `subagents/raven-research/.env`
in the host repo, which is that launcher's own file and uses `RESEARCH_`-prefixed
names.

| Variable | Read by | Effect |
|---|---|---|
| `SERPER_API_KEY` | `web_search` | The key for the default search backend. `tools.web.providers.serper.apiKey` overrides it. |
| `SERPAPI_API_KEY` | `web_search` | Used when `tools.web.search.provider` is `serpapi`; `tools.web.providers.serpapi.apiKey` overrides it. Pages by result offset, so depth works as it does on Serper. |
| `ANYSEARCH_API_KEY` | `web_search`, `web_fetch` | One key, both tools — the credential is keyed by vendor (`tools.web.providers.anysearch.apiKey`), not by the tool that uses it. As a search backend it serves no page beyond the first, so search depth is unavailable on it: the tool tells the saturation rule up front and the ledger records `sat_action=stopped_degraded` rather than a dry search the endpoint never had a chance to serve. As a fetch backend it has no anonymous tier, so selecting it without this key withholds `web_fetch`. |
| `TAVILY_API_KEY` | `web_search`, `web_fetch` | One key, both tools (`tools.web.providers.tavily.apiKey`). As search, no documented result offset, same `stopped_degraded` handling as AnySearch. As fetch (`/extract`) it has no anonymous tier, so selecting it without this key withholds `web_fetch`. |
| `EXA_API_KEY` | `web_search`, `web_fetch` | One key, both tools (`tools.web.providers.exa.apiKey`). As search, neural search over Exa's own index, also with no result offset. As fetch (`/contents`) it has no anonymous tier. |
| `BRAVE_API_KEY` | `web_search` | Used when `tools.web.search.provider` is `brave`; `tools.web.providers.brave.apiKey` overrides it. Pages by page index (`offset`), unlike SerpApi's result-count `start`. Brave has no vendor fetch/extract endpoint, so it is search-only. |
| `JINA_API_KEY` | `web_fetch` | The default page reader. **Optional** — `r.jina.ai` answers unauthenticated at a lower rate limit, and a dead key is worse than none (402 where no key answers 200). `tools.web.providers.jina.apiKey` overrides it. |
| `FIRECRAWL_API_KEY` | `web_search`, `web_fetch` | One key, both tools (`tools.web.providers.firecrawl.apiKey`). No anonymous tier on either endpoint (`/v1/search`, `/v1/scrape`), so selecting it without this key withholds the tool. |
| `RAVEN_WEB_LEDGER` | web tools | Name a file and both tools append a per-call JSONL ledger: every search with its **ordered** result URLs and whether the result was replayed from cache, every fetch with url / chars / ok / docid. Unset means off, **except** that `finalShape.processAppendix` opens a turn-scoped one under `<data dir>/ledger/` and deletes it after rendering the trail. Naming the variable always wins and that file is never removed. Write-only — it changes no behaviour and no output byte. |
| `RAVEN_TRACING`, `RAVEN_TRACING_DIR` | tracing | Tracing is **on by default**; `RAVEN_TRACING=0` makes it a no-op. `RAVEN_TRACING_DIR` moves the span log off `~/.raven/traces`. |
| `RAVEN_CLI_DEBUG` | CLI | Keeps loguru's default handler. Without it the CLI installs a WARNING-only stderr sink, so an INFO-level explanation of a failure is discarded before you see it. |
| `RAVEN_DOTENV` | config loader | `0` disables the `.env` seeding described above. Anything else (or unset) leaves it on. |
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

`drFlow.version` is **the key of the measurement ledger**, not a product name. Since
2026-08-25 it advances on **batch launch**: a label names the code a batch ran under, so it
moves when a batch closes, not when a distribution changes. Current: **`dr@3.5`** — dr@3.4's
web batch (`eval_web_dr34_dsv4f0731_20260821`, four arms, adjudicated) closed that rung out.
It is not `raven --version`, which reports the package version inherited from upstream.

The launch rule retired the *fold* tier. Under the old rule a bump protected a reading that
already carried the old label, so a label with zero batches behind it could be widened in
place — that is how `dr@3.3` absorbed the turn-task fix and `dr@3.4` absorbed three upstream
labels, which the validator then had to refuse as *folded*. Labels are cheap now, so
`dr@3.5`–`dr@3.7` are ordinary rungs again and the folded tier is gone. Renumbering is safe
either way, because a reading's join key was never the label: it is `dr_segment_sha`
(AGENTS.md 0.2).

**Bumping is two halves, and doing one is silent.** Raise the default *and* move the closed
label into `_SUPERSEDED_VERSIONS`. Do only the first and the old label still loads with the
flow on, so an unsynced config keeps stamping a rung this build no longer is — the label is
behaviour-inert, so nothing at runtime complains. Then sync every enabled config template.
`tests/test_config_raven_loader.py` asserts both halves, derived from the field default.

**The validator matches the base label**, so `dr@2.4-futurex` is rejected along with
`dr@2.4` — and that is also what makes suffixes usable: `dr@3.5-askuser` marks a product
profile with `drFlow.askUser` on. A suffix, not a rung, because a label marks a measured
build and no benchmark arm can reach that feature. The rejection is additionally gated on
`enabled`, so a flow-off anchor keeps its historical label: with no flow there are no flow
semantics to name.

**Reproducing published readings.** `main` is ahead of every published number. The corpus-axis
readings reproduce from the tag `dr2.4-published` (`raven/` tree_sha `2db7aa62042eb97c`); the
`dr@2.7` readings were taken from a working tree and reproduce only from each run's own
`_src_snapshot`, with `dr2.7-base` marking the commit they were cut from. `dr3.4-prebump`
marks `main` as it stood before this bump.

**A label and a commit do not map onto each other, in either direction.** `dr@3.1` never got
its own commit — it was measured, then landed with `dr@3.2`. `dr@3.3` got two commits with
different behaviour. So provenance never ran through git: it runs through each batch's
`_src_snapshot` plus the per-arm fingerprints in `iso_manifest.json`. **When a batch and a tag
disagree, the batch is right.**

The ladder below is one line per rung, and the launch rule is what makes that honest: every
label through `dr@3.4` has a closed batch, so what is worth knowing here is *which* thing
changed — the verdict, the measurement it rests on and what it does **not** establish live in
`raven_train/notes/`. `dr@3.5` gets a few lines because it has zero batches behind it.

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
| `dr@2.2` | fixes and instruments at a score budget of 0; a committed salvage stops counting as answerless |
| `dr@2.3` | the instruments stop lying: no "ok" on an overflowed run, and every answerless run is attributed |
| `dr@2.4` | the corpus SERP snippet is restored and deduplicated by docid, per-config so the anchor cannot move |
| `dr@2.5` | terminal-answer shaping arrives off by default: a read-only observer, plus a clause asking for `<answer>` |
| `dr@2.6` | terminal-answer shaping is on by default, and every shipped arm pins it explicitly |
| `dr@2.7` | the reader path stops discarding pages it could have read - a resolver failure is no longer a refusal |
| `dr@2.8` | three per-arm, class-default-off retrieval changes, so the anchor cannot move |
| `dr@2.9` | a wrap-up for turns killed by the completion budget - **not** flow-gated, so it carries its own label |
| `dr@3.5` | **first rung under the launch convention** — dr@3.4's batch closed, so the label moved; zero batches behind it. One distribution change, and it is on **both arms** because it sits in the tool rather than behind a knob: `web_fetch` detects a page whose text encoding was lost upstream, refetches once with `x-no-cache`, and if still garbled flags the envelope `encoding_lost` with a warning not to quote it — so this label's fresh anchor pair is needed for a behavioural reason, not merely because `tree_sha` moved. Everything else is off by class default or read-only: `drFlow.fetchGate`'s release predicate is repaired (it was dead from the day it landed — a fence-wrapped body made `json.loads` raise on every real fetch, so the withhold never lifted and the knob's own mechanism counter sat at 0, meaning its verification would have *passed* by measuring something that could not happen), plus six groups of new observability fields. Merged alongside and inert on every measured arm: the `raven/acp/` subsystem |
| `dr@3.0` | the two observers that divide by the context window divide by the window the turn actually runs on, not the configured default — and `BudgetObserver` is installed only on the flow-off branch, which made the divergence arm-correlated |
| `dr@3.1` | the replay cache key gains `page`. Without it a page-2 request was answered from the page-1 entry cached earlier in the turn — and a replay scores as a *dry* search, so every false page-turn pushed the saturation ladder one step closer to `stop`: the broken rung fed the rung after it |
| `dr@3.2` | the context-window fallback doubles (65536 → 131072) and the window is resolved **once** rather than at each consumer. Also `harness_text.py`, which splits one predicate into two that deliberately disagree: permissive where a false positive costs one item of look-back, strict where it would destroy a real answer |
| `dr@3.3` | `drFlow.fetchGate`, off by class default: after 15 searches with no successful fetch, `web_search` leaves the iteration's tool schema until a page opens, so re-asking is not an available move. The two mechanisms already watching that shape fail differently — `fetch_floor` only appends a sentence, and the saturation stop refuses in the tool's *return value*, which the model can re-request (one turn accumulated 195 suppressed rows). Folded in: the reviewer and the salvage gate stop reading the *first* user message as the task |
| `dr@3.4` | the rest of the `turn_task` bug class — three flow consumers stop re-deriving "this turn's messages" from the whole assembled context, reading `AgentHookContext.turn_base` instead. All three are invisible on a single-turn benchmark, where the slice IS the whole list. Also per-query pagination depth, so a brand-new query is no longer sent to page 2. Folded in: the fixed three-section report template, its precedence over formatting instructions in the question, the per-turn reminder (`finalShape.reportReminder`, on) and the optional shape bar (`finalShape.reportBounce`, off); the closing-tag bar is waived when reasoning arrives out-of-band; `_MAX_HOOK_ROLLBACKS` 6 → 8; and the CLI exit path stops letting a live native runtime segfault the process (see `raven/cli/_exit.py`) |

Human-facing name for the current build is **Raven-X 3.4**; every superseded label stays
in code so historical results remain indexable, and the validator matches the BASE label
so a profile suffix such as `-futurex` cannot smuggle a retired one onto a new build.

### The context window is a fallback, not a claim

`agents.defaults.context_window_tokens` is **131072** as of `dr@3.2`, raised from 65536. Read
it as "what we assume when we cannot find out", not as "this model has a 128k window": it is
consulted only when `resolve_context_window(model)` cannot answer. Do not quote it as a
capability, and **never infer a run's working point from it** — when the catalog answers, the
catalog wins. That cost a whole batch: an arm declared 131072, ran at 1000000, and every
overflow counter read 0 because the mechanism under test could not fire. To mean the number,
set `agents.defaults.contextWindowAuthoritative: true` (`dr@3.5`, default false); to find out
what actually applied, read `effective_context_window` and `context_window_source`
(`pinned` / `catalog` / `config_fallback`) off `turn_end`, never the config.

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
