/* The assembled live layer installs a playbook source that speaks the contract.
 *
 * Two failures this catches without a gateway: a method name the dispatcher does
 * not register (which reads as -32601 at the moment a reader opens the page),
 * and an answer the island is handed still wrapped -- both of which the fixture
 * source cannot reveal, because in demo mode this file never runs. */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const build = readFileSync(new URL('../build.py', import.meta.url), 'utf8')
const manifest = build.match(/_LIVE_PARTS = \[(.*?)\n\]/s)
if (!manifest) throw new Error('_LIVE_PARTS is absent from build.py')
const parts = [...manifest[1].matchAll(/"([^"]+\.js)"/g)].map((m) => m[1])
const live = parts
  .map((name) => readFileSync(new URL(`../src/live/${name}`, import.meta.url), 'utf8'))
  .join('')

const contract = JSON.parse(readFileSync(new URL('../../rpc-schema/openrpc.json', import.meta.url), 'utf8'))
const declared = new Set(contract.methods.map((m) => m.name))

function installation() {
  const mark = 'DS.playbooks = {'
  const start = live.indexOf(mark)
  if (start < 0) throw new Error('DS.playbooks is absent from the assembled live layer')
  const brace = live.indexOf('{', start)
  let depth = 0
  for (let index = brace; index < live.length; index += 1) {
    if (live[index] === '{') depth += 1
    else if (live[index] === '}') {
      depth -= 1
      if (depth === 0) return live.slice(start, index + 2)
    }
  }
  throw new Error('DS.playbooks has no closing brace in the assembled live layer')
}

function source(answers) {
  const calls = []
  const DS = {}
  const rpc = {
    call: (method, params) => {
      calls.push([method, params])
      return Promise.resolve(answers[method])
    }
  }
  Function('DS', 'rpc', installation())(DS, rpc)
  return { source: DS.playbooks, calls }
}

describe('the assembled live playbook source', () => {
  it('calls methods the contract declares', async () => {
    const { source: src, calls } = source({
      'playbooks.list': { playbooks: [{ name: 'a' }] },
      'playbooks.get': { playbook: { name: 'a' } }
    })
    await src.list()
    await src.get('a')
    expect(calls.map((c) => c[0])).toEqual(['playbooks.list', 'playbooks.get'])
    for (const [method] of calls) expect(declared.has(method)).toBe(true)
    expect(calls[1][1]).toEqual({ name: 'a' })
  })

  it('hands the island the list and the playbook, not the envelope', async () => {
    const { source: src } = source({
      'playbooks.list': { playbooks: [{ name: 'a' }, { name: 'b' }] },
      'playbooks.get': { playbook: { name: 'a', nodes: [] } }
    })
    expect(await src.list()).toHaveLength(2)
    expect((await src.get('a')).name).toBe('a')
  })

  it('answers an empty library as an empty list, not as undefined', async () => {
    /* An engine with no playbooks stored is a normal state, and the page's
       "nothing here yet" needs a list to be empty rather than absent. */
    const { source: src } = source({ 'playbooks.list': {} })
    expect(await src.list()).toEqual([])
  })
})
