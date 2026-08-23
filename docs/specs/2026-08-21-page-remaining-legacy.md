# What is left of the served page's legacy layers

Status: accepted as an inventory, not as a plan for any one piece. Two of its
plan calls have since been corrected by the work: the sheet dock's shape (see
Axis 1) and onboarding's "open question" (see the last section). Both were wrong
in the same direction -- reasoning from the code's shape rather than measuring
what it does. Every number
below is measured on `639eb25a` and is not kept current; re-measure before acting
on it -- `!159`, `!161` and `!162` were open against that commit and move some of
it, so anything landed since has already made a row here optimistic.
Scope: `ui/` only. No wire-protocol change, no server change.
Follows: `2026-08-19-page-datasource-seam.md`, `2026-08-20-page-turn-machine.md`

## Why this document exists

Because the answer to "how much drawing is left in the legacy layers" has been
given five times and been wrong five times, each time by a different fault in
the method rather than in the arithmetic:

| answer | method | what the method did |
|---|---|---|
| "two blocks, 134 lines" | counted renderers **for module pages** | missed every overlay, chrome primitive, skeleton, the onboarding flow, and the settings toolset |
| "13 strands" | the ratchet | correct for what it measures -- but it measures **one** of the two couplings, and the smaller one |
| "`drawPlugTab` is 173 lines" | measured the span from one top-level declaration to the next | that function is **18** lines; the span was mostly its neighbours |
| "`main.tsx` publishes 20 names" | counted assignment **lines** | **18** names; `window.RavenIslands` is assigned three times |
| "12 verbs are guarded" | `grep -c "if (typeof"`, a **line** count again | **10** are guard-only; two are in the literal as well, and behave as if unguarded |

Every one of the last three is the same failure at smaller scale: a number
produced by a proxy for the thing being counted, reported as the thing being
counted. Twice the proxy was "lines that look like the thing".

The fourth and fifth are the sharpest, because the fourth was the only number in
the first draft with no method attached, and the fifth was introduced by the very
table that was drawn to fix the fourth -- on a page whose whole argument is that
this is how the other answers went wrong. Review caught all three, not the
author. That is the strongest evidence on this page for the thing it recommends:
put the count behind a gate, because the author of a count is the worst reader
of it.

So every row below states its method, the numbers are pinned to a commit, and
the second coupling -- the one the ratchet does not see -- gets its own section
instead of a footnote.

## What is already done

`features/` is 14,424 lines and `shell/` is 1,545 across 15 islands and 12
writers. Every domain that owns a page or a panel has moved: cron, memory,
skills, plugins, connections, browser, subagents, rail, external agents,
settings, workspace, transcript, composer, dag, model.

The legacy layers are 6,196 lines: `demo/` 3,350 and `live/` 2,846. That number
is not "how much is left to move" -- most of it is fixture data, boot order, and
the bridge, which are the layers' actual job.

## Axis 1: the drawing that is left

Method: for every top-level function in `demo/` and `live/`, count the lines in
its body that construct DOM (`mk(`, `innerHTML`, `appendChild`,
`createElement`, `.append(`), read anything with three or more to decide whether
it draws or merely mounts, then measure the body by brace depth -- not by the
distance to the next declaration, which is what produced the 173 above.

| piece | file | lines | target shape |
|---|---|---|---|
| `showOnboard` | `demo/160-boot.js` | 204 | island -- `features/onboard/`; this cell said *undecided* and that is settled, see below |
| dag sheet svg (`dagSvg` + `drawDag`) | `live/240-external-agents.js` | 156 | island (half done: geometry is already `RavenIslands.dag`) |
| `openDetail` | `demo/120-capabilities.js` | 113 | **removed** -- its sole remaining entry opens the plugin island's market detail |
| clarify sheet | `live/070-notify.js` | 112 | the rack's next tenant, beside the approval sheet -- not a writer, for the reason below |
| settings toolset (`renderToolset`, `toolLine`, `toolCredRow`) | `demo/130`, `demo/120` | 96 | into the settings island, which currently delegates it back out -- and this row is an **Axis 2** reduction, not an Axis 1 move; see below |
| `approveSheet` | `demo/040-state.js` | 57 | **next** -- `features/composer/approve.ts` on the composer's bag (`!182`, open); decided, not landed |
| the sheet dock (`sheetSession` .. `sheetsSync`) | `demo/040-state.js` | 47 | **moved** -- `features/composer/sheets.ts` (`!180`); this row said *writer* and that was wrong, see below |
| `upShade` | `live/210-update-notice.js` | 39 | writer |
| `hubSkeleton` | `demo/152-skills.js` | 26 | **moved** -- the skills island owns the first-load skeleton host |
| `menuAt` | `demo/040-state.js` | 13 | writer |
| `toast` | `demo/040-state.js` | 11 | writer |
| small (`mkMcpRow`, `authFail`, two install buttons) | various | ~100 | `ico` concat copy removed; fold the rest into their owners |

