// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'

import * as reveal from './reveal'

/* Showing the reader a thing another island owns.
 *
 * The transcript has a citation and the knowledge page can open it; neither
 * may import the other. What is under test is that the island which can do it
 * says so, and that a page without it does nothing rather than throwing.
 */

beforeEach(() => {
  reveal._resetForTests()
})

describe('revealing a document', () => {
  it('does nothing, and says so, until an island offers', () => {
    /* A transcript restored on a page without the knowledge island still draws
       its citations. Pressing one must not throw. */
    expect(reveal.canOpenDocument()).toBe(false)
    expect(reveal.document({ baseId: 'kb-a', documentId: 'd1', chunkIndex: 0 })).toBe(false)
  })

  it('hands the whole address to whoever registered', () => {
    const seen: unknown[] = []
    reveal.onDocument((at) => seen.push(at))

    expect(reveal.document({ baseId: 'kb-a', documentId: 'd1', chunkIndex: 4 })).toBe(true)

    expect(seen).toEqual([{ baseId: 'kb-a', documentId: 'd1', chunkIndex: 4 }])
  })

  it('can be taken back, for a boot that installs twice', () => {
    reveal.onDocument(() => undefined)
    reveal.onDocument(null)

    expect(reveal.canOpenDocument()).toBe(false)
  })
})
