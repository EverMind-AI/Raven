# The page's turn machine

Status: accepted. Steps 1 to 3 are done; 4 to 6 open. This line is the only
place a reader can learn that -- the prose below is plan-tense on purpose, and
the inventory's file:line references are pinned to a commit rather than kept
current, so nothing else here reports progress.
Scope: `ui/` only. No wire-protocol change, no server change.
Follows: `2026-08-19-page-datasource-seam.md`

## Why this document exists

Every other domain in the served page has moved to an island with a DataSource
behind it. One cluster has not, and it is the largest one left: the live turn
machine, `ui/src/live/050-turn.js` (268 lines) plus
`ui/src/live/080-overrides.js` (278 lines), which between them write ten
bindings the live layer does not own.

The reason it had not moved is that the seam was the wrong question to ask of
it. `DS.x` models "where does this data come from". A turn is not data arriving
from somewhere; it is a state machine with a clock. Asking which source owns
`busy` has no answer, so the cluster kept getting deferred.

This document is the answer, and the answer starts by rejecting the premise:
**these ten names are not one cluster.** Eight of them are declared on two
adjacent lines of `ui/src/demo/040-state.js` (:2 and :3) and the remaining two
are the actions that drive them, and that shared declaration is the only thing
they have in common. They are four different things, and three of the four need
no machine at all.

## What the ten names actually are

Measured with a classifier over `ui/src/demo/*.js` and `ui/src/live/*.js` that
sorts every occurrence into a reset (`x = []` / `x = null`), a write, a push,
or a read, and skips comment lines. Counts below are from that run; where a
name is also a common local variable the raw count is noted as inflated.

### Group A -- dead state (5 names)

`kids`, `lastRun`, `raw`, `tl`, and (in live mode) `use`. No surface in the
page ever renders any of them. `kids` is the strict case -- written and read
nowhere at all; `lastRun` and `raw` are read only to be carried across a
session switch and put back, which is the same thing one indirection out; and
`tl` is read only to compute a `use` that live mode does not draw. The table is
exact, and its `file:line` references are the inventory as measured on
`0dbad1a7`, before any step below had landed -- steps 1 and 2 delete most of
the lines it cites, so read it as the case for the deletions rather than as a
map of the current tree:

| name | written | read |
|---|---|---|
| `kids` | fixture data in `demo/060-conversation.js:17`, `demo/080-replay.js:58`; reset in `live/080-overrides.js:49,89` | nowhere |
| `lastRun` | `demo/060-conversation.js:17`, `demo/090-composer.js:61`; reset in `live/080-overrides.js:49,89` | only by the park snapshot, `live/060-parked.js:32` (restored at `:61`). Never displayed. |
| `raw` | pushed 3x in `demo/080-replay.js`, 3x in `live/050-turn.js`, once in `finishTurn`, and by the `rawPush` shell verb from `features/transcript/store.ts:496` | only by the park snapshot, `live/060-parked.js:32` (restored at `:61`). Never displayed. |
| `use` | 12 write sites across both layers | `DS.composer.meter` in `demo/090-composer.js:45-46`, which live replaces with `() => ''` in `live/050-turn.js:199`; and the park snapshot |
| `tl` | pushed only by the `tlPush` shell verb, from `features/transcript/store.ts:495` | `tl.length` at `live/050-turn.js:209`, to fill a `use` live never draws; and the park snapshot. Not read at all in demo. |

"Read only to be saved and put back" is still dead, and it is worth being exact
about why, because it is the one place where deleting these is not obviously
free: `parkTurn` saves them so that leaving a session mid-turn and coming back
does not lose them. Nothing ever renders them, so what comes back is a value no
surface consults. Dead state that survives a session switch is dead state.

`raw` deserves a sentence of its own: it is an event log that the page
accumulates on every turn, that an island calls out through a shell verb to
append to, and that nothing displays. `use` is the same story restricted to
live mode: the numbers are computed on every `message.complete` and the only
reader of them is a meter that live mode has already emptied.

