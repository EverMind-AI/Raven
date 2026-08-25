# The build wrap-up is blocked on a precondition that is not met

Status: a decision not to act, with the measurement behind it. Numbers are on
`076d44e0`.
Scope: `ui/build.py`, `ui/vite.config.ts`, and the two gates the wrap-up would
retire.
Follows: `2026-08-21-page-remaining-legacy.md`, whose Endgame lists this work.

## What the wrap-up was going to be

Three items, from that document's Endgame:

- delete the seam/demo/live concat manifests and fold `ui/build.py` into Vite,
  with the single-file dist contract surviving as Vite config;
- retire `ui/scripts/count-shared-globals.mjs`;
- retire `tests/test_ui_language_repaint.py`.

The last two exist to watch a coupling the first one removes, so all three move
together or none do.

## Two documents, two different unlock conditions

| source | condition | met? |
|---|---|---|
| `2026-08-21-page-remaining-legacy.md`, Endgame | Axis 1 empty and Axis 2 deliberate | yes -- Axis 1 and Axis 3 are 0, and every surviving verb has a verdict as of `2026-08-25-shell-verb-audit.md` |
| `ui/vite.config.ts`, header | "when the last legacy part is gone this config grows the html entry and build.py retires" | **no** |

The second is the operative one, because it is the condition the code states
about itself, and it is measurably false. `_DEMO_PARTS` is 21 files and
`_LIVE_PARTS` is 24; `ui/src/demo/` is 2,510 lines and `ui/src/live/` is 2,814.
Nothing in the wrap-up removes any of them.

## Why the legacy parts are not going away

They are not residue. The remaining-legacy document already said what they are
-- fixture data, boot order and the bridge, "the layers' actual job" -- and the
offline mode is built out of exactly that: `ui/src/live/010-boot-guard.js:7`
returns early on `file://` or `?stub=1`, leaving the demo layer to answer every
source on its own.

Keeping that mode is a standing product decision, not an oversight. So "delete
the concat manifests" and "keep the offline demo" cannot both hold, and the
second one wins.

## What is actually available, and why it was not taken

With the layers staying, the only version of item 1 left is to move the
splicing rather than remove it: a Vite plugin doing what `build.py`'s 179 lines
do today -- injecting at `/*__STYLE__*/`, `/*__MODERN__*/` and `/*__DEMO__*/`,
appending the live layer, copying `dist/assets`.

That trades one benefit for one risk. The benefit is that the build becomes one
tool instead of two. The risk is that the rewrite runs through the two
capabilities this project has explicitly committed to keeping: the single-file
dist that the wheel and `raven serve` depend on, and the offline mode. Neither
gains anything from the move.

Not taken, on that trade. The build is two tools and works; the reasons it is
two tools are still true.

## What would unlock it

The legacy layers emptying -- which is a decision about the offline demo, not a
refactor anyone can schedule. Until then:

- `ui/build.py` stays, manifests included;
- `ui/scripts/count-shared-globals.mjs` stays. Two of its three counts are at
  zero and hold that floor -- a new strand or a newly held container still has
  to be argued for -- and the third reads 18, which is a coupling that is
  measurably still there;
- `tests/test_ui_language_repaint.py` stays, for the same reason.

Recording this so the next reader does not find an Endgame with an unticked box
and take it as work nobody got to.
