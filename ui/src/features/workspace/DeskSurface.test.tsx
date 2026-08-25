// @vitest-environment happy-dom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DeskSurface } from './DeskSurface'
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
