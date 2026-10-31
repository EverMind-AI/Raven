# Web UI

The browser front-end (`ui-web/`, React + TypeScript, one page). Renders the chat
transcript, the rail and the module pages; talks to the Runtime only over the RPC
protocol. Assembled into a single `dist/index.html` by `build.py`, which inlines
the stylesheet and the one JavaScript chunk Vite builds. Which directory holds
what -- `app`, `rpc`, `state`, `chrome`, `features`, `components`, `lib` -- is
the Layout table in `README.md`, and the conventions a domain follows are
`CONTRIBUTING.md`; what follows is the vocabulary.

## Language

**Domain**:
One directory under `src/features/`, and everything the page knows about one
subject: its contract types, everything it knows about the gateway, its state,
its components and the one declaration the page reads it through
(`features/knowledge/` is the shape). Eighteen of them. The directory name is the
domain's name everywhere else as well -- the seam key, the i18n namespace, the
DOM id prefix, the component prefix and the CSS prefix are the same word -- so
nothing has to be translated to be found. `scripts/gates/domain-shape.test.mjs`
holds the files a domain has; `CONTRIBUTING.md` section 2 is the skeleton.
_Avoid_: "module" for this -- a module is one file, and a module page is the
section a domain fills.

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

**Seam**:
An interface point the page fills in and everything below reads through, where
an import would point the wrong way: the `sources` object
(`src/state/sources.ts`), the three page callbacks (`src/state/page.ts`), the
pane the islands drawn inside it ask questions of (`src/state/wsPane.ts`), the
desk handed to the two stores that open something in it (`src/main.tsx`). Each
exists because the direct import would run up a layer or close a cycle that
evaluates while those modules are still being built. What fills a seam is the
page's own wiring (`src/app/`, `src/main.tsx`); what reads one asks by key or
calls what it was handed, and never reaches for the module behind it.

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
The offline answer library, `src/rpc/fixtures/` -- one module per wire
namespace rather than per domain, because a domain may speak several and one
speaks none, answering the same contract a real gateway does, with its clock
injected so two runs of the same request are byte-identical. What `?stub=1` and
a page opened from `file://` read: there is no second source layer and no
demo-mode branch in any `source.ts`. An answer is typed rather than cast --
`Wire<ResultOf<M>>` (`src/rpc/fixtureTransport.ts`) is the contract widened
only where the gateway really sends null -- and three gates hold the library:
`fixture-shape` against the contract, `offline-coverage` against the methods
the page calls, `fixture-now` against a clock of its own.

**Capabilities**:
`src/rpc/capabilities.ts` -- the one place that answers "this gateway is too old
to serve that". `absorb()` records what `system.hello` declared, `gone()` records
a method that answered `-32601`, and each named predicate (`hasStillOnDisk`,
`hasUpdateFlag`, ...) is one tolerance a caller would otherwise spell inline.
Type checking cannot cover this: a method the contract declares and this
particular gateway does not serve is still a valid name.

**Store**:
A module singleton holding one value, made by `makeStore<T>`
(`src/state/store.ts`): `get()` is the snapshot, `set(next)` writes it and
notifies inside `flushSync`, `subscribe(fn)` is what `useSyncExternalStore` is
given, and `_resetForTests()` puts the value back without notifying. The
synchronous notify is the contract rather than a detail -- a caller commits and
then hands over, and the React roots among the listeners have painted by the
time `set` returns. A singleton rather than a factory because half the callers
are not React: the Escape order, the language effects and the session pipeline
all read and write from outside any component.
`scripts/gates/store-shape.test.mjs` holds every subscribable module to the
shape and names the eight that keep a listener set of their own, with what one
value and one notifying `set` cannot express for each.
_Avoid_: `getState`, `put`, `commit`, `patch` -- one page, one set of verbs
(`CONTRIBUTING.md` section 3.1).

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

**Lane**:
One conversation's transcript stream, as `features/transcript/types.ts` declares
it: its key, whether it is the main one, its epoch, its segments, its own
listener set, and the buffer a streaming say flushes once a frame. One React
root per lane rather than one per page (`features/transcript/mount.tsx`), which
is why the transcript's store keeps a listener set per lane -- a token appended
to the open step re-renders that leaf and nothing else -- and why a resident
conversation keeps the detached host its lane is mounted in.

