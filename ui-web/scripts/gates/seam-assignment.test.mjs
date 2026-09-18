/* Who may put a source on the data seam.
 *
 * `ds('cron')` is a loud failure when nothing installed the cron source, and
 * the value of that is a reader who can answer "where does this page get its
 * data" by opening one file: `src/app/install.ts` installs every domain's
 * source once, synchronously, before the first data-driven paint (`boot-order`
 * is the gate on the ordering). A domain that installs its own source is that
 * answer being somewhere else, and it is also an install nothing sequences --
 * the first island to render would decide what is on the seam.
 *
 * Three doors, all held here:
 *
 *  - `sources.<key> = ...`. The installer, plus the one exception below.
 *  - `setSources(patch)`. `state/sources.ts` exports it as the test seam -- a
 *    case installs what it reads and `resetSources()` takes it back -- and
 *    every caller today is a `.test.` file. A production module calling it
 *    would be the first door with none of the ordering, which is why the rule
 *    is about the call site rather than about the shape of the write.
 *  - `Object.assign(sources, ...)`. What `setSources` is made of, allowed only
 *    in the module that declares the seam.
 *
 * Read with the TypeScript API rather than by regex: what tells
 * `sources.rail = x` apart from `s.sources.rail = x` is that the object is the
 * identifier `sources`, and a comment that quotes an assignment is not one.
 *
 * Out of reach: an alias (`const s = sources; s.rail = ...`), which needs the
 * type checker rather than the syntax. The seam is `Partial<Sources>` and
 * every member is optional, so nothing else makes the fourth door cheaper to
 * walk through than the three above.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const SRC = new URL('../../src/', import.meta.url).pathname

/** The module CONTRIBUTING section 5.3 names: the page's one installer. */
const INSTALLER = 'app/install.ts'

/* The assignments outside it, by file and by the keys they write.
   `app/boot.ts` claims the first frame -- the splash, the rail held on
   skeleton rows -- and holds the rail on those rows until the first session
   list lands. `holdRail()` on the line after it reads the source, so the
   source goes on the seam before the claim rather than after the sequence
   that fills it. Down or gone: this is an exception, not a second installer. */
const EXCEPTIONS = {
  'app/boot.ts': ['rail'],
}

/** The module that declares the seam, and so the only one that may assign in bulk. */
const SEAM = 'state/sources.ts'

/* A floor rather than a count, so a scan that stopped finding the installer
   cannot pass in silence. 22 members today; `domain-registration` is what
   holds the set of keys to the manifests. */
const FLOOR = 18

function* modules(dir) {
  for (const name of readdirSync(dir).sort()) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      if (name !== '__snapshots__' && name !== '__golden__') yield* modules(path)
      continue
    }
    if (/\.tsx?$/.test(name)) yield relative(SRC, path)
  }
}

const parse = (rel) => ts.createSourceFile(
  rel,
  readFileSync(join(SRC, rel), 'utf8'),
  ts.ScriptTarget.ES2022,
  true,
  rel.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
)

/** True for `sources.x` and `sources['x']`, false for any other object's member. */
const onSources = (node) => (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node))
  && ts.isIdentifier(node.expression) && node.expression.text === 'sources'

const WRITES = new Set([
  ts.SyntaxKind.EqualsToken,
  ts.SyntaxKind.QuestionQuestionEqualsToken,
  ts.SyntaxKind.BarBarEqualsToken,
  ts.SyntaxKind.AmpersandAmpersandEqualsToken,
])

/** The key a `sources.x = ...` writes, or the text of the index it used. */
const keyOf = (left) => (ts.isPropertyAccessExpression(left)
  ? left.name.text
  : ts.isStringLiteralLike(left.argumentExpression) ? left.argumentExpression.text : '<computed>')

/** Every seam write in the tree: an assignment, a setSources call, a bulk assign. */
function doors() {
  const assigned = []
  const set = []
  const bulk = []
  for (const rel of modules(SRC)) {
    const sf = parse(rel)
    const walk = (node) => {
      if (ts.isBinaryExpression(node) && WRITES.has(node.operatorToken.kind) && onSources(node.left)) {
        const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1
        assigned.push({ at: `${rel}:${line}`, rel, key: keyOf(node.left) })
      }
      if (ts.isCallExpression(node)) {
        const callee = node.expression
        const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1
        if (ts.isIdentifier(callee) && callee.text === 'setSources') set.push({ at: `${rel}:${line}`, rel })
        if (ts.isPropertyAccessExpression(callee) && callee.name.text === 'assign'
          && node.arguments.some((a) => ts.isIdentifier(a) && a.text === 'sources')) {
          bulk.push({ at: `${rel}:${line}`, rel })
        }
      }
      ts.forEachChild(node, walk)
    }
    walk(sf)
  }
  return { assigned, bulk, set }
}

const isTest = (rel) => rel.includes('.test.')

describe('putting a source on the seam', () => {
  const { assigned, bulk, set } = doors()
  const production = assigned.filter((a) => !isTest(a.rel))

  it('finds the installer it is written about', () => {
    expect(production.filter((a) => a.rel === INSTALLER).length, `${INSTALLER} no longer installs the sources`)
      .toBeGreaterThan(FLOOR)
  })

  it('assigns nowhere else but the installer and the pinned exception', () => {
    const stray = production
      .filter((a) => a.rel !== INSTALLER)
      .filter((a) => !(EXCEPTIONS[a.rel] ?? []).includes(a.key))
      .map((a) => `${a.at} sources.${a.key}`)
    expect(stray, `install the source in ${INSTALLER} (CONTRIBUTING section 5.3), or pin the site in EXCEPTIONS with the reason`)
      .toEqual([])
  })

  it('still needs every exception it pins', () => {
    /* The other direction: an exception whose assignment has moved into the
       installer is a licence nothing uses, and the next reader would take it
       for a rule. */
    const unused = Object.entries(EXCEPTIONS)
      .flatMap(([rel, keys]) => keys
        .filter((key) => !production.some((a) => a.rel === rel && a.key === key))
        .map((key) => `${rel} sources.${key}`))
    expect(unused, 'the assignment is gone -- delete the row, and the exception in CONTRIBUTING section 11 with it')
      .toEqual([])
  })

  it('keeps the test seam to the tests', () => {
    const wrong = set.filter((c) => !isTest(c.rel) && c.rel !== SEAM).map((c) => c.at)
    expect(wrong, `setSources is the test seam: a page module installs in ${INSTALLER} instead`)
      .toEqual([])
  })

  it('assigns the seam in bulk only in the module that declares it', () => {
    const wrong = bulk.filter((c) => c.rel !== SEAM && !isTest(c.rel)).map((c) => c.at)
    expect(wrong, `Object.assign(sources, ...) belongs to ${SEAM}; install one domain at a time`)
      .toEqual([])
  })
})
