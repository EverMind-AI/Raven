/** Tests for floating workspace pane identity and session reset behavior. */

// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import * as desk from './deskStore'
import { _resetForTests as sessionReset, setCurrent } from '../../shell/session'

import type { Shell } from '../../shell/bridge'
import type { InstanceRow } from '../subagents/types'

/* Recorded rather than ignored: the panel the desk lives in is legacy chrome,
   so telling it to open and to shut is the desk's only way to be seen. It was
   a no-op fake, which left both calls cuttable with the suite green. */
const panelCalls: boolean[] = []

function wire(): void {
  panelCalls.length = 0
  const fakeShell: Shell = {
    T: (key) => key,
    confirmAsk: (_title, _body, _label, fn) => fn(),
    showPage: () => {},
    workspaceSetOpen: (open) => { panelCalls.push(open) },
  }
  window.RavenShell = fakeShell
  localStorage.clear()
  sessionStorage.clear()
  sessionReset()
  setCurrent('s1')
  document.body.innerHTML = '<div id="split" data-open="false"></div>'
  desk._resetForTests()
}

const split = (): HTMLElement => document.getElementById('split') as HTMLElement

beforeEach(wire)

afterEach(() => {
  desk._resetForTests()
  sessionReset()
  window.RavenShell = undefined
  localStorage.clear()
  sessionStorage.clear()
})

describe('desk store', () => {
  const agentRow = (handle: string): InstanceRow =>
    ({ sessionKey: 's1', agent: 'hermes', kind: 'cli', handle }) as InstanceRow

  it('re-opening the agent pane that is already up changes nothing', () => {
    /* An agent pane re-finds its row in the live stores on every render, so the
       object held here is never staler -- which is what makes replacing it pure
       churn: a re-tile and a remount to arrive at the screen already showing. */
    desk.openDeskAgent(agentRow('a'))
    desk.openDeskAgent(agentRow('b'))
    split().dataset.open = 'true'
    desk.toggleSolo('agent:hermes:a')
    const panesBefore = desk.getState().panes
    panelCalls.length = 0

    desk.openDeskAgent(agentRow('a'))

    const after = desk.getState()
    expect(after.panes).toBe(panesBefore)
    expect(after.panes[0]).toBe(panesBefore[0])
    expect(after.solo).toBe('agent:hermes:a')
    expect(after.active).toBe('agent:hermes:a')
  })

  it('leaves fullscreen when a DIFFERENT already-open pane is asked for', () => {
    /* `DeskSurface` draws only the soloed pane while solo is set, so a path that
       moves `active` and leaves `solo` alone puts the reader in front of the
       pane they did not ask for, with no way out but the fullscreen toggle. */
    desk.openDeskAgent(agentRow('a'))
    desk.openDeskAgent(agentRow('b'))
    desk.toggleSolo('agent:hermes:a')

    desk.openDeskAgent(agentRow('b'))

    expect(desk.getState().active).toBe('agent:hermes:b')
    expect(desk.getState().solo).toBeNull()
  })

  it('still replaces a file pane, because that is how it re-reads', () => {
    /* `FileView` renders the file object directly. Re-opening is what picks up a
       download path the first caller did not pass, and what makes `FileBody`
       (keyed on `seq`, fetching only while `text` is null) read the file again
       after the agent rewrote it. Keeping the old object silently froze both. */
    desk.openDeskFile('/workspace/a.ts')
    const before = desk.getState().panes[0]

    desk.openDeskFile('/workspace/a.ts', '/dl/a.ts')

    const after = desk.getState().panes[0]!
    expect(after).not.toBe(before)
    expect(after.kind === 'file' && after.file.downloadPath).toBe('/dl/a.ts')
  })

  it('does not re-open a workspace that is already showing', () => {
    split().dataset.open = 'true'
    panelCalls.length = 0
    desk.openDeskFile('/workspace/a.ts')
    /* Opening the workspace runs a full workspace draw; asking for one while it
       is already open rebuilt the legacy panel's islands for nothing. */
    expect(panelCalls).toHaveLength(0)

    split().dataset.open = 'false'
    desk.openDeskFile('/workspace/c.ts')
    expect(panelCalls).toEqual([true])
  })

  it('promotes a graph node record into the pane it already occupies', () => {
    desk.openDeskAgentRecord({ kind: 'dag', run_id: 'r1', node: 'brief', agent: 'raven', label: 'brief' })
    desk.openDeskFile('/workspace/a.ts')
    expect(desk.getState().panes.map((pane) => pane.id))
      .toEqual(['agent-record:r1:brief', 'file:/workspace/a.ts'])

    /* The instance the node ran on is the SAME work reached a second way: it
       takes the record's slot instead of opening a third pane beside it. */
    desk.openDeskAgent({
      sessionKey: 's', agent: 'raven', handle: 'brief-9f', kind: 'dag',
      runId: 'r1', nodeId: 'brief', resumable: true,
    })

    expect(desk.getState().panes.map((pane) => pane.id))
      .toEqual(['agent:raven:brief-9f', 'file:/workspace/a.ts'])
    expect(desk.getState().active).toBe('agent:raven:brief-9f')
  })

  it('promotes a spawn record into the pane it already occupies', () => {
    /* The same promotion for a plain spawn, which the row cannot describe:
       `runId` and `nodeId` name a graph node and a spawn has neither, so the
       record id -- the call id -- has to be handed over by the caller that
       knows it. Derived instead of passed, the composer-less record pane the
       reader is being moved off stays open beside the instance pane. */
    desk.openDeskAgentRecord(
      { kind: 'spawn', id: '20260825T101500Z-ab12cd34', agent: 'hermes', label: 'quick survey' })
    desk.openDeskFile('/workspace/a.ts')
    expect(desk.getState().panes.map((pane) => pane.id))
      .toEqual(['agent-record:20260825T101500Z-ab12cd34', 'file:/workspace/a.ts'])

    desk.openDeskAgent(
      { sessionKey: 's', agent: 'hermes', handle: 'survey-9ab2c6', kind: 'cli', resumable: true },
      '20260825T101500Z-ab12cd34')

    expect(desk.getState().panes.map((pane) => pane.id))
      .toEqual(['agent:hermes:survey-9ab2c6', 'file:/workspace/a.ts'])
    expect(desk.getState().active).toBe('agent:hermes:survey-9ab2c6')
  })

  it('keeps fullscreen through that promotion and drops it for a different pane', () => {
    desk.openDeskAgentRecord({ kind: 'dag', run_id: 'r1', node: 'brief', agent: 'raven', label: 'brief' })
    desk.toggleSolo('agent-record:r1:brief')

    desk.openDeskAgent({
      sessionKey: 's', agent: 'raven', handle: 'brief-9f', kind: 'dag',
      runId: 'r1', nodeId: 'brief', resumable: true,
    })
    expect(desk.getState().solo).toBe('agent:raven:brief-9f')

    desk.openDeskFile('/workspace/a.ts')
    expect(desk.getState().solo).toBeNull()
  })

  /* The desk cannot show itself: the panel it sits in belongs to the legacy
     chrome. Opening the first pane has to raise it, and closing the last one
     has to drop it, or the reader is left with an empty panel standing open. */
  it('raises the panel for the first pane and drops it with the last', () => {
    desk.openDeskFile('/workspace/a.ts')
    expect(panelCalls).toContain(true)

    panelCalls.length = 0
    desk.openDeskFile('/workspace/b.ts')
    desk.closePane('file:/workspace/b.ts')
    expect(panelCalls).not.toContain(false)

    desk.closePane('file:/workspace/a.ts')
    expect(desk.getState().panes).toHaveLength(0)
    expect(panelCalls).toContain(false)
  })

  it('clears session panes without dropping subscribers', () => {
    desk.openDeskFile('/workspace/a.ts')
    let updates = 0
    const unsubscribe = desk.subscribe(() => { updates += 1 })

    desk.reset()

    expect(updates).toBe(1)
    expect(desk.getState().panes).toEqual([])
    desk.openDeskFile('/workspace/b.ts')
    expect(updates).toBe(2)
    unsubscribe()
  })
})

