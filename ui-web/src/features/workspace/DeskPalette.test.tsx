// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DeskPalette } from './DeskPalette'
import {
  DESK_COLUMN_FLOOR, DESK_DEFAULT_HEIGHT, DESK_DEFAULT_WIDTH, DESK_DRAG_THRESHOLD,
  DESK_GEOMETRY_KEY, DESK_LAUNCHER_EDGE, DESK_TEXT_GAP,
} from './deskGeometry'
import * as agents from '../subagents/store'
import * as deliveries from './deliveries'
import * as desk from './deskStore'
import * as seen from './seen'
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

/* The palette outlives the flag by one animation, so "on screen" and "open" are
   not the same question in these tests. */
const palette = (): HTMLElement | null => document.querySelector('.desk-palette')
const phase = (): string | null => palette()?.getAttribute('data-phase') ?? null

const emptyOf = (): { title: string; hint: string; icon: boolean } | null => {
  const box = document.querySelector('.desk-empty')
  if (!box) return null
  return {
    title: box.querySelector('b')?.textContent || '',
    hint: box.querySelector('span')?.textContent || '',
    icon: !!box.querySelector('svg'),
  }
}

describe('opening and shutting the desk', () => {
  it('leaves for one animation and then stops existing', async () => {
    vi.useFakeTimers()
    try {
      render(<DeskPalette />)
      await act(async () => { desk.update({ paletteOpen: true }) })
      expect(phase()).toBe('in')

      await act(async () => { desk.update({ paletteOpen: false }) })
      /* Still there, because a leaving element has to be on screen to leave --
         and inert, because a tab clicked on the way out would act on a desk the
         reader has already dismissed. */
      expect(phase()).toBe('out')
      expect(palette()?.hasAttribute('inert')).toBe(true)

      await act(async () => { vi.advanceTimersByTime(200) })
      expect(palette()).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  /* A double-click on the handle used to land on a palette that vanished a
     moment later. */
  it('cancels the exit when the reader opens it again mid-flight', async () => {
    vi.useFakeTimers()
    try {
      render(<DeskPalette />)
      await act(async () => { desk.update({ paletteOpen: true }) })
      await act(async () => { desk.update({ paletteOpen: false }) })
      await act(async () => { desk.update({ paletteOpen: true }) })

      await act(async () => { vi.advanceTimersByTime(200) })

      expect(phase()).toBe('in')
      expect(palette()?.hasAttribute('inert')).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  /* Nothing of the desk over a fullscreen pane: the pane IS the window. */
  it('is not on screen while a pane is fullscreen', async () => {
    vi.useFakeTimers()
    try {
      render(<DeskPalette />)
      await act(async () => {
        /* The file first: opening a window stands the desk down, so the
           palette this test is about has to be put back up after it. */
        desk.openDeskFile('/w/a.md')
        desk.update({ paletteOpen: true })
      })
      await act(async () => { desk.toggleSolo('file:/w/a.md') })
      await act(async () => { vi.advanceTimersByTime(200) })
      expect(palette()).toBeNull()

      await act(async () => { desk.toggleSolo('file:/w/a.md') })
      expect(phase()).toBe('in')
    } finally {
      vi.useRealTimers()
    }
  })

  /* A bubble is cleared by the reader LOOKING at the tab, and behind a
     fullscreen pane they are not looking at any of them. Read off the mark
     itself: `unseen` answers zero for the tab that is selected whatever the
     mark says, so the tab the palette is parked on is exactly where a mark
     moving unseen would leave no trace. */
  it('does not mark a tab seen behind a fullscreen pane', async () => {
    render(<DeskPalette />)
    await act(async () => {
      desk.openDeskFile('/w/a.md')
      desk.update({ paletteOpen: true, tab: 'deliverables' })
    })
    await act(async () => { desk.toggleSolo('file:/w/a.md') })

    await act(async () => {
      deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/b.md', name: 'b.md' }]))
    })

    expect([...seen.of_('deliverables')]).toEqual([])

    /* And the moment the pane gives the screen back, it counts as seen. */
    await act(async () => { desk.toggleSolo('file:/w/a.md') })
    expect([...seen.of_('deliverables')]).toEqual(['/w/b.md'])
  })
})

describe('a tab with nothing in it', () => {
  /* One design for all three, which is what was asked for: an icon, what is
     not here, and where it would come from. Diff was a line of grey text and
     the shelf an illustrated block, and the two read as two different kinds of
     nothing. */
  it('says the same kind of nothing whichever tab it is', async () => {
    render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true, tab: 'diff' }) })
    expect(emptyOf()).toEqual({ title: 'gui.ws.no_changes', hint: 'gui.ws.no_changes_sub', icon: true })

    await act(async () => { desk.update({ tab: 'deliverables' }) })
    expect(emptyOf()).toEqual({ title: 'gui.ws.dlv_none', hint: 'gui.ws.dlv_none_sub', icon: true })

    await act(async () => { desk.update({ tab: 'agents' }) })
    expect(emptyOf()).toEqual({ title: 'gui.ws.agents_none', hint: 'gui.ws.agents_none_sub', icon: true })
    /* And one class, so there is one stylesheet rule to keep them aligned. */
    expect(document.querySelector('.desk-dlv-empty')).toBeNull()
  })

  it('says so in the same shape when the server does not report the work', async () => {
    window.DS!.agents = { list: async () => [], instances: async () => [], absent: () => true }
    render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true, tab: 'agents' }) })
    await act(async () => { await agents.refreshInstances(true) })

    expect(emptyOf()).toEqual({ title: 'gui.ws.agents_none', hint: 'gui.ws.agents_absent', icon: true })
  })
})

