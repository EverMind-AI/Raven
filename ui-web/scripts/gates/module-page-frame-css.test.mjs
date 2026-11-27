/* Every module page sits in the task board's frame.
 *
 * The four module pages (src/state/pages.ts) share one shell -- `.page > .work
 * > .wrap` -- and each page's island draws its own first row into it. When each
 * page also tuned its own `.wrap`, the pages drifted: switching between them
 * on the rail moved the left edge, the right edge and the first row, and one
 * of them capped its width and centred itself on a wide window. The task board
 * is the reference: its frame is the base `.wrap`'s padding with no width cap.
 *
 * Read from the sheets because the frame is a fact about layout, which
 * happy-dom does not do. A page may still set what is its own (the knowledge
 * page's bottom padding, its full-height column); the top, the two sides and
 * the cap are the frame.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { css, rules } from './css.mjs'

const FEATURES = new URL('../../src/features/', import.meta.url)
const domainSheets = readdirSync(FEATURES, { withFileTypes: true })
  .filter((d) => d.isDirectory())
  .flatMap((d) => {
    try {
      return [readFileSync(new URL(`${d.name}/styles.css`, FEATURES), 'utf8')]
    } catch {
      return []
    }
  })

/* In the order build.py splices them: page.css, then the domains' sheets. */
const SHEETS = [css, ...domainSheets]

/* Read from the source text rather than imported, as the other gates read it. */
const pagesTs = readFileSync(new URL('../../src/state/pages.ts', import.meta.url), 'utf8')
const PAGE_IDS = [...pagesTs.matchAll(/\{\s*id:\s*'(\w+Page)'/g)].map((m) => m[1])

function applyDecls(frame, body) {
  for (const decl of body.split(';')) {
    const at = decl.indexOf(':')
    if (at <= 0) continue
    const prop = decl.slice(0, at).trim()
    const value = decl.slice(at + 1).trim()
    if (prop === 'padding') {
      const [t, r = t, b = t, l = r] = value.split(/\s+/)
      Object.assign(frame, { top: t, right: r, bottom: b, left: l })
    } else if (prop === 'padding-top') frame.top = value
    else if (prop === 'padding-right') frame.right = value
    else if (prop === 'padding-left') frame.left = value
    else if (prop === 'max-width') frame.max = value
  }
}

/* The frame a page's `.wrap` ends up with: the base rule, then every rule that
   names `#<id> .wrap` in its selector list, later sheets over earlier ones. */
function frameOf(id) {
  const frame = { top: '0', right: '0', bottom: '0', left: '0', max: 'none' }
  for (const sheet of SHEETS) {
    for (const [selector, body] of rules(null, sheet)) {
      const names = selector.split(',').map((s) => s.trim())
      if (sheet === css && names.includes('.wrap')) applyDecls(frame, body)
    }
  }
  for (const sheet of SHEETS) {
    for (const [selector, body] of rules(null, sheet)) {
      if (selector.split(',').map((s) => s.trim()).includes(`#${id} .wrap`)) applyDecls(frame, body)
    }
  }
  return { top: frame.top, left: frame.left, right: frame.right, max: frame.max }
}

describe('the module pages', () => {
  it('are the five pages src/state/pages.ts declares', () => {
    expect(PAGE_IDS).toContain('tasksBoardPage')
    expect(PAGE_IDS).toContain('skillsToolsPage')
    expect(PAGE_IDS).toHaveLength(5)
  })

  const board = frameOf('tasksBoardPage')

  it('take the task board as the frame: no width cap', () => {
    expect(board.max).toBe('none')
  })

  for (const id of PAGE_IDS) {
    it(`#${id} sits in the task board's frame`, () => {
      expect(frameOf(id), `make #${id} .wrap keep the task board's top padding, side padding and no width cap`)
        .toEqual(board)
    })
  }
})
