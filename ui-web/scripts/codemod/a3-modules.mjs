// A3 codemod: turn the 49 concatenated legacy parts into ES modules.
//
// Shape of every rewritten file:
//
//   <file header comment>
//   import { ... } from './xxx.js'          one line per owner file
//   <every top-level DECLARATION, in original order>
//   export function install() { <every other top-level statement, in order> }
//   export { a, b, c }                      every name this file declares
//
// Declarations whose initialiser is impure or order-dependent are hollowed to a
// bare `let name;` and assigned inside install() at their original position in
// the statement order, so install() reproduces today's evaluation exactly while
// the module body itself is inert -- which is what makes the 14-file demo cycle
// and the 13-file live cycle harmless.
//
// Exports are one trailing `export { ... }` rather than `export` prefixes: the
// Python test outside this directory and scripts/count-shared-globals.mjs both
// find declarations by `^function name(` / `^const name` at line start.
//
//   node scripts/codemod/a3-modules.mjs          # report only
//   node scripts/codemod/a3-modules.mjs --write  # rewrite in place
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const UIWEB = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SRC = join(UIWEB, 'src', 'legacy')
const py = readFileSync(join(UIWEB, 'build.py'), 'utf8')
const manifest = (n) => [...py.match(new RegExp(`_${n}_PARTS = \\[([\\s\\S]*?)\\]`))[1].matchAll(/"([^"]+)"/g)].map((x) => x[1])

export const FILES = [
  ...manifest('SEAM').map((p) => `seam/${p}`),
  ...manifest('DEMO').map((p) => `demo/${p}`),
  ...manifest('LIVE').map((p) => `live/${p}`),
]

// The modules outside both cycles. A top-level initialiser may reference these
// and nothing else from another file: everything else is a cycle-mate or sits
// behind one, and reading it at module-evaluation time is a TDZ bet.
export const LEAVES = new Set(['seam/000-datasource.js', 'demo/010-kernel.js', 'demo/020-prose.js', 'demo/030-fixtures.js'])

const IIFE_OPEN = '(() => {\n'
const LIVE_GUARD = "if (!/^http/.test(location.protocol) || /(^|[?&])stub=1/.test(location.search)) return;\n"
const LIVE_MODE = `export function liveMode() {
  /* The stub gate, which used to be this layer's opening \`return\`: opened from
     disk (file://) or with ?stub=1 the demo keeps its mock data untouched, so
     legacy/index.js installs the demo layer and stops. */
  return /^http/.test(location.protocol) && !/(^|[?&])stub=1/.test(location.search);
}
`

/* The wrapper the live parts share, removed so each part is a module of its
   own. The text between them is untouched. */
function unwrap(rel, text) {
  if (rel === 'live/010-boot-guard.js') {
    if (!text.includes(IIFE_OPEN)) throw new Error('live/010: IIFE opener not found')
    if (!text.includes(LIVE_GUARD)) throw new Error('live/010: stub guard not found')
    return text.replace(IIFE_OPEN, '').replace(LIVE_GUARD, LIVE_MODE)
  }
  if (rel === 'live/240-external-agents.js') {
    if (!/\n\}\)\(\);\n?$/.test(text)) throw new Error('live/240: IIFE closer not found')
    return text.replace(/\n\}\)\(\);\n?$/, '\n')
  }
  return text
}

const parse = (rel, text) => ts.createSourceFile(rel, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)

/* Why install()'s body is not indented. Six sandbox harnesses slice a part of
   this layer out by text and evaluate it, and their anchors are the exact
   columns the concatenated script had. Re-indenting would also rewrite every
   multi-line template literal a moved statement carries. The statements move
   verbatim; they get their indent when the harnesses stop reading text (the
   plan's A3b). */
const INSTALL_NOTE = `/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. The body keeps the
   statements' original column: the sandbox harnesses slice them out by text. */
`

