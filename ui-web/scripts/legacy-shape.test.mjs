/* The shape that makes the legacy module graph's evaluation order irrelevant.
 *
 * The 49 parts were one concatenated script whose order was load-bearing, and
 * ES modules do not honour that order: each evaluates when the graph first
 * reaches it, depth first. Both layers also contain a cycle -- 14 files in
 * demo/, 7 in live/ -- so inside one, a module can be evaluated before the
 * cycle-mate it reads has run at all.
 *
 * So the order was taken out of the evaluation: a module body may only
 * DECLARE, everything else lives in its install(), and src/legacy/index.js
 * calls those in the manifests' order. Two rules hold that, and this file is
 * both of them:
 *
 *   1. a top-level statement is an import, a declaration, `export function
 *      install`/`liveMode`, or the trailing `export { }`;
 *   2. a top-level initialiser references no binding imported from a part in
 *      its own strongly connected component -- a dependency outside it has
 *      finished evaluating before your body runs, one inside it may not have.
 *
 * Rule 2 is the one with teeth: a `const` initialised from a cycle-mate's
 * binding is a TDZ throw at boot, in one of the two load modes, depending on
 * which file the graph reached first.
 */

import { readFileSync } from 'node:fs'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { partNames } from './legacy-part.mjs'

const url = (p) => new URL(`../${p}`, import.meta.url)
/* The parts, in the order src/legacy/index.js installs them -- which is also
   where partNames checks that every file on disk is installed. */
const FILES = ['seam', 'demo', 'live'].flatMap((layer) =>
  partNames(layer).map((name) => `${layer}/${name}`),
)
/* What the codemod allows a top-level initialiser to read. Not trusted here:
   the gate derives the real condition from the import graph and checks that
   every one of these is outside every cycle. */
const CODEMOD_LEAVES = new Set(['seam/000-datasource.js', 'demo/010-kernel.js', 'demo/020-prose.js', 'demo/030-fixtures.js'])

const texts = new Map(FILES.map((rel) => [rel, readFileSync(url(`src/legacy/${rel}`), 'utf8')]))
const trees = new Map(
  [...texts].map(([rel, text]) => [rel, ts.createSourceFile(rel, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)]),
)

const names = (nm, out) => {
  if (!nm) return
  if (ts.isIdentifier(nm)) out.add(nm.text)
  else if (nm.elements) for (const e of nm.elements) { if (!ts.isOmittedExpression(e)) names(e.name, out) }
}
const declaredAtTop = (sf) => {
  const out = new Set()
  for (const st of sf.statements) {
    if (ts.isVariableStatement(st)) for (const d of st.declarationList.declarations) names(d.name, out)
    else if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) && st.name) out.add(st.name.text)
  }
  return out
}
const owner = new Map()
for (const [rel, sf] of trees) for (const n of declaredAtTop(sf)) owner.set(n, rel)

