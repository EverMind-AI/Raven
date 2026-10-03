// @vitest-environment happy-dom
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { hide, show } from '../chrome/behaviour/scrollbars'
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

  /* The thumbs are the real ones: chrome/behaviour/scrollbars.ts draws and
     marks them and this stylesheet reads the mark, so a change to either side
     fails here rather than leaving the two to agree on a spelling. */
  it('lifts the scrollbar layer over itself and shows only its own thumbs there', () => {
    styled()
    const behind = scrolls(document.createElement('div'))
    document.body.append(behind)
    show(behind)
    const layer = document.querySelector('.sbars')!
    const z = (n: Element): string => getComputedStyle(n).zIndex
    const under = z(layer)
    open('x')
    const box = scrolls(overlay()!)
    show(box)
    try {
      expect(Number(under)).toBeLessThan(Number(z(box)))
      /* happy-dom resolves the var() and leaves the calc() as written. */
      expect(z(layer)).toBe(`calc(${z(box)} + 1)`)
      const thumbs = [...layer.querySelectorAll('.sbar')]
      expect(thumbs).toHaveLength(2)
      const [theirs, own] = thumbs as [Element, Element]
      expect(getComputedStyle(own).display).not.toBe('none')
      expect(getComputedStyle(theirs).display).toBe('none')
      close()
      expect(getComputedStyle(theirs).display).not.toBe('none')
    } finally {
      hide(behind)
      hide(box)
    }
  })
})

/* happy-dom lays nothing out, so a scroller's geometry is stated: a 300x200 box
   over 1000px of content, which is what the scrollbar module needs to draw a
   thumb. */
function scrolls<T extends HTMLElement>(el: T): T {
  const size = { clientHeight: 200, scrollHeight: 1000, clientWidth: 300, scrollWidth: 300 }
  for (const [k, v] of Object.entries(size)) Object.defineProperty(el, k, { value: v, configurable: true })
  el.getBoundingClientRect = () =>
    ({ top: 0, left: 0, bottom: 200, right: 300, width: 300, height: 200, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  return el
}
