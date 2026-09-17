// @vitest-environment happy-dom
/* Every offline responder answers the shape the contract declares.
 *
 * The page is one piece of code in both modes, so a field the contract requires
 * and a fixture omits renders as `undefined` on the offline page while every
 * unit test stays green -- the component tests build their own objects, and the
 * live gates next door only watch the rpc layer. This walks the library the
 * offline page really serves and holds every answer to `rpc-schema/openrpc.json`
 * itself.
 *
 * TypeScript already holds the responders to the generated result types, which
 * is the same contract read at compile time. What this adds is the run: a
 * responder that builds its answer behind a cast, or narrows a union the wrong
 * way, is only caught by calling it.
 */

import { readFileSync } from 'node:fs'
/* Off cwd, not off `import.meta.url`: under happy-dom that is an http URL. */
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { PARAMS, PLAYBOOKS } from '../fixture-params.mjs'

const contract = JSON.parse(readFileSync(resolve(process.cwd(), '../rpc-schema/openrpc.json'), 'utf8'))
const schemas = contract.components.schemas
const resultOf = new Map(contract.methods.map((m) => [m.name, m.result.schema]))

const deref = (schema) => {
  let at = schema
  while (at && at.$ref) {
    const name = at.$ref.split('/').pop()
    at = schemas[name]
    if (!at) throw new Error(`${schema.$ref} is absent from the contract`)
  }
  return at || {}
}

const kinds = {
  string: (v) => typeof v === 'string',
  integer: (v) => typeof v === 'number' && Number.isInteger(v),
  number: (v) => typeof v === 'number',
  boolean: (v) => typeof v === 'boolean',
  object: (v) => !!v && typeof v === 'object' && !Array.isArray(v),
  array: (v) => Array.isArray(v),
  null: (v) => v === null,
}

/* A walk rather than a validator off the shelf: the four keywords the contract
   uses are type, required, properties and items, plus anyOf and enum. Adding a
   dependency for that would be the larger change. */
function check(schema, value, path, out) {
  const s = deref(schema)
  if (s.anyOf || s.oneOf) {
    const branches = s.anyOf || s.oneOf
    const ok = branches.some((b) => {
      const errs = []
      check(b, value, path, errs)
      return !errs.length
    })
    if (!ok) out.push(`${path}: matches none of ${branches.length} alternatives`)
    return
  }
  const types = s.type === undefined ? [] : [].concat(s.type)
  if (types.length && !types.some((t) => (kinds[t] || (() => true))(value))) {
    out.push(`${path}: expected ${types.join('|')}, got ${Array.isArray(value) ? 'array' : typeof value}`)
    return
  }
  if (s.enum && !s.enum.includes(value)) out.push(`${path}: ${JSON.stringify(value)} is not one of ${s.enum.join(', ')}`)
  if (types.includes('object') || s.properties || s.required) {
    if (!kinds.object(value)) return
    for (const key of s.required || []) {
      if (value[key] === undefined) out.push(`${path}.${key}: required by the contract, absent from the answer`)
    }
    for (const [key, sub] of Object.entries(s.properties || {})) {
      /* `null` on an optional field is the wire saying nothing, which several
         of the contract's own descriptions spell out (a playbook node's
         `skills` is three-state: null said nothing, [] said none). The
         generated types render those as optional rather than nullable, so a
         null here is checked as an absence rather than against the type. */
      if (value[key] === null && !(s.required || []).includes(key)) continue
      if (value[key] !== undefined) check(sub, value[key], `${path}.${key}`, out)
    }
  }
  if (types.includes('array') && Array.isArray(value) && s.items) {
    value.forEach((item, i) => check(s.items, item, `${path}[${i}]`, out))
  }
}

/* The library, on a clock and a timer of this gate's own: a scheduled emission
   is dropped rather than run, so a scripted turn cannot push frames into a
   page that is not there. */
async function library() {
  const { demoFixtures } = await import('../../src/rpc/fixtures/index.ts')
  const { FixtureTransport } = await import('../../src/rpc/fixtureTransport.ts')
  const transport = new FixtureTransport(demoFixtures, { now: () => 1789000000000, timer: () => {} })
  return transport
}

