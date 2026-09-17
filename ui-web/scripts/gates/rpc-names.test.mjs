/* Every method name the page calls is one the contract declares.
 *
 * Written against the legacy layer, which was plain JavaScript: there
 * `gateway().call('sessoin.list', ...)` type checked as readily as the spelling
 * that exists, because the compiler only saw the transport's signature where
 * the caller was TypeScript. A misspelt name is a -32601 at the moment a reader
 * opens the page that makes it, which is the one failure this refactor was
 * supposed to end.
 *
 * The layer is gone and tsc now sees every call site, so what this is still for
 * is the unchecked escape hatch: `callUnchecked` takes a plain string on either
 * side of the line, and this is what keeps it to the two names it was opened
 * for. They answer -32601 today and must go on doing so rather than being typed
 * into existence or silently dropped. The names are collected from the source
 * with the TypeScript API (scripts/gates/rpcCalls.mjs, which offline-coverage
 * reads too) and held to RPC_METHODS, which is generated from
 * rpc-schema/openrpc.json.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { names } from './rpcCalls.mjs'
import { RPC_METHODS } from '../../src/rpc/generated'

/* The two undeclared names, and the only two allowed. Both are the manual
   plugin-add path in features/plugins/source.ts. */
const UNCHECKED = ['raven.mcp.list', 'raven.mcp.set']

/* Every module under features/, components and all, for the rule below. */
const featureModules = (dir = 'features') => readdirSync(resolve(process.cwd(), 'src', dir), { withFileTypes: true })
  .flatMap((e) => (e.isDirectory()
    ? featureModules(`${dir}/${e.name}`)
    : /\.tsx?$/.test(e.name) && !e.name.includes('.test.') ? [`${dir}/${e.name}`] : []))

/* Domain modules that speak to the gateway outside their own source.ts. None
   today; a name here would be a call the domain's source does not know about,
   pinned with the reason it cannot move. Down or gone. */
const OUTSIDE_SOURCE = {}

describe('the method names the page calls', () => {
  const declared = new Set(RPC_METHODS)
  const called = names('call')

  it('reaches the layer at all', () => {
    /* A ratchet rather than a floor near zero: a gate that found nothing would
       pass forever, and so would one that lost a whole scan root -- the seven
       calls in app/ dropping out of the count was invisible under `> 100`. */
    expect(called.length).toBeGreaterThan(140)
  })

  /* Where a call may be written, as opposed to what it may be named. A domain's
     source is "everything one domain knows about speaking to the gateway"
     (CONTEXT.md), and the value of that is a reader who can answer "what does
     this page ask of the server for X" by opening one file. A wire call in a
     component, a tab module or a store is that answer being somewhere else --
     and it is also outside the scan above, so the name goes unchecked. */
  it('keeps every call in a domain\'s own source module', () => {
    const stray = featureModules()
      .filter((rel) => !rel.endsWith('/source.ts') && !(rel in OUTSIDE_SOURCE))
      .filter((rel) => /gateway\(\)/.test(readFileSync(resolve(process.cwd(), 'src', rel), 'utf8')))
    expect(stray, 'move the call into the domain\'s source.ts, or pin it in OUTSIDE_SOURCE with the reason')
      .toEqual([])
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
