// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { open as approveOpen, openApproval } from '../features/composer/approve'
import { open as clarifyOpen } from '../features/composer/clarify'
import { _resetForTests, add, forget, sync } from '../state/sheetRack'
import { _resetForTests as draftsReset } from '../state/sheetDrafts'
import { _resetForTests as sessionReset, setCurrent } from '../shell/session'
import { mountPageRoot } from '../test/pageRoot'
import { resetTranslator, setTranslator } from '../i18n/t'


let unmount: (() => void) | null = null

function wire(): void {
  setTranslator((key) => key)
  document.body.innerHTML =
    '<div class="chat"><div class="dock"><div class="sheets" id="sheetRack"></div>'
    + '<div class="dock-in"></div></div></div>'
  unmount = mountPageRoot()
}

const rack = (): HTMLElement => document.getElementById('sheetRack')!
const key = (k: string): void => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }))
}

/* The capture-phase handlers the sheets register, counted rather than inspected:
   one per pending question is what the page has today, parked or not. */
let captures = 0
const realAdd = document.addEventListener.bind(document)
const realRemove = document.removeEventListener.bind(document)
const capture = (o?: boolean | AddEventListenerOptions): boolean =>
  o === true || (!!o && typeof o === 'object' && !!o.capture)

beforeEach(() => {
  sessionReset()
  setCurrent('a')
  _resetForTests()
  draftsReset()
  wire()
  captures = 0
  document.addEventListener = ((t: string, fn: EventListener, o?: boolean | AddEventListenerOptions) => {
    if (t === 'keydown' && capture(o)) captures += 1
    realAdd(t as 'keydown', fn, o as boolean)
  }) as typeof document.addEventListener
  document.removeEventListener = ((t: string, fn: EventListener, o?: boolean | AddEventListenerOptions) => {
    if (t === 'keydown' && capture(o)) captures -= 1
    realRemove(t as 'keydown', fn, o as boolean)
  }) as typeof document.removeEventListener
})

afterEach(() => {
  document.addEventListener = realAdd
  document.removeEventListener = realRemove
  if (unmount) unmount()
  unmount = null
  sessionReset()
  resetTranslator()
  document.body.innerHTML = ''
})

