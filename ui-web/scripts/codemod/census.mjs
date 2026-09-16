// Per-file census of every top-level-scope reference to a given set of names
// across the concatenated legacy layers. Reports read/write and whether the
// reference sits at module-evaluation time or inside a function.
//
// Usage: node scripts/codemod/census.mjs name1 name2 ...
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const UIWEB = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SRC = join(UIWEB, 'src', 'legacy')
const py = readFileSync(join(UIWEB, 'build.py'), 'utf8')
const manifest = (n) => [...py.match(new RegExp(`_${n}_PARTS = \\[([\\s\\S]*?)\\]`))[1].matchAll(/"([^"]+)"/g)].map((x) => x[1])
const LAYER_DIR = { seam: 'seam', demo: 'demo', live: 'live' }
function concat(dir, parts) {
  let text = ''
  const spans = []
  for (const p of parts) {
    const t = readFileSync(join(SRC, LAYER_DIR[dir], p), 'utf8')
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

const want = new Set(process.argv.slice(2))
const rows = []
for (const label of ['demo', 'live']) {
  const { text, spans } = layers[label]
  const sf = ts.createSourceFile(label, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)
  const fileOf = (n) => { const pos = n.getStart(sf); for (const s of spans) if (pos >= s.start && pos < s.end) return s.file; return '?' }
  const lineIn = (n) => { const pos = n.getStart(sf); for (const s of spans) if (pos >= s.start && pos < s.end) { let c = 1; for (let i = s.start; i < pos; i++) if (text[i] === '\n') c++; return c } return 0 }
  // For the live layer the whole concatenation is one IIFE, so its arrow body
  // block IS the top scope. Taking the arrow instead (as the original reviewer
  // scripts do) makes local() treat every top-level live name as shadowed and
  // silently drop all live-layer references.
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
  const inFn = (id) => { for (let p = id.parent; p && p !== topNode; p = p.parent) if (ts.isFunctionLike(p)) return true; return false }
  const walk = (n) => {
    if (ts.isIdentifier(n) && want.has(n.text)) {
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
      const shorthand = ts.isShorthandPropertyAssignment(p)
      if (!isProp && !local(n)) {
        const decl = declNodes.has(n)
        const ASSIGN = new Set([ts.SyntaxKind.EqualsToken, ts.SyntaxKind.PlusEqualsToken, ts.SyntaxKind.MinusEqualsToken, ts.SyntaxKind.QuestionQuestionEqualsToken, ts.SyntaxKind.BarBarEqualsToken, ts.SyntaxKind.AmpersandAmpersandEqualsToken])
        const write = ts.isBinaryExpression(p) && p.left === n && ASSIGN.has(p.operatorToken.kind)
        const UPD = new Set([ts.SyntaxKind.PlusPlusToken, ts.SyntaxKind.MinusMinusToken])
        const update = (ts.isPrefixUnaryExpression(p) || ts.isPostfixUnaryExpression(p)) && p.operand === n && UPD.has(p.operator)
        rows.push({
          file: fileOf(n),
          line: lineIn(n),
          name: n.text,
          kind: decl ? 'DECL' : write ? 'write' : update ? 'update' : shorthand ? 'SHORTHAND' : 'read',
          load: !inFn(n),
          src: text.slice(text.lastIndexOf('\n', n.getStart(sf)) + 1, text.indexOf('\n', n.getStart(sf))).trim().slice(0, 120),
        })
      }
    }
    ts.forEachChild(n, walk)
  }
  walk(sf)
}
const byName = new Map()
for (const r of rows) { if (!byName.has(r.name)) byName.set(r.name, []); byName.get(r.name).push(r) }
for (const [name, rs] of byName) {
  console.log(`\n=== ${name}: ${rs.length} references ===`)
  for (const r of rs) console.log(`  ${r.file}:${r.line} ${r.kind}${r.load ? ' [eval-time]' : ''}  ${r.src}`)
}
console.log(`\ntotal references: ${rows.length}`)
const missing = [...want].filter((w) => !byName.has(w))
if (missing.length) console.log(`names with no reference found: ${JSON.stringify(missing)}`)