function bindingNames(nm, out) {
  if (!nm) return
  if (ts.isIdentifier(nm)) out.add(nm.text)
  else if (nm.elements) for (const e of nm.elements) { if (!ts.isOmittedExpression(e)) bindingNames(e.name, out) }
}
function declaredIn(scope) {
  const out = new Set()
  if (ts.isFunctionLike(scope)) {
    for (const p of scope.parameters || []) bindingNames(p.name, out)
    if (scope.name && ts.isIdentifier(scope.name) && (ts.isFunctionExpression(scope) || ts.isFunctionDeclaration(scope))) out.add(scope.name.text)
  }
  if (ts.isCatchClause(scope) && scope.variableDeclaration) bindingNames(scope.variableDeclaration.name, out)
  const stmts = ts.isBlock(scope) || ts.isSourceFile(scope) ? scope.statements
    : ts.isFunctionLike(scope) && scope.body && ts.isBlock(scope.body) ? scope.body.statements
      : ts.isCaseClause(scope) || ts.isDefaultClause(scope) ? scope.statements : null
  if (stmts) for (const st of stmts) {
    if (ts.isVariableStatement(st)) for (const d of st.declarationList.declarations) bindingNames(d.name, out)
    else if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) && st.name) out.add(st.name.text)
  }
  const i = scope.initializer
  if ((ts.isForStatement(scope) || ts.isForOfStatement(scope) || ts.isForInStatement(scope)) && i && ts.isVariableDeclarationList(i)) {
    for (const d of i.declarations) bindingNames(d.name, out)
  }
  return out
}
const isScope = (n) =>
  ts.isFunctionLike(n) || ts.isBlock(n) || ts.isSourceFile(n) || ts.isCatchClause(n)
  || ts.isForStatement(n) || ts.isForOfStatement(n) || ts.isForInStatement(n)
  || ts.isCaseClause(n) || ts.isDefaultClause(n)

/* Names for the trailing `export { }` block: what this file declares and has
   not already exported with a prefix (install, and liveMode in live/010). */
const topNames = (sf) => {
  const out = []
  for (const st of sf.statements) {
    if (st.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword)) continue
    if (ts.isVariableStatement(st)) for (const d of st.declarationList.declarations) { const s = new Set(); bindingNames(d.name, s); out.push(...s) }
    else if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) && st.name) out.push(st.name.text)
  }
  return out
}

/* Every identifier that is neither a property name nor declared in an
   enclosing scope of its own -- what the module has to import or find as a
   page global. `load` marks the ones read at module-evaluation time. */
function freeRefs(sf, own) {
  const cache = new Map()
  const scopeNames = (n) => { if (!cache.has(n)) cache.set(n, declaredIn(n)); return cache.get(n) }
  const shadowed = (id) => {
    for (let p = id.parent; p; p = p.parent) {
      if (ts.isSourceFile(p)) return false
      if (isScope(p) && scopeNames(p).has(id.text)) return true
    }
    return false
  }
  const inFn = (id) => { for (let p = id.parent; p && !ts.isSourceFile(p); p = p.parent) if (ts.isFunctionLike(p)) return true; return false }
  const declNodes = new Set()
  for (const st of sf.statements) {
    if (ts.isVariableStatement(st)) for (const d of st.declarationList.declarations) {
      const mark = (nm) => { if (ts.isIdentifier(nm)) declNodes.add(nm); else if (nm.elements) for (const e of nm.elements) e.name && mark(e.name) }
      mark(d.name)
    } else if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) && st.name) declNodes.add(st.name)
  }
  const out = []
  const walk = (n) => {
    if (ts.isIdentifier(n)) {
      const p = n.parent
      const isProp =
        (ts.isPropertyAccessExpression(p) && p.name === n) || (ts.isPropertyAssignment(p) && p.name === n)
        || (ts.isMethodDeclaration(p) && p.name === n) || (ts.isBindingElement(p) && p.propertyName === n)
        || (ts.isQualifiedName(p) && p.right === n) || (ts.isGetAccessorDeclaration(p) && p.name === n)
        || (ts.isSetAccessorDeclaration(p) && p.name === n) || (ts.isLabeledStatement(p) && p.label === n)
        || (ts.isBreakOrContinueStatement(p) && p.label === n)
      if (!isProp && !declNodes.has(n) && !shadowed(n) && !own.has(n.text)) {
        const ASSIGN = new Set([
          ts.SyntaxKind.EqualsToken, ts.SyntaxKind.PlusEqualsToken, ts.SyntaxKind.MinusEqualsToken,
          ts.SyntaxKind.AsteriskEqualsToken, ts.SyntaxKind.SlashEqualsToken,
          ts.SyntaxKind.QuestionQuestionEqualsToken, ts.SyntaxKind.BarBarEqualsToken,
          ts.SyntaxKind.AmpersandAmpersandEqualsToken,
        ])
        const UPD = new Set([ts.SyntaxKind.PlusPlusToken, ts.SyntaxKind.MinusMinusToken])
        const written = (ts.isBinaryExpression(p) && p.left === n && ASSIGN.has(p.operatorToken.kind))
          || ((ts.isPrefixUnaryExpression(p) || ts.isPostfixUnaryExpression(p)) && p.operand === n && UPD.has(p.operator))
        out.push({ name: n.text, node: n, load: !inFn(n), written })
      }
    }
    ts.forEachChild(n, walk)
  }
  walk(sf)
  return out
}

