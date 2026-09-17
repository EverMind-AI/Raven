// @vitest-environment happy-dom
/* The workspace pane's own state: the writes the six legacy verbs made, in the
 * order they made them.
 *
 * The order is the part worth pinning. Every one of these verbs ends by handing
 * over to the island (`draw`) or to the desk (`notifyDesk`), and both of those
 * read the DOM the verb has just written -- so the two fakes below snapshot the
 * document at the moment they are called, which is what says the writes landed
 * before the handover rather than after it.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { T } from '../i18n/t'
import { islands } from '../islands'
import * as lang from '../state/lang'
import { mountPageRoot } from '../test/pageRoot'
import * as ws from './ws'

import type { WsShared } from '../features/workspace/types'

/* The three containers page.html still carries: the grid the pane's flags land
   on, the chat header's toggle with its badge, and the column itself -- whose
   interior the page root renders (src/chrome/WsPane.tsx), which is where
   #wsTabs, #wsWide and #wsUnseen come from. */
const MARKUP = '<div class="app"><div class="split" id="split">'
  + '<div class="chat"><div class="top">'
  + '<button class="ghost-ic wstog tipdn" id="wsBtn" aria-expanded="false"'
  + ' data-i18n-tip="gui.expand_ws" data-i18n-aria="gui.expand_ws">'
  + '<span class="bdg" id="wsBdg" hidden>2</span></button>'
  + '</div></div>'
  + '<aside class="ws" id="ws" data-i18n-aria="gui.workspace"></aside>'
  + '</div></div>'

interface Snap {
  open?: string
  full?: string
  expanded: string | null
  selected: string[]
  badge: string
  badgeHidden: boolean
}

const el = (id: string): HTMLElement => document.getElementById(id) as HTMLElement
const split = (): HTMLElement => el('split')
const strip = (): string[] =>
  [...el('wsTabs').children].map((b) => b.getAttribute('aria-selected') ?? '')

const snap = (): Snap => ({
  open: split().dataset.open,
  full: split().dataset.full,
  expanded: el('wsBtn').getAttribute('aria-expanded'),
  selected: strip(),
  badge: el('wsBdg').textContent ?? '',
  badgeHidden: el('wsBdg').hidden,
})

const record: WsShared = { changes: [], urls: [], file: null, turn: 0, unseen: 0 }
let drew: Snap[] = []
let notified: Snap[] = []
let deskTabs: string[] = []
let resets: string[] = []
let unmount = (): void => {}

const real = { workspace: { ...islands.workspace }, subagents: { ...islands.subagents } }

beforeEach(() => {
  document.body.innerHTML = MARKUP
  unmount = mountPageRoot()
  record.changes = []
  record.urls = []
  record.unseen = 0
  drew = []
  notified = []
  deskTabs = []
  resets = []
  Object.assign(islands.workspace, {
    shared: () => record,
    draw: () => { drew.push(snap()) },
    notifyDesk: () => { notified.push(snap()) },
    openDeskTab: (tab: string) => { deskTabs.push(tab) },
    reset: () => { resets.push('workspace') },
  })
  Object.assign(islands.subagents, { reset: () => { resets.push('subagents') } })
})

/* Module state outlives a case, so it is put back while the markup it writes to
   is still standing: the collapse is what takes the expanded flag with it. */
afterEach(() => {
  ws.setOpen(false)
  ws.restore('diff', false)
  unmount()
  unmount = () => {}
  Object.assign(islands.workspace, real.workspace)
  Object.assign(islands.subagents, real.subagents)
  document.documentElement.classList.remove('desk-ready')
  document.body.innerHTML = ''
})

describe('the pane as the page serves it', () => {
  it('is collapsed, on the diff view, and nothing has written the grid', () => {
    expect(ws.open).toBe(false)
    expect(ws.tab).toBe('diff')
    expect(ws.wide).toBe(false)
    expect(ws.picked).toBe(false)
    expect(split().dataset.open).toBe(undefined)
    expect(split().dataset.full).toBe(undefined)
    expect(ws.view()).toEqual({ tab: 'diff', open: false, picked: false })
  })
})