`tl` belongs here and the first draft of this document said it did not, on the
grounds that `tl.length` fills `use.calls` (`live/050-turn.js:209`) and that
counts as a reader. It does not, and the analysis stopped one level short: the
`use` that read fills is the LIVE one, and live mode never displays `use` --
`DS.composer.meter` is `() => ''` there. So the chain is
`tlPush` -> `tl` -> `use.calls` -> a meter that draws nothing. `tl` is dead two
levels deep, and in demo mode it is not read at all: demo's `use` comes from
fixture data (`demo/030-fixtures.js`), never from `tl`.

`use` is the one with a real reader, and only in demo:
`demo/090-composer.js:46` formats the token readout under the composer from it.
Every live write of it is inert. So the split is not "both migrate" but **`tl`
is deleted outright and `use` becomes the demo layer's own** -- live stops
writing it, and the name leaves the coupling count without moving anywhere.

**These do not need migrating. They need deleting.** Five strands leave the
gate for the cost of removing write sites, two shell verbs (`tlPush`,
`rawPush`), and their island call sites. `use` is the one that is deleted from
the live layer rather than from the page: the demo keeps it, because the demo
reads it.

### Group B -- the session pointer (1 name)

`cur`, the id of the open conversation, `null` while a draft. 19 writes and 53
reads across 17 files in the two page layers, plus every source verb that takes
a session (`DS.agents.list(sessionId)`, `subagent.context({id, session_id: cur})`,
`dag.node({..., session_key: cur})`).

`cur` is in this cluster only by accident of where it is declared. It is not
the turn's state and it is not any one domain's data -- it is the page's
current selection, and it is the argument the sources already take. Its
migration is independent of everything else in this document and should not
wait on it.

### Group C -- two actions (2 names)

`send` and `halt`. One write each, both in `live/080-overrides.js` (:150,
:235). Read by the composer, the chrome's shortcuts, the replay, and the
retry affordance on an error row.

These are already verb-shaped. They are exactly what `DS.composer` is for --
that source already carries `meter` (`live/050-turn.js:199`), `upload`
(`live/180-attachments.js:10`) and `slash` (`live/190-session-actions.js:19`).

### Group D -- the turn itself (2 names, and the object)

`busy` and `q`, plus the `live` object in `live/050-turn.js:5`
(`subId`, `st`, `steps`, `say`, `open`, `sawEpisode`, `startedAt`, `answerAt`)
and the `cancelInFlight` latch in `live/080-overrides.js:233`.

This is the only part that is genuinely a state machine, and it is the only
part where the seam has nothing to offer.

`busy` is a boolean answering four different questions:

- the composer asks "may I send, or must I queue" (`live/080-overrides.js:164`)
- the rail asks "is this row running"
- the meter asks "should this tick"
- the park snapshot asks "is there a turn here worth saving"
  (`live/060-parked.js:24`)

and the machine's real states are more than two: idle, sent-and-waiting,
streaming, blocked on the reader (an approval or clarify sheet is up),
cancelling (`cancelInFlight`, which exists precisely because "not busy" was
not enough -- see the comment at `live/080-overrides.js:227-232` about a drain
racing a dying turn into a `-32003`).

`q` is the queue of messages typed while a turn was running. Its raw read count
is inflated by same-named locals; the real sites are the drain in `finishTurn`
(`live/050-turn.js:221`), `drainQueue` (`live/080-overrides.js:220`), the push
in `send` (:164), the park snapshot (`live/060-parked.js:32`, restored at
`:61`), and the strip the composer draws.

## The fourth consumer: the park snapshot

`ui/src/live/060-parked.js` is the file every step below has to account for, and
it is easy to miss because it neither draws nor fetches. It is what makes an
in-flight turn survive a session switch: leaving mid-turn parks the transcript's
detached DOM plus a snapshot of the turn's state, and returning puts both back.
Its own header says the detached nodes are "persisted nowhere else until the
turn ends", so this is a data path, not a convenience.

It held six of the ten when this document was written -- `busy` gates the whole
thing (`:24`, `if (!turnOwner || !busy) return;`), and
`busy, use, tl, raw, lastRun, q` were saved as shorthand properties at `:32`,
which makes all six of them reads, and restored at `:61`. Steps 1 and 2 took
four of those out (`raw` and `lastRun` because nothing reads them, `use` and
`tl` because live never drew them), so **the snapshot holds `busy` and `q`**,
and those two are the ones steps 4 and 6 have to move.

