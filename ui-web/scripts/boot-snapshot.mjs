// Boots the assembled page in happy-dom at ?stub=1 and compares the DOM shape
// it settles into against a golden. This is the gate on the two structural
// switches of the refactor -- concatenated script to modules, then Python
// assembly to the Vite entry -- neither of which may move a single element.
//
//   node scripts/boot-snapshot.mjs dist/index.html            # compare
//   node scripts/boot-snapshot.mjs dist/index.html --update   # rewrite golden
//
// build.py runs the compare form after writing dist/. A missing golden is
// written on a developer machine and refused under CI, so a fresh checkout
// cannot pass by accident.
//
// What is recorded: tag, #id, .classes and [data-*] per element, indented by
// depth, text dropped, script and style skipped (the build changes how many
// there are). Determinism rests on the timer clamps below: the splash lifts on
// a 250ms floor plus a fade, and clamping every timer to 50ms makes the tree
// stop moving within the tick budget rather than depending on wall time.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { Window } from 'happy-dom'

const [, , distArg, flag] = process.argv
if (!distArg) {
  console.error('usage: node scripts/boot-snapshot.mjs <dist/index.html> [--update]')
  process.exit(2)
}
const GOLDEN = fileURLToPath(new URL('./__golden__/boot-stub.txt', import.meta.url))
const TICKS = 40

const html = readFileSync(distArg, 'utf8')
const win = new Window({
  url: 'http://127.0.0.1:18792/?stub=1',
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
const errors = []
win.addEventListener('error', (e) => errors.push(String(e.error || e.message)))
win.document.write(html)
for (let i = 0; i < TICKS; i++) await new Promise((r) => setTimeout(r, 25))

const lines = []
const walk = (node, depth) => {
  if (node.nodeType !== 1) return
  const tag = node.tagName.toLowerCase()
  if (tag === 'script' || tag === 'style') return
  const parts = [tag]
  if (node.id) parts.push(`#${node.id}`)
  const classes = (node.getAttribute('class') || '').trim()
  if (classes) parts.push(`.${classes.split(/\s+/).join('.')}`)
  const data = node.getAttributeNames().filter((a) => a.startsWith('data-')).sort()
  for (const a of data) parts.push(`[${a}=${node.getAttribute(a)}]`)
  lines.push(`${'  '.repeat(depth)}${parts.join('')}`)
  for (const child of node.children) walk(child, depth + 1)
}
for (const child of win.document.body.children) walk(child, 0)
await win.happyDOM.close()

const actual = lines.join('\n') + '\n'
if (errors.length) {
  console.error(`boot-snapshot: ${errors.length} page error(s) during boot:\n  ${errors.join('\n  ')}`)
  process.exit(1)
}
if (flag === '--update' || (!existsSync(GOLDEN) && !process.env.CI)) {
  mkdirSync(new URL('./__golden__/', import.meta.url), { recursive: true })
  writeFileSync(GOLDEN, actual)
  console.log(`boot-snapshot: golden written (${lines.length} nodes)`)
  process.exit(0)
}
if (!existsSync(GOLDEN)) {
  console.error('boot-snapshot: golden missing under CI; run with --update on a developer machine and commit it')
  process.exit(1)
}
const expected = readFileSync(GOLDEN, 'utf8')
if (expected === actual) {
  console.log(`boot-snapshot: OK (${lines.length} nodes match golden)`)
  process.exit(0)
}
const a = expected.split('\n')
const b = actual.split('\n')
let first = 0
while (first < a.length && first < b.length && a[first] === b[first]) first++
console.error(`boot-snapshot: DOM shape differs from golden (${a.length - 1} vs ${b.length - 1} nodes); first difference at line ${first + 1}:`)
console.error(`  golden: ${a[first] ?? '<end>'}`)
console.error(`  actual: ${b[first] ?? '<end>'}`)
process.exit(1)
