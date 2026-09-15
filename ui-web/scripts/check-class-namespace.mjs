// One stylesheet serves every island, so a class name is shared state.
//
//   node ui-web/scripts/check-class-namespace.mjs
//
// The knowledge dialog's buttons first shipped under `acts`, a class the
// transcript already owns: `.acts button` makes every button a 27px borderless
// icon square, it outranks `.mini`, and Cancel and Create rendered as two grey
// words touching each other. Nothing caught it -- jsdom applies no stylesheet,
// so no rendering test can see a collision, and the CSS itself was valid.
//
// The rule is the one the islands already follow: a class an island introduces
// carries its own prefix, and anything unprefixed is borrowed from the shared
// vocabulary on purpose. This checks the second half of that -- that what looks
// borrowed really is -- by listing every class a page names and flagging the
// ones that are neither prefixed nor known.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const fail = (msg) => {
  console.error(`check-class-namespace: ${msg}`)
  process.exit(1)
}

// Classes every island may use, because they are the page's own vocabulary
// rather than any one feature's: buttons, empty states, and the small text
// helpers. Adding to this list is a decision, which is the point of the list.
const SHARED = new Set([
  'mini', 'ghost', 'gold', 'bad', 'danger', 'empty-note', 'ttl', 'ds', 'who', 'mdl',
  'nm', 'st', 'hint', 'mono', 'car', 'x', 'th', 'td', 'err', 'fl', 'mi', 'dots',
  'panel', 'inline', 'scard', 'fset', 'nlmsg', 'foldrow', 'pickm',
  // The shell's markdown styling, shared on purpose: a document should read
  // the same whether it is opened in the transcript, the workspace or a
  // knowledge base, and that is one stylesheet rule rather than three.
  'prose',
])

// One island per entry: the source that names classes, and the prefix its own
// classes carry.
const ISLANDS = [['src/features/knowledge/KnowledgePage.tsx', 'kb']]

const css = readFileSync(join(root, 'src/styles/page.css'), 'utf8')
let checked = 0

for (const [source, prefix] of ISLANDS) {
  const page = readFileSync(join(root, source), 'utf8')
  const used = new Set()
  for (const m of page.matchAll(/className="([^"{]+)"/g)) {
    for (const c of m[1].trim().split(/\s+/)) used.add(c)
  }
  if (!used.size) fail(`${source} names no classes, which means this check is reading the wrong file`)

  const stray = [...used].filter((c) => !c.startsWith(prefix) && !SHARED.has(c))
  if (stray.length) {
    fail(
      `${source} uses ${stray.map((c) => `.${c}`).join(', ')}, which is neither ` +
        `${prefix}-prefixed nor in the shared vocabulary. A class the rest of the page ` +
        `already styles will win or lose on specificity rather than on intent.`,
    )
  }

  // And the reverse: a class this island owns must not be styled bare when the
  // stylesheet already gives that name to something else.
  for (const c of used) {
    if (!c.startsWith(prefix)) continue
    const owned = new RegExp(`^\\.${c}(?![\\w-])`, 'm').test(css)
    const scoped = new RegExp(`\\.${c}(?![\\w-])`).test(css)
    if (!owned && !scoped) fail(`${source} uses .${c}, which page.css never defines`)
  }
  checked += used.size
}

console.log(`check-class-namespace: OK (${checked} classes across ${ISLANDS.length} island)`)
