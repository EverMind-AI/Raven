// @vitest-environment happy-dom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DeskFollowToggle, DeskSurface } from './DeskSurface'
import * as deliveries from './deliveries'
import * as desk from './deskStore'
import * as workspace from './store'

import type { Shell } from '../../shell/bridge'
import type { WorkspaceSource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

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
  }
  window.RavenShell = fakeShell
  window.DS = { workspace: source, prose: { pathOf: () => null, linkTargetOf: () => null } }
  /* The viewer reads /file for a text kind; a pending promise keeps it on the
     spinner rather than letting happy-dom dial a socket. */
  vi.stubGlobal('fetch', () => new Promise(() => {}))
  document.body.innerHTML = '<div id="split" data-open="true"></div><aside id="ws"></aside>'
  desk._resetForTests()
  deliveries.restore([])
  workspace.restore({ changes: [], urls: [], file: null, turn: 2, unseen: 0, deliveries: [] })
}

const manifest = (files: Array<Record<string, unknown>>): unknown => ({ raven_delivery: { files } })

const strip = (): HTMLElement | null => document.querySelector('.desk-pane > .dlv-strip')

beforeEach(wire)

afterEach(() => {
  cleanup()
  act(() => {
    desk._resetForTests()
    deliveries.restore([])
  })
  window.RavenShell = undefined
  window.DS = undefined
  vi.unstubAllGlobals()
})

describe('the desk handle', () => {
  /* The reader asked for nothing of the desk to be visible over a fullscreen
     pane, and the handle is the part that would otherwise sit on top of it:
     `#deskHost` is above the fullscreen layer in the z ladder. */
  it('leaves the screen while a pane is fullscreen, and comes back with it', async () => {
    render(<DeskFollowToggle />)
    await act(async () => {
      desk.openDeskFile('/w/a.md')
    })
    expect(document.querySelector('.desk-follow-toggle')).toBeTruthy()

    await act(async () => {
      desk.toggleSolo('file:/w/a.md')
    })
    expect(document.querySelector('.desk-follow-toggle')).toBeNull()

    await act(async () => {
      desk.toggleSolo('file:/w/a.md')
    })
    expect(document.querySelector('.desk-follow-toggle')).toBeTruthy()
  })
})

describe('the pane of a delivered file', () => {
  it('names what was delivered above the file it opened as', async () => {
    deliveries.record(2, manifest([{
      path: '/w/a.md', name: 'a.md', title: 'Comparison', description: 'Three products, one table', size: 1824,
    }]))
    render(<DeskSurface />)
    await act(async () => {
      desk.openDeskFile('/w/a.md')
    })

    expect(strip()?.querySelector('b')?.textContent).toBe('Comparison')
    expect(strip()?.querySelector('p')?.textContent).toBe('Three products, one table')
    expect([...strip()!.querySelectorAll('.dlv-meta i')].map((n) => n.textContent))
      .toEqual(['MD', '1.8 KB', 'gui.ws.dlv_here'])
    /* The pane is still the file: its header names the file, not the title. */
    expect(document.querySelector('.desk-pane > header b')?.textContent).toBe('a.md')
  })

  it('leaves a file that was never delivered without one', async () => {
    render(<DeskSurface />)
    await act(async () => {
      desk.openDeskFile('/w/pool.go')
    })

    expect(document.querySelector('.desk-pane')).toBeTruthy()
    expect(strip()).toBeNull()
  })

  it('says which earlier turn delivered it', async () => {
    deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md', title: 'Comparison' }]))
    render(<DeskSurface />)
    await act(async () => {
      desk.openDeskFile('/w/a.md')
    })

    expect([...strip()!.querySelectorAll('.dlv-meta i')].map((n) => n.textContent))
      .toEqual(['MD', 'gui.ws.dlv_turn {"n":"1"}'])
  })

  it('picks up the file going missing while the pane is open', async () => {
    deliveries.record(2, manifest([{ path: '/w/a.md', name: 'a.md', title: 'Comparison' }]))
    render(<DeskSurface />)
    await act(async () => {
      desk.openDeskFile('/w/a.md')
    })
    expect(strip()?.querySelector('.bad')).toBeNull()

    await act(async () => {
      workspace.markDeliveryMissing('/w/a.md')
    })
    expect(strip()?.querySelector('.bad')?.textContent).toBe('gui.arts.missing')
  })

  /* A picture that failed to load says nothing about why. */
  it('probes before marking a picture missing, and believes only a 404', async () => {
    deliveries.record(2, manifest([
      { path: '/w/gone.png', name: 'gone.png' },
      { path: '/w/huge.png', name: 'huge.png' },
    ]))
    const asked: string[] = []
    vi.stubGlobal('fetch', (url: string, init?: { method?: string }) => {
      asked.push(`${init?.method || 'GET'} ${url}`)
      return Promise.resolve({ ok: false, status: String(url).includes('gone') ? 404 : 413 })
    })

    await act(async () => {
      await workspace.probeDeliveryMissing('/w/gone.png')
      await workspace.probeDeliveryMissing('/w/huge.png')
    })

    expect(asked.every((a) => a.startsWith('HEAD '))).toBe(true)
    expect(deliveries.byPath('/w/gone.png')?.missing).toBe(true)
    expect(deliveries.byPath('/w/huge.png')?.missing).toBe(false)
  })

  /* 404 is the only status that means the file is gone. */
  it('marks the shelf from a read that 404s, and not from one that is refused', async () => {
    deliveries.record(2, manifest([
      { path: '/w/gone.md', name: 'gone.md' },
      { path: '/w/denied.md', name: 'denied.md' },
    ]))
    vi.stubGlobal('fetch', (url: string) =>
      Promise.resolve({ ok: false, status: String(url).includes('gone') ? 404 : 403 }))

    await act(async () => {
      await workspace.loadFileText(workspace.makeFile('/w/gone.md'))
      await workspace.loadFileText(workspace.makeFile('/w/denied.md'))
    })

    expect(deliveries.byPath('/w/gone.md')?.missing).toBe(true)
    expect(deliveries.byPath('/w/denied.md')?.missing).toBe(false)
  })
})

