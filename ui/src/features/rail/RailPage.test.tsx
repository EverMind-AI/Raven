// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { RailApp } from './RailPage'
import * as store from './store'

import type { MenuItem, Shell, ToastAction } from '../../shell/bridge'
import type { RailSnapshot, SessRow } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

function row(over: Partial<SessRow> = {}): SessRow {
  return { id: 'a', title: 'GTM research', last: 'made a table', when: '11:24', pin: false, ...over }
}

interface Harness {
  state: RailSnapshot
  calls: Array<[string, unknown]>
  menus: Array<Array<MenuItem | '-'>>
  toasts: Array<{ text: string; action?: ToastAction }>
}

/* The island runs against the same two seams production wires: a fake shell
   on window.RavenShell (T returns its key, so tests assert catalogue keys)
   and a snapshot source on window.DS.sessions. The fake drawList loops back
   into store.draw() exactly as the legacy shim does. */
function install(over: Partial<RailSnapshot> = {}): Harness {
  const state: RailSnapshot = { rows: [row()], cur: 'a', busy: false, query: '', ...over }
  const calls: Array<[string, unknown]> = []
  const menus: Array<Array<MenuItem | '-'>> = []
  const toasts: Array<{ text: string; action?: ToastAction }> = []
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    toast: (text, action) => toasts.push({ text, action }),
    menuAt: (_x, _y, items) => menus.push(items),
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: (id) => calls.push(['showPage', id]),
    drawList: () => store.draw(),
    setCur: (id) => {
      state.cur = id
      calls.push(['setCur', id])
    },
    openSession: (s) => calls.push(['openSession', (s as SessRow).id]),
    removeSession: (s) => {
      calls.push(['removeSession', (s as SessRow).id])
      store.remove(s as SessRow)
    },
    renameTitle: () => calls.push(['renameTitle', null]),
    dropDraft: (id) => calls.push(['dropDraft', id]),
    pinPersist: (id, pinned) => calls.push(['pinPersist', { id, pinned }]),
    openCron: () => calls.push(['openCron', null]),
    navState: () => ({ pages: [], btnOf: () => undefined }),
  }
  window.RavenShell = fakeShell
  window.DS = { sessions: { snapshot: () => state } }
  document.body.innerHTML =
    '<div class="app" data-page="off">' +
    '<button id="newBtn"></button><button id="skillBtn"></button>' +
    '<button id="plugBtn"></button><button id="memBtn"></button><button id="moreBtn"></button>' +
    '<div id="moreFly" data-open="false"></div>' +
    PAGES.map((p) => `<div id="${p}" data-open="false"></div>`).join('') +
    '<div id="list"></div><h1 id="title">t</h1><button id="renameBtn"></button></div>'
  return { state, calls, menus, toasts }
}

/* The nav the assembled page hands over (demo/155-bridge.js reads it off
   NAV_OF and MORE_ROWS): every module page, the rail button each one lights
   up, and the More group's rows in their drawn order. The default fake above
   hands over an empty one, which is the whole page shut. */
const PAGES = ['capsPage', 'xaPage', 'connPage', 'memPage', 'cronPage']
const BTN_OF: Record<string, string> = {
  capsPage: 'skillBtn', xaPage: 'moreBtn', connPage: 'moreBtn', memPage: 'memBtn', cronPage: 'moreBtn',
}

function navUp(open: string): void {
  window.RavenShell!.navState = () => ({
    pages: PAGES, btnOf: (p) => BTN_OF[p], morePages: ['xaPage', 'connPage', 'cronPage'],
  })
  document.querySelector<HTMLElement>('.app')!.dataset.page = 'on'
  PAGES.forEach((p) => { document.getElementById(p)!.dataset.open = String(p === open) })
}

const current = (id: string): string | null => document.getElementById(id)!.getAttribute('aria-current')

function mount(): HTMLElement {
  const host = document.getElementById('list')!
  render(<RailApp />, { container: host })
  act(() => store.draw())
  return host
}

const rowByTitle = (host: HTMLElement, title: string): HTMLElement =>
  [...host.querySelectorAll<HTMLElement>('.sess')].find((r) => r.querySelector('.t')?.textContent === title)!

afterEach(() => {
  cleanup()
})

