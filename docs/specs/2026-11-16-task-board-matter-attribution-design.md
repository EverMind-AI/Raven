# Task Board: one turn, several matters -- design

Status: accepted (owner, 2026-11-16); implementation plan
`docs/plans/2026-11-16-task-board-matter-attribution.md`. Extends `docs/specs/2026-10-29-task-board-design.md` (cited as "the board
spec"); where the two disagree, this one describes the intended change.

## Why

One host turn often moves several Task Groups at once: the user answers a question on one
matter and asks for work on another, or a delegated result comes back and the host also
merges a second PR and asks about a third. The post-turn update is supposed to write one
Progress Step per group the turn advanced. In practice it writes one step, on the group the
turn is "known" to be about, and the other matters' events either disappear or are written
into that one step's summary. A reader sees one task carrying several tasks' progress, and
the other tasks look idle.

Three things cause it.

1. **The fence.** A turn a delegated result opened, or whose message a Move on or an Answer
   named a group for, is confined to that group (unless a message the user sent while it ran
   joined it): the instruction says "give a step only to them" (`task_board_scope.md`), and
   `confine_update` drops every other step that neither places a dispatch nor asks a question,
   with every `groups` change on another group. Models follow the instruction: they fold the
   other matters into the owner's step, so the code seldom has a step left to drop.
2. **The model's habit.** Even unconfined, a single post-turn call tends to write one step per
   turn. How strongly depends on the model: `glm-5.3-flash` writes one step almost always;
   `deepseek-v4.1-flash` less so. A rule in the instruction ("one step per group the turn
   advanced") is read and not followed, and nothing checks it.
3. **The long window.** Once the fence is lifted, the model reads the whole conversation (a
   median of about 600K tokens on the replayed turns), attributes events of earlier turns to
   this one, and treats a matter the reply only mentions ("still running") as moved.

## Goals

- Every matter a turn moved gets its own step on its own group, whatever opened the turn.
- No matter's events are written into another matter's step.
- A matter the turn only mentions gets no step.
- Holds on the flash-class models Raven runs the host on, not only on the strongest one.
- The guarantees the fence existed for stay: a delegated result's group gets its step, every
  dispatch is placed, every question the reply asked lands on a step.

## Non-goals

- The page, the wire, recall and the reader's actions (Move on, Answer, Ignore) are unchanged.
- The question list appended to the instruction (`task_board_asked.md`) and the carry pass
  keep their rules.
- No change to how a step, a group or a Task is stored.

## Anti-goals

- A fix that buys recall with noise: a step for every group a reply names.
- A fix that costs a model call per group per turn.
- A rule only the model enforces. Every rule this design adds is checked in code.

## Approaches considered

All measured by replaying recorded post-turn calls (Evidence, below), on 36 hand-labelled
turns, four runs each. "Multi" is the share of multi-matter runs written exactly right (every
moved group stepped, no extra step); "single" the share of single-matter runs left without an
extra step.

| approach | what it does | glm multi | glm single | deepseek multi | deepseek single |
|---|---|---|---|---|---|
| today | fence on owner groups | 28% | 100% | 44% | 100% |
| hint only | lift the fence for Move on and Answer turns; name the picked group | 31% | 96% | not run | not run |
| A | lift every fence; code lists candidate groups; the update answers each in `matters`; refusals enforce it | 47% | 51% | not run | not run |
| A2 | A, plus a verbatim `evidence` quote for every "moved", checked against this turn's text | 54% | 65% | 69% | 97% |
| **A2 + D** | A2, with the post-turn window cut to this turn's own rows | **75%** | **99%** | **74%** | **99%** |

The hint-only row leaves delegated turns fenced, so their runs are today's.

Rejected:

- **Hint only** fixes Move on and Answer turns and leaves delegated results fenced; on glm the
  gain is within noise, because glm writes one step whether fenced or not.
- **Classify, then write per group** (one call to list moved matters, one call per matter to
  write its step): N+1 calls per turn, each re-reading the window, and the first call can omit
  a matter exactly as today's single call does.
- **Attribute at the source** (the host tags each dispatch and each reply section with a group
  id while it works): the strongest signal, but it changes `spawn` and `run_subagent_dag`
  schemas and the reply rules, costs tokens on every main-turn call, and still misses work done
  through `exec` or a question with no tag.
- **A alone**: the checklist ends omissions but invites a step for every listed group (glm
  wrote 63 extra steps over 72 single-matter runs).
- **A2 alone**: the quote check cuts that to 36; the rest is earlier turns' events read from
  the long window, which only D removes.

## Design

### 1. Candidate groups

Before the call, code lists the groups the turn may have moved -- its candidates:

- **Owners.** The group that placed the run whose result opened the turn (`owning_groups` over
  `TurnRequest.delegated`, read through lineage as today), or the group a Move on or an Answer
  picked. A turn can have several.
- **Named groups.** Every other group, open or completed, that the turn's own text names. The
  turn's text is its opening message when a user sent it -- the user's own words
  (`TurnRequest.text`), not the runtime-context block or the recalled-memory scratch the
  envelope carries -- every message the user sent while it ran, and its reply as the reader saw
  it (`_reply`: assistant text and what the `message` tool
  sent). A delegated result's own body is left out of this search: a report restates its
  brief, and a brief can name neighbouring matters the turn did not touch.

A group is named when the turn's text contains its id, the id of a Task its steps link (the
`<id>` of a `spawn:<id>` or `dag:<id>` handle), or one of its title's anchors. An anchor is a
token in the title that identifies rather than describes: an issue or PR reference (`#1234`),
a code span, a file or path name, a version, a number of three or more digits. Plain words in
a title are not anchors: "fix", "review" and "login" name half the board.

Owners come first, then named groups in board order. On the labelled turns, a list built from
ids and `#NNN` references alone held every existing group that had to get a step, with about
three candidates per turn and at most five; the other anchor kinds are this design's
generalisation (Decisions, 1).

### 2. The instruction

The scope paragraphs (`task_board_scope.md`, `task_board_scope_mid_turn.md`) are replaced by
one paragraph, `task_board_matters.md` (en and zh), rendered when the turn has at least one
candidate, and preceded, when the turn has owners, by one line from
`task_board_matters_owners.md`: "This turn opened on {owners}: the group that placed the run
whose result opened it, or the one the user picked on the board for its message." The two are
joined by a newline, so neither language needs spacing rules. Ids only, as today: a title is
text an earlier call wrote.

> This turn touched these groups: {groups}. Answer every one of them in `matters`, one entry
> apiece. Most groups a turn names are
> only mentioned: answer `{"group": "<id>", "moved": false}`, and give it no step. Answer
> `{"group": "<id>", "moved": true, "evidence": "<quote>"}` only when this turn itself did
> something on the matter, decided something about it, learned something new about it or asked
> the user about it, with `evidence` quoting word for word the words of this turn's message or
> reply that say so, and give it its own step. A run still going, a status the board already
> holds, a question an earlier turn already asked, or work done in an earlier turn is not
> movement. Every step on an existing group needs
> such a `matters` entry, a group not listed here included. Never write one matter's events
> into another matter's step.

`{groups}` lists the owners first, then the named groups. The paragraph shows an entry's
shape; the example at the end of `task_board_update.md`
keeps its new-group step, which needs no entry.

A turn with no candidate gets no paragraph, and its update is asked and checked as today:
`matters` is neither asked for nor required.

### 3. The `matters` field

`task_board`'s schema gains `matters`: an array of objects with `group` (string), `moved`
(boolean) and `evidence` (string). All three are declared: a provider that builds the call's
JSON from the model's XML by the schema (MiniMax) cannot tell a list or a boolean from text
otherwise (#1320). `matters` is read by the post-turn call only; it is not stored on the board,
and recall never shows it. The schema's increment on every host call is pinned beside
`task_board`'s current one in `tests/test_agent_loop_token_budget.py`.

### 4. The rules

For a turn with at least one candidate, checked with the board's own rules before anything is
written; every fault is named in one refusal, through the existing loop (`MAX_ATTEMPTS`,
`_refusal`):

1. Every candidate has a `matters` entry.
   `matters: answer g_x -- did the turn move it?`
2. A group answered `moved: true` has a step.
   `matters says the turn moved g_x, but no step is on it`
3. A step on an existing group has a `matters` entry with `moved: true`, listed or not.
   `g_x has a step, but matters does not say the turn moved it; ...`
4. Each `evidence` is found in the turn's text (section 1), the delegated result included this
   time: the result is this turn's news about its own group.
   `matters: the evidence for g_x is not in this turn's message or reply; quote this turn's own words, or answer moved: false`

The quote check normalises both sides the same way: a markdown link read as its text,
markdown emphasis and code backticks removed, whitespace collapsed, case folded. A quote may elide with `...` or `…`; each piece
of at least four characters must be found. Paraphrase, two sentences stitched together and a
sentence from an earlier turn all fail, and the refusal says how to fix it.

A step that opens a new group (`new_title`) needs no `matters` entry: it has no id to answer
for, and the placement rules already cover the work it carries. When every attempt is refused,
`salvage` writes the last update as today; faults in `matters` alone never stop a salvage.

### 5. No fence

`confine_update` and the scope paragraphs go. Owners stop limiting what a turn may write; they
become the first candidates. A delegated result's group still gets its step, now because the
result is this turn's movement on it and the rules demand a step for every moved candidate.

### 6. The window (D)

The post-turn call no longer sends the turn's whole window. It sends:

- the system message and the tools array the turn's last call sent, unchanged;
- the turn's opening message, as the turn's log holds it -- the original even when a window
  pass replaced it with a summary;
- the turn's own rows as the window last sent them (an elided body stays elided), mid-turn
  messages included;
- the instruction.

`TurnLog.cut_to_turn(window)` builds it: the log stamps every row the turn adds, so the
window's first stamped row is where the turn's own rows start.

Everything before the opening message is left out. The board record in the instruction already
gives every open group's title, status, summary and last two steps' titles, and every
completed group's title: the history a matter needs. The carry pass continues from this
exchange, as today, so it is short too.

Size, cost and time, measured on the replayed turns:

- Today's call sent a median of about 600K prompt tokens (367K-690K), about 93% of them cache
  reads. A2 + D sends about 26K (19K-41K), two thirds of it the system message and tools, which
  the turn's own calls share as a cached prefix.
- Billed per update on OpenRouter, retries included (median): glm $0.029 -> $0.0045, deepseek
  $0.0034 -> $0.0009. On a provider that counts every token against a plan quota, the call
  shrinks about twenty-fold.
- Time goes the other way on glm: with the short window it reasons before it answers (median
  3,062 reasoning tokens; none today), and an update takes a median 76 s instead of 22 s.
  deepseek barely moves (12 s -> 14 s). The board settles that much later; nothing waits on it.

### 7. Observability

Each update logs, at info, the candidate count and how many were answered moved; each refusal
is logged with its faults, as today. The audit record of the call already holds the arguments.

## Evidence

Replays of recorded post-turn calls from one long host session (Nov 9-14): the recorded
system message, tools and window, with only the instruction (and, for D, the window) changed,
sent through Raven's own provider and the runner's own retry loop; every update checked by
`apply_update` against the board the record showed. 36 turns were labelled by hand with the
groups that had to get a step: 24 opened by an Answer or a Move on (10 multi-matter, 14 single)
and 12 opened by a delegated result (8 multi-matter, 4 single), each run four times per arm.
Models: `z-ai/glm-5.3-flash` (Z.AI) and `deepseek/deepseek-v4.1-flash-1017` (DeepInfra, chat
route; #1325 explains the route), both through OpenRouter. The replay's candidate list used
ids and `#NNN` references, and its quote check read the opening message and the assistant's
text; mid-turn messages, the `message` tool's words and the other anchor kinds are this
design's additions.

Results, A2 + D against today:

| | glm today | glm A2+D | deepseek today | deepseek A2+D |
|---|---|---|---|---|
| multi-matter runs exactly right | 20/72 | 54/72 | 32/72 | 53/72 |
| groups that had to get a step and did | 25/76 | 66/76 | 38/76 | 63/76 |
| single-matter runs with no extra step | 72/72 | 71/72 | 72/72 | 71/72 |
| follow-up calls after a refusal | 95 | 59 | 79 | 127 |

Three labels were corrected after reading the extra steps against the turns: one
"single-matter" turn asked about reviews of two other authors' PRs (now a multi-matter turn
needing a new group), and two turns reported news on another matter (that group is optional).
Under the uncorrected labels A2 + D's single-matter result is 69/76 (glm) and 71/76 (deepseek);
the conclusion does not change.

What is left:

- **A question or proposal about another matter, made in passing.** Four of the multi-matter
  turns end with the reply asking whether to merge, resolve or change something on a second
  matter; the model often answers that matter `moved: false` and leaves it unstepped. In three
  of the four the sentence does not name the matter, so routing questions by anchor would not
  catch them.
- **A turn that reports on several review groups at once.** One turn accounts for 11 of glm's
  15 extra steps on multi-matter runs; the label holds one moved group, the models step two to
  four. That label was not re-adjudicated.

Not measured: MiniMax (its plan quota was spent during the first replay round), and turns a
plain user message opened (no owner; they get the checklist when they name a group).

## Costs and risks

- **Retries.** The quote check refuses paraphrase. On deepseek follow-up calls rose by about
  60% (79 -> 127 over 216 updates); on glm they fell (95 -> 59). Each one is short.
- **Latency on reasoning models.** See section 6: glm's update takes about three times as long.
- **Candidate misses.** A matter named by none of its anchors is not listed. The model may
  still give it a step (rule 3 then asks for evidence), but nothing makes it.
- **Model drift.** The rules hold whatever the model does, but the recall gain is a model
  behaviour; a new host model should be replayed before it becomes the default.
- **Rollback.** Revert the change: the fence and the full window come back together.

## Tests

- Candidate extraction: owners from a delegated result, a Move on and an Answer; a group named
  by id, by a linked Task and by each anchor kind; a title word that is not an anchor; a
  delegated result's body not searched; mid-turn messages searched.
- Each rule's refusal, alone and together, and a clean update passing; a quote with markdown,
  extra whitespace and an elision passing; a paraphrase, a stitched quote and an earlier turn's
  sentence refused; a turn with no candidate asked and checked as today.
- The window: it holds the system message, the opening message and the added rows and nothing
  older; the carry pass continues from it.
- No fence: an update on a delegated turn writes a step on another group; the scope templates
  are gone; the tests that pinned the fence pin the new behaviour instead.
- The schema: `matters` typed; the token-budget pin moved by the measured amount.
- en and zh templates rendered; `tests/test_i18n_prompts.py` keeps them paired.
- Acceptance before merge: the replay harness on the labelled turns, both models, with
  multi-matter runs at least 50/72 and single-matter runs at least 70/72 (one or two runs
  either way is noise at four runs per turn).

## Docs to update

- `CONTEXT.md`: the Task Board entry's sentence on what a turn known to be about a group may
  write; candidate group and `matters`, defined there as they are coined.
- The board spec: section 2 (the tool's schema), section 3 (Input; What a turn may write),
  section 5.1.1 (Move on: "kept to that group"), section 8 (Tests).
- `raven/agent/task_board/runner.py`'s module docstring; the comment on Answer in
  `ui-web/src/features/tasks/store.ts` ("its turn is kept to the group").

## Decisions (owner, 2026-11-16)

1. **Anchors** are derived by code from the title, as in section 1; groups carry no stored
   anchors.
2. **Plain turns** get the checklist whenever they have a candidate; the acceptance replay adds
   a labelled sample of plain turns that name groups.
3. **MiniMax** is measured before merge, run only once the owner clears the plan quota, which
   the live gateway shares.