/* Pure enough to evaluate at module-instantiation time: it can neither run
   another module's code nor read the DOM, so its position in the evaluation
   order cannot matter. Arrow and function expressions count -- their BODIES run
   later, and rule 3 checks what a body may reference separately. */
const PURE_CALLEES = new Set(['Map', 'Set', 'WeakMap', 'WeakSet', 'Date'])
function pure(node, own, ownFile) {
  if (!node) return true
  if (ts.isLiteralExpression(node) || ts.isNoSubstitutionTemplateLiteral(node)) return true
  switch (node.kind) {
    case ts.SyntaxKind.TrueKeyword:
    case ts.SyntaxKind.FalseKeyword:
    case ts.SyntaxKind.NullKeyword:
      return true
    default:
      break
  }
  if (ts.isArrowFunction(node) || ts.isFunctionExpression(node) || ts.isClassExpression(node)) return true
  if (ts.isArrayLiteralExpression(node)) return node.elements.every((e) => pure(e, own, ownFile))
  if (ts.isObjectLiteralExpression(node)) {
    return node.properties.every((p) => {
      if (ts.isPropertyAssignment(p)) return pure(p.initializer, own, ownFile)
      if (ts.isMethodDeclaration(p)) return true
      if (ts.isShorthandPropertyAssignment(p)) return own.has(p.name.text)
      return false
    })
  }
  if (ts.isNewExpression(node)) {
    return ts.isIdentifier(node.expression) && PURE_CALLEES.has(node.expression.text)
      && (node.arguments ?? []).every((a) => pure(a, own, ownFile))
  }
  if (ts.isPrefixUnaryExpression(node)) return pure(node.operand, own, ownFile)
  if (ts.isParenthesizedExpression(node)) return pure(node.expression, own, ownFile)
  if (ts.isBinaryExpression(node)) return pure(node.left, own, ownFile) && pure(node.right, own, ownFile)
  if (ts.isConditionalExpression(node)) {
    return pure(node.condition, own, ownFile) && pure(node.whenTrue, own, ownFile) && pure(node.whenFalse, own, ownFile)
  }
  if (ts.isTemplateExpression(node)) return node.templateSpans.every((s) => pure(s.expression, own, ownFile))
  if (ts.isIdentifier(node)) return own.has(node.text)
  if (ts.isRegularExpressionLiteral(node)) return true
  return false
}

const multiline = (node, sf) => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line !== sf.getLineAndCharacterOfPosition(node.end).line
/* Indenting a statement that carries a multi-line template literal would put
   two spaces inside the string. Such a statement is emitted at column 0. */