describe('opening and collapsing the pane', () => {
  it('writes the grid, the header toggle and its wording before it draws', () => {
    ws.setOpen(true)
    expect(ws.open).toBe(true)
    expect(split().dataset.open).toBe('true')
    expect(el('wsBtn').getAttribute('aria-expanded')).toBe('true')
    expect(el('wsBtn').dataset.tip).toBe(T('gui.collapse_ws'))
    expect(el('wsBtn').getAttribute('aria-label')).toBe(T('gui.collapse_ws'))
    /* The island draws once, and the grid and the toggle were already written
       when it did. */
    expect(drew).toHaveLength(1)
    expect(drew[0]!.open).toBe('true')
    expect(drew[0]!.expanded).toBe('true')
  })

  it('counts after it draws, and the desk hears last', () => {
    record.changes = [{ seen: false }, { seen: false }] as unknown as WsShared['changes']
    ws.setOpen(true)
    /* Nothing had been counted when the view drew -- the badge still carried
       the placeholder page.html serves it with -- because the bump comes after
       the draw, which is the order the legacy verb had. */
    expect(drew[0]!.badge).toBe('2')
    expect(notified).toHaveLength(1)
    expect(notified[0]!.badge).toBe('+2')
    expect(el('wsBdg').textContent).toBe('+2')
  })

  it('leaves expanded mode on the way down, and does not draw', () => {
    ws.setOpen(true)
    ws.setFull(true)
    drew = []
    ws.setOpen(false)
    expect(split().dataset.open).toBe('false')
    expect(split().dataset.full).toBe('false')
    expect(ws.wide).toBe(false)
    expect(el('wsBtn').dataset.tip).toBe(T('gui.expand_ws'))
    expect(drew).toEqual([])
  })
})

describe('expanding the pane to the window', () => {
  it('writes the flag, the glyph class and both readings of the label', () => {
    ws.setFull(true)
    expect(split().dataset.full).toBe('true')
    const b = el('wsWide')
    expect(b.classList.contains('on')).toBe(true)
    expect(b.getAttribute('aria-pressed')).toBe('true')
    expect(b.dataset.tip).toBe(T('gui.ws.restore_panel'))
    expect(b.getAttribute('aria-label')).toBe(T('gui.ws.restore_panel'))
    ws.setFull(false)
    expect(b.classList.contains('on')).toBe(false)
    expect(b.getAttribute('aria-pressed')).toBe('false')
    expect(b.dataset.tip).toBe(T('gui.ws.expand_panel'))
  })
})

describe('picking a view', () => {
  it('marks the view it picked and unmarks the others', () => {
    expect(strip()).toEqual(['true', 'false', 'false'])
    ws.pick('agents')
    expect(ws.tab).toBe('agents')
    expect(ws.picked).toBe(true)
    expect(strip()).toEqual(['false', 'false', 'true'])
    ws.pick('browser')
    expect(strip()).toEqual(['false', 'true', 'false'])
  })

  /* The file view has no button of its own -- it is what a row in the diff view
     opens -- so picking it leaves every tab unselected. Reproduced, not fixed. */
  it('leaves the strip blank for the view with no tab', () => {
    ws.pick('file')
    expect(strip()).toEqual(['false', 'false', 'false'])
  })

  it('redraws the view, and bumps the epoch every answer in flight reads', () => {
    ws.pick('agents')
    expect(drew).toHaveLength(1)
    const mine = 0
    expect(ws.stale(mine)).toBe(true)
  })
})

describe('the pane state out and back', () => {
  /* A restore is not a pick, and the strip is repainted by draw() alone: the
     residency rule hands the state back before it decides whether to draw, so a
     conversation resumed with the pane collapsed leaves the strip showing the
     view it last drew. The next open repaints it. */
  it('does not repaint the strip while the pane is collapsed', () => {
    ws.restore('agents', true)
    expect(ws.view()).toEqual({ tab: 'agents', open: false, picked: true })
    expect(strip()).toEqual(['true', 'false', 'false'])
    ws.setOpen(true)
    expect(strip()).toEqual(['false', 'false', 'true'])
  })

  it('falls back to the diff view when it is handed nothing', () => {
    ws.restore('', false)
    expect(ws.tab).toBe('diff')
    expect(ws.picked).toBe(false)
  })
})

