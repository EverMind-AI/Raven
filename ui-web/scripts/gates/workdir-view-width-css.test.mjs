/* Every view of the working-directory popover past its menu keeps the folder
 * browser's width.
 *
 * The popover sizes itself by its `data-view`: the menu takes the width of
 * its rows, and the folder browser is fixed so its path bar and list do not
 * resize with every folder. The list of drives belongs to the same walk -- it
 * opens from the browser's bar and closes by entering a drive -- so a view the
 * stylesheet does not name falls back to the menu's width, and the popover
 * shrinks and grows back across one click each way.
 *
 * Pinned here because no DOM test can see it: happy-dom applies no
 * stylesheet. The views are read off the store's own union
 * (src/state/workdir.ts), so a view added there without a width fails this
 * rather than drawing at the menu's.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { rules } from './css.mjs'

const store = readFileSync(new URL('../../src/state/workdir.ts', import.meta.url), 'utf8')

/** The popover's views, as `WdState.view` declares them. */
function views() {
  const union = store.match(/readonly view:\s*([^\n]+)/)
  if (!union) throw new Error('no `view` union in src/state/workdir.ts')
  return [...union[1].matchAll(/'([^']+)'/g)].map((m) => m[1])
}

/** The width the stylesheet gives one view, or null where no rule names it. */
function widthOf(view) {
  for (const [selector, body] of rules(/#wdPop\[data-view=/)) {
    if (!selector.split(',').map((s) => s.trim()).includes(`#wdPop[data-view="${view}"]`)) continue
    const width = body.match(/(?:^|;)\s*width\s*:\s*([^;]+)/)
    if (width) return width[1].trim()
  }
  return null
}

describe('the working-directory popover width', () => {
  it('reads the views off the store', () => {
    expect(views()).toEqual(expect.arrayContaining(['menu', 'browse']))
  })

  it("gives every view past the menu the folder browser's own width", () => {
    const browse = widthOf('browse')
    expect(browse).not.toBeNull()
    for (const view of views().filter((v) => v !== 'menu')) {
      expect([view, widthOf(view)]).toEqual([view, browse])
    }
  })
})
