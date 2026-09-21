---
name: {{name}}
description: {{description}}
---

# {{name}}

Say what this run is for, in a paragraph a person reads before approving it.
This half of the file is yours; only the fenced block below is read by the
machine.

Three things worth deciding before the first run, because the approval asks
about all of them and asks once:

* **how many rounds** -- `stop.maxRounds` below, and the caller can say a
  different number when they start it;
* **what ends it early** -- `stop.until`, a word the last role writes on a line
  of its own. Without one, the budget is the only ending;
* **where it works** -- `isolation`, which decides whether this run borrows your
  checkout or opens one of its own.

```yaml playbook-spec
version: 1
mode: stint
confirm: true
taskSummary: {{task_summary}}

triggers:
  # What a person might say that should bring this to mind. Matching is
  # substring and case-insensitive, and it decides visibility, never execution.
  keywords: [{{name}}]

stop:
  # Rounds this run may open. The ceiling is 99, and whoever starts it may say a
  # smaller number on the day.
  maxRounds: 3
  # A marker the last role writes on a line of its own to end the run early.
  # Matched as a whole line, so a role that mentions it mid-sentence does not
  # end anything. Delete this key and the budget is the only ending.
  until: NOTHING-LEFT

# Where the run works. Uncomment one; absent means `branch`.
#
#   branch    (default) your checkout, on a branch of the run's own. No second
#             copy of the repository, and the work lands where you will find it
#             (`git log stint/<id>`). The tree is the run's until it ends.
#   worktree  a checkout of its own, cut from HEAD. You keep using yours while
#             it runs; the price is a full copy of the repository per run.
#   none      your checkout, your branch, no commits. Legal only where no role
#             is held to its paths -- undoing a stray write there would undo
#             your own uncommitted work with it.
#
# isolation: branch

# Files that carry between rounds. Every round is a fresh conversation for every
# role, so nothing carries that is not written down. Delete the section if this
# run needs no memory of itself.
memory:
  - path: JOURNAL.md
    append: true
    recentRounds: 2
    maxChars: 16000

# Real commands a role's work is measured by -- the one signal in a round that
# no model produced. They run on the machine the stint runs on, every round, and
# the approval prints each one in full. A run with nothing to check (most
# research loops) deletes this section.
#
# Declare a check by what it proves. This file travels, and the command that
# proves it differs per project, so the project answers once with
# `raven playbook stint check set <playbook> <name> --run "..."` and the answer
# is kept in its `.stint/checks.json`. A `run:` in place of the description pins
# one command for every project this file is ever run on -- fine for a check
# that is the same everywhere, wrong for a build.
#
# verify:
#   - name: build
#     description: the project builds from a clean tree
#     timeoutSec: 300

roles:
  # One node of the round per role, in the order their dependencies imply.
  #
  # `owns` restricts rather than grants: what a role writes outside its own
  # paths is undone by putting the tree back, and a copy is kept under
  # `violations/`. A role that declares nothing is held to nothing.
  #
  # Two roles with no dependency path between them, where either is held to its
  # paths, are refused at load: they would share one tree and the first judged
  # would be graded against both. Order them, or drop the declarations.
  - as: worker
    name: Raven
    nodeSummary: do this round's work
    owns:
      - "reports/work_{NN}.md"
    promptTemplate: |
      Say what this role does with a round. `{NN}` in a path is this round's
      number, so `reports/work_{NN}.md` is yours and yours alone.

      {{round.journal}}

      {{round.guard}}

  - as: reviewer
    name: Raven
    dependsOn: [worker]
    nodeSummary: judge what the worker left
    owns:
      - "reports/review_{NN}.md"
    promptTemplate: |
      Say what this role looks for, and what it writes down.

      ## What the worker says it did

      {{worker.output}}

      {{round.guard}}
```