**For steps 4 and 6 the snapshot must move, not disappear.** Once `q` and `busy`
are island state, `parkTurn` and `restoreTurn` have to round-trip them through
island accessors. That pattern is already in this file, one field over, and it
exists for exactly this reason: `liveT0` is the composer island's live-clock
anchor, parked via `RavenIslands.composer.liveAnchor()` at `:37` and restored
via `setLiveAnchor()` at `:64`
(`features/composer/store.ts:225-226`, published at `main.tsx:296-297`,
exercised at `features/composer/ComposerPage.test.tsx:309-314`). The comment at
`:33-36` records the bug that accessor was added to fix: without carrying the
anchor, a turn ten minutes in read "2s" after a round trip through another
session. Same shape, two more fields.

### The incentive this document has to argue against

The coupling gate rewards removing live writes. So the cheapest way to make the
`q` and `busy` strands disappear is to delete lines `:32` and `:61` and watch
the number fall. **That is a data-loss change and nothing in the repo would
stop it.**

What would break: come back to a parked turn and the queue is empty, and --
worst -- the phase is unset, so a turn that is still streaming does not know it
is running.

An earlier revision of this paragraph also listed "the token readout is gone",
which was wrong and is worth keeping as a warning about how to read the rule:
live mode never drew `use`, which is exactly why that field could leave `:32`
and `:61` in step 2 with no accessor at all. **A field in this snapshot is not
automatically load-bearing.** The rule is that a field WHOSE STATE AN ISLAND
OWNS must leave through an accessor; a field nothing renders should simply go.
Deciding which one you have in front of you is the work, and the answer for `q`
and `busy` is the first kind.

What would not catch it:

- not the gate: the count genuinely falls, so it goes green.
- not `tsc`: `ui/tsconfig.json` has `include: ["src"]` and no `allowJs`, so
  `demo/*.js` and `live/*.js` are never typechecked.
- not the suite: no test drives `parkTurn`/`restoreTurn`. The only test with
  "parked" in its name
  (`features/transcript/TranscriptPage.test.tsx:490`) stubs
  `DS.transcript.parked` and never touches these bindings.

So the rule for steps 4 and 6 is stated here rather than left to be
rediscovered: **a strand whose state an island owns must leave the park snapshot
through an accessor.** A step whose diff deletes such a line from `parkTurn`
without adding the matching call is wrong even when every gate is green.

## The design

### Group A: delete

Two MRs, no seam, no new mechanism. Remove the write sites for `kids`,
`lastRun` and `raw`; remove `tl` outright and stop the live layer writing `use`;
remove the `tlPush` and `rawPush` verbs from `shell/bridge.ts` and their two
call sites in `features/transcript/store.ts`.

`lastRun` and `raw` also leave the park snapshot (`live/060-parked.js:32`
and `:61`). This is the one place where the gate does the checking for us:
deleting the declarations while the snapshot still names them leaves both
strands counted, so lowering `EXPECTED` fails with "the coupling GREW" and
`--list` names them. Noisy and self-revealing, which is the opposite of the
hazard steps 4 and 6 carry.

`use` needs no island store, which is what the first draft of this document
proposed. Its only reader is the demo meter, so it simply stops being shared:
the live layer drops its five inert writes (`live/050-turn.js`,
`live/080-overrides.js` x3, `live/190-session-actions.js:26`) and the park
snapshot's field, and `use` stays a demo-layer binding that the demo alone
declares, writes and reads. `tl` goes with the `tlPush` verb that feeds it.

That also means neither of them needs the park accessor: an accessor is for
state an island owns, and `use` ends up owned by the demo layer rather than by
the composer island. The accessor rule still governs steps 4 and 6.

### Group B: `cur` becomes `shell/session.ts`

A page-scoped module, not a DataSource, because `cur` is not data from
anywhere:

```ts
// ui/src/shell/session.ts
export function current(): string | null
export function setCurrent(id: string | null): void
export function onChange(fn: (id: string | null) => void): () => void
```

Published under the shim pattern the shell writers already use
(`window.drawFoot`, `window.drawBanner`), so the two page layers keep calling
by name while the module behind the name moves. `onChange` is what the rail
selection and the sheet sync (`sheetsSync`, `ui/src/demo/040-state.js:176`)
subscribe to, rather than every writer of `cur` remembering to call them --
which is the bug shape the current arrangement invites.

