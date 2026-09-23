# What `mode: stint` has to survive

The scenarios a multi-round run is accepted against, and where each one stands.
Written 2026-09-18 after a five-round plan finished cleanly and then could not be
continued -- the happy path had been tested and the shape of the state machine
around it had not.

Status is one of **held** (a test fails if it stops being true), **open** (agreed,
not built), or **known gap** (true today, deliberately).

## The state machine

A plan is `running`, `paused`, `stopped`, `finished` or `interrupted`, and the
verbs are start, advance, stop, pause, resume, extend, answer. Most of what broke
was in the cells nobody had visited.

| # | Scenario | Status |
|---|---|---|
| 1 | Budget spent, then `extend`: the next round opens on the same checkout and branch | held |
| 2 | `pause` mid-plan, then `resume`: the round after the paused one opens | held -- was broken (`resume` re-ran the finished round and reported nothing left to run) |
| 3 | `stop`, then `extend`: allowed, and the plan runs again | held |
| 4 | Host killed mid-round, then `resume`: finished roles are named, not re-run | held |
| 5 | Host killed mid-round, then `extend`: the reply does not promise a round in flight | held -- was broken |
| 6 | `extend` twice in a row | held (by 1, which leaves the plan in the state 1 starts from) |
| 7 | `extend` after the checkout was deleted: refused, and says why | held |
| 8 | `answer` a question, then `extend`: the answer reaches the new round | held |
| 9 | A plan ends, a second plan starts: the second cuts from the project's HEAD and does *not* carry the first's work | known gap -- the plan's closing summary now names the branch and offers `extend`, but nothing refuses the second plan |
| 10 | `stop` A, start B, then `extend` A: refused, B is named | held -- was broken (two live plans on one repository) |
| 11 | `extend` while a round really is in flight: budget raised, no round opened | held |
| 12 | Two windows `resume` the same plan at once | open |

## Budget and stopping early

| # | Scenario | Status |
|---|---|---|
| 13 | "run it for 3 rounds" reaches the plan, and the approval text says 3 | held |
| 14 | No number said: the playbook's budget, and `DEFAULT_MAX_ROUNDS` where it says nothing either | held |
| 15 | Past `MAX_ROUNDS`, or zero, or negative: refused, nothing written | held (both `start` and `extend`) |
| 16 | The last role writes `stop.until` on a line of its own: the plan ends that round | held |
| 17 | A role *mentions* the marker mid-sentence: the plan does not end | held -- the marker is matched as a whole line |
| 18 | A role nothing waits on is the only one told the marker | held |
| 19 | A playbook with no `until` says nothing to anyone about ending early | held |
| 20 | `resume` onto a round past the budget: refused, and points at `extend` | held |

## Boundaries and checks

The layers that were built first, kept here so the list is the whole contract.

| # | Scenario | Status |
|---|---|---|
| 21 | A role writes outside its paths: undone, original kept under `violations/`, an undo commit in git | held |
| 22 | A check fails: handed back to the role, at most twice, then recorded and the round moves on with downstream roles running | held |
| 23 | A check that needs a screen on a machine with none: skipped, not failed | held |
| 24 | A playbook that declares ownership over a directory that is not a repository: refused at start | held |
| 25 | `enforce.read: hard` | **refused at load**, because the guard text says reads are refused at the tool gate and nothing implements that refusal. Said and not done is worse than unsaid, so the grade is not available until the gate is. The wording stays in `rounds_prompt` for whoever wires it |

## More than one process, more than one surface

| # | Scenario | Status |
|---|---|---|
| 26 | A plan started in the TUI is found, read and extended from a terminal | held -- was broken, see `2026-09-17-session-key-resolution.md` |
| 27 | The web UI stops a plan the TUI started | open (the handler searches rather than derives, so it should hold; unverified) |
| 28 | The gateway dies mid-plan: something says the plan is not being advanced | held -- every read path (`stints list`, `stints get`, the RPC list and get) calls `mark_adrift` first, so a record whose holder is gone reads as interrupted, and `stints sweep` takes those up |

## Starting from nothing

A repository that has never run a plan. Seven commands before, and five of them
belonged to a component being removed.

