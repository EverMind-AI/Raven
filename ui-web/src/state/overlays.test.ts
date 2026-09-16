/* The order Escape closes things in, pinned before the chain that holds it is
 * replaced.
 *
 * Today the order is a fourteen-branch if chain in the legacy chrome
 * (src/legacy/demo/150-chrome.js). Stage C11 turns it into an ordered
 * priority table in src/state/overlays.ts -- a table, not a stack: each entry
 * answers "am I open" when Escape arrives, so "the last one opened closes
 * first" never happens, which is what the chain does today too.
 *
 * The array below is the expectation. This file asserts it against the chain's
 * source text now; C11 changes what is asserted, not the array. The three
 * capture-phase handlers each open sheet registers run *before* the chain and
 * two of them act on Escape without stopping propagation, so one Escape can
 * both deny an approval and interrupt the running turn. That is today's
 * behaviour, and it is pinned here as a count so it cannot be lost while the
 * chain moves.
 */
import { describe, expect, it } from 'vitest'

// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'

/* The fourteen, in the order Escape reaches them. Each item is the text the
   chain tests to decide whether that layer is on screen -- a selector for the
   twelve elements, the predicate's own name for the last two, which have no
   element of their own to look at. */
const ESCAPE_ORDER = [
  '.lightbox',
  '#veil',
  '#connVeil',
  '#detail',
  '#jobVeil',
  '#cronPage',
  '#memPage',
  '#pbPage',
  '#kbPage',
  '#capsPage',
  '#xaPage',
  '#connPage',
  'setIsOpen()',
  'turn.busy()',
] as const

const source = (path: string): string => readFileSync(path, 'utf8') as string

/* The Escape block of the chrome's document keydown handler: from the key test
   to the brace that closes it. Sliced rather than parsed because the point is
   the order the branches appear in, which is the order they run in. */
function escapeBlock(): string {
  const text = source('src/legacy/demo/150-chrome.js')
  const start = text.indexOf("if (e.key === 'Escape') {")
  expect(start, 'the chrome no longer has an Escape block to read').toBeGreaterThan(-1)
  const end = text.indexOf('\n    }\n', start)
  expect(end, 'the Escape block is not closed where this test expects').toBeGreaterThan(start)
  return text.slice(start, end)
}

describe('the Escape priority order', () => {
  it('is the order the chain tests the fourteen layers in', () => {
    const block = escapeBlock()
    const at = ESCAPE_ORDER.map((item) => {
      const index = block.indexOf(item)
      expect(index, `the Escape chain no longer mentions ${item}`).toBeGreaterThan(-1)
      return index
    })
    expect(new Set(at).size, 'two layers are read from the same place in the chain').toBe(at.length)
    /* Compared as names rather than as offsets so a reordering says which two
       layers swapped. */
    const asWritten = [...ESCAPE_ORDER].sort((a, b) => block.indexOf(a) - block.indexOf(b))
    expect(asWritten).toEqual([...ESCAPE_ORDER])
  })

  it('has no fifteenth branch', () => {
    /* One `if (...) return ...;` per layer and nothing else in the block, so a
       branch added without a place in the array above is caught here rather
       than surviving the rewrite unnoticed. */
    const branches = escapeBlock().match(/^\s+if \(.+\) return .+;$/gm) ?? []
    expect(branches).toHaveLength(ESCAPE_ORDER.length)
  })

  it('runs after the three capture-phase handlers the open sheets register', () => {
    const capture = /document\.addEventListener\('keydown', onKey, true\)/g
    const approve = source('src/features/composer/approve.ts').match(capture) ?? []
    const clarify = source('src/features/composer/clarify.ts').match(capture) ?? []
    expect(approve).toHaveLength(2)
    expect(clarify).toHaveLength(1)
    /* The chain itself is a bubble-phase listener, which is what puts it after
       all three regardless of when they were registered. Two keydown
       registrations in the chrome -- the chain and the settings shortcut --
       and neither asks for capture. */
    const chrome = source('src/legacy/demo/150-chrome.js')
    expect(chrome.match(/document\.addEventListener\('keydown'/g) ?? []).toHaveLength(2)
    expect(chrome).not.toMatch(/document\.addEventListener\('keydown'[\s\S]{0,40}, true\)/)
  })
})
