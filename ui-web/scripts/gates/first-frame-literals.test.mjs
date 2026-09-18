/* The words the page can put on screen that no `t(key)` looked up.
 *
 * CONTRIBUTING section 4.3 is "words come from t(key)", and the catalogue is
 * the one place a language lives -- a literal in a module is a word one
 * reader's language cannot reach. Three sets of them survive, for three
 * different reasons, and this is what keeps each set down or gone:
 *
 *  - The served first frame. `src/page.html` is what a reader sees before any
 *    script has run: the document's `lang` and the no-JavaScript shell that
 *    explains a page whose bundle did not execute. No catalogue has loaded and
 *    no store could read one, so the shell is written in one language.
 *  - A fallback argument. A confirm sheet's title and button, the settings
 *    heading, the capability filter's four chips and its search field, the chat
 *    heading, the environment chip, the permission chip: each renders a literal
 *    while `lang.get()` has answered nothing yet, which is the very frame
 *    `scripts/__golden__/` and `src/test/__golden__/` record. Turning one into
 *    `t()` moves the DOM those goldens are taken from.
 *  - A mark rather than a word. `lib/prose.ts` carries a CJK character class
 *    and a line prefix it matches, `state/session/runtime.ts` strips a
 *    trailing colon of either width, `state/session/naming.ts` compares a
 *    title against the literal `chrome/ChatTop.tsx` serves, and
 *    `features/cron/humanize.ts` joins an already-translated list with an
 *    ideographic comma -- drawn, but punctuation rather than a word.
 *
 * What is counted, for a `.ts` or `.tsx` module: a CJK character inside a
 * string, a template chunk, JSX text, a regular expression or an identifier,
 * found by parsing the module rather than by reading its lines. So a header
 * that quotes one of these strings is not one, and the budget stays a budget
 * for code. `.css` and `.html` have no parser here and lose their comments by
 * text instead.
 *
 * `src/rpc/fixtures/` is out of the scan: the offline library is canned demo
 * content (234 lines across ten files today) and a Chinese playbook in the
 * demo shell is the demo working, not a lookup the page skipped. The screen
 * those fixtures reach is `?stub=1`, never a gateway's.
 *
 * What counts as non-English is the repo's own definition, the `CJK_RUN` of
 * `scripts/check_source_language.py` -- Han, kana, hangul, CJK punctuation and
 * full-width forms. Deliberately narrower than "any non-ASCII": an em-dash in
 * an English comment is not a word. That script is the gate on *adding* one
 * (it reads a PR's added lines); this is the gate on the pile already standing.
 *
 * A ratchet, in the house style of state-dom-touch: a file may go down or
 * disappear, never up, and a file absent from the table may carry none at all.
 * The way off the table is a catalogue key, never another line here.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const SRC = new URL('../../src/', import.meta.url).pathname

/* Han (unified + extension A + compatibility), kana, hangul, CJK punctuation
   and full-width forms. Kept character-for-character the same as
   scripts/check_source_language.py's CJK_RUN, so the two gates cannot disagree
   about what a non-English character is. */
const CJK = /[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]/

/* Lines carrying one, per file, as the tree stands. Down or gone. The header
   says which of the three reasons each file is here for. */
const PINNED = {
  /* the served first frame */
  'page.html': 2,
  /* a fallback the served frame carries, until a language is picked */
  'App.tsx': 3,
  'chrome/CapsPage.tsx': 5,
  'chrome/ChatTop.tsx': 1,
  'chrome/Dock.tsx': 1,
  'chrome/PermChip.tsx': 1,
  'state/envChip.ts': 1,
  /* a message the page writes itself, with no key behind it yet */
  'app/install.ts': 1,
  'features/memory/MemoryPage.tsx': 1,
  'features/plugins/wire.ts': 1,
  /* a mark rather than a word */
  'features/cron/humanize.ts': 2,
  'lib/prose.ts': 2,
  'state/session/naming.ts': 1,
  'state/session/runtime.ts': 1,
}

/* The offline fixture library, whose canned content is the demo shell rather
   than the page's own words. Excluded by name so the exclusion cannot widen
   into the modules above without this line changing. */
const NOT_THE_PAGE = 'rpc/fixtures/'

/* A floor rather than a count: a scan that lost a root would otherwise pass
   forever, which is how the seven calls in app/ once dropped out of rpc-names
   unnoticed. 257 files today. */
const FLOOR = 230

/* Where a character can sit and still be part of the program. Everything else
   in a module -- and a comment is the only everything else that can hold a
   word -- is not counted. */
