// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { _resetForTests as sessionReset, setCurrent } from '../../lib/session'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import { _resetForTests as draftsReset, read, slot } from '../../state/sheetDrafts'
import { _resetForTests, forget, sync } from '../../state/sheetRack'
import { mountPageRoot } from '../../test/pageRoot'
import { open as approveOpen, openApproval } from './approve'
import { close, open } from './clarify'

import type { ClarifyRequest } from './clarify'


function wire(): void {
  setTranslator((key) => key)
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
  document.body.innerHTML =
    '<div class="chat"><div class="dock"><div class="sheets" id="sheetRack"></div>'
    + '<div class="dock-in"></div></div></div>'
  /* The sheets render from the page's own root (src/chrome/SheetRack.tsx), so it
     has to be standing before one is raised. */
  unmount = mountPageRoot()
}

let unmount: (() => void) | null = null

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
const chips = (): HTMLButtonElement[] =>
  [...rack().querySelectorAll<HTMLButtonElement>('.cp-steps .cp-step')]
const back = (): HTMLButtonElement => rack().querySelector<HTMLButtonElement>('.foot .cp-back')!
const skipper = (): HTMLButtonElement =>
  rack().querySelector<HTMLButtonElement>('.foot .btn:not(.key):not(.cp-back)')!
const asked = (): string => rack().querySelector('.hd .q')!.textContent!
const on = (): boolean[] => opts().map((o) => o.classList.contains('cp-on'))

const SKIPPED = 'gui.clarify.skipped_msg'

/* What `ask_user` sends for three questions asked in one call: every one of
   them carries its own options, which is what makes the sheet a form. */
const BATCH = [
  { question: 'which build?', header: 'build', choices: ['debug', 'release'] },
  { question: 'which targets?', header: 'targets', choices: ['mac', 'linux'], multi_select: true },
  { question: 'anything else?', header: 'notes', choices: ['no'], recommended: 'no' },
]

/* The frame for one of those questions: its own fields on top, the batch under
   them, the way the broker sends it. */
const frame = (at = 0): ClarifyRequest => ({
  ...BATCH[at]!, batch: BATCH, index: at, request_id: 'q1', total: BATCH.length,
})

beforeEach(() => {
  sessionReset()
  setCurrent('a')
  _resetForTests()
  draftsReset()
  wire()
})

