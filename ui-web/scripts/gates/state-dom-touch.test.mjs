/* Which layer is allowed to reach for an element, how often, and what it may
 * write once it has one.
 *
 * A store writing the flag on its own region is how this page works: the region
 * is rendered by src/App.tsx as the value the page was served with, and exactly
 * one store writes it afterwards (see App.tsx's own list). What the arrangement
 * cannot survive is a second writer, and every second writer starts as one more
 * `getElementById` in a module that already had some -- which is why this
 * counts them per file rather than forbidding them.
 *
 * Every match is counted, not every line that holds one: `document.body` beside
 * a `querySelector` in one ternary is two reaches, and a number that claims to
 * say how often has to answer for both. The page's own `$` (src/lib/dom.ts) is
 * `document.querySelector` under a shorter name, so it is a reach like any
 * other -- five modules reach that way and nothing else -- and the only file it
 * is not counted in is the one that defines it, on the EXEMPT list below.
 *
 * A ratchet, and both halves of one: the numbers below are what the tree holds
 * today, a file may only go down or disappear, and a count that has FALLEN
 * fails as well, naming the number to write instead. A new file, or a higher
 * count in a pinned one, fails -- and the fix is to say in that module's header
 * why it reaches for an element and to spend the budget somewhere else, not to
 * raise a number because the sum still looks small. A pin nobody lowers is
 * budget the tree grows back into for free.
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
 *
 * WRITES is the other half of the rule, over the same layers: the text, the
 * class and the markup of an element that was rendered elsewhere. A store may
 * write the flag on its own region (`data-open`), and that is a dataset write
 * this does not count; setting `textContent` or `innerHTML` on a node React
 * owns is the case rule (a) does not reach, and the twenty-four the tree still
 * holds are counted per file so they can only fall. A file absent from that
 * table may not write at all. features/ is out of this gate's reach, here as
 * above -- CONTRIBUTING section 4.2 says why.
 *
 * The page frame -- src/chrome/, src/App.tsx and src/main.tsx -- is counted in
 * FRAME instead, and its reaches are not violations of the rule above: that
 * rule is about a second writer on a region a store already owns, and these are
 * the page's own imperative behaviours. App.tsx portals into the body, the two
 * popovers write into a list the page was served with, main.tsx mounts the
 * island roots in the boxes that root committed, and chrome/behaviour/ is two
 * modules that say in their own headers that they are behaviour rather than
 * rendering: a grip drag writing one CSS variable, and scrollbar thumbs parked
 * in a fixed layer over elements other layers drew. Counting them is what keeps
 * a third such module from appearing unnoticed -- the counts may only fall, and
 * a frame file that is not on the list may not reach for an element at all.
 *
 * What the counts read is text, and three limits come with that. Two of the
 * twenty-six matches FRAME holds are a sentence in a module header that names
 * document.body, and two more are React's own `createElement` in main.tsx,
 * which the regex cannot tell from the document's: a budget that counts a
 * little too much is the safe direction for a ratchet. `event.target.closest()`
 * is deliberately not a reach -- it climbs from a node the handler was handed,
 * which is what every delegated listener on this page does, and counting it
 * would put a number on lib/popover.ts for reading the card around an anchor it
 * takes as an argument. And a reach spelled through an alias
 * (`const doc = globalThis.document`, then `doc.body`) is invisible to a text
 * match; no module does that today, and the honest statement is that this gate
 * would not see it.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import { relPath, root } from './paths.mjs'

const SRC = root(new URL('../../src/', import.meta.url))

/* Reaching for an element this module did not build: by id, by class or tag
   name, by selector, through the page's own `$`, at the body, at the focused
   element, at a form, by hit test, or by making one. `createElementNS` is
   deliberately not in it, and the header says why `.closest(` is not either.
   Global, and read through `matchAll` only -- `test()` on a global regex
   carries its `lastIndex` from one call into the next. */
