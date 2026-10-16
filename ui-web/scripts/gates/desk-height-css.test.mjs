/* How tall the stylesheet lets the desk palette be.
 *
 * DeskPalette sets the palette's height inline, and lets it reach down to the
 * composer card (features/desk/geometry.ts) -- in a tall window, well past the
 * 480px the width stops at. The stylesheet's max-height still has
 * the last word on what is drawn: a fixed cap there clips the inline height and
 * the palette stops short of the composer, every unit test still green, because
 * happy-dom applies no stylesheet. So the only ceiling pinned here is the
 * window's.
 */

import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

describe('the desk palette\'s height', () => {
  it('is capped by the window alone, leaving the composer to set how far down it reaches', () => {
    const palette = decls('.desk-palette')
    expect(palette).not.toBeNull()
    expect(palette.get('max-height')).toBe('calc(100vh - 16px)')
  })
})
