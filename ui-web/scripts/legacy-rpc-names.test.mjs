/* Every method name the legacy layer calls is one the contract declares.
 *
 * The layer is plain JavaScript, so `gateway().call('sessoin.list', ...)` type
 * checks as readily as the spelling that exists -- the compiler only sees the
 * transport's signature where the caller is TypeScript. A misspelt name is a
 * -32601 at the moment a reader opens the page that makes it, which is the one
 * failure this refactor was supposed to end.
 *
 * So the names are collected from the source with the TypeScript API and held
 * to RPC_METHODS, which is generated from rpc-schema/openrpc.json. The two the
 * contract does not declare go through `callUnchecked` and are listed here by
 * name: they answer -32601 today and must go on doing so rather than being
 * typed into existence or silently dropped.
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { partNames } from './legacy-part.mjs'

import { RPC_METHODS } from '../src/rpc/generated'

/* The two undeclared names, and the only two allowed. Both are the manual
   plugin-add path in live/150-plugins.js. */
const UNCHECKED = ['raven.mcp.list', 'raven.mcp.set']

const FILES = ['demo', 'live'].flatMap((layer) =>
  partNames(layer).map((name) => `${layer}/${name}`),
)

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
    const text = readFileSync(resolve(process.cwd(), 'src/legacy', rel), 'utf8')
    const sf = ts.createSourceFile(rel, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)
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

describe('the method names the legacy layer calls', () => {
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
