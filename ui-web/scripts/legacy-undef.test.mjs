/* no-undef for the legacy module graph, without eslint.
 *
 * The concat script resolved every name through one shared script scope, so a
 * missing import could not exist. Modules do not have that, and the two ways
 * the mistake shows up do not look alike:
 *
 *   - a missing import on a READ is silent until the line runs, and then it is
 *     a ReferenceError in whichever of the two load modes reaches it;
 *   - a missing import on a WRITE is worse: `foo = 1` used to land on window
 *     and keep working, and a module is strict, so it throws. One such write
 *     existed (demo/150-chrome.js's `sTab = 'model'`) and is now spelled
 *     window.sTab.
 *
 * So every free identifier in the layers has to be one of: declared in the
 * part, imported by it, a browser global, or a name the page hangs on window
 * (main.tsx publishes 28 plus RavenIslands; the legacy layers publish nine of
 * their own). A free WRITE is never allowed, whatever the name.
 *
 * eslint is not a dependency of this package and this does not add one: the
 * TypeScript compiler is already here, and a scope walk is what no-undef is.
 */

import { readFileSync } from 'node:fs'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const url = (p) => new URL(`../${p}`, import.meta.url)
const build = readFileSync(url('build.py'), 'utf8')
const manifest = (name) => {
  const block = build.match(new RegExp(`_${name}_PARTS = \\[([\\s\\S]*?)\\]`))
  if (!block) throw new Error(`_${name}_PARTS is absent from build.py`)
  return [...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1])
}
const FILES = [
  ...manifest('SEAM').map((p) => `seam/${p}`),
  ...manifest('DEMO').map((p) => `demo/${p}`),
  ...manifest('LIVE').map((p) => `live/${p}`),
]
const texts = new Map(FILES.map((rel) => [rel, readFileSync(url(`src/legacy/${rel}`), 'utf8')]))
const trees = new Map(
  [...texts].map(([rel, text]) => [rel, ts.createSourceFile(rel, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)]),
)

/* Read from the sources rather than listed here: a name published by main.tsx
   or by a legacy part is a page global whichever file hangs it, and a list
   kept by hand would go stale on the first republish. */
const PUBLISHED = new Set(
  [...readFileSync(url('src/main.tsx'), 'utf8').matchAll(/^window\.([A-Za-z_$][\w$]*)/gm)].map((m) => m[1]),
)
PUBLISHED.add('RavenIslands')
PUBLISHED.add('RavenShell')
for (const text of texts.values()) {
  for (const m of text.matchAll(/^\s*window\.([A-Za-z_$][\w$]*)\s*=/gm)) PUBLISHED.add(m[1])
}

const BROWSER = new Set([
  'window', 'document', 'console', 'Math', 'JSON', 'Object', 'Array', 'String', 'Number', 'Boolean', 'Date',
  'Promise', 'Set', 'Map', 'WeakMap', 'WeakSet', 'Error', 'TypeError', 'RangeError', 'Symbol', 'RegExp', 'Intl',
  'URL', 'URLSearchParams', 'navigator', 'location', 'history', 'localStorage', 'sessionStorage', 'fetch',
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'queueMicrotask', 'requestAnimationFrame',
  'cancelAnimationFrame', 'MutationObserver', 'ResizeObserver', 'IntersectionObserver', 'Event', 'CustomEvent',
  'MouseEvent', 'KeyboardEvent', 'Blob', 'File', 'FileReader', 'FormData', 'Headers', 'Request', 'Response',
  'WebSocket', 'TextEncoder', 'TextDecoder', 'atob', 'btoa', 'matchMedia', 'getComputedStyle', 'alert', 'confirm',
  'prompt', 'undefined', 'NaN', 'Infinity', 'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'encodeURIComponent',
  'decodeURIComponent', 'structuredClone', 'AbortController', 'AbortSignal', 'Notification', 'crypto',
  'performance', 'Uint8Array', 'ArrayBuffer', 'DataView', 'Image', 'HTMLElement', 'Node', 'NodeList', 'Element',
  'DocumentFragment', 'globalThis', 'self', 'addEventListener', 'removeEventListener', 'scrollTo', 'DOMParser',
  'XMLHttpRequest', 'CSS', 'requestIdleCallback', 'reportError', 'WeakRef', 'Proxy', 'Reflect', 'BigInt',
  'arguments', 'SVGElement', 'customElements', 'getSelection', 'visualViewport', 'screen', 'devicePixelRatio',
  'innerWidth', 'innerHeight', 'scrollX', 'scrollY', 'parent', 'postMessage', 'IntersectionObserverEntry',
  'CSSStyleSheet', 'ClipboardItem', 'ImageData', 'Text', 'Range', 'Selection', 'NodeFilter', 'XPathResult',
  'MediaQueryList',
])

