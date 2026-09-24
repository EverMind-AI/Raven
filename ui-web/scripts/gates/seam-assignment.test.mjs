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
 * Four doors, all held here:
 *
 *  - `sources.<key> = ...`. The installer, plus the one exception below,
 *    which is pinned by count: the reason a file may write one key once does
 *    not cover a second write of the same key, so the number is part of the
 *    licence rather than the file and the key alone. A destructuring pattern
 *    (`({ rail: sources.rail } = x)`, `[sources.rail] = y`) is the same write
 *    with the member nowhere near the `=`, and counts as the write it is.
 *  - `setSources(patch)`. `state/sources.ts` exports it as the test seam -- a
 *    case installs what it reads and `resetSources()` takes it back -- and
 *    every caller today is a test file. A production module calling it would
 *    be the first door with none of the ordering, which is why the rule is
 *    about the call site rather than about the shape of the write. Naming it
 *    counts as calling it: an import or a local alias of the test seam in a
 *    production module is that call one line away.
 *  - `Object.assign(sources, ...)`, and `defineProperty` / `defineProperties`
 *    with it. What `setSources` is made of, allowed only in the module that
 *    declares the seam.
 *  - `export { sources as x }`, `export const x = setSources`. A re-export is
 *    a second name for the seam in a module that is not the seam, and the name
 *    it arrives under downstream is one no file's own syntax can see.
 *
 * Read with the TypeScript API rather than by regex: what tells
 * `sources.rail = x` apart from `s.sources.rail = x` is that the object is the
 * identifier `sources`, and a comment that quotes an assignment is not one.
 *
 * The name a file uses, not the module graph. `import { sources as seam }`,
 * `import * as s from '../state/sources'` and `const seam = sources` all bind
 * a name that stands for the seam, so the doors above are matched against the
 * set of names one file bound rather than against the spelling `sources`. The
 * set is built from that file's own syntax and needs no type checker.
 *
 * Out of reach: the seam reached through a value rather than through a name --
 * a function that hands it out (`seam().rail = ...`), or a module renamed on
 * the way in (`export * as x from '../state/sources'`, then a member of `x`).
 * Those need the type checker; a rename of the binding is held, in the file
 * that renames it. The seam is `Partial<Sources>` and every member is
 * optional, so nothing else makes a further door cheaper to walk through than
 * the four above.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { relPath, root } from './paths.mjs'

const SRC = root(new URL('../../src/', import.meta.url))

/** The module CONTRIBUTING section 5.3 names: the page's one installer. */
const INSTALLER = 'app/install.ts'

/* The assignments outside it, by file, by key, and by how many writes of that
   key the row licenses. `app/boot.ts` claims the first frame -- the splash, the
   rail held on skeleton rows -- and holds the rail on those rows until the
   first session list lands. `holdRail()` on the line after it reads the source,
   so the source goes on the seam before the claim rather than after the
   sequence that fills it. That reason is one assignment's, hence the 1: a
   second `sources.rail = ...` in the same file would be a second installer
   standing on the first one's reason. Down or gone: this is an exception, not
   a second installer. */
const EXCEPTIONS = {
  'app/boot.ts': { rail: 1 },
}

/** The module that declares the seam, and so the only one that may assign in bulk. */
const SEAM = 'state/sources.ts'

/* A floor rather than a count, so a scan that stopped finding the installer
   cannot pass in silence. 19 installs today, and the floor is one under them:
   the slack is deliberate and it is one domain's worth, so retiring a domain
   is one change rather than two, while a second install going quiet -- or a
   walk that lost the file -- fails here. `domain-registration` is what holds
   the set of keys to the manifests. */
const FLOOR = 18

/* Every file the walk reads. By extension rather than by directory: the region
   goldens are `.txt` and the component snapshots `.snap`, which nothing here
   matches, so no directory has to be exempted by name -- and a source file
   that lands under a directory someone named `__golden__` is read like any
   other. `.js`/`.mjs`/`.cjs` are in it for the same reason: none is under
   `src/` today, and the day one is, it is a module like its neighbours. */
const CODE = /\.(?:[cm]?js|[jt]sx?)$/

function* modules(dir) {
  for (const name of readdirSync(dir).sort()) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      yield* modules(path)
      continue
    }
    if (CODE.test(name)) yield relPath(SRC, path)
  }
}

/* TS parses a `.js` or `.mjs` file too; the kind only decides whether `<` opens
   an element. */
const parse = (rel) => ts.createSourceFile(
  rel,
  readFileSync(join(SRC, rel), 'utf8'),
  ts.ScriptTarget.ES2022,
  true,
  rel.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
)