About 1,000 lines, and the shape column matters more than the total. The rule
the migration has settled on is the one `shell/lightbox.ts` states -- a thing
that owns one node appended to a host, belonging to no page's root, is a writer;
a thing that owns a container and a list is an island.

**Applying that rule to the sheets gave the wrong answer, and the correction is
worth more than the rows it fixes.** This table called the sheet dock a writer.
It owns `#sheetRack` and a `Map` of element sets keyed by conversation, which is
a container plus a list -- an island by the rule as stated. Worse, the
classification decided the price and the price was backwards: as a writer its six
names each needed a `window.` publish for the concat layers that call them,
taking the page's published surface from 19 to 25, on a migration whose point is
to shrink it. As members of a bag that already existed they needed none.

The question the rule does not ask is **whose**. `#sheetRack` sits inside
`.dock`, one node above the composer card in the same markup, and every mutation
of the rack ends in `dockLift()` -- which was already
`RavenIslands.composer.dockLift`. The dock had been calling into the composer
island across the layer boundary all along. So the shape question ("writer or
island") is second; the ownership question comes first, and answering it is what
makes a move cost nothing.

That reclassifies the sheet family: the approval sheet goes in beside the rack
rather than into `shell/`, and the clarify sheet follows it. Of the rows above,
the ones that really are writers are `upShade`, `menuAt` and `toast` -- each one
node appended to a host that belongs to no island.

### What looks like drawing and is not

`demo/153-plugins.js`'s `drawPlugTab` shows up in any DOM-construction count.
Its body is `box.appendChild(RavenIslands.plugins.host)` plus page chrome: the
caps page's **dispatch**, not a renderer. The same care is owed to the other
`draw*` names in `demo/152` and `demo/153`, and to anything in `demo/160` that
is boot order rather than drawing.

## Axis 2: the shell verbs, which nothing counted until this was written

`ui/src/shell/bridge.ts` declares `interface Shell` with **61 verbs** on this
document's baseline, and `demo/155-bridge.js` (75 lines) publishes **every one of
them**. Five are required (`T`, `toast`, `menuAt`, `confirmAsk`, `showPage`).

There is a gate on the number now -- see the end of this section -- and it reads
**60**, the one difference being `setLang`. Every count below is the baseline's;
re-measure before acting on one, as the status line says.

The other 56 are optional in the type. The `?` there is doing a different job
and `bridge.ts` says which: "Optional, so fakes that predate a helper stay
valid." That is about test fakes, not about a missing legacy half.

**What decides whether a verb can be retired is how it is published, and there
are two ways.** 51 are unconditional keys in the object literal
(`demo/155-bridge.js:7-59`, `T` at 8 through `plugRedraw` at 58); 12 are guarded
one-liners starting at line 64. Two verbs -- `appVersion` and `openConn` -- are
in **both**, which is how the counts reconcile:

```
$ grep -oE '^  [A-Za-z][A-Za-z0-9_]*:' ui/src/demo/155-bridge.js | sed 's/://;s/^ *//' | sort > lit
$ grep -oE 'window\.RavenShell\.[A-Za-z][A-Za-z0-9_]*' ui/src/demo/155-bridge.js | sed 's/.*\.//' | sort -u > grd
$ comm -12 lit grd
appVersion
openConn
51 literal + 12 guarded - 2 in both = 61
```

The runtime behaviours differ, and the split by behaviour is **10 / 51**:

| | what `shell().foo?.()` does when the legacy half is gone |
|---|---|
| guard-only (10) | the verb is absent, so the optional call no-ops |
| in the literal (51) | the verb is present -- a closure over a bare legacy identifier -- so the call throws `ReferenceError` |

**The two in both rows behave as the second.** A guard cannot make a verb
absent when the literal already assigned it thirteen lines earlier: the `typeof`
test skips, the literal's closure stays, and the call sails through and throws.

That trap sits on two verbs a reader is likely to reach for early -- one is the
foot row's build string, the other a nav destination -- and both appear in the
guarded list, which is the column that looks safe.

So "optional" must not be read as "already degrades", and neither may "guarded".
Before retiring any verb, check the literal, not the guard list: dropping one of
the 51 from `demo/155-bridge.js` without also changing the island that asks moves
the failure from compile time to first click.

These run the opposite way to a strand:

| | direction | counted by the ratchet |
|---|---|---|
| a strand | the live layer writes a name the demo layer declared | yes -- 16 left |
| a shell verb | an island asks the legacy layer a question | **no** |

So the page's islands do not stand on their own: they stand on 61 answers from
the layer this migration exists to retire, funnelled through one file. That is
the larger of the two couplings and, when this was written, nothing gated it --
a new verb could be added without anything objecting, and several had been.

**This is not an argument for deleting verbs.** Some are permanent seams by
design (`T` has to come from wherever the catalogue lives). It is an argument
for the number being visible: the seam ratchet earned its keep by making one
number go down on purpose, and the verb count was the number nobody was
watching.

Recommendation: extend `ui/scripts/count-shared-globals.mjs`, or add a sibling
script, to report the verb count with its own `EXPECTED`, so a verb added
casually has to be argued for in the change that adds it.

**Done.** `ui/scripts/count-shared-globals.mjs` reports it under
`EXPECTED_VERBS`, beside the two counts of the opposite direction. It reads the
members of `interface Shell` rather than the publish side, which makes it a
floor rather than the exact number of questions asked: `look` and `ntf` are
namespaces carrying two and three methods, so sixty members are sixty-three
callables.

The number moved between this document's baseline and that gate landing: 61 here,
**60** measured on `ec99bf85`. `!159` took `setLang` out when the language pick
became `DS.settings`'s, which is what an Axis 2 reduction looks like when it
happens as a side effect of an Axis 1 move rather than on purpose.

## Axis 3: nine containers the live layer fills in place

This document originally named two couplings. There is a third, and it is the
one that decides when a fixture can actually be deleted.

The ratchet counts **rebinding** a name, and says so: "`x.y =` are not rebinds
and are not counted". But the live layer's main way of getting real data onto
the page is not a rebind at all:

```js
// live/090-extensions.js
TOOLS.length = 0;
ext.tools.filter((t) => !t.mcp_server).forEach((t) => TOOLS.push(mkToolRow(t)));
```

`TOOLS` is declared in `demo/030-fixtures.js` as ten canned rows. The live layer
empties it and refills it from the server. The same file's own header states the
design plainly -- real lists are "written into the demo's data arrays IN PLACE, so
every existing renderer keeps working" -- and installs property setters on the
rows so that a toggle the demo renderer performs (`t.on = ...`) persists through
`settings.set`. It is a deliberate mechanism, not an accident, and it works.

Nine names, now measured by the same script under its own `EXPECTED`. Measured
on `fa98528e`, not on this document's own baseline:

| container | declared in | what the live layer does to it |
|---|---|---|
| `TOOLS`, `SKILLS`, `PLUGINS` | `demo/030-fixtures.js` | emptied at boot (`live/010`), refilled from `system.extensions` (`live/090`) |
| `CRONS` | `demo/030-fixtures.js` | emptied at boot and **never refilled** -- live installs its own `DS.cron`, so the emptying exists only to stop the fixture rows leaking into a live page |
| `PROVIDERS` | `demo/130-settings.js` | emptied and refilled from the config (`live/120`) |
| `CFG` | `demo/040-state.js` | two fields written from the config (`live/120`) |
| `SESS` | `demo/030-fixtures.js` | unshifted from four live files -- and also rebound outright (`SESS = rows`, `SESS = SESS.filter(...)`) |
| `q` | `demo/040-state.js` | shifted and pushed by the turn machine |
| `WS` | `demo/100-workspace.js` | its five fields overwritten in one go from a parked snapshot (`Object.assign(WS, pk.ws)`, `live/060`) and one of them incremented per turn (`WS.turn += 1`, `live/050`) |

`SESS` and `q` are already Axis 1 strands -- rebound *and* mutated -- so the two
numbers overlap by exactly two. The other seven are counted nowhere else.

`WS` is the row that had to be pointed out, and it is worth saying how it hid.
Its two write shapes are the two that put the container's name nowhere a
left-hand side would be: `Object.assign` names it as an argument, and `+=` puts
an operator where the `=` would go. The second is the sharper lesson, because the
rebind count excludes `+=` deliberately and correctly -- `x.f += 1` rebinds
nothing -- and this count has to include it for the same reason, read the other
way round. The one demo container whose own header says "the shared state every
layer mutates" was the one neither number held.

`CRONS` is the row worth reading twice, because it shows the mechanism has two
halves that come apart. Its domain is fully migrated: the cron island reads
`DS.cron`, which the live layer installs wholesale over the fixture source. Its
only remaining readers are that fixture source -- unreachable on a live page once
`live/100` has replaced `DS.cron` -- and `cronFailing()` in
`demo/030-fixtures.js`, which has no callers at all. So a container survives its
renderers, and the last thing tying this one to the live layer is the line that
blanks it.

That line is **not** safe to simply delete, and the reason is worth writing down
because it will recur on every container. `live/010`'s comment states an
invariant: the demo shell has already seeded *and painted* by the time it runs,
so the clearing happens in the same task, "so nothing mock survives to the first
frame". Between `live/010` and `live/100` there is most of the live layer, and a
repaint anywhere in that window still reads the fixture source. Retiring a
container therefore means answering an ordering question, not just a reader
question -- which is the argument for the live layer installing its sources
before the first paint, already on the endgame list below.

**Why this matters for the order of the remaining work.** An Axis 1 move retires
a name; it does not retire the storage. A renderer can move into an island and
read from a `DS` source while the fixture array it used to read stays exactly
where it is, still being filled by the live layer for whoever else reads it. So
"the demo layer is empty" is not implied by "Axis 1 is empty": the fixtures come
out only when every reader of each container has a source to read instead, which
is a per-container question, not a per-renderer one.

It also revises the estimate for the settings toolset (batch 0 below). Moving
`renderToolset` into the island needs the inventory itself to become a read on
`DS.settings`, because the island cannot see `TOOLS` -- so that row is an Axis 3
move as well as the Axis 2 reduction it is described as. It is still the
cheapest row; it is not the one-line row the Axis 2 section makes it sound.

## The constraint that decides the order

Almost everything the legacy concat scripts reach in the modern bundle is
reached by a `window.` name published in one place, `ui/src/main.tsx`. A concat
script cannot import, so for those there is no second route.

Method, because the first version of this line carried a number with none and
was wrong by two: `grep -oE '^window\.[A-Za-z]+' ui/src/main.tsx | sort -u | wc -l`
gives **18**. Counting assignment *lines* gives 20, because
`window.RavenIslands` is assigned three times (`main.tsx:135`, `291`, `302`).

The shape matters more than the miss. Those 18 are **17 scalars plus one bag**:
the 15 islands are not 15 names, they are 15 keys inside `window.RavenIslands`.
So the rule is exact for the writers -- each one costs a new scalar -- and only
collectively true of the islands, which cost a new key in a bag that already
exists.

Therefore **most pieces in the Axis 1 table touch `main.tsx`**, and it is in the
diff of essentially every island MR ever opened for this migration. Two
consequences:

1. **These cannot be parallelised across MRs.** Two pieces in flight at once
   means one rebases onto the other, and under `ff` + squash a parent merging
   forces the whole stack to be replayed -- which force-pushes, which clears
   every approval on it. A three-deep stack pays three re-approvals per merge.
   Measured, not predicted: `!156` merging did exactly this to `!159`, `!161`
   and `!162`, all three of which were green and approved at the time.
2. **Batch by publish, not by file** -- and the sharper form: **a piece already
   behind a shell verb has no publish to batch.** If one MR must pay the
   `main.tsx` cost, it should carry every piece that shares a host; the sheet
   family is the clear case, since moving the dock without its tenants leaves
   them reaching into a writer through verbs that exist only until they follow.
   But a piece the island *asks for* costs nothing there, and is the only kind
   that parallelises.

### The one row that is not an Axis 1 move

The settings toolset is reached the other way round. The island asks:

```
ui/src/features/settings/SettingsPage.tsx:1181  if (el) shell().renderToolset?.(el)
ui/src/demo/155-bridge.js:75                    if (typeof renderToolset === 'function') ...
ui/src/shell/bridge.ts:146                      renderToolset?(host: HTMLElement): void
```

and the trio is closed: `toolLine` and `toolCredRow` have exactly one caller
each (`demo/130-settings.js:162-163`). `renderToolset` appears nowhere in
`main.tsx`, and `settings` is already a key in the bag.

So moving it edits `SettingsPage.tsx`, deletes one line from
`demo/155-bridge.js` and one from `interface Shell`, and touches **no**
`window.` name. It is not an Axis 1 move that pays the toll -- it is the first
**Axis 2 reduction** this document argues for, 61 verbs to 60, and it can be in
flight beside anything else without stacking.

`hubSkeleton` is the contrast that shows the rule still holds elsewhere: its one
caller is `live/090-extensions.js:132` calling the bare name, which has to
become a new `RavenIslands.skills` key. Ten of the twelve rows are like that.

Suggested order:

| # | batch | pieces | ~lines | stacks? |
|---|---|---|---|---|
| 0 | settings toolset | `renderToolset`, `toolLine`, `toolCredRow` | 96 | **no** -- run it beside batch 1 |
| 1 | the sheet family | dock, `approveSheet`, clarify, dag svg | 372 | yes |
| 2 | chrome primitives | `toast`, `menuAt`, `ico` | ~55 | yes -- and it **does** owe a publish: both are reached the island way *and* by bare name from the concat scripts (`toast` 65 calls across 23 files, `menuAt` 3 across 3) |
| 3 | the caps page | `openDetail`, `hubSkeleton`, the two install buttons | ~170 | yes |
| 4 | odds | `upShade`, `mkMcpRow`, `authFail` | ~110 | yes |
| 5 | onboarding | `showOnboard` | 204 | yes |

Batch 0 first and in parallel because it costs no publish. Batch 1 next because
it is the only one whose pieces are coupled to each other.

Batch 5 was last "because it is the one open design question". It is not one any
more -- see below -- so its place in the order is now just its size.

## Onboarding: the question was closed, and the answer was already there

This section used to say `showOnboard`'s shape was undecided because it runs
during boot, before `main.tsx` has published anything, which "inverts the order
the rest of the migration relies on". **That is not true, and the rest of the
argument had already been answered by the code.** Three measurements:

**`main.tsx` runs first.** `page.html` carries exactly two scripts and
`/*__MODERN__*/` is the first of them (`ui/build.py` splices the seam and demo
layers into the second, and appends live to it). Checked on the built page
rather than read off the markers: `document.scripts.length` is 2,
`document.scripts[0]` is the one containing `RavenIslands`, and by the time the
`load` event fires `window.RavenIslands` is already populated. So every publish
and every island key is in place before any demo or live code runs, let alone
before an overlay opens. There is no inverted order.

**Its data side is already inverted.** `showOnboard(be)` takes its backend as a
parameter -- `{ options, saveKey, setModel, recheck }` -- with the live layer
mapping those onto the setup RPCs and the demo layer passing
`demoOnbBackend()`. That is a `DS` source in everything but where it is
installed. The "zero `RavenIslands` references" this section led with is true and
irrelevant: it does not reach for the page because it is handed what it needs.

**So the shape is the ordinary one.** It owns a container (`#onb`) and a
multi-step flow, which is an island by the stated rule; it goes to
`features/onboard/` and the backend it already takes becomes the source it
already resembles. Nothing about boot changes that.

What *was* actually blocking is a thing this section never mentioned: **there was
no way to look at the flow.** `?onboard=1` on a live page forces the real flow,
which writes -- `saveKey` and `setModel` hit the config. The canned version, which
writes nothing, was reachable only on `file://` or `?stub=1`, and both of those
blank the live data as well. Against a real `serve` there was no risk-free way to
open that screen, and a 204-line flow needs opening more than once. `?onboard=demo`
now does it (`!181`), which is the prerequisite this section should have named.

## Endgame, once Axis 1 is empty and Axis 2 is deliberate

- Delete the concat manifests; fold `ui/build.py` into Vite. The single-file
  dist contract survives as a Vite config rather than a Python script.
- Retire `count-shared-globals.mjs` and `tests/test_ui_language_repaint.py`.
  Both exist to watch a coupling that will be gone.
- **Install the live sources before the first paint.** This is the root fix for
  a whole class of bug, not a cleanup: the demo layer paints first today, so a
  live page's first frame is drawn from fixtures and corrected on the next
  redraw. Three separate bugs found during this migration were that one fact
  wearing different clothes.
