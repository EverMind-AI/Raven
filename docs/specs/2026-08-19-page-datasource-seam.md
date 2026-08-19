# The page's DataSource seam

Status: accepted (phase P2 of the UI consolidation plan)
Scope: `ui/` only. No wire-protocol change, no server change.

## Problem

The served page is a demo shell that paints fixture data, plus a live layer
that boots afterwards and takes the page over by reassigning the shell's
bindings -- 75 shared mutable globals at the time of writing (the exact,
current number is `node ui/scripts/count-shared-globals.mjs`). Every class
of bug the page is known for -- fixture data flashing before real data, the
double splash, a stale snapshot hijacking a live session -- is a race
between the two writers this structure creates.

## Shape

One seam object, declared before either layer loads, owned by no page:

```
// src/seam/000-datasource.js (loads before demo/ and live/)
const DS = {};            // per-domain sources: DS.cron, DS.sessions, ...
```

There is no second object beside it, and deliberately so: a "real source is
installed" latch would only be readable after the install it is meant to
guard, and the boot order below already makes the question unaskable.

- A domain's **renderer** lives once, in the demo shell, and reads only
  through `DS.<domain>` (list/get/save/delete/subscribe -- the method set
  mirrors the RPC surface the live layer already uses).
- The demo shell **registers** its fixture implementation:
  `DS.cron ??= fixtureCron`. Registration replaces declaration-for-override.
- The live layer **installs** the real implementation before first paint:
  `DS.cron = rpcCron`. Installing a property on the seam is not a shared
  global; the ratchet counts only bare demo bindings the live layer writes.
- Boot order makes this race-free without flags: the assembled script runs
  seam, then demo statements, then live statements, all synchronously,
  BEFORE the load event that triggers the first paint. When live mode is
  active it has already replaced the source by the time anything draws, so
  there is nothing to clear, veil, or repaint.

## The flip recipe (one page per MR)

1. Extract the page's renderer so it reads only `DS.<domain>`.
2. Wrap the page's fixture data as the fixture source; register it.
3. Rewrite the page's live part as the rpc source; install it.
4. Delete the live part's renderer copies and reassignments.
5. Lower the ratchet ceiling by the strands removed, same diff.
6. Verify both modes in a browser (``?stub=1`` and live) before pushing.

## Order of pages

Schedules (cron*) first: 6 strands, self-contained, both sides small.
Then connections (conn*), extensions/plugins, external agents (xa*),
memory, settings, workspace, and last the transcript + session rail
(`SESS`/`cur`, the deepest coupling -- by then the recipe is proven).

## What this does NOT do

- No ES modules yet: parts stay plain concatenated scripts. Import
  bindings are read-only, so modules become mechanical only once the
  ratchet reaches zero -- that conversion closes P2.
- No renderer redesign: pixels and behavior stay as they are; only who
  owns data changes. Any visual change belongs to its own MR.
