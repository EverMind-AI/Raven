# The desk's file tab becomes a deliverables shelf

Status: built, in the four commits this file ships with. Target surface: `ui/`
(the served page and, through it, the desktop window). No runtime change was
required -- see D2. Kept as the record of why the browser went and what the
viewer had to keep.

## What the reader gets

The floating desk (`.desk-palette`) keeps three tabs, but the middle one stops
being a file browser and becomes the session's **deliverables shelf**: every file
this conversation handed over through `deliver_files`, newest first, whatever turn
produced it. Clicking a row opens that file in a right-hand pane -- the same pane
the transcript already opens -- with the delivery's own identity on top of it
(title, one-line description, size, which turn delivered it) and the actions that
belong to a handed-over file (download, open in the host app, reveal, copy path).

Browsing the working directory goes away entirely: no tree, no file search, no
`fs.list`. The **rendering** half stays whole, because it is reached from five
places that are not the desk (D3).

## What is there today

| Surface | Where | What it does |
|---|---|---|
| Desk tabs | `DeskPalette.tsx:23-38` | `diff` / `file` / `agents` |
| File tab body | `DeskPalette.tsx:58-131` (`DeskTree`, `FileNav`) | tree + search over `fs.list` |
| Pane | `DeskSurface.tsx:36-92` | four kinds: `diff`, `file`, `agent`, `agent-record` |
| File viewer | `WorkspacePage.tsx:297-336` (`FileView`) | bar + tree + body, three columns |
| Viewer bar | `WorkspacePage.tsx:337-435` (`Fbar`) | tree toggle, path, render/source, new tab, download, reveal |
| Tree | `WorkspacePage.tsx:436-693` (`FtPane`, `FtResults`, `FtRows`, `Grip`) | ~260 lines |
| Renderers | `WorkspacePage.tsx:707-873` (`FileBody`, `CodeLines`, `CsvTable`, `JsonView`, `BinNote`) | md / img / svg / pdf / html / csv / json / diff / code / bin |
| Tree state | `store.ts:343-576` (`FT`, `ftFetch`, `ftCrawl`, `ftMatches`, `ftReveal`, `openDir`) | ~230 lines |
| Per-turn delivery card | `TranscriptPage.tsx:966-1020` (`DeliveryTile`), `1022-1080` (`ArtsView`) | tiles with previews, capped at 3 |
| Delivery record | `transcript/store.ts:92-123` | `recordDelivery` / `deliveriesOf`, a `WeakMap<Lane, Map<turn, DeliveryRow[]>>` |
| Wire | `raven/agent/tools/deliver.py:168-186` | manifest on `ToolEvent.metadata.raven_delivery` |
| Replay | `raven/rpc/methods/session.py:289-307` | resume re-sends the manifest and stamps `missing` per file |
| Download | `raven/rpc/transports/deliverables.py` | `/files/download?token=`, `410` when the token is gone |
| Read-for-render | `raven/rpc/transports/ws.py:208` | `/file?path=`, the agent's own read policy |

Two facts that decide most of the design:

- **The session already carries its whole delivery history to the page.**
  `session.resume` returns raw stored messages (N stored -> N wire) with tool
  `metadata` intact, and it computes `missing` per file server-side. Nothing has to
  be fetched or invented to list a session's deliverables.
- **A deliverable is readable by the viewer.** `/file` resolves through the same
  `resolve_path` the filesystem tools use, and `deliver_files` resolved the path
  through that same function before registering it. So a delivered file renders in
  the pane; the token URL is the download capability, not the read path.

## Decisions

**D1. The shelf is the session's deliverables, not the turn's.**
The transcript keeps its per-turn "本轮交付" card unchanged. The shelf is the
roll-up of the same records across every turn of the open session, deduplicated by
path (a re-delivered file appears once, at its newest turn -- which is what the
server does too: `register()` reuses the token per conversation+path and refreshes
the size).

**D2. No new RPC. The list is a client-side selector over records the page already has.**
Live turns record from `ToolEvent.metadata` (`live/050-turn.js:124`), a reload
records from `session.resume`'s replay (`transcript/store.ts:1241`). Both already
happen today.
_Rejected:_ a `deliverables.list` RPC reading `deliverables.json`. It would add a
second source of truth for the same list, and the registry is keyed by
`conversation` = session_key, which the page would then have to keep in step with
its own session pointer. Worth revisiting only if we ever want deliverables from
*other* sessions in this page.

