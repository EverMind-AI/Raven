/* Every entrance the page lists is named in both of the page's languages.
 *
 * The catalogue names each row through the shared message catalogue
 * (i18n/messages.json), and a lookup that misses falls back to text rather than
 * failing, so a gap renders as something plausible: a Chinese word in the
 * English slot read as a brand name on the English page. Held here against the
 * file itself, one entry at a time.
 */

import { describe, expect, it } from 'vitest'

import catalogue from '../../../../i18n/messages.json'
import { CHANNELS } from './catalogue'

const words = catalogue.ui as Record<string, { en?: string; zh?: string }>
const said = (key: string | undefined, lang: 'en' | 'zh'): string => (key ? words[key]?.[lang] ?? '' : '')

describe('the channel catalogue', () => {
  it('keys every entrance by its own id', () => {
    expect(CHANNELS.filter((c) => c.key !== `gui.chan.${c.id}`).map((c) => c.id)).toEqual([])
  })

  it('names every entrance in both languages', () => {
    expect(CHANNELS.filter((c) => !said(c.key, 'en') || !said(c.key, 'zh')).map((c) => c.id)).toEqual([])
  })

  it('writes the English name in English', () => {
    expect(CHANNELS.filter((c) => /\p{Script=Han}/u.test(said(c.key, 'en'))).map((c) => c.id)).toEqual([])
  })
})
