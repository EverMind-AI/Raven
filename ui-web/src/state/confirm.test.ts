// @vitest-environment happy-dom
/* The confirm dialog: the question, the two answers, and the flag on the veil.
 *
 * The sheet's markup is rendered by the page's own root (src/App.tsx), so the
 * root is what these cases raise -- the store alone would prove nothing about
 * the buttons, and the buttons are half of the contract. The literals are
 * asserted as "still there" rather than by their text: the page's Chinese
 * belongs in one place, and a second copy in a test is what would then have to
 * be kept in step (src/App.test.tsx says the same).
 */
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import * as confirm from './confirm'
import * as lang from './lang'

let root: ReturnType<typeof createRoot> | null = null

function render(): void {
  root?.unmount()
  document.body.innerHTML = '<div class="veil" id="veil" data-open="false"></div>'
  root = createRoot(document.createElement('div'))
  flushSync(() => root!.render(createElement(App)))
}

const el = (id: string): HTMLElement => document.getElementById(id)!
const veil = (): string | undefined => el('veil').dataset.open
const text = (id: string): string => el(id).textContent ?? ''

afterEach(() => {
  root?.unmount()
  root = null
  document.body.innerHTML = ''
})

/* First on purpose: a question stays in the markup after the sheet closes, the
   way the old textContent writes left it, so every case below this one has
   asked something. */
describe('the confirm dialog before anything has asked', () => {
  it('stands as the page was served, with the veil down', () => {
    render()
    expect(veil()).toBe('false')
    expect(text('cfTitle')).not.toBe('')
    expect(text('cfNo')).not.toBe('')
    expect(text('cfYes')).not.toBe('')
    expect(el('cfBody').childNodes).toHaveLength(0)
  })
})

describe('the confirm dialog', () => {
  it('raises the veil with the question it was handed, focused on cancel', () => {
    render()
    confirm.ask('Delete this?', 'It cannot be undone.', 'Delete', () => {})
    expect(text('cfTitle')).toBe('Delete this?')
    expect(text('cfBody')).toBe('It cannot be undone.')
    expect(text('cfYes')).toBe('Delete')
    expect(veil()).toBe('true')
    /* Cancel holds the focus, so a stray Enter answers no. */
    expect(document.activeElement).toBe(el('cfNo'))
  })

  it('runs the answer on yes, once, and takes the veil down first', () => {
    render()
    const seen: string[] = []
    confirm.ask('Delete this?', '', 'Delete', () => seen.push(`yes:${veil()}`))
    el('cfYes').click()
    expect(seen).toEqual(['yes:false'])
    expect(veil()).toBe('false')
    el('cfYes').click()
    expect(seen).toEqual(['yes:false'])
  })

  it('answers no from the cancel button, which is what the Escape chain clicks', () => {
    render()
    const seen: string[] = []
    confirm.ask('Delete this?', '', 'Delete', () => seen.push('yes'))
    /* A native click, not the React handler: the chrome's Escape chain does
       `$('#cfNo').click()` and the sheet has to answer it. */
    el('cfNo').click()
    expect(seen).toEqual([])
    expect(veil()).toBe('false')
  })

  it('answers no from the scrim, and not from the sheet over it', () => {
    render()
    const seen: string[] = []
    confirm.ask('Delete this?', '', 'Delete', () => seen.push('yes'))
    document.querySelector<HTMLElement>('#veil .sheet')!.click()
    expect(veil()).toBe('true')
    el('veil').click()
    expect(veil()).toBe('false')
    expect(seen).toEqual([])
  })

  it('asks the next question over the last one', () => {
    render()
    confirm.ask('First?', 'one', 'Yes', () => {})
    confirm.ask('Second?', 'two', 'Go', () => {})
    expect(text('cfTitle')).toBe('Second?')
    expect(text('cfBody')).toBe('two')
    expect(text('cfYes')).toBe('Go')
    expect(confirm.get().open).toBe(true)
  })

  it('does not throw on a page without the sheet', () => {
    document.body.innerHTML = ''
    expect(() => confirm.ask('t', 'b', 'l', () => {})).not.toThrow()
    expect(() => confirm.answer(false)).not.toThrow()
  })
})

/* Last in the file on purpose: applying a language is module state for
   everything after it. What this proves is that the label's two writers agree
   -- the pass state/lang/store.ts makes over the document's data-i18n attributes, and
   the component rendering the same key through lang.text -- so the sheet cannot
   come back in the served language once a flip has moved it. A re-render alone
   would not show it: React diffs against the props it rendered last, so a
   literal it never changes is a literal it never writes again, and only a
   remount asks the component what the text is. */
describe('the confirm dialog once a language is applied', () => {
  it('renders the applied label when the sheet is mounted again', () => {
    render()
    const served = text('cfNo')
    lang.set('en')
    const applied = text('cfNo')
    expect(applied).not.toBe(served)
    render()
    expect(text('cfNo')).toBe(applied)
    confirm.ask('Delete this?', '', 'Delete', () => {})
    expect(text('cfNo')).toBe(applied)
  })
})
