# Example configs

Four configs, copied from the ones the evaluation harness actually runs, with endpoints and
keys replaced by placeholders. Copy one, fill in the placeholders, pass it with `--config`.

| File | What it is | Needs |
|---|---|---|
| `dr_live_web.json` | **Start here.** DR Flow over the live web, one question per run. | LLM key, `SERPER_API_KEY`, `JINA_API_KEY` |
| `dr_multi_turn.json` | The same thing as a **conversation**: follow-ups decide for themselves whether they need new research. | same as above |
| `dr_pinned_corpus.json` | DR Flow over a fixed corpus instead of the live web. Reproduces our controlled measurements. | LLM endpoint, a corpus service on `:8765` |
| `anchor_flow_off.json` | The **measurement anchor** (`drFlow.enabled=false`). Not a product configuration — see README "Measurement anchor". | same as above |
| `everos_memory.json` | **Memory, not DR.** Wiring the EverOS backend so captured turns land. Recall ships off; one key turns it on. | LLM key, an EverOS service on `:8000` |

**Name the real gateway in `providers`, not `custom`.** The two OpenRouter configs use a
`providers.openrouter` slot; the two self-hosted ones use `custom`, which is the correct slot for an
unlabelled OpenAI-compatible endpoint. Pointing `custom` at a gateway the registry knows silently
gives up everything keyed on provider identity — prompt caching most of all, which on a measured
run here cost 4.3x. It is a config that works, returns correct answers, and is quietly expensive,
so nothing about the output says anything is wrong.

Placeholders to replace: `PUT_YOUR_LLM_API_KEY_HERE`, `YOUR_LLM_HOST`. Web-tool keys are read
from the environment (`SERPER_API_KEY` for search, `JINA_API_KEY` for fetch); they can also go
in the config under `tools.web.providers.serper.apiKey` and
`tools.web.providers.jina.apiKey`, but the environment
is preferred so a config file can be shared without carrying a secret.

The LLM key has **no environment fallback** — `providers.<name>.apiKey` must be present in the
config, or the run exits 1 with "No API key configured".

## The knobs, and which of them we have priced

`drFlow` is the single versioned carrier for behaviour. Everything below is on in the two DR
configs because that is the combination our published numbers were measured on.

| Knob | Why it is set that way |
|---|---|
| `version` **omitted** | Deliberate. The label is the key of our measurement ledger, so a superseded one with `enabled: true` is **rejected** by the validator — which means a pinned label in an example breaks the quickstart on the next bump, as `dr@2.5` did here. Omitting it inherits the current label and stays correct forever. Pin it only on an arm whose number you intend to publish. To reproduce a published reading, check out that reading's tag (`dr2.4-published`) rather than pinning its label on a newer build. |
| `toolsAllowlist: [web_search, web_fetch]` | An allowlist, not a denylist: a plugin can inject a tool a denylist never knew to name. Applied at assembly, so the model sees exactly two tools. |
| `forceFinalize` | An answerless terminal turn is salvaged rather than lost. |
| `spinBreaker` | Catches the model restarting its own research loop. |
| `fetchFloor` | Nudges when the agent keeps searching without ever opening a page. |
| `verify.strictRejectOnly` | The draft reviewer may only reject on a hard failure; it must not rewrite or blank an answer. |
| `digest.verbatimHeadChars: 800` | `web_fetch` returns a digest, keeping the first 800 characters verbatim. |
| `memory.backend: null` | Persistent user memory off, in all four DR configs. A memory backend carries content *across sessions*, which would make two runs of the same question non-independent. `dr_multi_turn.json` keeps it off for the same reason: what it adds is memory *within* one conversation, which is a different thing and does not need a backend. Note the field defaults to `"everos"`, so **omitting the block enables it** — the `null` is doing work. Memory has its own example; see below. |

Four knobs deserve a warning:

