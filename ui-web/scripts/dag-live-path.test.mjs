// @vitest-environment happy-dom
/* The live layer keeps the delivered DAG opener on the island path. */

import { describe, expect, it } from 'vitest'

import { loadPart, looseQuery } from './legacy-part.mjs'

/* `sources.transcript.openDagRun` is installed by live/050-turn.js onto the source
   object live/040-history.js built, so the seam is where it is read back. */
async function opener(calls, run) {
  const part = await loadPart(() => import('../src/legacy/live/050-turn.js'), {
    fakes: {
      'demo/010-kernel.js': { $: looseQuery() },
      'demo/040-state.js': { sheetSession: () => 'a' },
      'demo/100-workspace.js': { setWs: (...args) => calls.push(['fallback', ...args]) },
      'live/240-external-agents.js': {
        dagOpenNode: (runId, node) => calls.push(['node', runId, node.id]),
      },
    },
    globals: {
      RavenIslands: { dag: { run: (key) => (key === 'a' ? run : null) } },
      sessionCurrent: () => 'a',
    },
  })
  const { setSources, sources } = await import('../src/state/sources')
  setSources({ transcript: {}, composer: {}, sessions: {} })
  part.install()
  if (!sources.transcript.openDagRun) throw new Error('openDagRun is absent from the live layer')
  return sources.transcript.openDagRun
}

describe('the live DAG opener', () => {
  it('opens the island run last node and keeps the agents fallback', async () => {
    const calls = []
    const open = await opener(calls, { run_id: 'r1', order: ['first', 'last'] })

    open('r1')
    open('missing')

    expect(calls).toEqual([
      ['node', 'r1', 'last'],
      ['fallback', true, 'agents'],
    ])
  })
})