**D3. The delivery registry moves out of the transcript into `features/workspace/deliveries.ts`.**
Both readers need it: the transcript card and the desk shelf. Direction of imports
stays as it is today (`transcript/*` already imports `workspace/hunks` and
`workspace/store`), so nothing becomes cyclic.
Module shape: `record(turn, metadata)`, `ofTurn(turn)`, `all()`, `byPath(path)`,
`markMissing(path)`, `subscribe()`, `reset()`, `snapshot()/restore()`.
_Rejected:_ keeping it in `transcript/store.ts` and importing that from the desk --
`workspace -> transcript -> workspace` is a real cycle, and the desk would then
subscribe to the transcript's segment store for something that is not a segment.

**D4. The pane keeps one identity per path, and the delivery is looked up, not carried.**
`file:<path>` stays the pane id. Whether the click came from the shelf, from a
delivery tile in the transcript, from a changed-file row, or from a path chip in an
answer, it lands in one pane. The pane asks `deliveries.byPath(f.path)`; if it gets
a record, it draws the deliverable strip above the body.
_Rejected:_ a fifth pane kind `deliverable` with id `deliverable:<path>`. The same
file would then be able to occupy two panes at once, and the reader would have to
know which door they came through.

**D5. The detail popup IS the right-hand pane, at its existing two sizes.**
`.desk-pane` already floats over the page with its own header, close button and a
fullscreen toggle (`toggleSolo`), and it is where every file the page renders
already lands. Adding a centred modal would give the page a second file surface
with the same body and different chrome.
_Rejected:_ `<dialog>`-style centred modal. If we ever want it, `solo` is that
state already -- one pane, full window.

**D6. The viewer loses its tree and keeps its bar.**
`FileView` becomes `Fbar + FileBody` in one column. `Fbar` loses the tree toggle
and the "reveal in tree" context item; it keeps path, copy, render/source, new
tab, download, reveal-in-file-manager. `FileView`'s `onOpen` prop and
`deskStore.replacePaneFile` (`deskStore.ts:153`) die with the tree -- nothing else
navigates inside the viewer.

**D7. Rows are cheap; only the opened file is probed.**
The transcript tile HEADs its own URL per tile; the shelf must not, or opening the
tab fires one request per deliverable. Rows trust the `missing` flag the replay
stamped (fresh, because it is computed at resume) and live rows are trivially
present. The pane probes on open and calls `markMissing(path)` when the file is
gone, which is what turns the row grey. The kinds that never read text (an
image, a frame) have only a status-less `onError`, so they HEAD the URL for a
status first and mark only on 404 -- a 25 MB render cap, a refused path, or a
file the browser cannot decode are not absences.
_Rejected:_ a probe-all sweep when the tab opens. It is N requests for a list the
reader may only be scanning.

**D8. Two-line rows, no thumbnails.**
The palette is 250-480px wide (`deskGeometry.ts:8-13`); a tile grid does not fit
and a one-line row cannot carry both the human title and the file name. Line one:
title. Line two: `name · SIZE · turn`. The description stays in the pane and the
row's `title=` tooltip.

**D9. Folder links from an answer stop opening a tree.**
`openDir` (`store.ts:570`) exists only to unfold the tree at a folder. With the
tree gone, `DS.prose.open` routes a directory to `fs.reveal` when the gateway is
this desktop, and `prose.linkTargetOf` stops marking directories as links when it
is not (`live/170-workspace.js:53-61`). A link that opens nothing is worse than
plain text.

## The registry

```ts
// features/workspace/deliveries.ts
export interface DeliveryRow {          // unchanged shape, moved
  path: string; name: string; title: string; description: string
  ext: string; size: number; mediaType: string; downloadPath: string
  missing: boolean
  turn: number                          // NEW: which turn delivered it
}
```

- `record(turn, metadata)` parses `metadata.raven_delivery.files` exactly as
  `transcript/store.ts:92` does today, adds `turn`, keys by **(turn, path)**, and
  notifies. Per turn, not per path: `ofTurn` is what the transcript's own card
  reads, and a row that moved its turn forward when the file was delivered again
  took the earlier turn's card away with it. Collapsing a path to its newest
  delivery is the shelf's business, in `list`/`byPath`; `markMissing` marks every
  turn's row for the path, so no card disagrees with the shelf about one file.
- `list()` returns newest turn first, and inside a turn in arrival (manifest)
  order -- the rows are held in arrival order and sorted for reading, so no
  timestamp is needed and none is invented.