describe('the sheet rack', () => {
  /* `.dock .sheets:has(> *)` puts the stack's frost, its padding and its radius
     on the rack itself (src/styles/page.css:4401-4406), so an empty rack has to
     be empty: a placeholder wrapper would leave all three lit over nothing. */
  it('has no children at all when nothing is docked', () => {
    expect(document.querySelectorAll('#sheetRack > *')).toHaveLength(0)
    expect(rack().childNodes).toHaveLength(0)
    clarifyOpen({ question: 'q', request_id: 'q1' }, () => {})
    expect(document.querySelectorAll('#sheetRack > *')).toHaveLength(1)
    forget('a')
    expect(document.querySelectorAll('#sheetRack > *')).toHaveLength(0)
    expect(rack().childNodes).toHaveLength(0)
  })

  /* One sheet is the rack's direct child, not a wrapper around it:
     `.dock .sheets > *` styles the flex item and the rack writes `data-sess` on
     what it was handed (page.css:4385-4393). */
  it('docks the sheet itself, with no wrapper of its own', () => {
    clarifyOpen({ question: 'q', request_id: 'q1' }, () => {})
    const child = rack().firstElementChild!
    expect(child.className).toBe('csheet')
    expect((child as HTMLElement).dataset.sess).toBe('a')
    expect(child.querySelector('.hd .q')!.textContent).toBe('q')
  })

  it('renders only the open conversation, and answers only that one by number', () => {
    const said: string[] = []
    clarifyOpen({ question: 'A', choices: ['one'], conversation_id: 'a', request_id: 'qa' },
      (a) => said.push(`A:${a}`))
    clarifyOpen({ question: 'B', choices: ['one'], conversation_id: 'b', request_id: 'qb' },
      (a) => said.push(`B:${a}`))

    /* Two pending questions, one on screen -- and two handlers, which is what
       the page has today: a parked sheet keeps its own. */
    expect(document.querySelectorAll('#sheetRack > *')).toHaveLength(1)
    expect(rack().querySelector('.hd .q')!.textContent).toBe('A')
    expect(captures).toBe(2)

    /* The caret is where the sheet put it on arrival, and a number typed there
       is part of the answer -- so the reader has left the field before any of
       this, exactly as in the sheet's own cases. */
    rack().querySelector<HTMLInputElement>('.other input')!.blur()
    key('1')
    expect(said).toEqual(['A:one'])
    expect(captures).toBe(1)

    setCurrent('b')
    sync()
    expect(rack().querySelector('.hd .q')!.textContent).toBe('B')
    /* Coming back takes no caret: only the question that has just arrived does,
       so the number keys answer the sheet the reader returned to. */
    expect(document.activeElement).toBe(document.body)
    key('1')
    expect(said).toEqual(['A:one', 'B:one'])
    expect(captures).toBe(0)
  })

  /* The rack's child list is mixed: the graph sheet is a host element with a
     React root of its own, and a question raised while one is running belongs
     above it -- the graph folds itself for exactly that reason
     (features/dag/mount.tsx). A portal into the rack would append instead, which
     puts the question the reader has to answer under the graph. */
  it('stacks a question raised over a running graph on top of it', () => {
    const graph = document.createElement('div')
    graph.className = 'dsheet'
    add(graph)
    clarifyOpen({ question: 'q', request_id: 'q1' }, () => {})

    expect([...rack().children].map((n) => n.className)).toEqual(['csheet', 'dsheet'])

    /* And the same order comes back on a return, where both are re-docked. */
    setCurrent('b')
    sync()
    setCurrent('a')
    sync()
    expect([...rack().children].map((n) => n.className)).toEqual(['csheet', 'dsheet'])
  })

  /* The question is still pending on the server, so coming back has to be the
     same sheet rather than a fresh one replaying its entrance animation
     (`animation: sheetUp` on `.csheet`, page.css:4407-4412). */
  it('brings the same element back when the reader returns', () => {
    clarifyOpen({ question: 'q', request_id: 'q1' }, () => {})
    const host = rack().firstElementChild as HTMLElement
    host.dataset.mark = 'first'

    setCurrent('b')
    sync()
    expect(document.querySelectorAll('#sheetRack > *')).toHaveLength(0)

    setCurrent('a')
    sync()
    expect(rack().firstElementChild).toBe(host)
    expect((rack().firstElementChild as HTMLElement).dataset.mark).toBe('first')
  })

  /* The fold the reader left the sheet in is on the sheet's own element, which
     the rack keeps, so it survives the interior being unmounted and mounted
     again. Rebuilt from the component's own initial state instead, a folded
     question would spring open every time the reader came back to it. */
  it('comes back folded when that is how the reader left it', () => {
    clarifyOpen({ question: 'q', choices: ['one'], request_id: 'q1' }, () => {})
    rack().querySelector<HTMLElement>('.hd .ic')!.click()
    expect((rack().firstElementChild as HTMLElement).dataset.fold).toBe('true')

    setCurrent('b')
    sync()
    setCurrent('a')
    sync()
    expect((rack().firstElementChild as HTMLElement).dataset.fold).toBe('true')
    expect(rack().querySelector<HTMLElement>('.hd .ic')!.dataset.tip).toBe('gui.clarify.unfold')
  })

  /* Who blocks the reader, as the rack reads it off the sheets themselves. The
     permission sheet does NOT mark itself -- so a graph docked beside it does
     not step aside, which is today's behaviour and is recorded as such in the
     plan's findings rather than fixed here. */
  it('marks the clarify question and the preview as asking, and the approval not', () => {
    clarifyOpen({ question: 'q', request_id: 'q1' }, () => {})
    expect((rack().firstElementChild as HTMLElement).dataset.asks).toBe('1')
    forget('a')

    approveOpen('rm -rf build/', () => {}, () => {})
    expect((rack().firstElementChild as HTMLElement).dataset.asks).toBe('1')
    forget('a')

    openApproval({ approvalId: 'ap-1', command: 'rm file.txt', description: 'Delete files' }, () => {})
    const sheet = rack().firstElementChild as HTMLElement
    expect(sheet.className).toBe('csheet perm')
    expect(sheet.dataset.asks).toBeUndefined()
    expect(sheet.hasAttribute('data-asks')).toBe(false)
  })
})
