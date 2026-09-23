/* The model pill's clear control, read off the two sheets that draw one.
 *
 * A pill that can be cleared shows a 30px control over its right edge on
 * hover. It is absolutely placed, so nothing in the row makes room for it: the
 * chevron only went invisible and kept its space, and the provider name ran on
 * under the control. What makes room now is arithmetic -- on hover the chevron
 * leaves the row and the button's right padding grows by exactly what it took
 * (the row gap, its margin, its slot, the resting padding), which is at least
 * the control's width. Four numbers in two rules, and any one of them edited
 * alone puts the text back under the control or makes it jump; happy-dom lays
 * nothing out, so only the sheet can say.
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

const px = (v) => {
  const m = /^(-?\d+(?:\.\d+)?)px$/.exec(String(v || '').trim())
  if (!m) throw new Error(`not a px length: ${v}`)
  return Number(m[1])
}
/* The right side of a padding shorthand. */
const rightOf = (padding) => {
  const parts = padding.trim().split(/\s+/)
  return px(parts.length === 1 ? parts[0] : parts[1])
}

const PILLS = [
  {
    name: 'settings',
    sheet: '../../src/features/settings/styles.css',
    button: '.settings-mpill .settings-pm',
    chevron: '.settings-mpill .settings-ch',
    control: '.settings-mpill .settings-px',
    hoverButton: '.settings-mpill.settings-clearable:hover .settings-pm,.settings-mpill.settings-clearable:focus-within .settings-pm',
    hoverChevron: '.settings-mpill.settings-clearable:hover .settings-ch,.settings-mpill.settings-clearable:focus-within .settings-ch',
  },
  {
    name: 'agents',
    sheet: '../../src/features/extAgents/styles.css',
    button: '.extAgents-pm',
    chevron: '.extAgents-mch',
    control: '.extAgents-mx',
    hoverButton: '.extAgents-pill-clearable:hover .extAgents-pm, .extAgents-pill-clearable:focus-within .extAgents-pm',
    hoverChevron: '.extAgents-pill-clearable:hover .extAgents-mch, .extAgents-pill-clearable:focus-within .extAgents-mch',
  },
]

describe('a clearable model pill on hover', () => {
  for (const p of PILLS) {
    const sheet = readFileSync(new URL(p.sheet, import.meta.url), 'utf8')
    it(`${p.name}: takes the chevron out of the row rather than hiding it in place`, () => {
      expect(decls(p.hoverChevron, sheet).get('display')).toBe('none')
    })

    it(`${p.name}: pads the button by exactly what the chevron took, at least the control's width`, () => {
      const button = decls(p.button, sheet)
      const chevron = decls(p.chevron, sheet)
      const took = px(button.get('gap')) + px(chevron.get('margin-left')) + px(chevron.get('width')) + rightOf(button.get('padding'))
      const padded = px(decls(p.hoverButton, sheet).get('padding-right'))
      expect(padded).toBe(took)
      expect(padded).toBeGreaterThanOrEqual(px(decls(p.control, sheet).get('width')))
    })
  }
})