| # | Scenario | Status |
|---|---|---|
| 29 | `rounds` is in the library on a fresh install, with nothing copied | held -- it ships under `raven/playbook/builtin/` |
| 30 | A playbook that names a layout gets one written when the project lacks it | held |
| 31 | The layout reaches the checkout the rounds actually run in | held -- a worktree is cut from `HEAD` and setup does not commit, so without the carry every round failed on `{{ref:}}` for the whole budget |
| 32 | Nothing is committed to the person's branch; they get untracked files and the choice | held |
| 33 | A directory that is not a repository is refused before anything is written | held |
| 34 | A project with no document to plan from is refused, and told where to put one | held |
| 35 | The specification is picked by name **and** content: a stub called `SPEC.md` is not one | held |
| 36 | The pick is shown in the approval, because a silent wrong one misdirects every round | held |
| 37 | Laying out twice writes nothing and overwrites no edit | held |
| 38 | A recipe name the build does not have is refused rather than guessed | held |
| 39 | The backlog is written without anyone running a command | **open** -- specified, not built; see below |

## What a person sees

| # | Scenario | Status |
|---|---|---|
| 40 | The approval names the round budget, every command, the branch and the stopping rule | held -- it used to list three node ids and nothing else |
| 41 | The question a caller supplies cannot come from the caller's own JSON | held -- it is a callable, and a string arriving as one is ignored |
| 42 | An argument the tool schema never offered does not reach `execute` | held |
| 43 | The web library card for a `mode: stint` playbook draws its roles | held |
| 44 | The web detail page shows roles, what each may write, the check commands and the stopping rule | held -- it rendered an empty prompt box before |
| 45 | The plans list and one plan's detail | held (list / get / stop / answer) |
| 46 | Pausing, resuming or extending a plan from the web UI | open -- terminal only |
| 47 | The TUI shows a round's graph as it runs | open -- progress events carry `stint_id` and `round_index`; no reader uses them yet |

## The backlog step, specified and not built

Setup lays the project out and picks the specification. It does not write the
backlog, so a project reaching its first round has an empty one and the planner
has nothing to pick from.

The shape it should take, decided and not implemented: **a round of the plan**,
not a blocking call inside `start`. `start` has to return a receipt at once --
that is the whole reason the main conversation is free -- and a planning turn
takes minutes. So the plan opens at round zero with a one-node graph built from
`bootstrap.plan_prompt`, `advance` moves it to round one when that lands, and
the budget does not count it. It is then reported, interruptible and resumable
like every other round, and a backlog somebody dislikes costs one round rather
than thirty.

Until then a person runs the planning step themselves, and the approval says
how many tasks are in the backlog so an empty one is visible before the run.

## End to end

The eight in the PRD, plus what this list added:

1. One sentence starts it; the number comes back at once and the conversation is free
2. Rounds go 1..N, each with its own run id
3. A deliberate stray write is undone, quarantined and recorded
4. A check that fails once is handed back and passes on the retry
5. A check that always fails is recorded and the round finishes anyway
6. Killing the host mid-round loses no finished role
7. One notification, one dispatch charged
8. Stopping mid-plan ends after the current round and says nothing further
9. "Three rounds" runs three rounds, and the approval says three
10. Spent, extended by three, continues on the same tree with the journal intact
11. The reviewer writes the marker and the plan ends that round
12. Paused and resumed continues rather than repeating
13. One repository holds one live plan, whichever verb tried to make a second
14. A rounds playbook is legible in the web UI before it is approved
15. A fresh repository goes from "run rounds" to a first round without the
    person running a setup command, or is told the one thing only they can give

## Roles running at once

Nothing had to be built for a round to fan out: the graph runner already runs
independent nodes concurrently, and `compile_round` passes `dependsOn` straight
through. Three developers off one planner is a legal round today.

What it did was delete their work. A plan has one checkout, and a role's writes
are measured as `touched_since(base)` -- `diff_names` plus `changed()`, which is
the whole tree's uncommitted state. Two roles at once are two sets of changes in
one tree, so the first to be judged is graded against both and reverts the
other's. Reproduced with two roles whose `owns` did not overlap, each writing
only what it owned:

```
dev-b's own file still there after dev-a was judged: False
violations recorded against dev-a: ['dev-a wrote 1 path(s) it may not write: src/b/work.py']
```

Nothing errors. It reads as a role that would not stay in its lane.

**Refused where it is declared** (`_concurrent_and_enforced`): two roles with no
dependency path between them, where either is held to its paths, do not load.
The message names both ways out -- order them, or say their boundaries are not
enforced. Concurrency itself is untouched; the refusal is about the measurement.

