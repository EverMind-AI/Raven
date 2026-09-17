// Every literal `t('gui.x')` / `T('gui.x')` in the page names a key the shared
// catalogue carries. T() falls back to the hard-coded English silently, so a
// misspelled or never-added key ships as untranslated text with no signal --
// two did (see KNOWN_MISSING). Keys built by concatenation (`t('gui.x.' + n)`)
// are not checked: only a literal closed by `,` or `)` is a whole key.
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

const SRC = new URL('../../src/', import.meta.url)
const CATALOGUE = new URL('../../../i18n/messages.json', import.meta.url)

// Absent from the catalogue today; tracked as a copy fix, not fixed here
// because i18n/messages.json is outside the page's own tree.
const KNOWN_MISSING = new Set(['gui.queue.title', 'gui.queue.remove'])

function* sources(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      if (name !== 'node_modules') yield* sources(path)
    } else if (/\.(ts|tsx|js|mjs)$/.test(name) && !/\.test\./.test(name)) {
      yield path
    }
  }
}

describe('i18n keys', () => {
  it('every literal gui.* key exists in the catalogue', () => {
    const ui = JSON.parse(readFileSync(CATALOGUE, 'utf8')).ui
    const referenced = new Set()
    for (const file of sources(SRC.pathname)) {
      const text = readFileSync(file, 'utf8')
      for (const m of text.matchAll(/\b[tT]\('(gui\.[A-Za-z0-9_.]+)'\s*[,)]/g)) referenced.add(m[1])
    }
    const missing = [...referenced]
      .filter((key) => !(key in ui) && !KNOWN_MISSING.has(key))
      .sort()
    expect(missing).toEqual([])
    const healed = [...KNOWN_MISSING].filter((key) => key in ui)
    expect(healed, 'remove healed keys from KNOWN_MISSING').toEqual([])
  })
})
