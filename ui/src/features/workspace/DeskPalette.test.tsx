// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DeskPalette } from './DeskPalette'
import * as agents from '../subagents/store'
import * as deliveries from './deliveries'
import * as desk from './deskStore'
import * as workspace from './store'

import { setCurrent } from '../../shell/session'

import type { Shell } from '../../shell/bridge'
import type { InstanceRow } from '../subagents/types'
import type { WorkspaceSource, WsChange } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const opens: string[] = []
const asked: string[] = []
let agentRows: InstanceRow[] = []

/* Which tab's bubble, by the tab's own label -- the strip is three buttons and
   an index would silently follow a reordering. */
const tabButton = (tab: 'diff' | 'deliverables' | 'agents'): HTMLElement | undefined => {
  const label = tab === 'diff' ? 'Diff' : tab === 'deliverables' ? 'gui.ws.deliverables' : 'gui.ws.agents'
  return [...document.querySelectorAll<HTMLElement>('.desk-tabs button')]
    .find((b) => (b.querySelector('.lb')?.textContent || '') === label)
}

const bubble = (tab: 'diff' | 'deliverables' | 'agents'): string | null =>
  tabButton(tab)?.querySelector('.desk-count')?.textContent ?? null

/* What a screen reader is handed for that tab. */
const spoken = (tab: 'diff' | 'deliverables' | 'agents'): string | null =>
  tabButton(tab)?.getAttribute('aria-label') ?? null

const change = (key: string): WsChange => ({
  key, dir: '', name: key.split('/').pop() || key, kind: 'edit', add: 1, del: 0, hunks: [], turn: 1, open: false,
})

function wire(): void {
  opens.length = 0
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
    openPath: (p) => opens.push(p),
  }
  agentRows = []
  asked.length = 0
  /* The agents store files its lists under the open conversation and drops an
     answer for any other, so the harness has to be in one. */
  setCurrent('s1')
  window.RavenShell = fakeShell
  window.DS = {
    workspace: source,
    /* The agents tab counts the instances this session started, so the harness
       answers for them the way the live source does. */
    agents: {
      list: async () => [],
      instances: async (key: string) => { asked.push(key); return agentRows },
    },
  }
  /* The island bag, as main.tsx installs it: `openDelivery` reaches the desk
     through it, and a harness without it would exercise the legacy panel
     path instead of the one production takes. */
  window.RavenIslands = { workspace: { openFile: desk.openDeskFile } }
  localStorage.clear()
  document.body.innerHTML = '<div id="split" data-open="true"></div>'
  desk._resetForTests()
  deliveries.restore([])
}

const manifest = (files: Array<Record<string, unknown>>): unknown => ({ raven_delivery: { files } })

async function shelf() {
  const view = render(<DeskPalette />)
  await act(async () => {
    desk.update({ paletteOpen: true, tab: 'deliverables' })
  })
  return view
}

const rowNames = (): string[] =>
  [...document.querySelectorAll('.desk-dlv-row .desk-name b')].map((n) => n.textContent || '')

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
  localStorage.clear()
})

