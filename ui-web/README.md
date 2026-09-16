# ui-web

The page `raven serve` hosts: one HTML file with the stylesheet and the whole
script inlined, plus a directory of icons beside it. `dist/index.html` is what
the wheel ships and what nginx serves in a container; nothing on the page
fetches a second script.

## Layout

| Path | What it is |
|---|---|
| `src/page.html` | the document skeleton, with the two markers `build.py` splices into |
| `src/styles/page.css` | the stylesheet, inlined at `/*__STYLE__*/` |
| `src/main.tsx` | the entry: mounts the React islands, then installs the legacy layer |
| `src/legacy/` | the two pre-React layers (`demo/` fixtures, `live/` gateway), ES modules with an `install()` each, ordered by `src/legacy/index.js` |
| `src/rpc/`, `src/state/` | the typed gateway seam: generated method contract, WebSocket transport, data sources |
| `src/assets/` | icons served from `dist/assets` |
| `scripts/` | the gates, plus the generators they check |

## Build

Two steps, in this order:

```sh
npm ci
npm run build          # Vite -> .modern/modern.iife.js
python3 build.py       # splice into src/page.html -> dist/index.html, copy assets
```

They are separate because `build.py` must run where npm may not be on PATH:
the installer adds npm inside a subshell, and the python step is what the
release wheel and `make build-ui` call. `build.py` ends by booting the artifact
in happy-dom and comparing its DOM shape against a golden under
`scripts/__golden__/`, so a
structural regression fails the build rather than the browser.

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
node scripts/count-shared-globals.mjs
```

The page checks read `dist/`, so build before running them. The boot snapshot
runs on its own, from `build.py`.
