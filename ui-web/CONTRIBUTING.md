# Contributing to ui-web

The conventions this page is built to, and the checklist for adding a domain to
it. Written for whoever writes the next feature here -- a person or a model --
so every rule says what it is and who verifies it: a named gate under
`scripts/gates/`, or "by review" where nothing machine-checkable exists yet.

A rule with a gate is enforced on every `npm test`. A rule that says "by
review" is a rule all the same; it is just not one a run can catch. Nothing
below promises more than the gate beside it delivers, and every gate's own
header says what it pins and why -- the gate is the specification, this file is
the map to it.

The vocabulary -- domain, island, region, host, portal, store, seam -- is
`CONTEXT.md`. The directory tree and the gate roster are `README.md`.

- [1. Layout and layering](#1-layout-and-layering)
- [2. The shape of a domain](#2-the-shape-of-a-domain)
- [3. The store shape](#3-the-store-shape)
- [4. Rendering](#4-rendering)
- [5. Source and fixtures](#5-source-and-fixtures)
- [6. Naming](#6-naming)
- [7. CSS](#7-css)
- [8. Tests and gates](#8-tests-and-gates)
- [9. Tooling](#9-tooling)
- [10. Adding or rewriting a domain](#10-adding-or-rewriting-a-domain)
- [11. Known exceptions](#11-known-exceptions)

---

## 1. Layout and layering

### 1.1 What each directory is for

| Directory | Holds | Does not hold |
|---|---|---|
| `src/app/` | the page's lifecycle: the boot's ordered list, the wiring, the connection, the update watch, the splash | any domain's business |
| `src/rpc/` | the protocol: the generated contract, the gateway slot, the three transports, the offline fixture library | a runtime import of `features/` or `state/` (a type import erases and is fine; the channel fixture's read of the connections catalogue is the one pinned exception) |
| `src/state/` | the stores and tables the whole page reads, plus `lang/` and `session/` | state one domain owns alone |
| `src/chrome/` | the page's own furniture (rail, chat header, dock, sheet rack, tooltip, chips and their popovers), plus `behaviour/` for what it installs rather than renders | a component only one domain renders |
| `src/features/<domain>/` | everything one domain is: its types, its source, its store, its components, its tests | a small part two domains share |
| `src/components/` | the components more than one region renders | anything holding a store |
| `src/lib/` | pure helpers and browser-API wrappers, with no store and no page of their own | an import of `features/`, or a reach for an element -- `lib/dom.ts` IS the page's `$` and is the registered exception |
| `src/i18n/` | `t.ts`: the catalogue read and the one lookup | the catalogue itself (`i18n/messages.json` at the repo root) |
| `src/styles/` | `page.css`: the page's frame and the `--z` ladder | one domain's rules (those go in the domain, see section 7) |
| `src/test/` | helpers and suites about the assembled page, and the region goldens | a suite about one module (that sits beside the module) |
| `scripts/` | the seven tools | anything the page imports |
| `scripts/gates/` | every gate, plus the two readers gates share (`css.mjs` for the stylesheet, `rpcCalls.mjs` for the call sites) | a tool with a CLI of its own (those live in `scripts/`) |

### 1.2 Which way an import may point

```
lib <- components <- chrome <- app
lib <- rpc <- state <- chrome, features, app
lib <- features <- app
features -/-> features   (a sibling is reached through its source.ts or types.ts)
state -/-> features      (at runtime; a type import erases, and a call the
                          other way is a registered callback)
```

**Enforced by `import-direction`**, which reads the source text the way Vite
resolves it and holds four things: the ranking above, every upward edge the
tree has today (`PINNED`, 89 rows), every cross-domain edge into something that
is not a sibling's public surface (`CROSS`, 59 rows), and the runtime cycles
(`CYCLES`, two components; `IN_CYCLES`, 17 files inside one). All four are
ratchets: they may shrink, never grow. A new upward edge fails, and the fix is
to invert the call -- a registered callback (`state/page.ts`'s `onShow`) or the
sibling's `source.ts` -- not another line on the list.

`features/manifests.ts` ranks above the domains (every domain declares into it,
so a domain reading it back is an upward edge, which is what keeps every island
out of every island's closure) and `features/hosts.ts` below them (a leaf with
no imports of its own; it is the gate's one `EXEMPT`).

## 2. The shape of a domain

### 2.1 The files

```
features/<domain>/
  types.ts         the domain's contract types (wire shapes and UI shapes); public
  source.ts        everything the domain knows about the gateway; public
  store.ts         the domain's state, section 3's shape; private
  <Domain>App.tsx  the root component, exported as <Domain>App; private
  manifest.ts      the one declaration the page reads the domain through
  wire.ts          optional: the domain's own page wiring, installed at boot
  mount.tsx        optional: only where a root is not one-host-one-root
  styles.css       optional: the domain's own rules, imported by its App
  <module>.test.ts a suite per module; <module>.<aspect>.test.ts for several
  __snapshots__/   the island's DOM snapshots
```

**Enforced by `domain-shape`** (the four required files, plus a root component
for every domain the page mounts as one piece; `EXCEPTIONS` names the five
misses the tree carries and why) and **`file-names`** (what a file may be
called; see section 6).

### 2.2 The public surface

`types.ts` and `source.ts` are the domain's public surface. `store.ts`, the
components and `mount.tsx` are private. A sibling domain, `state/` or `chrome/`
that needs a domain to do something calls a free function in its `source.ts` or
a callback the page registered -- never its store.

**Enforced by `import-direction`**'s `CROSS` list, which pins the edges that
break this today and lets none be added.

### 2.3 The manifest

`features/<domain>/manifest.ts` is where a domain declares itself, and
`features/manifests.ts` is the only reader:

| Field | Meaning |
|---|---|
| `domain` | the directory name, which is the domain's name everywhere else |
| `page?` | the module page it owns, as `state/pages.ts` declares it |
| `sources` | the seam keys it answers, installed by `src/app/install.ts` |
| `root?` | its root component, for an island mounted into a rendered box |
| `host?` | the box that root goes into, when it is not the page's own body |
| `cssPrefix?` | the prefix its class names carry, when that is not the domain's own name (section 7) |

A page's identity is a different table: `state/pages.ts` holds the seven module
pages (section id, body id, rail buttons, Escape rank, heading and aria keys),
and everything that names a page derives from it -- `src/App.tsx`'s sections,
`state/page.ts`'s `PageId`, `state/escapeOrder.ts`'s rows, `state/portals.ts`'s
body order, `chrome/Rail.tsx`'s nav strip, `features/rail/store.ts`'s marks and
`src/test/regions.test.ts`'s goldens. Two tables, because every one of those
readers is in `state/` and a single aggregate would pull twenty islands in
there with it.

**Enforced by `domain-registration`**: every domain declares itself once into
`features/manifests.ts` and names itself after its directory; every page a
domain claims is one the table declares, claimed once (the capabilities page is
chrome's and is the one page no domain owns); the seam keys the manifests claim
are exactly the members of `Sources`, none twice; and every declared root is
mounted through the one loop in `src/main.tsx`.

## 3. The store shape

One store shape for the page, `makeStore<T>` in `src/state/store.ts`:

```ts
const store = makeStore<State>(initial)
export const { get, set, subscribe, _resetForTests } = store
```

- `get()` reads the snapshot. `set(next)` writes it and notifies inside
  `flushSync`, so a listener -- a React root included -- has committed by the
  time `set` returns. That synchronous notify is a contract: an imperative
  caller writes and then hands over.
- `set` takes a value or a function of the previous one. The state a store
  holds is a value, never a function.
- A component reads it as `useSyncExternalStore(store.subscribe, store.get)`.
- `_resetForTests()` puts the value back, leaves the subscribers alone, and
  does not notify.

### 3.1 The verbs

| Meaning | Use | Not |
|---|---|---|
| read the snapshot | `get` | `getState`, `saved`, `snapshot`, `current` |
| write the state | `set` | `put`, `commit`, `patch` |
| make React render once more (a state bump) | `redraw` | `draw`, `sync`, `repaint`, `langRedraw` |
| mount / unmount a React root | `mount` / `unmount` | `draw`, `route` |
| commit and mark read | `sync` | -- |
| an overlay | `open` / `close` / `toggle` / `isOpen` | `show` / `hide` (the toast is the exception: `show`) |
| boot wiring | a module's `install()`, and only that | a domain action takes an object: `installSkill`, `installEntry` |
| the test seam | `_resetForTests` | `wipe`, `reset`, `_resetAppsForTests`, `_clearForTests` |

**Enforced by `store-shape`**: a module that exports `subscribe` exports `get`,
`set` and `_resetForTests` too; `listeners.add(` and `new Set<() => void>()`
appear only in `state/store.ts`; a module holding module-level `let` has a reset
seam. Three ratchets, each pin carrying the reason it is one. The verb table
itself is by review.

Stores are module singletons rather than factories, because the page is one
page and half its callers are not React: the Escape order, the language
effects and the session pipeline all read and write from outside any component.

### 3.2 Calling into a domain from `state/`

`state/` does not import `features/` at runtime. Where the page's machinery has
to ask a domain to do something, the state module declares a slot and the domain
fills it: `src/app/install.ts` fills `state/page.ts`'s three `onShow` slots, and
`features/rail/store.ts` fills `state/navfly.ts`'s `onMark` at its own module
evaluation, because a fold the reader can click carries no guarantee that the
page's wiring has run. Enforced by `import-direction`.

## 4. Rendering

1. **React renders the markup.** `src/page.html` carries only `<head>`,
   `#splash`, `#onb`, `#noJs` and the two script blocks; everything else is
   `src/App.tsx` and the islands. By review, with the region goldens
   (`src/test/__golden__/`) and the two boot goldens (`scripts/__golden__/`)
   recording the result.
2. **Two imperative DOM writes are allowed**: (a) a store writing a flag on its
   own region's container (`data-open`, `data-rail`, ...), which `src/App.tsx`
   renders as the value the page was served with and lists by name; (b) a
   measuring behaviour under `src/chrome/behaviour/` (the pane grips, the
   scrollbars). Writing the text, class or `innerHTML` of an element React
   rendered is the case neither of those covers, and it is ratcheted rather
   than forbidden outright: the 24 such writes the tree still holds are pinned
   per file and may only fall.
   **Ratcheted by `state-dom-touch`**: a per-file count of element reaches --
   every match, not every line that holds one -- 79 over the 34 pinned files in
   `state/` and `app/`, zero for `lib/` and `components/` (two registered
   exemptions: `lib/dom.ts` IS the page's `$`, and `components/Ico.tsx` builds
   detached SVG with `createElementNS`), and every pinned module's header has
   to say why it reaches (`SILENT` names the one header that does not yet). A
   reach through the page's own `$` is counted like any other, because it is
   `document.querySelector` under a shorter name. The page frame -- `chrome/`,
   `src/App.tsx`, `src/main.tsx` -- is counted in a table of its own, `FRAME`,
   26 matches over ten files: rule (b) is a budget, not a licence. The writes
   are `WRITES`, over those layers plus `lib/` and `components/`, on the same
   terms -- down or gone, and a file absent from the table writes nothing.
   `features/` is counted by neither -- rule (a) lives there, so that one is by
   review.
   Both counts read text, so a comment that merely mentions `querySelector`
   raises one. Reword the comment; the number is a budget for the code. Two
   things the text match does not count, the first on purpose and the second
   not: `event.target.closest()`, which climbs from a node the handler was
   handed rather than reaching for one, and a reach spelled through an alias
   (`const doc = globalThis.document`), which no module does today and which
   the gate would not see.
3. **Language.** Every `<Domain>App` subscribes with
   `useSyncExternalStore(lang.subscribe, lang.get)`, so a flip re-renders the
   island by construction (**enforced by `island-lang`**, `NO_ROOT` naming the
   four domains with no root of their own and why). `t(key)`
   (`src/i18n/t.ts`) is the lookup; `lang.attr(key)` is the one other take-a-key
   form, for keyed attributes the served markup did not carry, and it answers
   `undefined` until a reader has picked a language. `state/lang/effects.ts`
   holds only what is drawn rather than rendered.
   **Enforced by `i18n-keys`**: every literal key, in either take-a-key form
   (`t(` and `lang.attr(`), names an entry the catalogue carries. A key built by
   concatenation is outside it.
   The lookup is not yet the only source of words, and what remains is pinned.
   The unit is a maximal run of CJK -- a script written without spaces has no
   smaller thing to call a word, and a line already pinned cannot carry a
   second word in on the same line for free. Six runs are the served first
   frame, the no-JavaScript shell in `src/page.html`, which no catalogue and no
   store has reached yet (its `<html lang>` is a tag rather than a word).
   Thirty-two more are in code, across thirteen modules: sixteen words a reader
   sees (a `??` fallback rendered before a language is picked, a JSX literal,
   the string `state/envChip.ts` writes, the month and day
   `features/memory/MemoryPage.tsx` formats), two separators a reader sees (the
   ideographic comma `features/cron/humanize.ts` joins a translated list with),
   thirteen in three patterns matched against text and never drawn -- twelve of
   them the range ends and class members spelling out `lib/prose.ts`'s two --
   and one comparison against the title `chrome/ChatTop.tsx` serves.
   **Ratcheted by `first-frame-literals`**: a count of runs per file, down or
   gone, each with its reason in the gate's header; a string is read cooked, so
   an escaped run counts as the characters it stands for, and a file that loses
   a run lowers its own row in the same change. The offline fixture library is
   out of that count -- 489 runs in ten files whose content is the demo shell
   rather than the page's own words.
   English source is the repo's rule rather than this page's:
   `scripts/check_source_language.py` at the root fails a PR that adds a
   non-English line, and lets one through only by a named zone, the `*.md`
   suffix, a relocation of the same run, or the file having carried one at the
   base revision. `ui-web/` is no zone, so that last pass is what the fourteen
   files above stand on, and `first-frame-literals` is what keeps the pile from
   growing.
4. **Portals go at the body**, in the order `state/portals.ts` declares, and a
   place in that order is derived (`BOOT_BODY_ORDER.indexOf`), never written as
   an absolute index. By review, with `src/test/portals.test.ts` reading the
   booted page.
5. **Shared ground stays empty.** A container filled by something other than
   the page root (`#capsBody`, `#wsBody`, `#list`, `#stage`, `#dBody`) is
   rendered with no children at all, and an island root or a tab module fills
   it; React never reconciles its child list. By review -- `CONTEXT.md`'s Region and Host entries say which
   containers those are.
6. **Text inputs are uncontrolled** and keyboard handling is native, behind the
   IME guard: `composing(e)` (`features/composer/store.ts`) is
   `e.isComposing || e.keyCode === 229`, and a key handler on a text field
   returns early on it. By review.

## 5. Source and fixtures

1. **One `source.ts` per domain, and every `gateway()` call in it.** A renderer,
   a store or a wiring module never calls the gateway itself.
   **Enforced by `rpc-names`**, which also holds every method name to
   `rpc-schema/openrpc.json` and keeps `callUnchecked` to the two names the
   contract omits. The location rule is checked under `features/` only:
   `state/session/` and `app/` call the gateway too (the turn pipeline, the
   boot, the update watch), and their names are checked while their placement
   is by review.
2. **Ask for a source by key.** `ds('cron')` returns `Sources['cron']`
   (`src/state/sources.ts`), so a renamed or misspelt domain is a compile error
   rather than a throw at the first paint. Never `sources.cron!` or
   `sources.cron?.` in production code: the first hides the failure the seam
   exists to make loud, the second makes a missing source silently do nothing.
3. **`src/app/install.ts` assigns the seam.** The one exception is
   `src/app/boot.ts`'s claim on the first frame, which installs the session
   source (`sources.rail`) because the `holdRail()` on the next line reads it,
   ahead of the sequence that fills it. Nothing else assigns a member, and
   `setSources` is the test seam rather than a second door into it.
   **Enforced by `seam-assignment`**: every `sources.<key> = ...` in the tree
   is the installer's or that one pinned exception's -- the row pins exactly
   one assignment, by count, so a second write of the same key in the same file
   fails rather than inheriting the first one's reason -- `setSources` is named
   only in a `*.test.ts` file, and `Object.assign(sources, ...)`,
   `Object.defineProperty(sources, ...)` and a re-export of either name belong
   to `state/sources.ts`, which declares the seam. The doors are matched
   against the names a file bound to the seam, so an alias is inside the rule
   (`const s = sources`, `import { sources as s }`, `import * as s`), and a
   destructuring target (`({ rail: sources.rail } = x)`) is the write it is.
   Outside it: the seam handed on as a value rather than under a name -- a
   function that returns it, a namespace renamed on the way in -- which needs
   the type checker rather than the syntax.
4. **One responder module per wire namespace under `src/rpc/fixtures/`**,
   registered in `fixtures/index.ts`. A responder answers the contract and
   nothing else: no clock of its own (the transport hands it `now`), no import
   of an island, and events pushed only through the transport's notify channel.
   - Type the answer, do not cast it. A responder's return type is
     `Wire<ResultOf<M>>` (`src/rpc/fixtureTransport.ts`): the contract, widened
     in exactly one direction -- an optional field may be `null`, because the
     gateway really sends null where the schema writes a value type. Required
     fields, enums and nested shapes are held exactly, so a missing required
     field is a compile error again. An `as` on a responder's answer switches
     the whole check off; seven of them were hiding five real bugs.
   - **Enforced by `fixture-shape`** (every answer against the contract itself,
     plus `UNSENT`: the 205 declared-but-never-sent optional fields, pinned by
     method and shrink-only), **`offline-coverage`** (every method the page
     calls has a responder or an entry in `EXEMPT` saying why the offline page
     has no answer -- 18 today), and **`fixture-now`** (two libraries born on
     the same instant answer the same bytes).

## 6. Naming

| Thing | Rule | Example |
|---|---|---|
| directory | one lowercase word, the domain's name | `features/knowledge/` |
| component file and export | PascalCase, after the component; a page root is `<Domain>App` | `KnowledgePage.tsx` exporting `KnowledgeApp` |
| other module | camelCase | `chooseTransport.ts` |
| test | `<module>.test.ts(x)`, or `<module>.<aspect>.test.ts(x)` with the module beside it | `store.lane.test.ts` |
| gate | `scripts/gates/<what-it-pins>.test.mjs`, kebab-case | `import-direction.test.mjs` |
| DOM id | `<domain>Page` / `<domain>Body`; a chrome element is named after its function | `#knowledgeBody` |
| i18n key | `gui.<domain>.<leaf>`, the namespace owned by one domain | `gui.knowledge.empty` |
| CSS class | `<domain>-<name>` | `.knowledge-row` |
| seam key | the directory name | `sources.knowledge` |
| wiring module | `wire.ts`, one per domain | `features/settings/wire.ts` |
| store verbs | section 3.1 | -- |
| reserved words | `chrome` means `src/chrome/` only; `registry` the session registry only; `shell` the desktop shell only; `install` boot wiring only | -- |

**Enforced by `file-names`** (rows two to five, with `NOT_COMPONENTS`,
`PAGE_SUITES` and `NOT_MODULES` pinning today's handful of exceptions),
**`domain-shape`** (the root component's export name), **`i18n-keys`** (the key
shape and the namespace-to-domain equation, with `LEGACY_FLAT` for 39
namespace-less keys, `ALIAS` for 28 namespaces that abbreviate their domain and
`SHARED` for 15 namespaces more than one domain speaks) and
**`check-class-namespace`** (section 7). DOM ids, seam keys, `wire.ts` and the
reserved words are by review.

A domain's name is written once. The directory, the seam key, the i18n
namespace, the DOM id prefix, the component prefix and the CSS prefix are the
same word -- `knowledge` gives `sources.knowledge`, `gui.knowledge.*`,
`#knowledgePage` / `#knowledgeBody`, `KnowledgeApp` and `.knowledge-*`. Where
the tree is not there yet the gap is pinned, not repeated: section 11.

## 7. CSS

`src/styles/page.css` is the page's own sheet: the frame, the `--z` ladder and
the shared class vocabulary. A domain's own rules go in
`features/<domain>/styles.css`, imported by that domain's App --
`features/extAgents/styles.css` is the worked example and says so in its
header. Vite collects every such sheet into `.modern/domains.css` (the
`cssFileName` in `vite.config.ts`) and `build.py` inlines it into the page's one
`<style>` block after `page.css`, so `check-page.mjs`'s "exactly one style
block" still holds. `build.py` checks both directions: a `styles.css` with no
built asset, or an asset with no source, fails the build.

Every class a domain introduces carries the domain's prefix -- its own name, or
the `cssPrefix` its manifest declares because it named its classes
consistently before there was a rule (six do: `kb`, `pb`, `ob`, `pm`, `su`,
`mem`).

A class the page frame introduces -- `src/chrome/`, `src/App.tsx`,
`src/main.tsx` -- is named `chrome-<rest>`, or exactly `chrome`. A class a
shared component in `src/components/` introduces carries its own file's name in
kebab-case (`Skeleton.tsx` names `skeleton-<rest>`, `SetupSheet.tsx` names
`setup-sheet-<rest>`), and that component's root class may be the bare name.
Each file is its own namespace there, and each is held to its own name alone:
`.model-tags` belongs to `ModelTags.tsx`, so `SetupSheet.tsx` writing it too is
borrowing and fails -- the check names the borrower and the owner. Both
namespaces are styled from `page.css`, which is the only sheet either has.

**Enforced by `check-class-namespace`** (run as a gate by
`check-class-namespace.test.mjs` and as a CLI), with three shrink-only debts:
`LEGACY_SHARED` (86 classes two or more domains name, pinned with how many
domains each reaches), `LEGACY_LOCAL` (how many of a domain's own classes are
still unprefixed, 342 in total) and `LEGACY_EXPR` (the same count for the
literals inside a `className={...}` expression, 91 in total). A new shared
class fails; one more unprefixed class in a domain fails, in an attribute or in
an expression. Moving a domain's rules out of `page.css` into its own sheet is
what lowers a number.

Moving a rule is not yet a mechanical change, and three facts decide how it
goes:

- **Order flips.** `build.py` reads `styles/page.css`, appends the collected
  `.modern/domains.css` to it, and splices the pair into the page's one
  `/*__STYLE__*/` marker. So a rule moved into `features/<domain>/styles.css`
  lands after the whole of `page.css`, and any same-specificity rule that used
  to win by source order now loses to it -- or starts winning where it lost.
- **Nothing checks pixels.** The stylesheet's digest was pinned for the
  refactor and that gate is retired; what is left records shape, not style. The
  two boot goldens (`scripts/__golden__/`) and the region goldens
  (`src/test/__golden__/`) hold each element's tag, id, classes and `data-*`
  attributes, and `check-css` holds declaration-level invariants. A migration
  brings its own verification.
- **`features/extAgents/styles.css` is the exemplar and is empty on purpose.**
  It establishes the mechanism -- a domain's sheet, imported by its App,
  collected by Vite -- and says so in its header. No rule has moved yet.

The two namespaces beyond `features/` carry three more shrink-only lists in the
same tool: `LEGACY_CHROME` (136 unprefixed classes, 103 in the frame and 33 in
the components), `LEGACY_CHROME_EXPR` (9 more inside a `className={...}`
expression, 1 and 8) and `UNSTYLED` (`.newrun`, the one class the frame's
markup writes that no stylesheet defines). A name in the shared vocabulary
passes there too, and so does a name already pinned in `LEGACY_SHARED` --
counting the frame's use of a class two domains name would say the debt grew
when nothing moved.

The gate's reach is every class a domain's non-test `.tsx` files name, and the
same for the frame's and the shared components': the literal attribute
`className="a b"`, and inside a `className={...}` expression every
single-quoted, double-quoted and template-literal static chunk, split on
whitespace. A token glued to an interpolation is a stem rather than a class
(`` `kbf-${family}` `` names `.kbf-md`, never `.kbf-`) and is not read as one.
An expression cannot tell a class from a comparison operand, which is why its
literals are counted in their own list rather than mixed into the other two,
and why the defined-in-a-stylesheet half reads every attribute literal but
only the prefixed literals of an expression.

## 8. Tests and gates

1. **A suite sits beside its module** and is named after it (section 6). A
   suite about the assembled page goes in `src/test/`.
2. **An island test mounts the page** (`src/test/pageRoot.ts`), lets the
   offline fixtures answer, and asserts on the DOM or on the store's public
   verbs. Asserting on source text is a last resort, for a contract with no
   observable point at run time -- and then the assertion says so.
3. **One gate, one concern.** A gate that needs two paragraphs to say what it
   holds is two gates (`boot-order` was three).
4. **A gate is a table plus two real sources.** It compares one place that
   declares against one place that reads -- the tree, the stylesheet, the
   contract, the Python roster -- rather than restating a constant. Both
   directions: a declaration nothing reads fails as loudly as a read nothing
   declares.
5. **The failure message names the fix**, not the fact. `expect(x, 'move the
   call into the domain's source.ts, or pin it in OUTSIDE_SOURCE with the
   reason')`.
6. **No absolute indices.** A position is derived from the table that decides
   it (`BOOT_BODY_ORDER.indexOf`, `ORDER`), so a reorder moves the assertion
   with it.
7. **A ratchet carries a reason per pin.** Historical debt is a named list that
   may only shrink, each entry with the sentence saying why it is one; the way
   off the list is the fix, never another line. A pin with no reason reads as a
   rule nobody meant.
8. **Prove a new gate by mutation.** Break the thing it pins, see it red,
   restore it, see it green -- and record which mutations you tried.
9. **A golden changes only in the commit that changes the DOM**, and that
   commit's body says which lines moved and why. Never regenerate snapshots
   wholesale (`vitest -u`): edit the golden by hand from the same table the
   source change came from, and read the diff back line by line.

No gate asks a module for a suite: nothing above fails because a new module
arrived without tests, or because a branch of one goes unreached. Whether a
change brings its tests is a review item, like the nine rules in this file
whose verifier is a reader rather than a run.

`npm test` is `vitest run` over both trees, so it is every unit suite and every
gate at once. `README.md` lists the 37 gates and what each pins. `npm run lint`
is the eslint pass (section 9).

## 9. Tooling

```sh
npm ci                  # once
npm test                # every suite and every gate
npm run type-check      # tsc
npm run lint            # eslint: import order, unused imports and locals
npm run gen:check       # src/rpc/generated.ts matches rpc-schema/openrpc.json
npm run build && python3 build.py   # the artifact, plus the two boot goldens
node scripts/check-page.mjs         # reads dist/, so build first
```

`eslint.config.js` reads the repo root's `eslint.base.mjs` -- the same factory
`ui-tui` uses, on the versions that tree resolves to, so the two front ends
lint on one engine. What this tree adds is the React hooks rules --
`rules-of-hooks` as an error, `exhaustive-deps` as a warning because the rule
cannot tell an effect that deliberately runs once on a value it reads from a
stale closure (six of those) -- and the four places the base's rules do not fit
this tree, each with its reason in the config: type imports last and a block
comment as a partition boundary (the default hoists an import above the
module's own header), `curly` off (2,063 one-line guards), a test may name the
type of a module it loads dynamically, and the gates under `scripts/`, which
are node scripts rather than part of the page.

`tsconfig.json` runs `strict`, `noUncheckedIndexedAccess`, `noUnusedLocals` and
`noUnusedParameters`. A parameter the body does not read is deleted; it keeps
its place behind a leading underscore only where a caller's arity puts
something the body does need after it -- a fixture factory's `_env`, an
override responder's `_p` in front of `next`.

## 10. Adding or rewriting a domain

Seven steps. The right-hand column is what fails if you skip it, so the run
tells you what is missing rather than the reader finding out.

| # | Do | What reddens if you do not |
|---|---|---|
| 1 | Create `features/<domain>/` and write, in this order, `types.ts`, `source.ts` (every method name in the contract), `store.ts` (`makeStore`), `<Domain>App.tsx` (the root subscribes to `lang`), `styles.css` (prefixed classes), `manifest.ts` | `domain-shape` (a missing file or a root named something else), `island-lang` (a root that does not subscribe), `file-names` (a file named against the rule), `store-shape` (a store of its own shape), `rpc-names` (a `gateway()` call outside `source.ts`, or a name the contract does not declare) |
| 2 | Write `src/rpc/fixtures/<domain>.ts` and register it in `fixtures/index.ts`, typed rather than cast | `offline-coverage` (a method with no answer), `fixture-shape` (an answer the contract does not accept), `fixture-now` (a clock of its own), `tsc` (a missing required field) |
| 3 | Assign the seam in `src/app/install.ts`: `sources.<domain> = ...`, and declare the key in `manifest.ts`'s `sources` | `domain-registration` (a seam key no domain claims, or two claiming one), `tsc` (a key absent from `Sources`) |
| 4 | Add the words to `i18n/messages.json` at the repo root, under `gui.<domain>.*`. `ui-tui` generates its own copy of that file, so run `npm run --prefix ui-tui gen:i18n` and commit the regenerated `ui-tui/src/i18n/messages.generated.ts` in the same change | `i18n-keys` (a key the catalogue lacks, or a shape that is not `gui.<ns>.<leaf>`); the `TUI checks` job in `.github/workflows/ci.yml`, whose `npm run lint:i18n` is that generator in `--check` mode |
| 5 | If it owns a module page, add its row to `src/state/pages.ts` and claim it in the manifest -- nothing else needs touching, because every other table derives | `domain-registration` (a page declared and unclaimed, or claimed and undeclared), `rail-nav-registry` (a button the rail cannot mark) |
| 6 | Keep the classes prefixed, and put new rules in the domain's own sheet | `check-class-namespace` (an unprefixed or shared class) |
| 7 | Define any new term in `CONTEXT.md` in the same change, then run `npm test`, `npm run type-check`, `npm run lint` and the build | by review (the terms), the gates (everything else) |

Rewriting an existing domain: build the new skeleton beside the old files and
delete them, rather than editing in place. The gates above are the same either
way, and the two boot goldens plus the region goldens tell you whether the page
still renders what it did.

## 11. Known exceptions

Every pin the tree carries today, what holds it, and what takes it off. All are
shrink-only: the way off a list is the fix.

| Exception | Why it stands | Held by |
|---|---|---|
| The knowledge domain's ids are `kbPage` / `kbBody` / `kbBtn` | `src/styles/page.css` scopes two rules to `#kbPage`, so the id renames when those rules move into the domain's own sheet; the three ids are one set and half a rename is worse than the wait | `features/knowledge/manifest.ts`, `check-class-namespace`'s `cssPrefix: 'kb'` |
| No i18n namespace equals its domain's name but `rail`'s | The catalogue is `i18n/messages.json` at the repo root and `ui-tui` generates its own copy from it, so a rename edits both front ends and the file neither owns. No TUI source reads any of the 28 aliased namespaces, so the rename is mechanical once the boundary allows it | `i18n-keys`'s `ALIAS` (28), `SHARED` (15), `LEGACY_FLAT` (39) |
| `manifest.ts` has no `i18n` field and no `onLangChange` | Both would declare something untrue today: no domain's keys live under one namespace, and the two imperative language repaints left are named by hand because `state/` may not import `features/manifests.ts` | `features/manifests.ts`'s header |
| `features/installed/` has no `types.ts`, no `store.ts` and no island | One shared read of `ext.list` that the two capability tabs, the memory page and the settings dialog draw from | `domain-shape`'s `EXCEPTIONS` |
| `features/desk/` has no `source.ts` | A pane shows the workspace's record, so the desk reads the workspace's source; its own `source.ts` is what takes its reaches into that store off the cross-domain list | `domain-shape`'s `EXCEPTIONS`, `import-direction`'s `CROSS` |
| `features/composer/` and `features/dag/` have no `source.ts` | The dock's seam is assembled by the page (no transport answers either half); a graph arrives on the turn's own events | `domain-shape`'s `EXCEPTIONS` |
| Eight modules keep a listener set of their own | Each holds something one value and one notifying `set` cannot express: `state/page.ts` (subscribers run after seven DOM writes and four effects), `state/session/registry.ts` (a map, not a value), `features/browser/store.ts` (a quiet `patch()` for a frame-fed view), `features/dag/store.ts` (an epoch bump over a Map), `features/model/store.ts` and `features/workspace/deliveries.ts` (independent reactive values), `features/subagents/store.ts` (one deliberately quiet write), `features/transcript/store.ts` (a listener set per lane) | `store-shape`'s `SHAPE` |
| Four more non-store subscriber groups | `app/connection.ts` and `lib/session.ts` are watcher lists rather than stores; `state/caps.ts`'s `switched` and `state/lang/store.ts`'s `afterwards` are second groups beside a store's own subscribers | `store-shape`'s `LISTENERS` |
| Four modules hold module-level `let` with no reset | Three hold a React root a reset would have to unmount; the fourth is a pinned store | `store-shape`'s `NO_RESET` |
| `lang.attr(key)` survives beside `t(key)` | It answers `undefined` until a language is picked, which is what keeps nine `data-tip` and twenty-four `aria-label` attributes off the first frame of a page nobody has picked for -- exactly what the served markup carries. Turning them into `t()` would change the DOM the boot and region goldens record | `state/lang/store.ts`'s `picked`, `i18n-keys`'s `KEYED` regex |
| 86 shared class names, 342 unprefixed local ones and 91 unprefixed inside a `className={...}` | The rules are in `page.css`, whose bytes are what the two boot goldens and the region goldens are taken from; they move a domain at a time. The third list is an upper bound: an expression literal may be a comparison operand rather than a class | `check-class-namespace`'s `LEGACY_SHARED`, `LEGACY_LOCAL` and `LEGACY_EXPR` |
| 136 unprefixed classes in the two namespaces beyond `features/`, 9 more inside a `className={...}` | 103 and 1 in the page frame, 33 and 8 in the shared components. There is no `chrome/styles.css` to move a rule into, so these come down by renaming in `page.css`, in a commit that changes the DOM the goldens record | `check-class-namespace`'s `LEGACY_CHROME` and `LEGACY_CHROME_EXPR` |
| `.newrun` is written and never styled | `src/chrome/Rail.tsx` puts it on the new-session button and no rule defines it; taking it out edits `src/test/__golden__/region-app.txt`. `src/components/SetupSheet.tsx` writes two more from inside an expression (`.badtx`, `.warntx`), where the check cannot tell a class from a comparison operand and so does not read them | `check-class-namespace`'s `UNSTYLED` |
| 26 imperative reaches for an element in the page frame | Not the rule's case: a portal at the body, two popovers filling a served list, the island mount boxes, and `chrome/behaviour/`'s grip drag and overlay scrollbars, which are behaviour rather than rendering and own no component tree. Counted so a third such module cannot appear unnoticed | `state-dom-touch`'s `FRAME` |
| 24 writes of an element's text, class or markup outside `features/` | Rule (a) is a flag on a store's own region; these set `textContent`, `innerHTML` or the class of a node something else rendered. `state/session/registry.ts` and `app/updates.ts` hold four each, and eight more files the rest. A row goes when the markup says the text instead | `state-dom-touch`'s `WRITES` |
| `state/session/registry.ts` reaches four ids and its header names none | It reaches `#stage`, `#flash`, `#title` and `#ta` through the page's own `$`, while its header is about which conversation the page is on. One sentence in that header takes the row off, and the gate then fails until it is deleted | `state-dom-touch`'s `SILENT` |
| `.xaedit` keeps its old prefix | Five `page.css` rules scope it; it renames with them | `check-class-namespace`'s `LEGACY_LOCAL.extAgents` |
| 205 optional contract fields the fixtures never send | Most are one state this canvas is deliberately in; the header names the few a page really draws and this library has never exercised | `fixture-shape`'s `UNSENT` |
| 18 methods with no offline answer | Each entry says why the offline page has nothing to answer with | `offline-coverage`'s `EXEMPT` |
| 17 files inside a runtime cycle, in two components | The session knot is the large one; `state/session/naming.ts` is in it because it was carved out of `runtime.ts`, which already was. Inverting `runtime.ts`'s two calls into it is the way back to 16 | `import-direction`'s `CYCLES` and `IN_CYCLES` |
| 59 cross-domain edges, eight of them the desk's | Splitting the desk out of `features/workspace/` turned eight intra-domain edges into cross-domain ones. Same imports, same runtime edges, two domains | `import-direction`'s `CROSS` |
| `src/app/boot.ts` assigns one seam key of its own, exactly once | The first frame's claim installs the session source because the `holdRail()` on the next line reads it, and the installer runs later in the boot sequence. The row pins the count, so a second write of `sources.rail` -- spelled as an assignment, a destructuring target, or through a name the file bound to the seam -- is not covered by that reason. Inverting the two is what takes the row off | `seam-assignment`'s `EXCEPTIONS` |
| 38 non-English runs in 14 files | A run is an unbroken stretch of CJK, the nearest thing to a word in a script written without spaces. Six are the served first frame, which has no catalogue to read; sixteen words and two separators a reader sees before a language is picked, and the boot and region goldens are taken from that frame; thirteen spell out three patterns matched against text and one is a comparison with a served title | `first-frame-literals`'s `PINNED` |
| `curly` is off | 2,063 one-line guards | `eslint.config.js` |
| `rpc-schema/openrpc.json` disagrees with its own descriptions in three places | `CronJobInfo.next_run_at_ms` / `last_run_at_ms` are sent as null against an integer schema, and `PlaybookNode.skills` / `mcps` describe three states against an array schema. `Wire<T>` is this page's accommodation; the schema is the cure, and it is outside `ui-web/` | `src/rpc/fixtureTransport.ts`'s header |
