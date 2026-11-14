# ui-web search dialog

## Intent

The rail's search used to be an inline row: `#findBtn` revealed `#findBox`
under the rail navigation, and the term filtered the conversation list in
place (`state/find.ts`, `features/rail/RailPage.tsx`). The reader wanted the
web frontend to open a centered search dialog instead, the way the
documentation site's search does: a card over a dimmed page, one field,
results below it.

The docs site is the reference, not the target: it already renders that dialog
(`docs-site/docs/stylesheets/evermind.css`, `[data-md-toggle="search"]:checked`).
This change brings the same structure to ui-web and gives it a keyboard
shortcut.

## Goals

- Clicking `#findBtn` opens a centered dialog over a scrim.
- Cmd+K opens the same dialog; Cmd+F stays an alias, as it is today.
- Results are conversation cards: title, the row's `last` line as the excerpt,
  and the matched substrings marked.
- The list is keyboard-drivable: ArrowUp / ArrowDown move the active row,
  Enter opens it, Escape closes the dialog, a scrim click closes it too.
- Closing clears the term and the active row, so the next opening starts fresh.
- Opening a result opens that conversation and closes the dialog.

## Non-goals

- Searching inside message bodies. No RPC answers that today; it is a backend
  project of its own.
- Jumping to module pages, agents or actions. That is the command-palette
  scope, and it can grow on top of this dialog later.
- Any change to the docs site.

## Constraints the change has to respect

- Import direction (`ui-web/scripts/gates/import-direction.test.mjs`):
  `state/` may not import `features/` at run time; `chrome/` may.
- The Escape chain is an ordered table (`ui-web/src/state/escapeOrder.ts`), and
  every page-level keydown is registered through
  `ui-web/src/state/globalListeners.ts`, whose order test pins the sequence.
- `App.tsx` regions are golden-tested (`ui-web/src/test/__golden__/region-*.txt`,
  `ui-web/src/test/regions.test.ts`); a new region needs its name in `REGIONS`
  and its golden minted on a developer machine.
- `i18n/messages.json` feeds the TUI as well. `gui.rail.search_hits` and
  `gui.rail.no_hits` belong to the TUI's own rail search and stay in the
  catalogue even though ui-web stops reading them.
- The search field stays uncontrolled (no `value` prop): what the store owns is
  the lowercased term, and the browser keeps the caret and the IME state.
- Source comments are English and are added only where the code cannot say the
  thing itself.

## Approaches considered

