/* Every switch's knob stops when the reader asked for less motion.
 *
 * The knob is a pseudo-element that slides on `transition: transform`, and
 * happy-dom applies no stylesheet: a DOM test sees `aria-checked` flip with the
 * same paint either way, so the guard is a fact about the sheet or it is
 * unpinned. The same switch is copied across the page -- `.swi` and
 * `.two-pane-swi` in the frame, `.settings-swi` in the settings domain,
 * `.ob-switch` in onboarding -- and the copies drift: the onboarding one was
 * resized to the shared .22s curve while the one-line guard beside it was left
 * behind. So the rule is stated over the naming convention the copies share (a
 * class whose name ends in `swi` or `switch`, owning its animated
 * pseudo-element), not over a list that the next copy would miss.
 *
 * The four known variants are also asserted by name, so a rename cannot hollow
 * the scan into passing on nothing.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import { rules } from './css.mjs'

const SRC = fileURLToPath(new URL('../../src/', import.meta.url))
const SWITCH_CLASS = /swi(tch)?$/
const KNOWN = ['.ob-switch::after', '.settings-swi::after', '.swi::after', '.two-pane-swi::after']

const escape = (sel) => sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const sheets = readdirSync(SRC, { recursive: true, encoding: 'utf8' })
  .filter((file) => file.endsWith('.css'))
  .map((file) => ({ file, text: readFileSync(join(SRC, file), 'utf8') }))

function animatedKnobs(sheet) {
  const found = []
  for (const [selector, body] of rules(null, sheet.text)) {
    const slides = [...body.matchAll(/transition\s*:\s*([^;]+)/g)].some(([, value]) => !/^\s*none\s*$/.test(value))
    if (!slides) continue
    for (const part of selector.split(',')) {
      const knob = part.trim()
      if (!/::(after|before)$/.test(knob)) continue
      const owner = knob.match(/\.[A-Za-z0-9_-]+/g)?.at(-1)?.slice(1) ?? ''
      if (SWITCH_CLASS.test(owner)) found.push(knob)
    }
  }
  return found
}

function guarded(sheet, selector) {
  const body = sheet.text.replace(/\/\*[\s\S]*?\*\//g, ' ')
  return new RegExp(
    `@media\\s*\\(prefers-reduced-motion:\\s*reduce\\)\\s*\\{[^{}]*${escape(selector)}[^{}]*\\{[^{}]*transition:\\s*none`,
  ).test(body)
}

describe('the switches', () => {
  const perSheet = sheets.map((sheet) => ({ sheet, knobs: animatedKnobs(sheet) }))
  const discovered = perSheet.flatMap(({ knobs }) => knobs)

  it('finds every switch variant the page draws', () => {
    for (const selector of KNOWN) expect(discovered).toContain(selector)
  })

  for (const { sheet, knobs } of perSheet) {
    for (const selector of knobs) {
      it(`${selector} in ${sheet.file} stops under prefers-reduced-motion`, () => {
        expect(guarded(sheet, selector), `${sheet.file} has no reduced-motion guard for ${selector}`).toBe(true)
      })
    }
  }
})