- Reset: `workspace.reset()` (already wired to session switch and new chat through
  `RavenIslands.workspace.reset`) clears it; the transcript's `history()` clears it
  before a replay, the way it clears `deliveriesByLane` today
  (`transcript/store.ts:1085`).
- **Park/restore:** the records join `WorkspaceSnapshot` (`workspace/types.ts:78`),
  because `live/060-parked.js:37,68` round-trips `workspace.snapshot()` and a
  parked session restored from it never replays history. Today the per-lane WeakMap
  survives that by accident (the lane object survives); a module-level registry
  would silently lose a live turn's deliveries on a session switch and back if the
  snapshot did not carry them. **This is the one regression the change can cause
  invisibly, and the test for it is in the plan below.**

## Surfaces

### The shelf (desk palette, `产物` tab)

```
        ┌─────────────────────────────────────────┐
        │  ◫ Diff   ▣ 产物 ③   ▢ 子智能体          │  ← drag strip, 48px
        ├─────────────────────────────────────────┤
        │  本轮                                    │  ← group divider (.wsgrp)
        │  ┌───────────────────────────────────┐  │
        │  │ MD  GTM agent 赛道对比             │  │  42px row
        │  │     gtm-compare.md · 1.8 KB       │  │
        │  ├───────────────────────────────────┤  │
        │  │ CSV 产品定价明细                    │  │
        │  │     pricing.csv · 936 B           │  │
        │  ├───────────────────────────────────┤  │
        │  │ PDF 官网摘录                       │  │
        │  │     source-notes.pdf · 86.3 KB    │  │
        │  └───────────────────────────────────┘  │
        │  较早                                    │
        │  ┌───────────────────────────────────┐  │
        │  │ GO  连接池修复                      │  │
        │  │     pool.go · 3.3 KB · 第 2 轮     │  │
        │  ├───────────────────────────────────┤  │
        │  │ XLS 市场分层            【已丢失】   │  │  ← missing: dimmed, not hidden
        │  │     market-map.xlsx               │  │
        │  └───────────────────────────────────┘  │
        │                                     ⋱   │  ← resize corner
        └─────────────────────────────────────────┘
```

Empty state, in the place the tree's empty note used to be:

```
        │                                         │
        │            ▣                            │
        │     这个会话还没有交付产物                 │
        │   Raven 交付文件后会出现在这里              │
        │                                         │
```

A `missing` row is dimmed and badged, and stays clickable: the viewer says "that
file is no longer there" in words, which is more than a dead row does. (The
transcript's own tile disables itself instead, because it probes per tile and
has a picture to withhold; the shelf has neither.)

### The detail pane (right side)

```
   ┌──────────────────────────────────── the chat ──────┐┌──────── pane ─────────────────────┐
   │                                                    ││ ▯ gtm-compare.md      ⤢  ×        │  43px header (unchanged)
   │   ... transcript ...                               ││───────────────────────────────────│
   │                                                    ││ GTM agent 赛道对比                 │
   │   ┌── 本轮交付 ─────────────────────┐               ││ 三家 GTM agent 产品的定位与定价对比   │  ← the strip, only for
   │   │ ▤ gtm-compare  ▤ pricing  ▤ ... │  ← tiles     ││ MD · 1.8 KB · 本轮交付             │    a path in the registry
   │   └────────────────────────────────┘  click ──────▶││───────────────────────────────────│
   │                                                    ││ research/gtm-compare.md   ⧉      │  ← Fbar: path, copy,
   │                                                    ││        [渲染|源码]  ⧉ ⤓ ⌂        │    render/source, tab,
   │                                                    ││───────────────────────────────────│    download, reveal
   │                                                    ││ # GTM agent 赛道对比               │
   │                                                    ││                                   │
   │                                                    ││ ## 结论                            │  ← FileBody, untouched
   │                                                    ││ Clay 与 11x 的差距主要在 ...        │
   └────────────────────────────────────────────────────┘└───────────────────────────────────┘
```

The same pane, opened from a path chip in an answer (not a deliverable) -- the
strip is simply absent:

```
   ┌──────── pane ─────────────────────┐
   │ ▯ pool.go             ⤢  ×        │
   │───────────────────────────────────│
   │ internal/db/pool.go   ⧉  ⤓ ⌂     │
   │───────────────────────────────────│
   │  1  package db                    │
   │  2                                │
```

States the pane must still show (all of them exist today, none may regress):
loading, `403` "confined to the workspace", `404` gone, `413` too large, binary
(`BinNote`: open-with picker + reveal + download + copy path), image, pdf/html in a
sandboxed frame, csv table, json tree with its node budget, diff colouring.

