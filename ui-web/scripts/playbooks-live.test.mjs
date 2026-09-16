/* The live layer installs a playbook source that speaks the contract.
 *
 * Two failures this catches without a gateway: a method name the dispatcher does
 * not register (which reads as -32601 at the moment a reader opens the page),
 * and an answer the island is handed still wrapped -- both of which the fixture
 * source cannot reveal, because in demo mode this file never runs. */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart } from './legacy-part.mjs'

const contract = JSON.parse(readFileSync(new URL('../../rpc-schema/openrpc.json', import.meta.url), 'utf8'))
const declared = new Set(contract.methods.map((m) => m.name))

async function source(answers) {
  const calls = []
  const part = await loadPart(() => import('../src/legacy/live/167-playbooks.js'))
  await fakeGateway((method, params) => {
    calls.push([method, params])
    return Promise.resolve(answers[method])
  })
  const { DS } = await import('../src/legacy/seam/000-datasource.js')
  part.install()
  if (!DS.playbooks) throw new Error('DS.playbooks is absent from the live layer')
  return { source: DS.playbooks, calls }
}

describe('the live playbook source', () => {
  it('calls methods the contract declares', async () => {
    const { source: src, calls } = await source({
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
    const { source: src } = await source({
      'playbooks.list': { playbooks: [{ name: 'a' }, { name: 'b' }] },
      'playbooks.get': { playbook: { name: 'a', nodes: [] } }
    })
    expect(await src.list()).toHaveLength(2)
    expect((await src.get('a')).name).toBe('a')
  })

  it('answers an empty library as an empty list, not as undefined', async () => {
    /* An engine with no playbooks stored is a normal state, and the page's
       "nothing here yet" needs a list to be empty rather than absent. */
    const { source: src } = await source({ 'playbooks.list': {} })
    expect(await src.list()).toEqual([])
  })
})
