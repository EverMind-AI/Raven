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

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/module-harness.mjs'

import type { Sources } from '../../state/sources'

/* `sources.transcript.openDagRun` is one of the verbs the page's wiring puts
   onto the transcript source it builds, so the seam is where it is read back. */
async function opener(calls: unknown[][], run: unknown) {
  /* This feature's source is imported first and the part that wires it after,
     which is the order that keeps one module graph: the fakes are installed
     around the module under test. */
  const wiring = await loadPart(async () => {
    await import('./source')
    return import('../../app/install')
  }, {
    fakes: {
      'src/state/wsPane': {
        pane: () => ({ setOpen: (...args: unknown[]) => calls.push(['fallback', ...args]) }),
      },
      'src/state/sheetRack': {
        session: () => 'a',
      },
      'src/lib/dom': {
        $: looseQuery(),
      },
      'src/lib/session': { current: () => 'a' },
      'src/features/dag/open': {
        dagOpenNode: (runId: string, node: { id: string }) => calls.push(['node', runId, node.id]),
      },
      'src/features/dag/mount': {
        run: (key: string) => (key === 'a' ? run : null),
      },
    },
  })
  const { setSources, sources } = await import('../../state/sources')
  setSources({ transcript: {}, composer: {}, rail: {} } as unknown as Partial<Sources>)
  wiring.installSources()
  if (!sources.transcript!.openDagRun) throw new Error('openDagRun is absent from the page wiring')
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
  const wiring = await loadPart(async () => {
    await import('./source')
    return import('../../app/install')
  }, {
    fakes: {
      'src/state/wsPane': {
        pane: () => ({
          view: () => ({ tab: 'diff', open: false, picked: false }),
          setOpen: (open: boolean, tab?: string) => calls.push(['setWs', open, tab ?? null]),
          pick: (tab: string) => calls.push(['wsPick', tab]),
          draw: () => calls.push(['drawWs']),
        }),
      },
      'src/lib/session': { current: () => 's1' },
      'src/features/rail/title': { plainTitle: (s: unknown) => String(s) },
      'src/features/subagents/store': {
        openDagNode: (run: string, node: { id: string }) => calls.push(['openDagNode', run, node.id]),
        rows: () => rows,
        openRow: (row: SpawnRow) => calls.push(['openRow', row.label]),
        refresh: () => calls.push(['refresh']),
      },
      'src/features/desk/store': {
        openDeskTab: (tab: string) => calls.push(['openDeskTab', tab]),
      },
    },
  })
  await fakeGateway(() => Promise.resolve({}))
  const { setSources, sources } = await import('../../state/sources')
  setSources({ transcript: {}, composer: {} } as unknown as Partial<Sources>)
  wiring.installSources()
  const { dagOpenNode } = await import('../dag/open')
  return { dagOpenNode, sources, calls }
}

afterEach(() => {
  vi.useRealTimers()
  deskReady(false)
})

describe('opening a graph node from the transcript source', () => {
  it('raises the node window alone while the floating desk is up', async () => {
    deskReady(true)
    const { dagOpenNode, calls } = await nodeHarness()

    dagOpenNode('r1', { id: 'brief' })

    expect(calls).toEqual([['openDagNode', 'r1', 'brief']])
    /* Named explicitly, because these are what popped the palette: either one
       reaches `openDeskTab` in desk mode. */
    expect(calls.some(([verb]) => verb === 'wsPick' || verb === 'setWs')).toBe(false)
  })

  it('still selects the panel view where there are no windows', async () => {
    /* The pre-desk panel, where picking the agents view was how the instance
       got on screen at all. */
    deskReady(false)
    const { dagOpenNode, calls } = await nodeHarness()

    dagOpenNode('r1', { id: 'brief' })

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

  it('falls back to the tasks list when no row ever turns up', async () => {
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
    expect(calls[calls.length - 1]).toEqual(['openDeskTab', 'tasks'])
  })

  it('keeps the panel view for a spawn record without the desk', async () => {
    deskReady(false)
    const { sources, calls } = await nodeHarness()

    sources.transcript!.openSpawn!('raven', 'qc')

    expect(calls).toEqual([['setWs', true, 'agents'], ['openRow', 'qc']])
  })
})
