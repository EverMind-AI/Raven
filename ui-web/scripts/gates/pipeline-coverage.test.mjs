/* Every turn event the contract declares has a stage, and no stage names one
 * it does not.
 *
 * The compiler already proves the first half: `stageOf` switches on `ev.type`
 * and its default hands the frame to `assertNever`, which a member with no case
 * cannot satisfy. What it cannot prove is the other direction -- a stage may
 * declare a `handles` entry for a name that has been renamed or removed on the
 * wire, and a table entry nothing can reach is a stage nobody will ever notice
 * is wrong. So the union is parsed out of the generated types and compared
 * against the declared handles, both ways.
 *
 * The declaration is read rather than imported because importing the table
 * evaluates the whole session graph, which is a page's worth of modules for a
 * question about two lists.
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const read = (rel) => readFileSync(resolve(process.cwd(), rel), 'utf8')

/** The `type` literal of every member of the generated `TurnEvent` union. */
function contractTypes() {
  const rel = 'src/rpc/generated.ts'
  const sf = ts.createSourceFile(rel, read(rel), ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS)
  const union = sf.statements.find((st) => ts.isTypeAliasDeclaration(st) && st.name.text === 'TurnEvent')
  if (!union || !ts.isUnionTypeNode(union.type)) throw new Error(`${rel}: TurnEvent is not a union type`)
  /* Each member is an interface reference; the discriminator is the `type`
     property's literal on that interface. */
  const byName = new Map()
  for (const st of sf.statements) {
    if (!ts.isInterfaceDeclaration(st)) continue
    const member = st.members.find((m) => ts.isPropertySignature(m) && m.name.getText(sf) === 'type')
    if (!member || !member.type || !ts.isLiteralTypeNode(member.type)) continue
    if (!ts.isStringLiteral(member.type.literal)) continue
    byName.set(st.name.text, member.type.literal.text)
  }
  return union.type.types.map((node) => {
    const name = node.getText(sf)
    const found = byName.get(name)
    if (!found) throw new Error(`${rel}: ${name} declares no string-literal type`)
    return found
  })
}

/** Every name the stage table declares it handles. */
function declaredHandles() {
  const rel = 'src/state/session/stages.ts'
  const sf = ts.createSourceFile(rel, read(rel), ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS)
  const table = sf.statements.find((st) => ts.isVariableStatement(st)
    && st.declarationList.declarations.some((d) => d.name.getText(sf) === 'STAGES'))
  if (!table) throw new Error(`${rel}: no STAGES declaration`)
  /* An `arm('x', …)` names one and an `unhandled([…])` several, and in both the
     names are the FIRST argument: a stage's body is full of other strings. */
  const found = []
  const names = (node) => {
    if (ts.isStringLiteral(node)) { found.push(node.text); return }
    if (ts.isArrayLiteralExpression(node)) { for (const el of node.elements) names(el); return }
    throw new Error(`${rel}: a stage names its events as ${node.getText(sf)}`)
  }
  const walk = (node) => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)
      && (node.expression.text === 'arm' || node.expression.text === 'unhandled')) {
      if (!node.arguments[0]) throw new Error(`${rel}: a stage names no event`)
      names(node.arguments[0])
    }
    ts.forEachChild(node, walk)
  }
  for (const d of table.declarationList.declarations) if (d.initializer) walk(d.initializer)
  return found
}

describe('the stage table against the contract', () => {
  const contract = contractTypes()
  const handles = declaredHandles()

  it('read both lists at all', () => {
    /* A parse that found nothing would make every assertion below vacuous. */
    expect(contract.length).toBe(24)
    expect(handles.length).toBeGreaterThanOrEqual(24)
  })

  it('has a stage for every event the contract declares', () => {
    const missing = contract.filter((name) => !handles.includes(name))
    expect(missing).toEqual([])
  })

  it('declares no event the contract does not', () => {
    /* The two the page used to handle and nothing ever sent -- `cron.started`
       and `cron.finished` -- are what this would have caught. */
    const extra = handles.filter((name) => !contract.includes(name))
    expect(extra).toEqual([])
  })

  it('gives each event exactly one stage', () => {
    const twice = handles.filter((name, at) => handles.indexOf(name) !== at)
    expect(twice).toEqual([])
  })
})
