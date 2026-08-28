/* The fixture library answers the same shape the handlers do.
 *
 * The island is one piece of code in both modes, so a field the contract
 * requires and the fixtures omit renders as `undefined` on the demo page while
 * every unit test stays green -- the component tests build their own detail
 * objects, and the live gate next door only watches the rpc layer. This walks
 * the fixtures the demo page actually serves and holds them to the contract's
 * own required lists.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const demo = readFileSync(new URL('../src/demo/154-playbooks.js', import.meta.url), 'utf8')
const contract = JSON.parse(readFileSync(new URL('../../rpc-schema/openrpc.json', import.meta.url), 'utf8'))

function required(schema) {
  const found = contract.components.schemas[schema]
  if (!found) throw new Error(`${schema} is absent from the contract`)
  return found.required
}

function fixtures() {
  const DS = {}
  /* `RavenIslands` is the shell face the rail calls; the fixture source is what
     this gate is after, and it is installed by the same module body. */
  Function('DS', 'RavenIslands', demo)(DS, { playbooks: { open() {}, close() {} } })
  if (!DS.playbooks) throw new Error('DS.playbooks is absent from the demo layer')
  return DS.playbooks
}

describe('the demo playbook fixtures', () => {
  it('answer every field the contract makes required', async () => {
    const source = fixtures()
    const rows = await source.list()
    expect(rows.length).toBeGreaterThan(0)

    const rowKeys = required('PlaybookRow')
    for (const row of rows) {
      for (const key of rowKeys) expect([row.name, key, row[key]]).not.toContain(undefined)
    }

    const detailKeys = required('PlaybookDetail')
    for (const row of rows) {
      const detail = await source.get(row.name)
      for (const key of detailKeys) expect([detail.name, key, detail[key]]).not.toContain(undefined)
    }
  })

  it('answer every field a param is required to carry', async () => {
    const source = fixtures()
    const paramKeys = required('PlaybookParam')
    for (const row of await source.list()) {
      const detail = await source.get(row.name)
      for (const [name, param] of Object.entries(detail.params || {})) {
        for (const key of paramKeys) expect([row.name, name, key, param[key]]).not.toContain(undefined)
      }
    }
  })
})
