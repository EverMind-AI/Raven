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
// consistently before there was a rule), and the three debts the tree carries
// are pinned below and may only shrink:
//
//   LEGACY_SHARED -- a class two or more domains name that is not page
//                    vocabulary. A NEW one fails; an existing one reaching one
//                    more domain fails.
//   LEGACY_LOCAL  -- how many of a domain's own classes are still unprefixed.
//                    One more fails.
//   LEGACY_EXPR   -- how many unprefixed classes a domain names from inside a
//                    `className={...}` expression. One more fails.
//
// So the tree is green as it stands and no debt can grow, which is what makes
// features/<domain>/styles.css usable: a domain's new rules go in its own sheet
// under its own prefix (features/extAgents/styles.css says how), and the rules
// already in styles/page.css come across a domain at a time, each move lowering
// a number here.
//
// Reach: every class a domain's non-test .tsx files name, whether the attribute
// is a literal (`className="a b"`) or an expression (`className={...}`) -- and
// inside an expression, every single-quoted, double-quoted and template-literal
// static chunk, split on whitespace. A token glued to an interpolation is a stem
// rather than a class (`kbf-${family}` names `.kbf-md`, never `.kbf-`), so it is
// not read as one. No .ts under features/ names a class today.
//
// What an expression cannot tell apart is a class from a comparison operand:
// `state === 'bad' ? 'v err' : 'v'` names .v and .err and reads 'bad' as a class
// too. So the expression literals are counted in LEGACY_EXPR rather than mixed
// into the two lists above -- an operand there costs a row on one domain's count
// instead of a wrong verdict about a shared class -- and the prefix rule's
// inverse (a prefixed class the stylesheets define) holds over them as it does
// over the attribute literals.
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
  // The state dot, which `bad` above is already two thirds of. A colour that
  // means one thing in one list and another two rows above it in the same panel
  // is worse than a colour that differs from a mock, so the three are one
  // vocabulary and one rule (features/tasks/store.ts's `dot`).
  'run', 'ok',
  // The desk panel's list chrome. Every panel drawn in that 340px slot is the
  // same list with a different row in it -- the tasks view replaces the agents
  // view inside it -- so these belong to the panel rather than to whichever
  // domain is filling it today. `bd` is the row's body and is only ever styled
  // as `.sarow .bd`, beside `.nm` and `.st` which are shared already; `wsempty`
  // is that panel's one empty state, which DeskEmpty draws for every tab.
  'salist', 'sarow', 'wsgrp', 'bd', 'wsempty',
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
  a: 4, act: 3, ag: 2, body: 3, btn: 3, cap: 6, cfind: 2, chev: 2, chgs: 2,
  cmd: 3, ct: 3, d: 5, dact: 3, empty: 2, faint: 2, ff: 2, foot: 4, gap: 3,
  'ghost-ic': 3, grp: 2, h: 4, hd: 6, hubcard: 2, hubgrid: 2, hubpage: 2, ic: 5,
  icb: 2, k: 4, kd: 3, key: 5, l1: 5, l2: 5, lb: 4, led: 2, mk: 2, n: 8, none: 2,
  note: 2, okpill: 2, one: 3, perr: 3, ph: 2, pill: 2, pmback: 3, pmcard: 2,
  pmchips: 2, pmcnt: 2, pmdesc: 3, pmdhead: 4, pmdmeta: 4, pmhead: 2, pmhero: 5,
  pmid: 2, pmnm: 2, pmpub: 2, pmsec: 4, pmsign: 2, pnote: 4,
  'provider-choice-action': 2, rm: 3, row: 3, sheet: 2, shot: 3, sk: 4, skel: 4,
  srow: 2, step: 2, sulist: 3, sustate: 2, swi: 3, sz: 2, t: 3, tag: 3, tick: 2,
  tipdn: 3, tm: 2, top: 2, v: 3, val: 2, w: 4, warn: 3, wkg: 3, wsnote: 2, wt: 2,
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
  /* Up from 7, and every one of the six is a class that MOVED here rather than
     a new one: the pan-and-zoom viewport the playbook page carried became
     features/dag/Board.tsx so the task board could read it too, and its
     `.g*` classes came with it. The playbooks row below falls by the same six,
     so the page's total debt is unchanged -- and the prefix pass that retires
     them is one rename over one file now instead of two. */
  dag: 13,
  desk: 5,
  extAgents: 5,
  installed: 0,
  knowledge: 0,
  memory: 6,
  model: 5,
  onboard: 1,
  playbooks: 13,
  plugins: 3,
  rail: 10,
  settings: 81,
  skills: 4,
  subagents: 39,
  tasks: 9,
  /* Up from 73. The transcript's turn shapes are `.turn` and `.msg` now,
     which retires `.ask`, `.answer-turn` and `.b` -- three names for two. The
     row rises anyway because the two modifiers those shapes carry, `.me` and
     `.ai`, used to be written inside a className expression and are literal
     attributes here: they moved onto this list from the one below, which
     falls from 26 to 25 in the same change. */
  transcript: 75,
  workspace: 37,
}

