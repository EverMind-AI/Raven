# ui-web

The page `raven serve` hosts: one HTML file with the stylesheet and the whole
script inlined, plus a directory of icons beside it. `dist/index.html` is what
the wheel ships and what nginx serves in a container; nothing on the page
fetches a second script.

## Layout

| Path | What it is |
|---|---|
| `src/page.html` | the document skeleton: `<head>`, the two pre-JavaScript shells, and the two markers `build.py` splices into |
| `src/styles/page.css` | the stylesheet, inlined at `/*__STYLE__*/` |
| `src/main.tsx` | the entry, a straight line: the page root, the listeners, the island roots, the transport, the boot |
| `src/App.tsx` | the page root: every region at the body, in one portal, in document order |
| `src/app/` | the page's lifecycle, called once from `main.tsx`: the boot's ordered list, the wiring, the connection, the update watch, the splash |
| `src/rpc/` | the typed gateway seam: the generated method contract, the gateway slot, the three transports and the offline fixture library |
| `src/state/` | the stores every region reads, plus `lang/` and `session/` |
| `src/chrome/` | the page's own furniture -- rail, chat header, dock, sheet rack, tooltip, chips -- plus `behaviour/`, the two it installs rather than renders |
| `src/features/<domain>/` | one island per domain: its component, its store, its `source.ts`, its `manifest.ts`; `features/manifests.ts` is where those declarations are assembled and `features/hosts.ts` holds the three detached nodes a tab re-attaches |
| `src/components/` | the small components more than one region renders |
| `src/lib/` | helpers with no store and no page of their own (`lib/dom.ts` is the page's `$`) |
| `src/assets/` | icons served from `dist/assets` |
| `scripts/` | the seven tools: the two generators, the three artifact checks, the boot snapshot and the test harness |
| `scripts/gates/` | the gates vitest runs over the tree, the stylesheet and the contract |

Nothing is published on `window`, and nothing outside the bundle reaches in: a
writer imports what it calls, and the direction those imports may run in is
`scripts/gates/import-direction.test.mjs`. A page-wide writer that `src/state/`
cannot import from takes a registered callback instead (`state/page.ts`'s
`onShow`). The vocabulary -- region, portal, escape order, language store -- is
defined in `CONTEXT.md`.

## Build

Two steps, in this order:

```sh
npm ci
npm run build          # Vite -> .modern/modern.iife.js
python3 build.py       # splice into src/page.html -> dist/index.html, copy assets
```

They are separate because `build.py` must run where npm may not be on PATH:
the installer adds npm inside a subshell, and the python step is what the
release wheel and `make build-ui` call. `build.py` is three substitutions and a
copy -- every line of JavaScript the page runs comes from the one bundle -- and
it ends by booting the artifact in happy-dom twice: once on its fixtures
(`?stub=1`), once in live mode with no gateway answering, comparing each DOM
shape against its golden under `scripts/__golden__/`, so a structural
regression fails the build rather than the browser.

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

## Gates

```sh
npm test                               # vitest
npm run type-check                     # tsc
npm run gen:check                      # src/rpc/generated.ts matches the schema
node scripts/check-page.mjs            # the artifact: one style, two inline scripts, no markers
node scripts/check-css.mjs
node scripts/check-class-namespace.mjs
```

The page checks read `dist/`, so build before running them. The two boot
snapshots run on their own, from `build.py`.
