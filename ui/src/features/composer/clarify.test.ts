// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { open as approveOpen } from './approve'
import { open } from './clarify'
import { _resetForTests, forget, sync } from './sheets'
import { _resetForTests as sessionReset, setCurrent } from '../../shell/session'

import type { Shell } from '../../shell/bridge'

function wire(): void {
  const shell: Shell = {
    T: (key) => key,
    toast: () => {},
    menuAt: () => {},
    confirmAsk: () => {},
    showPage: () => {},
  }
  window.RavenShell = shell
  document.body.innerHTML =
    '<div class="chat"><div class="dock"><div class="sheets" id="sheetRack"></div>'
    + '<div class="dock-in"></div></div></div>'
}

const rack = (): HTMLElement => document.getElementById('sheetRack')!
const sheets = (): HTMLElement[] => [...rack().querySelectorAll<HTMLElement>('.csheet')]
const opts = (): HTMLElement[] => [...rack().querySelectorAll<HTMLElement>('.opt')]
const field = (): HTMLInputElement => rack().querySelector<HTMLInputElement>('.other input')!
const submit = (): HTMLButtonElement => rack().querySelector<HTMLButtonElement>('.foot .btn.key')!
/* Capture phase, because that is where the sheet listens. */
const key = (k: string, over: Partial<KeyboardEventInit> = {}): void => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, ...over }))
}
const type = (v: string): void => {
  field().value = v
  field().dispatchEvent(new Event('input'))
}

beforeEach(() => {
  sessionReset()
  setCurrent('a')
  _resetForTests()
  wire()
})

afterEach(() => {
  sessionReset()
  delete window.RavenShell
  document.body.innerHTML = ''
})

