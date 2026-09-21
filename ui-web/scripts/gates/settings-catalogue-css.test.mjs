/* The provider catalogue's two columns, read straight off the settings sheet.
 *
 * The catalogue is a grid whose row is meant to be one screenful, with the
 * vendor list and the detail pane each scrolling inside it. It was sized with a
 * max-height, which a grid's fr row does not resolve against, so the row grew
 * to the list's two thousand pixels and the container clipped it: the list could
 * not scroll past the first screen of vendors and the "pick one" placeholder,
 * centred in a column that tall, sat below the fold. jsdom does no layout, so
 * the component tests could not see any of it; the sheet is where it is pinned.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

const settingsCss = readFileSync(new URL('../../src/features/settings/styles.css', import.meta.url), 'utf8')

describe('the provider catalogue grid', () => {
  it('bounds its row track with a definite height, which is what lets its columns scroll', () => {
    const tp = decls('.settings-tp', settingsCss)
    expect(tp).toBeTruthy()
    expect(tp.get('height')).toMatch(/calc\(100vh/)
    expect(tp.get('grid-template-rows')).toMatch(/minmax\(0,\s*1fr\)/)
    expect(tp.get('max-height')).toBeUndefined()
  })

  it('lets each column scroll on its own inside that row', () => {
    expect(decls('.settings-tp-list', settingsCss).get('overflow-y')).toBe('auto')
    expect(decls('.settings-tp-main', settingsCss).get('overflow-y')).toBe('auto')
  })
})