Do this one alone. It has the widest diff of anything left in the migration
and it must not share an MR with a behaviour change.

### Group C: `DS.composer.send` / `DS.composer.stop`

The composer source gains two verbs; `live/080-overrides.js` installs them
where it currently assigns the bare names. The demo keeps its fixture pair
through `??=`, as every other source does.

One detail that is not cosmetic: `send` currently reaches into the composer
island for the attachment tray (`RavenIslands.composer.attsPending()` and
`.takeAtts()`, `live/080-overrides.js:154-156`). With `send` installed as a
source verb the island calls in rather than the live layer calling out, so the
tray stays where it belongs and those two reach-ins go away.

### Group D: a turn store in the composer island

The composer island already owns the field, the queue strip, the meter and the
stop button. The turn store goes there, and it replaces `busy` with a phase
rather than translating it:

```ts
type TurnPhase = 'idle' | 'sending' | 'streaming' | 'waiting' | 'cancelling'
```

- `waiting` is blocked on the reader (a sheet is up), which today is invisible
  to `busy` and is why a sheet and a running turn look the same to the queue.
- `cancelling` is `cancelInFlight` given a name, so the drain rule stops being
  a latch two files apart from the state it guards.

The live layer keeps `live/050-turn.js`'s event dispatcher -- that is genuinely
live-side work, mapping the wire's events onto phase transitions -- and stops
assigning a flag. The `live` object's own fields (`st`, `steps`, `say`,
`open`, the two clocks) stay in the live layer: they are already
transcript-island-adjacent bookkeeping and no page layer other than the parked
turn machinery reads them.

Order matters here: Group D last, because the phase model is only worth having
once `cur` (B) can be subscribed to. A turn store that has to be told which
session it belongs to by every writer of `cur` is the current arrangement with
more types.

## Order, and what each step is worth

| step | group | strands retired | notes | state |
|---|---|---|---|---|
| 1 | A (minus `use`) | `kids`, `lastRun`, `raw` | deletions only; drops 2 park fields, gate checks it | done |
| 2 | A (`use`, `tl`) | `use`, `tl` | deletions again; `use` becomes demo-layer-only | done |
| 3 | C | `send`, `halt` | also removes two `RavenIslands` reach-ins | done |
| 4 | D (queue) | `q` | composer store owns the queue; parked via copied accessors | done |
| 5 | B | `cur` | `shell/session.ts` owns the subscribed page pointer | done |
| 6 | D (phase) | `busy` | the phase model; needs step 5; **park via accessor** | open |

The two bold notes are the ones no gate enforces -- see "the incentive this
document has to argue against" above. Step 2 lost its bold note when the
analysis was corrected: `use` ends up owned by the demo layer rather than by an
island, so there is no accessor for it to leave through.

All ten, in six shippable steps, none of which needs a mechanism this codebase
does not already have. After step 6 the two files are the wire's event
dispatcher and nothing else, which is what a live layer is supposed to be.

## A boot-order finding that outlives this cluster

While measuring the above, two places were found where the served page shows
fixture data in live mode. Both have the same cause and neither is a bug in
the notice itself.

The demo layer boots and **paints** before the live layer installs its sources.
Anything the demo painted from fixture data and the live layer does not happen
to repaint stays on screen showing the fixture's values.

Measured on a `raven serve` instance with a throwaway agent home, on
`main` (`562f3251`) and on a branch, identically:

- `#bannerHost` holds the unconfigured-websearch notice at load, drawn by the
  demo boot while `DS.banner` was still the fixture source.
- the composer's meter reads `4 calls / 14.2k in / 3.2k out`, which is
  `demo/030-fixtures.js:193` (`{calls: 4, in: 14226, out: 3180}`) formatted by
  `demo/090-composer.js:46`.

On a healthy gateway both are cleared within milliseconds by the first session
open, which calls `drawBanner()` and `drawMeter()`. On a gateway that cannot
open a session they persist -- which is to say they are visible exactly when
the reader is already trying to work out what is wrong.

This is not a reason to fix either notice. It is the argument for the endgame
step already in the plan: the live layer installs its sources **before** any
paint, at which point the whole class of stale-fixture-on-screen bugs stops
being possible rather than being cleared after the fact.
