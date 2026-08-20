// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { PANE, cssPx, gripDrag, install, load, paneMax, set } from './panes'

import type { Shell } from './bridge'

/* The width the grid refuses to take the chat below. A CSS token in the page,
   stated here because happy-dom loads no stylesheet. */
const CHAT_MIN = 420

function installShell(): { lifts: number } {
  const seen = { lifts: 0 }
  const fake: Shell = {
    T: (key) => key,
    toast: () => {},
    menuAt: () => {},
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
    dockLift: () => {
      seen.lifts += 1
    },
  }
  window.RavenShell = fake
  return seen
}

function width(px: number): void {
  Object.defineProperty(window, 'innerWidth', { value: px, configurable: true })
}

/* The rail's own width is what the workspace panel has to leave room for. */
function railBox(px: number): void {
  const rail = document.querySelector<HTMLElement>('.rail')
  if (rail) Object.defineProperty(rail, 'offsetWidth', { value: px, configurable: true })
}

const grip = (id: string): HTMLElement => document.getElementById(id)!

beforeEach(() => {
  installShell()
  localStorage.clear()
  width(1024)
  document.body.innerHTML =
    '<div class="app" data-rail="on"><aside class="rail"></aside>' +
    '<div class="grip" id="railGrip"></div><div class="grip" id="wsGrip"></div></div>'
  railBox(240)
  document.documentElement.style.setProperty('--chat-min', CHAT_MIN + 'px')
  document.documentElement.style.removeProperty(PANE.rail.v)
  document.documentElement.style.removeProperty(PANE.ws.v)
})

afterEach(() => {
  delete window.RavenShell
})

describe('the pane ceiling', () => {
  it('is the pane own bound while the window is wide enough for it', () => {
    expect(paneMax(PANE.rail)).toBe(PANE.rail.max)
  })

  it('drops to whatever the chat floor leaves on a narrow window', () => {
    width(700)
    expect(paneMax(PANE.rail)).toBe(700 - CHAT_MIN)
  })

  it('never returns less than the floor, however narrow the window', () => {
    width(300)
    expect(paneMax(PANE.rail)).toBe(PANE.rail.min)
  })

  it('counts the rail as taken for the panel on the other edge', () => {
    expect(paneMax(PANE.ws)).toBe(1024 - 240 - CHAT_MIN)
  })

  it('stops counting it once the rail is collapsed', () => {
    document.querySelector<HTMLElement>('.app')!.dataset.rail = 'off'
    expect(paneMax(PANE.ws)).toBe(1024 - CHAT_MIN)
  })
})

describe('setting a pane width', () => {
  it('writes the variable the grid reads, rounded', () => {
    expect(set('rail', 240.6, false)).toBe(241)
    expect(cssPx('--rail')).toBe(241)
  })

  it('clamps to the floor and to the ceiling', () => {
    expect(set('rail', 10, false)).toBe(PANE.rail.min)
    expect(set('rail', 9000, false)).toBe(PANE.rail.max)
    width(700)
    expect(set('rail', 9000, false)).toBe(700 - CHAT_MIN)
  })

  it('remembers a width only when asked to', () => {
    set('rail', 300, false)
    expect(localStorage.getItem(PANE.rail.key)).toBeNull()
    set('rail', 300, true)
    expect(localStorage.getItem(PANE.rail.key)).toBe('300')
  })

  it('stores what it clamped to, not what it was handed', () => {
    set('rail', 9000, true)
    expect(localStorage.getItem(PANE.rail.key)).toBe(String(PANE.rail.max))
  })
})

describe('reloading stored widths', () => {
  it('puts both panes back', () => {
    width(1400)
    localStorage.setItem(PANE.rail.key, '300')
    localStorage.setItem(PANE.ws.key, '400')
    load()
    expect(cssPx('--rail')).toBe(300)
    expect(cssPx('--wsw')).toBe(400)
  })

  it('re-clamps a width stored on a wider screen instead of trusting it', () => {
    localStorage.setItem(PANE.rail.key, '410')
    width(700)
    load()
    expect(cssPx('--rail')).toBe(700 - CHAT_MIN)
  })

  it('leaves a pane alone when nothing was stored for it', () => {
    set('rail', 260, false)
    load()
    expect(cssPx('--rail')).toBe(260)
  })
})

