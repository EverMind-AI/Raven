// @vitest-environment happy-dom
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
/* Off cwd, not off `import.meta.url`: under happy-dom that is an http URL. */
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { loadPart } from './legacy-part.mjs'

const contract = JSON.parse(readFileSync(resolve(process.cwd(), '../rpc-schema/openrpc.json'), 'utf8'))

function required(schema) {
  const found = contract.components.schemas[schema]
  if (!found) throw new Error(`${schema} is absent from the contract`)
  return found.required
}

/* The fixture source is what this gate is after, and the part's install() is
   what registers it -- onto the seam, which is where the rail reads it. */
async function fixtures() {
  const part = await loadPart(() => import('../src/legacy/demo/154-playbooks.js'), {
    islands: { playbooks: { open() {}, close() {} } },
  })
  const { sources } = await import('../src/state/sources')
  part.install()
  if (!sources.playbooks) throw new Error('sources.playbooks is absent from the demo layer')
  return sources.playbooks
}

describe('the demo playbook fixtures', () => {
  it('answer every field the contract makes required', async () => {
    const source = await fixtures()
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
    const source = await fixtures()
    const paramKeys = required('PlaybookParam')
    for (const row of await source.list()) {
      const detail = await source.get(row.name)
      for (const [name, param] of Object.entries(detail.params || {})) {
        for (const key of paramKeys) expect([row.name, name, key, param[key]]).not.toContain(undefined)
      }
    }
  })
})
