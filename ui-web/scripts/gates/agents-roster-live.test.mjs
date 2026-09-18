// @vitest-environment happy-dom
/* What the desk's agent list is a list OF, asserted on the shipped wiring.
 *
 * The roster seam is one line of wiring with no island behind it, so nothing
 * in the vitest suite reaches it -- which is how a filter on the wrong field
 * survived: `vendored` is true of every agent that ships WITH raven, so the
 * four bundled ones were dropped while disabled rows were kept. */

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart } from '../module-harness.mjs'

/* The seam the page installs, driven against a transport of our own: the
   filter is the whole subject, so the rows it is handed have to be ours. */
async function roster(call) {
  const wiring = await loadPart(() => import('../../src/app/install'))
  await fakeGateway(call)
  const { setSources, sources } = await import('../../src/state/sources')
  /* The two seam objects the chrome builds, which this case does not install. */
  setSources({ composer: {}, transcript: {} })
  wiring.installSources()
  if (!sources.subagents) throw new Error('sources.subagents is absent from the page wiring')
  return sources.subagents.roster
}

const ROWS = [
  { name: 'Raven', kind: 'builtin', enabled: true, vendored: false, group: 'builtin' },
  { name: 'Raven-Code', kind: 'cli', enabled: true, vendored: true, group: 'installed' },
  { name: 'Raven-Research', kind: 'cli', enabled: true, vendored: true, group: 'installed' },
  { name: 'Raven-Oncall', kind: 'cli', enabled: false, vendored: true, group: 'uninstalled' },
  { name: 'claude_code', kind: 'acp', enabled: false, vendored: false, group: 'uninstalled' },
  { name: 'openclaw', kind: 'acp', enabled: true, vendored: false, group: 'installed' },
]

const ask = async () => (await roster(async () => ({ rows: ROWS })))()

describe('the installed agent roster', () => {
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
    const ask2 = await roster(async (method, params) => { asked.push([method, params]); return { rows: [] } })
    await ask2()
    expect(asked).toEqual([['subagents.list', { probe: false }]])
  })
})
