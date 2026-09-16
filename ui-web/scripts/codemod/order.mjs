import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
const UIWEB = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SRC = join(UIWEB, 'src')
const py = readFileSync(join(UIWEB, 'build.py'), 'utf8')
const manifest = (n) => [...py.match(new RegExp(`_${n}_PARTS = \\[([\\s\\S]*?)\\]`))[1].matchAll(/"([^"]+)"/g)].map((x) => x[1])
function concat(dir, parts) {
  let text = ''; const spans = []
  for (const p of parts) { const t = readFileSync(join(SRC, dir, p), 'utf8'); spans.push({ file: `${dir}/${p}`, start: text.length, end: text.length + t.length }); text += t }
  if (text.endsWith('\n')) { text = text.slice(0, -1); spans[spans.length - 1].end -= 1 }
  return { text, spans }
}
const seam = concat('seam', manifest('SEAM')), demo = concat('demo', manifest('DEMO')), live = concat('live', manifest('LIVE'))
const off = seam.text.length + 1
const layers = {
  demo: { text: seam.text + '\n' + demo.text, spans: [...seam.spans, ...demo.spans.map((s) => ({ file: s.file, start: s.start + off, end: s.end + off }))] },
  live: { text: live.text, spans: live.spans },
}
function bindingNames(nm, out) { if (!nm) return; if (ts.isIdentifier(nm)) out.add(nm.text); else if (nm.elements) for (const e of nm.elements) { if (ts.isOmittedExpression(e)) continue; bindingNames(e.name, out) } }
function declaredIn(scope) { const out = new Set()
  if (ts.isFunctionLike(scope)) { for (const p of scope.parameters || []) bindingNames(p.name, out); if (scope.name && ts.isIdentifier(scope.name) && (ts.isFunctionExpression(scope) || ts.isFunctionDeclaration(scope))) out.add(scope.name.text) }
  if (ts.isCatchClause(scope) && scope.variableDeclaration) bindingNames(scope.variableDeclaration.name, out)
  const stmts = ts.isBlock(scope) || ts.isSourceFile(scope) ? scope.statements : (ts.isFunctionLike(scope) && scope.body && ts.isBlock(scope.body)) ? scope.body.statements : (ts.isCaseClause(scope) || ts.isDefaultClause(scope)) ? scope.statements : null
  if (stmts) for (const st of stmts) { if (ts.isVariableStatement(st)) for (const d of st.declarationList.declarations) bindingNames(d.name, out); else if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) && st.name) out.add(st.name.text) }
  const i = scope.initializer
  if ((ts.isForStatement(scope) || ts.isForOfStatement(scope) || ts.isForInStatement(scope)) && i && ts.isVariableDeclarationList(i)) for (const d of i.declarations) bindingNames(d.name, out)
  return out }