const TOUCH = /getElementById|getElementsBy|querySelector|elementFromPoint|document\.(?:body|activeElement|forms)|\$\(|\bcreateElement\(/g

/* Writing the text, the class or the markup of an element. A dataset write is
   not here: that is the flag a store sets on its own region, which is the one
   imperative write the page is arranged around. */
const WRITE = /\.(?:innerHTML|outerHTML|textContent|innerText|className)\s*\+?=(?!=)|\.classList\.(?:add|remove|toggle|replace)\(/g

/* TOUCH matches, per file, as the tree stands. Down or gone only. */
const PINNED = {
  'state/banner.ts': 1,
  'state/caps.ts': 3,
  'state/confirm.ts': 2,
  'state/detail.ts': 3,
  'state/envChip.ts': 3,
  'state/escapeOrder.ts': 2,
  'state/find.ts': 1,
  'state/globalListeners.ts': 3,
  'state/lightbox.ts': 3,
  'state/menu.ts': 1,
  'state/navfly.ts': 3,
  'state/page.ts': 4,
  'state/perm.ts': 3,
  'state/portals.ts': 3,
  'state/proseChips.ts': 1,
  'state/rail.ts': 2,
  'state/selection.ts': 1,
  'state/session/conversation.ts': 2,
  'state/session/naming.ts': 3,
  'state/session/registry.ts': 6,
  'state/session/residency.ts': 3,
  'state/session/runtime.ts': 3,
  'state/settings.ts': 2,
  'state/sheetRack.ts': 2,
  'state/tier.ts': 3,
  'state/toast.ts': 1,
  'state/tooltip.ts': 1,
  'state/upgradeShade.ts': 1,
  'state/ws.ts': 2,
  'app/boot.ts': 3,
  'app/connection.ts': 1,
  'app/install.ts': 1,
  'app/splash.ts': 2,
  'app/updates.ts': 4,
}

/* The same, for the page frame's own imperative behaviours, which the header
   says are not the rule's case. Down or gone: the numbers are what the frame
   holds today, and a file absent from the list may not reach at all. */
const FRAME = {
  'App.tsx': 3,
  'main.tsx': 5,
  'chrome/FailureBar.tsx': 1,
  'chrome/Lightbox.tsx': 1,
  'chrome/PermPopover.tsx': 2,
  'chrome/TierPopover.tsx': 2,
  'chrome/UpgradeShade.tsx': 1,
  'chrome/WsPane.tsx': 2,
  'chrome/behaviour/panes.ts': 6,
  'chrome/behaviour/scrollbars.ts': 3,
}

/* WRITE matches, per file, over every layer this gate reads. Down or gone, and
   a file that is not here writes nothing: the way off a row is to let the
   markup say it (a React child, a class in JSX), not to move the write. */
const WRITES = {
  'state/detail.ts': 1,
  'state/envChip.ts': 2,
  'state/globalListeners.ts': 2,
  'state/portals.ts': 2,
  'state/session/naming.ts': 4,
  'state/session/registry.ts': 4,
  'state/session/runtime.ts': 1,
  'state/ws.ts': 3,
  'app/updates.ts': 4,
  'chrome/behaviour/scrollbars.ts': 1,
}

/* The two touches that are not a page element, registered one file at a time. */
const EXEMPT = new Set(['lib/dom.ts', 'components/Ico.tsx'])

/* The pinned modules whose header does not name the element they reach for, one
   file at a time. state/session/registry.ts reaches four ids through `$` --
   #stage, #flash, #title, #ta -- and its header is about which conversation the
   page is on; the row comes off when that header gains the sentence, and a
   header that has gained it fails below until the row is deleted. */
const SILENT = new Set(['state/session/registry.ts'])

/* A test file, told by its own name and not by a substring of the path: a
   `grip.test.helpers.ts` is a module that helps a test, and the rule holds over
   it like over any other module. */
const TEST = /(^|\.)test\.[jt]sx?$/

/* What counts as a module of this page: .ts and .tsx, plus the .js, .mjs and
   .cjs that a copied helper or a generated file would arrive as. No directory
   is skipped, so a reach cannot sit in one and go unread. */
const MODULE = /\.(?:[cm]?[jt]s|[jt]sx)$/

function* modules(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) yield* modules(path)
    else if (MODULE.test(name) && !TEST.test(name)) yield path
  }
}

/** Every module under one of src/'s directories, by its path from src/. */
const layer = (dir) => [...modules(join(SRC, dir))].map((path) => relPath(SRC, path)).sort()

/** The page frame: the chrome, and the two files that assemble the page. */
const frame = () => [...layer('chrome'), 'App.tsx', 'main.tsx'].sort()

const source = (rel) => readFileSync(join(SRC, rel), 'utf8')

/** Every match of `re` in one file, each as `line: the text that matched`. */
function found(rel, re) {
  const text = source(rel)
  return [...text.matchAll(re)].map((m) => {
    const line = text.slice(0, m.index).split('\n').length
    return `${line}: ${m[0]}`
  })
}

const touches = (rel) => found(rel, TOUCH)
const writes = (rel) => found(rel, WRITE)

/** One table against one measurement: over its pin here, under it in `stale`. */
function ratchet(files, pinned, measure, stale) {
  const over = []
  for (const rel of files) {
    const count = measure(rel).length
    const pin = pinned[rel]
    if (pin === undefined) {
      if (count) over.push(`${rel}: ${count}, not on the list`)
      continue
    }
    if (count > pin) over.push(`${rel}: ${count} (pinned ${pin})`)
    else if (count < pin) stale.push(`${rel}: ${count} now, pinned ${pin}, lower it`)
  }
  return over
}

/** All three tables measured in one pass: what is over a pin, and what is under
    one. Measured per test rather than shared, so no test depends on another
    having run first. */
function survey() {
  const stale = []
  const pinned = ratchet([...layer('state'), ...layer('app')], PINNED, touches, stale)
  const frameOver = ratchet(frame(), FRAME, touches, stale)
  const written = [...layer('state'), ...layer('app'), ...frame(), ...layer('lib'), ...layer('components')]
  const writeOver = ratchet(written, WRITES, writes, stale)
  return { frame: frameOver, pinned, stale, writes: writeOver }
}

/** The first comment block, which is where a module says what it is for. */
function header(rel) {
  const text = source(rel)
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

describe('which layer reaches for the page, and how often', () => {
  it('touches the DOM only where it is pinned, and never more often', () => {
    expect(survey().pinned, 'say in the module header why it reaches for an element, and lower another count instead')
      .toEqual([])
  })

  it('holds the frame to the imperative behaviours it already has', () => {
    expect(survey().frame, 'the frame installs behaviour rather than adding reaches: spend a count it already has')
      .toEqual([])
  })

  it('writes the text, class or markup of an element only where it is pinned', () => {
    expect(survey().writes, 'render the text or the class instead: a write into a node React owns may only be lost')
      .toEqual([])
  })

  it('leaves the layers with no page of their own alone', () => {
    const reaching = []
    for (const dir of ['lib', 'components']) {
      for (const rel of layer(dir)) {
        if (EXEMPT.has(rel)) continue
        for (const hit of touches(rel)) reaching.push(`${rel}:${hit}`)
      }
    }
    expect(reaching, 'a pure module reaching for an element belongs in state/ or app/, or takes it as an argument')
      .toEqual([])
  })

  it('reaches for nothing but a detached svg in the one exempt component', () => {
    /* The exemption is for createElementNS, so the file has to stay clear of
       what TOUCH names -- otherwise the entry on the list would cover a real
       reach for a page element. */
    expect(touches('components/Ico.tsx')).toEqual([])
    expect(source('components/Ico.tsx')).toContain('createElementNS')
  })

  it('says in every pinned module header why it reaches for an element', () => {
    const silent = Object.keys(PINNED).filter((rel) => !SAYS_WHY.test(header(rel)) && !SILENT.has(rel))
    expect(silent, 'name the element or the document in the module header, in one sentence').toEqual([])
    const spoken = [...SILENT].filter((rel) => SAYS_WHY.test(header(rel)))
    expect(spoken, 'this header names an element now: take the file off SILENT').toEqual([])
  })

  it('has no pin the tree has outgrown', () => {
    /* The other half of down-or-gone, for all three tables at once: a file that
       reaches or writes less often than its pin says -- a pinned file down to
       zero included -- leaves a budget nobody spent, and the next reach lands
       inside it without failing anything. */
    expect(survey().stale, 'the count fell: write the number below it, or delete the row').toEqual([])
  })

  it('pins every file it counts, and counts every file it pins', () => {
    /* Every table is read by name, so a renamed module would drop out of the
       count silently and a deleted one would leave a pin nothing tests. */
    const present = new Set([...layer('state'), ...layer('app')])
    expect(Object.keys(PINNED).filter((rel) => !present.has(rel))).toEqual([])
    const framed = new Set(frame())
    expect(Object.keys(FRAME).filter((rel) => !framed.has(rel))).toEqual([])
    const written = new Set([...present, ...framed, ...layer('lib'), ...layer('components')])
    expect(Object.keys(WRITES).filter((rel) => !written.has(rel))).toEqual([])
    expect([...SILENT].filter((rel) => PINNED[rel] === undefined)).toEqual([])
  })
})