**Turn Nav**:
The strip of marks at the chat column's right edge, one per turn of the
conversation on the stage, drawn by `features/transcript/TurnNav.tsx` under the
domain's root (`TranscriptApp`) into `#turnNav` -- the box
`src/chrome/ChatTop.tsx` renders after `#scroll` and hands over empty, and
which the stylesheet parks with the scroller. A mark is a question that opened
a turn (an `ask` segment without `midTurn`), counted from the first one the
lane holds; the mark being read is the last question scrolled past the reading
line. Pointing at or focusing a mark shows the turn's number, how long ago it
was asked and what was asked; a click scrolls the question into view. It draws
nothing below two turns; a strip longer than its band scrolls with no thumb
(`data-thumb="none"`), centred on the mark being read, and fades at either end
instead. A single root, not one per lane: it reads the lane whose host is on
`#stage` (`mount.tsx`'s `shownLane`), never one held off it.
_Avoid_: "minimap" -- it marks turns, not a scaled copy of the transcript.

**Trajectory View**:
The chat column's second view: the conversation's trace entries as an agent
waterfall, in place of the transcript, drawn by `features/trajectory/` into
`#trajHost` -- the box `src/chrome/ChatTop.tsx` renders beside `#scroll` and
hands over empty. While it is up the composer card, the task strip and the
desk step aside: `ChatTop.tsx` sets `data-traj` on the document and the
stylesheet hides them under it. The sheet rack stays, so an approval, confirm
or clarify sheet the open conversation raises is still in front of the reader,
who would otherwise answer one they cannot see with the Escape that closes the
details pane.
Switching to it parks the scroller (`#scroll[data-parked]`: out of the flow,
hidden, its size and scroll position kept) rather than unmounting it, so the
transcript goes on streaming underneath and comes back exactly where it was.
The header's toggle (`#trajBtn`) appears only when the gateway announced
`trajectory-v1` (every gateway does while its trajectory policy is on, which is the default), `trajectory.state` says the view is on, a conversation is open
and that conversation is not in its empty state (`state/session/conversation.ts`'s
flag); the store's `available` is that one condition.
The waterfall (`features/trajectory/waterfall/`) draws every entry the list
store holds (`entries`), the ones the index marked `meta.hidden` included: grouped
by agent -- the main session first, then one group per worker, a worker's runs
folded into one by instance, else by name, where runs of one name that overlap
in time are different agents and keep apart (`model.ts`'s `agentIdentity` and
`foldAgents`) -- then by turn, then by span. A worker's mark is picked by that same recorded name
(`meta.agent`, else `meta.instance`): an entry carries no preset, so the
preset key `components/AgentMark.tsx` is built on is not available here.
A worker's row is named by its task; open, it adds a second line -- the agent
that ran it, the turn that sent it and how many turns it took. Every worker is
a sub-agent, an external agent's run included: the header counts the main
agent and its sub-agents, and only a worker's mark says who ran it.
Every agent shares one time axis (`axis.ts`): a stretch
in which no agent was doing anything collapses to a fixed width. Each row
prints its busy time in a column at the end of its label -- for an agent, its
turns and loose rows merged (`model.ts`'s `busyRuns`), never its lifetime.
Above the rows sit the activity strip -- the whole session's calls per bucket of
real time, its idle stretches folded wide enough to say how long they were
while the folds keep to a third of it, and the window as a brush a drag takes,
moves or stretches (`ActivityStrip.tsx`) --
and the ruler, marked on round clock times (`ticks.ts`) beside the name of the
window it shows; what
crosses between agents is marked on rows -- a dispatching row's door arrow, and
the worker's name on the host turn its report started -- and drawn only once a
worker is open: a line from the instant it was sent, on its dispatcher's row,
to where its work begins. Agents start folded when there are several, one row each: a capsule
per run of work on a faint line from the first to the last, the main agent in
gold, a diamond where a failure happened, an arrow at an edge its work runs
past, and a chip naming when it works next once none of it is in the window.
A lone agent starts open, and turns start folded; a plain wheel scrolls, ctrl
or cmd with the wheel zooms about the pointer (read against the ruler, which
spans the axis exactly), and shift with the wheel pans. The header's presets
set a window of three hours, one hour or fifteen minutes about the middle of
the one on screen -- a middle in an idle fold moves to the work beside it --
and Fit gives the whole session back; Collapse all folds every agent and turn.
Once the window is narrowed, the workers whose work lies wholly before it, or
wholly after it, step into one fold row for each side ("2 agents finished
earlier"), which opens in place; the main agent always keeps its own row.
The zoomed window and a
stretch picked on the ruler are kept in real time for the conversation they
were made on, so a live session growing past its end does not reset them.
Escape takes the picked stretch back through the one ordered table (before the
details pane, as the bar's own popovers were), and the arrow keys walk only the
entries that stretch leaves drawn; a selection made elsewhere -- a link in the
pane -- drops a pick that would hide it.
The rows are a tree for assistive technology: agents, turns and spans at three
levels, each fold a button with `aria-expanded`, and the selected row
`aria-selected` and the tree's active descendant. A failed operation is marked
on its own row and on the turn that holds it. A call row is keyed by a square
in its kind's colour -- the model's violet, a tool's teal, anything else
neutral, as the turn's bar paints it -- and named by its tool, or by the part
of a model call it is, beside what the call worked on. A turn row under the
pointer shows what its time went on (`segments.ts`'s `turnBreakdown`): its
model and tool calls, each counted once per call, their time and share of
the turn, and the waiting.
Picking a row opens the details pane, a white card floating over the
waterfall's right side -- the rows keep their whole width -- and over all of it
below 800px
(`features/trajectory/detailStore.ts`): a descriptor read through
`trajectory.detail` and block bodies read through `trajectory.block`, each filed
under its whole identity (session, epoch, entry, revision, block) and shown only
under the ticket it was asked with.
The reader moves the card by its head and sizes it by its corner, as they do
the desk palette: let go near where it opened, it goes back there, and
wherever it is the store keeps it whole inside the area. The corner also takes
the focus, and each arrow moves a free edge 16px (`RESIZE_STEP`). It is a button
rather than a separator, which is a range along one axis: its description says
both sizes, and a keyboard step reads out the new one.
The pane's head carries a pin for the tab on screen: the conversation's
**pinned tab**, opened first whenever the pane arrives at an entry whose strip
has it (a new selection, or the pane reopened) and never while the reader is
already on the entry; kept per conversation in memory and in the tab's session
storage (`lib/persist.ts`), with no cap.
Under the raw record the details pane lists the files
the span names (`features/trajectory/Files.tsx`): the `files` directory whole,
each file's content read by its own cursor while it is open and in view, kept
apart from the block pages and let go stalest first when the budget is short.
The list store also keeps a current Sub-agent Scope (`scope`, `scopes` and
`saved` in `store.ts`), though no strip shows it: `select` on an entry of another scope (a link in the pane) moves it. The
pane's button on an entry that opens a scope, or on the first row of a turn a
sub-agent's report started, and a dispatching row's door arrow land on the run
they name -- its external agent's entry, which opens on what the agent
recorded, else its first entry (`scopes.ts`'s `landingOf`). Like a click on a
waterfall row, they select across scopes without moving the current one
(`selectAcross`), since every agent is on screen at once.
_Avoid_: "trace view" or "audit view" for this -- the trace is what the runtime
records, the trajectory is how this page reads it back -- and "sub-agent trace"
-- a spawned run's steps are in the trace of the turn that dispatched it.

**Region**:
One of the things `src/App.tsx` renders at the body, in the standing order
`src/state/portals.ts`'s `BOOT_BODY_ORDER` declares: `div.app`, the collapse's
twin, the module page, the three veils, the shared drawer and the two standing
hosts. A region is markup plus the flags its store writes --
never its contents: a region that is shared ground (`#capsBody`, `#wsBody`,
`#list`, `#stage`, ...) is rendered with no children at all, because an island
root or a tab module fills it. `src/test/__golden__/region-*.txt` holds one
golden per region, and `src/App.test.tsx` holds the order.
_Avoid_: "layer" for this -- a layer is one of the four things `host()` appends.
_Avoid_: "writer" for whatever fills one: name it, and call the act a DOM touch
(`scripts/gates/state-dom-touch.test.mjs`).

**Module page**:
One row of `src/state/pages.ts`: a `<section>` id, the empty box its island
fills, the rail button it lights, its rank in the Escape chain, and the keys its
heading and accessible name speak. In the order they sit among the body's
children -- and every table that names a page derives from them:
`src/App.tsx`'s sections, `state/page.ts`'s `PageId` and open flags,
`state/escapeOrder.ts`'s Escape rows, `state/portals.ts`'s body order,
`chrome/Rail.tsx`'s nav strip, `features/rail/store.ts`'s marks and
`src/test/regions.test.ts`'s goldens. Adding a page is adding a row.

Two rows today, because a place and a setting are different things: what a
reader goes TO is the **connections hub** -- the agents Raven hands work to and
the channels it can be reached from -- while schedules and memory are set up
once and then left alone, so those two are **settings sections** instead.
_Avoid_: "page" for the whole document, or for a dialog -- the settings dialog
and the model picker are overlays, not module pages.

**Connections hub**:
The two module pages the rail's one `agentsBtn` row opens -- `extAgentsPage`
(agents) and `connectionsPage` (channels) -- and `src/state/hub.ts`, which
makes them one place: the module the reader was last on, followed off the page
switch, so the rail row goes back to it. Both pages open on the same header,
`src/components/HubHead.tsx`, whose two tabs are the top of the page. They stay
two pages because each domain already owns its store, its drawer card and its
fetch; each registers what opening its module costs on `hub.onOpen` at its own
module evaluation, because `state/` may not import an island.
_Avoid_: "agent hub" for the whole -- that is the agents module, one of its two.

**Settings section**:
One row of `features/settings/store.ts`'s `SECTIONS`: a pane of the settings
dialog. Nine of the eleven are that domain's own components. The other two --
schedules and memory -- are another domain's island, rooted in a box
`src/App.tsx` renders beside `#spanels` and named by that store's `HOSTED`,
because a React root inside the settings island's own tree would be unmounted
the moment the reader picked another section. Which one is on screen is
`data-section` on the veil, written by the settings island on every draw, and
the stylesheet is what shows the box it names. A domain that is a section
registers what arriving at it costs on `state/settings.ts`'s `onEnter`, and
what leaving it costs on `onLeave`.
_Avoid_: "page" for one -- a page is a row of `state/pages.ts` and has a rail
button.

**Manifest**:
`features/<domain>/manifest.ts` -- what one domain declares about itself: its
name (`domain`), the module page it owns (`page`), the seam keys it answers
(`sources`), the root `src/main.tsx` mounts for it (`root`) and the box that
root goes into when it is not the page's own body (`host`), plus the class
prefix it already uses where that is not its own name (`cssPrefix`).
`features/manifests.ts` is the assembly point that reads all eighteen; no domain
may read it back -- an import the other way would put every island in every
island's closure. Two fields a reader may look for are deliberately absent, and
that file says why: an `i18n` namespace and an `onLangChange`.
`scripts/gates/domain-shape.test.mjs` holds the files a domain has and
`domain-registration.test.mjs` holds the manifest against the page table and the
seam.

**Island**:
A domain's root component together with the box it renders into: `<Domain>App`,
mounted by `src/main.tsx` into the body its manifest names, into a host from
`features/hosts.ts`, or into a layer `host()` hands out. An island renders one
domain's data into a container it was given and never renders the container
itself. Sixteen of them, and each subscribes to the language itself, which is
what `scripts/gates/island-lang.test.mjs` holds it to (`NO_ROOT` names the four
domains with no root of their own, and why).
_Avoid_: "page" for an island -- the page is the section it fills.

**Host**:
Two senses, and no third. (1) The element an island roots itself in: the page
body its manifest names, one of the three detached nodes in
`src/features/hosts.ts` that a tab module re-attaches on every draw, or the lane
host the transcript appends inside shared ground. The three detached ones cannot
be rendered by anybody, because the capabilities page's two tabs clear their box
with `innerHTML` and React must own neither. (2) A standing layer at the body,
handed out by `host()` (`src/state/portals.ts`) in the table's order rather than
in the order it is asked for.
_Avoid_: "host" for the container a region renders empty -- that is shared
ground, and an island roots itself in it; and for an element merely looked up by
id or selector -- that is a `...El`.

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
at runtime). `src/state/portals.ts` is the table: thirteen rows keyed by `id`
-- eleven of which also carry the `selector` that finds them, while the model
picker's wrapper and the design's error bar have none -- their `--z` step, and
their place among the body's children, derived from `BOOT_BODY_ORDER` rather
than written as an index. It is the table rather than
the stylesheet that decides two of them, because two steps of the `--z` ladder
are deliberate ties -- for those four elements DOM order at the body IS the
stacking decision. `host()` hands out the four standing layers in table order
rather than in the order they are asked for.

**Popover**:
A panel anchored to the control that opened it and floating over the page: the
permission modes (`#permPop`), the session tier (`#tierPop`) and the model
picker (`.mpick`). `src/lib/popover.ts` decides what one has to clear -- the
composer card when its anchor sits on one, the anchor itself when it does not --
and `src/chrome/PermPopover.tsx` and `TierPopover.tsx` are the two the dock
raises.
_Avoid_: "pop" and "panel" for this.

**Kind**:
Which bucket a model list files a model under: `text`, `image`, `audio`,
`video`, `embedding` or `reranker`. Derived once, in Python, by
`raven/providers/registry_data.py`'s `kind_of`, from what a model WRITES -- so
a model that reads pictures is still `text` -- and carried on the wire as
`model_labels[<id>].kind`. `features/model/types.ts` reads it (`modelKind`),
and `guessKind` there is the one place that guesses: a translation of
`inferred_tags`, for an id a person just typed that no catalogue describes.
A model with no label entry is `text`.
_Avoid_: "type" or "category" for this; "kind" alone for a provider's auth
shape, which the rows call `auth_type`.

**Offer**:
What ONE opening of the model picker lists, and what a pick there means
(`Offer` in `features/model/types.ts`): a kind, optionally the providers the
caller allows, optionally a title and the pair to mark, optionally the write to
make. The composer's chip opens with none of it and gets the default -- text
models, every connected provider, switch this conversation. A settings role
slot opens with its own through `openPicker` in `features/model/source.ts`.
The kind narrows each provider's column, never the provider list.

**Gateway provider**:
A provider that resells other vendors' models under `vendor/model` ids --
`ProviderSpec.is_gateway` in `raven/providers/registry.py`, `gateway` on a
`model.options` row. Twenty-two of the fifty-six.
_Avoid_: "gateway" alone, which in this document is the page's one data entry
point above.

**Provider catalogue**:
Every provider `model.options` returns, connected or not: the left column of
the settings dialog's Model providers page
(`features/settings/providers/ProviderSide.tsx`). Distinct from what a picker
offers, which is the connected ones.
_Avoid_: "catalogue" alone, which in the Runtime's `CONTEXT.md` is the Session
Mode catalogue.

**Task**:
One row of `tasks.list` as the desk's tasks tab draws it (`features/tasks/`): a spawn or a
DAG run this conversation started, with its nodes inline. The row is the list's, the pane it
opens is the desk's (`kind: 'task'`), and the node's context comes from `dag.node` /
`subagent.context` through the transcript's renderer -- the domain holds the rows and what
is picked, never a copy of the record.
_Avoid_: "task" for a turn or for the composer's draft.

**Pane**:
A resizable column: the workspace pane beside the chat (`src/state/ws.ts` holds
it, `src/chrome/WsPane.tsx` renders it, and the islands drawn inside it ask it
about itself through `src/state/wsPane.ts`) and the desk's floating panes
(`src/features/desk/`). The grips that resize one are a measuring behaviour
(`src/chrome/behaviour/panes.ts`), which is one of the two places a deliberate
DOM touch lives.
_Avoid_: "panel" for this.

**Step body**:
What the onboarding wizard (`features/onboard/`) draws for one of its steps: a
component another domain owns, over that domain's own store, with the four
questions the wizard asks it (`load`, `subscribe`, `loaded`, `done`;
`features/onboard/types.ts`). The settings domain hands over its model page
and its web-search controls, the sub-agents roster its connect list, and
`src/app/install.ts` is what hands them -- the wizard reads no sibling's
private file, and every control has one owner. The wizard's own frame around
them is the step strip, the scrolling column and the footer.
_Avoid_: "pane" for this -- a pane is a resizable column.

**Sheet**:
A card that docks above the composer for as long as one turn needs it: a
clarifying question, or an approval request
(`src/features/composer/ClarifySheet.tsx`, `GateSheet.tsx`,
`AskApproveSheet.tsx`) -- or for as long as the reader wants it: the deck
template picker (`TemplateSheet.tsx`, opened by `templates.ts`) docks the same
way but asks nothing, so the composer stays live under it. A clarifying
question may be a whole batch: where every question of one `ask_user` call
brought its own options, the sheet is a form the reader steps through -- back
as well as forward, multi-select where the tool allows it -- and one Submit
reports the lot. Filed under the session it was raised in and mounted only
while that session is open -- see Sheet rack. A delegated graph used to
dock here too and no longer does -- see Task strip.
_Avoid_: "dialog" for this -- a dialog is the settings or channel one, which is
not docked and is not about a turn.

**Drawer**:
`#detail`, the one shared detail drawer (`src/state/detail.ts`): whose card is
in it, whether it is up, and the host each island renders its card into. Four
islands used to draw into the same element by id, each clearing it with
`innerHTML` and two of them watching it with a MutationObserver to learn that
another had closed it; it is one store now, and `CLOSE_ORDER` declares the order
a close is spent in.
_Avoid_: "dialog" and "panel" for this.

**Card**:
What an island renders into the drawer's host -- one card per owner under
`#dBody`, with the title and the close button `src/App.tsx` renders around it
(`#dTitle` is the store's to fill). The word is also the page's layout noun
elsewhere -- the composer's card, the hub grids' `hubcard` -- but in this
vocabulary a card is the drawer's content.

**Escape order**:
The ordered table in `src/state/escapeOrder.ts` (`ESCAPE_ORDER`, of
`EscapeLayer`): fifteen layers that can be on screen at once, and which one an
Escape takes back. A table rather than a
stack, because each entry answers "am I open" when the key arrives -- the
channel dialog opens over the entries page and closes first, while the shared
drawer opens over the dialog and closes second. Capture-phase handlers the
docked sheets register run ahead of it. Approval and credential cards consume
Escape after denying or skipping, leaving the turn running. A later Escape
reaches the normal close order and stops a running turn only when no higher
layer is open.

**Export Wizard**:
The dialog `#exportVeil` holds (`src/App.tsx` renders the veil, the box and the
empty `#exportBody`), drawn by `features/export/` and opened from the header's
conversation menu (`#moreBtn` in `src/chrome/ChatTop.tsx`, the corner slot,
with the desk launcher one slot in over `#wsBtn`). A flow is a sequence of
pages: the export is the turn picker, then the export page; the bug report
(offered while the handshake carries `bugreport-v1`) is the picker, the
problem, the decisions and the upload. The picker drops out when the
conversation has a single finished turn, and the decisions are marked skipped
when the Redaction History or a clean snapshot leaves nothing to decide. The
export runs as a gateway job (`trajectory.export`) the page follows through
`trajectory.export_status` and downloads with the job's link; the report is a
Report Session the page follows through `bugreport.status`, which carries the
values to decide in full only while it waits on the decisions -- the
decisions page lists those values and nothing else, and an item's contexts
come one at a time (`bugreport.item`), their hit spans counted in code points. The close button and Escape both
cancel, asking first while a job is under way or a report is started and not
over, and closing cancels the report (`bugreport.cancel`) whatever its phase.
Every answer is checked against the wizard's `epoch` and the job or report it
was asked about: a start that lands after its wizard closed has its job or
report cancelled on arrival, and a status query, or a report step whose
answer is lost, keeps the same job or report and asks its status rather than
starting or sending again. `exportable` is the one place that says whether the
conversation on screen may be exported, and the menu button is disabled
exactly when it says no.
_Avoid_: "report dialog" for the bug report -- it is this wizard on another
flow, not a dialog of its own.

**Imported Conversation** (page side; the runtime term is in `../CONTEXT.md`):
A package read back in, shown read-only. The rail's `#importedBtn`
(`src/chrome/Rail.tsx`, under the module rows) opens a file dialog through the
`imported` domain's store, which holds one claim from reading the file to
dealing with the answer: while it is held the button names the file and takes
no second one, and the second copy a duplicate offers goes through the same
claim. A package over the upload limit is refused before it is read; the bytes
ride `imported.upload` as one frame. A duplicate raises the confirm sheet with
its second answer (`#cfAlt`): open the copy already imported, or import
another. The rows are the rail's: `session.list` returns them for the
`imported` channel (`from === 'imported'`), the Imported group draws them after
Recent and only while there is one, and each offers rename -- on the row, never
in the conversation's header -- and delete, and nothing else. The settings
page's delete-all leaves them alone.
Opened, one is read-only. The session registry sets `.chat[data-readonly]`
(`state/session/conversation.ts`'s `readOnly`) as a switch begins, before
`session.resume` answers, from the key's own prefix (`lib/imported.ts`); the
stylesheet then takes away the composer dock, the workdir tag and the
conversation menu (its slot kept), and the name stops being a rename control.
Sending, branching, the header editor and exporting refuse in their own code
too. What the package does not carry degrades quietly instead of failing: a
graph card reads no run record and shows no node count, an agent card asks
for no roster, a task pane or a file does not open (a notice says why),
pictures show their names without a request, delivered files are missing,
and the desk puts back nothing. A package with no session record gets a quiet
note instead of the empty state, so its trajectory toggle stays.
_Avoid_: "import" alone -- the `importSync` domain and `#importRow` are the
cold-start import of another tool's memory.

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
-- the reader's remembered pick, else the first of their own languages this page
has words for, else what `<html lang>` declares -- so `get().lang` is never null
and the first frame is one language rather than the served markup with words
from another drawn over it. The middle step is what a page that never reaches
the gateway has in place of its answer, a failed connect returning before
`state/lang/pick.ts`'s `load` is called. Two groups hear a pick:
`subscribe` is what a component reads through `useSyncExternalStore`, so every
region and every island re-renders its own words, and `onApplied` is for
everything that is drawn rather than rendered. `get().picked` says whether a
language was chosen rather than inherited from the document or the browser, and
it decides one thing: `attr(key)` answers `undefined` until a pick lands, because an attribute
the served markup does not carry is one applyI18n would not have written
either.

**Language repaint**:
`src/state/lang/effects.ts` -- the steps a pick asks of everything that
draws itself rather than rendering: the rail's own draw and the capabilities
page's, three stores whose draw commits a field their component renders (the
permission chip, the rail foot, the context ring), the model label -- the one
step left that writes an element by id -- the settings dialog's epoch,
the transcript's per-lane version bump, the composer's queue,
the shared drawer (closed rather than redrawn: nothing above it could hand back
the subject it was drawn from) and one conversation reload. An island that only needs to
re-render is not in it: each `<Domain>App` subscribes to the language itself
(`scripts/gates/island-lang.test.mjs`). Subscribed once, from `src/main.tsx`,
through `lang.onApplied`, so it runs after the rendered half has committed. The
order is pinned by `src/state/lang/effects.test.ts`; the reload is last because
it rebuilds the conversation from disk.

**Sheet rack**:
`src/state/sheetRack.ts` plus `src/chrome/SheetRack.tsx` -- what docks above the
composer (a clarify question, an approval request, the deck template picker),
filed under the session it was raised in and mounted only while that session is
open. A parked sheet keeps its element and loses its interior, which is why what
the reader typed into one lives in `src/state/sheetDrafts.ts` rather than in the
input; a sheet re-added with a new view keeps its id and its place, so a repaint
is a repaint and not a remount.

**Task strip**:
`TaskRuns` in `src/features/tasks/TasksPage.tsx`, rendered by
`src/chrome/Dock.tsx` directly below the Sheet rack -- one chip saying how many
tasks are running, their names as its hover title. It is a reminder that
delegated work is under way, whether spawned or dispatched as a graph, not a
list of it: the chip opens the Desk's tasks tab, whose rows name each run and
open its pane, which is where the graph itself is drawn and panned. Nothing
about a run docks in the rack: a graph there was the whole of
itself between the transcript and the box you type in, for as long as the run
lasted.
_Avoid_: "dag sheet" -- there is no longer one. The run's durable record in the
conversation is the trail's delegation card
(`src/features/transcript/store.ts`'s `dagFeed`); `src/features/dag/mount.ts`
keeps the run's state for the pane and for resume, and renders nothing.

**Global listeners**:
`src/state/globalListeners.ts` -- every listener the page holds on the document
or the window, in one function, in the order it registers them. The order is a
contract (five are capture-phase, and inside one phase the first registered
runs first), so it is one place and `src/state/globalListeners.test.ts` asserts
it call for call. A control's own handler is not here: that belongs with the
control.

**Page callback**:
One of the three slots `src/state/page.ts` declares and `src/app/install.ts`
fills: what a page switch spends on an island (the rail's mark, the channel
dialog's close, the new-job sheet's close), in the order `show` spells out.
Registered rather than imported, because `src/state/` does not import
`src/features/` -- which is also how a case names the slots it is about.
