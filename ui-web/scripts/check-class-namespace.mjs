// A class name is shared state, so every domain's classes carry that domain's
// prefix -- and what is unprefixed is borrowed from the page's own vocabulary
// on purpose.
//
//   node ui-web/scripts/check-class-namespace.mjs
//
// The knowledge dialog's buttons first shipped under `acts`, a class the
// transcript already owns: `.acts button` makes every button a 27px borderless
// icon square, it outranks `.mini`, and Cancel and Create rendered as two grey
// words touching each other. Nothing caught it -- happy-dom applies no
// stylesheet, so no rendering test can see a collision, and the CSS itself was
// valid.
//
// This used to state that rule page-wide and then check one island against one
// hard-coded prefix, over a 6,000-line global stylesheet. The prefixes are read
// from the domains themselves now (features/<domain>/manifest.ts: the directory
// name, or the `cssPrefix` six of them declare because they named their classes
// consistently before there was a rule), and the two debts the tree carries are
// pinned below and may only shrink:
//
//   LEGACY_SHARED -- a class two or more domains name that is not page
//                    vocabulary. A NEW one fails; an existing one reaching one
//                    more domain fails.
//   LEGACY_LOCAL  -- how many of a domain's own classes are still unprefixed.
//                    One more fails.
//
// So the tree is green as it stands and neither debt can grow, which is what
// makes features/<domain>/styles.css usable: a domain's new rules go in its own
// sheet under its own prefix (features/extAgents/styles.css says how), and the
// rules already in styles/page.css come across a domain at a time, each move
// lowering a number here.
//
// Reach: `className="..."` in a domain's non-test .tsx files. No .ts under
// features/ names a class today, and a class assembled at runtime
// (`className={...}`) is outside this -- a literal inside such an expression is
// as often a comparison operand as a class, and a gate that reported those
// would be reporting the wrong thing.
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const src = join(root, 'src')
const fail = (msg) => {
  console.error(`check-class-namespace: ${msg}`)
  process.exit(1)
}

// Classes every domain may use, because they are the page's own vocabulary
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
  // Every vendor logo on the page wears this, from one component and one rule
  // in components/ProviderMark.tsx -- including its dark-mode handling, which an
  // island restyling the class for itself would get wrong.
  'provider-icon',
])

// Every class two or more domains name, with the number of domains that name
// it, as the tree stands. Down or gone: a higher count is a class spreading
// further, and a count this gate can no longer measure is a pin to delete.
//
// These are not shared vocabulary -- nobody decided them. `.n`, `.hd`, `.cap`,
// `.row`, `.key` are the names a writer reaches for first, and the `.pm*`
// family is one domain's rows that a second domain drew the same way. Either
// end of a row here can retire it: move the rule into one domain's styles.css
// and prefix it there, or put the name in SHARED on purpose.
const LEGACY_SHARED = {
  a: 4, act: 3, ag: 2, bd: 2, body: 3, btn: 3, cap: 6, cfind: 2, chev: 2, chgs: 2,
  cmd: 3, ct: 3, d: 5, dact: 3, empty: 2, faint: 2, ff: 2, foot: 4, gap: 3,
  'ghost-ic': 3, grp: 2, h: 4, hd: 6, hubcard: 2, hubgrid: 2, hubpage: 2, ic: 5,
  icb: 2, k: 4, kd: 3, key: 5, l1: 5, l2: 5, lb: 4, led: 2, mk: 2, n: 8, none: 2,
  note: 2, okpill: 2, one: 3, perr: 3, ph: 2, pill: 2, pmback: 3, pmcard: 2,
  pmchips: 2, pmcnt: 2, pmdesc: 3, pmdhead: 4, pmdmeta: 4, pmhead: 2, pmhero: 5,
  pmid: 2, pmnm: 2, pmpub: 2, pmsec: 4, pmsign: 2, pnote: 4,
  'provider-choice-action': 2, rm: 3, row: 3, sheet: 2, shot: 3, sk: 4, skel: 4,
  srow: 2, step: 2, sulist: 3, sustate: 2, swi: 3, sz: 2, t: 3, tag: 3, tick: 2,
  tipdn: 3, tm: 2, top: 2, v: 3, val: 2, w: 4, warn: 3, wkg: 3, wsempty: 2,
  wsnote: 2, wt: 2,
}

// How many of a domain's own classes -- the ones no other domain names -- still
// carry no prefix, as the tree stands. Down or gone, one domain at a time: the
// rules are in styles/page.css, and moving a domain's rules into its own
// styles.css is where the renames belong (page.css is where the boot goldens
// and the region goldens take their bytes from, so a rename without the move
// is a golden churn for nothing).
//
// A zero is a domain that is done, and it stays on the list: the entry is what
// says the count was measured rather than forgotten.
const LEGACY_LOCAL = {
  browser: 24,
  composer: 7,
  connections: 5,
  cron: 13,
  dag: 7,
  desk: 5,
  extAgents: 5,
  installed: 0,
  knowledge: 0,
  memory: 6,
  model: 5,
  onboard: 1,
  playbooks: 14,
  plugins: 3,
  rail: 10,
  settings: 81,
  skills: 4,
  subagents: 41,
  transcript: 73,
  workspace: 38,
}

const domains = readdirSync(join(src, 'features'))
  .filter((name) => statSync(join(src, 'features', name)).isDirectory())
  .sort()

/* The prefix from the domain's own declaration, read from the source text: a
   gate about names has no business importing twenty islands to learn them. */
