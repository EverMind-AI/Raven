// @vitest-environment happy-dom
/* The transcript source's delegation verbs: opening a graph's task pane and a
 * spawn record.
 *
 * What the spawn opener must NOT do is the point: `wsPick`/`setWs` route to
 * `openDeskTab` in desk mode, whose whole job is to open the little desk, so a
 * spawn record opened from the trail's card popped the palette beside the
 * window the reader had actually asked for.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart } from '../../../scripts/module-harness.mjs'

import type { Sources } from '../../state/sources'

/* `openDagRun` is read straight off the module under test, not through
   `app/install`'s wiring: `installSources()` builds its own `sources.tasks`
   (from `tasks/store.ts`'s `byKey`), which would overwrite whatever `openRun`
   / `one` a case installs here before this function ever ran. */
async function opener(over: {
  openRun?: (runId: string) => boolean
  one?: (kind: string, id: string) => Promise<unknown>
} = {}) {
  const deskCalls: unknown[][] = []
  const wiring = await loadPart(() => import('./source'), {
    fakes: {
      'src/features/desk/store': {
        openDeskTask: (row: { id: string }) => deskCalls.push(['openDeskTask', row.id]),
        openDeskTab: (tab: string) => deskCalls.push(['openDeskTab', tab]),
      },
    },
  })
  const { setSources } = await import('../../state/sources')
  setSources({
    tasks: {
      openRun: over.openRun ?? (() => false),
      one: over.one ?? (async () => null),
    },
  } as unknown as Partial<Sources>)
  return { openDagRun: wiring.openDagRun as (runId: string) => void, deskCalls }
}

describe('the live DAG opener', () => {
  it('answers through the tasks store alone, once openRun hits', async () => {
    const opened: string[] = []
    const { openDagRun, deskCalls } = await opener({ openRun: (id) => { opened.push(id); return true } })

    openDagRun('r1')

    expect(opened).toEqual(['r1'])
    expect(deskCalls).toEqual([])
  })

  it('opens the row a one-shot read finds, once openRun misses', async () => {
    const { openDagRun, deskCalls } = await opener({
      openRun: () => false,
      one: async () => ({ kind: 'dag', id: 'r1' }),
    })

    openDagRun('r1')
    await Promise.resolve()

    expect(deskCalls).toEqual([['openDeskTask', 'r1']])
  })

  /* A branched conversation replays its parent's delivered row: the run it
     names was dispatched by a session whose `tasks.list` this page never
     reads, so `openRun` and the one-shot read both come up empty and the tab
     is where it lands. */
  it('falls back to the tasks tab when the one-shot read finds no row either', async () => {
    const { openDagRun, deskCalls } = await opener({ openRun: () => false, one: async () => null })

    openDagRun('r1')
    await Promise.resolve()

    expect(deskCalls).toEqual([['openDeskTab', 'tasks']])
  })

  it('falls back to the tasks tab when the one-shot read fails', async () => {
    const { openDagRun, deskCalls } = await opener({
      openRun: () => false,
      one: async () => { throw new Error('gone') },
    })

    openDagRun('r1')
    /* One more tick than the row-found case: the rejection has to pass through
       the `.then()` it skips before the `.catch()` after it runs. */
    await Promise.resolve()
    await Promise.resolve()

    expect(deskCalls).toEqual([['openDeskTab', 'tasks']])
  })
})

/* The class the shell sets while the floating desk owns the workspace. */
function deskReady(on: boolean) {
  document.documentElement.classList.toggle('desk-ready', on)
}

interface SpawnRow { kind: string; agent: string; label: string }

interface TaskRowLike { kind: string; id: string }

async function nodeHarness({
  rows = [{ kind: 'spawn', agent: 'raven', label: 'qc' }] as SpawnRow[],
  taskRow = null as TaskRowLike | null,
} = {}) {
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
        rows: () => rows,
        openRow: (row: SpawnRow) => calls.push(['openRow', row.label]),
        refresh: () => calls.push(['refresh']),
      },
      'src/features/tasks/store': {
        byKey: (kind: string, id: string) => (taskRow && taskRow.kind === kind && taskRow.id === id ? taskRow : null),
      },
      'src/features/desk/store': {
        openDeskTab: (tab: string) => calls.push(['openDeskTab', tab]),
        openDeskTask: (row: TaskRowLike) => calls.push(['openDeskTask', row.id]),
      },
    },
  })
  await fakeGateway(() => Promise.resolve({}))
  const { setSources, sources } = await import('../../state/sources')
  setSources({ transcript: {}, composer: {} } as unknown as Partial<Sources>)
  wiring.installSources()
  return { sources, calls }
}

afterEach(() => {
  vi.useRealTimers()
  deskReady(false)
})

describe('opening a spawn record from the transcript source', () => {
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

  it('opens the task pane directly when the node id resolves in the tasks store', async () => {
    deskReady(true)
    const { sources, calls } = await nodeHarness({ taskRow: { kind: 'spawn', id: 'node-9' } })

    sources.transcript!.openSpawn!('raven', 'qc', 'node-9')

    /* Exact, so the fuzzy label match below never runs -- no fallback pane,
       no retry ladder. */
    expect(calls).toEqual([['openDeskTask', 'node-9']])
  })

  it('falls back to the fuzzy match when the node id names no row in the tasks store', async () => {
    deskReady(true)
    const { sources, calls } = await nodeHarness()

    sources.transcript!.openSpawn!('raven', 'qc', 'no-such-node')

    expect(calls).toEqual([['openRow', 'qc']])
  })
})
