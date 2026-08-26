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
describe('arranging the desk', () => {
  it('reorders the panes and turns the pair, and a drop is what a reload replays', () => {
    desk.openDeskFile('/workspace/a.ts')
    desk.openDeskFile('/workspace/b.ts')

    desk.arrange(['file:/workspace/b.ts', 'file:/workspace/a.ts'], 'cols')

    expect(desk.getState().panes.map((pane) => pane.id))
      .toEqual(['file:/workspace/b.ts', 'file:/workspace/a.ts'])
    expect(desk.getState().duo).toBe('cols')
    const kept = desk.saved('s1')!
    expect(kept.open.map((intent) => (intent as { path: string }).path))
      .toEqual(['/workspace/b.ts', '/workspace/a.ts'])
    expect(kept.duo).toBe('cols')
  })

  it('refuses an order that does not name exactly the panes that are up, each once', () => {
    desk.openDeskFile('/workspace/a.ts')
    desk.openDeskFile('/workspace/b.ts')

    /* Stale by one close, a pane this desk never had, and a duplicate that
       would put one pane object in the list twice. */
    desk.arrange(['file:/workspace/b.ts'], 'cols')
    desk.arrange(['file:/workspace/b.ts', 'file:/workspace/zz.ts'], 'cols')
    desk.arrange(['file:/workspace/b.ts', 'file:/workspace/b.ts'], 'cols')

    expect(desk.getState().panes.map((pane) => pane.id))
      .toEqual(['file:/workspace/a.ts', 'file:/workspace/b.ts'])
    expect(desk.getState().duo).toBe('rows')
  })
})

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

  describe('the palette a conversation is opened with', () => {
    /* The default is the screen, not the last thing the reader did somewhere
       else: a conversation shows the desk, the new-task screen does not. */
    it('opens on a conversation and stays shut on a draft', () => {
      setCurrent(null)
      desk.sync()
      expect(desk.getState().paletteOpen).toBe(false)

      setCurrent('s2')
      desk.sync()
      expect(desk.getState().paletteOpen).toBe(true)
    })

    it('comes back collapsed to the conversation it was collapsed in', () => {
      desk.sync()
      desk.toggleDesk()
      expect(desk.getState().paletteOpen).toBe(false)

      /* Away and back the way the page does it: the desk is torn down on the
         way out and the pointer moves after. */
      desk.reset()
      setCurrent('s2')
      desk.sync()
      expect(desk.getState().paletteOpen).toBe(true)

      desk.reset()
      setCurrent('s1')
      desk.sync()
      expect(desk.getState().paletteOpen).toBe(false)
    })

    /* A reset runs on the way OUT, while `session.resume` is still in flight,
       so it cannot know whose desk is about to be on screen. Closing the
       palette there and opening it again a moment later is a flicker with no
       information in it. */
    it('leaves the palette where it is until the pointer moves', () => {
      desk.sync()
      expect(desk.getState().paletteOpen).toBe(true)

      desk.reset()

      expect(desk.getState().paletteOpen).toBe(true)
    })

    /* The first message turns a draft into a session in place -- same screen,
       same composer -- and the desk the reader had just put away must not open
       in their face. Driven the way the page drives it: the pointer moves to
       the new id and the layer that did it says so (main.tsx binds
       `claimDraft` to the composer's claim and this one together). */
    it('carries a collapse made on the draft into the session it becomes', () => {
      setCurrent(null)
      desk.sync()
      desk.toggleDesk()
      expect(desk.getState().paletteOpen).toBe(true)
      desk.toggleDesk()

      setCurrent('s9')
      desk.sync()
      desk.claimDraft('s9')

      expect(desk.getState().paletteOpen).toBe(false)
    })

    /* `#newBtn` is unconditional, so pressing New task while already on the
       new-task screen runs the reset and then `sessionSet(null)`, which is a
       no-op -- nothing moves. An answer dropped by the reset would be gone with
       the reader still looking at the screen they gave it on, and the desk
       would open on their next message. Reported in review. */
    it('keeps the answer when New task is pressed on the new-task screen', () => {
      setCurrent(null)
      desk.sync()
      desk.toggleDesk()
      desk.toggleDesk()

      desk.reset()
      setCurrent(null)
      desk.sync()
      setCurrent('s9')
      desk.sync()
      desk.claimDraft('s9')

      expect(desk.getState().paletteOpen).toBe(false)
    })

    /* An answer given on the new-task screen is about the new-task screen. The
       reader who opens some existing conversation instead finds that one as
       they left it -- which is only sayable because the transition is
       announced rather than guessed from the pointer. */
    it("does not carry the draft's answer into a conversation it opens instead", () => {
      setCurrent(null)
      desk.sync()
      /* Twice: the draft starts shut, so putting it away deliberately is an
         open and a close -- and a collapse is the answer that would be visible
         if it were carried somewhere it does not belong. */
      desk.toggleDesk()
      desk.toggleDesk()
      expect(desk.getState().paletteOpen).toBe(false)

      desk.reset()
      setCurrent('s9')
      desk.sync()

      expect(desk.getState().paletteOpen).toBe(true)
    })

    /* The answer is about the new-task screen, not about one draft, so the next
       new task keeps it: a reader who opened the desk there does not have to
       open it again on the next one. Opened rather than put away on purpose --
       shut is this screen's default, so a held "shut" and a cleared one are the
       same screen, and only this direction can tell holding from clearing. */
    it('holds the answer across drafts', () => {
      setCurrent(null)
      desk.sync()
      desk.toggleDesk()
      expect(desk.getState().paletteOpen).toBe(true)

      /* Away to a conversation and back to a new task, the long way round. */
      desk.reset()
      setCurrent('a')
      desk.sync()
      desk.reset()
      setCurrent(null)
      desk.sync()

      expect(desk.getState().paletteOpen).toBe(true)
    })

    /* The other half of the same rule, and the one with a conversation in it: an
       answer that outlived the draft that gave it still only reaches a
       conversation started FROM that screen. */
    it('spends an older draft answer on the conversation a later draft becomes', () => {
      setCurrent(null)
      desk.sync()
      desk.toggleDesk()
      desk.toggleDesk()

      desk.reset()
      setCurrent('a')
      desk.sync()
      /* Not this one: the reader opened it, they did not start it here. */
      expect(desk.getState().paletteOpen).toBe(true)

      desk.reset()
      setCurrent(null)
      desk.sync()
      setCurrent('b')
      desk.sync()
      desk.claimDraft('b')

      expect(desk.getState().paletteOpen).toBe(false)
    })

    /* Forking a conversation and opening a cron run both move the pointer to a
       brand new id BEFORE the desk is reset, so an ordering rule does not tell
       them apart from a draft's first message either. Neither is the reader's
       draft becoming a session, and neither says so. */
    it('does not carry it into a new conversation an action opened', () => {
      setCurrent(null)
      desk.sync()
      desk.toggleDesk()
      desk.toggleDesk()

      setCurrent('forked-1')
      desk.sync()
      desk.reset()

      expect(desk.getState().paletteOpen).toBe(true)
    })

    /* Asking for a view of the desk is asking for the desk. */
    it('lets a request for a tab outrank the collapse on file', () => {
      desk.sync()
      desk.toggleDesk()

      desk.openDeskTab('deliverables')
      desk.reset()
      setCurrent('s2')
      desk.sync()
      desk.reset()
      setCurrent('s1')
      desk.sync()

      expect(desk.getState().paletteOpen).toBe(true)
    })
  })

  describe('a fullscreen pane', () => {
    /* The pane IS the window while it is up: there is no column for the desk to
       hang off, and nothing of the desk belongs over it. */
    it('is not something the palette shows over', () => {
      desk.sync()
      desk.openDeskFile('/workspace/a.ts')
      expect(desk.showing()).toBe(true)

      desk.toggleSolo('file:/workspace/a.ts')

      expect(desk.showing()).toBe(false)
      /* The reader did not put the palette away, so it is still open -- and
         comes back as it was when the pane does. */
      expect(desk.getState().paletteOpen).toBe(true)

      desk.toggleSolo('file:/workspace/a.ts')
      expect(desk.showing()).toBe(true)
    })

    it('is not showing a palette the reader shut either', () => {
      desk.sync()
      desk.toggleDesk()
      desk.openDeskFile('/workspace/a.ts')
      desk.toggleSolo('file:/workspace/a.ts')

      expect(desk.showing()).toBe(false)
    })
  })

  it('refuses a split that is not a percentage', () => {
    /* The surface hands these straight to a CSS grid template. */
    desk.openDeskFile('/workspace/a.ts')
    const kept = { ...desk.saved('s1')!, splits: { column: 0, left: NaN, right: 70 } }

    desk.applyLayout(kept)

    expect(desk.getState().splits).toEqual({ column: 50, left: 50, right: 70 })
  })

  it('reads the pair orientation back, and a note from before the field as a stack', () => {
    desk.openDeskFile('/workspace/a.ts')
    desk.openDeskFile('/workspace/b.ts')
    const kept = desk.saved('s1')!

    desk.applyLayout({ ...kept, duo: 'cols' })
    expect(desk.getState().duo).toBe('cols')

    /* A stored note that predates the field, and one carrying a value no
       version ever wrote: both are the stack every desk was until then. */
    const { duo: _omitted, ...before } = kept
    desk.applyLayout(before as typeof kept)
    expect(desk.getState().duo).toBe('rows')
  })
})
