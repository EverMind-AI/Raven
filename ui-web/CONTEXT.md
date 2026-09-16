# Web UI

The browser front-end (`ui-web/`, React + TypeScript, one page). Renders the chat
transcript, the rail and the module pages; talks to the Runtime only over the RPC
protocol. Assembled into a single `dist/index.html` by `build.py`, which inlines
the stylesheet and the one JavaScript chunk Vite builds.

## Language

**Gateway**:
The page's one data entry point, `gateway()` (`src/state/gateway.ts`): the
`RpcTransport` every call and every push goes through. Held in one module slot,
installed once by `src/main.tsx` before anything can ask for it.
_Avoid_: "the socket" for this -- a socket is one of the two things that can be
behind it.

**Transport**:
An implementation of `RpcTransport` (`src/rpc/transport.ts`): the WebSocket to a
real raven (`WsTransport`), the offline fixture library (`FixtureTransport`), or
a canvas layered over either (`OverrideTransport`). Which one this page got is
decided once, from the URL, by `chooseTransport()` (`src/state/transport.ts`).

**Source**:
`features/<domain>/source.ts` -- everything one domain knows about speaking to
the gateway, plus the pure functions that map an answer into the shape that
domain's renderer reads. One per domain; the renderer beside it never calls the
gateway itself. Installed onto the `sources` seam (`src/state/sources.ts`) by
the page's wiring (`src/state/install.ts`), which is the only module that
assigns those members.

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
`scripts/pipeline-coverage.test.mjs` holds the union of `handles` equal to it.
An event the page deliberately does not render still has a stage, with an empty
body.

**Legacy layer**:
`src/legacy/` -- what is left of the concatenated page script: chrome, in the
install order `build.py`'s two manifests pin, reached through
`src/legacy/index.js`. Module bodies only declare; everything that happens at
load happens in a part's `install()`. Stage C of the refactor turns these into
components and the directory goes.
