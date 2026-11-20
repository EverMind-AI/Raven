/* The boot splash moves on the compositor.
 *
 * The splash is on screen for exactly the stretch in which the bundle's one
 * long task holds the main thread: the browser parses, compiles and runs the
 * whole inlined script before it paints again. An animation the main thread
 * has to drive (`left`, `width`, a background position) stops dead for that
 * stretch, so the one line of motion that says the page is loading stands
 * still while it loads. `transform` and `opacity` are run by the compositor
 * and keep moving whatever the main thread is doing.
 *
 * Read from the sheet because the motion is a fact about paint: happy-dom runs
 * no animation, and every boot test would pass as well over a frozen bar.
 */

import { describe, expect, it } from 'vitest'

import { css, decls } from './css.mjs'

const MOVING = ['.sp-rv', '.sp-bar i']
const COMPOSITED = new Set(['transform', 'opacity'])

const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/* A keyframes block's frames, read as the properties they set. */
function keyframeProps(name) {
  const block = css.replace(/\/\*[\s\S]*?\*\//g, ' ')
    .match(new RegExp(`@keyframes\\s+${escape(name)}\\s*\\{((?:[^{}]*\\{[^{}]*\\})*)\\s*\\}`))
  if (!block) return null
  const props = new Set()
  for (const frame of block[1].matchAll(/\{([^{}]*)\}/g)) {
    for (const decl of frame[1].split(';')) {
      const at = decl.indexOf(':')
      if (at > 0) props.add(decl.slice(0, at).trim())
    }
  }
  return props
}

describe('the boot splash', () => {
  for (const selector of MOVING) {
    it(`${selector} animates only what the compositor runs`, () => {
      const animation = decls(selector, css)?.get('animation')
      expect(animation, `${selector} no longer animates; drop it from MOVING`).toBeTruthy()
      const name = animation.trim().split(/\s+/)[0]
      const props = keyframeProps(name)
      expect(props, `@keyframes ${name} is not defined in page.css`).toBeTruthy()
      expect(
        [...props].filter((p) => !COMPOSITED.has(p)),
        `@keyframes ${name} moves a property the main thread drives; move it with transform`,
      ).toEqual([])
    })
  }
})
