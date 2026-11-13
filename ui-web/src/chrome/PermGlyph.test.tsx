// @vitest-environment happy-dom
/* The permission glyph map, pinned to the icon each tier names.
 *
 * The map is the one place a tier and a drawing meet. A swap here would still
 * render three distinct SVGs and still pass the popover's shape checks, so
 * these cases assert the drawing itself, not only that something was drawn.
 * An id the map cannot answer must draw the neutral served shield, never the
 * full-access lock.
 */
import { HandIcon, LockOpenIcon, ZapIcon } from '@hugeicons/core-free-icons'
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { PermGlyph, SERVED_SHIELD } from './PermGlyph'

import type { IconSvgElement } from '@hugeicons/react'

/** Every path `d` in a Hugeicons drawing, in the order the drawing lists them. */
function drawingOf(icon: IconSvgElement): (string | null)[] {
  const found: (string | null)[] = []
  const walk = (value: unknown): void => {
    if (Array.isArray(value)) {
      value.forEach(walk)
      return
    }
    if (!value || typeof value !== 'object') return
    const record = value as Record<string, unknown>
    if (typeof record.d === 'string') found.push(record.d)
    Object.values(record).forEach(walk)
  }
  walk(icon)
  return found
}

const rendered = (id: string): HTMLElement =>
  render(<PermGlyph id={id} size={18} stroke={1.5} className="pico" />).container

const dsIn = (el: Element): (string | null)[] =>
  [...el.querySelectorAll('path')].map((p) => p.getAttribute('d'))

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('the permission glyph', () => {
  it('draws the hand for ask, the bolt for smart and the open lock for full', () => {
    expect(dsIn(rendered('ask'))).toEqual(drawingOf(HandIcon))
    expect(dsIn(rendered('smart'))).toEqual(drawingOf(ZapIcon))
    expect(dsIn(rendered('full'))).toEqual(drawingOf(LockOpenIcon))
  })

  it('draws the neutral served shield for an id no tier names', () => {
    const ds = dsIn(rendered('future-mode'))
    expect(ds).toEqual([SERVED_SHIELD])
    expect(ds).not.toEqual(drawingOf(LockOpenIcon))
  })
})
