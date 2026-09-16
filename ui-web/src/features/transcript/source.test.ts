// @vitest-environment happy-dom
/* The transcript source's delegation verbs: opening a graph, a graph node and
 * a spawn record.
 *
 * What the two openers must NOT do is the point: `wsPick`/`setWs` route to
 * `openDeskTab` in desk mode, whose whole job is to open the little desk, so a
 * node opened from the trail's card or from the sheet popped the palette beside
 * the window the reader had actually asked for.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/legacy-part.mjs'

import type { Sources } from '../../state/sources'

/* `sources.transcript.openDagRun` is installed by live/050-turn.js onto the source
   object live/040-history.js built, so the seam is where it is read back. */
async function opener(calls: unknown[][], run: unknown) {
  const part = await loadPart(() => import('../../legacy/live/050-turn.js'), {
    fakes: {
      'src/shell/session': { current: () => 'a' },
      'demo/010-kernel.js': { $: looseQuery() },
      'demo/040-state.js': { sheetSession: () => 'a' },
      'demo/100-workspace.js': { setWs: (...args: unknown[]) => calls.push(['fallback', ...args]) },
      'live/240-external-agents.js': {
        dagOpenNode: (runId: string, node: { id: string }) => calls.push(['node', runId, node.id]),
      },
    },
    islands: { dag: { run: (key: string) => (key === 'a' ? run : null) } },
  })
  const { setSources, sources } = await import('../../state/sources')
  setSources({ transcript: {}, composer: {}, sessions: {} } as unknown as Partial<Sources>)
  part.install()
  if (!sources.transcript!.openDagRun) throw new Error('openDagRun is absent from the live layer')
  return sources.transcript!.openDagRun
}

describe('the live DAG opener', () => {
  it('opens the island run last node and keeps the agents fallback', async () => {
    const calls: unknown[][] = []
    const open = await opener(calls, { run_id: 'r1', order: ['first', 'last'] })

    open('r1')
    open('missing')

    expect(calls).toEqual([
      ['node', 'r1', 'last'],
      ['fallback', true, 'agents'],
    ])
  })
})

/* The class the shell sets while the floating desk owns the workspace. */
function deskReady(on: boolean) {
  document.documentElement.classList.toggle('desk-ready', on)
}

interface SpawnRow { kind: string; agent: string; label: string }

async function nodeHarness({ rows = [{ kind: 'spawn', agent: 'raven', label: 'qc' }] as SpawnRow[] } = {}) {
  const calls: unknown[][] = []
  const part = await loadPart(() => import('../../legacy/live/240-external-agents.js'), {
    fakes: {
      'src/shell/session': { current: () => 's1' },
      'src/features/rail/title': { plainTitle: (s: unknown) => String(s) },
      'demo/100-workspace.js': {
        wsOpen: false,
        setWs: (open: boolean, tab?: string) => calls.push(['setWs', open, tab ?? null]),
        wsPick: (tab: string) => calls.push(['wsPick', tab]),
        drawWs: () => calls.push(['drawWs']),
      },
      /* The last part of the live manifest queues the first paint; nothing
         here is that paint. */
      'demo/160-boot.js': { bootPage: () => {} },
      'live/050-turn.js': { onEvent: () => {} },
    },
    islands: {
      subagents: {
        openDagNode: (run: string, node: { id: string }) => calls.push(['openDagNode', run, node.id]),
        rows: () => rows,
        openRow: (row: SpawnRow) => calls.push(['openRow', row.label]),
        refresh: () => calls.push(['refresh']),
      },
      workspace: { openDeskTab: (tab: string) => calls.push(['openDeskTab', tab]) },
    },
  })
  await fakeGateway(() => Promise.resolve({}))
  const { setSources, sources } = await import('../../state/sources')
  setSources({ transcript: {} } as unknown as Partial<Sources>)
  part.install()
  return { part, sources, calls }
}

afterEach(() => {
  vi.useRealTimers()
  deskReady(false)
})

describe('opening a graph node from the live layer', () => {
  it('raises the node window alone while the floating desk is up', async () => {
    deskReady(true)
    const { part, calls } = await nodeHarness()

    part.dagOpenNode('r1', { id: 'brief' })

    expect(calls).toEqual([['openDagNode', 'r1', 'brief']])
    /* Named explicitly, because these are what popped the palette: either one
       reaches `openDeskTab` in desk mode. */
    expect(calls.some(([verb]) => verb === 'wsPick' || verb === 'setWs')).toBe(false)
  })

  it('still selects the panel view where there are no windows', async () => {
    /* The pre-desk panel, where picking the agents view was how the instance
       got on screen at all. */
    deskReady(false)
    const { part, calls } = await nodeHarness()

    part.dagOpenNode('r1', { id: 'brief' })

    expect(calls).toEqual([
      ['openDagNode', 'r1', 'brief'],
      ['setWs', true, null],
      ['wsPick', 'agents'],
      ['drawWs'],
    ])
  })

  it('opens a spawn record without the palette either', async () => {
    deskReady(true)
    const { sources, calls } = await nodeHarness()

    sources.transcript!.openSpawn!('raven', 'qc')

    expect(calls).toEqual([['openRow', 'qc']])
  })

  it('falls back to the agents list when no row ever turns up', async () => {
    /* A click that opens nothing reads as broken, and `refresh` keeps the drawn
       list on a failed read rather than emptying it -- so a gateway hiccup or a
       label the registry spells differently lands here. The list is somewhere
       to look; the window this MR stops opening a palette beside was never
       raised on this branch. */
    deskReady(true)
    const { sources, calls } = await nodeHarness({ rows: [] })
    vi.useFakeTimers()

    sources.transcript!.openSpawn!('raven', 'qc')
    /* The retries run on a 700ms ladder; four of them exhaust it. */
    await vi.advanceTimersByTimeAsync(700 * 5)

    expect(calls.filter(([verb]) => verb === 'openRow')).toEqual([])
    expect(calls[calls.length - 1]).toEqual(['openDeskTab', 'agents'])
  })

  it('keeps the panel view for a spawn record without the desk', async () => {
    deskReady(false)
    const { sources, calls } = await nodeHarness()

    sources.transcript!.openSpawn!('raven', 'qc')

    expect(calls).toEqual([['setWs', true, 'agents'], ['openRow', 'qc']])
  })
})