/* happy-dom lays nothing out, so the geometry the gesture reads is stubbed at
   the one seam it crosses: the grid's rect. Slot arithmetic is pure
   (deskDrag.test.ts); this is the wiring from a pointer to the store. */
describe('dragging a pane by its header', () => {
  /* The rect spy patches Element.prototype; scoped restore so no sibling suite
     inherits an 800x600 world. */
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  const rect = { left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, x: 0, y: 0, toJSON: () => ({}) } as DOMRect

  const pointer = (type: string, target: EventTarget, x: number, y: number): void => {
    const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 })
    Object.defineProperty(event, 'pointerId', { value: 7 })
    target.dispatchEvent(event)
  }

  const twoPanes = async (): Promise<void> => {
    render(<DeskSurface />)
    await act(async () => {
      desk.openDeskFile('/workspace/a.ts')
      desk.openDeskFile('/workspace/b.ts')
    })
  }

  const ids = (): string[] => desk.getState().panes.map((pane) => pane.id)

  it('turns the stack sideways when a pane is dropped at the left edge', async () => {
    await twoPanes()
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(rect)
    const header = document.querySelectorAll('.desk-pane > header')[1] as HTMLElement

    await act(async () => {
      pointer('pointerdown', header, 400, 450)
      pointer('pointermove', window, 100, 450)
    })
    /* Lifted, and the drop indicator shows the slot the drop would take. */
    expect(document.querySelector('.desk-grid')!.getAttribute('data-dragging')).toBe('true')
    expect(document.querySelector('.desk-drop')).toBeTruthy()
    await act(async () => {
      pointer('pointerup', window, 100, 450)
    })

    expect(ids()).toEqual(['file:/workspace/b.ts', 'file:/workspace/a.ts'])
    expect(desk.getState().duo).toBe('cols')
    const grid = document.querySelector('.desk-grid') as HTMLElement
    expect(grid.getAttribute('data-duo')).toBe('cols')
    expect(grid.getAttribute('data-dragging')).toBeNull()
    expect(document.querySelector('.desk-drop')).toBeNull()
    /* The pair's seam is now the column split, one divider either way. */
    expect(document.querySelector('.desk-divider.column')).toBeTruthy()
    expect(document.querySelector('.desk-divider.row')).toBeNull()
  })

  it('trades the pair when a pane is dropped on the other one', async () => {
    await twoPanes()
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(rect)
    const header = document.querySelectorAll('.desk-pane > header')[0] as HTMLElement

    await act(async () => {
      pointer('pointerdown', header, 400, 150)
      pointer('pointermove', window, 400, 450)
      pointer('pointerup', window, 400, 450)
    })

    expect(ids()).toEqual(['file:/workspace/b.ts', 'file:/workspace/a.ts'])
    expect(desk.getState().duo).toBe('rows')
  })

  it('keeps a short header press as a click', async () => {
    await twoPanes()
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(rect)
    const header = document.querySelectorAll('.desk-pane > header')[0] as HTMLElement

    await act(async () => {
      pointer('pointerdown', header, 400, 150)
      pointer('pointermove', window, 402, 152)
    })
    /* Mid-gesture, which is the only place the slack is observable: a lift that
       happened and settled back leaves the same desk as one that never did. */
    expect(document.querySelector('.desk-grid')!.getAttribute('data-dragging')).toBeNull()
    expect(document.querySelector('.desk-drop')).toBeNull()
    await act(async () => {
      pointer('pointerup', window, 402, 152)
    })

    expect(ids()).toEqual(['file:/workspace/a.ts', 'file:/workspace/b.ts'])
  })

  it('refuses the gesture where the desk shows one pane at a time', async () => {
    /* page.css collapses the grid below 840px: only the active pane is
       displayed and the dividers are gone. A drag there lifted the visible
       pane over a phantom grid and silently rewrote an arrangement the reader
       could not see -- they learned their desk was turned sideways the next
       time they widened the window. The guard reads the CSS's own outcome
       (computed display), which is what this stands in for: happy-dom loads
       no stylesheet, so the collapse is expressed directly. */
    await twoPanes()
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(rect)
    const grid = document.querySelector('.desk-grid') as HTMLElement
    const panes = document.querySelectorAll<HTMLElement>('.desk-pane')
    ;(panes[0] as HTMLElement).style.display = 'none'
    const header = document.querySelectorAll('.desk-pane > header')[1] as HTMLElement

    await act(async () => {
      pointer('pointerdown', header, 400, 450)
      pointer('pointermove', window, 100, 450)
      pointer('pointerup', window, 100, 450)
    })

    expect(grid.getAttribute('data-dragging')).toBeNull()
    expect(ids()).toEqual(['file:/workspace/a.ts', 'file:/workspace/b.ts'])
    expect(desk.getState().duo).toBe('rows')
  })

  it('does not let the last drop\'s cleanup strip the lift off a re-grabbed pane', async () => {
    /* settleDrag schedules a per-pane cleanup; grabbed again inside that
       window, the stale timer fired mid-gesture and removed the lift -- the
       pane in hand lost its z-index and painted UNDER the sibling it was being
       dragged across, until yet another drag put the class back. */
    vi.useFakeTimers()
    try {
      await twoPanes()
      vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(rect)
      const headers = (): NodeListOf<HTMLElement> => document.querySelectorAll('.desk-pane > header')

      await act(async () => {
        pointer('pointerdown', headers()[0] as HTMLElement, 400, 150)
        pointer('pointermove', window, 400, 450)
        pointer('pointerup', window, 400, 450)
      })
      /* Re-grab the same pane (now second) ~0ms after the drop, well inside
         the settle window, and hold it lifted while the old timer comes due. */
      await act(async () => {
        pointer('pointerdown', headers()[1] as HTMLElement, 400, 450)
        pointer('pointermove', window, 400, 300)
      })
      const held = document.querySelector('.desk-pane-lift') as HTMLElement
      expect(held).toBeTruthy()
      await act(async () => {
        vi.advanceTimersByTime(1000)
      })

      expect(held.classList.contains('desk-pane-lift')).toBe(true)
      await act(async () => {
        pointer('pointerup', window, 400, 300)
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it('puts everything back on Escape', async () => {
    await twoPanes()
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(rect)
    const header = document.querySelectorAll('.desk-pane > header')[1] as HTMLElement

    await act(async () => {
      pointer('pointerdown', header, 400, 450)
      pointer('pointermove', window, 100, 450)
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    })

    expect(ids()).toEqual(['file:/workspace/a.ts', 'file:/workspace/b.ts'])
    expect(desk.getState().duo).toBe('rows')
    expect(document.querySelector('.desk-grid')!.getAttribute('data-dragging')).toBeNull()
  })
})