const IN_THE_PROGRAM = new Set([
  ts.SyntaxKind.Identifier,
  ts.SyntaxKind.JsxText,
  ts.SyntaxKind.NoSubstitutionTemplateLiteral,
  ts.SyntaxKind.PrivateIdentifier,
  ts.SyntaxKind.RegularExpressionLiteral,
  ts.SyntaxKind.StringLiteral,
  ts.SyntaxKind.TemplateHead,
  ts.SyntaxKind.TemplateMiddle,
  ts.SyntaxKind.TemplateTail,
])

/** Every file under src/ this rule reads, by its path from src/. */
function* files(dir) {
  for (const name of readdirSync(dir).sort()) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      if (name !== '__snapshots__' && name !== '__golden__' && name !== 'assets') yield* files(path)
      continue
    }
    const rel = relative(SRC, path)
    if (rel.includes('.test.') || rel.startsWith(NOT_THE_PAGE)) continue
    if (/\.(ts|tsx|css|html|json)$/.test(rel)) yield rel
  }
}

/** The lines of a module whose program text -- not its comments -- carries one. */
function inModule(rel, text) {
  const sf = ts.createSourceFile(
    rel,
    text,
    ts.ScriptTarget.ES2022,
    true,
    rel.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )
  const lines = new Set()
  const walk = (node) => {
    if (IN_THE_PROGRAM.has(node.kind)) {
      const start = node.getStart(sf)
      const body = text.slice(start, node.getEnd())
      for (let i = 0; i < body.length; i++) {
        if (CJK.test(body[i])) lines.add(sf.getLineAndCharacterOfPosition(start + i).line + 1)
      }
    }
    ts.forEachChild(node, walk)
  }
  walk(sf)
  return [...lines].sort((a, b) => a - b)
}

/** Every comment blanked, every other byte and every newline kept. */
function withoutBlocks(text, open, close) {
  let out = ''
  let at = 0
  while (at < text.length) {
    const start = text.indexOf(open, at)
    if (start < 0) return out + text.slice(at)
    const found = text.indexOf(close, start + open.length)
    const end = found < 0 ? text.length : found + close.length
    out += text.slice(at, start) + text.slice(start, end).replace(/[^\n]/g, ' ')
    at = end
  }
  return out
}

const carrying = (text) => text.split('\n').flatMap((line, i) => (CJK.test(line) ? [i + 1] : []))

/** Every file with the lines of it that carry a CJK character outside a comment. */
function found() {
  const hits = {}
  let scanned = 0
  for (const rel of files(SRC)) {
    scanned++
    const text = readFileSync(join(SRC, rel), 'utf8')
    if (!CJK.test(text)) continue
    const lines = /\.tsx?$/.test(rel) ? inModule(rel, text)
      : rel.endsWith('.css') ? carrying(withoutBlocks(text, '/*', '*/'))
        : rel.endsWith('.html') ? carrying(withoutBlocks(text, '<!--', '-->'))
          : carrying(text)
    if (lines.length) hits[rel] = lines
  }
  return { hits, scanned }
}

describe('the words the page shows without a catalogue', () => {
  const { hits, scanned } = found()

  it('reads the tree it means to read', () => {
    expect(scanned, 'the scan lost a root: check the extensions and the skipped directories above')
      .toBeGreaterThan(FLOOR)
  })

  it('holds every file to its pin, and admits no new one', () => {
    const over = Object.entries(hits)
      .filter(([rel, lines]) => lines.length > (PINNED[rel] ?? 0))
      .map(([rel, lines]) => `${rel}:${lines.join(',')} -- ${lines.length} (pinned ${PINNED[rel] ?? 0})`)
    expect(over, 'put the words in i18n/messages.json and render them with t(key); do not raise a number here')
      .toEqual([])
  })

  it('pins nothing it does not count', () => {
    /* The other direction: a literal that became a catalogue key leaves a pin
       with nothing behind it, and the next reader would take the number for a
       debt that is still there. */
    const stale = Object.keys(PINNED).filter((rel) => !(rel in hits)).sort()
    expect(stale, 'the literals are gone -- delete the row, and the exception in CONTRIBUTING section 4.3 with it')
      .toEqual([])
  })

  it('counts the served frame and not the offline library', () => {
    /* Both halves of the scan's shape, so neither can drift: page.html is in
       it because the shell is what a reader sees first, and the fixtures are
       out of it because their content is the demo rather than the page. */
    expect(hits['page.html'], 'page.html carries the no-JavaScript shell; it is in the scan on purpose').toBeDefined()
    expect(Object.keys(hits).filter((rel) => rel.startsWith(NOT_THE_PAGE))).toEqual([])
  })
})
