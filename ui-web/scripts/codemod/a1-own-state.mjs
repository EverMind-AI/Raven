// A1 codemod: rewrite every cross-file-assigned top-level `let` into a field
// on a container object owned by its declaring file.
//
// Scoping is resolved on the CONCATENATED layer text, because a single file's
// AST cannot tell a reference to another file's top-level name from a free
// global. Declaration sites are reported, not rewritten -- those are edited by
// hand, since the declarations are interleaved with names that stay put.
//
// Usage: node scripts/codemod/a1-own-state.mjs [--write]
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const UIWEB = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SRC = join(UIWEB, 'src')
const py = readFileSync(join(UIWEB, 'build.py'), 'utf8')
const manifest = (n) => [...py.match(new RegExp(`_${n}_PARTS = \\[([\\s\\S]*?)\\]`))[1].matchAll(/"([^"]+)"/g)].map((x) => x[1])

// old identifier -> replacement member expression
export const MAP = {
  HOST_PLATFORM: 'host.platform',
  use: 'runState.use',
  cKind: 'capFilter.kind',
  cQuery: 'capFilter.query',
  APP_VERSION: 'appInfo.version',
  turnOwner: 'park.turnOwner',
  lastAsk: 'park.lastAsk',
  pendingPerm: 'staged.perm',
  pendingModel: 'staged.model',
  pendingTier: 'staged.tier',
  providerConfiguredLive: 'setupState.providerConfigured',
}

function concat(dir, parts) {
  let text = ''
  const spans = []
  for (const p of parts) {
    const t = readFileSync(join(SRC, dir, p), 'utf8')
    spans.push({ file: `${dir}/${p}`, start: text.length, end: text.length + t.length })
    text += t
  }
  if (text.endsWith('\n')) {
    text = text.slice(0, -1)
    spans[spans.length - 1].end -= 1
  }
  return { text, spans }
}
const seam = concat('seam', manifest('SEAM'))
const demo = concat('demo', manifest('DEMO'))
const live = concat('live', manifest('LIVE'))
const off = seam.text.length + 1
const layers = {
  demo: { text: seam.text + '\n' + demo.text, spans: [...seam.spans, ...demo.spans.map((s) => ({ file: s.file, start: s.start + off, end: s.end + off }))] },
  live: { text: live.text, spans: live.spans },
}
function bn(nm, out) {
  if (!nm) return
  if (ts.isIdentifier(nm)) out.add(nm.text)
  else if (nm.elements) for (const e of nm.elements) { if (!ts.isOmittedExpression(e)) bn(e.name, out) }
}
function declaredIn(scope) {
  const out = new Set()
  if (ts.isFunctionLike(scope)) {
    for (const p of scope.parameters || []) bn(p.name, out)
    if (scope.name && ts.isIdentifier(scope.name) && (ts.isFunctionExpression(scope) || ts.isFunctionDeclaration(scope))) out.add(scope.name.text)
  }
  if (ts.isCatchClause(scope) && scope.variableDeclaration) bn(scope.variableDeclaration.name, out)
  const stmts = ts.isBlock(scope) || ts.isSourceFile(scope) ? scope.statements : ts.isFunctionLike(scope) && scope.body && ts.isBlock(scope.body) ? scope.body.statements : ts.isCaseClause(scope) || ts.isDefaultClause(scope) ? scope.statements : null
  if (stmts) for (const st of stmts) {
    if (ts.isVariableStatement(st)) for (const d of st.declarationList.declarations) bn(d.name, out)
    else if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) && st.name) out.add(st.name.text)
  }
  const i = scope.initializer
  if ((ts.isForStatement(scope) || ts.isForOfStatement(scope) || ts.isForInStatement(scope)) && i && ts.isVariableDeclarationList(i)) for (const d of i.declarations) bn(d.name, out)
  return out
}
const isScope = (n) =>
  ts.isFunctionLike(n) || ts.isBlock(n) || ts.isSourceFile(n) || ts.isCatchClause(n) || ts.isForStatement(n) || ts.isForOfStatement(n) || ts.isForInStatement(n) || ts.isCaseClause(n) || ts.isDefaultClause(n)

