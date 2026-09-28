/* A loading bar shimmers, and holds still for a reader who asked for less
 * motion.
 *
 * The bars are the whole of what a wait says: they carry no text, so a bar
 * that does not move is indistinguishable from an empty grey box that is all
 * the page will ever show. happy-dom applies no stylesheet and runs no
 * animation, so every case that asserts a wait is on screen would pass just as
 * well over a dead rectangle -- the movement is a fact about paint, and the
 * sheet is where it can be pinned.
 *
 * Every sheet that draws one, because a class name has one owner and three
 * namespaces draw a bar: the settings sections under the domain's prefix
 * (features/settings/Skeletons.tsx), the two-pane frame under its own
 * (components/TwoPane.tsx) and the schedules' run history under the cron
 * domain's (features/cron/CronPage.tsx). None may quietly lose its
 * reduced-motion rule.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { css, decls } from './css.mjs'

const settings = readFileSync(new URL('../../src/features/settings/styles.css', import.meta.url), 'utf8')
const cron = readFileSync(new URL('../../src/features/cron/styles.css', import.meta.url), 'utf8')
const extAgents = readFileSync(new URL('../../src/features/extAgents/styles.css', import.meta.url), 'utf8')

const escape = (sel) => sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const BARS = [
  ['.settings-wbar', settings],
  ['.two-pane-wbar', css],
  ['.cronwbar', cron],
  ['.extAgents-wbar', extAgents],
]

describe('the bars a wait is drawn with', () => {
  for (const [selector, sheet] of BARS) {
    it(`${selector} runs a shimmer off keyframes the same sheet defines`, () => {
      const rule = decls(selector, sheet)
      expect(rule).toBeTruthy()
      const animation = rule.get('animation')
      expect(animation).toMatch(/\binfinite\b/)
      const name = animation.trim().split(/\s+/)[0]
      /* A name nothing defines animates nothing, and CSS reports no error. */
      expect(sheet).toMatch(new RegExp(`@keyframes\\s+${escape(name)}\\s*\\{`))
      /* The band has to be wider than the bar for anything to travel across
         it: at 100% the gradient is stationary however it is positioned. */
      expect(rule.get('background-size')).toMatch(/([2-9]\d\d|\d{4,})%/)
    })

    it(`${selector} stops under prefers-reduced-motion`, () => {
      expect(sheet).toMatch(new RegExp(
        `@media\\s*\\(prefers-reduced-motion:\\s*reduce\\)\\s*\\{[^{}]*${escape(selector)}[^{}]*\\{[^{}]*animation:\\s*none`,
      ))
    })
  }
})
