// The stylesheet is frozen for the architecture refactor: every stage promises
// zero change to what the reader sees, and the cheapest half of that promise
// is that page.css itself does not move. A deliberate style change updates
// the digest below in the same PR that makes it, so the diff carries both.
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const PINNED = 'a42577123b92385bf4829e9018a90c4174f73caaeda59ce5d405fccb8e62a04b'

describe('page.css', () => {
  it('is byte-identical to the pinned digest', () => {
    const css = readFileSync(new URL('../src/styles/page.css', import.meta.url))
    expect(createHash('sha256').update(css).digest('hex')).toBe(PINNED)
  })
})