const importsOf = (rel) => {
  const sf = trees.get(rel)
  const out = new Set()
  for (const st of sf.statements) {
    if (!ts.isImportDeclaration(st)) continue
    const spec = st.moduleSpecifier.text
    if (!spec.startsWith('.') || !spec.endsWith('.js')) continue
    const target = spec.startsWith('./') ? `${rel.split('/')[0]}/${spec.slice(2)}` : spec.replace(/^\.\.\//, '')
    if (trees.has(target)) out.add(target)
  }
  return out
}

/* Tarjan, because the real rule is not "which file is a leaf" but "which
   files evaluate as one knot": a dependency outside your own strongly
   connected component is finished before your body runs, and one inside it
   may not be. */
const sccOf = (() => {
  const index = new Map(); const low = new Map(); const onStack = new Set(); const stack = []
  const comp = new Map(); let counter = 0
  const strong = (v) => {
    index.set(v, counter); low.set(v, counter); counter += 1
    stack.push(v); onStack.add(v)
    for (const w of importsOf(v)) {
      if (!index.has(w)) { strong(w); low.set(v, Math.min(low.get(v), low.get(w))) }
      else if (onStack.has(w)) low.set(v, Math.min(low.get(v), index.get(w)))
    }
    if (low.get(v) !== index.get(v)) return
    const members = []
    let w
    do { w = stack.pop(); onStack.delete(w); members.push(w) } while (w !== v)
    for (const m of members) comp.set(m, members)
  }
  for (const rel of FILES) if (!index.has(rel)) strong(rel)
  return comp
})()
const cycleMates = (rel) => new Set(sccOf.get(rel).length > 1 ? sccOf.get(rel) : [])

const line = (sf, node) => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1

describe('every legacy module body only declares', () => {
  for (const rel of FILES) {
    it(rel, () => {
      const sf = trees.get(rel)
      const offenders = []
      let installs = 0
      for (const st of sf.statements) {
        if (ts.isImportDeclaration(st) || ts.isVariableStatement(st) || ts.isClassDeclaration(st)) continue
        if (ts.isExportDeclaration(st)) continue
        if (ts.isFunctionDeclaration(st)) {
          if (st.name?.text === 'install') installs += 1
          continue
        }
        offenders.push(`${rel}:${line(sf, st)} ${ts.SyntaxKind[st.kind]}`)
      }
      expect(offenders).toEqual([])
      expect(installs).toBe(1)
      expect(texts.get(rel)).toContain('export function install()')
    })
  }
})

describe('no top-level initialiser reads a binding from a cycle-mate', () => {
  for (const rel of FILES) {
    it(rel, () => {
      const sf = trees.get(rel)
      const own = declaredAtTop(sf)
      const mates = cycleMates(rel)
      const imported = new Set()
      for (const st of sf.statements) {
        if (!ts.isImportDeclaration(st)) continue
        const named = st.importClause?.namedBindings
        if (named && ts.isNamedImports(named)) for (const s of named.elements) imported.add(s.name.text)
      }
      const offenders = []
      for (const st of sf.statements) {
        if (!ts.isVariableStatement(st)) continue
        for (const d of st.declarationList.declarations) {
          if (!d.initializer) continue
          const walk = (n) => {
            // A function body runs later; what it may read is not this rule's
            // business, which is exactly why arrow initialisers may stay.
            if (ts.isFunctionLike(n)) return
            if (ts.isIdentifier(n)) {
              const p = n.parent
              const isProp = (ts.isPropertyAccessExpression(p) && p.name === n)
                || (ts.isPropertyAssignment(p) && p.name === n)
                || (ts.isBindingElement(p) && p.propertyName === n)
              const home = owner.get(n.text)
              if (!isProp && !own.has(n.text) && imported.has(n.text) && home && mates.has(home)) {
                offenders.push(`${rel}:${line(sf, d)} reads ${n.text} from cycle-mate ${home}`)
              }
            }
            ts.forEachChild(n, walk)
          }
          walk(d.initializer)
        }
      }
      expect(offenders).toEqual([])
    })
  }
})

describe('the codemod leaf list', () => {
  /* The codemod moves an initialiser into install() unless every part it
     reads is on its leaf list, which is only sound while no leaf sits in a
     cycle. If one ever does, the list is what has to change. */
  it('names no part that is inside a cycle', () => {
    const inside = [...CODEMOD_LEAVES].filter((rel) => sccOf.get(rel).length > 1)
    expect(inside).toEqual([])
  })

  it('finds the two cycles the layers are known to have', () => {
    /* 14 in demo/, 7 in live/. The live knot was 13 while every part that
       speaks to the gateway imported the rpc client from live/020-rpc.js;
       those parts reach the transport through state/gateway.ts now, which is
       outside the layer, so six of them left the cycle. */
    const sizes = [...new Set([...sccOf.values()].filter((c) => c.length > 1))].map((c) => c.length).sort((a, b) => b - a)
    expect(sizes).toEqual([14, 7])
  })
})

describe('the layers have no module-syntax leftovers', () => {
  it('no part opens or closes the old live IIFE', () => {
    const bad = FILES.filter((rel) => texts.get(rel).includes('})();\n') && /\n\}\)\(\);\s*$/.test(texts.get(rel)))
    expect(bad).toEqual([])
  })

  it('no part carries an assembly marker any more', () => {
    const bad = FILES.filter((rel) => /\/\*__[A-Z0-9]+__\*\//.test(texts.get(rel)))
    expect(bad).toEqual([])
  })
})