/* A test is a file whose own name ends in `.test.ts(x)`. Not a path containing
   `.test.`: that reads `store.test.helpers.ts` -- a production module -- as a
   test, and every file under a directory someone named `x.test` with it. */
const isTest = (rel) => /(^|\.)test\.[jt]sx?$/.test(rel.slice(rel.lastIndexOf('/') + 1))

/** A name that stands for the seam object in the file being read. */
const isSeam = (node, names) => (ts.isIdentifier(node) && names.seam.has(node.text))
  || (ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression)
    && names.namespaces.has(node.expression.text) && node.name.text === 'sources')

/** A name that stands for `setSources` in the file being read. */
const isInstaller = (node, names) => (ts.isIdentifier(node) && names.install.has(node.text))
  || (ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression)
    && names.namespaces.has(node.expression.text) && node.name.text === 'setSources')

/* What the seam and its test door are called inside one file: the two names
   `state/sources.ts` exports, plus every alias the file binds to them -- an
   import specifier that renames, a namespace import, a `const` that takes the
   value. Collected in source order, so an alias of an alias is in it too. */
function bound(sf) {
  const names = { install: new Set(['setSources']), namespaces: new Set(), seam: new Set(['sources']) }
  const walk = (node) => {
    const bindings = ts.isImportDeclaration(node) ? node.importClause?.namedBindings : undefined
    if (bindings && ts.isNamespaceImport(bindings)) names.namespaces.add(bindings.name.text)
    if (bindings && ts.isNamedImports(bindings)) {
      for (const element of bindings.elements) {
        const imported = (element.propertyName ?? element.name).text
        if (imported === 'sources') names.seam.add(element.name.text)
        if (imported === 'setSources') names.install.add(element.name.text)
      }
    }
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      if (isSeam(node.initializer, names)) names.seam.add(node.name.text)
      if (isInstaller(node.initializer, names)) names.install.add(node.name.text)
    }
    ts.forEachChild(node, walk)
  }
  walk(sf)
  return names
}

/** True for a member of the seam under any of those names, false for any other object's. */
const onSeam = (node, names) => (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node))
  && isSeam(node.expression, names)

const WRITES = new Set([
  ts.SyntaxKind.EqualsToken,
  ts.SyntaxKind.QuestionQuestionEqualsToken,
  ts.SyntaxKind.BarBarEqualsToken,
  ts.SyntaxKind.AmpersandAmpersandEqualsToken,
])

/** The bulk writes, which take the whole object rather than one key. */
const BULK = new Set(['assign', 'defineProperties', 'defineProperty'])

/** The key a `sources.x = ...` writes, or the text of the index it used. */
const keyOf = (left) => (ts.isPropertyAccessExpression(left)
  ? left.name.text
  : ts.isStringLiteralLike(left.argumentExpression) ? left.argumentExpression.text : '<computed>')

/* Every expression one assignment writes to. Usually the left of the `=` is
   the member itself; when it is an object or an array literal the assignment
   is a destructuring pattern and the members it writes are inside it, one per
   property or element, with a nested pattern or a default around them. */
function* targets(node) {
  if (ts.isParenthesizedExpression(node)) {
    yield* targets(node.expression)
  } else if (ts.isObjectLiteralExpression(node)) {
    for (const property of node.properties) {
      if (ts.isPropertyAssignment(property)) yield* targets(property.initializer)
      if (ts.isSpreadAssignment(property)) yield* targets(property.expression)
    }
  } else if (ts.isArrayLiteralExpression(node)) {
    for (const element of node.elements) yield* targets(element)
  } else if (ts.isSpreadElement(node)) {
    yield* targets(node.expression)
  } else if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
    yield* targets(node.left)
  } else {
    yield node
  }
}

/** Whether a statement carries `export`, which is what hands a name onward. */
const exported = (node) => node.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword) === true

/** Which of the seam's two names this expression is a second name for, if either. */
const secondName = (node, names) => {
  if (isSeam(node, names)) return 'the seam'
  return isInstaller(node, names) ? 'setSources' : ''
}

