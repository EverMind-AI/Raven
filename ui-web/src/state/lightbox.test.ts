// @vitest-environment happy-dom
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import { mountPageRoot } from '../test/pageRoot'
import * as confirmStore from './confirm'
import { close, isOpen, open } from './lightbox'
import * as pageStore from './page'


/* The overlay is drawn by src/chrome/Lightbox.tsx, so the page's own root has
   to be standing for one to reach the body -- the way src/main.tsx stands it
   up before anything can ask for an overlay. */
let unmount = (): void => {}

function wire(): void {
  setTranslator((key) => key)
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
  document.body.innerHTML = ''
}

const overlay = (): HTMLElement | null => document.querySelector('.lightbox')

beforeEach(() => {
  unmount = mountPageRoot()
})

afterEach(() => {
  close()
  unmount()
  resetTranslator()
  document.body.innerHTML = ''
})

describe('the lightbox', () => {
  it('appends one overlay carrying the image, and reports itself open', () => {
    wire()
    expect(isOpen()).toBe(false)
    open('data:image/png;base64,AA', 'shot.png')
    const box = overlay()!
    expect(box.tagName).toBe('BUTTON')
    expect(box.getAttribute('aria-label')).toBe('gui.img.close')
    const img = box.querySelector('img')!
    expect(img.getAttribute('src')).toBe('data:image/png;base64,AA')
    expect(img.alt).toBe('shot.png')
    expect(isOpen()).toBe(true)
  })

  it('keeps a nameless image nameless rather than inventing alt text', () => {
    wire()
    open('data:image/png;base64,AA')
    expect(document.querySelector('.lightbox img')!.getAttribute('alt')).toBe('')
  })

  it('closes on a click anywhere on it', () => {
    wire()
    open('x')
    ;(overlay() as HTMLElement).click()
    expect(overlay()).toBeNull()
  })

  it('never stacks two, so one click cannot leave one behind', () => {
    wire()
    open('one')
    open('two')
    expect(document.querySelectorAll('.lightbox').length).toBe(1)
    expect(document.querySelector('.lightbox img')!.getAttribute('src')).toBe('two')
  })

  /* The chrome's Escape chain finds the overlay by class and calls close(); it
     never asks whether one is open, so close() has to be safe with none. */
  it('closes nothing without complaining', () => {
    wire()
    expect(() => close()).not.toThrow()
    expect(isOpen()).toBe(false)
  })

})

/* happy-dom lays nothing out, so these read what page.css cascades onto the
 * overlay -- the mechanism, not the geometry. Only a browser can show the
 * image scrolling.
 */
describe('the lightbox, styled', () => {
  let sheet: HTMLStyleElement | null = null

  function styled(): void {
    wire()
    sheet = document.createElement('style')
    sheet.textContent = readFileSync('src/styles/page.css', 'utf8') as string
    document.head.append(sheet)
  }

  afterEach(() => {
    sheet?.remove()
    sheet = null
  })

  /* Auto tracks rather than a cell fixed to the window, for the reason page.css
     gives: a fixed cell puts the overflow on both sides of the image's centre,
     and the part above and to the left out of reach of any scroll. */
  it('shows the image at its own size and scrolls both ways to reach all of it', () => {
    styled()
    open('x')
    const box = getComputedStyle(overlay()!)
    expect(box.overflow).toBe('auto')
    for (const tracks of [box.gridTemplateRows, box.gridTemplateColumns]) expect(tracks).not.toMatch(/fr|%|px/)
    const img = getComputedStyle(overlay()!.querySelector('img')!)
    expect([img.width, img.height]).toEqual(['auto', 'auto'])
    for (const cap of [img.maxWidth, img.maxHeight]) expect(cap).not.toMatch(/%/)
  })

  /* The marking half is chrome/behaviour/scrollbars.test.ts's. */
  it('lifts the scrollbar layer over itself and shows only its own thumbs there', () => {
    styled()
    const layer = document.createElement('div')
    layer.className = 'sbars'
    const own = document.createElement('div')
    own.className = 'sbar'
    own.dataset.over = 'lightbox'
    const behind = document.createElement('div')
    behind.className = 'sbar'
    layer.append(own, behind)
    document.body.append(layer)
    const z = (n: Element): string => getComputedStyle(n).zIndex
    const under = z(layer)
    open('x')
    expect(Number(under)).toBeLessThan(Number(z(overlay()!)))
    /* happy-dom resolves the var() and leaves the calc() as written. */
    expect(z(layer)).toBe(`calc(${z(overlay()!)} + 1)`)
    expect(getComputedStyle(own).display).not.toBe('none')
    expect(getComputedStyle(behind).display).toBe('none')
    close()
    expect(getComputedStyle(behind).display).not.toBe('none')
  })
})
