// @vitest-environment happy-dom
/* The transcript source's delegation verbs: opening a graph's or a spawn's
 * task pane. Both land in the same pane, and neither opens the agents panel's
 * instance window.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { loadPart } from '../../../scripts/module-harness.mjs'

import type { Sources } from '../../state/sources'

/* `openDagRun` is read straight off the module under test, not through
   `app/install`'s wiring: `installSources()` builds its own `sources.tasks`
   (from `tasks/store.ts`'s `byKey`), which would overwrite whatever `openRun`
   / `one` a case installs here before this function ever ran. */
async function opener(over: {
  openRun?: (runId: string) => boolean
  openByNode?: (nodeId: string) => boolean
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
      openByNode: over.openByNode ?? (() => false),
      one: over.one ?? (async () => null),
    },
  } as unknown as Partial<Sources>)
  return {
    openDagRun: wiring.openDagRun as (runId: string) => void,
    openSpawn: wiring.openSpawn as (nodeId: string) => void,
    deskCalls,
  }
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

afterEach(() => {
  vi.useRealTimers()
})

describe('the spawn opener', () => {
  it('opens the task the tasks store already holds, and nothing else', async () => {
    const asked: string[] = []
    const { openSpawn, deskCalls } = await opener({ openByNode: (id) => { asked.push(id); return true } })

    openSpawn('read_dir')

    expect(asked).toEqual(['read_dir'])
    expect(deskCalls).toEqual([])
  })

  /* The click the card takes right after dispatch: the pending frame filed the
     row under the task id, so the store has nothing under the record id yet.
     This is where the label match used to open the instance window instead. */
  it('reads the one row from the server when the store has not caught up', async () => {
    const read: unknown[][] = []
    const { openSpawn, deskCalls } = await opener({
      one: async (kind, id) => { read.push([kind, id]); return { kind: 'spawn', id } },
    })

    openSpawn('read_dir')
    await Promise.resolve()
    await Promise.resolve()

    expect(read).toEqual([['spawn', 'read_dir']])
    expect(deskCalls).toEqual([['openDeskTask', 'read_dir']])
  })

  it('keeps asking on a short ladder while the record is still being written', async () => {
    vi.useFakeTimers()
    let reads = 0
    const { openSpawn, deskCalls } = await opener({
      one: async (_kind, id) => { reads += 1; return reads < 3 ? null : { kind: 'spawn', id } },
    })

    openSpawn('read_dir')
    await vi.advanceTimersByTimeAsync(700 * 3)

    expect(reads).toBe(3)
    expect(deskCalls).toEqual([['openDeskTask', 'read_dir']])
  })

  it('also takes the row the store gains between two reads', async () => {
    /* The running frame renames the pending row to the record id; the next rung
       finds it locally and asks the server nothing more. */
    vi.useFakeTimers()
    let renamed = false
    let reads = 0
    const { openSpawn, deskCalls } = await opener({
      openByNode: () => renamed,
      one: async () => { reads += 1; renamed = true; return null },
    })

    openSpawn('read_dir')
    await vi.advanceTimersByTimeAsync(700 * 5)

    expect(reads).toBe(1)
    expect(deskCalls).toEqual([])
  })

  it('lands on the tasks list when no row ever turns up', async () => {
    vi.useFakeTimers()
    const { openSpawn, deskCalls } = await opener()

    openSpawn('read_dir')
    await vi.advanceTimersByTimeAsync(700 * 6)

    expect(deskCalls).toEqual([['openDeskTab', 'tasks']])
  })

  it('lands on the tasks list when the read fails', async () => {
    const { openSpawn, deskCalls } = await opener({ one: async () => { throw new Error('gone') } })

    openSpawn('read_dir')
    await Promise.resolve()
    await Promise.resolve()

    expect(deskCalls).toEqual([['openDeskTab', 'tasks']])
  })

  it('goes straight to the tasks list for a call that named no record', async () => {
    const { openSpawn, deskCalls } = await opener()

    openSpawn('')

    expect(deskCalls).toEqual([['openDeskTab', 'tasks']])
  })
})