describe('dragging a grip', () => {
  const down = (el: HTMLElement, x: number): void => {
    el.dispatchEvent(new PointerEvent('pointerdown', { clientX: x, bubbles: true, pointerId: 1 }))
  }
  const move = (el: HTMLElement, x: number): void => {
    el.dispatchEvent(new PointerEvent('pointermove', { clientX: x, bubbles: true, pointerId: 1 }))
  }
  const up = (el: HTMLElement): void => {
    el.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1 }))
  }

  it('widens a left-edge pane as the pointer goes right', () => {
    const el = grip('railGrip')
    gripDrag(el, 'rail')
    set('rail', 240, false)
    down(el, 500)
    expect(el.dataset.drag).toBe('true')
    expect(document.body.style.userSelect).toBe('none')
    move(el, 560)
    expect(cssPx('--rail')).toBe(300)
    up(el)
    expect(el.dataset.drag).toBeUndefined()
    expect(document.body.style.userSelect).toBe('')
  })

  it('widens a right-edge pane as the pointer goes left', () => {
    width(1400)
    const el = grip('wsGrip')
    gripDrag(el, 'ws')
    set('ws', 340, false)
    down(el, 500)
    move(el, 440)
    expect(cssPx('--wsw')).toBe(400)
  })

  it('holds at the ceiling however far the pointer keeps going', () => {
    const el = grip('railGrip')
    gripDrag(el, 'rail')
    set('rail', 400, false)
    down(el, 500)
    move(el, 5000)
    expect(cssPx('--rail')).toBe(PANE.rail.max)
    move(el, 0)
    expect(cssPx('--rail')).toBe(PANE.rail.min)
  })

  it('keeps the docked composer offset current through the drag', () => {
    const lifts = installShell()
    const el = grip('railGrip')
    gripDrag(el, 'rail')
    down(el, 500)
    move(el, 520)
    move(el, 540)
    expect(lifts.lifts).toBe(2)
  })

  it('stores the width once, on release, not per frame', () => {
    const el = grip('railGrip')
    gripDrag(el, 'rail')
    set('rail', 240, false)
    down(el, 500)
    move(el, 560)
    expect(localStorage.getItem(PANE.rail.key)).toBeNull()
    up(el)
    expect(localStorage.getItem(PANE.rail.key)).toBe('300')
  })

  it('stops listening after release', () => {
    const el = grip('railGrip')
    gripDrag(el, 'rail')
    set('rail', 240, false)
    down(el, 500)
    up(el)
    move(el, 560)
    expect(cssPx('--rail')).toBe(240)
  })
})

describe('the grip as a separator', () => {
  const key = (el: HTMLElement, k: string, shift = false): void => {
    el.dispatchEvent(new KeyboardEvent('keydown', { key: k, shiftKey: shift, bubbles: true }))
  }

  it('is reachable by tab', () => {
    const el = grip('railGrip')
    gripDrag(el, 'rail')
    expect(el.tabIndex).toBe(0)
  })

  it('moves in both directions, and further with shift', () => {
    const el = grip('railGrip')
    gripDrag(el, 'rail')
    set('rail', 300, false)
    key(el, 'ArrowRight')
    expect(cssPx('--rail')).toBe(310)
    key(el, 'ArrowLeft')
    expect(cssPx('--rail')).toBe(300)
    key(el, 'ArrowRight', true)
    expect(cssPx('--rail')).toBe(340)
  })

  it('widens the right-edge pane with the arrow that points at it', () => {
    const el = grip('wsGrip')
    gripDrag(el, 'ws')
    set('ws', 340, false)
    key(el, 'ArrowLeft')
    expect(cssPx('--wsw')).toBe(350)
  })

  it('stores each step, unlike a drag frame', () => {
    const el = grip('railGrip')
    gripDrag(el, 'rail')
    set('rail', 300, false)
    key(el, 'ArrowRight')
    expect(localStorage.getItem(PANE.rail.key)).toBe('310')
  })

  it('leaves other keys to whoever else wants them', () => {
    const el = grip('railGrip')
    gripDrag(el, 'rail')
    set('rail', 300, false)
    key(el, 'ArrowUp')
    expect(cssPx('--rail')).toBe(300)
  })
})

describe('installing the panes', () => {
  it('wires both grips and re-clamps them when the window shrinks', () => {
    install()
    set('rail', 400, false)
    set('ws', 700, false)
    width(700)
    window.dispatchEvent(new Event('resize'))
    expect(cssPx('--rail')).toBe(700 - CHAT_MIN)
    expect(cssPx('--wsw')).toBe(PANE.ws.min)
    /* And the grips it found are live separators. */
    expect(grip('railGrip').tabIndex).toBe(0)
    expect(grip('wsGrip').tabIndex).toBe(0)
  })

  it('installs over a page without grips rather than throwing', () => {
    document.body.innerHTML = ''
    expect(() => install()).not.toThrow()
  })
})
