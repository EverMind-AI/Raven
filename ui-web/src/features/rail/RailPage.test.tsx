// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { RailApp } from './RailPage'
import * as store from './store'
import {
  _resetForTests as sessionReset,
  current as sessionCurrent,
  onChange,
  setCurrent,
} from '../../shell/session'

import type { Shell } from '../../shell/bridge'
import type { MenuItem } from '../../shell/menu'
import type { ToastAction } from '../../shell/toast'
import type { RailSnapshot, RailSource, SessRow } from './types'

/* The search term is the find row's, not the snapshot's, so the island reads it
   straight out of shell/find. Stubbed here rather than mounting that row's
   markup: this file asks what the LIST does with a term, and find.test.ts asks
   how the row produces one. */
const found = vi.hoisted(() => ({ term: '' }))
const toastWriter = vi.hoisted(() => ({ items: [] as Array<{ text: string; action?: ToastAction }> }))
vi.mock('../../shell/find', () => ({ term: () => found.term }))
vi.mock('../../shell/toast', () => ({
  show: (text: string, action?: ToastAction) => { toastWriter.items.push({ text, action }) },
}))

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

function row(over: Partial<SessRow> = {}): SessRow {
  return { id: 'a', title: 'GTM research', last: 'made a table', when: '11:24', pin: false, ...over }
}

interface Harness {
  state: RailSnapshot
  calls: Array<[string, unknown]>
  toasts: Array<{ text: string; action?: ToastAction }>
}

/* The island runs against the same two seams production wires: a fake shell
   on window.RavenShell (T returns its key, so tests assert catalogue keys)
   and a snapshot source on window.DS.sessions. */
function install(over: Partial<RailSnapshot> = {}): Harness {
  const state: RailSnapshot = { rows: [row()], cur: 'a', busy: false, ...over }
  const calls: Array<[string, unknown]> = []
  const toasts: Array<{ text: string; action?: ToastAction }> = []
  toastWriter.items = toasts
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: id => calls.push(['showPage', id]),
    navState: () => ({ pages: [], btnOf: () => undefined })
  }
  window.RavenShell = fakeShell
  sessionReset()
  setCurrent(state.cur)
  onChange((id) => {
    state.cur = id
    store.draw()
  })
  window.DS = { sessions: {
    snapshot: () => state,
    replace: (rows: SessRow[]) => { state.rows = rows },
    open: (s: SessRow) => calls.push(['openSession', s.id]),
  } }
  document.body.innerHTML =
    '<div class="app" data-page="off">' +
    '<button id="newBtn"></button><button id="skillBtn"></button>' +
    '<button id="plugBtn"></button><button id="memBtn"></button><button id="moreBtn"></button>' +
    '<div id="moreFly" data-open="false"></div>' +
    PAGES.map(p => `<div id="${p}" data-open="false"></div>`).join('') +
    '<div id="list"></div><h1 id="title">t</h1><button id="renameBtn"></button></div>'
  return { state, calls, toasts }
}

/* The installed source, for the cases that add a write verb to it. */
const src = (): RailSource => window.DS!.sessions as RailSource

/* The nav the assembled page hands over (demo/155-bridge.js reads it off
   NAV_OF and MORE_ROWS): every module page, the rail button each one lights
   up, and the More group's rows in their drawn order. The default fake above
   hands over an empty one, which is the whole page shut. */
const PAGES = ['capsPage', 'xaPage', 'connPage', 'memPage', 'cronPage']
const BTN_OF: Record<string, string> = {
  capsPage: 'skillBtn',
  xaPage: 'moreBtn',
  connPage: 'moreBtn',
  memPage: 'memBtn',
  cronPage: 'moreBtn'
}

function navUp(open: string): void {
  window.RavenShell!.navState = () => ({
    pages: PAGES,
    btnOf: p => BTN_OF[p],
    morePages: ['xaPage', 'connPage', 'cronPage']
  })
  document.querySelector<HTMLElement>('.app')!.dataset.page = 'on'
  PAGES.forEach(p => {
    document.getElementById(p)!.dataset.open = String(p === open)
  })
}

