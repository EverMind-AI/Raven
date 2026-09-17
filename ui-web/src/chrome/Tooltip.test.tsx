// @vitest-environment happy-dom
/* The hover pill: the label the pointer earns, in the one layer that holds it.
 *
 * Placement is measured, so every case here fixes the boxes it measures --
 * happy-dom gives an unstyled element a zero rect, and a pill placed from
 * zeroes would clamp to the corner and prove nothing about the flip.
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Tooltip } from './Tooltip'
import { _resetForTests as resetLayers } from '../state/portals'
import { _resetForTests as resetTip, follow, hide, mount, watch } from '../state/tooltip'

const pill = (): HTMLElement => document.querySelector('.tipp') as HTMLElement

/* A box where the page would have one. `height` is what decides whether the
   label goes above or below, so it is the number every case turns on. */
function box(el: HTMLElement, at: { top: number; left: number; height?: number; width?: number }): HTMLElement {
  const rect = {
    top: at.top, left: at.left, height: at.height ?? 20, width: at.width ?? 60,
    bottom: at.top + (at.height ?? 20), right: at.left + (at.width ?? 60), x: at.left, y: at.top,
  }
  el.getBoundingClientRect = (): DOMRect => ({ ...rect, toJSON: () => ({}) }) as DOMRect
  return el
}

function control(tip: string, at: { top: number; left: number }, className = ''): HTMLElement {
  const el = document.createElement('button')
  if (className) el.className = className
  el.dataset.tip = tip
  document.body.appendChild(el)
  return box(el, at)
}

/* The page's arrangement: one component drawing into the layer the store
   raises, and the one document listener that drives it
   (state/globalListeners.ts). */
function setup(): void {
  /* A viewport, which the placer clamps and flips against. happy-dom reports
     zero for both, and a pill placed against a zero-height window would flip
     every time -- so the case would pass whatever the rule said. */
  for (const [prop, value] of [['clientWidth', 1200], ['clientHeight', 800]] as const) {
    Object.defineProperty(document.documentElement, prop, { configurable: true, value })
  }
  render(<Tooltip />)
  mount()
  document.addEventListener('pointerover', follow)
}

afterEach(() => {
  document.removeEventListener('pointerover', follow)
  cleanup()
  resetTip()
  resetLayers()
  document.body.innerHTML = ''
})

/* The store reads event.target, which happy-dom only sets while an event is
   being dispatched -- so the cases dispatch for real. */
const point = (el: Element): void => {
  el.dispatchEvent(new Event('pointerover', { bubbles: true }))
}

describe('the hover pill', () => {
  it('takes the label of the control the pointer is on and places it above', () => {
    setup()
    const btn = control('Expand workspace', { top: 200, left: 100 })
    box(pill(), { top: 0, left: 0, height: 24, width: 140 })
    point(btn)
    expect(pill().textContent).toBe('Expand workspace')
    expect(pill().dataset.on).toBe('true')
    /* Above: the control's top, less the pill's own height and the 5px gap. */
    expect(pill().style.top).toBe('171px')
    /* Centred on the control, clamped to the viewport either way. */
    expect(pill().style.left).toBe('60px')
  })

  it('places the label below a control that asks for it', () => {
    setup()
    const btn = control('Expand rail', { top: 200, left: 100 }, 'tipdn')
    box(pill(), { top: 0, left: 0, height: 24, width: 140 })
    point(btn)
    expect(pill().style.top).toBe('225px')
  })

  it('flips below a control too near the top of the window to fit above', () => {
    setup()
    const btn = control('New task', { top: 10, left: 100 })
    box(pill(), { top: 0, left: 0, height: 24, width: 140 })
    point(btn)
    expect(pill().style.top).toBe('35px')
  })

  it('clamps the label to the window rather than letting it hang off', () => {
    setup()
    const btn = control('Expand workspace', { top: 200, left: 2 })
    box(pill(), { top: 0, left: 0, height: 24, width: 140 })
    point(btn)
    expect(pill().style.left).toBe('4px')
  })

  it('re-reads a control that rewrites its own label while hovered', () => {
    setup()
    watch()
    const btn = control('Copy', { top: 200, left: 100 })
    box(pill(), { top: 0, left: 0, height: 24, width: 140 })
    point(btn)
    expect(pill().textContent).toBe('Copy')
    btn.dataset.tip = 'Copied'
    return new Promise<void>((done) => {
      /* The observer answers in a microtask, which is one turn later than the
         attribute write. */
      queueMicrotask(() => {
        expect(pill().textContent).toBe('Copied')
        done()
      })
    })
  })

  it('goes down on a scroll and leaves the label it had', () => {
    setup()
    const btn = control('Expand workspace', { top: 200, left: 100 })
    box(pill(), { top: 0, left: 0, height: 24, width: 140 })
    point(btn)
    hide()
    expect(pill().dataset.on).toBe('false')
    /* Nothing reads the label while the pill is down, and clearing it would
       rewrite text that had not changed on the next hover of the same
       control. */
    expect(pill().textContent).toBe('Expand workspace')
  })

  it('goes down when the pointer reaches something with no label', () => {
    setup()
    const btn = control('Expand workspace', { top: 200, left: 100 })
    box(pill(), { top: 0, left: 0, height: 24, width: 140 })
    point(btn)
    const bare = document.createElement('div')
    document.body.appendChild(bare)
    point(bare)
    expect(pill().dataset.on).toBe('false')
  })
})
