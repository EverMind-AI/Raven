// SPDX-License-Identifier: MIT
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * A scrolled list renders two paging hints, one above the rows and one below,
 * and they are written at opposite ends of a component's JSX. Localizing one
 * half leaves a frame that is Chinese above the rows and English below, which
 * no snapshot catches because the English catalogue entry and the literal it
 * replaced are the same string.
 *
 * The set is derived from the sources rather than listed, so a picker added
 * later is held to the pair without anyone remembering to add it here.
 */

const COMPONENTS = fileURLToPath(new URL('../components', import.meta.url))

function componentSources(): Map<string, string> {
  const sources = new Map<string, string>()
  for (const name of readdirSync(COMPONENTS)) {
    if (name.endsWith('.tsx')) {
      sources.set(name, readFileSync(`${COMPONENTS}/${name}`, 'utf8'))
    }
  }
  return sources
}

function count(source: string, needle: string): number {
  return source.split(needle).length - 1
}

describe('paging hints', () => {
  it('draws both halves of the pair from the catalogue', () => {
    const scrolling = [...componentSources()].filter(([, source]) => source.includes('gui.panel.more_up'))

    expect(scrolling.length).toBeGreaterThan(0)
    const unpaired = scrolling
      .filter(([, source]) => count(source, 'gui.panel.more_down') !== count(source, 'gui.panel.more_up'))
      .map(([name]) => name)
      .sort()
    expect(unpaired, `these render more_up without a matching more_down: ${unpaired.join(', ')}`).toEqual([])
  })

  it('leaves no paging hint hardcoded', () => {
    const literal = /[↑↓] \{[^}]+\} more/
    const hardcoded = [...componentSources()]
      .filter(([, source]) => literal.test(source))
      .map(([name]) => name)
      .sort()
    expect(hardcoded, `these hardcode a paging hint instead of calling uiText: ${hardcoded.join(', ')}`).toEqual([])
  })
})
