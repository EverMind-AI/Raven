// Prints every top-level declared name in the legacy layers, with its owner
// file. Used to pick collision-free names for new shared-state containers.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const UIWEB = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SRC = join(UIWEB, 'src', 'legacy')
const py = readFileSync(join(UIWEB, 'build.py'), 'utf8')
const manifest = (n) => [...py.match(new RegExp(`_${n}_PARTS = \\[([\\s\\S]*?)\\]`))[1].matchAll(/"([^"]+)"/g)].map((x) => x[1])
const files = [...manifest('SEAM').map((p) => ['seam', p]), ...manifest('DEMO').map((p) => ['demo', p]), ...manifest('LIVE').map((p) => ['live', p])]
function bn(nm, out) {
  if (!nm) return
  if (ts.isIdentifier(nm)) out.add(nm.text)
  else if (nm.elements) for (const e of nm.elements) { if (!ts.isOmittedExpression(e)) bn(e.name, out) }
}
const owner = new Map()
for (const [dir, name] of files) {
  const text = readFileSync(join(SRC, dir, name), 'utf8')
  const sf = ts.createSourceFile(name, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)
  let stmts = sf.statements
  if (dir === 'live' && stmts.length && ts.isExpressionStatement(stmts[0])) {
    let a = null
    const f = (n) => { if (!a && ts.isArrowFunction(n)) a = n; else ts.forEachChild(n, f) }
    f(stmts[0])
    if (a && a.body && ts.isBlock(a.body)) stmts = [...a.body.statements, ...stmts.slice(1)]
  }
  const out = new Set()
  for (const st of stmts) {
    if (ts.isVariableStatement(st)) for (const d of st.declarationList.declarations) bn(d.name, out)
    else if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) && st.name) out.add(st.name.text)
  }
  for (const n of out) {
    if (owner.has(n)) console.log(`DUPLICATE ${n}: ${owner.get(n)} and ${dir}/${name}`)
    else owner.set(n, `${dir}/${name}`)
  }
}
const probe = process.argv.slice(2)
if (probe.length) {
  for (const p of probe) console.log(owner.has(p) ? `TAKEN  ${p} -> ${owner.get(p)}` : `free   ${p}`)
} else {
  console.log(`top-level declared names: ${owner.size}`)
  for (const [n, f] of [...owner].sort()) console.log(`  ${n}  ${f}`)
}
