/* Every method name the page calls is one the contract declares.
 *
 * The legacy layer is plain JavaScript, so `gateway().call('sessoin.list', ...)`
 * type checks as readily as the spelling that exists -- the compiler only sees
 * the transport's signature where the caller is TypeScript. A misspelt name is
 * a -32601 at the moment a reader opens the page that makes it, which is the
 * one failure this refactor was supposed to end.
 *
 * So the names are collected from the source with the TypeScript API and held
 * to RPC_METHODS, which is generated from rpc-schema/openrpc.json. The feature
 * sources are read too, even though tsc already checks them: the unchecked
 * escape hatch below is a plain string on either side of the line, and this is
 * what keeps it to the two names it was opened for. They answer -32601 today
 * and must go on doing so rather than being typed into existence or silently
 * dropped.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { partNames } from './legacy-part.mjs'

import { RPC_METHODS } from '../src/rpc/generated'

/* The two undeclared names, and the only two allowed. Both are the manual
   plugin-add path in features/plugins/source.ts. */
const UNCHECKED = ['raven.mcp.list', 'raven.mcp.set']

/* Both legacy layers, every feature's source module, and the page's own wiring
   and boot: the calls moved there as each domain left the layer, and the three
   sets together are the page's whole traffic. Paths are from src/. */
const sourceModules = () => readdirSync(resolve(process.cwd(), 'src/features'), { withFileTypes: true })
  .filter((e) => e.isDirectory())
  .map((e) => `features/${e.name}/source.ts`)
  .filter((rel) => {
    try { readFileSync(resolve(process.cwd(), 'src', rel)); return true } catch { return false }
  })

const FILES = [
  ...['demo'].flatMap((layer) => partNames(layer).map((name) => `legacy/${layer}/${name}`)),
  ...sourceModules(),
  'state/boot.ts',
  'state/connection.ts',
  'state/install.ts',
  'state/updates.ts',
]

/* A `gateway().call(...)` or `gateway().binary(...)`: the callee is a property
   of a call to `gateway`, which is what tells it apart from `source.call(...)`
   or any other object's method of the same name. */
function isGatewayMember(node, member) {
  if (!ts.isCallExpression(node)) return false
  const callee = node.expression
  if (!ts.isPropertyAccessExpression(callee) || callee.name.text !== member) return false
  const object = callee.expression
  return ts.isCallExpression(object)
    && ts.isIdentifier(object.expression)
    && object.expression.text === 'gateway'
}

/* Every [file:line, name] the layer calls `member` with. A name that is not a
   string literal is reported as such: an expression is a name nothing can
   check, which is the whole failure mode here. */
function names(member) {
  const found = []
  for (const rel of FILES) {
    const text = readFileSync(resolve(process.cwd(), 'src', rel), 'utf8')
    const kind = rel.endsWith('.ts') ? ts.ScriptKind.TS : ts.ScriptKind.JS
    const sf = ts.createSourceFile(rel, text, ts.ScriptTarget.ES2022, true, kind)
    const walk = (node) => {
      if (isGatewayMember(node, member)) {
        const first = node.arguments[0]
        const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1
        found.push([
          `${rel}:${line}`,
          first && ts.isStringLiteral(first) ? first.text : `<not a literal: ${first ? first.getText(sf) : 'no argument'}>`,
        ])
      }
      ts.forEachChild(node, walk)
    }
    walk(sf)
  }
  return found
}

describe('the method names the page calls', () => {
  const declared = new Set(RPC_METHODS)
  const called = names('call')

  it('reaches the layer at all', () => {
    /* A gate that found nothing would pass forever. */
    expect(called.length).toBeGreaterThan(100)
  })

  it('are all declared by the contract', () => {
    const undeclared = called.filter(([, name]) => !declared.has(name)).map(([at, name]) => `${at} ${name}`)
    expect(undeclared).toEqual([])
  })

  it('go through the unchecked path only for the two the contract omits', () => {
    const unchecked = names('callUnchecked').map(([, name]) => name)
    expect([...new Set(unchecked)].sort()).toEqual([...UNCHECKED].sort())
    /* And those two are genuinely absent from the contract -- listing a
       declared name here would be a call quietly opting out of the check. */
    for (const name of UNCHECKED) expect(declared.has(name)).toBe(false)
  })
})