function carriesMultilineTemplate(node, sf) {
  let found = false
  const walk = (n) => {
    if (found) return
    if ((ts.isNoSubstitutionTemplateLiteral(n) || ts.isTemplateExpression(n)) && multiline(n, sf)) { found = true; return }
    ts.forEachChild(n, walk)
  }
  walk(node)
  return found
}

// ---------------------------------------------------------------- ownership

const texts = new Map()
const trees = new Map()
const owns = new Map()
const owner = new Map()
for (const rel of FILES) {
  const raw = readFileSync(join(SRC, rel), 'utf8')
  // It rewrites the concatenated parts, so it cannot be run on its own output.
  if (raw.includes('export function install()')) {
    throw new Error(`${rel} is already a module -- this codemod runs once, against the concatenated parts`)
  }
  const text = unwrap(rel, raw)
  texts.set(rel, text)
  const sf = parse(rel, text)
  trees.set(rel, sf)
  const names = topNames(sf)
  owns.set(rel, new Set(names))
  for (const n of names) {
    if (owner.has(n)) throw new Error(`${n} declared in both ${owner.get(n)} and ${rel}`)
    owner.set(n, rel)
  }
}

// Names main.tsx hangs on window, plus the two bags. They stay globals in this
// step: the legacy layers read them only from inside install() or from function
// bodies, by which time main.tsx has published them.
export const PUBLISHED = new Set(
  [...readFileSync(join(UIWEB, 'src', 'main.tsx'), 'utf8').matchAll(/^window\.([A-Za-z_$][\w$]*)/gm)].map((m) => m[1]),
)
PUBLISHED.add('RavenIslands')
PUBLISHED.add('RavenShell')
// The legacy layers publish a few of their own onto window; a bare read of one
// resolves through the global object the same way.
for (const rel of FILES) {
  for (const m of readFileSync(join(SRC, rel), 'utf8').matchAll(/^window\.([A-Za-z_$][\w$]*)/gm)) PUBLISHED.add(m[1])
}

export const BROWSER = new Set([
  'window', 'document', 'console', 'Math', 'JSON', 'Object', 'Array', 'String', 'Number', 'Boolean', 'Date',
  'Promise', 'Set', 'Map', 'WeakMap', 'WeakSet', 'Error', 'TypeError', 'RangeError', 'Symbol', 'RegExp', 'Intl',
  'URL', 'URLSearchParams', 'navigator', 'location', 'history', 'localStorage', 'sessionStorage', 'fetch',
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'queueMicrotask', 'requestAnimationFrame',
  'cancelAnimationFrame', 'MutationObserver', 'ResizeObserver', 'IntersectionObserver', 'Event', 'CustomEvent',
  'MouseEvent', 'KeyboardEvent', 'Blob', 'File', 'FileReader', 'FormData', 'Headers', 'Request', 'Response',
  'WebSocket', 'TextEncoder', 'TextDecoder', 'atob', 'btoa', 'matchMedia', 'getComputedStyle', 'alert', 'confirm',
  'prompt', 'undefined', 'NaN', 'Infinity', 'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'encodeURIComponent',
  'decodeURIComponent', 'structuredClone', 'AbortController', 'Notification', 'crypto', 'performance',
  'Uint8Array', 'ArrayBuffer', 'DataView', 'Image', 'HTMLElement', 'Node', 'NodeList', 'Element',
  'DocumentFragment', 'globalThis', 'self', 'addEventListener', 'removeEventListener', 'scrollTo', 'DOMParser',
  'XMLHttpRequest', 'CSS', 'requestIdleCallback', 'reportError', 'WeakRef', 'Proxy', 'Reflect', 'BigInt',
  'arguments', 'SVGElement', 'customElements', 'getSelection', 'visualViewport', 'screen', 'devicePixelRatio',
  'innerWidth', 'innerHeight', 'scrollX', 'scrollY', 'parent', 'postMessage', 'IntersectionObserverEntry',
  'CSSStyleSheet', 'ClipboardItem', 'AbortSignal', 'ImageData', 'Text', 'Range', 'Selection', 'URLPattern',
  'NodeFilter', 'XPathResult', 'MediaQueryList',
])

