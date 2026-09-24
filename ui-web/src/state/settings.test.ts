// @vitest-environment happy-dom
/* Whether the settings dialog is up, and the two controls that take it down.
 *
 * The dialog's frame is rendered by the page's own root (src/App.tsx) over the
 * static div#setVeil, so the root is what these cases raise: the close button
 * and the scrim are the component's half of the same contract the store holds.
 *
 * The rail's marks are the one side effect worth proving: settings layers over
 * whatever you were reading, and the row you came from is marked from the
 * topmost surface, so both verbs ask the rail to decide again. `navState` is
 * the first thing that decision reads, which is what makes the fake shell below
 * a witness for it.
 */
import { createElement } from 'react'
import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { resetTranslator, setTranslator } from '../i18n/t'
import * as confirmStore from './confirm'
import * as lang from './lang'
import * as pageStore from './page'
import * as settings from './settings'


const marks: string[] = []
let root: ReturnType<typeof createRoot> | null = null

function render(): void {
  root?.unmount()
  marks.length = 0
  setTranslator((key: string) => key)
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
  vi.spyOn(pageStore, 'navState').mockImplementation(() => {
  marks.push('markNew')
  return { pages: [], btnOf: () => '' }
  })
  document.body.innerHTML = '<div class="veil setveil" id="setVeil" data-open="false"></div>'
  root = createRoot(document.createElement('div'))
  flushSync(() => root!.render(createElement(App)))
}

const veil = (): string | undefined => document.getElementById('setVeil')!.dataset.open

afterEach(() => {
  /* Shut before the shell goes: closing asks the rail to mark again. */
  settings.close()
  root?.unmount()
  root = null
  document.body.innerHTML = ''
  resetTranslator()
})

describe('the settings dialog', () => {
  it('opens on General the first time, not on Usage', async () => {
    /* The page's own starting value, so a fresh copy of the module rather than
       whatever an earlier case left in the slot. */
    vi.resetModules()
    const fresh = await import('./settings')
    expect(fresh.settingsTab.id).toBe('general')
  })

  it('is shut on a page nobody has opened', () => {
    render()
    expect(settings.isOpen()).toBe(false)
    expect(veil()).toBe('false')
  })

  it('raises the veil and asks the rail to mark again', () => {
    render()
    settings.open()
    expect(settings.isOpen()).toBe(true)
    expect(veil()).toBe('true')
    expect(marks).toEqual(['markNew'])
  })

  it('takes the veil down and asks the rail again', () => {
    render()
    settings.open()
    marks.length = 0
    settings.close()
    expect(settings.isOpen()).toBe(false)
    expect(veil()).toBe('false')
    expect(marks).toEqual(['markNew'])
  })

  it('closes from the close button in the header', () => {
    render()
    settings.open()
    document.getElementById('setClose')!.click()
    expect(settings.isOpen()).toBe(false)
    expect(veil()).toBe('false')
  })

  it('closes from the scrim, and not from the dialog over it', () => {
    render()
    settings.open()
    document.getElementById('setModal')!.click()
    expect(veil()).toBe('true')
    document.getElementById('setVeil')!.click()
    expect(veil()).toBe('false')
  })

  /* markNewCurrent runs during the first draw, before the dialog's markup is
     in the document, which is why neither verb may need the element. */
  it('answers and does not throw on a page without the dialog', () => {
    setTranslator((key: string) => key)
    document.body.innerHTML = ''
    expect(() => settings.open()).not.toThrow()
    expect(settings.isOpen()).toBe(true)
    expect(() => settings.close()).not.toThrow()
    expect(settings.isOpen()).toBe(false)
  })
})

/* Last in the file on purpose: applying a language is module state for
   everything after it. The claim is that the wordmark is the catalogue's rather
   than the literal the markup was served with -- the stand-in translator here
   answers the key, so the served word being off screen is what says so -- and
   that a pick and a remount both leave it that way. */
describe('the settings dialog once a language is applied', () => {
  it('renders the wordmark from the catalogue, before and after a pick', () => {
    const wm = (): string => document.querySelector('.wm')!.textContent ?? ''
    render()
    expect(wm()).toBe('gui.page.set')
    lang.set('zh')
    expect(wm()).toBe('gui.page.set')
    /* A remount over fresh markup: the frame would render the served literal
       again if it did not read the catalogue itself. */
    render()
    expect(wm()).toBe('gui.page.set')
  })
})
