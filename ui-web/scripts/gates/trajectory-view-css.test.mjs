/* What the trajectory view hides, and what it must not.
 *
 * While a trace is up the composer card and the task strip step aside, but the
 * sheet rack stays: an approval, confirm or clarify sheet raised by the open
 * conversation is still waiting on the reader, and its keyboard handler keeps
 * listening whether or not it can be seen -- so a hidden sheet would be denied
 * by the Escape that closes the details pane. Pinned against page.css because
 * happy-dom applies no stylesheet: a hidden rack and a shown one are the same
 * tree there.
 */

import { describe, expect, it } from 'vitest'

import { rules } from './css.mjs'

const hidden = rules(/data-traj/).filter(([, body]) => /display\s*:\s*none/.test(body))

describe('the trajectory view', () => {
  it('hides nothing the sheet rack sits in', () => {
    const subjects = hidden.map(([selector]) => selector.split(/[\s>+~]+/).pop())
    for (const subject of subjects) {
      expect(['.chat', '.dock', '#sheetRack', '.sheets', '*']).not.toContain(subject)
    }
  })

  it('still hides the composer card and the task strip beside the rack', () => {
    expect(hidden.map(([selector]) => selector)).toContain('html[data-traj] .chat > .dock > :not(#sheetRack)')
  })
})