afterEach(() => {
  if (unmount) unmount()
  unmount = null
  sessionReset()
  resetTranslator()
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

  it('marks itself as asking, so whatever else is docked can step aside', () => {
    /* The turn is waiting on this answer, and the rack is shared -- a running
       graph is tall enough to push the question below the fold. */
    open({ question: 'q' }, () => {})

    expect(sheets()[0]!.dataset.asks).toBe('1')
  })

  it('takes the sheet down when the server says the question died', () => {
    /* A question the reader never answers is failed safe to its default on the
       server, and until it said so the sheet stayed up offering an answer
       nothing was waiting for any more. */
    const said: string[] = []
    open({ question: 'q', request_id: 'q1' }, (a) => said.push(...a))
    expect(sheets().length).toBe(1)

    close('q1')

    expect(sheets()).toEqual([])
    expect(said).toEqual([])
  })

  it('ignores a close for a question that is no longer the one on screen', () => {
    /* The server fail-safes a superseded question to its default, and that
       close can land after the next question is already up. */
    open({ question: 'q1', request_id: 'q1' }, () => {})
    open({ question: 'q2', request_id: 'q2' }, () => {})

    close('q1')

    expect(sheets().length).toBe(1)
  })

  it('takes down a question raised in a conversation the reader left', () => {
    /* The rack keeps a sheet per conversation, so two questions coexist: the
       one on screen and one raised for a turn the reader stepped away from.
       That second one is what this whole close exists for -- a sub-agent asks
       after the turn that spawned it has replied -- so a close naming it has to
       find it even though it is not the newest. */
    open({ question: 'qA', request_id: 'qA', conversation_id: 'a' }, () => {})
    open({ question: 'qB', request_id: 'qB', conversation_id: 'b' }, () => {})
    setCurrent('a')
    sync()
    expect(sheets().length).toBe(1)

    close('qA')
    sync()

    expect(sheets()).toEqual([])
  })

  it('answers with the chosen option and takes the sheet down', () => {
    const said: string[] = []
    open({ question: 'q', choices: ['one', 'two'] }, (a) => said.push(...a))
    opts()[1]!.click()
    expect(said).toEqual(['two'])
    expect(sheets()).toEqual([])
  })

  it('answers with the typed text, trimmed, and not before there is any', () => {
    const said: string[] = []
    open({ question: 'q' }, (a) => said.push(...a))
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
    open({ question: 'q' }, (a) => said.push(...a))
    type('typed')
    field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(said).toEqual(['typed'])
  })

  /* Skipping is an answer, not a drop: the engine is waiting either way, and
     what it hears is this sheet's own wording for "no answer". */
  it('skips with its own wording, from either control', () => {
    const said: string[] = []
    open({ question: 'q' }, (a) => said.push(...a))
    rack().querySelector<HTMLElement>('.hd .ic:last-child')!.click()
    expect(said).toEqual(['gui.clarify.skipped_msg'])
    open({ question: 'q2' }, (a) => said.push(...a))
    rack().querySelector<HTMLElement>('.foot .btn:not(.key)')!.click()
    expect(said).toEqual(['gui.clarify.skipped_msg', 'gui.clarify.skipped_msg'])
  })

  /* Number picking is for a reader whose caret has left the field, which is
     where the sheet puts it on arrival -- so a number typed straight after the
     question appears is part of the typed answer, not a pick. Blurring is what
     tells the two cases apart. */
  it('picks a choice by number once the focus has left the field', () => {
    const said: string[] = []
    open({ question: 'q', choices: ['one', 'two'] }, (a) => said.push(...a))
    field().blur()
    key('1')
    expect(said).toEqual(['one'])
  })

  /* The number past the last choice is the field's own: it unfolds the sheet
     and puts the caret in it rather than answering anything. */
  it('sends the number past the last choice to the field', () => {
    const said: string[] = []
    open({ question: 'q', choices: ['one'] }, (a) => said.push(...a))
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
    open({ question: 'q', choices: ['one'] }, (a) => said.push(...a))
    field().focus()
    key('1')
    expect(said).toEqual([])
  })

  /* The Enter that commits a candidate is not the Enter that answers: the whole
     handler stands back while an input method is composing, the way the
     composer's own field does (features/composer/store.ts). */
  it('leaves an input method alone in the field', () => {
    const said: string[] = []
    open({ question: 'q' }, (a) => said.push(...a))
    type('typed')
    field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, isComposing: true }))
    field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, keyCode: 229 }))
    expect(said).toEqual([])
    field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(said).toEqual(['typed'])
  })

  /* What is typed into the field is not the page's to read: the chrome owns
     Escape and the slash palette's keys, and a document handler seeing them
     would act on an answer being written. */
  it('keeps what is typed in the field away from the page', () => {
    open({ question: 'q' }, () => {})
    let heard = 0
    const sentinel = (): void => { heard += 1 }
    document.addEventListener('keydown', sentinel)
    try {
      field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      field().dispatchEvent(new KeyboardEvent('keydown', { key: '/', bubbles: true }))
      expect(heard).toBe(0)
    } finally {
      document.removeEventListener('keydown', sentinel)
    }
  })

  /* A sheet parked with another conversation still holds a document handler,
     because the rack detaches the element rather than destroying it. */
  it('is not answerable by number while it is parked', () => {
    const said: string[] = []
    open({ question: 'q', choices: ['one'] }, (a) => said.push(...a))
    field().blur()
    setCurrent('b')
    sync()
    key('1')
    expect(said).toEqual([])
  })

  /* The half-typed answer is state, not a DOM leftover: the sheet's interior is
     unmounted while the reader is in another conversation, so what they typed
     has to be somewhere else by then. */
  it('keeps the half-typed answer in the draft store', () => {
    open({ question: 'q', request_id: 'q1' }, () => {})
    type('half a sen')
    expect(read(slot('a', 'q1')).steps?.[0]?.text).toBe('half a sen')
  })

  it('still has the half-typed answer after a conversation switch and back', () => {
    open({ question: 'q', request_id: 'q1' }, () => {})
    type('half a sen')
    setCurrent('b')
    sync()
    setCurrent('a')
    sync()
    expect(field().value).toBe('half a sen')
    /* And the submit button is live for it, rather than waiting for one more
       keystroke to notice there is an answer. */
    expect(submit().disabled).toBe(false)
  })

  /* A pending approval has no deadline behind it: taken down unanswered, its
     call waits until somebody presses stop. So a question docks above it
     rather than over it, and a landed approval -- answered, only saying so --
     goes the way any other sheet does. */
  it('leaves a pending approval standing and sweeps only what is not waiting', () => {
    const said: string[] = []
    openApproval({ approvalId: 'ap-1', command: 'rm file.txt', description: 'Delete' }, { onChoice: (c) => { said.push(c) } })
    open({ question: 'q1' }, () => {})
    expect(sheets().length).toBe(2)
    expect(sheets().map((el) => el.classList.contains('perm'))).toEqual([false, true])
    expect(said).toEqual([])

    /* Answered, the approval is a landed line, which the next question does replace. */
    sheets()[1]!.querySelector<HTMLButtonElement>('.cp-acts .opt:last-child')!.click()
    expect(said).toEqual(['allow'])
    open({ question: 'q2' }, () => {})
    expect(sheets().length).toBe(1)
    expect(sheets()[0]!.classList.contains('perm')).toBe(false)
  })

  /* A question that replaces the pending one starts empty: the draft belonged to
     the question the reader was answering, not to the conversation. */
  it('does not hand a new question the retired one\'s draft', () => {
    open({ question: 'q1' }, () => {})
    type('half a sen')
    open({ question: 'q2' }, () => {})
    expect(field().value).toBe('')
    expect(submit().disabled).toBe(true)
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

  /* One pending question per conversation -- but an approval waiting beside it
     is not its to take down: nothing behind an approval ends it if nobody
     answers, so the question docks above and the approval keeps its place. The
     preview variant wears the same mark and gets the same treatment. */
  it('docks above a pending approval rather than replacing it, the preview variant too', () => {
    approveOpen('rm -rf build/', () => {}, () => {})
    expect(sheets().length).toBe(1)
    open({ question: 'q' }, () => {})
    expect(sheets().length).toBe(2)
    expect(sheets()[0]!.querySelector('.hd .q')!.textContent).toBe('q')
    expect(sheets()[1]!.querySelector('.what')!.textContent).toBe('rm -rf build/')
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

/* A batch whose questions all brought their options: one sheet the reader steps
   through, answered in one report. */
describe('the clarify sheet as a form', () => {
  it('draws a chip per question of the batch and asks the first', () => {
    open(frame(), () => {})

    expect(chips().map((c) => c.textContent)).toEqual(['build', 'targets', 'notes'])
    expect(asked()).toBe('which build?')
    expect(chips()[0]!.classList.contains('cp-on')).toBe(true)
    expect(chips()[0]!.getAttribute('aria-current')).toBe('step')
    expect(chips()[0]!.getAttribute('aria-label')).toBe('gui.clarify.step_aria')
    /* Nothing further along may be jumped to before the reader has been there. */
    expect(chips().map((c) => c.disabled)).toEqual([false, true, true])
  })

  /* The first answer used to reach the server the moment it was picked, which
     is what made going back impossible. */
  it('carries a pick on a middle step forward and reports nothing yet', () => {
    const said: string[][] = []
    open(frame(), (a) => said.push(a))

    opts()[0]!.click()

    expect(said).toEqual([])
    expect(asked()).toBe('which targets?')
    expect(chips()[0]!.classList.contains('cp-done')).toBe(true)
    expect(chips()[1]!.classList.contains('cp-on')).toBe(true)
    expect(sheets().length).toBe(1)
  })

  it('goes back to a step holding what was picked there, and forward again on a new pick', () => {
    const said: string[][] = []
    open(frame(), (a) => said.push(a))
    opts()[0]!.click()

    back().click()

    expect(asked()).toBe('which build?')
    expect(on()).toEqual([true, false])
    opts()[1]!.click()
    expect(asked()).toBe('which targets?')
    back().click()
    expect(on()).toEqual([false, true])
    expect(said).toEqual([])
  })

  it('jumps by chip to a step the reader has already been to', () => {
    open(frame(), () => {})
    opts()[0]!.click()

    chips()[0]!.click()

    expect(asked()).toBe('which build?')
    expect(on()).toEqual([true, false])
    chips()[1]!.click()
    expect(asked()).toBe('which targets?')
  })

  it('toggles a multi-select step rather than moving on, and joins what it holds', () => {
    const said: string[][] = []
    open(frame(), (a) => said.push(a))
    opts()[0]!.click()

    expect(rack().querySelector('.body .cp-hint')!.textContent).toBe('gui.clarify.multi_hint')
    expect(submit().textContent).toBe('gui.clarify.next')
    expect(submit().disabled).toBe(true)
    opts()[0]!.click()
    opts()[1]!.click()
    expect(asked()).toBe('which targets?')
    expect(on()).toEqual([true, true])
    expect(submit().disabled).toBe(false)
    opts()[0]!.click()
    expect(on()).toEqual([false, true])

    opts()[0]!.click()
    submit().click()
    opts()[0]!.click()
    submit().click()

    expect(said).toEqual([['debug', 'mac, linux', 'no']])
  })

  it('holds a pick on the last step until Submit, and reports it against the batch', () => {
    const said: string[][] = []
    open(frame(), (a) => said.push(a))
    opts()[0]!.click()
    opts()[0]!.click()
    submit().click()

    expect(submit().textContent).toBe('gui.clarify.submit')
    opts()[0]!.click()

    expect(said).toEqual([])
    expect(sheets().length).toBe(1)
    expect(on()).toEqual([true])

    submit().click()

    expect(said).toEqual([['debug', 'mac', 'no']])
    expect(sheets()).toEqual([])
  })

  it('skips one step of the form and reports the rest', () => {
    const said: string[][] = []
    open(frame(), (a) => said.push(a))

    skipper().click()

    expect(asked()).toBe('which targets?')
    expect(chips()[0]!.classList.contains('cp-skip')).toBe(true)
    opts()[0]!.click()
    submit().click()
    skipper().click()

    expect(said).toEqual([[SKIPPED, 'mac', SKIPPED]])
  })

  it('picks by number and walks the batch with the arrows', () => {
    const said: string[][] = []
    open(frame(), (a) => said.push(a))
    field().blur()

    key('1')
    expect(asked()).toBe('which targets?')
    key('1')
    key('2')
    expect(on()).toEqual([true, true])

    key('ArrowLeft')
    expect(asked()).toBe('which build?')
    key('ArrowRight')
    expect(asked()).toBe('which targets?')
    expect(said).toEqual([])
  })

  /* The composer is one element below the sheet, and a reader typing into it is
     not answering: an arrow key there moves their caret, not the form. */
  it('leaves the keys alone while any field has the caret', () => {
    const said: string[][] = []
    open(frame(), (a) => said.push(a))
    field().blur()
    key('1')
    const box = document.createElement('textarea')
    document.body.appendChild(box)
    box.focus()

    key('ArrowLeft')
    expect(asked()).toBe('which targets?')

    key('1')
    expect(on()).toEqual([false, false])

    expect(said).toEqual([])
    box.remove()
  })

  /* A producer that sends the question texts alone -- the elicitor -- has no
     form to fill in: the sheet answers the one question it was sent, and the
     chips only say how far along the batch is. */
  it('asks a batch without options one question at a time', () => {
    const said: string[][] = []
    open({
      question: 'second?', request_id: 'q1', index: 1, total: 3,
      batch: [{ question: 'first?' }, { question: 'second?' }, { question: 'third?' }],
    }, (a) => said.push(a))

    expect(chips().map((c) => c.textContent)).toEqual(['1', '2', '3'])
    expect(chips().map((c) => c.disabled)).toEqual([true, false, true])
    expect(submit().textContent).toBe('gui.clarify.submit')
    type('later')
    submit().click()

    expect(said).toEqual([['', 'later']])
  })

  /* A question answered before this sheet was raised is not this sheet's to
     answer again: its position is reported empty and the caller drops it. */
  it('reports an empty answer for the positions it did not ask', () => {
    const said: string[][] = []
    open(frame(1), (a) => said.push(a))

    expect(chips()[0]!.disabled).toBe(true)
    expect(chips()[0]!.classList.contains('cp-done')).toBe(true)
    expect(asked()).toBe('which targets?')
    opts()[0]!.click()
    submit().click()
    opts()[0]!.click()
    submit().click()

    expect(said).toEqual([['', 'mac', 'no']])
  })

  it('comes back to the step and the picks the reader left', () => {
    open(frame(), () => {})
    opts()[0]!.click()
    opts()[1]!.click()
    type('and bsd')

    setCurrent('b')
    sync()
    setCurrent('a')
    sync()

    expect(asked()).toBe('which targets?')
    expect(on()).toEqual([false, true])
    expect(field().value).toBe('and bsd')
    back().click()
    expect(on()).toEqual([true, false])
  })

  it('marks the option the tool would pick itself', () => {
    open({ question: 'q', choices: ['one', 'two'], recommended: 'two' }, () => {})

    expect(opts()[1]!.querySelector('.cp-rec')!.textContent).toBe('gui.clarify.recommended')
    expect(opts()[0]!.querySelector('.cp-rec')).toBe(null)
  })

  /* One question, several answers: the pick cannot report on its own, so this
     is the shape that needs the button even without a batch behind it. */
  it('waits for Submit on a single multi-select question', () => {
    const said: string[][] = []
    open({ question: 'which targets?', choices: ['a', 'b'], multi_select: true }, (a) => said.push(a))

    opts()[0]!.click()
    opts()[1]!.click()

    expect(said).toEqual([])
    expect(chips()).toEqual([])
    expect(sheets().length).toBe(1)

    submit().click()

    expect(said).toEqual([['a, b']])
  })
})
