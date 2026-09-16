// Boots a built dist/index.html in happy-dom and prints the settled body
// class tree: tag, #id, .classes and [data-*] per element, nesting by
// indentation, no text. script and style elements are skipped so a change in
// how the page ships its script blocks cannot move the snapshot.
//
// Usage: node scripts/codemod/boot-snapshot.mjs <dist/index.html> [url]
import { readFileSync } from 'node:fs'
import { Window } from 'happy-dom'

const TICKS = 40
const dist = process.argv[2]
const url = process.argv[3] || 'http://127.0.0.1:18792/?stub=1'
if (!dist) {
  console.error('usage: boot-snapshot.mjs <dist/index.html> [url]')
  process.exit(2)
}
const html = readFileSync(dist, 'utf8')
const logs = []
const win = new Window({
  url,
  console: {
    ...console,
    log: (...a) => logs.push('log: ' + a.join(' ')),
    warn: (...a) => logs.push('warn: ' + a.join(' ')),
    error: (...a) => logs.push('error: ' + a.join(' ')),
    info: () => {},
    debug: () => {},
  },
  settings: {
    enableJavaScriptEvaluation: true,
    suppressInsecureJavaScriptEnvironmentWarning: true,
    disableJavaScriptFileLoading: true,
    disableCSSFileLoading: true,
    enableImageFileLoading: false,
    disableComputedStyleRendering: true,
    handleDisabledFileLoadingAsSuccess: true,
    timer: { maxTimeout: 50, maxIntervalTime: 50, maxIntervalIterations: 2, preventTimerLoops: true },
  },
})
win.document.write(html)
for (let i = 0; i < TICKS; i++) await new Promise((r) => setTimeout(r, 25))

const SKIP = new Set(['script', 'style'])
const lines = []
const walk = (n, d) => {
  if (n.nodeType !== 1) return
  const tag = n.tagName.toLowerCase()
  if (SKIP.has(tag)) return
  const attrs = []
  if (n.id) attrs.push('#' + n.id)
  const cn = typeof n.className === 'string' ? n.className.trim() : ''
  if (cn) attrs.push('.' + cn.split(/\s+/).join('.'))
  for (const a of n.getAttributeNames ? n.getAttributeNames() : []) {
    if (a.startsWith('data-')) attrs.push(`[${a}=${n.getAttribute(a)}]`)
  }
  lines.push('  '.repeat(d) + tag + attrs.join(''))
  for (const c of n.children) walk(c, d + 1)
}
walk(win.document.body, 0)
process.stdout.write(lines.join('\n') + '\n')
if (process.env.BOOT_SNAPSHOT_LOG) process.stderr.write(logs.join('\n') + '\n')
process.exit(0)
