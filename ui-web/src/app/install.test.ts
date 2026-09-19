// @vitest-environment happy-dom
/* The page's wiring, for the two things import-direction's ratchet pushed
 * onto this seam rather than onto a direct import: the tasks domain's
 * openByNode and its five live-event consumers (state/session/stages.ts used
 * to import features/tasks/store.ts directly for these), and the session
 * pointer's own reload (features/tasks/store.ts's writes are guarded by the
 * session key, so something has to ask again once it changes).
 */

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../scripts/module-harness.mjs'

import type { TaskRow } from '../features/tasks/types'
import type { Sources } from '../state/sources'

type Install = typeof import('./install')
type Session = typeof import('../lib/session')

async function harness(fakes: Record<string, Record<string, unknown>> = {}) {
  const wiring = await loadPart(() => import('./install'), {
    fakes: { 'src/lib/dom': { $: looseQuery() }, ...fakes },
  }) as Install
  await fakeGateway(() => Promise.resolve({}))
  return wiring
}

describe('the tasks seam installSources grows', () => {
  it('carries openByNode and the five live consumers as functions', async () => {
    const wiring = await harness()
    const { setSources, sources } = await import('../state/sources')
    setSources({ composer: { slash: [] } } as unknown as Partial<Sources>)

    wiring.installSources()

    const tasks = sources.tasks as Sources['tasks']
    expect(typeof tasks.openByNode).toBe('function')
    expect(typeof tasks.onRunStarted).toBe('function')
    expect(typeof tasks.onNodeUpdated).toBe('function')
    expect(typeof tasks.onRunCompleted).toBe('function')
    expect(typeof tasks.onRunReplanned).toBe('function')
    expect(typeof tasks.onSubagentStatus).toBe('function')
  })

  it('opens the desk task a spawn node id resolves to, and answers false otherwise', async () => {
    const opened: TaskRow[] = []
    const wiring = await harness({
      'src/features/desk/store': {
        openDeskTab: () => {},
        openDeskTask: (r: TaskRow) => opened.push(r),
      },
    })
    const { setSources, sources } = await import('../state/sources')
    setSources({ composer: { slash: [] } } as unknown as Partial<Sources>)
    const row = { kind: 'spawn', id: 'node-9' } as TaskRow

    wiring.installSources()
    const { set: setTasks } = await import('../features/tasks/store')
    setTasks({ rows: [row], loaded: true, nodes: {}, hover: null, tabByPane: {}, nodeVersions: {}, folds: {} })

    const found = sources.tasks!.openByNode!('node-9')
    const missing = sources.tasks!.openByNode!('no-such-node')

    expect(found).toBe(true)
    expect(missing).toBe(false)
    expect(opened).toEqual([row])
  })
})

describe('the session-pointer reload (installActions)', () => {
  it('asks the seam for the conversation arrived at, not only when nothing has loaded yet', async () => {
    const wiring = await harness()
    const { setSources } = await import('../state/sources')
    const asked: Array<string | null> = []
    setSources({
      composer: { slash: [] },
      tasks: {
        list: async (key: string) => { asked.push(key); return [] },
        one: async () => null,
        stop: async () => false,
        node: async () => ({ dispatch: null, steps: [], answer: null, outputTruncated: false }),
        roster: async () => [],
      },
    } as unknown as Partial<Sources>)

    wiring.installActions()
    const { setCurrent, _resetForTests } = await import('../lib/session') as Session
    setCurrent('s1')
    /* refresh() awaits src.list before patching; give its promise a turn. */
    await Promise.resolve()
    await Promise.resolve()

    expect(asked).toContain('s1')
    _resetForTests()
  })
})