const current = (id: string): string | null => document.getElementById(id)!.getAttribute('aria-current')

function mount(): HTMLElement {
  const host = document.getElementById('list')!
  render(<RailApp />, { container: host })
  act(() => store.draw())
  return host
}

const rowByTitle = (host: HTMLElement, title: string): HTMLElement =>
  [...host.querySelectorAll<HTMLElement>('.sess')].find(r => r.querySelector('.t')?.textContent === title)!

const rowItems = (host: HTMLElement, title: string): Array<MenuItem | '-'> =>
  (rowByTitle(host, title) as HTMLElement & { _ctx: () => Array<MenuItem | '-'> })._ctx()

afterEach(() => {
  cleanup()
  sessionReset()
  found.term = ''
  localStorage.clear()
})

describe('rail island', () => {
  it('keeps only an unsaved current row when a refresh cannot list it yet', () => {
    const pending = row({ id: 'pending', persisted: false, status: 'run' })
    const saved = row({ id: 'saved', persisted: true })
    const pendingResult = store.reconcileRows([pending], [saved], 'pending')
    expect(pendingResult.currentMissing).toBe(false)
    expect(pendingResult.rows.map(x => x.id)).toEqual(['pending', 'saved'])

    const deletedResult = store.reconcileRows([row({ id: 'gone', persisted: true })], [saved], 'gone')
    expect(deletedResult.currentMissing).toBe(true)
    expect(deletedResult.rows.map(x => x.id)).toEqual(['saved'])
  })

  it('retains client-only completion state without reviving stale pin data', () => {
    const [next] = store.reconcileRows(
      [row({ id: 'a', pin: true, persisted: true, status: 'done' })],
      [row({ id: 'a', pin: false, persisted: true })],
      'a'
    ).rows
    expect(next?.status).toBe('done')
    expect(next?.pin).toBe(false)
  })

  it('describes the complete state transition after deleting a session', () => {
    const a = row({ id: 'a' })
    const b = row({ id: 'b' })
    expect(store.removeSessionRow([a, b], 'a', 'b')).toEqual({ kind: 'unchanged', rows: [a] })
    expect(store.removeSessionRow([a, b], 'b', 'b')).toEqual({ kind: 'open', next: a, rows: [a] })
    expect(store.removeSessionRow([b], 'b', 'b')).toEqual({ kind: 'draft', rows: [] })
  })

  it('renders the groups and the rows, titles stripped of leading emoji', () => {
    install({
      rows: [
        row(),
        row({ id: 'p', title: 'pinned one', pin: true }),
        row({ id: 'k', title: 'daily digest', from: 'cron' }),
        row({ id: 'e', title: '🚀 Ship it' })
      ]
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
    const h = install({
      rows: [
        row(),
        row({ id: 'd', title: 'done one', status: 'done' }),
        row({ id: 'x', title: 'broken one', status: 'err' })
      ],
      busy: true
    })
    const host = mount()
    const run = rowByTitle(host, 'GTM research').querySelector('.w')!
    expect(run.getAttribute('data-sig')).toBe('run')
    expect(run.getAttribute('aria-label')).toBe('gui.sess.running')
    expect(run.querySelector('i')).toBeTruthy()
    const done = rowByTitle(host, 'done one').querySelector('.w')!
    expect(done.getAttribute('data-sig')).toBe('done')
    /* A failed turn reports from the same slot the running one does, not from a
       dot at the other end of the row. */
    const bad = rowByTitle(host, 'broken one').querySelector('.w')!
    expect(bad.getAttribute('data-sig')).toBe('err')
    expect(bad.getAttribute('aria-label')).toBe('gui.sess.failed')
    expect(bad.querySelector('i')).toBeTruthy()
    expect(rowByTitle(host, 'broken one').querySelector('.dot')).toBeNull()
    h.state.busy = false
    h.state.rows[1]!.status = null
    act(() => store.draw())
    expect(rowByTitle(host, 'GTM research').querySelector('[data-sig]')).toBeNull()
    expect(rowByTitle(host, 'done one').querySelector('[data-sig]')).toBeNull()
    expect(rowByTitle(host, 'broken one').querySelector('[data-sig="err"]')).toBeTruthy()
  })

  it('keeps a queued session on a leading dot: it is not the state of a turn', () => {
    install({ rows: [row({ id: 'q', title: 'queued one', status: 'que' })], busy: false, cur: null })
    const host = mount()
    expect(rowByTitle(host, 'queued one').querySelector('.dot.que')).toBeTruthy()
    expect(rowByTitle(host, 'queued one').querySelector('[data-sig]')).toBeNull()
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
    act(() => store.hold())
    const skels = host.querySelectorAll('.sess.skel')
    expect(skels.length).toBe(6)
    expect(skels[0]!.querySelectorAll('.sk').length).toBe(2)
    act(() => store.draw())
    expect(host.querySelectorAll('.sess.skel').length).toBe(6)
    act(() => store.release())
    expect(host.querySelector('.sess.skel')).toBeNull()
    expect(screen.getByText('GTM research')).toBeTruthy()
  })

  it('shows a placeholder instead of a title while the name is being generated', () => {
    /* The row is not loading -- the list is here. Only its name is coming, so
       the bar stands where the title goes and the timestamp keeps its slot. */
    install({ rows: [row({ naming: true, title: 'gui.new_task' })] })
    const host = mount()

    const bars = host.querySelectorAll('.sess .t .sk')
    expect(bars.length).toBe(1)
    expect(bars[0]!.getAttribute('aria-label')).toBe('gui.sess.naming')
    expect(screen.queryByText('gui.new_task')).toBeNull()
    expect(screen.getByText('11:24')).toBeTruthy()
  })

  it('leaves the placeholder no width of its own', () => {
    /* Width and flex belong to the stylesheet, not to this element. Two earlier
       versions sized the bar here and both were wrong for the same reason: a
       per-row inline size resolves against the title slot, whose width depends
       on how long the neighbouring timestamp is and shrinks again under hover.
       Only the height stays inline, since it is the one dimension the
       surrounding line box does not set. */
    install({ rows: [row({ naming: true, title: 'gui.new_task' })] })
    const host = mount()

    const bar = host.querySelector('.sess .t .sk') as HTMLElement
    expect(bar.parentElement!.classList.contains('t')).toBe(true)
    expect(bar.style.width).toBe('')
    expect(bar.style.maxWidth).toBe('')
    expect(bar.style.flex).toBe('')
    expect(bar.style.height).toBe('11px')
    /* The shimmer keyframes hang off `.skel .sk`, so an ancestor must carry it. */
    expect(bar.closest('.skel')).not.toBeNull()
  })

  it('draws the title once the name has landed', () => {
    install({ rows: [row({ naming: false, title: 'Cut a desktop release' })] })
    const host = mount()

    expect(host.querySelector('.sess .t .sk')).toBeNull()
    expect(screen.getByText('Cut a desktop release')).toBeTruthy()
  })

  it('keeps the last rows when the source cannot answer a draw', () => {
    install()
    const host = mount()
    expect(screen.getByText('GTM research')).toBeTruthy()
    window.DS = {
      sessions: {
        snapshot: () => {
          throw new Error('gone')
        }
      }
    }
    act(() => store.draw())
    expect(host.querySelectorAll('.sess').length).toBe(1)
    expect(screen.getByText('GTM research')).toBeTruthy()
  })

  it('answers a search with hits and with the empty note', () => {
    install({ rows: [row(), row({ id: 'b', title: 'second task' })] })
    found.term = 'gtm'
    const host = mount()
    expect(screen.getByText('gui.rail.search_hits {"n":1}')).toBeTruthy()
    expect(host.querySelectorAll('.sess').length).toBe(1)
    found.term = 'zzz'
    act(() => store.draw())
    expect(screen.getByText('gui.rail.no_hits {"q":"zzz"}')).toBeTruthy()
    expect(host.querySelector('.sess')).toBeNull()
  })

  /* The term is read at paint time, never captured: a redraw the find row did
     not trigger (a new session, a finished turn) must still filter. */
  it('reads the term on every draw, not once at mount', () => {
    install({ rows: [row(), row({ id: 'b', title: 'second task' })] })
    const host = mount()
    expect(host.querySelectorAll('.sess').length).toBe(2)
    found.term = 'second'
    act(() => store.draw())
    expect(host.querySelectorAll('.sess').length).toBe(1)
  })

  it('opens another session through the source and repaints the mark', () => {
    const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })], cur: 'a' })
    const host = mount()
    act(() => {
      rowByTitle(host, 'second task').click()
    })
    expect(h.calls).toContainEqual(['showPage', null])
    expect(sessionCurrent()).toBe('b')
    expect(h.calls).toContainEqual(['openSession', 'b'])
    expect(rowByTitle(host, 'second task').getAttribute('aria-current')).toBe('true')
    h.calls.length = 0
    act(() => {
      rowByTitle(host, 'second task').click()
    })
    expect(h.calls).toContainEqual(['showPage', null])
    expect(h.calls.find(c => c[0] === 'openSession')).toBeUndefined()
  })

  it('pins through the hover shortcut, optimistically and persisted', () => {
    const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })] })
    const pins: Array<[string, boolean]> = []
    src().pin = (id: string, pinned: boolean) => pins.push([id, pinned])
    const host = mount()
    act(() => rowByTitle(host, 'second task').querySelector<HTMLButtonElement>('.quick-pin')!.click())
    expect(h.state.rows[1]!.pin).toBe(true)
    expect(pins).toEqual([['b', true]])
    expect(h.toasts.map(x => x.text)).toContain('gui.pinned_ok')
    expect(screen.getByText('gui.rail.pinned')).toBeTruthy()
  })

  /* A page with nowhere to keep the flag still moves the row: the pin is
     optimistic, and the attempt to record it must not undo it. */
  it('pins with no source verb at all', () => {
    const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })] })
    const host = mount()
    expect(() =>
      act(() => rowByTitle(host, 'second task').querySelector<HTMLButtonElement>('.quick-pin')!.click())
    ).not.toThrow()
    expect(h.state.rows[1]!.pin).toBe(true)
  })

  it('archives through the hover shortcut and restores the local row on undo', () => {
    const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })] })
    const host = mount()
    const archive = rowByTitle(host, 'second task').querySelectorAll<HTMLButtonElement>('.quick button')[1]!
    act(() => archive.click())
    expect(host.querySelectorAll('.sess')).toHaveLength(1)
    const undo = h.toasts.find(x => x.action)!
    expect(undo.text).toBe('gui.sess.archived {"title":"second task"}')
    expect(undo.action!.label).toBe('gui.undo')
    act(() => undo.action!.fn())
    expect(screen.getByText('second task')).toBeTruthy()
  })

  it('hands archive navigation to a source that can persist it', () => {
    install({ rows: [row(), row({ id: 'b', title: 'second task' })] })
    const archived: string[] = []
    src().archive = s => archived.push(s.id)
    const host = mount()
    const archive = rowByTitle(host, 'second task').querySelectorAll<HTMLButtonElement>('.quick button')[1]!
    act(() => archive.click())
    expect(archived).toEqual(['b'])
    expect(screen.getByText('second task')).toBeTruthy()
  })

  it('renames a session inline on double click', () => {
    install({ rows: [row(), row({ id: 'b', title: 'second task' })], cur: 'b' })
    const renamed: Array<[string, string]> = []
    src().renamed = (id, title) => renamed.push([id, title])
    const host = mount()
    fireEvent.doubleClick(rowByTitle(host, 'second task'))
    const input = host.querySelector<HTMLInputElement>('input.ren')!
    fireEvent.change(input, { target: { value: 'renamed inline' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(renamed).toEqual([['b', 'renamed inline']])
    expect(rowByTitle(host, 'renamed inline')).toBeTruthy()
    expect(document.getElementById('title')!.textContent).toBe('renamed inline')
  })

  it('sizes the rename box from the name as it is drawn', () => {
    /* It used to be the width of the rail whatever it held -- a two-character
       title in a box eight times its length, which reads as a form field waiting
       to be filled in rather than a name being corrected.

       The width is not computed here: the wrapper renders `data-value` in the
       same grid cell with the same font and the column takes that width, so the
       browser measures the drawn text. Counting characters cannot -- the face is
       proportional, and `WWWW` needs four times what `iiii` does.

       So what this pins is the mechanism, which is all that is assertable
       without layout: the mirror carries exactly what the field holds, and the
       field declares no width of its own. Whether the resulting box fits the
       glyphs is a question only a real browser can answer, and is checked by
       hand there. */
    install({ rows: [row({ id: 'b', title: '你好' })], cur: 'b' })
    const host = mount()
    fireEvent.doubleClick(rowByTitle(host, '你好'))
    const input = host.querySelector<HTMLInputElement>('input.ren')!
    const sizer = host.querySelector<HTMLElement>('.rensize')!

    expect(sizer.dataset.value).toBe('你好')
    expect(input.style.width).toBe('')

    fireEvent.change(input, { target: { value: 'WWWW' } })
    expect(sizer.dataset.value).toBe('WWWW')

    fireEvent.change(input, { target: { value: 'a much longer name than that one' } })
    expect(sizer.dataset.value).toBe('a much longer name than that one')
  })

  /* A source that can delete gets the whole action, and the island does none
     of the local work -- no splice, no undo, no moving off the row. */
  it('hands the delete to a source that can do it', () => {
    const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })], cur: 'b' })
    localStorage.setItem('raven.gui.drafts', JSON.stringify({ b: { t: 'keep', at: 1 } }))
    const gone: string[] = []
    src().remove = (s: SessRow) => gone.push(s.id)
    const host = mount()
    act(() => (rowItems(host, 'second task').find(x => x !== '-' && x.bad) as MenuItem).fn())
    expect(gone).toEqual(['b'])
    expect(screen.getByText('second task')).toBeTruthy()
    expect(h.toasts.find(x => x.action)).toBeUndefined()
    expect(JSON.parse(localStorage.getItem('raven.gui.drafts') || '{}').b.t).toBe('keep')
  })

  it('deletes locally when no source can, and restores on undo', () => {
    const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })], cur: 'b' })
    localStorage.setItem('raven.gui.drafts', JSON.stringify({ b: { t: 'discard', at: 1 } }))
    const host = mount()
    const del = rowItems(host, 'second task').find(x => x !== '-' && x.bad) as MenuItem
    act(() => del.fn())
    expect(JSON.parse(localStorage.getItem('raven.gui.drafts') || '{}').b).toBeUndefined()
    expect(sessionCurrent()).toBe('a')
    expect(h.calls).toContainEqual(['openSession', 'a'])
    expect(screen.queryByText('second task')).toBeNull()
    const undo = h.toasts.find(x => x.action)!
    expect(undo.text).toBe('gui.sess.deleted_x {"title":"second task"}')
    act(() => undo.action!.fn())
    expect(screen.getByText('second task')).toBeTruthy()
  })

  /* The rename had no coverage at all, and the seam is the reason to give it
     some: the live layer used to persist it by hanging a blur listener off the
     input this island creates, which an Enter -- replacing that input while it
     still has focus -- could slip past entirely. Telling the source from
     inside the commit is what closes that. */
  describe('renaming the current session', () => {
    function edit(h: Harness): HTMLInputElement {
      const host = mount()
      const it_ = rowItems(host, 'second task').find(x => x !== '-' && x.label === 'gui.sess.rename') as MenuItem
      act(() => it_.fn())
      return document.querySelector<HTMLInputElement>('input.titin')!
    }

    function key(inp: HTMLInputElement, k: string): void {
      act(() => {
        inp.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }))
      })
    }

    const wire = (): Array<[string, string]> => {
      const said: Array<[string, string]> = []
      src().renamed = (id: string, title: string) => said.push([id, title])
      return said
    }

    it('tells the source on Enter, which is the case a blur listener missed', () => {
      const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })], cur: 'b' })
      const said = wire()
      const inp = edit(h)
      inp.value = 'renamed by hand'
      key(inp, 'Enter')
      expect(said).toEqual([['b', 'renamed by hand']])
      /* And the editor is gone, with the heading back. */
      expect(document.querySelector('input.titin')).toBeNull()
      expect(document.getElementById('title')!.textContent).toBe('renamed by hand')
    })

    it('tells the source on blur too', () => {
      const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })], cur: 'b' })
      const said = wire()
      const inp = edit(h)
      inp.value = 'renamed by leaving'
      act(() => inp.dispatchEvent(new FocusEvent('blur')))
      expect(said).toEqual([['b', 'renamed by leaving']])
    })

    it('says nothing on escape, or when the title did not change', () => {
      const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })], cur: 'b' })
      const said = wire()
      const inp = edit(h)
      inp.value = 'thrown away'
      key(inp, 'Escape')
      expect(said).toEqual([])
      expect(document.getElementById('title')!.textContent).toBe('second task')

      const again = edit(h)
      again.value = 'second task'
      key(again, 'Enter')
      expect(said).toEqual([])
    })

    it('renames with no source verb at all', () => {
      const h = install({ rows: [row(), row({ id: 'b', title: 'second task' })], cur: 'b' })
      const inp = edit(h)
      inp.value = 'offline rename'
      expect(() => key(inp, 'Enter')).not.toThrow()
      expect(h.state.rows[1]!.title).toBe('offline rename')
    })
  })

  it('puts the caret after the label it opens, not in front of it', () => {
    /* The eyebrow starts on the word the eye is looking for. Order rather than
       presence: a caret is in the row either way, and what was asked for is
       where it sits. The hairline stays last -- it is the one child that takes
       the remaining width, so anything after it would be pushed off the row. */
    install({ rows: [row()] })
    const host = mount()
    const grp = [...host.querySelectorAll<HTMLElement>('.grp')]
      .find(g => g.textContent!.includes('gui.rail.recent'))!
    expect([...grp.children].map(c => c.className)).toEqual(['lab', 'n', 'car', 'rule'])
  })

  it('folds a group on its eyebrow and unfolds it again', () => {
    install({ rows: [row(), row({ id: 'b', title: 'second task' })] })
    const host = mount()
    const grp = [...host.querySelectorAll<HTMLElement>('.grp')].find(g => g.textContent!.includes('gui.rail.recent'))!
    act(() => grp.click())
    expect(host.querySelector('.sess')).toBeNull()
    expect(
      [...host.querySelectorAll<HTMLElement>('.grp')]
        .find(g => g.textContent!.includes('gui.rail.recent'))!
        .getAttribute('aria-expanded')
    ).toBe('false')
    act(() => {
      ;[...host.querySelectorAll<HTMLElement>('.grp')].find(g => g.textContent!.includes('gui.rail.recent'))!.click()
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
    fly.innerHTML = '<button class="navi"></button><button class="navi"></button><button class="navi"></button>'
    navUp('cronPage')
    fly.dataset.open = 'true'
    act(() => store.markNew())
    const rows = [...fly.querySelectorAll('.navi')].map(b => b.getAttribute('aria-current'))
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
