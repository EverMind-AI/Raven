# Web UI

The browser front-end (`ui-web/`, React + TypeScript, one page). Renders the chat
transcript, the rail and the module pages; talks to the Runtime only over the RPC
protocol. Assembled into a single `dist/index.html` by `build.py`, which inlines
the stylesheet and the one JavaScript chunk Vite builds. Which directory holds
what -- `app`, `rpc`, `state`, `chrome`, `features`, `components`, `lib` -- is
the Layout table in `README.md`; what follows is the vocabulary.

## Language

**Gateway**:
The page's one data entry point, `gateway()` (`src/rpc/gateway.ts`): the
`RpcTransport` every call and every push goes through. Held in one module slot,
installed once by `src/main.tsx` before anything can ask for it.
_Avoid_: "the socket" for this -- a socket is one of the two things that can be
behind it.

**Transport**:
An implementation of `RpcTransport` (`src/rpc/transport.ts`): the WebSocket to a
real raven (`WsTransport`), the offline fixture library (`FixtureTransport`), or
a canvas layered over either (`OverrideTransport`). Which one this page got is
decided once, from the URL, by `chooseTransport()`
(`src/rpc/chooseTransport.ts`).

**Source**:
`features/<domain>/source.ts` -- everything one domain knows about speaking to
the gateway, plus the pure functions that map an answer into the shape that
domain's renderer reads. One per domain, and the only place in it a
`gateway()` call may be written (`scripts/gates/rpc-names.test.mjs`); the
renderer beside it never calls the gateway itself. Installed onto the `sources`
seam (`src/state/sources.ts`) by the page's own lifecycle -- `src/app/install.ts`
for all but one, and `src/app/boot.ts`'s claim on the first frame for the
session source, which everything below it reads. Nothing outside `src/app/`
assigns a member of the seam. A domain is asked for by its key: `ds('cron')`
answers `Sources['cron']`, so a domain renamed or misspelt is a compile error
rather than a throw at the first paint that reads it.

**Fixtures**:
The offline answer library, `src/rpc/fixtures/` -- one responder per domain,
answering the same contract a real gateway does, with its clock injected so two
runs of the same request are byte-identical. What `?stub=1` and a page opened
from `file://` read: there is no second source layer and no demo-mode branch in
any `source.ts`.

**Capabilities**:
`src/rpc/capabilities.ts` -- the one place that answers "this gateway is too old
to serve that". `absorb()` records what `system.hello` declared, `gone()` records
a method that answered `-32601`, and each named predicate (`hasStillOnDisk`,
`hasUpdateFlag`, ...) is one tolerance a caller would otherwise spell inline.
Type checking cannot cover this: a method the contract declares and this
particular gateway does not serve is still a valid name.

**SessionRuntime**:
Everything one conversation holds while the page is open
(`src/state/session/runtime.ts`): the turn's phase, the step that is open, the
calls in flight, its subscription id, its send queue, its naming timer, and the
model, tier and permission mode a draft staged before it had a session to write
them under. One per session key, plus one for the draft that has no key yet. A
frame names its subscription and the subscription names its runtime, so "which
conversation is this about" is a lookup rather than a page-level flag.

**Registry**:
`src/state/session/registry.ts` -- the map from session key and subscription id
to `SessionRuntime`, the `active` pointer saying which one the page is showing,
and the switch between them. A switch takes a view ticket, so an answer to an
open the reader has already left is dropped rather than painted.

**Residency**:
Whether a conversation keeps its runtime while it is off screen
(`src/state/session/residency.ts`). The active conversation and any with a turn
in flight are resident, and a resident conversation also keeps the detached DOM
host its transcript lane is mounted in; everything else is released on the way
out and read back from disk on return.

**Pipeline**:
`src/state/session/pipeline.ts` -- the one consumer of the `event` frame and of
the five side-channel requests that block a turn. It routes a frame to the
runtime its subscription names and hands it to the stage table.

