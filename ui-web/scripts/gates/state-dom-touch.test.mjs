/* Which layer is allowed to reach for an element, and how often.
 *
 * A store writing the flag on its own region is how this page works: the region
 * is rendered by src/App.tsx as the value the page was served with, and exactly
 * one store writes it afterwards (see App.tsx's own list). What the arrangement
 * cannot survive is a second writer, and every second writer starts as one more
 * `getElementById` in a module that already had some -- which is why this
 * counts them per file rather than forbidding them.
 *
 * A ratchet: the numbers below are what the tree holds today, and a file may
 * only go down or disappear. A new file, or a higher count in a pinned one,
 * fails here -- and the fix is to say in that module's header why it reaches
 * for an element and to spend the budget somewhere else, not to raise a number
 * because the sum still looks small.
 *
 * The three layers with no page to reach for are held at zero instead, with
 * their two exceptions registered by name: src/lib/dom.ts IS the page's `$`,
 * and src/components/Ico.tsx builds detached svg nodes with createElementNS,
 * which is a node nobody can reach by id because it has no document in it yet.
 *
 * src/app is pinned rather than zero, because the page's lifecycle is where the
 * elements nobody renders are handled: the pre-JavaScript splash it takes down,
 * the rail it holds on skeleton rows, and the rail-foot row the update notice
 * fills.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { describe, expect, it } from 'vitest'

const SRC = new URL('../../src/', import.meta.url).pathname

/* Reaching for an element this module did not build: by id, by selector, at the
   body, or by making one. `createElementNS` is deliberately not in it. */
const TOUCH = /getElementById|querySelector|document\.body|\bcreateElement\(/

/* Lines matching TOUCH, per file, as the tree stands. Down or gone only. */
const PINNED = {
  'state/banner.ts': 1,
  'state/caps.ts': 3,
  'state/confirm.ts': 2,
  'state/detail.ts': 3,
  'state/envChip.ts': 2,
  'state/find.ts': 1,
  'state/globalListeners.ts': 2,
  'state/lightbox.ts': 3,
  'state/menu.ts': 1,
  'state/navfly.ts': 3,
  'state/overlays.ts': 2,
  'state/page.ts': 3,
  'state/perm.ts': 2,
  'state/portals.ts': 3,
  'state/rail.ts': 2,
  'state/selection.ts': 1,
  'state/session/conversation.ts': 2,
  'state/session/naming.ts': 1,
  'state/session/residency.ts': 1,
  'state/settings.ts': 2,
  'state/sheetRack.ts': 1,
  'state/tier.ts': 2,
  'state/toast.ts': 1,
  'state/tooltip.ts': 1,
  'state/upgradeShade.ts': 1,
  'state/ws.ts': 2,
  'app/boot.ts': 3,
  'app/connection.ts': 1,
  'app/splash.ts': 2,
  'app/updates.ts': 4,
}

/* The two touches that are not a page element, registered one file at a time. */
const EXEMPT = new Set(['lib/dom.ts', 'components/Ico.tsx'])

function* modules(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) yield* modules(path)
    else if (/\.tsx?$/.test(name) && !name.includes('.test.')) yield path
  }
}

/** Every module under one of src/'s directories, by its path from src/. */
const layer = (dir) => [...modules(join(SRC, dir))].map((path) => relative(SRC, path)).sort()

const touches = (rel) => readFileSync(join(SRC, rel), 'utf8').split('\n').filter((line) => TOUCH.test(line))

/** The first comment block, which is where a module says what it is for. */
function header(rel) {
  const text = readFileSync(join(SRC, rel), 'utf8')
  const block = text.match(/^\s*\/\*[\s\S]*?\*\//)
  if (block) return block[0]
  const lines = []
  for (const line of text.split('\n')) {
    if (!line.startsWith('//')) break
    lines.push(line)
  }
  return lines.join('\n')
}

/* Saying why, as far as a gate can read it: the header names the document, a
   node, an element, the markup, or an id. A module that reaches for an element
   and never mentions one in its header is the case this catches. */
const SAYS_WHY = /\bDOM\b|\bdocument\b|\belements?\b|\bnodes?\b|\bmarkup\b|#[A-Za-z]/

describe('the state layer reaching for the page', () => {
  it('touches the DOM only where it is pinned, and never more often', () => {
    const over = []
    for (const rel of [...layer('state'), ...layer('app')]) {
      const found = touches(rel).length
      if (!found) continue
      const pinned = PINNED[rel] ?? 0
      if (found > pinned) over.push(`${rel}: ${found} (pinned ${pinned})`)
    }
    expect(over, 'say in the module header why it reaches for an element, and lower another count instead')
      .toEqual([])
  })

  it('leaves the layers with no page of their own alone', () => {
    const found = []
    for (const dir of ['lib', 'components']) {
      for (const rel of layer(dir)) {
        if (EXEMPT.has(rel)) continue
        for (const line of touches(rel)) found.push(`${rel}: ${line.trim()}`)
      }
    }
    expect(found, 'a pure module reaching for an element belongs in state/ or app/, or takes it as an argument')
      .toEqual([])
  })

  it('reaches for nothing but a detached svg in the one exempt component', () => {
    /* The exemption is for createElementNS, so the file has to stay clear of
       what TOUCH names -- otherwise the entry on the list would cover a real
       reach for a page element. */
    expect(touches('components/Ico.tsx')).toEqual([])
    expect(readFileSync(join(SRC, 'components/Ico.tsx'), 'utf8')).toContain('createElementNS')
  })

  it('says in every pinned module header why it reaches for an element', () => {
    const silent = Object.keys(PINNED).filter((rel) => !SAYS_WHY.test(header(rel)))
    expect(silent, 'name the element or the document in the module header, in one sentence').toEqual([])
  })

  it('pins every file it counts, and counts every file it pins', () => {
    /* The table is read by name, so a renamed module would drop out of the
       count silently and a deleted one would leave a pin nothing tests. */
    const present = new Set([...layer('state'), ...layer('app')])
    expect(Object.keys(PINNED).filter((rel) => !present.has(rel))).toEqual([])
  })
})
