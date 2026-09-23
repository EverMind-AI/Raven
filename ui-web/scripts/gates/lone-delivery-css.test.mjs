/* A lone delivery's preview keeps the shape of what is in it.
 *
 * The delivery tile has two arrangements. In the grid, a preview sits ABOVE its
 * caption in a column of stated width, so the box states a ratio and the image
 * fills it -- `width: 100%` and `cover`, and a little crop is the price of a
 * tidy grid. Alone, the preview stands BESIDE the caption at a stated height,
 * and the column it stands in follows it (`grid-template-columns: auto ...`).
 *
 * That last part is what has to be paid for. A column told to follow the
 * picture needs the picture to state a width, and an image that fills a box
 * states none: it is `width: 100%` of whatever it is given. Given an auto
 * column, that resolved to the whole row -- a 98px-tall box 688px wide, into
 * which `cover` fitted a 16:9 slide by keeping the middle quarter of it. A
 * delivered deck arrived as a letterboxed strip with its own title cut in
 * half, which is what this is pinned against.
 *
 * Sized from the height instead, the image resolves its own width from its own
 * ratio, the column resolves from that, and the box ends up the shape of its
 * contents -- so there is nothing left to crop and `contain` and `cover` would
 * agree. `contain` is stated anyway: it is the one that cannot crop, and the
 * rule should say what it means rather than rely on the box being right.
 *
 * Pinned here rather than in a DOM test for the reason every CSS gate in this
 * directory exists: happy-dom does no layout and applies no stylesheet, so a
 * picture cropped to a strip and one drawn whole are the same assertion there.
 * Only the stylesheet says which happens.
 */

import { describe, expect, it } from 'vitest'

import { decls, rules } from './css.mjs'

describe('a lone delivery keeps its picture whole', () => {
  it('sizes the picture from its height, so it states a width to follow', () => {
    const img = decls('.atiles.single .atile .pic.shot img')
    expect(img, 'the lone arrangement overrides how the image is sized').not.toBeNull()
    /* The height is the stated one, the width comes from the image's ratio.
       Either of these back to `100%` and the picture has no width of its own
       again, which is the whole of the fault. */
    expect(img.get('height')).toBe('100%')
    expect(img.get('width')).toBe('auto')
    /* And nothing may be cropped away: the box is the picture's own shape now,
       so a crop could only be a sign that it is not. */
    expect(img.get('object-fit')).toBe('contain')
  })

  it('leaves the grid arrangement filling its stated box', () => {
    /* The general rule is untouched: in the grid the box states the ratio and
       the image fills it, which is what keeps a row of tiles a row. */
    const img = decls('.atile .pic.shot img')
    expect(img).not.toBeNull()
    expect(img.get('width')).toBe('100%')
    expect(img.get('object-fit')).toBe('cover')
  })

  it('keeps the column following the picture', () => {
    /* The other half of the pair, from the change that introduced it: a column
       fixed at the document face's 92px cut the picture off instead, and the
       two rules only work together. */
    const following = rules(/^\.atiles\.single \.atile:has\(> \.pic\.shot\)$/)
    expect(following.length, 'the picture column is still auto').toBe(1)
    expect(following[0][1]).toMatch(/grid-template-columns:\s*auto/)
  })
})
