import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
const UIWEB = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SRC = join(UIWEB, 'src', 'legacy')
const py = readFileSync(join(UIWEB, 'build.py'), 'utf8')
const manifest = (n) => [...py.match(new RegExp(`_${n}_PARTS = \\[([\\s\\S]*?)\\]`))[1].matchAll(/"([^"]+)"/g)].map((x) => x[1])
// exact reproduction of build.py _concat: join texts, strip ONE trailing newline
function concat(dir, parts) {
  let text = ''; const spans = []
  for (const p of parts) { const t = readFileSync(join(SRC, dir, p), 'utf8'); spans.push({ file: `${dir}/${p}`, start: text.length, end: text.length + t.length }); text += t }
  if (text.endsWith('\n')) { text = text.slice(0, -1); spans[spans.length - 1].end -= 1 }
  return { text, spans }
}
const seam = concat('seam', manifest('SEAM')), demo = concat('demo', manifest('DEMO')), live = concat('live', manifest('LIVE'))
const demoText = seam.text + '\n' + demo.text
const off = seam.text.length + 1
const demoSpans = [...seam.spans, ...demo.spans.map((s) => ({ file: s.file, start: s.start + off, end: s.end + off }))]
const layers = { demo: { text: demoText, spans: demoSpans }, live: { text: live.text, spans: live.spans } }
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
  const lineOf = (n) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1
  const lineInFile = (n) => { const pos = n.getStart(sf); for (const s of spans) if (pos >= s.start && pos < s.end) { let c = 1; for (let i = s.start; i < pos; i++) if (text[i] === '\n') c++; return c } return 0 }
  let topNode = sf
  // The live layer is one IIFE, so its arrow BODY BLOCK is the top scope.
  // Stopping at the arrow leaves that block in local()'s walk, which then
  // reports every top-level live name as shadowed and drops all live-layer
  // references (the original reviewer copy had this bug: 411 -> 693 pairs).
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
      const isProp = (ts.isPropertyAccessExpression(p) && p.name === n) || (ts.isPropertyAssignment(p) && p.name === n) || (ts.isMethodDeclaration(p) && p.name === n) || (ts.isBindingElement(p) && p.propertyName === n) || (ts.isQualifiedName(p) && p.right === n) || (ts.isGetAccessorDeclaration(p) && p.name === n) || (ts.isSetAccessorDeclaration(p) && p.name === n) || (ts.isLabeledStatement(p) && p.label === n)
      if (!isProp && !declNodes.has(n) && !local(n)) refs.push({ name: n.text, file: fileOf(n), line: lineInFile(n), load: !inFn(n) }) }
    ts.forEachChild(n, walk) }
  walk(sf); return { decls, refs } }
const D = analyse('demo'), L = analyse('live')
const all = new Map([...D.decls, ...[...L.decls].filter(([k]) => !D.decls.has(k))])
const order = [...manifest('SEAM').map((p)=>'seam/'+p), ...manifest('DEMO').map((p)=>'demo/'+p), ...manifest('LIVE').map((p)=>'live/'+p)]
const idx = new Map(order.map((f, i) => [f, i]))
const perFile = new Map(); const edges = new Map(); let cross = 0; const loadRefs = []
const borrowed = new Set(); const backRefs = []
for (const [lbl, A] of [['demo', D], ['live', L]]) for (const r of A.refs) {
  const to = all.get(r.name); if (!to || to === r.file) continue
  cross++
  if (!perFile.has(r.file)) perFile.set(r.file, new Set()); perFile.get(r.file).add(r.name)
  if (!edges.has(r.file)) edges.set(r.file, new Set()); edges.get(r.file).add(to)
  if (lbl === 'live' && !to.startsWith('live/')) borrowed.add(r.name)
  if (lbl === 'demo' && to.startsWith('live/')) backRefs.push(`${r.name} @ ${r.file}:${r.line}`)
  if (r.load) loadRefs.push({ ...r, to })
}
let total = 0; for (const [, s] of perFile) total += s.size
console.log(`declarations: demo layer ${D.decls.size}, live layer ${L.decls.size}, overlap ${[...L.decls.keys()].filter((k)=>D.decls.has(k)).length}`)
console.log(`cross-file identifier references: ${cross}`)
console.log(`distinct (file, foreign name) pairs = explicit import bindings needed: ${total} across ${perFile.size} files`)
console.log(`live-layer names borrowed from the demo/seam layer: ${borrowed.size}`)
console.log(`demo-layer reads of live-declared names: ${backRefs.length} ${JSON.stringify(backRefs)}`)
console.log(`file-level edges: ${[...edges.values()].reduce((a,s)=>a+s.size,0)}`)
const back = []; for (const [f, tos] of edges) for (const t of tos) if (idx.get(t) > idx.get(f)) back.push(`${f} -> ${t}`)
console.log(`forward edges (file uses a name declared LATER in manifest order): ${back.length}`)
console.log(back.map((x)=>'  '+x).join('\n'))
const ids = new Map(); const low = new Map(); const on = new Set(); const stk = []; let c = 0; const sccs = []
const strong = (v) => { ids.set(v,c); low.set(v,c); c++; stk.push(v); on.add(v)
  for (const w of edges.get(v) || []) { if (!ids.has(w)) { strong(w); low.set(v, Math.min(low.get(v), low.get(w))) } else if (on.has(w)) low.set(v, Math.min(low.get(v), ids.get(w))) }
  if (low.get(v) === ids.get(v)) { const comp = []; let w; do { w = stk.pop(); on.delete(w); comp.push(w) } while (w !== v); if (comp.length > 1) sccs.push(comp) } }
for (const f of order) if (!ids.has(f)) strong(f)
console.log(`file-level cycles: ${sccs.length}`); for (const s of sccs) console.log(`  [${s.length}] ${s.sort().join(', ')}`)
const trivial = new Set(['$','mk','esc','DS'])
const hard = loadRefs.filter((r) => !trivial.has(r.name))
console.log(`\nmodule-evaluation-time cross-file reads: ${loadRefs.length} total, ${hard.length} outside {$, mk, esc, DS}`)
for (const r of hard) console.log(`  ${r.file}:${r.line}  ${r.name} <- ${r.to}`)
console.log('\n-- files by import bindings needed --')
for (const [f, s] of [...perFile].sort((a,b)=>b[1].size-a[1].size)) console.log(`${String(s.size).padStart(4)}  ${f}`)
