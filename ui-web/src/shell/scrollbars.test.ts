// @vitest-environment happy-dom
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { SB_HIDE, SB_MIN, SB_PAD, hide, install, show, sync } from './scrollbars'

/* A scroller is only its geometry as far as this module is concerned, and
   happy-dom lays nothing out, so the numbers are stated. Defaults: a 200px
   viewport over 1000px of content, its box at 10,5. */
interface Geom {
  clientHeight: number
  scrollHeight: number
  clientWidth: number
  scrollWidth: number
  top: number
  left: number
}

function scroller(over: Partial<Geom> = {}): HTMLElement {
  const g: Geom = { clientHeight: 200, scrollHeight: 1000, clientWidth: 300, scrollWidth: 300, top: 10, left: 5, ...over }
  const el = document.createElement('div')
  document.body.appendChild(el)
  for (const k of ['clientHeight', 'scrollHeight', 'clientWidth', 'scrollWidth'] as const) {
    Object.defineProperty(el, k, { value: g[k], configurable: true })
  }
  el.getBoundingClientRect = () =>
    ({
      top: g.top,
      left: g.left,
      bottom: g.top + g.clientHeight,
      right: g.left + g.clientWidth,
      width: g.clientWidth,
      height: g.clientHeight,
      x: g.left,
      y: g.top,
      toJSON: () => ({}),
    }) as DOMRect
  return el
}

const thumbs = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.sbars > .sbar')]
const vert = (): HTMLElement | undefined => thumbs().find((t) => t.dataset.axis === 'v')
const px = (t: HTMLElement, prop: 'top' | 'left' | 'height' | 'width'): number => parseFloat(t.style[prop])

beforeAll(() => {
  install()
})

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  /* Leave the layer alone -- it is installed once, as in the page -- but take
     every scroller and thumb down between tests. */
  for (const t of thumbs()) t.remove()
  for (const n of [...document.body.children]) if (!n.classList.contains('sbars')) n.remove()
})

describe('overlay scrollbars', () => {
  it('sizes and places the thumb from the box it floats over', () => {
    const el = scroller()
    show(el)
    const t = vert()
    expect(t).toBeTruthy()
    /* track = 200 - 2*SB_PAD; len = round(track * 200/1000) */
    const track = 200 - SB_PAD * 2
    expect(px(t!, 'height')).toBe(Math.round(track * 0.2))
    /* At the top of the content the thumb sits SB_PAD below the box top, and
       6px wide against its right edge. */
    expect(px(t!, 'top')).toBe(10 + SB_PAD)
    expect(px(t!, 'left')).toBe(305 - 8)
    expect(px(t!, 'width')).toBe(6)
  })

  it('never draws a thumb shorter than SB_MIN, however long the content is', () => {
    const el = scroller({ clientHeight: 100, scrollHeight: 10000 })
    show(el)
    const t = vert()
    /* The proportional length would be 1px. */
    expect(px(t!, 'height')).toBe(SB_MIN)
  })

  it('offsets the thumb by how far down the content stands', () => {
    const el = scroller()
    el.scrollTop = 400
    show(el)
    const track = 200 - SB_PAD * 2
    const len = Math.round(track * 0.2)
    /* 400 of 800 scrollable pixels: half way down the free track. */
    expect(px(vert()!, 'top')).toBe(10 + SB_PAD + Math.round((track - len) * 0.5))
  })

  it('draws no thumb for a scroller with nothing to scroll, or too small to show one', () => {
    show(scroller({ scrollHeight: 200 }))
    expect(thumbs()).toHaveLength(0)
    show(scroller({ clientHeight: 30, scrollHeight: 900 }))
    expect(vert()).toBeUndefined()
  })

  it('shows the bar, then takes it back after SB_HIDE', () => {
    show(scroller())
    expect(vert()!.dataset.on).toBe('true')
    vi.advanceTimersByTime(SB_HIDE - 1)
    expect(vert()!.dataset.on).toBe('true')
    vi.advanceTimersByTime(1)
    expect(vert()!.dataset.on).toBe('false')
  })

  it('restarts the fade on the next scroll rather than stacking two', () => {
    const el = scroller()
    show(el)
    vi.advanceTimersByTime(SB_HIDE - 100)
    show(el)
    vi.advanceTimersByTime(200)
    expect(vert()!.dataset.on).toBe('true')
  })

  it('follows a scroll event from anywhere, without being registered for it', () => {
    const el = scroller()
    el.dispatchEvent(new Event('scroll'))
    expect(vert()!.dataset.on).toBe('true')
  })

  it('puts a showing thumb back where its box moved to', () => {
    let top = 10
    const el = scroller()
    const rect = el.getBoundingClientRect.bind(el)
    el.getBoundingClientRect = () => ({ ...rect(), top, bottom: top + 200 }) as DOMRect
    show(el)
    expect(px(vert()!, 'top')).toBe(10 + SB_PAD)
    top = 60
    sync()
    expect(px(vert()!, 'top')).toBe(60 + SB_PAD)
  })

  it('takes the thumb of a scroller that left the page down', () => {
    const el = scroller()
    show(el)
    expect(thumbs()).toHaveLength(1)
    el.remove()
    sync()
    expect(thumbs()).toHaveLength(0)
  })

  it('drops everything on demand, timer included', () => {
    const el = scroller()
    show(el)
    hide(el)
    expect(thumbs()).toHaveLength(0)
    /* Nothing left to fire at the old thumb. */
    expect(() => vi.advanceTimersByTime(SB_HIDE * 2)).not.toThrow()
  })

  it('scrolls the element proportionally while the thumb is dragged', () => {
    const el = scroller()
    show(el)
    const t = vert()!
    t.dispatchEvent(new PointerEvent('pointerdown', { clientY: 100, bubbles: true, pointerId: 1 }))
    expect(t.dataset.drag).toBe('true')
    expect(document.body.style.userSelect).toBe('none')
    t.dispatchEvent(new PointerEvent('pointermove', { clientY: 150, bubbles: true, pointerId: 1 }))
    /* 50px down a track of (196 - 39.2) free pixels, over 800 scrollable ones. */
    const track = 200 - SB_PAD * 2
    const len = Math.max(SB_MIN, track * 0.2)
    expect(el.scrollTop).toBeCloseTo((50 * 800) / (track - len), 5)
    t.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1 }))
    expect(t.dataset.drag).toBeUndefined()
    expect(document.body.style.userSelect).toBe('')
  })

  it('holds the bar open for as long as the pointer holds the thumb', () => {
    const el = scroller()
    show(el)
    const t = vert()!
    t.dispatchEvent(new PointerEvent('pointerdown', { clientY: 100, bubbles: true, pointerId: 1 }))
    vi.advanceTimersByTime(SB_HIDE * 2)
    expect(t.dataset.on).toBe('true')
    t.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1 }))
    vi.advanceTimersByTime(SB_HIDE + 1)
    expect(t.dataset.on).toBe('false')
  })

  it('stops the drag from reaching whatever is under the thumb', () => {
    const el = scroller()
    show(el)
    const seen: string[] = []
    document.addEventListener('pointerdown', () => seen.push('document'))
    vert()!.dispatchEvent(new PointerEvent('pointerdown', { clientY: 100, bubbles: true, pointerId: 1 }))
    expect(seen).toEqual([])
  })

  it('clears the bars on a window resize, where every box moves at once', () => {
    show(scroller())
    expect(thumbs()).toHaveLength(1)
    window.dispatchEvent(new Event('resize'))
    expect(thumbs()).toHaveLength(0)
  })
})
