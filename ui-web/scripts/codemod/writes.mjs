import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

import { requireConcatEra } from './concat-era.mjs'
const UIWEB = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SRC = join(UIWEB, 'src', 'legacy')
requireConcatEra(SRC)
const py = readFileSync(join(UIWEB, 'build.py'), 'utf8')
const manifest = (n) => [...py.match(new RegExp(`_${n}_PARTS = \\[([\\s\\S]*?)\\]`))[1].matchAll(/"([^"]+)"/g)].map((x) => x[1])
function concat(dir, parts) { let text = ''; const spans = []
  for (const p of parts) { const t = readFileSync(join(SRC, dir, p), 'utf8'); spans.push({ file: `${dir}/${p}`, start: text.length, end: text.length + t.length }); text += t }
  if (text.endsWith('\n')) { text = text.slice(0, -1); spans[spans.length-1].end -= 1 }
  return { text, spans } }
const seam = concat('seam', manifest('SEAM')), demo = concat('demo', manifest('DEMO')), live = concat('live', manifest('LIVE'))
const off = seam.text.length + 1
const layers = {
  demo: { text: seam.text + '\n' + demo.text, spans: [...seam.spans, ...demo.spans.map((s)=>({file:s.file,start:s.start+off,end:s.end+off}))] },
  live: { text: live.text, spans: live.spans },
}
function bn(nm, out) { if (!nm) return; if (ts.isIdentifier(nm)) out.add(nm.text); else if (nm.elements) for (const e of nm.elements) { if (ts.isOmittedExpression(e)) continue; bn(e.name, out) } }
const info = {}
for (const label of ['demo','live']) {
  const { text, spans } = layers[label]
  const sf = ts.createSourceFile(label, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)
  const fileOf = (pos) => { for (const s of spans) if (pos >= s.start && pos < s.end) return s.file; return '?' }
  const lineIn = (pos) => { for (const s of spans) if (pos >= s.start && pos < s.end) { let c=1; for (let i=s.start;i<pos;i++) if (text[i]==='\n') c++; return c } return 0 }
  let topNode = sf
  if (label === 'live') { let a=null; const f=(n)=>{ if(!a && ts.isArrowFunction(n)) a=n; else ts.forEachChild(n,f) }; f(sf.statements[0]); topNode=a }
  const topStatements = label === 'live' ? topNode.body.statements : sf.statements
  const decls = new Map()
  const kinds = new Map()
  for (const st of topStatements) {
    if (ts.isVariableStatement(st)) { const k = st.declarationList.flags & ts.NodeFlags.Const ? 'const' : st.declarationList.flags & ts.NodeFlags.Let ? 'let' : 'var'
      for (const d of st.declarationList.declarations) { const s=new Set(); bn(d.name,s); for (const n of s) { decls.set(n, fileOf(d.getStart(sf))); kinds.set(n, k) } } }
    else if (ts.isFunctionDeclaration(st) && st.name) { decls.set(st.name.text, fileOf(st.name.getStart(sf))); kinds.set(st.name.text, 'function') }
    else if (ts.isClassDeclaration(st) && st.name) { decls.set(st.name.text, fileOf(st.name.getStart(sf))); kinds.set(st.name.text, 'class') }
  }
  info[label] = { sf, fileOf, lineIn, decls, kinds }
}
const all = new Map([...info.demo.decls, ...[...info.live.decls].filter(([k])=>!info.demo.decls.has(k))])
const kindOf = new Map([...info.demo.kinds, ...[...info.live.kinds].filter(([k])=>!info.demo.kinds.has(k))])
const out = []
for (const label of ['demo','live']) {
  const { sf, fileOf, lineIn } = info[label]
  const walk = (n) => {
    if (ts.isBinaryExpression(n) && n.operatorToken.kind === ts.SyntaxKind.EqualsToken && ts.isIdentifier(n.left)) {
      const name = n.left.text, to = all.get(name)
      if (to) { const from = fileOf(n.left.getStart(sf)); if (from !== to) out.push({ from, line: lineIn(n.left.getStart(sf)), name, to, kind: kindOf.get(name) }) }
    }
    ts.forEachChild(n, walk)
  }
  walk(sf)
}
console.log(`cross-file assignments to a binding declared in another concat file: ${out.length}`)
for (const w of out) console.log(`  ${w.from}:${w.line}  ${w.name} = ...   (declared ${w.kind} in ${w.to})`)
const names = new Set(out.map((w)=>w.name))
console.log(`\ndistinct names reassigned across files: ${names.size} ${JSON.stringify([...names])}`)
const fnNames = [...names].filter((n)=>kindOf.get(n)==='function')
console.log(`of which declared with 'function' (illegal to reassign via ESM import, and not even assignable in the SAME module from another module): ${fnNames.length} ${JSON.stringify(fnNames)}`)
