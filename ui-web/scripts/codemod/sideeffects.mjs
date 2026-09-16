import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
const UIWEB = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SRC = join(UIWEB, 'src', 'legacy')
const py = readFileSync(join(UIWEB, 'build.py'), 'utf8')
const manifest = (n) => [...py.match(new RegExp(`_${n}_PARTS = \\[([\\s\\S]*?)\\]`))[1].matchAll(/"([^"]+)"/g)].map((x) => x[1])
const files = [...manifest('SEAM').map((p)=>['seam',p]), ...manifest('DEMO').map((p)=>['demo',p]), ...manifest('LIVE').map((p)=>['live',p])]
for (const [dir,name] of files) {
  const text = readFileSync(join(SRC,dir,name),'utf8')
  const sf = ts.createSourceFile(name, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)
  // for live parts, the file is a fragment of one IIFE; parse anyway and take
  // statements at depth 0 (fragments parse as statements after error recovery)
  const out = []
  const line = (n)=>sf.getLineAndCharacterOfPosition(n.getStart(sf)).line+1
  for (const st of sf.statements) {
    if (ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) continue
    if (ts.isVariableStatement(st)) {
      // only flag initialisers that call something / are IIFEs
      for (const d of st.declarationList.declarations) {
        const i = d.initializer
        if (i && (ts.isCallExpression(i) || (ts.isParenthesizedExpression(i)))) out.push(`${line(d)}  var-init call: ${text.slice(d.getStart(sf), Math.min(d.end, d.getStart(sf)+90)).replace(/\s+/g,' ')}`)
      }
      continue
    }
    if (ts.isExpressionStatement(st) || ts.isBlock(st) || ts.isIfStatement(st) || ts.isForStatement(st) || ts.isForOfStatement(st) || ts.isTryStatement(st) || ts.isSwitchStatement(st) || ts.isLabeledStatement(st)) {
      out.push(`${line(st)}  ${text.slice(st.getStart(sf), Math.min(st.end, st.getStart(sf)+110)).replace(/\s+/g,' ')}`)
    }
  }
  if (out.length) { console.log(`\n--- ${dir}/${name} : ${out.length} eval-time statements ---`); for (const o of out) console.log('   '+o) }
}
