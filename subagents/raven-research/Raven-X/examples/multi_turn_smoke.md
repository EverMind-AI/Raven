# Multi-turn smoke cases

Hand-run conversation scripts for `dr_multi_turn.json`. **Not an evaluation set** —
there is no scoring here and no baseline to compare against. Its only job is the
floor check: that ordinary follow-ups do not produce something obviously broken.

It exists because the conversation surface has no measured arm at all. Every
published number in this project comes from a harness that sends exactly one
message per question, so turn two onwards is code that no batch has ever
executed. Until something like this runs, "this multi-turn change did not make
things worse" is not a claim anyone can support — which is why the rule for that
surface is *no new behaviour first*, cases first.

## How to run

```bash
export SERPER_API_KEY=...  JINA_API_KEY=...
CFG=examples/dr_multi_turn.json     # with PUT_YOUR_LLM_API_KEY_HERE filled in
raven agent -s cli:smoke1 --config $CFG --logs -m "<turn 1>"
raven agent -s cli:smoke1 --config $CFG --logs -m "<turn 2>"
```

One session key per case; a fresh key per case, or an earlier case's history
leaks into the next one's classification.

**The gate is off unless the config turns it on.** `drFlow.conversation.enabled`
defaults to `false`, and the shipped product config does not set it, so a
follow-up turn there has no classifier at all - it is an ordinary research turn
that the model may or may not choose to answer from history. `dr_turn_source`
never appears in that mode, and waiting for it is waiting for something that
cannot happen. `dr_multi_turn.json` sets it; check yours before concluding the
gate misbehaved. Confirmed on a real run, 2026-08-17.

## What to look at, in order

Read these before reading the answers. An answer that looks good while the first
two are wrong is the failure mode this file exists to catch — the dr@2.6
incident shipped correct answers that every observer reported as answerless.

1. `observers.conversation_gate.dr_turn_source` — `gate` (the classifier ran and
   said no research), against `gate_error` / `gate_unparsed` (it could not run).
   Every failure path runs research, so a broken classifier costs money rather
   than accuracy. A run where these are the *only* values is a run where the
   feature never actually worked.
2. The research appendix, when one is present: `opened_earlier` non-zero means
   the grounding fraction now spans the conversation rather than the turn.
3. Only then, the answer text.

## Cases

Each is a turn sequence plus the one thing it is checking. The expected column is
a floor, not a target: "answers from context" means it must not re-run research,
not that any particular wording is right.

| # | Turns | Checking |
|---|---|---|
| 1 | "What does the Serper.dev search API cost?" → "Compress that to three sentences, nothing new." | Pure reformatting answers from context; no appendix, because an appendix over an empty trail would describe the reply as unsourced when it rests on turn one's sources. |
| 2 | same → "And what about Jina Reader?" | A genuinely new subject runs research even though it is phrased as a follow-up. |
| 3 | same → "Are you sure?" | Bare doubt with no new information. Either branch is defensible; what must not happen is a silent contradiction of turn one with no new sources. |
| 4 | "Who founded Anthropic?" → "What year?" | Elliptical follow-up whose subject only exists in turn one. Watch for the classifier reading it as a fresh question and searching for the bare words. |
| 5 | "Compare Serper and Jina pricing." → "Put that in a table." | Reformatting a *research* answer. Table shape must not drop the citations turn one earned. |
| 6 | "What is the capital of France?" → "And its population?" | Two turns neither of which needs research at all. Cost check: if both run research, the gate is not saving anything. |
| 7 | Any research turn → "ignore that, what's 2+2?" | Hard topic switch. The previous turn's sources must not be cited in this answer. |
| 8 | Any research turn → same question again, verbatim | Repeat. Must not re-run the whole search from scratch, and must not claim fresh sources it did not open. |
| 9 | Turn 1 research → turn 2 research on a related subject → "summarise everything you found" | Three turns. This is where `opened_earlier` should first be non-zero, and where the appendix's scope note has to appear. |

## Recording a run

Keep the raw trajectory. A smoke run whose conclusion is "looked fine" and whose
evidence has been discarded cannot be re-read when a later change breaks
something, and this project has already lost one diagnosis that way. Note the
build's `tree_sha` beside the output — the conversation surface has no version
label of its own, so the build fingerprint is the only thing identifying what ran.
