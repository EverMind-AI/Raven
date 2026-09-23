/* A delivered file's picture keeps the shape of what is in it.
 *
 * A delivery is a small card (Figma: Raven / AssistantMessage, the output-file
 * variants): the file's mark, or -- where the file has one -- its picture, in a
 * slot the height of the mark. The slot states that height and no width, and
 * the picture is what decides how wide it is.
 *
 * That only works if the picture states a width of its own. An image told to
 * fill its box (`width: 100%`, `cover`) states none: it is 100% of whatever it
 * is given, and a box that follows it has nothing to follow. That is how a
 * delivered deck once arrived as a letterboxed strip, `cover` having fitted a
 * 16:9 slide into a box the wrong shape by keeping the middle quarter of it.
 *
 * Sized from the height instead, the image resolves its width from its own
 * ratio, the slot resolves from that, and there is nothing left to crop -- so
 * `contain` and `cover` would agree. `contain` is stated anyway: it is the one
 * that cannot crop, and the rule should say what it means rather than rely on
 * the box being right.
 *
 * Pinned here rather than in a DOM test for the reason every CSS gate in this
 * directory exists: happy-dom does no layout and applies no stylesheet, so a
 * picture cropped to a strip and one drawn whole are the same assertion there.
 * Only the stylesheet says which happens.
 */

import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

describe("a delivery's picture keeps its shape", () => {
  it('sizes the picture from its height, so it states a width of its own', () => {
    const img = decls('.atile .pic.shot img')
    expect(img, 'the card sizes its picture').not.toBeNull()
    /* The height is the slot's, the width comes from the image's ratio. Either
       of these back to `100%` and the picture has no width of its own again,
       which is the whole of the fault. */
    expect(img.get('height')).toBe('100%')
    expect(img.get('width')).toBe('auto')
    /* And nothing may be cropped away: the slot is the picture's own shape. */
    expect(img.get('object-fit')).toBe('contain')
  })

  it('gives the slot a height and leaves its width to the picture', () => {
    const pic = decls('.atile .pic')
    expect(pic).not.toBeNull()
    expect(pic.get('height')).toMatch(/^\d+px$/)
    /* A floor for the mark, never a stated width: a stated one is a box the
       picture would have to be cropped into. */
    expect(pic.has('width')).toBe(false)
    expect(pic.has('aspect-ratio')).toBe(false)
  })
})