- **`thinkClosingTagRequired`** is `true` by default and set to **`false`** in
  `dr_live_web.json`. Match it to your model, and get this wrong and everything still
  *looks* fine. The flow treats a terminal turn carrying no `</think>` as having produced no
  answer, which is right for a served model whose template prefills the opening tag, and wrong
  for every model that does not emit think tags at all - including the
  `anthropic/claude-sonnet-5` this example ships with.

  Measured on a real run of this config before the fix: the model returned a correct, fully
  cited answer wrapped in `<answer>` tags, and the flow reported `answerless: true`,
  `answer_chars: 0`, `finalShape.form: "empty"`, and ran the salvage path three times against
  a problem that did not exist. The answer still reached the caller - an exemption passes it
  through - so the only symptom was in the observers, i.e. in exactly the numbers anyone would
  use to judge the run. Set it to `false` for a non-think model.


- **`finalShape`** is **on by default** since `dr@2.6`, and all four configs now write all four
  values out rather than inherit them. `record` adds a read-only observer that records a shaped
  form of the terminal answer beside the raw one — it touches nothing the model reads, so it
  cannot change a score. `requireMarker` appends a clause asking for an explicit `<answer>`
  marker; that one **does** change what the model reads, so an arm using it needs its own
  anchor pair and a prompt stamp (`scripts/stamp_dr_segment.py`).

  The three DR configs differ here on purpose. `dr_live_web.json` runs the shipped default
  (`requireMarker: true`). `dr_pinned_corpus.json` sets it **false**, which is what our
  measurement arm runs: that keeps its DR segment byte-identical to the published one
  (`593c46c416c3f4cf`), so a number you produce with it is comparable to a number we published.
  `dr_multi_turn.json` also sets it **false**. It is the only one of the three whose reason is
  not written down anywhere; if you are changing it, work out what a marker clause should mean
  on a follow-up turn before you flip it, rather than assuming it inherited the corpus arm's
  reason.

  Writing the values out is the point, not clutter. This block was previously absent from all
  the files and documented here as "absent, i.e. off"; when the default flipped, every one of
  them changed behaviour and **not one config file changed a character** — including this
  sentence, which was wrong for a while. An inherited default does not appear in the file that
  describes the run, so it can change what a run does without leaving a trace where anyone
  looks.


- **`search.includeSnippets`** is `true` in `dr_pinned_corpus.json` and **absent (false)** in
  `dr_live_web.json`. That asymmetry is deliberate. On a fixed corpus a snippet is a 300-char
  window around the query — a selection signal we measured and shipped per-arm in dr@2.4. On
  the live web a snippet is **answer-optimised by the search engine**, which is a different
  thing we have not priced. Turning it on for live web is untested, not recommended-against.
- **`disabledTools`** is what fences a corpus-pinned run, and it must contain `exec` and
  anything else that can start a subprocess. This is not theoretical: an upstream build we
  benchmarked reached 31 real web pages out of a pinned corpus using `exec` plus `curl`. Note
  that `toolsAllowlist` only applies when the flow is **on**, so for `anchor_flow_off.json`
  the denylist is the only fence there is.

## Wiring the memory backend (`everos_memory.json`)

