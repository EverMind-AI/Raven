// @vitest-environment happy-dom
/* Opening a graph node raises that node's window and nothing else.
 *
 * What the two openers must NOT do is the point: `wsPick`/`setWs` route to
 * `openDeskTab` in desk mode, whose whole job is to open the little desk, so a
 * node opened from the trail's card or from the sheet popped the palette beside
 * the window the reader had actually asked for.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { fakeRpc, loadPart } from './legacy-part.mjs'

/* The class the shell sets while the floating desk owns the workspace. */
function deskReady(on) {
  document.documentElement.classList.toggle('desk-ready', on)
}

async function harness({ rows = [{ kind: 'spawn', agent: 'raven', label: 'qc' }] } = {}) {
  const calls = []
  const part = await loadPart(() => import('../src/legacy/live/240-external-agents.js'), {
    fakes: {
      'demo/100-workspace.js': {
        wsOpen: false,
        setWs: (open, tab) => calls.push(['setWs', open, tab ?? null]),
        wsPick: (tab) => calls.push(['wsPick', tab]),
        drawWs: () => calls.push(['drawWs']),
      },
      /* The last part of the live manifest queues the first paint; nothing
         here is that paint. */
      'demo/160-boot.js': { bootPage: () => {} },
      'live/050-turn.js': { onEvent: () => {} },
    },
    globals: {
      RavenIslands: {
        subagents: {
          openDagNode: (run, node) => calls.push(['openDagNode', run, node.id]),
          rows: () => rows,
          openRow: (row) => calls.push(['openRow', row.label]),
          refresh: () => calls.push(['refresh']),
        },
        workspace: { openDeskTab: (tab) => calls.push(['openDeskTab', tab]) },
      },
      sessionCurrent: () => 's1',
      plainTitle: (s) => String(s),
    },
  })
  await fakeRpc(() => Promise.resolve({}))
  const { DS } = await import('../src/legacy/seam/000-datasource.js')
  DS.transcript = {}
  part.install()
  return { part, DS, calls }
}

afterEach(() => {
  vi.useRealTimers()
  deskReady(false)
})

describe('opening a graph node from the live layer', () => {
  it('raises the node window alone while the floating desk is up', async () => {
    deskReady(true)
    const { part, calls } = await harness()

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
    const { part, calls } = await harness()

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
    const { DS, calls } = await harness()

    DS.transcript.openSpawn('raven', 'qc')

    expect(calls).toEqual([['openRow', 'qc']])
  })

  it('falls back to the agents list when no row ever turns up', async () => {
    /* A click that opens nothing reads as broken, and `refresh` keeps the drawn
       list on a failed read rather than emptying it -- so a gateway hiccup or a
       label the registry spells differently lands here. The list is somewhere
       to look; the window this MR stops opening a palette beside was never
       raised on this branch. */
    deskReady(true)
    const { DS, calls } = await harness({ rows: [] })
    vi.useFakeTimers()

    DS.transcript.openSpawn('raven', 'qc')
    /* The retries run on a 700ms ladder; four of them exhaust it. */
    await vi.advanceTimersByTimeAsync(700 * 5)

    expect(calls.filter(([verb]) => verb === 'openRow')).toEqual([])
    expect(calls[calls.length - 1]).toEqual(['openDeskTab', 'agents'])
  })

  it('keeps the panel view for a spawn record without the desk', async () => {
    deskReady(false)
    const { DS, calls } = await harness()

    DS.transcript.openSpawn('raven', 'qc')

    expect(calls).toEqual([['setWs', true, 'agents'], ['openRow', 'qc']])
  })
})
