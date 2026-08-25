/* What the desk's agent list is a list OF, asserted on the shipped live layer.
 *
 * The roster seam is three lines of wiring with no island behind it, so nothing
 * in the vitest suite reaches it -- which is how a filter on the wrong field
 * survived: `vendored` is true of every agent that ships WITH raven, so the
 * four bundled ones were dropped while disabled rows were kept. */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const build = readFileSync(new URL('../build.py', import.meta.url), 'utf8')
const manifest = build.match(/_LIVE_PARTS = \[(.*?)\n\]/s)
if (!manifest) throw new Error('_LIVE_PARTS is absent from build.py')
const parts = [...manifest[1].matchAll(/"([^"]+\.js)"/g)].map((m) => m[1])
const live = parts
  .map((name) => readFileSync(new URL(`../src/live/${name}`, import.meta.url), 'utf8'))
  .join('')

/* The property's own expression, lifted out of the object literal it sits in:
   evaluating the whole assignment would need every other seam's dependencies. */
function rosterSeam() {
  const mark = "roster: () => rpc.call('subagents.list'"
  const start = live.indexOf(mark)
  if (start < 0) throw new Error('the roster seam is absent from the assembled live layer')
  const end = live.indexOf('\n', live.indexOf('),', start))
  const expression = live.slice(start + 'roster: '.length, end).replace(/,\s*$/, '')
  return Function('rpc', `return (${expression});`)
}

const ROWS = [
  { name: 'Raven', kind: 'builtin', enabled: true, vendored: false, group: 'builtin' },
  { name: 'Raven-Code', kind: 'cli', enabled: true, vendored: true, group: 'installed' },
  { name: 'Raven-Research', kind: 'cli', enabled: true, vendored: true, group: 'installed' },
  { name: 'Raven-Oncall', kind: 'cli', enabled: false, vendored: true, group: 'uninstalled' },
  { name: 'claude_code', kind: 'acp', enabled: false, vendored: false, group: 'uninstalled' },
  { name: 'openclaw', kind: 'acp', enabled: true, vendored: false, group: 'installed' },
]

const ask = async () => {
  const rpc = { call: async () => ({ rows: ROWS }) }
  return rosterSeam()(rpc)()
}

describe('the assembled live agent roster', () => {
  it('lists every agent that is registered and available', async () => {
    /* Bundled and third-party alike: where an agent came from is not a reason
       to hide one the user can dispatch to right now. */
    expect((await ask()).map((row) => row.name))
      .toEqual(['Raven', 'Raven-Code', 'Raven-Research', 'openclaw'])
  })

  it('lists no agent that is switched off', async () => {
    /* Listing one advertises work it cannot take: a spawn against a disabled
       agent is refused outright (`_require_addressable`). */
    const names = (await ask()).map((row) => row.name)
    expect(names).not.toContain('claude_code')
    expect(names).not.toContain('Raven-Oncall')
  })

  it('does not ask the server to probe', async () => {
    /* A probe per row is a process launch per row, on a list that redraws on a
       poll. The panel wants names, not reachability. */
    const asked = []
    const rpc = { call: async (method, params) => { asked.push([method, params]); return { rows: [] } } }
    await rosterSeam()(rpc)()
    expect(asked).toEqual([['subagents.list', { probe: false }]])
  })
})