describe('rail island', () => {
  it('renders the groups and the rows, titles stripped of leading emoji', () => {
    install({
      rows: [row(), row({ id: 'p', title: 'pinned one', pin: true }), row({ id: 'k', title: 'daily digest', from: 'cron' }), row({ id: 'e', title: '🚀 Ship it' })],
    })
    const host = mount()
    expect(screen.getByText('gui.rail.pinned')).toBeTruthy()
    expect(screen.getByText('gui.rail.from_cron')).toBeTruthy()
    expect(screen.getByText('gui.rail.recent')).toBeTruthy()
    expect(screen.getByText('GTM research')).toBeTruthy()
    expect(screen.getByText('daily digest')).toBeTruthy()
    expect(screen.getByText('Ship it')).toBeTruthy()
    expect(host.querySelectorAll('.sess').length).toBe(4)
    expect(screen.getByText('gui.rail.manage')).toBeTruthy()
  })

  it('marks only the current session, and the new button when nothing is', () => {
    install({ rows: [row(), row({ id: 'b', title: 'second task' })], cur: 'b' })
    const host = mount()
    expect(rowByTitle(host, 'second task').getAttribute('aria-current')).toBe('true')
    expect(rowByTitle(host, 'GTM research').getAttribute('aria-current')).toBe('false')
    expect(document.getElementById('newBtn')!.getAttribute('aria-current')).toBe('false')
  })

  it('marks the new button while no session is current', () => {
    install({ rows: [row()], cur: null })
    mount()
    expect(document.getElementById('newBtn')!.getAttribute('aria-current')).toBe('true')
  })

  it('shows the running tail on the busy current row and clears it after', () => {
    const h = install({ rows: [row(), row({ id: 'd', title: 'done one', status: 'done' }), row({ id: 'x', title: 'broken one', status: 'err' })], busy: true })
    const host = mount()
    const run = rowByTitle(host, 'GTM research').querySelector('.w')!
    expect(run.getAttribute('data-sig')).toBe('run')
    expect(run.getAttribute('aria-label')).toBe('gui.sess.running')
    expect(run.querySelector('i')).toBeTruthy()
    const done = rowByTitle(host, 'done one').querySelector('.w')!
    expect(done.getAttribute('data-sig')).toBe('done')
    expect(rowByTitle(host, 'broken one').querySelector('.dot.err')).toBeTruthy()
    h.state.busy = false
    h.state.rows[1]!.status = null
    act(() => store.draw())
    expect(host.querySelector('[data-sig]')).toBeNull()
    expect(host.querySelector('.dot.err')).toBeTruthy()
  })

  it('keeps the permanent groups on an empty list', () => {
    install({ rows: [], cur: null })
    const host = mount()
    expect(host.querySelectorAll('.grp-empty').length).toBe(2)
    expect(screen.getByText('gui.rail.from_cron')).toBeTruthy()
    expect(screen.getByText('gui.rail.recent')).toBeTruthy()
    expect(screen.queryByText('gui.rail.pinned')).toBeNull()
  })

  it('holds skeleton rows for the live boot and swaps them for the list', () => {
    install()
    const host = document.getElementById('list')!
    render(<RailApp />, { container: host })
    act(() => store.skeleton())
    const skels = host.querySelectorAll('.sess.skel')
    expect(skels.length).toBe(6)
    expect(skels[0]!.querySelectorAll('.sk').length).toBe(2)
    act(() => store.draw())
    expect(host.querySelector('.sess.skel')).toBeNull()
    expect(screen.getByText('GTM research')).toBeTruthy()
  })

  it('keeps the last rows when the source cannot answer a draw', () => {
    install()
    const host = mount()
    expect(screen.getByText('GTM research')).toBeTruthy()
    window.DS = {
      sessions: {
        snapshot: () => {
          throw new Error('gone')
        },
      },
    }
    act(() => store.draw())
    expect(host.querySelectorAll('.sess').length).toBe(1)
    expect(screen.getByText('GTM research')).toBeTruthy()
  })

  it('answers a search with hits and with the empty note', () => {
    const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })], query: 'gtm' })
    const host = mount()
    expect(screen.getByText('gui.rail.search_hits {"n":1}')).toBeTruthy()
    expect(host.querySelectorAll('.sess').length).toBe(1)
    h.state.query = 'zzz'
    act(() => store.draw())
    expect(screen.getByText('gui.rail.no_hits {"q":"zzz"}')).toBeTruthy()
    expect(host.querySelector('.sess')).toBeNull()
  })

  it('opens another session through the shell and repaints the mark', () => {
    const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })], cur: 'a' })
    const host = mount()
    act(() => {
      rowByTitle(host, 'second task').click()
    })
    expect(h.calls).toContainEqual(['showPage', null])
    expect(h.calls).toContainEqual(['setCur', 'b'])
    expect(h.calls).toContainEqual(['openSession', 'b'])
    expect(rowByTitle(host, 'second task').getAttribute('aria-current')).toBe('true')
    h.calls.length = 0
    act(() => {
      rowByTitle(host, 'second task').click()
    })
    expect(h.calls).toContainEqual(['showPage', null])
    expect(h.calls.find((c) => c[0] === 'openSession')).toBeUndefined()
  })

  it('pins through the row menu, optimistically and persisted', () => {
    const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })] })
    const host = mount()
    act(() => {
      rowByTitle(host, 'second task').querySelector<HTMLElement>('.more')!.click()
    })
    const pin = h.menus[0]!.find((x) => x !== '-' && x.label === 'gui.sess.pin') as MenuItem
    act(() => pin.fn())
    expect(h.state.rows[1]!.pin).toBe(true)
    expect(h.calls).toContainEqual(['pinPersist', { id: 'b', pinned: true }])
    expect(h.toasts.map((x) => x.text)).toContain('gui.pinned_ok')
    expect(screen.getByText('gui.rail.pinned')).toBeTruthy()
  })

  it('deletes through the shell verb and restores on undo', () => {
    const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })], cur: 'b' })
    const host = mount()
    act(() => {
      rowByTitle(host, 'second task').querySelector<HTMLElement>('.more')!.click()
    })
    const del = h.menus[0]!.find((x) => x !== '-' && x.bad) as MenuItem
    act(() => del.fn())
    expect(h.calls).toContainEqual(['removeSession', 'b'])
    expect(h.calls).toContainEqual(['dropDraft', 'b'])
    expect(h.calls).toContainEqual(['setCur', 'a'])
    expect(h.calls).toContainEqual(['openSession', 'a'])
    expect(screen.queryByText('second task')).toBeNull()
    const undo = h.toasts.find((x) => x.action)!
    expect(undo.text).toBe('gui.sess.deleted_x {"title":"second task"}')
    act(() => undo.action!.fn())
    expect(screen.getByText('second task')).toBeTruthy()
  })

  it('folds a group on its eyebrow and unfolds it again', () => {
    install({ rows: [row(), row({ id: 'b', title: 'second task' })] })
    const host = mount()
    const grp = [...host.querySelectorAll<HTMLElement>('.grp')].find((g) => g.textContent!.includes('gui.rail.recent'))!
    act(() => grp.click())
    expect(host.querySelector('.sess')).toBeNull()
    expect(
      [...host.querySelectorAll<HTMLElement>('.grp')]
        .find((g) => g.textContent!.includes('gui.rail.recent'))!
        .getAttribute('aria-expanded'),
    ).toBe('false')
    act(() => {
      ;[...host.querySelectorAll<HTMLElement>('.grp')].find((g) => g.textContent!.includes('gui.rail.recent'))!.click()
    })
    expect(host.querySelectorAll('.sess').length).toBe(2)
  })

  it('marks the rail button of the page that is up, over the new-task row', () => {
    install({ cur: '' })
    mount()
    /* Nothing covering the chat and no session: the draft row is current. */
    act(() => store.markNew())
    expect(current('newBtn')).toBe('true')
    navUp('memPage')
    act(() => store.markNew())
    expect(current('memBtn')).toBe('true')
    expect(current('newBtn')).toBe('false')
    /* The capabilities page lights whichever capability button is showing;
       btnOf is the shell's answer, not a table the island keeps. */
    navUp('capsPage')
    act(() => store.markNew())
    expect(current('skillBtn')).toBe('true')
    expect(current('memBtn')).toBe('false')
  })

  it('hands the mark to the More row while the group is open, and takes it back when folded', () => {
    install({ cur: 'a' })
    mount()
    const fly = document.getElementById('moreFly')!
    fly.innerHTML = '<button class="mrow"></button><button class="mrow"></button><button class="mrow"></button>'
    navUp('cronPage')
    fly.dataset.open = 'true'
    act(() => store.markNew())
    const rows = [...fly.querySelectorAll('.mrow')].map((b) => b.getAttribute('aria-current'))
    /* morePages is [xa, conn, cron]: the third row is the page that is up. */
    expect(rows).toEqual(['false', 'false', 'true'])
    expect(current('moreBtn')).toBe('false')
    /* Folded, the group has to stand in for the page it hides. */
    fly.dataset.open = 'false'
    act(() => store.markNew())
    expect(current('moreBtn')).toBe('true')
  })

  it('caps the recent group and expands the tail behind one row', () => {
    const rows = Array.from({ length: 17 }, (_, i) => row({ id: 'r' + i, title: 'task ' + i }))
    install({ rows, cur: 'r0' })
    const host = mount()
    expect(host.querySelectorAll('.sess').length).toBe(15)
    const more = host.querySelector<HTMLElement>('.grp-more')!
    expect(more.textContent).toBe('gui.rail.expand_rest {"n":2}')
    act(() => more.click())
    expect(host.querySelectorAll('.sess').length).toBe(17)
    const fold = host.querySelector<HTMLElement>('.grp-more')!
    expect(fold.textContent).toBe('gui.rail.collapse')
    act(() => fold.click())
    expect(host.querySelectorAll('.sess').length).toBe(15)
  })
})
