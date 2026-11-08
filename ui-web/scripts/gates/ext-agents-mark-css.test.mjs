/* The family picker's agent mark sizes both shapes it can be drawn with.
 *
 * Eight of the nine presets ship an image mark today, so the svg half is
 * invisible until one ships a glyph -- and a rule that names only `img` looks
 * exactly like a rule that covers both until then. The other three mark slots
 * on this page size `> img, > svg`; this gate keeps the drawer's row in step.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { rules } from './css.mjs'

const featureCss = readFileSync(new URL('../../src/features/extAgents/styles.css', import.meta.url), 'utf8')

describe('the family picker mark', () => {
  it('sizes the svg glyph the same as the image mark', () => {
    const sized = rules(/\.extAgents-pickrow .*\.agent-mark > img/, featureCss)
    expect(sized.length).toBeGreaterThan(0)
    for (const [selector] of sized) expect(selector).toMatch(/> svg/)
  })
})