const bind = (nm, out) => {
  if (!nm) return
  if (ts.isIdentifier(nm)) out.add(nm.text)
  else if (nm.elements) for (const e of nm.elements) { if (!ts.isOmittedExpression(e)) bind(e.name, out) }
}
function declaredIn(scope) {
  const out = new Set()
  if (ts.isFunctionLike(scope)) {
    for (const p of scope.parameters || []) bind(p.name, out)
    if (scope.name && ts.isIdentifier(scope.name) && (ts.isFunctionExpression(scope) || ts.isFunctionDeclaration(scope))) {
      out.add(scope.name.text)
    }
  }
  if (ts.isCatchClause(scope) && scope.variableDeclaration) bind(scope.variableDeclaration.name, out)
  const stmts = ts.isBlock(scope) || ts.isSourceFile(scope) ? scope.statements
    : ts.isFunctionLike(scope) && scope.body && ts.isBlock(scope.body) ? scope.body.statements
      : ts.isCaseClause(scope) || ts.isDefaultClause(scope) ? scope.statements : null
  if (stmts) for (const st of stmts) {
    if (ts.isVariableStatement(st)) for (const d of st.declarationList.declarations) bind(d.name, out)
    else if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) && st.name) out.add(st.name.text)
    else if (ts.isImportDeclaration(st)) {
      const clause = st.importClause
      if (clause?.name) out.add(clause.name.text)
      const nb = clause?.namedBindings
      if (nb && ts.isNamedImports(nb)) for (const s of nb.elements) out.add(s.name.text)
      if (nb && ts.isNamespaceImport(nb)) out.add(nb.name.text)
    }
  }
  const i = scope.initializer
  if ((ts.isForStatement(scope) || ts.isForOfStatement(scope) || ts.isForInStatement(scope)) && i && ts.isVariableDeclarationList(i)) {
    for (const d of i.declarations) bind(d.name, out)
  }
  return out
}
const isScope = (n) =>
  ts.isFunctionLike(n) || ts.isBlock(n) || ts.isSourceFile(n) || ts.isCatchClause(n)
  || ts.isForStatement(n) || ts.isForOfStatement(n) || ts.isForInStatement(n)
  || ts.isCaseClause(n) || ts.isDefaultClause(n)

const ASSIGN = new Set([
  ts.SyntaxKind.EqualsToken, ts.SyntaxKind.PlusEqualsToken, ts.SyntaxKind.MinusEqualsToken,
  ts.SyntaxKind.AsteriskEqualsToken, ts.SyntaxKind.SlashEqualsToken, ts.SyntaxKind.PercentEqualsToken,
  ts.SyntaxKind.QuestionQuestionEqualsToken, ts.SyntaxKind.BarBarEqualsToken,
  ts.SyntaxKind.AmpersandAmpersandEqualsToken,
])
const UPDATE = new Set([ts.SyntaxKind.PlusPlusToken, ts.SyntaxKind.MinusMinusToken])

function freeIdentifiers(rel) {
  const sf = trees.get(rel)
  const cache = new Map()
  const scopeNames = (n) => { if (!cache.has(n)) cache.set(n, declaredIn(n)); return cache.get(n) }
  const declNodes = new Set()
  const mark = (nm) => { if (!nm) return; if (ts.isIdentifier(nm)) declNodes.add(nm); else if (nm.elements) for (const e of nm.elements) e.name && mark(e.name) }
  const collectDecls = (n) => {
    if (ts.isVariableDeclaration(n)) mark(n.name)
    else if ((ts.isFunctionDeclaration(n) || ts.isClassDeclaration(n) || ts.isFunctionExpression(n) || ts.isClassExpression(n)) && n.name) declNodes.add(n.name)
    else if (ts.isParameter(n)) mark(n.name)
    else if ((ts.isImportSpecifier(n) || ts.isImportClause(n) || ts.isNamespaceImport(n)) && n.name) mark(n.name)
    else if (ts.isBindingElement(n)) mark(n.name)
    else if (ts.isCatchClause(n) && n.variableDeclaration) mark(n.variableDeclaration.name)
    ts.forEachChild(n, collectDecls)
  }
  collectDecls(sf)

  const out = []
  const walk = (n) => {
    if (ts.isIdentifier(n)) {
      const p = n.parent
      const isProp = (ts.isPropertyAccessExpression(p) && p.name === n)
        || (ts.isPropertyAssignment(p) && p.name === n)
        || (ts.isMethodDeclaration(p) && p.name === n)
        || (ts.isBindingElement(p) && p.propertyName === n)
        || (ts.isQualifiedName(p) && p.right === n)
        || (ts.isGetAccessorDeclaration(p) && p.name === n)
        || (ts.isSetAccessorDeclaration(p) && p.name === n)
        || (ts.isLabeledStatement(p) && p.label === n)
        || (ts.isBreakOrContinueStatement(p) && p.label === n)
        || ts.isImportSpecifier(p) || ts.isExportSpecifier(p)
      if (!isProp && !declNodes.has(n)) {
        let bound = false
        for (let s = n.parent; s; s = s.parent) {
          if (isScope(s) && scopeNames(s).has(n.text)) { bound = true; break }
        }
        if (!bound) {
          const written = (ts.isBinaryExpression(p) && p.left === n && ASSIGN.has(p.operatorToken.kind))
            || ((ts.isPrefixUnaryExpression(p) || ts.isPostfixUnaryExpression(p)) && p.operand === n && UPDATE.has(p.operator))
          out.push({ name: n.text, written, line: sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1 })
        }
      }
    }
    ts.forEachChild(n, walk)
  }
  walk(sf)
  return out
}

describe('every free identifier in a legacy module is imported, a browser global, or published on window', () => {
  for (const rel of FILES) {
    it(rel, () => {
      const undef = freeIdentifiers(rel)
        .filter((r) => !BROWSER.has(r.name) && !PUBLISHED.has(r.name))
        .map((r) => `${rel}:${r.line} ${r.name}`)
      expect([...new Set(undef)]).toEqual([])
    })
  }
})

describe('no legacy module writes a name it does not declare', () => {
  /* Separate from the rule above because being on window does not save it: a
     module is strict, so `sTab = x` throws even though window.sTab exists. */
  for (const rel of FILES) {
    it(rel, () => {
      const writes = freeIdentifiers(rel)
        .filter((r) => r.written)
        .map((r) => `${rel}:${r.line} ${r.name} =`)
      expect([...new Set(writes)]).toEqual([])
    })
  }
})