- **A. Conversation dialog.** Keep the existing search target (title plus the
  row's `last` line), move it into a dialog. No backend work. This is the one
  chosen.
- **B. Global command palette.** A's dialog plus jump targets for pages,
  agents and actions. Needs a searchable entry table and a set of actions, and
  it is a larger surface to keep correct.
- **C. Conversation full-text search.** Needs a server-side index and RPC.

## Design

### State

`state/find.ts` grows from `{ open, query }` to `{ open, query, active }`.

- `toggle(force?)` opens or closes the dialog. Opening focuses `#sfind`;
  closing resets `query` and `active`.
- `term()` is unchanged, still lowercased and trimmed at write time.
- `move(delta)` moves `active` within the current result list and wraps at both
  ends; `active` is clamped to the result count whenever the term changes.
- `activate()` opens the conversation the active result points at and closes.

The input keeps its native `input` and `keydown` listeners. The `blur` handler
that closed the row on an empty term goes away: in a dialog, clicking a result
blurs the field, and that must not close it. Arrow, Enter and Escape are read in
the field's `keydown`; Escape stops propagation so the page chain does not also
take a layer down behind the dialog.

### Opening a conversation

`features/rail/store.ts` gains `openSession(row)`: the three steps
`RailPage.Row.go()` runs today (`page.show(null)`, then `setCurrent` and
`store.open` when the row is not the current one). The row and the dialog both
call it, so the two entry points cannot drift.

### Component

`chrome/SearchDialog.tsx` renders a veil (`#findVeil`) with the card inside it,
following the existing veil pattern in `App.tsx` (scrim click closes via
`useScrim`). It subscribes to `state/find.ts` and to `features/rail/store.ts`,
which `chrome/` may reach.

Structure:

```
#findVeil.veil[data-open]
  .findcard[role=dialog][aria-modal=true][aria-label=Search sessions]
    .findfield
      magnifier · input#sfind · button#sclr
    .findcount        "N matching conversations"
    .findlist[role=listbox]
      .findhit[role=option][aria-selected]  (×N)
        .t   title, matched substrings in <mark>
        .x   last line, matched substrings in <mark>
```

An empty result list renders `gui.find.empty` in place of the rows.

Highlighting is one helper in the component: escape the term for a regular
expression, split the text on a case-insensitive match, wrap the hits in
`<mark>`. No `dangerouslySetInnerHTML`.

### Mounting

`App.tsx` renders `<SearchDialog />` after `#exportVeil`, as a region named
`findVeil`. `regions.test.ts` gains `findVeil` in `REGIONS`, and
`region-findVeil.txt` is minted on this machine and committed.

### Rail

`chrome/Rail.tsx` loses `FindRow`; `#findBtn` keeps its place, calls
`find.toggle()`, and reports `aria-expanded` from `find.get().open`.

`features/rail/RailPage.tsx` stops filtering by `find.term()`: the rail always
draws the full grouped list. The search-hit label and the no-hits note are
removed from that component; the two catalogue keys stay for the TUI.

### Shortcut

`state/globalListeners.ts`'s `onEscapeOrder` gains a Cmd/Ctrl+K branch that opens
the dialog; the existing Cmd/Ctrl+F branch points at the same call instead of
`toggleFind(true)`, and no longer forces the rail open. The order table's label
for that listener changes from "three shortcuts" to "four", and the matching
case in `state/globalListeners.test.ts` is updated.

`state/escapeOrder.ts` gains the dialog as the first entry of `ABOVE`: it stands
over every other layer except the image lightbox.

### Styles

All in `ui-web/src/styles/page.css`, on tokens only, so both themes fall out of
the existing ramps:

- scrim: the veil's (`--scrim-soft`, 3px blur), so the dialog reads as one of
  the page's own overlays rather than a second design;
- card: `min(640px, calc(100vw - 32px))`, top-aligned at 20vh, 12px radius,
  `--line` hairline and `--shadow`;
- field row: 44px, hairline below;
- count row: `--faint`, 11px, on `--surface`;
- result cards: title 13.5px, excerpt 12px `--muted`, `mark` on `--amber-dp`;
- active row: `color-mix(in srgb, var(--warm) 12%, transparent)`.

### Copy

New keys in `i18n/messages.json` (English and Chinese):

- `gui.find.count` — "{n} matching conversations" / "匹配的会话 · {n}"
- `gui.find.empty` — "No conversation matches that." / "没有匹配的会话。"

Reused: `gui.search_sessions` for the field's placeholder and label,
`gui.clear_search` for the clear button.

## Tests

Written first, red before green:

- `state/find.test.ts`: open and close, term normalization, `move()` wrapping,
  `activate()`, the reset on close, focus on open.
- `chrome/SearchDialog.test.tsx`: the count line, a highlighted title and
  excerpt, the empty state, Enter and a click both calling `openSession`, Escape
  and the scrim closing.
- `features/rail/RailPage.test.tsx`: the term no longer filters the list; the
  two search-hit cases move to the dialog.
- `chrome/Rail.test.tsx`: no `#findBox`; `#findBtn` reports `aria-expanded`.
- `App.test.tsx`: the served placeholder still reads `gui.search_sessions`.
- `state/globalsListeners.test.ts` and `state/escapeOrder.test.ts`: the updated
  order label and the new layer.
- `src/test/regions.test.ts`: the new golden.

Gates run before push: `npm test` (ui-web), `npx tsc --noEmit`, the import
direction and domain registration gates, and the page build.

## Rollback

Front end only, no data or protocol change: revert the commit.
