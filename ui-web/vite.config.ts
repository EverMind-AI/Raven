import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

// Two shapes from one source tree.
//
// `vite build` emits the island bundle as one classic IIFE script. Final
// assembly stays with ui-web/build.py: it inlines this output at the
// /*__MODERN__*/ marker, keeping the one-file dist contract the wheel and
// `raven serve` depend on.
//
// `vite dev` serves src/page.html directly, with the same two markers turned
// into a stylesheet link and a module entry (see devPage below) and with the
// backend routes proxied to a local `raven serve`. Nothing it does reaches the
// artifact: dev is a developer path over the same source, not a second build.
//
// vitest reuses this file with mode 'test' and gets neither.

/** Where a local gateway listens, when one has recorded itself. */
const FALLBACK_PORT = 18792

const gatewayTarget = (): string => {
  let port: unknown
  try {
    port = JSON.parse(readFileSync(join(homedir(), '.raven', 'serve.json'), 'utf8')).port
  } catch {
    port = undefined
  }
  return `http://127.0.0.1:${typeof port === 'number' ? port : FALLBACK_PORT}`
}

/* Every route the gateway answers besides the page itself, so the dev server
   is the same origin to the page as `raven serve` is. Same origin is the
   point: the session cookie is host-scoped and SameSite=Strict, and /rpc
   refuses a cross-origin handshake, which is what rewriteWsOrigin answers for
   the one request that carries an Origin the gateway did not mint.
   `/files` precedes `/file` only for reading; the two carry the same options.
   See raven/rpc/transports/ws.py for the route table. */
const gatewayProxy = () => {
  const target = gatewayTarget()
  return {
    '/rpc': { target, ws: true, rewriteWsOrigin: true },
    '/files': { target },
    '/file': { target },
    '/knowledge/file': { target },
    '/health': { target },
    '/auth': { target },
    '/oauth/callback': { target },
  }
}

const STYLE_BLOCK = '<style>\n/*__STYLE__*/\n</style>'
const SCRIPT_BLOCK = '<script>\n/*__MODERN__*/\n</script>'

/* page.html is written for build.py: it carries markers where the stylesheet
   and the bundle get spliced in. The dev server splices different things into
   the same two places -- a link to the stylesheet Vite watches, and the real
   module entry -- so one page skeleton serves both paths and neither owns a
   copy of the other's markup. */
const devPage = (): Plugin => ({
  name: 'raven-dev-page',
  apply: 'serve',
  transformIndexHtml: {
    order: 'pre',
    handler(html) {
      for (const block of [STYLE_BLOCK, SCRIPT_BLOCK]) {
        if (!html.includes(block)) throw new Error(`vite dev: src/page.html no longer carries ${block}`)
      }
      return html
        .replace(STYLE_BLOCK, '<link rel="stylesheet" href="/src/styles/page.css">')
        .replace('"__ASSETV__"', '"dev"')
        .replace(SCRIPT_BLOCK, '<script type="module" src="/src/main.tsx"></script>')
    },
  },
  configureServer(server) {
    /* Asset URLs are relative (`assets/providers/x.svg`), so from /src/page.html
       they land on /src/assets -- where they really are, except for the one the
       build copies in from ui-web/icon. */
    server.middlewares.use((req, _res, next) => {
      const mark = '/src/assets/raven.svg'
      if (req.url?.startsWith(mark)) req.url = `/icon/raven.svg${req.url.slice(mark.length)}`
      next()
    })
  },
})

export default defineConfig(({ command, mode }) => {
  const dev = command === 'serve' && mode !== 'test'
  return {
    plugins: dev ? [react(), devPage()] : [react()],
    // Lib mode does not substitute NODE_ENV on its own; without this the
    // bundle carries React's development build, three times the size. Scoped
    // to build: vitest shares this config, and tests need the development
    // React (act() only exists there).
    define: command === 'build' ? { 'process.env.NODE_ENV': JSON.stringify('production') } : undefined,
    // `..` is the repo root: the message catalogue the demo layer imports is
    // i18n/messages.json, outside this directory on purpose.
    server: dev
      ? { host: '127.0.0.1', open: '/src/page.html', fs: { allow: ['..'] }, proxy: gatewayProxy() }
      : undefined,
    build: {
      lib: {
        entry: 'src/main.tsx',
        name: 'RavenModern',
        formats: ['iife'],
        fileName: () => 'modern.iife.js',
      },
      outDir: '.modern',
      emptyOutDir: true,
      sourcemap: false,
      target: 'es2020',
    },
  }
})
