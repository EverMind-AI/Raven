---
name: topic-briefing
description: Research a topic from two angles in parallel (recent developments, key players) and merge the findings into one cited briefing for a chosen audience.
---

# topic-briefing

Say "topic briefing on <topic>" (or "brief me on <topic>") and two research
steps run in parallel -- one sweeping recent developments, one mapping the key
players and their positions. A writing step waits for both and merges them
into a single briefing with citations, shaped for the audience you name
(default: the team).

Params: topic (what to research), audience (who reads the briefing).
Steps: scan_recent + scan_players -> brief

```yaml playbook-spec
version: 1
mode: dag
confirm: true
triggers:
  keywords: [topic briefing, brief me on, briefing on]
params:
  topic:
    type: string
    required: true
    description: What should the briefing cover?
  audience:
    type: string
    default: the team
    description: Who is the briefing for?
nodes:
  - id: scan_recent
    subagent: raven
    promptTemplate: >-
      Research the most recent developments on "${params.topic}": what changed
      lately, what is announced or rumored, what the trade press says. Cite
      every claim. Write your findings to a markdown file and keep them
      factual -- no synthesis yet.
  - id: scan_players
    subagent: raven
    promptTemplate: >-
      Map the key players around "${params.topic}": who matters, what each
      one's position or offering is, and where they visibly disagree or
      compete. Cite every claim. Write your findings to a markdown file and
      keep them factual -- no synthesis yet.
  - id: brief
    subagent: raven
    dependsOn: [scan_recent, scan_players]
    promptTemplate: >-
      Merge the two research files into one briefing on "${params.topic}" for
      ${params.audience}: lead with the three things they must know, then
      recent developments, then the player landscape. Keep citations from the
      sources. Recent developments: {{ scan_recent.output_path }} -- player
      landscape: {{ scan_players.output_path }}
```