## Deletions

| File | Goes | ~Lines |
|---|---|---|
| `workspace/store.ts` | `FT`, `FTW_KEY`, `isErr`, `ftReset`, `ftJoin`, `ftAbs`, `relToRoot`, `ftFetch`, `ftLoad`, `ftLoadVisible`, `FT_SHOW_MAX`, `ftCrawl`, `ftMatches`, `ftOpenTo`, `ftReveal`, `ftQuery`, `ftKindOf`, `openDir` | ~235 |
| `workspace/WorkspacePage.tsx` | `FtPane`, `FtLeaf`, `FtResults`, `FtRows`, `Grip`, `FT_ICO.folder`, the tree toggle in `Fbar`, the `file` launch card | ~270 |
| `workspace/DeskPalette.tsx` | `DeskTree`, `FileNav` (replaced by `DeliverablesNav`, ~60 lines) | ~75 |
| `workspace/deskStore.ts` | `replacePaneFile` | ~15 |
| `workspace/types.ts` | `FtEntry`, `FtKids`, `WorkspaceSource.list` | ~10 |
| `live/170-workspace.js` | `DS.workspace.list`, the `wsRoot`-from-`fs.list` learn, the dir branch of `DS.prose.open` | ~15 |
| `styles/page.css` | `.ftree`, `.ftq`, `.ftlist`, `.ftrow`, `.frow`, `.fpane`, `.grip`, `.tree-chev`, `.desk-search` | ~45 |
| `workspace/WorkspacePage.test.tsx` | the four tree tests (`draws the tree...`, `hides the tree...`, `shows the read failure in the tree...`, `keeps the demo file tab...`) | ~70 |

Not deleted, on purpose: `fs.list` on the runtime side. It is a protocol method
with a schema and a generated client; retiring it is a separate change with its own
compatibility question (an older page talking to a newer gateway). The page simply
stops calling it.

## Retained render paths -- the regression list

Every one of these must still open a file in the pane after the change. This is the
list to walk before calling the change done:

1. A delivery tile in the transcript (`TranscriptPage.tsx:1016`, `openDelivery`).
2. A changed-file row in the turn's products card (`TranscriptPage.tsx:1052`).
3. An attachment chip on the reader's own message (`TranscriptPage.tsx:694`).
4. A file name in a tool-call card header (`DtlHead`, `TranscriptPage.tsx:206`).
5. A path or markdown link inside an answer (`DS.prose.open` -> `showFile`).
6. A row in the diff tab of the desk, and its context menu's "open"
   (`WorkspacePage.tsx:115`).
7. A row in the new shelf.

## Hazards

**H1. `shortPath` loses its root.** `DS.workspace.shortPath` shortens a path against
`wsRoot`, and `wsRoot` is learned from `fs.list`'s answer (`live/170-workspace.js:15-21`).
Stop calling `fs.list` and every path in the viewer bar and in the transcript's edit
rows silently gets longer. Fix in the same change: take the root from
`session.resume`/`session.create`'s `info.cwd`, which `live/080-overrides.js`
already has in hand.

`info.cwd` had to grow up to carry it. It was `os.getcwd()`, while the root
`fs.list` reported came from `WorkdirResolver` -- `explicit -w` or the session's
persisted `workdir` override or the policy default, and `raven serve` resolves
**per channel**, so a `tui:` session with nothing pinned does not run in the
launch directory at all. `_session_cwd` now asks the resolver
(`peek_session_workdir`, the read-only form the file panel already used), which
is one question instead of three guesses; `os.getcwd()` remains the answer for a
caller with no loop to ask.

**H2. Park/restore.** See the registry section. Add deliveries to
`snapshot()/restore()` in the same commit that moves the registry, not after.

**H3. The demo layer.** The fixture source has no `canBrowse`, and the fixtures'
delivery entries carry no `download_path` (`demo/030-fixtures.js:181-192`). The
shelf must render from fixtures (three sessions already deliver files) and a click
must keep falling back to `source().openPath` -> the demo toast. Do not gate the
shelf on `canBrowse`: the list is not a file read.

**H4. `canBrowse` now means "can read a file", not "can browse".** Left named as
it is, with the contract saying what it means, rather than renaming a seam flag
the demo and live sources both set in the same change that removes 790 lines
around it. Worth the rename on its own later.