**Stage**:
One entry of `STAGES` (`src/state/session/stages.ts`): the names of the
`TurnEvent` kinds it handles, and what each does to the runtime it is given.
The table is applied in order and is exhaustive over `TurnEvent['type']` --
`assertNever` makes a new member a compile error, and
`scripts/gates/pipeline-coverage.test.mjs` holds the union of `handles` equal
to it.
An event the page deliberately does not render still has a stage, with an empty
body.

**Region**:
One of the sixteen things `src/App.tsx` renders at the body, in the standing
order `src/state/portals.ts`'s `BOOT_BODY_ORDER` declares: `div.app`, the
collapse's twin, the seven module pages, the four veils, the shared drawer and
the two standing hosts. A region is markup plus the flags its store writes --
never its contents: a region that is shared ground (`#capsBody`, `#wsBody`,
`#list`, `#stage`, ...) is rendered with no children at all, because an island
root or a writer fills it. `src/test/__golden__/region-*.txt` holds one golden
per region, and `src/App.test.tsx` holds the order.
_Avoid_: "layer" for this -- a layer is one of the four things `host()` appends.

**Module page**:
One row of `src/state/pages.ts`: a `<section>` id, the empty box its island
fills, the rail button it lights, its rank in the Escape chain, and the keys its
heading and accessible name speak. Seven rows, in the order they sit among the
body's children -- and every table that names a page derives from them:
`src/App.tsx`'s sections, `state/page.ts`'s `PageId` and open flags,
`state/overlays.ts`'s Escape rows, `state/portals.ts`'s body order,
`chrome/Rail.tsx`'s nav strip, `features/rail/store.ts`'s marks and
`src/test/regions.test.ts`'s goldens. Adding a page is adding a row.
_Avoid_: "page" for the whole document, or for a dialog -- the settings dialog
and the model picker are overlays, not module pages.

**Domain manifest**:
`features/<domain>/manifest.ts` -- what one domain declares about itself: its
name, the module page it owns, the seam keys it answers, and the root
`src/main.tsx` mounts for it. `features/manifests.ts` is the assembly point that
reads all nineteen; no domain may read it back.
`scripts/gates/domain-shape.test.mjs` holds the files a domain has and
`domain-registration.test.mjs` holds the manifest against the page table and the
seam.

**Chrome component**:
A component under `src/chrome/` -- the page's own furniture (the rail, the chat
header, the dock, the sheet rack, the tooltip, the two chips and their
popovers), as opposed to a feature island under `src/features/<domain>/`. A
chrome component renders markup the whole page shares and often hands a
container to somebody else; an island renders one domain's data into a
container it was given.