describe('the offline fixture library', () => {
  it('answers every method the contract declares it for', async () => {
    const transport = await library()
    const failures = []
    for (const method of Object.keys(transport.fixtures)) {
      if (!resultOf.has(method)) { failures.push(`${method}: not a contract method`); continue }
      let answer
      try {
        answer = await transport.call(method, PARAMS[method] ?? {})
      } catch (e) {
        failures.push(`${method}: threw ${e instanceof Error ? e.message : String(e)}`)
        continue
      }
      const errs = []
      check(resultOf.get(method), answer, method, errs)
      failures.push(...errs)
    }
    expect(failures).toEqual([])
  })

  /* Each playbook in turn, not just the first: the library answers `get` from
     one object per playbook, and a field missing from the seventh is exactly
     what a single-row walk would let through. */
  it('answers every playbook the same way', async () => {
    const transport = await library()
    const failures = []
    const listed = await transport.call('playbooks.list', {})
    expect(listed.playbooks.map((p) => p.name)).toEqual(PLAYBOOKS)
    for (const name of PLAYBOOKS) {
      for (const method of ['playbooks.get', 'playbooks.credentials.get']) {
        const answer = await transport.call(method, { name })
        check(resultOf.get(method), answer, `${method}(${name})`, failures)
      }
    }
    expect(failures).toEqual([])
  })

  /* The two canvases the URL asks for, held to the same contract: they are
     overrides on whichever transport the page chose, so a page reaching them on
     a LIVE socket gets these answers over real ones and a wrong shape there is
     a wrong shape in front of a working gateway. */
  it('answers every method its two canvases override', async () => {
    const base = await library()
    const { OverrideTransport } = await import('../../src/rpc/overrideTransport.ts')
    const { deskDemoOverrides, onboardDemoOverrides } = await import('../../src/rpc/fixtures/index.ts')
    const later = (ms, fn) => { fn() }
    const roster = () => Promise.resolve(['raven', 'claude_code'])
    const groups = {
      'onboard=demo': onboardDemoOverrides(later),
      'desk-demo=1': deskDemoOverrides(() => 1789000000000, later, roster),
    }
    /* The direct-chat door the canvas knocks on to tell a pane its instance
       answered reads the roster through the seam, and only a booted page has
       one installed. The shape of the answer is what this gate is about, so the
       seam gets the two verbs that door touches and nothing else. */
    const { sources } = await import('../../src/state/sources.ts')
    sources.agents = { instances: async () => [], roster: async () => [] }
    const failures = []
    for (const [label, overrides] of Object.entries(groups)) {
      const transport = new OverrideTransport(base, overrides)
      /* Asked first, and the row it answers with is what the rest are aimed at:
         the canvas rewrites its instances' agent names onto the roster the
         install really has, so a hard-coded name here would miss. */
      const listed = overrides['subagents.instances']
        ? (await transport.call('subagents.instances', { session_key: 'a' })).instances[0]
        : null
      const at = listed ? { session_key: 'a', agent: listed.agent, handle: listed.handle } : {}
      /* Forget last: it takes the row the calls above are aimed at out of the
         canvas, and a walk that ran it first would be asking the rest about an
         instance it had just removed. */
      const walk = Object.keys(overrides)
        .sort((a, b) => Number(a.endsWith('forget')) - Number(b.endsWith('forget')))
      for (const method of walk) {
        const params = method.startsWith('subagents.instance.')
          ? { ...at, ...(method.endsWith('set_mode') ? { mode: 'max' } : {}) }
          : method === 'turn.send'
            ? { session_key: 'a', content: 'go on', target: { agent: at.agent, handle: at.handle } }
            : PARAMS[method] ?? {}
        let answer
        try {
          answer = await transport.call(method, params)
        } catch (e) {
          failures.push(`${label} ${method}: threw ${e instanceof Error ? e.message : JSON.stringify(e)}`)
          continue
        }
        check(resultOf.get(method), answer, `${label} ${method}`, failures)
      }
    }
    expect(failures).toEqual([])
  })

  /* Both forks of the research conversation, because which one plays depends on
     whether web search is configured and only one of them is ever reachable
     from a single page. */
  it('answers a resume for every scripted conversation', async () => {
    const transport = await library()
    const failures = []
    for (const id of ['a', 'b', 'g']) {
      const answer = await transport.call('session.resume', { session_id: id })
      check(resultOf.get('session.resume'), answer, `session.resume(${id})`, failures)
    }
    expect(failures).toEqual([])
  })
})
