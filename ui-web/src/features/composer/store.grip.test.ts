// @vitest-environment happy-dom
/* The composer's height seam: a drag from the card's top edge sets the field's
 * height between its resting size and half the window, the dragged height takes
 * precedence over the content fit, and the lift the transcript reserves is
 * published inside the move rather than after the paint that follows it.
 *
 * happy-dom does no layout and loads no stylesheet, so the field's min-height
 * is stated inline (the value page.css gives it) and its scroll height and
 * rects are stubbed; the numbers are what is being asserted.
 */
import { beforeEach, describe, expect, it } from 'vitest'

import { _resetForTests, fitField, gripDrag, resizeField } from './store'

const FLOOR = 52

function viewport(px: number): void {
  Object.defineProperty(window, 'innerHeight', { value: px, configurable: true })
}

function rect(top: number, height: number): DOMRect {
  return {
    top, bottom: top + height, height, left: 0, right: 300, width: 300, x: 0, y: top, toJSON: () => ({}),
  } as DOMRect
}

function box(el: Element, top: number, height: number): void {
  el.getBoundingClientRect = () => rect(top, height)
}

function content(ta: HTMLTextAreaElement, height: number): void {
  Object.defineProperty(ta, 'scrollHeight', { value: height, configurable: true })
}

function mount(): HTMLTextAreaElement {
  document.body.innerHTML =
    '<div class="chat"><div class="dock"><div class="dock-in">'
    + '<div class="cp-grip" id="composerGrip"></div>'
    + '<div class="field"><textarea id="ta" rows="1"></textarea></div>'
    + '</div></div></div>'
  const ta = document.getElementById('ta') as HTMLTextAreaElement
  ta.style.minHeight = `${FLOOR}px`
  content(ta, FLOOR)
  box(ta, 0, FLOOR)
  return ta
}

const grip = (): HTMLElement => document.getElementById('composerGrip')!

function press(el: HTMLElement, y: number): void {
  el.dispatchEvent(new PointerEvent('pointerdown', { clientY: y, bubbles: true, pointerId: 1 }))
}

function moveTo(el: HTMLElement, y: number): void {
  el.dispatchEvent(new PointerEvent('pointermove', { clientY: y, bubbles: true, pointerId: 1 }))
}

function key(el: HTMLElement, name: string, shiftKey = false): void {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: name, shiftKey, bubbles: true, cancelable: true }))
}

beforeEach(() => {
  _resetForTests()
  viewport(800)
  document.body.style.userSelect = ''
})

describe('the composer height seam', () => {
  it('grows the field by the distance the pointer travels up', () => {
    const ta = mount()
    gripDrag(grip())
    press(grip(), 500)
    moveTo(grip(), 400)
    expect(ta.style.height).toBe('152px')
  })

  it('stops at the resting height on the way down', () => {
    const ta = mount()
    gripDrag(grip())
    press(grip(), 500)
    moveTo(grip(), 700)
    expect(ta.style.height).toBe(`${FLOOR}px`)
  })

  it('stops at half the window on the way up', () => {
    const ta = mount()
    gripDrag(grip())
    press(grip(), 600)
    moveTo(grip(), -400)
    expect(ta.style.height).toBe('400px')
  })

  it('takes its floor from the field min-height rather than a second number', () => {
    const ta = mount()
    ta.style.minHeight = '60px'
    resizeField(10)
    expect(ta.style.height).toBe('60px')
  })

  it('publishes the new lift inside the move, before any paint', () => {
    const ta = mount()
    box(document.querySelector('.chat')!, 0, 600)
    box(document.querySelector('.dock')!, 0, 600)
    /* The card's own box follows the field's inline height, which is what a
       real browser's layout would do between two pointer moves. */
    const card = document.querySelector('.dock-in') as HTMLElement
    card.getBoundingClientRect = () => {
      const height = parseFloat(ta.style.height) || FLOOR
      return rect(600 - height, height)
    }
    gripDrag(grip())
    press(grip(), 500)
    moveTo(grip(), 400)
    const chat = document.querySelector('.chat') as HTMLElement
    expect(chat.style.getPropertyValue('--lift')).toBe('152px')
  })
})

describe('a dragged height against the content fit', () => {
  it('wins over the content, in both directions', () => {
    const ta = mount()
    resizeField(200)
    content(ta, 60)
    fitField()
    expect(ta.style.height).toBe('200px')
    content(ta, 900)
    fitField()
    expect(ta.style.height).toBe('200px')
  })

  it('is re-clamped under a window that shrank, and restored when it grows back', () => {
    const ta = mount()
    resizeField(350)
    viewport(400)
    fitField()
    expect(ta.style.height).toBe('200px')
    viewport(1200)
    fitField()
    expect(ta.style.height).toBe('350px')
  })

  it('still lets the content fit while no drag has happened', () => {
    const ta = mount()
    content(ta, 300)
    fitField()
    expect(ta.style.height).toBe('300px')
  })

  it('caps the content fit at half the window', () => {
    const ta = mount()
    content(ta, 900)
    fitField()
    expect(ta.style.height).toBe('400px')
  })
})

describe('the seam by keyboard', () => {
  it('moves ten pixels a press and forty with Shift', () => {
    const ta = mount()
    gripDrag(grip())
    key(grip(), 'ArrowUp')
    expect(ta.style.height).toBe('62px')
    key(grip(), 'ArrowUp', true)
    expect(ta.style.height).toBe('102px')
    key(grip(), 'ArrowDown')
    expect(ta.style.height).toBe('92px')
  })

  it('leaves every other key alone', () => {
    const ta = mount()
    gripDrag(grip())
    key(grip(), 'ArrowLeft')
    expect(ta.style.height).toBe('')
  })
})

describe('resizing without a pointer', () => {
  it('clamps to the resting height and to half the window', () => {
    const ta = mount()
    resizeField(10)
    expect(ta.style.height).toBe(`${FLOOR}px`)
    resizeField(9999)
    expect(ta.style.height).toBe('400px')
  })
})
