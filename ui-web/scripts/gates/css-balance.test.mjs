/* Every stylesheet that ships closes every rule it opens.
 *
 * Three rules in components/modelPicker.css were copied out of the prototype a
 * line at a time and lost their continuation line -- the rest of the
 * declarations AND the closing brace. An unclosed rule is not a parse error:
 * CSS nesting reads whatever follows as its descendants, so the whole of
 * features/settings/styles.css, which vite concatenates after it, was parsed as
 * `.mpick .mpp .mpm .settings-panel` and friends. Every one of those selectors
 * matches nothing, and the settings dialog rendered with no styling at all --
 * icons at their natural SVG size, the eight-page layout stacked as text.
 *
 * Nothing saw it. build.py asserts that .modern/domains.css exists, not that it
 * parses; check-class-namespace reads class names, not braces; happy-dom
 * applies no stylesheet, so no DOM test can see paint; and the real-host run
 * asserted on accessibility snapshots and on config.json, neither of which
 * carries a computed style. The page was served broken for a day behind 53
 * green cases.
 *
 * So: the one machine-checkable half of "the stylesheet says what it looks
 * like it says". Depth returns to zero at EOF, and no rule is left open at the
 * end of a sheet that another sheet gets concatenated after.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import { relPath, root } from './paths.mjs'

const src = root(new URL('../../src/', import.meta.url))

/** Every stylesheet the build ships: the page's own, each domain's, each component's. */
function sheets() {
  const found = [join(src, 'styles', 'page.css')]
  for (const [dir, depth] of [['features', 1], ['components', 0]]) {
    const base = join(src, dir)
    for (const entry of readdirSync(base, { withFileTypes: true })) {
      if (depth === 1 && entry.isDirectory()) {
        const sheet = join(base, entry.name, 'styles.css')
        try {
          readFileSync(sheet)
          found.push(sheet)
        } catch {
          /* a domain without its own stylesheet is the norm */
        }
      }
      if (depth === 0 && entry.isFile() && entry.name.endsWith('.css')) found.push(join(base, entry.name))
    }
  }
  return found
}

/** Comments blanked out, newlines kept, so a reported line number is the real one. */
const mask = (css) => css.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))

/** The line each still-open rule was opened on, in order. */
function unclosed(css) {
  const open = []
  const lines = mask(css).split('\n')
  for (let n = 0; n < lines.length; n += 1) {
    for (const ch of lines[n]) {
      if (ch === '{') open.push(n + 1)
      else if (ch === '}') open.pop()
    }
  }
  return open
}

describe('every stylesheet that ships', () => {
  for (const sheet of sheets()) {
    const name = relPath(fileURLToPath(new URL('../../', import.meta.url)), sheet)
    it(`closes every rule it opens -- ${name}`, () => {
      const css = readFileSync(sheet, 'utf8')
      expect({ sheet: name, openedAtLines: unclosed(css) }).toEqual({ sheet: name, openedAtLines: [] })
    })

    it(`closes more than it opens nowhere -- ${name}`, () => {
      let depth = 0
      let below = 0
      for (const ch of mask(readFileSync(sheet, 'utf8'))) {
        if (ch === '{') depth += 1
        else if (ch === '}') {
          depth -= 1
          if (depth < 0) {
            below += 1
            depth = 0
          }
        }
      }
      expect({ sheet: name, strayClosingBraces: below }).toEqual({ sheet: name, strayClosingBraces: 0 })
    })
  }
})
