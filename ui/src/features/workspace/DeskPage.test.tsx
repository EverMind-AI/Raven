// @vitest-environment happy-dom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as turn from '../composer/turn'
import * as agents from '../subagents/store'
import { DeskApp } from './DeskPage'
import * as deliveries from './deliveries'
import * as desk from './deskStore'
import * as workspace from './store'

import { setCurrent } from '../../shell/session'

import type { Shell } from '../../shell/bridge'
import type { InstanceRow } from '../subagents/types'
import type { WorkspaceSource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const asked: string[] = []
let agentRows: InstanceRow[] = []

const inst = (handle: string, status?: string): InstanceRow =>
  ({ sessionKey: 's1', agent: 'hermes', handle, kind: 'cli', resumable: true, ...(status ? { status } : {}) })

function wire(): void {
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
    workspaceSetOpen: () => {},
  }
  const source: WorkspaceSource = {
    shortPath: (p) => p,
    hostPlatform: () => 'mac',
    canBrowse: true,
    openPath: () => {},
  }
  agentRows = []
  asked.length = 0
  setCurrent('s1')
  window.RavenShell = fakeShell
  window.DS = {
    workspace: source,
    agents: {
      list: async () => [],
      instances: async (key: string) => { asked.push(key); return agentRows },
    },
  }
  window.RavenIslands = { workspace: { openFile: desk.openDeskFile } }
  localStorage.clear()
  document.body.innerHTML = '<div id="split" data-open="true"></div>'
  desk._resetForTests()
  deliveries.restore([])
  turn._resetForTests()
}

beforeEach(wire)

afterEach(() => {
  cleanup()
  act(() => {
    desk._resetForTests()
    deliveries.restore([])
    workspace.restore({ changes: [], urls: [], file: null, turn: 0, unseen: 0, deliveries: [] })
  })
  window.RavenShell = undefined
  window.DS = undefined
  window.RavenIslands = undefined
  setCurrent(null)
  agents.reset()
  turn._resetForTests()
  localStorage.clear()
})

const launcher = (): HTMLElement | null => document.querySelector('.desk-follow-toggle')

/* Advance the desk's own timer and let the answer land. */
async function tick(ms = 9000): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(ms)
    await Promise.resolve()
    await Promise.resolve()
  })
}

/* Who asks for the instance list, and when.
 *
 * Two readings of that list report background work -- the agents tab's bubble
 * and the launcher's own glyph -- and neither is told anything: the list is
 * asked for, never pushed. The rule therefore has to cover the case the
 * launcher exists for, which is the desk being DOWN while a turn spawns
 * something. */
describe('asking for the instance list', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('keeps the count live while the reader is on another tab', async () => {
    render(<DeskApp />)
    await act(async () => { desk.update({ paletteOpen: true, tab: 'diff' }) })
    await tick(0)
    expect(desk.unseen('agents')).toBe(0)

    agentRows = [inst('h1')]
    await tick()

    expect(desk.unseen('agents')).toBe(1)
  })

  it('keeps asking with the desk shut while a turn is running', async () => {
    render(<DeskApp />)
    await act(async () => { desk.update({ paletteOpen: false }) })
    await tick(0)
    const before = asked.length

    await act(async () => { turn.dispatch({ type: 'send' }) })
    agentRows = [inst('h1', 'running')]
    await tick()

    expect(asked.length).toBeGreaterThan(before)
    /* And the launcher has something to say about it, which is the whole
       point of asking while nothing is on screen. */
    expect(desk.working()).toBe(true)
    expect(desk.unseenAll()).toBe(1)
  })

  /* The glyph's way back to off.
   *
   * A run STARTING can only happen under a live turn, but a run ENDING is
   * discovered from the same list -- and by then the turn is idle and the desk
   * is still down, so the first version of `wanted()` stopped asking and the
   * launcher breathed forever. It only settled when the reader opened the desk
   * or sent the next message, which is to say: for exactly as long as they were
   * relying on the glyph instead of the palette. */
  it('keeps asking until the run it is showing has settled', async () => {
    render(<DeskApp />)
    await act(async () => { desk.update({ paletteOpen: false }) })
    await act(async () => { turn.dispatch({ type: 'send' }) })
    agentRows = [inst('h1', 'running')]
    await tick()
    expect(desk.working()).toBe(true)
    expect(launcher()?.hasAttribute('data-working')).toBe(true)

    /* The turn ends first: `spawn` is a blocking call, so the run settling and
       the turn going idle land within one interval of each other. */
    await act(async () => { turn.dispatch({ type: 'idle' }) })
    agentRows = [inst('h1', 'completed')]
    const before = asked.length
    await tick()

    expect(asked.length).toBeGreaterThan(before)
    expect(desk.working()).toBe(false)
    expect(launcher()?.hasAttribute('data-working')).toBe(false)
  })

  it('stops asking once the desk is shut and the turn is over', async () => {
    render(<DeskApp />)
    await act(async () => { desk.update({ paletteOpen: true, tab: 'diff' }) })
    await tick(0)

    await act(async () => { desk.update({ paletteOpen: false }) })
    const before = asked.length
    await tick(30000)

    expect(asked.length).toBe(before)
  })

  it('leaves the agents tab to refresh its own list', async () => {
    render(<DeskApp />)
    await act(async () => { desk.update({ paletteOpen: true, tab: 'agents' }) })
    const before = asked.length
    await tick(30000)

    /* The panel asks for itself while it is the tab on screen; a second
       request from here is the same answer twice. */
    expect(asked.length).toBe(before)
  })
})
