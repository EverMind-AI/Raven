// @vitest-environment happy-dom
/* The workspace pane as the page root renders it.
 *
 * The shape is pinned by the region golden (src/test/regions.test.ts, which
 * renders page.html's body plus this root) and by both boot goldens, so nothing
 * here re-states it. What is here is what a golden of tags, ids, classes and
 * data-* cannot see: the roles and served flags that are not data-*, the box
 * handed over to three islands, which control drives what, the keyboard
 * shortcuts on a container the page still carries, and that the literals come
 * from the catalogue rather than from a copy in the JSX.
 */
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as desk from '../features/desk/store'
import * as workspace from '../features/workspace/store'
import * as lang from '../state/lang'
import * as ws from '../state/ws'
import { mountPageRoot } from '../test/pageRoot'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* Nothing: the page root renders the grid, the chat header's toggle with its
   badge, and this column, so a case gets all of them by mounting it. */
const MARKUP = ''

let unmount = (): void => {}
let picked: string[] = []
let desks: string[] = []

function render(): void {
  unmount()
  document.body.innerHTML = MARKUP
  unmount = mountPageRoot()
}

const el = (id: string): HTMLElement => document.getElementById(id) as HTMLElement
const pane = (): HTMLElement => el('ws')

beforeEach(() => {
  picked = []
  desks = []
  vi.spyOn(workspace, 'shared').mockReturnValue({ changes: [], urls: [], file: null, turn: 0, unseen: 0 })
  /* The pick is read off the state rather than off the call, because the tab
     strip's click and the keyboard both go through the store. */
  vi.spyOn(workspace, 'draw').mockImplementation(() => { picked.push(ws.tab) })
  vi.spyOn(desk, 'notifyDesk').mockImplementation(() => {})
  vi.spyOn(desk, 'toggleDesk').mockImplementation(() => { desks.push('toggle') })
  render()
})