// The same count for the classes a domain names from inside a `className={...}`
// expression, and on the same terms: down or gone, a zero stays on the list.
// Separate from LEGACY_LOCAL because the two are not measured the same way -- an
// expression literal may be a comparison operand rather than a class (the header
// says why), so a row here is an upper bound on a debt where a row there is the
// debt. A name in both places is counted once, by LEGACY_LOCAL.
const LEGACY_EXPR = {
  browser: 2,
  composer: 3,
  connections: 4,
  cron: 0,
  dag: 3,
  desk: 2,
  extAgents: 1,
  installed: 0,
  knowledge: 0,
  memory: 1,
  model: 3,
  onboard: 1,
  playbooks: 7,
  plugins: 2,
  rail: 2,
  settings: 13,
  skills: 2,
  tasks: 6,
  subagents: 6,
  transcript: 25,
  workspace: 7,
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

/* The expression after each `className={`, to its balanced closing brace --
   quotes and nested `${}` skipped, so a brace inside a string does not end it. */
const expressions = (text) => {
  const out = []
  let at = text.indexOf('className={')
  while (at !== -1) {
    let i = at + 'className={'.length
    const start = i
    let depth = 1
    while (i < text.length && depth > 0) {
      const ch = text[i]
      if (ch === '{') depth += 1
      else if (ch === '}') depth -= 1
      else if (ch === "'" || ch === '"' || ch === '`') i = endOfString(text, i)
      i += 1
    }
    out.push(text.slice(start, i - 1))
    at = text.indexOf('className={', i)
  }
  return out
}

/* The index of the quote that closes the one at `i`. */
const endOfString = (text, i) => {
  const quote = text[i]
  let j = i + 1
  while (j < text.length && text[j] !== quote) {
    if (text[j] === '\\') { j += 2; continue }
    if (quote === '`' && text[j] === '$' && text[j + 1] === '{') { j = endOfHole(text, j + 1) + 1; continue }
    j += 1
  }
  return j
}

/* The index of the brace that closes the `${` whose brace is at `i`. */
const endOfHole = (text, i) => {
  let j = i + 1
  let depth = 1
  while (j < text.length && depth > 0) {
    const ch = text[j]
    if (ch === '{') depth += 1
    else if (ch === '}') depth -= 1
    else if (ch === "'" || ch === '"' || ch === '`') j = endOfString(text, j)
    j += 1
  }
  return j - 1
}

/* Every class token of the static strings in one expression. A chunk that an
   interpolation runs into is a stem, not a class, so the token on that side of
   it is dropped; anything unquoted is an identifier, which is a variable. */
const tokensIn = (expr) => {
  const out = []
  const take = (text, glueStart, glueEnd) => {
    const parts = text.split(/\s+/)
    if (glueStart) parts[0] = ''
    if (glueEnd) parts[parts.length - 1] = ''
    for (const c of parts) if (c) out.push(c)
  }
  let i = 0
  while (i < expr.length) {
    const ch = expr[i]
    if (ch === "'" || ch === '"') {
      const end = endOfString(expr, i)
      take(expr.slice(i + 1, end), false, false)
      i = end + 1
      continue
    }
    if (ch === '`') {
      const end = endOfString(expr, i)
      let at = i + 1
      let glueStart = false
      while (at < end) {
        const hole = expr.indexOf('${', at)
        if (hole === -1 || hole >= end) break
        take(expr.slice(at, hole), glueStart, true)
        glueStart = true
        at = endOfHole(expr, hole + 1) + 1
      }
      take(expr.slice(at, end), glueStart, false)
      i = end + 1
      continue
    }
    i += 1
  }
  return out
}

const prefixes = new Map(domains.map((domain) => [domain, prefixOf(domain)]))
const named = new Map()
const inExpr = new Map()
for (const domain of domains) {
  const files = components(join(src, 'features', domain))
  const used = new Set()
  const fromExpr = new Set()
  for (const file of files) {
    const text = readFileSync(file, 'utf8')
    for (const m of text.matchAll(/className="([^"{]+)"/g)) {
      for (const c of m[1].trim().split(/\s+/)) if (c) used.add(c)
    }
    for (const expr of expressions(text)) {
      for (const c of tokensIn(expr)) fromExpr.add(c)
    }
  }
  if (files.length && !used.size) {
    fail(`features/${domain} renders ${files.length} component(s) and names no class, which means this check is reading the wrong files`)
  }
  named.set(domain, used)
  inExpr.set(domain, fromExpr)
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

/* The same, for the literals inside `className={...}`. A name the domain also
   writes as an attribute is already on one of the two lists above. */
const exprOver = []
const fromExpr = new Map(domains.map((domain) => [domain, []]))
for (const [domain, used] of inExpr) {
  for (const c of used) {
    if (SHARED.has(c) || named.get(domain).has(c)) continue
    if (!c.startsWith(prefixes.get(domain))) fromExpr.get(domain).push(c)
  }
}
for (const domain of domains) {
  const found = fromExpr.get(domain).length
  const pinned = LEGACY_EXPR[domain]
  if (pinned === undefined) exprOver.push(`${domain}: ${found} unprefixed in expressions, not on the list`)
  else if (found > pinned) {
    exprOver.push(`${domain}: ${found} unprefixed in expressions, pinned ${pinned} -- ${fromExpr.get(domain).sort().map((c) => `.${c}`).join(', ')}`)
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
if (exprOver.length) {
  fail(
    `${exprOver.join('; ')}. Same rule inside a className expression as outside one: the domain's ` +
      'prefix, and the rule in features/<domain>/styles.css.',
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
  const pinnedExpr = LEGACY_EXPR[domain]
  if (pinnedExpr !== undefined && fromExpr.get(domain).length < pinnedExpr) {
    stale.push(`${domain}: ${fromExpr.get(domain).length} unprefixed in expressions now, pinned ${pinnedExpr}, lower it`)
  }
}
for (const domain of new Set([...Object.keys(LEGACY_LOCAL), ...Object.keys(LEGACY_EXPR)])) {
  if (!prefixes.has(domain)) stale.push(`${domain}: no such domain, delete the pin`)
}
if (stale.length) fail(`${stale.join('; ')}. All three lists above are down-or-gone.`)

/* And the reverse of the prefix rule: a class a domain owns must be a class the
   stylesheets define. A prefixed name nothing styles is a rule that was renamed
   or deleted from under the markup, which no rendering test can see either. */
const sheets = [readFileSync(join(src, 'styles/page.css'), 'utf8')]
for (const domain of domains) {
  try { sheets.push(readFileSync(join(src, 'features', domain, 'styles.css'), 'utf8')) } catch { /* not every domain has one */ }
}
const css = sheets.join('\n')
const undefined_ = []
for (const domain of domains) {
  for (const c of new Set([...named.get(domain), ...inExpr.get(domain)])) {
    if (!c.startsWith(prefixes.get(domain))) continue
    if (!new RegExp(`\\.${c}(?![\\w-])`).test(css)) undefined_.push(`features/${domain} uses .${c}`)
  }
}
if (undefined_.length) fail(`${undefined_.join(', ')}, which no stylesheet defines`)

const classes = [...named.values()].reduce((n, used) => n + used.size, 0)
const literals = [...inExpr.values()].reduce((n, used) => n + used.size, 0)
console.log(
  `check-class-namespace: OK (${classes} classes and ${literals} expression literals across ` +
    `${domains.length} domains; ${Object.keys(LEGACY_SHARED).length} shared, ` +
    `${Object.values(LEGACY_LOCAL).reduce((a, b) => a + b, 0)} unprefixed and ` +
    `${Object.values(LEGACY_EXPR).reduce((a, b) => a + b, 0)} unprefixed in expressions pinned)`,
)