describe('the clarify sheet', () => {
  it('quotes the question and numbers the choices, the free-text row last', () => {
    open({ question: 'which build?', choices: ['debug', 'release'] }, () => {})
    expect(sheets().length).toBe(1)
    expect(rack().querySelector('.hd .q')!.textContent).toBe('which build?')
    expect(opts().map((b) => b.textContent)).toEqual(['1debug', '2release'])
    expect(rack().querySelector('.other .n')!.textContent).toBe('3')
    expect(field().placeholder).toBe('gui.clarify.other_ph')
  })

  /* With nothing to pick from, the field is the whole answer and says so. */
  it('numbers the free-text row first when there are no choices', () => {
    open({ question: 'what should it be called?' }, () => {})
    expect(opts()).toEqual([])
    expect(rack().querySelector('.other .n')!.textContent).toBe('1')
    expect(field().placeholder).toBe('gui.clarify.ph')
  })

  /* The server names the conversation it is asking for, and that beats the one
     the reader happens to be looking at: the turn that asked may be parked. */
  it('files the sheet under the conversation the server named', () => {
    open({ question: 'q', conversation_id: 'b' }, () => {})
    expect(sheets()).toEqual([])
    setCurrent('b')
    sync()
    expect(sheets().length).toBe(1)
    expect(sheets()[0]!.dataset.sess).toBe('b')
  })

  it('answers with the chosen option and takes the sheet down', () => {
    const said: string[] = []
    open({ question: 'q', choices: ['one', 'two'] }, (a) => said.push(a))
    opts()[1]!.click()
    expect(said).toEqual(['two'])
    expect(sheets()).toEqual([])
  })

  it('answers with the typed text, trimmed, and not before there is any', () => {
    const said: string[] = []
    open({ question: 'q' }, (a) => said.push(a))
    expect(submit().disabled).toBe(true)
    submit().click()
    expect(said).toEqual([])
    type('  a name  ')
    expect(submit().disabled).toBe(false)
    submit().click()
    expect(said).toEqual(['a name'])
  })

  it('answers on Enter in the field', () => {
    const said: string[] = []
    open({ question: 'q' }, (a) => said.push(a))
    type('typed')
    field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(said).toEqual(['typed'])
  })

  /* Skipping is an answer, not a drop: the engine is waiting either way, and
     what it hears is this sheet's own wording for "no answer". */
  it('skips with its own wording, from either control', () => {
    const said: string[] = []
    open({ question: 'q' }, (a) => said.push(a))
    rack().querySelector<HTMLElement>('.hd .ic:last-child')!.click()
    expect(said).toEqual(['gui.clarify.skipped_msg'])
    open({ question: 'q2' }, (a) => said.push(a))
    rack().querySelector<HTMLElement>('.foot .btn:not(.key)')!.click()
    expect(said).toEqual(['gui.clarify.skipped_msg', 'gui.clarify.skipped_msg'])
  })

  /* Number picking is for a reader whose caret has left the field, which is
     where the sheet puts it on arrival -- so a number typed straight after the
     question appears is part of the typed answer, not a pick. Carried over from
     the legacy sheet unchanged; blurring is what the two cases apart. */
  it('picks a choice by number once the focus has left the field', () => {
    const said: string[] = []
    open({ question: 'q', choices: ['one', 'two'] }, (a) => said.push(a))
    field().blur()
    key('1')
    expect(said).toEqual(['one'])
  })

  /* The number past the last choice is the field's own: it unfolds the sheet
     and puts the caret in it rather than answering anything. */
  it('sends the number past the last choice to the field', () => {
    const said: string[] = []
    open({ question: 'q', choices: ['one'] }, (a) => said.push(a))
    field().blur()
    rack().querySelector<HTMLElement>('.hd .ic')!.click()
    expect(sheets()[0]!.dataset.fold).toBe('true')
    key('2')
    expect(said).toEqual([])
    expect(sheets()[0]!.dataset.fold).toBe('false')
    expect(document.activeElement).toBe(field())
  })

  /* A number typed into the field is part of the answer, not a pick. */
  it('leaves the number keys alone while the field has the caret', () => {
    const said: string[] = []
    open({ question: 'q', choices: ['one'] }, (a) => said.push(a))
    field().focus()
    key('1')
    expect(said).toEqual([])
  })

  /* A sheet parked with another conversation still holds a document handler,
     because the rack detaches the element rather than destroying it. */
  it('is not answerable by number while it is parked', () => {
    const said: string[] = []
    open({ question: 'q', choices: ['one'] }, (a) => said.push(a))
    field().blur()
    setCurrent('b')
    sync()
    key('1')
    expect(said).toEqual([])
  })

  it('folds and unfolds, and the question unfolds it too', () => {
    open({ question: 'q' }, () => {})
    const sheet = sheets()[0]!
    const fold = rack().querySelector<HTMLElement>('.hd .ic')!
    expect(sheet.dataset.fold).toBe('false')
    expect(fold.dataset.tip).toBe('gui.clarify.fold')
    fold.click()
    expect(sheet.dataset.fold).toBe('true')
    expect(fold.dataset.tip).toBe('gui.clarify.unfold')
    rack().querySelector<HTMLElement>('.hd .q')!.click()
    expect(sheet.dataset.fold).toBe('false')
  })

  /* One pending question per conversation, and the sweep is by class: an
     approval wears `.csheet` too, and two sheets stacked over the composer is
     two things to answer for one blocked turn. */
  it('replaces whatever was pending in its conversation, approval included', () => {
    approveOpen('rm -rf build/', () => {}, () => {})
    expect(sheets().length).toBe(1)
    open({ question: 'q' }, () => {})
    expect(sheets().length).toBe(1)
    expect(sheets()[0]!.querySelector('.hd .q')!.textContent).toBe('q')
  })

  /* The takedown the rack runs. Every exit has to reach it, including the one
     this module is never told about -- the conversation being deleted while its
     question is still on screen. */
  it('unregisters its key handler on every exit, deletion included', () => {
    type Listener = EventListenerOrEventListenerObject
    const live = new Set<Listener>()
    const realAdd = document.addEventListener.bind(document)
    const realRemove = document.removeEventListener.bind(document)
    const spy = (keep: (fn: Listener) => void, pass: typeof realAdd) =>
      ((t: string, fn: Listener, o?: boolean | object) => {
        if (t === 'keydown') keep(fn)
        pass(t as 'keydown', fn as EventListener, o as boolean)
      }) as typeof document.addEventListener
    document.addEventListener = spy((fn) => live.add(fn), realAdd)
    document.removeEventListener = spy((fn) => live.delete(fn), realRemove)
    try {
      open({ question: 'answered', choices: ['one'] }, () => {})
      expect(live.size).toBe(1)
      opts()[0]!.click()
      expect(live.size).toBe(0)

      open({ question: 'replaced' }, () => {})
      open({ question: 'replacing' }, () => {})
      expect(live.size).toBe(1)

      forget('a')
      expect(live.size).toBe(0)
    } finally {
      document.addEventListener = realAdd
      document.removeEventListener = realRemove
    }
  })
})