**H5. Generated files and gates.** `i18n/messages.json` is inlined into the page by
`ui/build.py:153` and generated into `ui-tui/src/i18n/messages.generated.ts`; a new
key without `npm run gen:i18n --prefix ui-tui` fails `make lint-tui`. The CSS gate
(`ui/scripts/check-css.mjs`) rejects any colour literal outside a `--token:`
definition, so the new rows use existing tokens only.

**H6. The desk's own persistence.** `DESK_OPEN_KEY` stores only whether the palette
is open, and `DESK_GEOMETRY_KEY` holds a rectangle -- neither can strand a tab id.

**This premise moved while the branch was open.** `!294` landed on `main` and
now keeps the desk's layout per conversation in `sessionStorage`, `tab`
included, and `applyLayout` puts it back -- reached from `shell/resume.ts`, which
is the one door into `state.tab` that does not go through `openDeskTab`'s guard.
A note written by the previous bundle can therefore name the retired `file` tab,
and it restored exactly the fall-through that guard exists to stop: no tab
selected, palette body on the agents list. Closed twice over, because the two
halves answer different sources: the slot's version goes to 2 (the mechanism
`persist.ts` documents for a shape change, which is what retiring a stored tab id
is), and `applyLayout` validates the tab against the three that exist (which also
holds for a note edited by hand, or written by a build the reader rolled back to
and forward from). Both are pinned by a test that reds when that half alone is
reverted.

## What was run

- `npm test --prefix ui` -- 66 files, 737 tests, all passing (19 of them new:
  the registry, the shelf and the pane strip).
- Mutation-checked, one at a time: reversing the shelf's sort, dropping the tab
  count, dropping the shelf's subscription, dropping the tab guard, giving every
  file a strip, dropping the pane's subscription, marking a deliverable missing
  on any failed read, and dropping the snapshot line. Each fails one test and
  only one; each passes again when put back.
- `npm run type-check --prefix ui`, `npm run gen:check --prefix ui`,
  `npm run lint --prefix ui-tui`, `npm run lint:i18n --prefix ui-tui`,
  `npm run type-check --prefix ui-tui`.
- `node ui/scripts/check-css.mjs`, `python3 ui/build.py` +
  `node ui/scripts/check-page.mjs`, `node ui/scripts/count-shared-globals.mjs`
  (0 / 0 / 18, unchanged).
- Against the demo shell (`?stub=1`): the GTM session's five deliverables list
  under one turn group with the count on the tab, and a click reaches the demo's
  own note rather than opening a viewer that cannot read.
- Against a running `raven serve`: the shelf lists, the pane opens with its
  strip (title, description, `MD 7.9 KB 本轮交付`), a deliverable whose file was
  removed opens saying so and turns its row grey, a file that was never
  delivered opens with no strip, the rendered/source toggle still flips, and
  `shortPath` shortens against the session's cwd with `~/` outside it.

## i18n delta

Added:

| key | en | zh |
|---|---|---|
| `gui.ws.deliverables` | Deliverables | 产物 |
| `gui.ws.dlv_none` | Nothing delivered yet | 这个会话还没有交付产物 |
| `gui.ws.dlv_none_sub` | Files Raven hands over show up here | Raven 交付的文件会出现在这里 |
| `gui.ws.dlv_turn` | turn {n} | 第 {n} 轮 |
| `gui.ws.dlv_here` | delivered this turn | 本轮交付 |
| `gui.ws.file_unreadable` | This view cannot read files here | 这里没法读取文件 |

The file-gone message the viewer already had (`gui.ws.file_gone`) covers the
opened-and-missing case, so the shelf needed no key of its own for it.
`gui.ws.file_unreadable` replaces `gui.ws.dir_empty` ("Empty directory"), which
was the tree's answer and said the wrong thing once there was no tree.

Retired with the browser: `gui.ws.files`, `gui.ws.sub.files`, `gui.ws.search`,
`gui.ws.no_match`, `gui.ws.searching`, `gui.ws.search_capped`,
`gui.ws.search_more`, `gui.ws.tree_show`, `gui.ws.tree_hide`,
`gui.ws.tree_resize`, `gui.ws.reveal`, `gui.ws.dir_expand`,
`gui.ws.dir_collapse`, `gui.ws.read_fail`.

## Domain terms

No new term. The runtime glossary already defines **Deliverable** and **Delivery
Manifest** (`CONTEXT.md`), and this change is those two terms reaching one more
surface. UI code and the shelf's identifiers use `deliverable`/`delivery`; 产物
is the Chinese display string only.