describe('the desk shelf', () => {
  it('says the session has delivered nothing rather than showing an empty list', async () => {
    await shelf()
    expect(screen.getByText('gui.ws.dlv_none')).toBeTruthy()
    expect(document.querySelector('.desk-dlv-row')).toBeNull()
    /* Nothing on the shelf is not worth a number on the tab. */
    expect(document.querySelector('.desk-count')).toBeNull()
  })

  const at = (when: number, path: string, title: string): unknown => ({
    raven_delivery: { delivered_at: when, files: [{ path, name: path.split('/').pop(), title }] },
  })

  it('lists the whole session newest turn first, under a group per turn', async () => {
    workspace.restore({ changes: [], urls: [], file: null, turn: 2, unseen: 0, deliveries: [] })
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/old.md', name: 'old.md', title: 'Older brief' }]))
    deliveries.record(deliveries.SESSION, 2, manifest([
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

  it('does not read a delegated stream\'s turn as one of this conversation\'s', async () => {
    /* Each stream counts its turns from one. A sub-agent row filed under its own
       turn 2 was labelled "turn 2" on a conversation that has had five, and one
       filed under 5 was grouped as "this turn" for no better reason than the
       number matching. The shelf is session-wide, and the reader's position in it
       is the conversation's turn -- a delegated row has no place in that count,
       so it says what the file is and leaves the position out, exactly as a row
       recovered from the registry does. */
    workspace.restore({ changes: [], urls: [], file: null, turn: 5, unseen: 0, deliveries: [] })
    deliveries.record('agent:one', 5, manifest([{ path: '/w/sub.md', name: 'sub.md', title: 'From a sub-agent' }]))
    deliveries.record('agent:one', 2, manifest([{ path: '/w/sub2.md', name: 'sub2.md', title: 'Also delegated' }]))
    await shelf()

    /* Both unstamped, so they rank by arrival -- the later-recorded one on top. */
    const meta = [...document.querySelectorAll('.desk-dlv-row .desk-name s')].map((n) => n.textContent)
    expect(meta).toEqual(['sub2.md', 'sub.md'])
    expect([...document.querySelectorAll('.desk-grp')].map((n) => n.textContent))
      .toEqual(['gui.ws.turn_earlier'])
  })

  it('gives each group one heading, however the streams interleave', async () => {
    /* The rows are ranked by when each delivery happened, so two streams on one
       shelf are not contiguous by group. Emitting a heading whenever the key
       changed from the row before rendered "This turn" / "Earlier" / "This turn"
       -- three headings for two groups, and the reader cannot tell which of the
       two "This turn" blocks is the one they are in. */
    workspace.restore({ changes: [], urls: [], file: null, turn: 2, unseen: 0, deliveries: [] })
    deliveries.record(deliveries.SESSION, 2, at(1_000, '/w/first.md', 'First'))
    deliveries.record('agent:one', 1, at(2_000, '/w/mid.md', 'Delegated'))
    deliveries.record(deliveries.SESSION, 2, at(3_000, '/w/last.md', 'Last'))
    await shelf()

    expect([...document.querySelectorAll('.desk-grp')].map((n) => n.textContent))
      .toEqual(['gui.ws.turn_now', 'gui.ws.turn_earlier'])
    expect(rowNames()).toEqual(['Last', 'First', 'Delegated'])
  })

  it('opens the clicked row as a pane, carrying its download path', async () => {
    deliveries.record(deliveries.SESSION, 1, manifest([
      { path: '/w/a.md', name: 'a.md', title: 'Comparison', download_path: '/files/download?token=t1' },
    ]))
    await shelf()
    await act(async () => {
      (document.querySelector('.desk-dlv-row') as HTMLElement).click()
    })

    const panes = desk.getState().panes
    expect(panes.map((pane) => pane.id)).toEqual(['file:/w/a.md'])
  })

  /* The demo shell's source cannot read a file, and a viewer with nothing to
     show is worse than the note it answers with. */
  it('hands the path to a source that cannot read files, opening no pane', async () => {
    ;(window.DS!.workspace as WorkspaceSource).canBrowse = false
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/a.md', name: 'a.md', title: 'Comparison' }]))
    await shelf()
    await act(async () => {
      (document.querySelector('.desk-dlv-row') as HTMLElement).click()
    })

    expect(opens).toEqual(['/w/a.md'])
    expect(desk.getState().panes).toEqual([])
  })

  it('marks a file that is gone, and still lets it be opened to say so', async () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/gone.md', name: 'gone.md', missing: true }]))
    await shelf()

    const row = document.querySelector('.desk-dlv-row') as HTMLButtonElement
    expect(row.classList.contains('gone')).toBe(true)
    expect(row.querySelector('.dlv-gone')?.textContent).toBe('gui.arts.missing')
    expect(row.disabled).toBe(false)
  })

  it('keeps one row for a re-delivered file, at its newest turn', async () => {
    workspace.restore({ changes: [], urls: [], file: null, turn: 3, unseen: 0, deliveries: [] })
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/a.md', name: 'a.md', title: 'First cut' }]))
    deliveries.record(deliveries.SESSION, 3, manifest([{ path: '/w/a.md', name: 'a.md', title: 'Second cut' }]))
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
      deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/a.md', name: 'a.md' }]))
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
      deliveries.record(deliveries.SESSION, 1, manifest([
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
      .toEqual(['gui.ws.deliverables', 'gui.ws.agents', 'Diff'])

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
    deliveries.record(deliveries.SESSION, 1, manifest([
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
      deliveries.record(deliveries.SESSION, 1, manifest([
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
      deliveries.record(deliveries.SESSION, 1, manifest([
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
      deliveries.record(deliveries.SESSION, 1, manifest([
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
      deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/a.md', name: 'a.md' }]))
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
      deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/late.md', name: 'late.md', title: 'Landed late' }]))
    })
    expect(rowNames()).toEqual(['Landed late'])
  })

  /* The legacy shell forwards view names this palette no longer has. */
  it('ignores a tab name it does not know instead of drawing another tab', async () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/a.md', name: 'a.md', title: 'Comparison' }]))
    await shelf()
    await act(async () => {
      desk.openDeskTab('file' as 'diff')
    })

    expect(desk.getState().tab).toBe('deliverables')
    expect(rowNames()).toEqual(['Comparison'])
  })
})

describe('the size the desk comes up at', () => {
  it('opens at the default rather than the minimum', async () => {
    await shelf()
    expect(palette()?.style.width).toBe(`${DESK_DEFAULT_WIDTH}px`)
    expect(palette()?.style.height).toBe(`${DESK_DEFAULT_HEIGHT}px`)
  })

  /* The geometry is written back on the first render, so every reader who has
     ever opened the desk holds the old size under the old key. Reading that key
     would hand the new default to nobody but a fresh browser. */
  it('does not inherit a size stored under the previous key', async () => {
    localStorage.setItem(
      'raven.gui.desk.geometry.v5',
      JSON.stringify({ x: 8, y: 8, w: 250, h: 250, detached: true }),
    )
    await shelf()
    expect(palette()?.style.width).toBe(`${DESK_DEFAULT_WIDTH}px`)
    expect(palette()?.dataset.anchored).toBe('true')
  })

  /* And a size the reader chose under the CURRENT key is still theirs. */
  it('keeps a size stored under the current key', async () => {
    localStorage.setItem(
      DESK_GEOMETRY_KEY,
      JSON.stringify({ x: 40, y: 40, w: 420, h: 400, detached: true }),
    )
    await shelf()
    expect(palette()?.style.width).toBe('420px')
    expect(palette()?.style.height).toBe('400px')
  })
})

/* That the reserve reaches the stylesheet at all.
 *
 * `deskGeometry.test.ts` pins what the number is and
 * `scripts/desk-reserve-css.test.mjs` pins where the stylesheet spends it. This
 * is the join: the panel is `position: fixed`, so the only thing connecting it
 * to the layout is this property landing on the root, and neither of those two
 * tests would notice if it stopped being set.
 */
describe('the reserve the palette publishes', () => {
  /* happy-dom measures everything as zero, and the reserve turns on the chat's
     width, so the harness has to say how wide the chat is. */
  const chatIs = (width: number): HTMLElement => {
    const chat = document.createElement('div')
    chat.className = 'chat'
    chat.getBoundingClientRect = (() => ({ width, height: 600, left: 0, top: 0, right: width, bottom: 600 })) as never
    document.body.append(chat)
    return chat
  }
  const reserve = (): string => document.documentElement.style.getPropertyValue('--desk-reserve')
  const want = DESK_DEFAULT_WIDTH + DESK_LAUNCHER_EDGE + DESK_TEXT_GAP

  it('sets it on the root while the desk is anchored over the chat', async () => {
    chatIs(want + DESK_COLUMN_FLOOR + 40)
    render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true }) })

    expect(reserve()).toBe(`${want}px`)
  })

  it('takes it back when the desk goes down', async () => {
    /* Removed rather than set to 0: the stylesheet's own `, 0px` fallback is
       what applies, which is the one path a detached or floor-blocked panel
       also takes. */
    chatIs(want + DESK_COLUMN_FLOOR + 40)
    render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true }) })
    await act(async () => { desk.update({ paletteOpen: false }) })

    expect(reserve()).toBe('')
  })

  it('takes it back when the palette stops being rendered', async () => {
    /* Nothing unmounts the palette today -- `DeskApp` renders it once and a
       module page hides `#deskHost` rather than dropping it. This pins the
       cleanup anyway, because the property lives outside React and the failure
       is silent: a transcript left 324px narrow with nothing on screen to
       explain it. */
    chatIs(want + DESK_COLUMN_FLOOR + 40)
    const view = render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true }) })
    expect(reserve()).toBe(`${want}px`)

    await act(async () => { view.unmount() })

    expect(reserve()).toBe('')
  })

  it('publishes nothing when the chat cannot spare the width', async () => {
    /* The all-or-nothing floor, through the component rather than the pure
       function: this is the case where the panel keeps overlapping and the
       column keeps its width, which is today's behaviour. */
    chatIs(want + DESK_COLUMN_FLOOR - 1)
    render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true }) })

    expect(reserve()).toBe('')
  })

  it('re-answers when the chat changes width under it', async () => {
    /* The chat resizes without this component rendering -- the window, the
       rail, the workspace column opening beside it -- so the observer is what
       keeps the answer true rather than merely true at mount. */
    const chat = chatIs(want + DESK_COLUMN_FLOOR + 40)
    render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true }) })
    expect(reserve()).toBe(`${want}px`)

    chat.getBoundingClientRect = (() => ({
      width: want + DESK_COLUMN_FLOOR - 1, height: 600, left: 0, top: 0,
      right: want + DESK_COLUMN_FLOOR - 1, bottom: 600,
    })) as never
    await act(async () => { window.dispatchEvent(new Event('resize')) })

    expect(reserve()).toBe('')
  })
})

