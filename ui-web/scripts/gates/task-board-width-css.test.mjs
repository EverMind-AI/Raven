/* How wide the Task Board page may grow.
 *
 * `.wrap` is every page's centred reading column, capped at 1080px. The task
 * board is a workspace, not a document: its columns are the content, and the
 * cap left a wide window's sides empty while the columns squeezed into the
 * middle. happy-dom applies no stylesheet, so the width is pinned here.
 */

import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

describe('the task board page', () => {
  it('fills the pane rather than the reading column', () => {
    expect(decls('#tasksBoardPage .wrap')?.get('max-width')).toBe('none')
  })
})
