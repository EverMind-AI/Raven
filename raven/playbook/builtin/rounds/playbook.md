---
name: rounds
description: push a project forward one round at a time, with a planner, a builder and a verifier
---

A planner, a builder and a verifier, pushing one project forward a round at
a time. Ships with Raven: say "run rounds" in the project, or
`raven playbook run rounds` from it.

What it needs from the project, and what happens when it is not there:

* a git repository. What a role writes outside its own paths is undone by
  putting the tree back, and there is nothing to put back without one, so a
  directory that is not a repository is refused rather than run unguarded;
* `.stint/` -- the standing orders each role reads (`planner.md`,
  `builder.md`, `verifier.md`), the specification they plan from, and the backlog
  they move work through. **Laid out for you** when it is missing, before the
  run is put to you for approval.

The standing orders are referenced rather than pasted (`{{ref:...}}`): a round
that inlined every rule into every prompt is how the reply ceiling was reached
the first time.

Where it works: this repository, on a branch of its own (`stint/<id>`), which is
the default `isolation`. The tree is the run's until it ends and your own branch
is left where it is; `git log stint/<id>` is where the work lands. A project that
would rather keep its checkout usable meanwhile adds `isolation: worktree`, and
pays a second copy of the repository for it.

What it does *not* carry, and why: no role runs in a tree of its own (the run has
one and the roles share it), and there is one builder rather than three.

```yaml playbook-spec
version: 1
mode: stint
confirm: true
# Laid out when the project does not have it -- see the note above.
setup: stint
taskSummary: push the project forward by one round of planning, work and review
triggers:
  keywords: [round, rounds, backlog, stint]

memory:
  - path: .stint/backlog.json
  - path: JOURNAL.md
    append: true
    recentRounds: 2
    maxChars: 16000

# Declared by what it proves, not by a command: this file travels, and the
# command that builds a Godot project is nothing on a Node one. The project
# answers once -- `raven playbook stint check set rounds build --run "..."`
# -- and the answer is kept in `.stint/checks.json`. A project the tree plainly
# affords a build for is answered for you; a fresh one is asked before the run.
verify:
  - name: build
    description: the project builds from a clean tree, and a build that cannot fail proves nothing
    timeoutSec: 300

# Every role below mirrors the frontmatter of the guard file it reads. Those
# files carry their own owns/appends/reads, so declaring something different
# here would give each role two rosters -- the one it is told and the one it is
# judged by. .stint/backlog.json and .stint/HUMAN_DECISIONS.md are artifacts
# rather than anyone's: a role reaches them through `raven playbook stint task` and
# `raven playbook stint ask`, and grading them by path would undo every legal move.

roles:
  - as: planner
    name: Raven-Code
    nodeSummary: pick this round's work
    owns:
      - "reports/brief_{NN}.md"
    reads:
      - .stint/SPEC.md
      - .stint/HUMAN_DECISIONS.md
      - .stint/FIXLOG.md
      - "reports/verify_{NN-1}.md"
    # Every output directory the layout's own guards name, and the two ledgers.
    # Held equal to `raven.stint.bootstrap.OUTPUT_DIRS` by a test: the guard
    # files are generated from that tuple and this list is written by hand, so
    # the two drifted the moment it grew -- and a Builder that wrote its replay
    # evidence where its standing orders said to had the work reverted by a list
    # it could not see.
    artifacts: &ledger
      - .stint/backlog.json
      - .stint/HUMAN_DECISIONS.md
      - "build/**"
      - "builds/**"
      - "dist/**"
      - "out/**"
      - "demo_outputs/**"
      - "replays/**"
    journalSection: Plan
    promptTemplate: |
      {{ref:.stint/planner.md}}

      ## Round {{round.index}}

      {{round.journal}}

      Assign what this round should do with `raven playbook stint task assign --role
      planner`, and write the brief to the report path named below as yours.

      {{round.guard}}

  - as: builder
    name: Raven-Code
    dependsOn: [planner]
    nodeSummary: do the assigned work
    owns:
      - .stint/AGENT_DECISIONS.md
      - .stint/FIXLOG.md
      - .stint/PLAYBOOK.md
      - "reports/round_{NN}.md"
      # Every directory the layout's guard may grant the Builder, not the one
      # this project happens to use: the guard file is written per project from
      # `raven.stint.bootstrap.SOURCE_DIRS` (a fresh project gets `src/`,
      # `project/` and `tools/`), and this row is what the Builder is judged
      # by. Held a superset by a test. Written narrower than the guard, a
      # Builder that put its checks under `tools/` where its orders said it
      # could had them undone as a stray write.
      - "src/**"
      - "project/**"
      - "lib/**"
      - "app/**"
      - "pkg/**"
      - "cmd/**"
      - "tools/**"
      - "scripts/**"
      - "assets/**"
    reads:
      - "reports/brief_{NN}.md"
      - .stint/SPEC.md
      - .stint/HUMAN_DECISIONS.md
    artifacts: *ledger
    journalSection: Build
    verifyAfter: [build]
    maxHandbacks: 2
    promptTemplate: |
      {{ref:.stint/builder.md}}

      ## What the planner assigned

      {{planner.output}}

      {{round.verify}}

      Move each task you finish with `raven playbook stint task implement <id> --role
      builder`.

      {{round.guard}}

  - as: verifier
    name: Raven-Code
    dependsOn: [builder]
    nodeSummary: judge what the builder left
    owns:
      - "reports/verify_{NN}.md"
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
    journalSection: Verifier
    promptTemplate: |
      {{ref:.stint/verifier.md}}

      ## What the builder says it did

      {{builder.output}}

      {{round.verify}}

      Give a verdict per task in review with `raven playbook stint task verdict <id>
      --role verifier --proven --evidence <path>` or `--not-proven --reason "..."`. A
      verdict with no evidence behind it is an opinion, not a verdict.

      {{round.guard}}

stop:
  maxRounds: 30
  until: NOTHING-LEFT
```