/* The empty tab saying what the OTHER tab is holding.
 *
 * `changed` and `delivered` are two facts, and only the selected tab carries a
 * label -- so "Nothing delivered yet" sat next to a bubble on an unlabelled
 * icon, with nothing saying the bubble counted something else. These cases are
 * the answer, and the one that matters most is the last: an empty tab with nothing anywhere else must NOT
 * grow a way out, or the fix becomes a permanent nudge to a second empty tab.
 */
describe('an empty tab points at the other one', () => {
  const withChanges = async (...keys: string[]): Promise<void> => {
    await act(async () => {
      workspace.shared().changes.push(...keys.map(change))
      desk.notifyDesk()
    })
  }

  it('says how many files changed, when none were delivered', async () => {
    await shelf()
    await withChanges('/w/one.py', '/w/two.py')

    expect(screen.getByText('gui.ws.n.dlv_none_kept {"n":"2"}')).toBeTruthy()
    /* The label is not counted -- "Show what changed" is true of one file and
       of five, so it takes no `{n}` and the sentence above it carries the
       number. */
    expect(document.querySelector('.desk-empty-to')?.textContent).toBe('gui.ws.see_changes')
  })

  it('says it in the singular for one file, the way phraseOf does', async () => {
    await shelf()
    await withChanges('/w/one.py')

    expect(screen.getByText('gui.ws.dlv_none_kept')).toBeTruthy()
    expect(document.querySelector('.desk-empty-to')?.textContent).toBe('gui.ws.see_changes')
  })

  it('takes the reader to the tab that has them', async () => {
    await shelf()
    await withChanges('/w/one.py')

    await act(async () => { (document.querySelector('.desk-empty-to') as HTMLElement).click() })

    expect(desk.getState().tab).toBe('diff')
  })

  it('answers the other way round too, from an empty Diff', async () => {
    /* The same confusion runs backwards: "No changes yet" beside a bubble on
       the shelf's icon. One mechanism, both directions. */
    render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true, tab: 'diff' }) })
    await act(async () => {
      deliveries.record('', 1, manifest([{ path: 'out/report.md', bytes: 12 }]))
    })

    expect(screen.getByText('gui.ws.no_changes_dlv')).toBeTruthy()
    await act(async () => { (document.querySelector('.desk-empty-to') as HTMLElement).click() })
    expect(desk.getState().tab).toBe('deliverables')
  })

  it('offers no way out when the other tab is empty as well', async () => {
    /* Both empty is the ordinary state of a fresh conversation, and a link to
       a second empty tab is worse than the plain sentence it replaced. */
    await shelf()

    expect(screen.getByText('gui.ws.dlv_none_sub')).toBeTruthy()
    expect(document.querySelector('.desk-empty-to')).toBeNull()
  })
})