describe('the badge', () => {
  it('counts the changed files nobody has read, in both places', () => {
    record.changes = [{ seen: false }, { seen: true }] as unknown as WsShared['changes']
    ws.bump()
    expect(record.unseen).toBe(1)
    expect(el('wsBdg').textContent).toBe('+1')
    expect(el('wsBdg').hidden).toBe(false)
    expect(el('wsUnseen').textContent).toBe('+1')
    expect(el('wsUnseen').hidden).toBe(false)
  })

  it('empties and hides both when everything has been read', () => {
    record.changes = [{ seen: true }] as unknown as WsShared['changes']
    ws.bump()
    expect(el('wsBdg').textContent).toBe('')
    expect(el('wsBdg').hidden).toBe(true)
    expect(el('wsUnseen').textContent).toBe('')
    expect(el('wsUnseen').hidden).toBe(true)
  })
})

describe('a different session', () => {
  it('drops the picked view and clears both lists the pane shows', () => {
    ws.pick('agents')
    ws.reset()
    expect(ws.tab).toBe('diff')
    expect(ws.picked).toBe(false)
    expect(resets).toEqual(['workspace', 'subagents'])
  })
})

describe('whether a tool event is worth a redraw', () => {
  it('is what the pane shows, and never the sub-agent view', () => {
    expect(ws.showsTurn()).toBe(false)
    ws.setOpen(true)
    expect(ws.showsTurn()).toBe(true)
    ws.pick('agents')
    expect(ws.showsTurn()).toBe(false)
  })
})

describe('the window growing too narrow to split', () => {
  let query: MediaQueryList
  const change = (matches: boolean): void => {
    const e = new Event('change') as Event & { matches: boolean }
    e.matches = matches
    query.dispatchEvent(e)
  }

  beforeEach(() => {
    query = ws.watchNarrow()
  })

  it('takes a standing pane down', () => {
    ws.setOpen(true)
    change(true)
    expect(ws.open).toBe(false)
    expect(split().dataset.open).toBe('false')
  })

  /* No initial check and nothing to do when the pane is already collapsed: this
     is the window taking a standing pane down, not a rule about how narrow
     pages open. */
  it('does nothing to a collapsed one, or when the window grows back', () => {
    change(true)
    expect(split().dataset.open).toBe(undefined)
    ws.setOpen(true)
    change(false)
    expect(ws.open).toBe(true)
  })
})

describe('the floating desk', () => {
  beforeEach(() => {
    document.documentElement.classList.add('desk-ready')
  })

  /* Asking for a named view while the desk owns the workspace opens a window
     there instead, and the pane is left alone -- the panel's own flags must not
     move, or the next collapse would write a state nothing is showing. */
  it('takes the named view and leaves the pane untouched', () => {
    ws.setOpen(true, 'agents')
    expect(deskTabs).toEqual(['agents'])
    expect(ws.open).toBe(false)
    expect(split().dataset.open).toBe(undefined)
    ws.pick('agents')
    expect(deskTabs).toEqual(['agents', 'agents'])
    expect(ws.tab).toBe('diff')
  })

  /* The browser view has no desk window, so it stays a pane view even there. */
  it('keeps the browser view in the pane', () => {
    ws.pick('browser')
    expect(deskTabs).toEqual([])
    expect(ws.tab).toBe('browser')
  })
})

/* Last in the file on purpose: applying a language is module state for
   everything after it, and it is the one thing that makes the pane render
   again. The four writes on #wsWide are this module's, and the component
   renders that button -- so the button drawing again may not undo them. React
   diffs against the props it rendered last rather than against the document,
   which is what keeps one value to one writer. */
describe('the expanded flag once the pane has rendered again', () => {
  it('is still on the button the component drew', () => {
    ws.setFull(true)
    lang.set('en')
    expect(el('wsWide').classList.contains('on')).toBe(true)
    expect(el('wsWide').getAttribute('aria-pressed')).toBe('true')
    expect(el('split').dataset.full).toBe('true')
  })
})
