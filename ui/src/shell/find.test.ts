// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { install, onChange, term, toggle } from './find'

import type { Shell } from './bridge'

/* The same four elements page.html carries, with the same starting state: the
   row hidden, the clear button hidden, the button reporting collapsed. */
function markup(): void {
  document.body.innerHTML = `
    <button id="findBtn" aria-expanded="false"></button>
    <div class="find" id="findBox" hidden>
      <input id="sfind" />
      <button class="clr" id="sclr" hidden></button>
    </div>`
}

let draws = 0

function wire(): void {
  markup()
  draws = 0
  const shell: Shell = {
    T: (key) => key,
    confirmAsk: () => {},
    showPage: () => {},
  }
  window.RavenShell = shell
  onChange(() => {
    draws += 1
  })
  install()
}

const field = (): HTMLInputElement => document.getElementById('sfind') as HTMLInputElement
const clr = (): HTMLElement => document.getElementById('sclr')!
const box = (): HTMLElement => document.getElementById('findBox')!
const btn = (): HTMLElement => document.getElementById('findBtn')!

const type = (text: string): void => {
  field().value = text
  field().dispatchEvent(new Event('input'))
}

const key = (name: string, over: Partial<KeyboardEventInit> = {}): boolean =>
  field().dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true, ...over }))

beforeEach(wire)

afterEach(() => {
  onChange(() => {})
  delete window.RavenShell
  document.body.innerHTML = ''
})

describe('the search row', () => {
  it('trims and lowercases what was typed, and redraws once per keystroke', () => {
    type('  GTM Research ')
    expect(term()).toBe('gtm research')
    expect(draws).toBe(1)
    type('gtm')
    expect(term()).toBe('gtm')
    expect(draws).toBe(2)
  })

  it('shows the clear button exactly while there is something to clear', () => {
    expect(clr().hidden).toBe(true)
    type('a')
    expect(clr().hidden).toBe(false)
    /* Whitespace alone is not a term: it would filter every row out. */
    type('   ')
    expect(term()).toBe('')
    expect(clr().hidden).toBe(true)
  })

  it('clears the field, the term and itself, then puts the caret back', () => {
    type('gtm')
    draws = 0
    clr().click()
    expect(field().value).toBe('')
    expect(term()).toBe('')
    expect(clr().hidden).toBe(true)
    expect(draws).toBe(1)
    expect(document.activeElement).toBe(field())
  })
})

describe('the search row, opening and closing', () => {
  it('opens on the button, reports it, and takes the caret', () => {
    toggle()
    expect(box().hidden).toBe(false)
    expect(btn().getAttribute('aria-expanded')).toBe('true')
    expect(document.activeElement).toBe(field())
  })

  it('honours a forced state instead of flipping', () => {
    toggle(true)
    toggle(true)
    expect(box().hidden).toBe(false)
    toggle(false)
    expect(box().hidden).toBe(true)
    expect(btn().getAttribute('aria-expanded')).toBe('false')
  })

  it('drops a term when it closes, and costs nothing when there was none', () => {
    toggle(true)
    type('gtm')
    draws = 0
    toggle(false)
    expect(term()).toBe('')
    expect(field().value).toBe('')
    expect(clr().hidden).toBe(true)
    expect(draws).toBe(1)
    /* Closing an already-empty row must not redraw: the blur handler closes it
       on every click away, and the list would repaint for nothing. */
    toggle(true)
    draws = 0
    toggle(false)
    expect(draws).toBe(0)
  })

  it('closes on escape and keeps the keystroke to itself', () => {
    toggle(true)
    const notCancelled = key('Escape')
    expect(box().hidden).toBe(true)
    /* stopPropagation, not preventDefault: the document handler above must not
       also take a panel down, but the key itself was not consumed. */
    expect(notCancelled).toBe(true)
  })

  it('leaves escape alone while an input method is composing', () => {
    toggle(true)
    key('Escape', { isComposing: true })
    expect(box().hidden).toBe(false)
    /* The older spelling some IMEs still send. */
    key('Escape', { keyCode: 229 })
    expect(box().hidden).toBe(false)
  })

  it('closes on blur only while empty', () => {
    toggle(true)
    field().dispatchEvent(new Event('blur'))
    expect(box().hidden).toBe(true)
    toggle(true)
    type('gtm')
    field().dispatchEvent(new Event('blur'))
    expect(box().hidden).toBe(false)
  })

  it('does nothing at all when the row is not in the document', () => {
    document.body.innerHTML = ''
    expect(() => {
      install()
      toggle(true)
    }).not.toThrow()
  })
})