const prefixOf = (domain) => {
  const text = readFileSync(join(src, 'features', domain, 'manifest.ts'), 'utf8')
  return text.match(/\bcssPrefix:\s*'([^']+)'/)?.[1] ?? domain
}

const components = (dir, out = []) => {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (entry.name !== '__snapshots__') components(path, out)
    } else if (entry.name.endsWith('.tsx') && !entry.name.endsWith('.test.tsx')) {
      out.push(path)
    }
  }
  return out
}

const prefixes = new Map(domains.map((domain) => [domain, prefixOf(domain)]))
const named = new Map()
for (const domain of domains) {
  const files = components(join(src, 'features', domain))
  const used = new Set()
  for (const file of files) {
    for (const m of readFileSync(file, 'utf8').matchAll(/className="([^"{]+)"/g)) {
      for (const c of m[1].trim().split(/\s+/)) if (c) used.add(c)
    }
  }
  if (files.length && !used.size) {
    fail(`features/${domain} renders ${files.length} component(s) and names no class, which means this check is reading the wrong files`)
  }
  named.set(domain, used)
}

const owners = new Map()
for (const [domain, used] of named) {
  for (const c of used) {
    if (!owners.has(c)) owners.set(c, [])
    owners.get(c).push(domain)
  }
}
if (!owners.size) fail('no domain names a class, which means this check is reading the wrong tree')

const spreading = []
const unpinnedShared = []
const localOver = []
const local = new Map(domains.map((domain) => [domain, []]))
for (const [c, ds] of owners) {
  if (SHARED.has(c)) continue
  if (ds.length >= 2) {
    const pinned = LEGACY_SHARED[c]
    if (pinned === undefined) unpinnedShared.push(`.${c} (${ds.join(', ')})`)
    else if (ds.length > pinned) spreading.push(`.${c}: ${ds.length} domains, pinned ${pinned} (${ds.join(', ')})`)
    continue
  }
  const domain = ds[0]
  if (!c.startsWith(prefixes.get(domain))) local.get(domain).push(c)
}
for (const domain of domains) {
  const found = local.get(domain).length
  const pinned = LEGACY_LOCAL[domain]
  if (pinned === undefined) localOver.push(`${domain}: ${found} unprefixed, not on the list`)
  else if (found > pinned) {
    localOver.push(`${domain}: ${found} unprefixed, pinned ${pinned} -- ${local.get(domain).sort().map((c) => `.${c}`).join(', ')}`)
  }
}

if (unpinnedShared.length) {
  fail(
    `${unpinnedShared.join(', ')} ${unpinnedShared.length === 1 ? 'is named' : 'are named'} by more than one ` +
      'domain and is not the page\'s vocabulary. Give the class the owning domain\'s prefix and put its ' +
      'rule in that domain\'s styles.css, or add it to SHARED because every domain should look the same here.',
  )
}
if (spreading.length) {
  fail(
    `${spreading.join('; ')}. A class already shared reached one more domain: prefix it for the new ` +
      'domain rather than borrowing another\'s name.',
  )
}
if (localOver.length) {
  fail(
    `${localOver.join('; ')}. A class a domain introduces carries that domain's prefix ` +
      '(features/<domain>/manifest.ts declares it, or the directory name is it) and its rule goes in ' +
      'features/<domain>/styles.css.',
  )
}

/* Down or gone, the other half: a pin the tree has outgrown has to come off,
   or the numbers stop meaning anything a year from now. */
const stale = []
for (const [c, pinned] of Object.entries(LEGACY_SHARED)) {
  const ds = owners.get(c) ?? []
  if (ds.length < 2) stale.push(`.${c}: ${ds.length} domain(s) now, delete the pin`)
  else if (ds.length < pinned) stale.push(`.${c}: ${ds.length} domains now, pinned ${pinned}, lower it`)
}
for (const domain of domains) {
  const pinned = LEGACY_LOCAL[domain]
  if (pinned !== undefined && local.get(domain).length < pinned) {
    stale.push(`${domain}: ${local.get(domain).length} unprefixed now, pinned ${pinned}, lower it`)
  }
}
for (const domain of Object.keys(LEGACY_LOCAL)) {
  if (!prefixes.has(domain)) stale.push(`${domain}: no such domain, delete the pin`)
}
if (stale.length) fail(`${stale.join('; ')}. Both lists above are down-or-gone.`)

/* And the reverse of the prefix rule: a class a domain owns must be a class the
   stylesheets define. A prefixed name nothing styles is a rule that was renamed
   or deleted from under the markup, which no rendering test can see either. */
const sheets = [readFileSync(join(src, 'styles/page.css'), 'utf8')]
for (const domain of domains) {
  try { sheets.push(readFileSync(join(src, 'features', domain, 'styles.css'), 'utf8')) } catch { /* not every domain has one */ }
}
const css = sheets.join('\n')
const undefined_ = []
for (const [domain, used] of named) {
  for (const c of used) {
    if (!c.startsWith(prefixes.get(domain))) continue
    if (!new RegExp(`\\.${c}(?![\\w-])`).test(css)) undefined_.push(`features/${domain} uses .${c}`)
  }
}
if (undefined_.length) fail(`${undefined_.join(', ')}, which no stylesheet defines`)

const classes = [...named.values()].reduce((n, used) => n + used.size, 0)
console.log(
  `check-class-namespace: OK (${classes} classes across ${domains.length} domains; ` +
    `${Object.keys(LEGACY_SHARED).length} shared and ${Object.values(LEGACY_LOCAL).reduce((a, b) => a + b, 0)} ` +
    'unprefixed pinned)',
)
