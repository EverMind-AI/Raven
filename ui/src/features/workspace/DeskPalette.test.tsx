// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { DeskPalette } from './DeskPalette'
import * as deliveries from './deliveries'
import * as desk from './deskStore'
import * as workspace from './store'

import type { Shell } from '../../shell/bridge'
import type { WorkspaceSource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const opens: string[] = []

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
  window.RavenShell = fakeShell
  window.DS = { workspace: source }
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
    expect(document.querySelector('.desk-count')?.textContent).toBe('3')
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

  /* The case the badge exists for: the reader is somewhere else when the file
     lands. Nothing else repaints the palette on a turn that delivered a file
     and changed none. */
  it('shows the count while another tab is the one on screen', async () => {
    render(<DeskPalette />)
    await act(async () => {
      desk.update({ paletteOpen: true, tab: 'diff' })
    })
    expect(document.querySelector('.desk-count')).toBeNull()

    await act(async () => {
      deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md' }]))
    })

    expect(document.querySelector('.desk-count')?.textContent).toBe('1')
    expect(document.querySelector('.desk-dlv-row')).toBeNull()
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