/* Dragging the panel by its handle.
 *
 * The handle carries the tab strip, so most of what looks like a title bar is
 * buttons: measured on the running page, 182 of its 298px, the rest broken into
 * 3px slivers between the tabs. A press there used to return before doing
 * anything, which left the panel draggable from 39% of its own handle and let
 * the browser take the gesture instead. So a press has to be able to become
 * either a drag or a click, and these cases are where the two part.
 *
 * Only what the DOM can answer without a layout: which element the press
 * reaches, whether the panel detached, and whether the tab's click survived.
 * The two geometry claims -- that the panel follows the hand through the anchor
 * instead of freezing in it, and that it goes home when released nearby -- are
 * measured in a browser and recorded in the commit, because happy-dom lays
 * nothing out and would pass them either way.
 */
describe('the panel drags by its handle', () => {
  const handle = (): HTMLElement => document.querySelector('.desk-drag') as HTMLElement
  const panel = (): HTMLElement => document.querySelector('.desk-palette') as HTMLElement
  const tab = (label: string): HTMLElement =>
    [...document.querySelectorAll<HTMLElement>('.desk-tabs button')]
      .find((b) => (b.querySelector('.lb')?.textContent || '') === label)!

  /* Whether the handle took the pointer. Recorded rather than silently stubbed,
     because WHEN capture is taken is the thing one of these cases is about. */
  let captured = false
  const press = async (on: HTMLElement, x: number, y: number): Promise<void> => {
    captured = false
    const handle = document.querySelector('.desk-drag') as HTMLElement
    handle.setPointerCapture = () => { captured = true }
    handle.hasPointerCapture = () => captured
    handle.releasePointerCapture = () => { captured = false }
    await act(async () => {
      on.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, pointerId: 1, clientX: x, clientY: y }))
    })
  }
  const to = async (x: number, y: number): Promise<void> => {
    await act(async () => {
      window.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 1, clientX: x, clientY: y }))
    })
  }
  const release = async (x: number, y: number): Promise<void> => {
    await act(async () => {
      window.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1, clientX: x, clientY: y }))
    })
  }
  const at = (): { left: string; top: string } => ({ left: panel().style.left, top: panel().style.top })

  it('starts a drag from a tab, which is most of the handle', async () => {
    render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true }) })

    await press(tab('Diff'), 500, 500)
    await to(500 + DESK_DRAG_THRESHOLD + 20, 520)
    await release(520 + DESK_DRAG_THRESHOLD, 520)

    expect(panel().dataset.anchored).toBe('false')
    expect(at().left).not.toBe('')
  })

  it('leaves a tab its click when the press did not move', async () => {
    /* The other half of the same rule: a press that goes nowhere is a click,
       and swallowing it would make the tabs unusable to fix the handle.

       The capture is what this really guards. Taken on `pointerdown`, pointer
       events retarget the browser's own click to the capture target, so the
       click never reaches the tab -- measured in a real browser, a press on
       `Diff` produced a click on `desk-drag` and the tab did not switch. So the
       case asserts the handle did NOT capture, which is the condition that
       lets a real click land, and then lets the click run its own course. */
    render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true, tab: 'deliverables' }) })

    await press(tab('Diff'), 500, 500)
    await release(500, 501)

    expect(captured).toBe(false)
    await act(async () => {
      tab('Diff').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    })
    expect(desk.getState().tab).toBe('diff')
  })

  it('leaves a tab its click when the hand only shook', async () => {
    /* The threshold's own case, and it needs a MOVE below it: a press with no
       pointermove at all never reaches the comparison, so it passes whether the
       threshold is there or not. */
    render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true, tab: 'deliverables' }) })

    await press(tab('Diff'), 500, 500)
    await to(501, 501)
    await release(501, 501)

    expect(captured).toBe(false)
    await act(async () => {
      tab('Diff').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    })
    expect(desk.getState().tab).toBe('diff')
    expect(panel().dataset.anchored).toBe('true')
  })

  it('does not also switch tab on the drag that ended on one', async () => {
    /* The gesture ends on a button whose click is about to fire. A panel that
       moved and changed what it shows did two things for one gesture. */
    render(<DeskPalette />)
    await act(async () => { desk.update({ paletteOpen: true, tab: 'deliverables' }) })

    await press(tab('Diff'), 500, 500)
    /* A drag DOES capture, which is what keeps the pointer with the handle
       while the hand moves -- checked here, because the release gives it
       straight back. */
    expect(captured).toBe(false)
    await to(600, 600)
    expect(captured).toBe(true)
    await release(600, 600)

    await act(async () => {
      tab('Diff').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    })
    expect(desk.getState().tab).toBe('deliverables')
  })

  it('keeps the press from an ancestor that would act on it too', async () => {
    /* An ancestor acting on the same gesture is the other half of what reads as
       a conflict with the page behind -- the desk's own panes carry
       `onPointerDown` to raise themselves, and the pane header is a drag handle
       of its own.

       A REACT ancestor, which is what this reaches and what `resize` beside it
       has always guarded against. React dispatches from the root, so a native
       listener further up has already had the event by then and
       `stopPropagation` cannot take it back; a document-level capture listener
       runs before the target and could not be stopped by anything here either.
       This pins the half that is actually in reach. */
    const seen: string[] = []
    render(<div onPointerDown={() => seen.push('ancestor')}><DeskPalette /></div>)
    await act(async () => { desk.update({ paletteOpen: true }) })

    await press(handle(), 500, 500)

    expect(seen).toEqual([])
  })
})