afterEach(() => {
  ws.restore('diff', false)
  unmount()
  unmount = () => {}
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('the workspace pane', () => {
  it('renders the column with the two children page.html had, in order', () => {
    expect(Array.from(pane().children).map((child) => child.id || child.className)).toEqual([
      'ws-top',
      'wsBody',
    ])
    expect(pane().querySelector('.ws-top')!.children).toHaveLength(2)
  })

  it('renders every id the chrome, the islands and the writers reach for, once each', () => {
    for (const id of ['wsTabs', 'wsUnseen', 'wsAgentRun', 'wsWide', 'wsClose', 'wsBody']) {
      expect(document.querySelectorAll(`#${id}`), id).toHaveLength(1)
    }
  })

  /* What the goldens drop: they record tag, id, class and data-*, so a deleted
     role, a deleted flag or a blanked literal all pass them. */
  it('keeps the roles, the flags and the served states that are not data-*', () => {
    expect(el('wsTabs').getAttribute('role')).toBe('tablist')
    const tabs = [...el('wsTabs').children]
    expect(tabs.map((b) => b.getAttribute('role'))).toEqual(['tab', 'tab', 'tab'])
    expect(tabs.map((b) => (b as HTMLElement).dataset.w)).toEqual(['diff', 'browser', 'agents'])
    /* The diff view is the one the page is served showing. */
    expect(tabs.map((b) => b.getAttribute('aria-selected'))).toEqual(['true', 'false', 'false'])
    expect(el('wsWide').getAttribute('aria-pressed')).toBe('false')
    /* aria-expanded is what paints the toggle as engaged, and nothing has ever
       written this one. */
    expect(el('wsClose').getAttribute('aria-expanded')).toBe('true')
    expect(el('wsUnseen').hidden).toBe(true)
    expect(el('wsAgentRun').hidden).toBe(true)
    for (const svg of pane().querySelectorAll('svg')) {
      expect(svg.getAttribute('aria-hidden')).toBe('true')
    }
  })

  it('renders a literal in every element that carried one', () => {
    const words = [...pane().querySelectorAll('.lb')].map((n) => n.textContent)
    expect(words).toHaveLength(3)
    for (const word of words) expect(word).not.toBe('')
  })

  /* Shared ground: the workspace island roots itself in #wsBody and the browser
     and sub-agent views are handed the cleared box, so React must not own that
     child list. The two counters inside the strip belong to the bump and to the
     sub-agents island, so they are served empty here. */
  it('hands the body and the two counters over empty', () => {
    for (const id of ['wsBody', 'wsUnseen', 'wsAgentRun']) {
      expect(el(id).childNodes, id).toHaveLength(0)
    }
  })

  /* A language flip is the only thing that makes this pane render again, so it
     is the trigger here -- and it is the served language rather than the other
     one, because the flip to English is what the case at the foot of this file
     needs to still be able to make. */
  it('leaves what an island appended to the body alone when the pane renders again', () => {
    const box = document.createElement('div')
    box.className = 'wsview'
    el('wsBody').appendChild(box)
    act(() => {
      lang.set('zh')
    })
    expect(el('wsBody').firstElementChild).toBe(box)
  })
})

describe('the pane controls', () => {
  /* One delegated click on the row, as the legacy handler had it: each button
     names its own view. */
  it('picks the view of whichever tab was clicked', () => {
    for (const b of [...el('wsTabs').children]) {
      act(() => {
        (b as HTMLElement).click()
      })
    }
    expect(picked).toEqual(['diff', 'browser', 'agents'])
  })

  /* The row has padding of its own, so a click can land on it rather than on a
     tab -- and it has to pick nothing AND raise nothing. The raising is the
     half worth asserting: happy-dom reports a handler that throws as an error
     on the window rather than letting it out of dispatchEvent, so a missing
     guard would read as a harmless no-op here. */
  it('picks nothing off the row itself, and raises nothing', () => {
    const errors: unknown[] = []
    const onError = (e: Event): void => { errors.push((e as ErrorEvent).message || e.type) }
    window.addEventListener('error', onError)
    try {
      act(() => {
        el('wsTabs').click()
      })
    } finally {
      window.removeEventListener('error', onError)
    }
    expect(picked).toEqual([])
    expect(errors).toEqual([])
  })

  it('expands to the window and back from its own button', () => {
    act(() => {
      el('wsWide').click()
    })
    expect(ws.wide).toBe(true)
    expect(el('split').dataset.full).toBe('true')
    act(() => {
      el('wsWide').click()
    })
    expect(ws.wide).toBe(false)
  })

  it('collapses the pane from the toggle in its own corner', () => {
    ws.setOpen(true)
    act(() => {
      el('wsClose').click()
    })
    expect(ws.open).toBe(false)
    expect(el('split').dataset.open).toBe('false')
  })

  /* The chat header's toggle is not this component's markup -- the header is a
     region of its own -- so the pane binds it by id. React leaves an empty
     onclick on every element it takes a click of, and this one is not that:
     the handler on it is the pane's. */
  it('opens the desk from the toggle in the chat header', () => {
    expect(el('wsBtn').onclick).not.toBe(null)
    act(() => {
      el('wsBtn').click()
    })
    expect(desks).toEqual(['toggle'])
  })
})

describe('the keyboard shortcuts on the pane', () => {
  const press = (init: KeyboardEventInit, target: Element = pane()): KeyboardEvent => {
    const e = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init })
    act(() => {
      target.dispatchEvent(e)
    })
    return e
  }

  it('picks a view from 1 to 4', () => {
    for (const key of ['1', '2', '3', '4']) press({ key })
    expect(picked).toEqual(['diff', 'file', 'browser', 'agents'])
  })

  it('takes the keystroke, so the digit does not reach anything else', () => {
    expect(press({ key: '1' }).defaultPrevented).toBe(true)
  })

  it('leaves a shortcut, an unmapped key and a composition alone', () => {
    press({ key: '1', metaKey: true })
    press({ key: '2', ctrlKey: true })
    press({ key: '3', altKey: true })
    press({ key: '9' })
    press({ key: '1', isComposing: true })
    expect(picked).toEqual([])
  })

  /* A digit typed into the sub-agent view's own field is a digit, not a view. */
  it('leaves a digit typed into a field alone', () => {
    const field = document.createElement('input')
    el('wsBody').appendChild(field)
    press({ key: '1' }, field)
    expect(picked).toEqual([])
  })
})

/* Last in the file on purpose: applying a language is module state for
   everything after it. Same claim as the rail's: every word here is the
   catalogue's, read at render time, so the strip cannot come back in the
   language before the flip. A re-render alone would not show it: React diffs
   against the props it rendered last, so a value it never changes is a value it
   never writes again, and only a remount asks the component what the text
   is. */
describe('the pane once a language is applied', () => {
  it('renders the applied words when the column is mounted again', () => {
    const words = (): (string | null)[] => [...pane().querySelectorAll('.lb')].map((n) => n.textContent)
    const served = words()
    act(() => {
      lang.set('en')
    })
    const applied = words()
    expect(applied).not.toEqual(served)
    render()
    expect(words()).toEqual(applied)
  })
})