// ------------------------------------------------------------------ rewrite

const importPath = (from, to) => {
  const [fromDir] = from.split('/')
  const [toDir, toFile] = to.split('/')
  return fromDir === toDir ? `./${toFile}` : `../${toDir}/${toFile}`
}

const report = []
const unknown = []
const implicitWrites = []
const outputs = new Map()

for (const rel of FILES) {
  const text = texts.get(rel)
  const sf = trees.get(rel)
  const own = owns.get(rel)

  const refs = freeRefs(sf, own)
  const byOwner = new Map()
  for (const r of refs) {
    const home = owner.get(r.name)
    if (home) {
      if (!byOwner.has(home)) byOwner.set(home, new Set())
      byOwner.get(home).add(r.name)
    } else if (!PUBLISHED.has(r.name) && !BROWSER.has(r.name)) {
      unknown.push(`${rel}: ${r.name}`)
    }
    // An ES module is strict, so assigning a name it does not declare throws
    // instead of landing on window. Every one has to be spelled window.X.
    if (!home && r.written) {
      implicitWrites.push(`${rel}:${sf.getLineAndCharacterOfPosition(r.node.getStart(sf)).line + 1}  ${r.name} =`)
    }
  }

  const stay = []
  const move = []
  let moved = 0
  let hollowed = 0
  for (const [i, st] of sf.statements.entries()) {
    // The first statement's leading trivia is the file's own header and stays
    // at the top; every later statement carries its comment with it.
    const head = i === 0 ? st.getStart(sf) : st.getFullStart()
    const body = text.slice(head, st.end)
    if (ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) { stay.push(body); continue }
    if (ts.isVariableStatement(st)) {
      const impure = st.declarationList.declarations.filter(
        (d) => !pure(d.initializer, own, rel) || (d.initializer && initTouchesNonLeaf(d.initializer, own, rel)),
      )
      if (impure.length === 0) { stay.push(body); continue }
      if (impure.length !== st.declarationList.declarations.length) {
        throw new Error(`${rel}: mixed pure/impure declarator list needs a hand split: ${body.slice(0, 80)}`)
      }
      const names = []
      for (const d of st.declarationList.declarations) { const s = new Set(); bindingNames(d.name, s); names.push(...s) }
      // The comment explains the name, so it stays with the hollowed `let`.
      const trivia = text.slice(head, st.getStart(sf))
      stay.push(`${trivia}let ${names.join(', ')};`)
      for (const d of st.declarationList.declarations) {
        if (!d.initializer) continue
        const lhs = text.slice(d.name.getStart(sf), d.name.end)
        const rhs = text.slice(d.initializer.getStart(sf), d.initializer.end)
        move.push({ text: ts.isIdentifier(d.name) ? `${lhs} = ${rhs};` : `(${lhs} = ${rhs});`, raw: d.initializer })
      }
      hollowed += names.length
      continue
    }
    move.push({ text: text.slice(head, st.end), raw: st })
    moved++
  }

  const imports = [...byOwner]
    .sort((a, b) => FILES.indexOf(a[0]) - FILES.indexOf(b[0]))
    .map(([home, names]) => `import { ${[...names].sort().join(', ')} } from '${importPath(rel, home)}'`)

  // Each slice already carries the blank lines that preceded it, so the parts
  // are concatenated rather than joined: what survives keeps its own spacing.
  // Moved statements keep their original column too -- see INSTALL_NOTE.
  const installBody = move.map((m) => (m.text.startsWith('\n') ? m.text : `\n${m.text}`)).join('').replace(/^\n+/, '')

  const header = text.slice(0, sf.statements.length ? sf.statements[0].getStart(sf) : text.length).replace(/\s+$/, '')
  const exportNames = topNames(sf)
  const parts = []
  if (header) parts.push(header, '\n')
  if (imports.length) parts.push('\n', imports.join('\n'), '\n')
  parts.push('\n', stay.join('').replace(/^\n+/, ''))
  parts.push('\n\n', `${INSTALL_NOTE}export function install() {\n${installBody}\n}`)
  if (exportNames.length) parts.push('\n\n', `export { ${exportNames.join(', ')} }`)
  outputs.set(rel, parts.join('').replace(/\n{3,}/g, '\n\n').replace(/^\n+/, '') + '\n')
  report.push({ rel, imports: imports.length, bindings: [...byOwner.values()].reduce((a, s) => a + s.size, 0), moved, hollowed, decls: exportNames.length })
}