The other four configs are DR runs with memory off. This one is the opposite: no flow, a real
backend. It runs against an [EverOS](https://github.com/EverMind-AI/EverOS) service on `:8000`
in the eager profile — every turn is written and promoted as it happens, so there is nothing to
run afterwards to see episodes and cases appear.

```bash
raven agent -w ./ws --config examples/everos_memory.json -s cli:demo -m "I work mostly in Rust."
raven agent -w ./ws --config examples/everos_memory.json -s cli:demo -m "And I dislike ORMs."
```

**Recall ships off** (`recall_enabled: false`). That is the deliberate default: recall changes
what the model reads, and an example that hands you a prompt-altering setting switched on is the
wrong default to copy. Turn it on with that one key when you want retrieval; the two `recall_*`
lanes below it are already set, so nothing else has to change.

Four things here are load-bearing, and three of them fail *silently* — an empty
`# Recalled memory` block and no error anywhere:

- **`recall_enabled: false` is a real mode, not just "off".** It keeps the after-turn write and
  takes the backend out of the context engine — the **store-only** profile. That is how a
  measurement arm captures trajectories while its prompt bytes stay identical to
  `memory.backend: null`, which is what lets the write path ride along without a
  `drFlow.version` bump.

- **The two identity pairs have to match** once recall is on. `memory.userId` / `memory.agentId`
  are the *read* side (what the host passes to `backend.recall`); the plugin slice's `user_id` /
  `agent_id` are the *write* side (stamped as the sender on every captured message). EverOS
  routes by sender, so a mismatch writes to one owner and reads from another. Both sides default
  to `"default"`, which is why setting neither works and setting one does not. Raven warns at
  startup when they diverge and recall is on.

- **With the DR flow on, recall is inert by default.** `drFlow.minimalContext` (default `true`)
  drops the `memory`, `skills` and `active_skills` segments, so `recall_enabled: true` under
  `drFlow.enabled: true` retrieves hits that never reach the prompt. This example sidesteps it by
  leaving the flow off; combining the two means setting `minimalContext: false`, which is a
  distribution change and needs its own `drFlow.version`.

- **`defer_extraction: false` is what makes this example self-contained.** In the deferred profile
  (`true`) `/add` only appends to a server-side buffer and *nothing* is derived until something
  asks: pass `--flush-skill-buffer` on the last run of a session, or promote out of band with
  `scripts/everos_flush_batch.py`. `memory.flushOnTaskEnd` does the same per run — and does
  nothing at all in the eager profile, which has nothing buffered to promote. A capture that is
  buffered but never promoted is indistinguishable from a backend that is not working.

`require_service: true` is set here on purpose: it turns a missing service into exit 1 instead of
a logged warning and a silently memory-less run. The default is `false`, which is right for a
measurement arm that must not die on an infra blip and wrong for a demo whose entire subject is
the backend.

**Authenticating to a remote service:** set `EVEROS_API_KEY` in the environment. The plugin's
`api_key` config key still works and takes precedence, but the environment is preferred for the
same reason as the web-tool keys — a config file can then be shared without carrying a secret.

## Running a conversation (`dr_multi_turn.json`)

Everything else here is one question, one run. Pass a session key and the same key again to
keep talking:

```bash
export SERPER_API_KEY=...  JINA_API_KEY=...
CFG=examples/dr_multi_turn.json     # with PUT_YOUR_LLM_API_KEY_HERE filled in

raven agent -s cli:demo --config $CFG --logs -m "What does the Serper.dev search API cost?"
raven agent -s cli:demo --config $CFG --logs -m "Compress that to three sentences, nothing new."
raven agent -s cli:demo --config $CFG --logs -m "And what about Jina Reader?"
```

Turn one runs research because `drFlow.enabled` says so. Turn two and three each get classified
first, and `--logs` shows the verdict when the answer comes from context:

```
conversation-gate: answering from context (gate) - asks to compress existing information
```

No line means the turn ran research. A turn answered from context does no searching and gets no
research appendix, because an appendix over an empty trail would describe the reply as
unsourced when it is resting on the previous turn's sources.

Two things worth watching across turns. `observers.conversation_gate.dr_turn_source` separates
"the classifier said no" from "the classifier could not run" (`gate` / `gate_error` /
`gate_unparsed`) — every failure runs research, so a broken classifier costs money rather than
accuracy, and you want to know which you are paying for. And `opened_earlier` in the appendix
counters is non-zero once a later turn cites a page an earlier turn opened: the
fabricated-citation check then spans the conversation rather than the turn, so its
`citation_grounding_rate` is not comparable with a single-turn run's.

The slot matters more here than anywhere else in this directory. A single-question run pays the
uncached prefix once; a conversation re-sends a growing prefix every turn, which is exactly what
caching exists for. See the note above the placeholder list.

## Reproducing an A/B

Every Flow claim we publish is a same-batch A-minus-B between a DR config and
`anchor_flow_off.json` over the same questions, scored per question and compared with a paired
test. The anchor is an instrument: it is deliberately not improved, because moving it
invalidates the historical reference frame. Do not read it as "Raven-X with the flow off is
our baseline product" — it is not a product at all.