describe('the desk shelf', () => {
  it('says the session has delivered nothing rather than showing an empty list', async () => {
    await shelf()
    expect(screen.getByText('gui.ws.dlv_none')).toBeTruthy()
    expect(document.querySelector('.desk-dlv-row')).toBeNull()
    /* Nothing on the shelf is not worth a number on the tab. */
    expect(document.querySelector('.desk-count')).toBeNull()
  })

  it('lists the whole session newest turn first, under a group per turn', async () => {
    workspace.restore({ changes: [], urls: [], file: null, turn: 2, unseen: 0, deliveries: [] })
    deliveries.record(1, manifest([{ path: '/w/old.md', name: 'old.md', title: 'Older brief' }]))
    deliveries.record(2, manifest([
      { path: '/w/a.md', name: 'a.md', title: 'Comparison', size: 1824 },
      { path: '/w/b.csv', name: 'b.csv', title: 'Pricing' },
    ]))
    await shelf()

    expect(rowNames()).toEqual(['Comparison', 'Pricing', 'Older brief'])
    expect([...document.querySelectorAll('.desk-grp')].map((n) => n.textContent))
      .toEqual(['gui.ws.turn_now', 'gui.ws.turn_earlier'])
    /* No bubble on the tab the reader is looking at: whatever is in it has
       been seen by definition. */
    expect(document.querySelector('.desk-tabs button[aria-selected="true"] .desk-count')).toBeNull()
    /* This turn's row names the file and its size; an older one says which turn
       it came from, because "this turn" is the only turn the reader is in. */
    const meta = [...document.querySelectorAll('.desk-dlv-row .desk-name s')].map((n) => n.textContent)
    expect(meta[0]).toBe('a.md · 1.8 KB')
    expect(meta[2]).toBe('old.md · gui.ws.dlv_turn {"n":"1"}')
  })

  it('opens the clicked row as a pane, carrying its download path', async () => {
    deliveries.record(1, manifest([
      { path: '/w/a.md', name: 'a.md', title: 'Comparison', download_path: '/files/download?token=t1' },
    ]))
    await shelf()
    await act(async () => {
      (document.querySelector('.desk-dlv-row') as HTMLElement).click()
    })

    const panes = desk.getState().panes
    expect(panes.map((pane) => pane.id)).toEqual(['file:/w/a.md'])
    expect(panes[0]?.kind === 'file' && panes[0].file.downloadPath).toBe('/files/download?token=t1')
  })

  /* The demo shell's source cannot read a file, and a viewer with nothing to
     show is worse than the note it answers with. */
  it('hands the path to a source that cannot read files, opening no pane', async () => {
    ;(window.DS!.workspace as WorkspaceSource).canBrowse = false
    deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md', title: 'Comparison' }]))
    await shelf()
    await act(async () => {
      (document.querySelector('.desk-dlv-row') as HTMLElement).click()
    })

    expect(opens).toEqual(['/w/a.md'])
    expect(desk.getState().panes).toEqual([])
  })

  it('marks a file that is gone, and still lets it be opened to say so', async () => {
    deliveries.record(1, manifest([{ path: '/w/gone.md', name: 'gone.md', missing: true }]))
    await shelf()

    const row = document.querySelector('.desk-dlv-row') as HTMLButtonElement
    expect(row.classList.contains('gone')).toBe(true)
    expect(row.querySelector('.dlv-gone')?.textContent).toBe('gui.arts.missing')
    expect(row.disabled).toBe(false)
  })

  it('keeps one row for a re-delivered file, at its newest turn', async () => {
    workspace.restore({ changes: [], urls: [], file: null, turn: 3, unseen: 0, deliveries: [] })
    deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md', title: 'First cut' }]))
    deliveries.record(3, manifest([{ path: '/w/a.md', name: 'a.md', title: 'Second cut' }]))
    await shelf()

    expect(rowNames()).toEqual(['Second cut'])
    expect([...document.querySelectorAll('.desk-grp')].map((n) => n.textContent))
      .toEqual(['gui.ws.turn_now'])
  })

  /* The case the bubble exists for: the reader is somewhere else when the file
     lands. Nothing else repaints the palette on a turn that delivered a file
     and changed none. */
  it('shows what is new while another tab is the one on screen', async () => {
    render(<DeskPalette />)
    await act(async () => {
      desk.update({ paletteOpen: true, tab: 'diff' })
    })
    expect(document.querySelector('.desk-count')).toBeNull()

    await act(async () => {
      deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md' }]))
    })

    expect(bubble('deliverables')).toBe('1')
    /* And it is in the name the button already had, because an explicit
       aria-label replaces the subtree: a label on the bubble is never read. */
    expect(spoken('deliverables')).toBe('gui.ws.deliverables, gui.ws.unseen_tab {"n":"1"}')
    expect(document.querySelector('.desk-count')?.getAttribute('aria-hidden')).toBe('true')
    expect(spoken('agents')).toBe('gui.ws.agents')
    expect(document.querySelector('.desk-dlv-row')).toBeNull()
  })

  /* One rule for all three tabs, which is the point of it: the reader on any
     one of them has the same question about the other two. */
  it('counts what is new in the tabs the reader is not on, and drops it when they look', async () => {
    render(<DeskPalette />)
    await act(async () => {
      desk.update({ paletteOpen: true, tab: 'diff' })
    })

    await act(async () => {
      deliveries.record(1, manifest([
        { path: '/w/a.md', name: 'a.md' },
        { path: '/w/b.md', name: 'b.md' },
      ]))
      agentRows = [
        { sessionKey: 's1', agent: 'hermes', handle: 'h1', kind: 'cli', resumable: true },
        { sessionKey: 's1', agent: 'hermes', handle: 'h2', kind: 'cli', resumable: true },
        { sessionKey: 's1', agent: 'hermes', handle: 'h3', kind: 'cli', resumable: true },
      ]
      await agents.refreshInstances(true)
    })

    expect(bubble('deliverables')).toBe('2')
    expect(bubble('agents')).toBe('3')
    /* The label is what the stylesheet hides on a collapsed tab, and it hides
       it by this class -- the bubble being a sibling is what broke the
       positional rule it used to use. */
    expect([...document.querySelectorAll('.desk-tabs button .lb')].map((n) => n.textContent))
      .toEqual(['Diff', 'gui.ws.deliverables', 'gui.ws.agents'])

    await act(async () => {
      desk.update({ tab: 'deliverables' })
    })
    expect(bubble('deliverables')).toBeNull()
    /* Only the tab that was looked at. */
    expect(bubble('agents')).toBe('3')
  })

  it('counts a change written while the reader is on another tab', async () => {
    render(<DeskPalette />)
    await act(async () => {
      desk.update({ paletteOpen: true, tab: 'deliverables' })
    })

    await act(async () => {
      workspace.shared().changes.push(change('/w/one.py'), change('/w/two.py'))
      desk.notifyDesk()
    })

    expect(bubble('diff')).toBe('2')
  })

  /* The mark catches up in an effect, so between something landing and that
     effect running the open tab would otherwise paint a bubble for what is
     already on screen. */
  it('reports nothing new for the tab that is open, before any mark moves', async () => {
    deliveries.record(1, manifest([
      { path: '/w/a.md', name: 'a.md' },
      { path: '/w/b.md', name: 'b.md' },
    ]))
    /* Straight at the store, with the marks still at zero: this is the state
       the first render of a freshly opened palette sees. */
    desk.update({ paletteOpen: true, tab: 'deliverables' })

    expect(desk.unseen('deliverables')).toBe(0)
    expect(desk.unseen('diff')).toBe(0)

    desk.update({ tab: 'diff' })
    expect(desk.unseen('deliverables')).toBe(2)
  })

  /* Nothing tells the desk that a turn spawned a sub-agent: the instance list
     is asked for, and while the reader is on another tab nobody was asking. */
  it('keeps the agents count live while the reader is on another tab', async () => {
    vi.useFakeTimers()
    try {
      render(<DeskPalette />)
      await act(async () => {
        desk.update({ paletteOpen: true, tab: 'diff' })
      })
      /* The open itself asks once, so what the reader had at that moment is
         what "new" is measured from. */
      await act(async () => { await Promise.resolve() })
      expect(bubble('agents')).toBeNull()

      agentRows = [{ sessionKey: 's1', agent: 'hermes', handle: 'h1', kind: 'cli', resumable: true }]
      await act(async () => {
        vi.advanceTimersByTime(9000)
        await Promise.resolve()
        await Promise.resolve()
      })

      expect(bubble('agents')).toBe('1')
    } finally {
      vi.useRealTimers()
    }
  })

  it('stops asking for the list once the palette is shut', async () => {
    vi.useFakeTimers()
    try {
      render(<DeskPalette />)
      await act(async () => {
        desk.update({ paletteOpen: true, tab: 'diff' })
      })
      await act(async () => { await Promise.resolve() })
      const before = asked.length

      await act(async () => {
        desk.update({ paletteOpen: false })
      })
      await act(async () => {
        vi.advanceTimersByTime(30000)
        await Promise.resolve()
      })

      expect(asked.length).toBe(before)
    } finally {
      vi.useRealTimers()
    }
  })

  /* The window a resume opens: the desk is reset, the replay repopulates the
     change list, and the palette's effect marks the shown tab seen -- all
     before the note is read back to restore the panes. A mark that wrote the
     whole note there would publish the empty desk it happens to be looking at,
     over the panes the replay was about to bring back. */
  it('leaves the desk note alone when a mark moves during a replay', async () => {
    render(<DeskPalette />)
    await act(async () => {
      desk.update({ paletteOpen: true, tab: 'diff' })
      desk.openDeskFile('/w/open-before.ts')
    })
    expect(desk.saved('s1')?.open).toEqual([{ k: 'file', path: '/w/open-before.ts' }])

    /* Exactly the order `openLiveSession` runs in -- including the palette
       still being open, which is what `desk.reset()` leaves it as for a reader
       who had it open (the flag is stored). */
    await act(async () => {
      workspace.reset()
      desk.reset()
    })
    await act(async () => {
      desk.update({ paletteOpen: true, tab: 'diff' })
    })
    await act(async () => {
      workspace.shared().changes.push(change('/w/one.py'), change('/w/two.py'))
      desk.notifyDesk()
    })

    /* What the resume is about to read. */
    expect(desk.saved('s1')?.open).toEqual([{ k: 'file', path: '/w/open-before.ts' }])
    expect(desk.saved('s1')?.tab).toBe('diff')
  })

  /* The other half of the same round trip, and the commoner one: a conversation
     with no turn in flight is not parked at all -- it is rebuilt from disk on
     the way back, so the marks have to come from the note the desk keeps per
     conversation. Found by doing it in the browser after the parked case was
     already green. */
  it('does not re-report a conversation rebuilt from disk rather than parked', async () => {
    render(<DeskPalette />)
    await act(async () => {
      desk.update({ paletteOpen: true, tab: 'diff' })
      deliveries.record(1, manifest([
        { path: '/w/a.md', name: 'a.md' },
        { path: '/w/b.md', name: 'b.md' },
      ]))
    })
    await act(async () => { desk.update({ tab: 'deliverables' }) })
    await act(async () => { desk.update({ tab: 'diff' }) })
    /* Away, and back the long way: everything cleared, the pointer moved and
       moved back, the transcript replayed and the registry seeded again.
       Nothing carries the marks across -- they are filed under the key. */
    await act(async () => {
      workspace.reset()
      desk.reset()
      setCurrent('s2')
    })
    await act(async () => {
      setCurrent('s1')
      deliveries.record(1, manifest([
        { path: '/w/a.md', name: 'a.md' },
        { path: '/w/b.md', name: 'b.md' },
      ]))
      desk.update({ paletteOpen: true, tab: 'diff' })
    })

    expect(bubble('deliverables')).toBeNull()
  })

  /* Switching away and back is a park and a restore: the changes and the
     deliveries come back with the conversation, so what the reader had already
     looked at must come back with them, or every round-trip re-reports it. */
  it('does not re-report a conversation as new after a round trip through another', async () => {
    render(<DeskPalette />)
    await act(async () => {
      desk.update({ paletteOpen: true, tab: 'diff' })
      deliveries.record(1, manifest([
        { path: '/w/a.md', name: 'a.md' },
        { path: '/w/b.md', name: 'b.md' },
      ]))
    })
    /* Looked at, so nothing in it is new any more. */
    await act(async () => { desk.update({ tab: 'deliverables' }) })
    await act(async () => { desk.update({ tab: 'diff' }) })
    expect(bubble('deliverables')).toBeNull()

    /* Away to another conversation... */
    const parked = workspace.snapshot()
    await act(async () => {
      workspace.reset()
      desk.reset()
    })
    /* ...and back. */
    await act(async () => {
      workspace.restore(parked)
      desk.update({ paletteOpen: true, tab: 'diff' })
    })

    expect(bubble('deliverables')).toBeNull()
  })

  /* A tab that shrank has nothing new to say -- and a session switch empties
     every source at once. */
  it('says nothing is new after the sources empty under it', async () => {
    render(<DeskPalette />)
    await act(async () => {
      desk.update({ paletteOpen: true, tab: 'diff' })
      deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md' }]))
    })
    expect(bubble('deliverables')).toBe('1')

    await act(async () => {
      deliveries.restore([])
      desk.notifyDesk()
    })

    expect(bubble('deliverables')).toBeNull()
  })

  it('shows a delivery that lands while the shelf is already open', async () => {
    await shelf()
    expect(rowNames()).toEqual([])
    await act(async () => {
      deliveries.record(1, manifest([{ path: '/w/late.md', name: 'late.md', title: 'Landed late' }]))
    })
    expect(rowNames()).toEqual(['Landed late'])
  })

  /* The legacy shell forwards view names this palette no longer has. */
  it('ignores a tab name it does not know instead of drawing another tab', async () => {
    deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md', title: 'Comparison' }]))
    await shelf()
    await act(async () => {
      desk.openDeskTab('file' as 'diff')
    })

    expect(desk.getState().tab).toBe('deliverables')
    expect(rowNames()).toEqual(['Comparison'])
  })
})
