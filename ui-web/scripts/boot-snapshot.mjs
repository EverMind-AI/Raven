import { Window } from 'happy-dom'
// Boots the assembled page in happy-dom and compares the DOM shape it settles
// into against a golden. This is the gate on the structural switches of the
// refactor -- concatenated script to modules, one build shape to another --
// none of which may move a single element.
//
//   node scripts/boot-snapshot.mjs dist/index.html            # stub mode
//   node scripts/boot-snapshot.mjs dist/index.html \
//        --url http://127.0.0.1:18792/ --golden scripts/__golden__/boot-live-noserver.txt
//   node scripts/boot-snapshot.mjs dist/index.html --update   # rewrite the golden
//
// build.py runs the compare form twice, once per mode. Stub mode is the demo
// shell on its fixtures; live mode is what a reader sees when the page loads
// and the gateway is not there, which is a different tree and its own golden.
// A missing golden is written on a developer machine and refused under CI, so
// a fresh checkout cannot pass by accident.
//
// What is recorded: tag, #id, .classes and [data-*] per element, indented by
// depth, text dropped, script and style skipped (the build changes how many
// there are). The two data attributes that carry a phrase rather than a flag
// are recorded by name alone, for the reason beside PHRASE below. Determinism rests on the timer clamps below: the splash lifts on
// a 250ms floor plus a fade, and clamping every timer to 50ms makes the tree
// stop moving within the tick budget rather than depending on wall time.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/** The data attributes whose value is a sentence, not a state. */
const PHRASE = new Set(['data-tip', 'data-label'])

const usage = () => {
  console.error('usage: node scripts/boot-snapshot.mjs <dist/index.html> [--url <url>] [--golden <path>] [--update]')
  process.exit(2)
}

let dist = null
let url = 'http://127.0.0.1:18792/?stub=1'
let golden = fileURLToPath(new URL('./__golden__/boot-stub.txt', import.meta.url))
let update = false
for (let i = 2; i < process.argv.length; i++) {
  const arg = process.argv[i]
  if (arg === '--update') update = true
  else if (arg === '--url') url = process.argv[++i] ?? usage()
  else if (arg === '--golden') golden = resolve(process.argv[++i] ?? usage())
  else if (arg.startsWith('--') || dist) usage()
  else dist = arg
}
if (!dist) usage()

/* Without ?stub=1 the live layer installs and reaches for a gateway. This
   snapshot is taken with none running on purpose -- "what the reader gets when
   serve is down" is a shape worth pinning -- so a refused connection is the
   expected condition here, not a failure. */
const offline = !url.includes('stub=1')
const NETWORK = /ECONNREFUSED|ECONNRESET|ENOTFOUND|EPIPE|socket hang up|fetch failed|Failed to fetch|NetworkError/i
const FATAL = /^(ReferenceError|TypeError|SyntaxError)\b/
const TICKS = 40

const html = readFileSync(dist, 'utf8')
const win = new Window({
  url,
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
win.addEventListener('error', (e) => errors.push(String(e.error?.stack || e.error || e.message)))
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
  /* A flag's value is shape and is recorded; a phrase's is text and is not.
     Two data attributes carry a sentence from the catalogue rather than a
     state -- the hover pill's words and the copy button's label -- so what
     goes in the golden is that the element carries one. Their wording is the
     catalogue's business (scripts/gates/i18n-keys.test.mjs), and recording it
     here made every copy edit, and the language the page boots in, a golden
     change in a file whose subject is the shape. */
  for (const a of data) parts.push(PHRASE.has(a) ? `[${a}]` : `[${a}=${node.getAttribute(a)}]`)
  lines.push(`${'  '.repeat(depth)}${parts.join('')}`)
  for (const child of node.children) walk(child, depth + 1)
}
for (const child of win.document.body.children) walk(child, 0)
await win.happyDOM.close()

const actual = lines.join('\n') + '\n'
/* Offline, only a broken program counts: a name that is not there, a call on
   nothing, a script that does not parse. Everything else reaching the window
   with no gateway behind it is the absent gateway. */
const fatal = offline ? errors.filter((e) => FATAL.test(e) && !NETWORK.test(e)) : errors
if (fatal.length) {
  console.error(`boot-snapshot: ${fatal.length} page error(s) during boot:\n  ${fatal.join('\n  ')}`)
  process.exit(1)
}
if (update || (!existsSync(golden) && !process.env.CI)) {
  mkdirSync(dirname(golden), { recursive: true })
  writeFileSync(golden, actual)
  console.log(`boot-snapshot: golden written (${lines.length} nodes)`)
  process.exit(0)
}
if (!existsSync(golden)) {
  console.error('boot-snapshot: golden missing under CI; run with --update on a developer machine and commit it')
  process.exit(1)
}
const expected = readFileSync(golden, 'utf8')
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
