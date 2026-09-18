# ui-web

The page `raven serve` hosts: one HTML file with the stylesheet and the whole
script inlined, plus a directory of icons beside it. `dist/index.html` is what
the wheel ships and what nginx serves in a container; nothing on the page
fetches a second script.

The conventions a change here follows -- the layering, the shape of a domain,
the store, the naming, the gates -- are `CONTRIBUTING.md`. The vocabulary is
`CONTEXT.md`.

## Layout

| Path | What it is |
|---|---|
| `src/page.html` | the document skeleton: `<head>`, the two pre-JavaScript shells, and the two markers `build.py` splices into |
| `src/main.tsx` | the entry, a straight line: the page root, the listeners, the island roots, the transport, the boot |
| `src/App.tsx` | the page root: every region at the body, in one portal, in document order |
| `src/app/` | the page's lifecycle, called once from `main.tsx`: the boot's ordered list, the wiring, the connection, the update watch, the splash |
| `src/rpc/` | the typed gateway seam: the generated method contract, the gateway slot, the three transports and the offline fixture library under `rpc/fixtures/` |
| `src/state/` | the stores and tables every region reads (`store.ts` is the one store shape, `sources.ts` the data seam, `pages.ts` the page table), plus `lang/` and `session/` |
| `src/chrome/` | the page's own furniture -- rail, chat header, dock, sheet rack, tooltip, chips and their popovers -- plus `behaviour/`, the two it installs rather than renders |
| `src/features/<domain>/` | one domain per directory: its `types.ts`, `source.ts`, `store.ts`, `<Domain>App.tsx` and `manifest.ts`, plus its own `wire.ts`, `mount.tsx` or `styles.css` where it needs them. `features/manifests.ts` assembles those declarations and `features/hosts.ts` holds the three detached nodes a tab re-attaches |
| `src/components/` | the small components more than one region renders |
| `src/lib/` | helpers with no store and no page of their own (`lib/dom.ts` is the page's `$`) |
| `src/i18n/` | `t.ts`: the lookup into the shared catalogue at the repo root |
| `src/test/` | helpers and suites about the assembled page, and the region goldens under `src/test/__golden__/` |
| `src/styles/page.css` | the page's own stylesheet -- the frame and the `--z` ladder -- inlined at `/*__STYLE__*/`. A domain's own rules live in its `styles.css` |
| `src/assets/` | icons served from `dist/assets` |
| `scripts/` | the seven tools: the client generator, the three artifact checks, the boot snapshot, the test harness and the gates' fixture call table |
| `scripts/gates/` | the gates vitest runs over the tree, the stylesheet and the contract |

Nothing is published on `window`, and nothing outside the bundle reaches in: a
module imports what it calls, and the direction those imports may run in is
`scripts/gates/import-direction.test.mjs`. Where the direction forbids the
import -- `src/state/` calling into a domain -- the call goes through a callback
the state module declares and the domain fills (`state/page.ts`'s `onShow`,
`state/navfly.ts`'s `onMark`).

## Adding a domain

Seven steps, and which gate reddens for each omission:
`CONTRIBUTING.md` section 10.

## Build

Two steps, in this order:

```sh
npm ci
npm run build          # Vite -> .modern/modern.iife.js and .modern/domains.css
python3 build.py       # splice into src/page.html -> dist/index.html, copy assets
```

They are separate because `build.py` must run where npm may not be on PATH:
the installer adds npm inside a subshell, and the python step is what the
release wheel and `make build-ui` call. `build.py` is three substitutions and a
copy -- every line of JavaScript the page runs comes from the one bundle, and
the one `<style>` block is `styles/page.css` followed by the domains' own
sheets, which Vite collects into `.modern/domains.css` -- and it ends by booting
the artifact in happy-dom twice: once on its fixtures (`?stub=1`), once in live
mode with no gateway answering, comparing each DOM shape against its golden
under `scripts/__golden__/`, so a structural regression fails the build rather
than the browser.

## Develop

```sh
raven serve            # start the gateway first, in another terminal
npm run dev            # http://127.0.0.1:5173/src/page.html
```

The dev server serves `src/page.html` with hot reload and proxies `/rpc`,
`/health`, `/auth`, `/file`, `/files`, `/knowledge/file` and `/oauth/callback`
to the gateway, whose port it reads from `~/.raven/serve.json` (18792 if there
is no record). Same origin, so the session cookie works: sign in once through
the proxied `/auth` page. Dev never produces a `dist/` -- the artifact only
ever comes from the two build steps above.

Without a gateway, `?stub=1` runs the page against its own fixtures. So does
opening `dist/index.html` from disk.

## Checks

```sh
npm test                               # vitest: every unit suite and every gate
npm run type-check                     # tsc
npm run lint                           # eslint: import order, unused imports and locals
npm run gen:check                      # src/rpc/generated.ts matches the schema
node scripts/check-page.mjs            # the artifact: one style, two inline scripts, no markers
node scripts/check-css.mjs
node scripts/check-class-namespace.mjs
```

The page checks read `dist/`, so build before running them. The two boot
snapshots run on their own, from `build.py`. The last two also run as gates
under `npm test`, so the inner loop covers them.

`make lint-ui` at the repo root is `gen:check` plus `type-check`; the line the
maintainer has yet to add to that target is `npm run lint --prefix ui-web`,
between the two, matching the order `lint-tui` uses. The `Makefile` is outside
this directory, which is why it is a note here rather than a change.

## Gates

`npm test` is one run over both trees, so the 35 files under `scripts/gates/`
go with the unit suites. Each one's header says what it pins and why; the
conventions they are written to are `CONTRIBUTING.md` section 8.

| Gate | What it pins |
|---|---|
| `import-direction` | which way an import may point; every upward and cross-domain edge, and every runtime cycle, as shrink-only lists |
| `domain-shape` | the files a domain has and the name its root component exports |
| `domain-registration` | every domain declared once, every page claimed by one domain, every seam key answered by one |
| `store-shape` | one store shape and one set of verbs; the listener set lives only in `state/store.ts` |
| `state-dom-touch` | how often each module in `state/` and `app/` may reach for an element, and zero for `lib/` and `components/` |
| `island-lang` | every island's root subscribes to the language store |
| `i18n-keys` | every literal key exists in the catalogue, sits under a namespace, and the namespace belongs to one domain |
| `file-names` | what a file may be called: PascalCase components, camelCase modules, `<module>.test.ts`, kebab-case gates |
| `rpc-names` | every method name is in the contract, and every call inside `features/` is in that domain's `source.ts` |
| `fixture-shape` | every offline responder answers the shape the contract declares, plus the fields it never sends |
| `offline-coverage` | every method the page calls has an offline answer, or a reason it does not |
| `fixture-now` | the offline library has no clock of its own |
| `check-class-namespace` | a domain's class names carry the domain's prefix, in a `className` attribute or inside a `className={...}` expression; the shared and unprefixed debts may only shrink |
| `check-css` | the stylesheet's own declaration-level invariants |
| `boot-order` | the page defers its first data-driven paint until every source is installed |
| `first-run-model-setup` | a page with no provider configured sends every task action to Models |
| `permission-chip` | the permission chip mirrors every settings load |
| `rail-nav-registry` | every rail button a page can light is one the rail writes to |
| `pipeline-coverage` | every turn event the contract declares has a stage, and no stage names one it does not |
| `notifications-contract` | the notification table is the server's roster plus exactly one name |
| `provider-mark-assets` | every logo the provider map names is in the bundle, and every file is named |
| `dag-renderer` | the CSS contract that keeps the compact DAG card content-sized and scroll-free |
| `dag-label-css` | a dag node's label is cut by the node, in the stylesheet |
| `agent-mark-css` | which agent marks the dark theme filters, and with which filter |
| `agent-fold-css` | the roster's fold slot keeps its width on a head that cannot fold |
| `rail-row-css` | the session row's height contract |
| `rail-sig-css` | every state the session row's tail slot can hold has a visible mark |
| `desk-header-css` | the pane header's two controls sit on one line |
| `desk-layer-css` | the three layers a drag puts on the desk, in order |
| `desk-reserve-css` | the chat's right inset while the anchored desk is over it |
| `desk-page-gate` | the desk is bound to the chat, by a stylesheet rule |
| `instance-composer-css` | a sub-agent's composer is the page's composer, one size down |
| `agents-roster-live` | what the desk's agent list is a list of, on the shipped wiring |
| `direct-chat-media-live` | the instance composer's send carries its attachments |
| `playbooks-live` | the page installs a playbook source that speaks the contract |