**And a plan may not take the host** (`PLAN_MAX_PARALLEL = 2`): a plan is charged
one dispatch for its whole life, so without a second limit that one charge buys
unbounded concurrency. A round now takes its own limit first and the host's
second, in that order, so a node waiting for its plan's slot is not holding
one everybody else needs.

### What would make it work: a checkout per concurrent role

Not built. The design, so the next person does not start from the reproduction:

1. **A worktree per role in a fan, cut from the round's starting commit.** The
   plan already opens one for itself (`_open_tree`); this is the same call, once
   per role, on a branch named for the round and the role.
2. **Measurement becomes per-tree again, and therefore correct.** Each role's
   `touched_since` sees only its own tree. No change to `enforce` at all -- the
   bug is that the tree is shared, not that the grading is wrong.
3. **Each role merges itself in when it is judged, and resolves what it hits.**
   Not a merge at the end of the fan, and not a role that does the merging.

   The first of the fan to finish merges into an empty round branch and cannot
   conflict; the second merges onto the first and may; the third onto both. A
   conflict therefore lands on the role whose work caused it, against work
   already in the branch -- which is the side that role has the context for.
   The last one to finish would have had the worst view of all three.

   It needs no new channel. A conflict is a handback: `Verdict.follow_up` is
   how a judge already tells a node what to do differently, `maxHandbacks`
   already bounds the attempts, and a role that cannot reconcile in two tries
   is recorded and the round moves on -- the same treatment a failing check
   gets. This is what a person does by hand, in the order they do it.

   Two things it does not solve, and both have to be said:
   * **Merging in the judge is a write to one branch from concurrent nodes.**
     Two roles can be judged at once, so the merge needs a lock. The judge is
     the natural place for it, but it has to be explicit -- nothing serialises
     it today.
   * **A clean merge is not a correct one.** Two roles appending to different
     parts of `FIXLOG.md` merge without conflict and may leave two entries that
     contradict each other. No handback catches that; only Verifier reads for it. So
     a merge that succeeded must not be reported as a round that went well.
4. **`artifacts` paths need a rule.** They are ungraded by design because a
   build writes them; in separate trees each role builds its own, and merging
   them is meaningless. They should be excluded from the merge rather than
   conflict.
5. **Cost.** Three worktrees per round, times thirty rounds, on a repository
   that may be large. They are cheap (`git worktree` shares the object store)
   but they are not free, and they have to be removed when the round ends --
   the plan's own tree already leaks on an interrupted run, which this would
   multiply.

Considered and rejected: **a `merge` role depending on the fan.** It is
expressible today and needs nothing built, which is what makes it tempting. It
fails on ownership: it has to write files owned by every role in the fan, and
one path has one owner, so either it owns everything and the fan owns nothing
-- dissolving the grid the whole feature exists to enforce -- or it is not a
role. The plan already writes outside everyone's ownership when enforcement
reverts a stray write (`revert(round-NN)`); a merge belongs in that category,
as machinery, not as somebody's turn.

Also rejected: **telling the last role to finish that it is the last.** Which
role that is is a runtime fact no role can see, so it would need a channel that
exists for nothing else -- and the role it names is the one with the least
context on the conflict.

Until then the shipped shape is a chain, which is what `rounds` declares
and what the validation now holds every enforced playbook to.

## What went offline with the loop this replaced, and what did not come with it

The predecessor loop is deleted in this change. Three of its capabilities are
worth naming, because deleting them silently would leave somebody rediscovering
them as bugs.

**Per-role denial of one server's tools.** The reviewer role ran against the
game engine with every mutating tool refused at the gate, so a role judging
work could not quietly fix it. A charter narrows *which tools* a node may call;
this narrowed *one server's* tools, which is not the same thing and has no
equivalent here yet. This is the one capability that goes without a
replacement.

**Several developers at once, each in its own checkout.** Ruled out on purpose,
not lost: a role that fanned out would be nested expansion, and what a plan
dispatches has to be knowable when it is approved. A project that needs three
developers declares three roles, which are visible in the playbook before
anything runs. See "Roles running at once" above for the checkout-per-role
design that would make that real.

**Per-role compaction inside one turn.** Ruled out for the same reason it was
needed there: that loop's role could run for hours, and a round's role runs
once and hands off through files. A role whose single turn will not fit is a
role whose task is too big for one round.

