// Where the conversation surface's type stops: at the agent pane, read off the
// stylesheet. The pane draws the same segments the conversation does -- the
// user's bubble, the process trail, the answer -- but without the reply's card
// around them, so a rule that reads --fs-body unscoped stepped the bubble and
// the trail up while the answer, which only steps up inside that card, stayed
// at the body's size: two sizes in one pane. The pane restates the token at
// the body's size instead, which keeps every reader of it -- the ones there
// now and any added later -- at one size there. happy-dom applies no
// stylesheet, so no component test can see a size at all.
import { describe, expect, it } from 'vitest'

import { decls, rules } from './css.mjs'

/* Both halves of the pane: the transcript box, and the composer under it,
   which sits outside that box in the panel's own detail view. */
const PANE = '.satx, .instance-dock'

const bodySize = () => decls('body').get('font').match(/^(\d+(?:\.\d+)?px)\//)?.[1]

describe('the agent pane type scale', () => {
  it('restates the reply body at the size the rest of the pane reads at', () => {
    const pane = decls(PANE)
    expect(pane, `a ${PANE} rule`).not.toBeNull()
    expect(bodySize()).toMatch(/px$/)
    expect(pane.get('--fs-body')).toBe(bodySize())
    /* The answer's own parts are at the markdown's base sizes there, the
       heading three among them, which is the body size by design. */
    expect(decls('.prose').get('--md-h3')).toBe(bodySize())
  })

  it('reaches the bubble, the trail and the composer through the token', () => {
    /* A literal here would read past the restatement and step the pane up
       again, or hold the conversation down. */
    expect(decls('.msg.me').get('font-size')).toBe('var(--fs-body)')
    expect(decls('.think, .tfh, .wrow.sum').get('font-size')).toBe('var(--fs-body)')
    expect(decls('.wrow').get('font-size')).toBe('var(--fs-body)')
    expect(decls('.dock-in textarea').get('font-size')).toBe('var(--fs-body)')
  })

  it('keeps the bubble at its 40px line at either size', () => {
    /* The inset is (40px - 1.8 lines) / 2; spelled as one number it was right
       for one size only, and the pane's bubble came out short. */
    const padding = decls('.msg.me').get('padding')
    expect(padding).toContain('var(--fs-body)')
    expect(decls('.msg.me').get('line-height')).toBe('1.8')
  })

  it('never steps the pane up again from inside it', () => {
    const readers = rules(/\.satx|\.instance-dock/)
      .filter(([, body]) => /var\(--fs-/.test(body))
      .map(([selector]) => selector)
    expect(readers).toEqual([])
  })
})