/* A reload replaces the page and takes the desk with it. What is kept is what
   the reader OPENED -- a path, an (agent, handle) -- and where the frame put it;
   resuming replays those opens, so a restored window goes through the same verb
   a clicked one does and reads its own body back from the gateway. */
describe('what a reload finds on the desk', () => {
  it('records the file windows, by path, in the order they were opened', () => {
    desk.openDeskFile('/workspace/a.ts')
    desk.openDeskFile('/workspace/b.ts', '/dl/b.ts')

    expect(desk.saved('s1')!.open).toEqual([
      { k: 'file', path: '/workspace/a.ts' },
      { k: 'file', path: '/workspace/b.ts', dl: '/dl/b.ts' },
    ])
  })

  it('records the file itself nowhere', () => {
    /* The pane reads its own body when it opens. A stored copy would come back
       as a file the agent has since rewritten, which is the failure this whole
       design is shaped to avoid. */
    desk.openDeskFile('/workspace/a.ts')

    expect(sessionStorage.getItem('raven.gui.view.desk')).not.toContain('"seq"')
  })

  it('records an instance by the pair the panel can reopen it from', () => {
    desk.openDeskAgent({
      sessionKey: 's1', agent: 'raven', handle: 'brief-9f', kind: 'dag',
      runId: 'r1', nodeId: 'brief', resumable: true,
    })

    expect(desk.saved('s1')!.open)
      .toEqual([{ k: 'agent', agent: 'raven', handle: 'brief-9f', run: 'r1', node: 'brief' }])
  })

  it('records a graph node record by run and node', () => {
    desk.openDeskAgentRecord({ kind: 'dag', run_id: 'r1', node: 'brief', agent: 'raven', label: 'brief' })

    expect(desk.saved('s1')!.open).toEqual([{ k: 'record', run: 'r1', node: 'brief' }])
  })

  it('records a spawn record by its call id', () => {
    desk.openDeskAgentRecord({ kind: 'spawn', id: 'call-7', agent: 'raven', label: 'research' })

    expect(desk.saved('s1')!.open).toEqual([{ k: 'record', id: 'call-7' }])
  })

  it('records no diff window', () => {
    /* Its hunks are the turn's own live tool events and the gateway cannot
       answer for them afterwards, so there is no open to replay -- and storing
       the hunks would be the one place this kept content instead of a pointer. */
    desk.openDeskDiff({ key: '/workspace/a.ts', turn: 3 } as never)
    desk.openDeskFile('/workspace/a.ts')

    expect(desk.saved('s1')!.open).toEqual([{ k: 'file', path: '/workspace/a.ts' }])
  })

  it('drops a window the reader closed', () => {
    desk.openDeskFile('/workspace/a.ts')
    desk.openDeskFile('/workspace/b.ts')

    desk.closePane('file:/workspace/a.ts')

    expect(desk.saved('s1')!.open).toEqual([{ k: 'file', path: '/workspace/b.ts' }])
  })

  it('keeps the record through the session switch that clears the desk', () => {
    /* `reset` runs on every session switch, and it runs BEFORE the session
       pointer moves -- so a record written from the teardown would erase the
       desk of the conversation being left, at the moment of leaving it. */
    desk.openDeskFile('/workspace/a.ts')

    desk.reset()

    expect(desk.getState().panes).toEqual([])
    expect(desk.saved('s1')!.open).toEqual([{ k: 'file', path: '/workspace/a.ts' }])
  })

  /* The note is `sessionStorage`, so it outlives the bundle that wrote it: the
     reader's tab is replaced by a new build and the old note is still there.
     One of the tabs it could name is retired, and `applyLayout` is the one door
     into `state.tab` that does not go through `openDeskTab`'s guard. */
  it('ignores a note from a bundle whose tabs were different', () => {
    sessionStorage.setItem('raven.gui.view.desk', JSON.stringify({
      v: 1,
      s: {
        s1: {
          at: Date.now(),
          d: { tab: 'file', open: [], solo: null, active: null, splits: { column: 50, left: 50, right: 50 } },
        },
      },
    }))

    expect(desk.saved('s1')).toBeNull()
  })

  it('keeps the tab it has when a note names one it does not', () => {
    desk.openDeskTab('deliverables')

    desk.applyLayout({
      tab: 'file' as 'diff',
      open: [],
      solo: null,
      active: null,
      splits: { column: 50, left: 50, right: 50 },
    })

    expect(desk.getState().tab).toBe('deliverables')
  })

  it('records nothing for a draft', () => {
    /* A draft has no id to file under and cannot be reopened; a record for it
       would be a layout with no way back. */
    setCurrent(null)

    desk.openDeskFile('/workspace/a.ts')

    expect(sessionStorage.getItem('raven.gui.view.desk')).toBeNull()
  })

  it('puts the front pane and the fullscreen back', () => {
    desk.openDeskFile('/workspace/a.ts')
    desk.openDeskFile('/workspace/b.ts')
    const kept = { ...desk.saved('s1')!, active: 'file:/workspace/a.ts', solo: 'file:/workspace/a.ts' }

    desk.applyLayout(kept)

    expect(desk.getState().active).toBe('file:/workspace/a.ts')
    expect(desk.getState().solo).toBe('file:/workspace/a.ts')
  })

  it('refuses a fullscreen on a pane that did not come back', () => {
    /* `DeskSurface` draws the soloed pane and only it, so a solo naming a
       window whose open could not be replayed is a blank desk with no way out. */
    desk.openDeskFile('/workspace/a.ts')
    const kept = { ...desk.saved('s1')!, active: 'file:/gone.ts', solo: 'file:/gone.ts' }

    desk.applyLayout(kept)

    expect(desk.getState().solo).toBeNull()
    expect(desk.getState().active).toBe('file:/workspace/a.ts')
  })

  it('refuses a split that is not a percentage', () => {
    /* The surface hands these straight to a CSS grid template. */
    desk.openDeskFile('/workspace/a.ts')
    const kept = { ...desk.saved('s1')!, splits: { column: 0, left: NaN, right: 70 } }

    desk.applyLayout(kept)

    expect(desk.getState().splits).toEqual({ column: 50, left: 50, right: 70 })
  })
})