/* Rule 3: a top-level initialiser may reference only its own module or the
   cycle-free leaves. Checked on the ORIGINAL declaration, which is why an
   offender is hollowed rather than reported. */
function initTouchesNonLeaf(init, own, rel) {
  let bad = false
  const walk = (n) => {
    if (bad) return
    if (ts.isFunctionLike(n)) return
    if (ts.isIdentifier(n)) {
      const p = n.parent
      const isProp = (ts.isPropertyAccessExpression(p) && p.name === n) || (ts.isPropertyAssignment(p) && p.name === n)
        || (ts.isBindingElement(p) && p.propertyName === n) || (ts.isMethodDeclaration(p) && p.name === n)
      if (!isProp && !own.has(n.text)) {
        const home = owner.get(n.text)
        if (home && home !== rel && !LEAVES.has(home)) bad = true
      }
    }
    ts.forEachChild(n, walk)
  }
  walk(init)
  return bad
}

/* Rule 8. The catalogue used to be spliced in by build.py at a marker inside
   this part; the marker cannot survive Vite, so the part imports the file. */
const I18N_MARKER = 'const I18N = /*__I18N__*/{ "slash": {}, "ui": {} };'
const NOJS = "(() => { const n = document.getElementById('noJs'); if (n) n.remove(); })();"
const NOJS_COMMENT = '// If the script runs at all, this marker goes; if you still see it, it did not.\n'
function kernelCatalogue(out) {
  if (!out.includes(I18N_MARKER)) throw new Error('demo/010-kernel.js: the I18N marker is gone')
  let next = out
    .replace('The catalogue is spliced in from i18n/messages.json at build time', 'The catalogue comes from i18n/messages.json at build time')
    .replace(I18N_MARKER, 'const I18N = { slash: catalog.slash, ui: catalog.ui };')
  const anchor = 'const $ = (s) => document.querySelector(s);'
  next = next.replace(anchor, `import catalog from '../../../../i18n/messages.json'\n\n${anchor}`)
  // The marker comment belongs with the statement, which is now in install().
  return next.replace(NOJS_COMMENT, '').replace(NOJS, NOJS_COMMENT + NOJS)
}
outputs.set('demo/010-kernel.js', kernelCatalogue(outputs.get('demo/010-kernel.js')))

const write = process.argv.includes('--write')
console.log('file'.padEnd(34), 'imp', 'bind', 'moved', 'hollow', 'decls')
for (const r of report) {
  console.log(r.rel.padEnd(34), String(r.imports).padStart(3), String(r.bindings).padStart(4), String(r.moved).padStart(5), String(r.hollowed).padStart(6), String(r.decls).padStart(5))
}
const sum = (k) => report.reduce((a, r) => a + r[k], 0)
console.log(`\ntotals: ${sum('imports')} import statements, ${sum('bindings')} bindings, ${sum('moved')} statements into install(), ${sum('hollowed')} hollowed declarators, ${sum('decls')} exported names`)
if (implicitWrites.length) {
  console.log(`\nIMPLICIT GLOBAL WRITES (these throw in a module): ${implicitWrites.length}`)
  for (const w of implicitWrites) console.log('  ' + w)
}
if (unknown.length) {
  console.log(`\nfree identifiers that are neither a legacy name, a window publication, nor a browser global: ${unknown.length}`)
  for (const u of [...new Set(unknown)]) console.log('  ' + u)
}
if (write) {
  for (const [rel, out] of outputs) writeFileSync(join(SRC, rel), out)
  console.log(`\nwrote ${outputs.size} files`)
} else {
  console.log('\n(dry run; pass --write)')
}
