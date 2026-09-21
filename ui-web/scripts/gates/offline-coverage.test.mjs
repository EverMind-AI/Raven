// @vitest-environment happy-dom
/* Every method the page calls has an answer on the offline page.
 *
 * The library is gated inwards -- scripts/gates/fixture-shape.test.mjs holds
 * every answer it gives to the contract -- and was gated outwards by nothing.
 * src/rpc/fixtureTransport.ts answers an unregistered method with -32601, which
 * src/rpc/capabilities.ts reads as "this gateway is older than this page": a
 * call added to a domain's source works live and, on `?stub=1` and on a page
 * opened from disk, quietly does nothing. Both boot goldens are taken from that
 * page, and neither can see it.
 *
 * So: intersect the names the page calls with the names the library registers,
 * and pin what is left. Each exemption says why the offline page has no answer
 * to give, because "nothing to show here" and "nobody wrote the fixture" are
 * the same silence otherwise.
 */

import { describe, expect, it } from 'vitest'

import { names } from './rpcCalls.mjs'

/* Methods the page calls that the offline library deliberately does not
   answer, and why. Down or gone: writing the responder takes the line off, and
   a name the page no longer calls comes off with it. */
const EXEMPT = {
  /* The page answering a question the gateway asked. None of the scripted
     conversations asks one -- their frames are say/think/tool/answer/end, no
     confirm, approval or clarify request (src/rpc/fixtures/turn.ts) -- so there
     is never a request_id on this page to respond about. A script that pushes a
     request is what these three need, and it would want the sheets' own
     behaviour anyway (src/state/session/pipeline.ts). */
  'confirm.respond': 'no scripted turn asks for a confirmation',
  'approval.respond': 'no scripted turn asks for an approval',
  'approval.revoke': 'no scripted turn saves a rule there would be to take back',
  'clarify.respond': 'no scripted turn asks a clarifying question',
  /* A spawned run's own record. The scripted conversations carry their tool
     calls as frames rather than as spawned runs, so no call id on this page has
     a record to read (src/features/transcript/source.ts asks for one only after
     a run reports itself running). */
  'subagent.context': 'no scripted turn spawns a run with a record to read',
  /* The direct-chat instances. The base library answers `subagents.instances`
     with none, so three of these are reached only on the desk canvas and are
     answered there by deskDemoOverrides (src/rpc/fixtures/subagents.ts), which
     this gate does not count: `?stub=1` and a page opened from disk are what it
     is about, and both goldens come from the first of those. `create` is
     answered by nothing at all -- the canvas starts with its instances already
     there. */
  'subagents.instance.create': 'nothing answers it; the desk canvas starts with its instances',
  'subagents.instance.forget': 'the desk canvas answers it, the base library has no instances',
  'subagents.instance.set_mode': 'the desk canvas answers it, the base library has no instances',
  'subagents.instance.set_model': 'the desk canvas answers it, the base library has no instances',
}

describe('the offline library answers what the page asks', () => {
  it('registers a responder for every method the page calls', async () => {
    const { demoFixtures } = await import('../../src/rpc/fixtures/index.ts')
    const registered = new Set(Object.keys(demoFixtures({
      now: () => 1789000000000, emit: () => {}, schedule: () => {},
    })))
    const called = [...new Set(names('call').map(([, name]) => name))].sort()
    const missing = called.filter((name) => !registered.has(name))
    expect(missing.filter((name) => !(name in EXEMPT)),
      'write the responder in src/rpc/fixtures/, or pin it in EXEMPT with why the offline page has no answer')
      .toEqual([])
    expect(Object.keys(EXEMPT).filter((name) => !missing.includes(name)).sort(),
      'answered or no longer called: take it off EXEMPT')
      .toEqual([])
    for (const [name, why] of Object.entries(EXEMPT)) {
      expect(why.length, `${name}: give the exemption a reason`).toBeGreaterThan(20)
    }
  })

  /* And the other end: a name the page calls has to be a name the transport can
     tell apart from a typo. An expression rather than a literal is a name this
     gate and rpc-names both lose sight of. */
  it('calls every method by a literal name', () => {
    const wild = names('call').filter(([, name]) => name.startsWith('<not a literal'))
    expect(wild.map(([at, name]) => `${at} ${name}`)).toEqual([])
  })
})