const isScope = (n) => ts.isFunctionLike(n) || ts.isBlock(n) || ts.isSourceFile(n) || ts.isCatchClause(n) || ts.isForStatement(n) || ts.isForOfStatement(n) || ts.isForInStatement(n) || ts.isCaseClause(n) || ts.isDefaultClause(n)
function analyse(label) { const { text, spans } = layers[label]
  const sf = ts.createSourceFile(label, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)
  const fileOf = (n) => { const pos = n.getStart(sf); for (const s of spans) if (pos >= s.start && pos < s.end) return s.file; return '?' }
  const lineInFile = (n) => { const pos = n.getStart(sf); for (const s of spans) if (pos >= s.start && pos < s.end) { let c = 1; for (let i = s.start; i < pos; i++) if (text[i] === '\n') c++; return c } return 0 }
  let topNode = sf
  // See deps5.mjs: the live IIFE's arrow BODY BLOCK is the top scope.
  if (label === 'live') { let a = null; const f = (n) => { if (!a && ts.isArrowFunction(n)) a = n; else ts.forEachChild(n, f) }; f(sf.statements[0]); topNode = a.body }
  const topStatements = label === 'live' ? topNode.statements : sf.statements
  const decls = new Map(); const declNodes = new Set()
  for (const st of topStatements) { if (ts.isVariableStatement(st)) for (const d of st.declarationList.declarations) {
      const s = new Set(); bindingNames(d.name, s)
      const mark = (nm) => { if (ts.isIdentifier(nm)) declNodes.add(nm); else if (nm.elements) for (const e of nm.elements) e.name && mark(e.name) }
      mark(d.name); for (const n of s) decls.set(n, fileOf(d))
    } else if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) && st.name) { decls.set(st.name.text, fileOf(st.name)); declNodes.add(st.name) } }
  const cache = new Map(); const scopeNames = (n) => { if (!cache.has(n)) cache.set(n, declaredIn(n)); return cache.get(n) }
  const local = (id) => { for (let p = id.parent; p; p = p.parent) { if (p === topNode || (label === 'demo' && ts.isSourceFile(p))) return false; if (isScope(p) && scopeNames(p).has(id.text)) return true } return false }
  const inFn = (id) => { for (let p = id.parent; p && p !== topNode; p = p.parent) if (ts.isFunctionLike(p)) return true; return false }
  const refs = []
  const walk = (n) => { if (ts.isIdentifier(n)) { const p = n.parent
      const isProp = (ts.isPropertyAccessExpression(p) && p.name === n) || (ts.isPropertyAssignment(p) && p.name === n) || (ts.isMethodDeclaration(p) && p.name === n) || (ts.isBindingElement(p) && p.propertyName === n) || (ts.isQualifiedName(p) && p.right === n) || (ts.isGetAccessorDeclaration(p) && p.name === n) || (ts.isSetAccessorDeclaration(p) && p.name === n) || (ts.isLabeledStatement(p) && p.label === n) || ts.isShorthandPropertyAssignment(p) && false
      if (!isProp && !declNodes.has(n) && !local(n)) refs.push({ name: n.text, file: fileOf(n), line: lineInFile(n), load: !inFn(n) }) }
    ts.forEachChild(n, walk) }
  walk(sf); return { decls, refs } }
const D = analyse('demo'), L = analyse('live')
const all = new Map([...D.decls, ...[...L.decls].filter(([k]) => !D.decls.has(k))])
const order = [...manifest('SEAM').map((p)=>'seam/'+p), ...manifest('DEMO').map((p)=>'demo/'+p), ...manifest('LIVE').map((p)=>'live/'+p)]
// ---------- build import graph: file -> set of owner files it needs ----------
const edges = new Map(order.map((f)=>[f, new Set()]))
for (const A of [D, L]) for (const r of A.refs) { const to = all.get(r.name); if (!to || to === r.file) continue; edges.get(r.file).add(to) }
// ---------- simulate ES module evaluation: root index.js imports order[] ----------
const state = new Map(); const evalOrder = []
function evaluate(f) {
  if (state.get(f)) return
  state.set(f, 'evaluating')
  for (const dep of [...edges.get(f)].sort((a,b)=>order.indexOf(a)-order.indexOf(b))) if (!state.get(dep)) evaluate(dep)
  state.set(f, 'done'); evalOrder.push(f)
}
for (const f of order) evaluate(f)
console.log('=== simulated ES module evaluation order (deps in manifest order inside each file) ===')
evalOrder.forEach((f,i)=>console.log(String(i+1).padStart(3)+'  '+f+(order.indexOf(f)===i?'':'   [manifest pos '+(order.indexOf(f)+1)+']')))
let moved = 0
for (let i=0;i<order.length;i++) if (evalOrder[i]!==order[i]) moved++
console.log(`\nfiles whose evaluation slot changes: ${moved} of ${order.length}`)
// pairs that invert
const pos = new Map(evalOrder.map((f,i)=>[f,i]))
const inversions = []
for (let i=0;i<order.length;i++) for (let j=i+1;j<order.length;j++) if (pos.get(order[i])>pos.get(order[j])) inversions.push([order[i],order[j]])
console.log(`ordered pairs that invert (A before B today, B before A under ESM): ${inversions.length}`)
console.log(inversions.slice(0,400).map(([a,b])=>`  ${a}  <->  ${b}`).join('\n'))
console.log('\n=== edges out of demo/130-settings.js ===')
console.log([...edges.get('demo/130-settings.js')].join(', '))
console.log('=== who imports demo/050-rail.js ===')
for (const [f,s] of edges) if (s.has('demo/050-rail.js')) console.log('  '+f)
console.log('=== edges out of demo/152-skills.js ===')
console.log([...edges.get('demo/152-skills.js')].join(', '))
console.log('=== edges out of demo/120-capabilities.js ===')
console.log([...edges.get('demo/120-capabilities.js')].join(', '))
