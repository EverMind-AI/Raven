---
name: deep-dive
description: Open-ended investigation of one question - a model composes the step graph per run from assembly guidance, then delivers a single cited answer.
---

# deep-dive

Say "deep dive into <question>" and the orchestrator designs a run for that
question: complementary research angles chosen from the question itself, a
data step when the answer hinges on numbers, and one final writing step that
synthesizes everything into a cited answer. Unlike topic-briefing, the step
graph is not fixed here -- this file carries assembly guidance, and the graph
is composed per run and validated like any other.

Params: question (what to investigate).

```yaml playbook-spec
version: 1
mode: prompt
confirm: true
triggers:
  keywords: [deep dive, dig into this]
params:
  question:
    type: string
    required: true
    description: What is the one question to investigate?
prompts: >-
  Compose a graph that answers "${params.question}" end to end. Pick 2-4
  research nodes covering complementary angles implied by the question (e.g.
  current state, causes, counter-arguments, precedents) -- each researches its
  angle with citations and writes findings to a file, no synthesis. Add one
  data node only if the answer hinges on quantitative claims that need
  checking. Finish with exactly one content node that depends on every other
  node, reads their output files, and writes the answer: verdict first, then
  the evidence for it, then what remains uncertain, citations kept. Prefer
  fewer, sharper angles over coverage for its own sake.
```
