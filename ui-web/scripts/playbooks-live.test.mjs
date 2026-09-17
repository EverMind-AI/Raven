// @vitest-environment happy-dom
/* The page installs a playbook source that speaks the contract.
 *
 * Two failures this catches without a gateway: a method name the dispatcher does
 * not register (which reads as -32601 at the moment a reader opens the page),
 * and an answer the island is handed still wrapped. */

import { readFileSync } from 'node:fs'
/* Off cwd, not off `import.meta.url`: under happy-dom that is an http URL. */
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart } from './module-harness.mjs'

const contract = JSON.parse(readFileSync(resolve(process.cwd(), '../rpc-schema/openrpc.json'), 'utf8'))
const declared = new Set(contract.methods.map((m) => m.name))

async function source(answers) {
  const calls = []
  const wiring = await loadPart(() => import('../src/app/install'))
  await fakeGateway((method, params) => {
    calls.push([method, params])
    return Promise.resolve(answers[method])
  })
  const { setSources, sources } = await import('../src/state/sources')
  /* The two seam objects the chrome builds, which this case does not install. */
  setSources({ composer: {}, transcript: {} })
  wiring.installSources()
  if (!sources.playbooks) throw new Error('sources.playbooks is absent from the page wiring')
  return { source: sources.playbooks, calls }
}

describe('the page playbook source', () => {
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