**Portal**:
An element that sits at the body rather than inside a page, and which of the
three kinds it is -- `static` (rendered there from the root's first commit),
`reparent` (born in a page, moved to the body on first open), `append` (created
at runtime). `src/state/portals.ts` is the table: thirteen rows, their `--z`
step, and their place among the body's children. It is the table rather than
the stylesheet that decides two of them, because two steps of the `--z` ladder
are deliberate ties -- for those four elements DOM order at the body IS the
stacking decision. `host()` hands out the four standing layers in table order
rather than in the order they are asked for.

**Escape order**:
The ordered table in `src/state/overlays.ts`: fourteen layers that can be on
screen at once, and which one an Escape takes back. A table rather than a
stack, because each entry answers "am I open" when the key arrives -- the
channel dialog opens over the entries page and closes first, while the shared
drawer opens over the dialog and closes second. Three capture-phase handlers
the docked sheets register run ahead of it, and two of them do not stop
propagation, so one Escape can both deny an approval and interrupt the turn
behind it.

**Menu**:
`src/state/menu.ts` plus `<ContextMenu/>` (`src/App.tsx`) -- the one context
menu the whole page shares: a single standing `div#menu`, with the rows of
whatever raised it portaled into it. The store holds the host it was raised in
and those rows; `data-open` and the `left` / `top` are written on the element,
because the placement is measured after the rows are in it and a menu measured
empty would be placed as if it had none. The host is remembered rather than
looked up again, so a region redrawn under an open menu takes down the rows the
reader is pointing at rather than a fresh host.
_Avoid_: "menu" for the model picker or for the dock's chips -- each of those
opens a popover of its own.

**Right-click rule**:
`src/state/contextMenu.ts` -- which menu a right-click gets: the host's own
inside a text field or over a real selection, this page's over a surface that
declares actions, and nothing anywhere else. One document listener rather than
a handler per surface, and a surface declares its actions as a function on
`_ctx`, so the rows are built from what is true when the gesture happens rather
than from what was true when the surface was drawn.

**Language store**:
`src/state/lang/store.ts` -- the page's language, the catalogue behind it, and
the one notification a pick sends. The language is resolved as the module loads
-- the reader's remembered pick, otherwise what `<html lang>` declares -- so
`get().lang` is never null and the first frame is one language rather than the
served markup with words from another drawn over it. Two groups hear a pick:
`subscribe` is what a component reads through `useSyncExternalStore`, so every
region and every island re-renders its own words, and `onApplied` is for
everything that is drawn rather than rendered. `get().picked` says whether a
language was chosen rather than inherited from the document, and it decides one
thing: `attr(key)` answers `undefined` until a pick lands, because an attribute
the served markup does not carry is one applyI18n would not have written
either.

**Language repaint**:
`src/state/lang/effects.ts` -- the twelve steps a pick asks of everything that
draws itself rather than rendering: the rail's own draw and the capabilities
page's, three stores whose draw commits a field their component renders (the
permission chip, the rail foot, the context ring), the model label -- the one
step left that writes an element by id -- the settings dialog's epoch, the nav
flyout's marks, the transcript's per-lane version bump, the composer's queue,
the shared drawer and one conversation reload. An island that only needs to
re-render is not in it: each `<Domain>App` subscribes to the language itself
(`scripts/gates/island-lang.test.mjs`). Subscribed once, from `src/main.tsx`,
through `lang.onApplied`, so it runs after the rendered half has committed. The
order is pinned by `src/state/lang/effects.test.ts`; the reload is last because
it rebuilds the conversation from disk.

**Inert marker**:
A `data-i18n`, `data-i18n-ph`, `data-i18n-tip`, `data-i18n-aria` or
`data-i18n-title` attribute on rendered markup. Nothing reads them any more --
each element's own `t(key)` (or `lang.attr(key)` for the attributes the served
markup did not carry) is what fills it -- and they stay because the CSS
namespace gate and the region goldens record them, and because they say which
phrase belongs to which key at the point a reader edits the JSX.

**Sheet rack**:
`src/state/sheetRack.ts` plus `src/chrome/SheetRack.tsx` -- what docks above the
composer (a clarify question, an approval request, a dag graph), filed under the
session it was raised in and mounted only while that session is open. A parked
sheet keeps its element and loses its interior, which is why what the reader
typed into one lives in `src/state/sheetDrafts.ts` rather than in the input.

**Global listeners**:
`src/state/globalListeners.ts` -- every listener the page holds on the document
or the window, in one function, in the order it registers them. The order is a
contract (three are capture-phase, and inside one phase the first registered
runs first), so it is one place and `src/state/globalListeners.test.ts` asserts
it call for call. A control's own handler is not here: that belongs with the
control.

**Island host**:
One of the three detached nodes in `src/features/hosts.ts` that an island root
renders into and a tab module re-attaches on every draw. Every other root is
given a container `src/App.tsx` rendered or a layer `host()` hands out; these
three cannot be rendered, because the capabilities page's two tabs clear their
box with `innerHTML` and React must own neither.
_Avoid_: "host" for the container a region renders empty -- that is shared
ground, and the island roots itself in it.

**Page callback**:
One of the three slots `src/state/page.ts` declares and `src/app/install.ts`
fills: what a page switch spends on an island (the rail's mark, the channel
dialog's close, the new-job sheet's close), in the order `show` spells out.
Registered rather than imported, because `src/state/` does not import
`src/features/` -- which is also how a case names the slots it is about.