**The confirm gate has no teeth here yet.** `raven playbook stint confirm` records a
person's word that the plan is the plan, and `roster.ready` is the check that
reads it -- but nothing in the rounds path calls `roster.ready`, so today the
flag is recorded and not enforced. Either the plan's setup step asks it before
round one, or the verb should stop implying a gate.

## The vocabulary, and the follow-up it is owed

Decided 2026-09-18: the names below ship as they are, and the rename is a
follow-up rather than part of this change. Written down because the problem is
real and a later reader should not have to rediscover it.

**`plan` means three things.** Two of them are user-facing and one is not:

| | Where | What it means |
|---|---|---|
| 1 | `executor.ExecutionPlan` | What the executor answers with, for *every* mode. Predates this work and never reaches a user |
| 2 | `rounds/plan.py`, `stint_id`, `raven playbook plan ...` | One multi-round run |
| 3 | `bootstrap.PLAN_SENTINEL`, `plan_prompt`, "the plan is the plan" | The backlog a person confirmed |

2 and 3 collide where a person can see both: `raven playbook stints get <id>`
reads a run, and the standing orders say "read the plan before the first round"
about a file. 1 only collides in the source.

**`rounds` is a plural where its siblings are singular.** `mode: dag` and
`mode: prompt` name a shape; `mode: stint` names a repetition, and the same
word then has to serve as the type, the CLI family (`raven playbook stint`) and the unit
inside a run (`round_index`, `stop.maxRounds`).

**The shape a rename would take.** One noun, its singular naming one run and
replacing `plan`, its plural naming the command family and replacing the
top-level `raven playbook stint`; `round` and `stage` keep their present meanings as the
units inside. Candidates measured against the tree for collisions: `stint` (0),
`arc` (0), `mission` (0), `tour` (8, all incidental prose); ruled out for real
collisions: `crew` (the landing page's illustrations), `cycle` (import cycles),
`shift` (the config schema), `campaign` (the ops tree).

**When it happens, the project directory follows the noun.** `.stint/` is named
for the mode, so a mode under another name leaves it stranded. It is the one
piece that is project-level rather than run-level, which is the argument for
leaving it out of the rename -- and the reason to decide it explicitly rather
than by default.

**Cost, measured now so it is not guessed later.** `stint_id` 182 uses across 17
files, `StintRecord` 56, `StintStore` 43, `StintRef` 20, `StintDriver` 27, plus
the OpenRPC document, the mirrored models, two generated clients, the i18n keys
and the suites. Mechanical, wide, and cheaper before this ships than after.

## Taking up a stint whose host is gone

Closed 2026-09-18. The gap it closes: a round runs as a task inside the host
process, so killing the host kills the round mid-way and nothing is written on
the way out. The record went on saying `running`, `stints list` reported a
corpse as work in progress, and a person waited for a notification nobody was
going to send. `adrift` and `sweep` existed and had no caller, because the only
liveness signal was `active_run_ids` -- this process's own memory, which says
"not mine" and was being read as "dead".

**The signal is now in the file.** `StintRecord.touched_at_ms` is stamped on
every write and moved by a beat (`HEARTBEAT_EVERY_SEC`, 60s) for as long as this
process holds a round. The beat starts when a round is dispatched and is
cancelled when it hands over, so it stops exactly when the claim stops being
true -- and dies with the process, which is the case it exists for. A stamp
older than `STALE_AFTER_SEC` (300s, five beats) means the holder is gone, and
any process can read that.

**Marking is separated from taking up, and both now have a caller.** The read
paths -- `stints list` and `stints get`, on the CLI and over RPC -- mark on the
way past, so the record stops lying wherever a person looks. Taking one up again
spends money and hours, so it stays a verb: `stints resume <id>` for one,
`stints sweep` for every stint here whose holder is gone.

**Two guards came with it.** `mark_adrift` takes the stints the caller is
holding and checks them before the write rather than filtering them out of the
answer -- filtering afterwards would already have rewritten the record it meant
to protect. And `stints resume` refuses a round this process is working:
the other guards read the file, and the file cannot say that. Without it, taking
up a live round would put two graphs on one checkout, each judged against a
baseline the other is moving.

**What is still open.** The beat is per process, so two hosts sharing one agent
home still rely on `_already_running` to stop a second stint on one project, and
on the stamp to stop a second host taking up the first host's work. Neither is a
lock: a host that is paused (SIGSTOP, a laptop asleep) stops beating without
being gone, and after five minutes another host would call its stint adrift. A
person resuming it is what makes that a problem, and the refusal above only
covers the process that is actually holding the round.
