/* Every ink the trajectory sheet uses is defined for every theme.
 *
 * The sheet's colors are tokens set in three blocks: the light :root, the
 * dark theme's attribute, and the system-dark media query that answers when
 * no theme is set. A token missing from a dark block falls back to its light
 * value there -- a near-black ink on the dark pane, the fault check-css.mjs
 * exists to stop -- and no stylesheet test would notice, because the rule
 * itself is written in tokens and passes.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const sheet = readFileSync(new URL('../../src/features/trajectory/styles.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, ' ')

const block = (from, to) => sheet.slice(sheet.indexOf(from), sheet.indexOf(to, sheet.indexOf(from)))
const defined = (text) => new Set([...text.matchAll(/(--trajectory-[a-z0-9-]+)\s*:/g)].map((m) => m[1]))

const used = new Set([...sheet.matchAll(/var\((--trajectory-[a-z0-9-]+)\)/g)].map((m) => m[1]))
const themes = {
  light: defined(block(':root {', '}')),
  dark: defined(block('html[data-theme="dark"] {', '}')),
  'system dark': defined(block('@media (prefers-color-scheme: dark)', '}\n}')),
}

describe('the trajectory sheet tokens', () => {
  it('uses tokens at all, so the checks below have something to read', () => {
    expect(used.size).toBeGreaterThan(20)
  })

  for (const [theme, names] of Object.entries(themes)) {
    it(`defines every token it uses in the ${theme} block`, () => {
      expect([...used].filter((name) => !names.has(name))).toEqual([])
    })
  }
})
