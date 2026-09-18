---
name: game-rounds
description: push a game project forward one round at a time, with a planner, a developer and a reviewer
---

A planner, a developer and a reviewer, pushing one project forward a round at
a time. Ships with Raven: say "run game-rounds" in the project, or
`raven playbook run game-rounds` from it.

What it needs from the project, and what happens when it is not there:

* a git repository. What a role writes outside its own paths is undone by
  putting the tree back, and there is nothing to put back without one, so a
  directory that is not a repository is refused rather than run unguarded;
* `.stint/` -- the standing orders each role reads (`planner.md`,
  `developer.md`, `qa.md`), the specification they plan from, and the backlog
  they move work through. **Laid out for you** when it is missing, before the
  run is put to you for approval.

The standing orders are referenced rather than pasted (`{{ref:...}}`): a round
that inlined every rule into every prompt is how the reply ceiling was reached
the first time.

What it does *not* carry, and why: no role runs in a worktree of its own (the
plan has one, and the roles share it), and there is one developer rather than
three.

```yaml playbook-spec
version: 1
mode: stint
confirm: true
# Laid out when the project does not have it -- see the note above.
setup: stint
taskSummary: push the project forward by one round of planning, work and review
triggers:
  keywords: [game, round, backlog, rounds]

memory:
  - path: .stint/backlog.json
  - path: JOURNAL.md
    append: true
    recentRounds: 2
    maxChars: 16000

verify:
  - name: build
    run: "python3 -m compileall -q src"
    timeoutSec: 300

# Every role below mirrors the frontmatter of the guard file it reads. Those
# files carry their own owns/appends/reads, so declaring something different
# here would give each role two rosters -- the one it is told and the one it is
# judged by. .stint/backlog.json and .stint/HUMAN_DECISIONS.md are artifacts
# rather than anyone's: a role reaches them through `raven playbook stint task` and
# `raven playbook stint ask`, and grading them by path would undo every legal move.

roles:
  - as: planner
    name: Raven-Research
    nodeSummary: pick this round's work
    owns:
      - "reports/brief_{NN}.md"
    reads:
      - .stint/SPEC.md
      - .stint/HUMAN_DECISIONS.md
      - .stint/FIXLOG.md
      - "reports/qa_{NN-1}.md"
    artifacts: &ledger
      - .stint/backlog.json
      - .stint/HUMAN_DECISIONS.md
      - "build/**"
      - "dist/**"
      - "out/**"
      - "demo_outputs/**"
    journalSection: Plan
    promptTemplate: |
      {{ref:.stint/planner.md}}

      ## Round {{round.index}}

      {{round.journal}}

      Assign what this round should do with `raven playbook stint task assign --role
      planner`, and write the brief to the report path named below as yours.

      {{round.guard}}

  - as: developer
    name: Raven-Code
    dependsOn: [planner]
    nodeSummary: do the assigned work
    owns:
      - .stint/AGENT_DECISIONS.md
      - .stint/FIXLOG.md
      - .stint/PLAYBOOK.md
      - "reports/round_{NN}.md"
      - "src/**"
    reads:
      - "reports/brief_{NN}.md"
      - .stint/SPEC.md
      - .stint/HUMAN_DECISIONS.md
    artifacts: *ledger
    journalSection: Dev
    verifyAfter: [build]
    maxHandbacks: 2
    promptTemplate: |
      {{ref:.stint/developer.md}}

      ## What the planner assigned

      {{planner.output}}

      {{round.verify}}

      Move each task you finish with `raven playbook stint task implement <id> --role
      developer`.

      {{round.guard}}

  - as: qa
    name: Raven-Research
    dependsOn: [developer]
    nodeSummary: judge what the developer left
    owns:
      - "reports/qa_{NN}.md"
      - "reports/evidence/round_{NN}/**"
    appends:
      - .stint/FIXLOG.md
      - .stint/PLAYBOOK.md
    reads:
      - "reports/round_{NN}.md"
      - "reports/brief_{NN}.md"
      - .stint/SPEC.md
      - .stint/HUMAN_DECISIONS.md
    artifacts: *ledger
    journalSection: QA
    promptTemplate: |
      {{ref:.stint/qa.md}}

      ## What the developer says it did

      {{developer.output}}

      {{round.verify}}

      Give a verdict per task in review with `raven playbook stint task verdict <id>
      --role qa --proven --evidence <path>` or `--not-proven --reason "..."`. A
      verdict with no evidence behind it is an opinion, not a verdict.

      {{round.guard}}

stop:
  maxRounds: 30
  until: NOTHING-LEFT
```