const edits = new Map()
const decls = []
const skipped = []
for (const label of ['demo', 'live']) {
  const { text, spans } = layers[label]
  const sf = ts.createSourceFile(label, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)
  const spanOf = (pos) => spans.find((s) => pos >= s.start && pos < s.end)
  let topNode = sf
  if (label === 'live') { let a = null; const f = (n) => { if (!a && ts.isArrowFunction(n)) a = n; else ts.forEachChild(n, f) }; f(sf.statements[0]); topNode = a.body }
  const topStatements = label === 'live' ? topNode.statements : sf.statements
  const declNodes = new Set()
  for (const st of topStatements) {
    if (ts.isVariableStatement(st)) for (const d of st.declarationList.declarations) {
      const mark = (nm) => { if (ts.isIdentifier(nm)) declNodes.add(nm); else if (nm.elements) for (const e of nm.elements) e.name && mark(e.name) }
      mark(d.name)
    } else if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) && st.name) declNodes.add(st.name)
  }
  const cache = new Map()
  const scopeNames = (n) => { if (!cache.has(n)) cache.set(n, declaredIn(n)); return cache.get(n) }
  const local = (id) => { for (let p = id.parent; p; p = p.parent) { if (p === topNode || (label === 'demo' && ts.isSourceFile(p))) return false; if (isScope(p) && scopeNames(p).has(id.text)) return true } return false }
  const walk = (n) => {
    if (ts.isIdentifier(n) && Object.hasOwn(MAP, n.text)) {
      const p = n.parent
      const isProp =
        (ts.isPropertyAccessExpression(p) && p.name === n) ||
        (ts.isPropertyAssignment(p) && p.name === n) ||
        (ts.isMethodDeclaration(p) && p.name === n) ||
        (ts.isBindingElement(p) && p.propertyName === n) ||
        (ts.isQualifiedName(p) && p.right === n) ||
        (ts.isGetAccessorDeclaration(p) && p.name === n) ||
        (ts.isSetAccessorDeclaration(p) && p.name === n) ||
        (ts.isLabeledStatement(p) && p.label === n)
      const pos = n.getStart(sf)
      const span = spanOf(pos)
      if (ts.isShorthandPropertyAssignment(p)) { skipped.push(`SHORTHAND ${span.file} ${n.text}`); return }
      if (isProp || local(n)) return
      if (declNodes.has(n)) { decls.push(`${span.file}  ${n.text}`); return }
      if (!edits.has(span.file)) edits.set(span.file, [])
      edits.get(span.file).push({ at: pos - span.start, len: n.text.length, from: n.text, to: MAP[n.text] })
    }
    ts.forEachChild(n, walk)
  }
  walk(sf)
}
const write = process.argv.includes('--write')
let total = 0
for (const [file, list] of [...edits].sort()) {
  const path = join(SRC, file)
  let text = readFileSync(path, 'utf8')
  list.sort((a, b) => b.at - a.at)
  for (const e of list) {
    if (text.slice(e.at, e.at + e.len) !== e.from) throw new Error(`${file}@${e.at}: expected ${e.from}, found ${JSON.stringify(text.slice(e.at, e.at + e.len))}`)
    text = text.slice(0, e.at) + e.to + text.slice(e.at + e.len)
  }
  total += list.length
  console.log(`${String(list.length).padStart(3)}  ${file}`)
  if (write) writeFileSync(path, text)
}
console.log(`\n${total} references rewritten across ${edits.size} files${write ? '' : ' (dry run; pass --write)'}`)
console.log(`declaration sites left for hand edits: ${decls.length}`)
for (const d of decls) console.log(`  ${d}`)
if (skipped.length) console.log(`skipped: ${JSON.stringify(skipped)}`)