/** Every seam write in the tree: a key, the test seam, a bulk assign, a re-export. */
function doors() {
  const assigned = []
  const bulk = []
  const handed = []
  const set = []
  for (const rel of modules(SRC)) {
    const sf = parse(rel)
    const names = bound(sf)
    const at = (node) => `${rel}:${sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1}`
    const walk = (node) => {
      if (ts.isBinaryExpression(node) && WRITES.has(node.operatorToken.kind)) {
        for (const target of targets(node.left)) {
          if (onSeam(target, names)) assigned.push({ at: at(target), key: keyOf(target), rel })
        }
      }
      if (ts.isCallExpression(node)) {
        if (isInstaller(node.expression, names)) set.push({ at: at(node), how: 'calls setSources', rel })
        if (ts.isPropertyAccessExpression(node.expression) && BULK.has(node.expression.name.text)
          && node.arguments.some((argument) => isSeam(argument, names))) {
          bulk.push({ at: at(node), rel })
        }
      }
      if (ts.isImportSpecifier(node) && (node.propertyName ?? node.name).text === 'setSources') {
        set.push({ at: at(node), how: 'imports setSources', rel })
      }
      if (ts.isExportSpecifier(node)) {
        const second = secondName(node.propertyName ?? node.name, names)
        if (second) handed.push({ at: at(node), how: second, rel })
      }
      if (ts.isVariableStatement(node) && exported(node)) {
        for (const declaration of node.declarationList.declarations) {
          const second = declaration.initializer ? secondName(declaration.initializer, names) : ''
          if (second) handed.push({ at: at(declaration), how: second, rel })
        }
      }
      ts.forEachChild(node, walk)
    }
    walk(sf)
  }
  return { assigned, bulk, handed, set }
}

/** Every pinned exception beside the assignments the tree really carries for it. */
const counted = (production) => Object.entries(EXCEPTIONS)
  .flatMap(([rel, keys]) => Object.entries(keys).map(([key, pinned]) => ({
    rel,
    key,
    pinned,
    found: production.filter((a) => a.rel === rel && a.key === key),
  })))

/** The sites a rule is about: outside the tests, outside the seam's own module. */
const elsewhere = (sites) => sites.filter((site) => !isTest(site.rel) && site.rel !== SEAM)

describe('putting a source on the seam', () => {
  const { assigned, bulk, handed, set } = doors()
  const production = assigned.filter((a) => !isTest(a.rel))
  const exceptions = counted(production)

  it('finds the installer it is written about', () => {
    expect(production.filter((a) => a.rel === INSTALLER).length, `${INSTALLER} no longer installs the sources`)
      .toBeGreaterThanOrEqual(FLOOR)
  })

  it('assigns nowhere else but the installer and the pinned exception', () => {
    const stray = production
      .filter((a) => a.rel !== INSTALLER)
      .filter((a) => !Object.hasOwn(EXCEPTIONS[a.rel] ?? {}, a.key))
      .map((a) => `${a.at} sources.${a.key}`)
    expect(stray, `install the source in ${INSTALLER} (CONTRIBUTING section 5.3), or pin the site in EXCEPTIONS with the reason`)
      .toEqual([])
  })

  it('holds every exception to the number of writes it pins', () => {
    /* The count is the exception. Reading the row as "this file may write this
       key" leaves a licence that grows in silence: the same line twice, or a
       second site in the same file, would inherit a reason written for one
       assignment and nothing here would say so. */
    const over = exceptions
      .filter((e) => e.found.length > e.pinned)
      .map((e) => `${e.rel} sources.${e.key} -- ${e.found.map((a) => a.at).join(', ')} (pinned ${e.pinned})`)
    expect(over, 'the first-frame exception is one assignment; a second write goes through app/install.ts')
      .toEqual([])
  })

  it('still needs every exception it pins', () => {
    /* The other direction: an exception whose assignment has moved into the
       installer is a licence nothing uses, and the next reader would take it
       for a rule. */
    const unused = exceptions
      .filter((e) => e.found.length < e.pinned)
      .map((e) => `${e.rel} sources.${e.key} -- ${e.found.length} (pinned ${e.pinned})`)
    expect(unused, 'the assignment is gone -- delete the row, and the exception in CONTRIBUTING section 11 with it')
      .toEqual([])
  })

  it('keeps the test seam to the tests', () => {
    const wrong = elsewhere(set).map((c) => `${c.at} ${c.how}`)
    expect(wrong, `setSources is the test seam: a page module installs in ${INSTALLER} instead`)
      .toEqual([])
  })

  it('assigns the seam in bulk only in the module that declares it', () => {
    const wrong = elsewhere(bulk).map((c) => c.at)
    expect(wrong, `Object.assign(sources, ...) and defineProperty belong to ${SEAM}; install one domain at a time`)
      .toEqual([])
  })

  it('lets no other module hand the seam out under a second name', () => {
    const wrong = elsewhere(handed).map((c) => `${c.at} re-exports ${c.how}`)
    expect(wrong, `a re-export is a name this check cannot follow: read the seam through ds(), and install in ${INSTALLER}`)
      .toEqual([])
  })
})
