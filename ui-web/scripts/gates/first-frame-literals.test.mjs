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
 *    no store could read one, so the shell is written in one language. The
 *    `lang` attribute is a tag rather than a word, so the runs counted for
 *    that file are all the shell's.
 *  - A word the page writes with no key behind it yet.
 *    `features/memory/MemoryPage.tsx` spells a month and a day in the arm it
 *    takes for a reader who is in that language.
 *  - A mark rather than a word. `lib/prose.ts` carries a CJK character class
 *    and a line prefix it matches, and `state/session/runtime.ts` strips a
 *    trailing colon of either width -- read, but punctuation rather than a
 *    word.
 *
 * The set that used to stand here and no longer does: the literal every region
 * was served with before a language was picked. Each was Chinese because
 * `src/page.html` was, and each is transcribed rather than looked up because a
 * rendered value would fight the store that owns the element (src/App.tsx names
 * them). They are in English now -- the language the backend defaults to
 * (raven/config/schema.py) and the message catalogue's own spelling of the word
 * each one stands in for -- so the frame before a pick is one language
 * throughout rather than two.
 *
 * The unit is a maximal run of CJK characters, not a line: CJK is written
 * without spaces, so an unbroken run is the closest machine-checkable thing to
 * a word, and a line already on the table cannot carry a second word in for
 * free. A character class pays once per gap its dashes cut, which is why
 * `lib/prose.ts` holds the largest number here and not the largest debt.
 *
 * Where a run can sit and still be part of the program, for a `.ts`/`.tsx`
 * module (`.js`/`.jsx`/`.mjs`/`.cjs` too, though src/ has none today): a
 * string, a template chunk, JSX text, a regular expression or an identifier,
 * found by parsing the module rather than by reading its lines. So a header
 * that quotes one of these strings is not one, and the budget stays a budget
 * for code. A string or template chunk is read cooked, so `'\u65b0'` counts as
 * the character it is; the run is then reported on the node's own first line,
 * having no source position of its own. A run assembled at runtime --
 * `String.fromCharCode(26032)`, a concatenation of escapes -- is out of reach
 * of any parser and is not counted; nothing in src/ does that today, and
 * review is what would catch it. `.css` and `.html` have no parser here and
 * lose their comments by text instead.
 *
 * `src/rpc/fixtures/` is out of the scan: the offline library is canned demo
 * content (489 runs across ten files today) and a Chinese playbook in the
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
 * Going down means lowering the row in the same change, so a number is what
 * the tree holds rather than what it once held. The way off the table is a
 * catalogue key, never another run here.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { basename, join } from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { relPath, root } from './paths.mjs'

const SRC = root(new URL('../../src/', import.meta.url))

/* Han (unified + extension A + compatibility), kana, hangul, CJK punctuation
   and full-width forms. Kept character-for-character the same as
   scripts/check_source_language.py's CJK_RUN, so the two gates cannot disagree
   about what a non-English character is. */
const CJK = /[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]/
const CJK_RUN = /[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]+/g

/* Runs carrying one, per file, as the tree stands. Down or gone. The header
   says which of the three reasons each file is here for. */
const PINNED = {
  /* the served first frame */
  'page.html': 6,
  /* a message the page writes itself, with no key behind it yet */
  'features/memory/MemoryPage.tsx': 2,
  /* a mark rather than a word */
  'lib/prose.ts': 12,
  'state/session/runtime.ts': 1,
}

/* The offline fixture library, whose canned content is the demo shell rather
   than the page's own words. Excluded by name so the exclusion cannot widen
   into the modules above without this line changing. */
const NOT_THE_PAGE = 'rpc/fixtures/'

/* Not the page's source, by path from src/ rather than by bare directory name:
   `assets/` is the one bundled asset folder at the root, so a `chrome/assets/`
   added tomorrow is read like any other module, and the two recorded-output
   names are matched on a path boundary rather than anywhere in a name. */
const NOT_SOURCE = /^assets$|(^|\/)__(snapshots|golden)__$/

/* A test file by its own name, not by the path holding it: `a.test.ts` and
   `test.tsx` are tests, `stray.test.helpers.ts` is production code with a
   test-shaped middle name and is read. */
const A_TEST = /(^|\.)test\.[jt]sx?$/

/* Every extension a run can hide in. `.js`/`.jsx`/`.mjs`/`.cjs` are here
   against the day one appears; src/ carries none today, and the floor below is
   what notices if the walk stops finding the ones that do exist. */
const READ = /\.(ts|tsx|js|jsx|mjs|cjs|css|html|json)$/

/* A floor rather than a count: a scan that lost a root would otherwise pass
   forever, which is how the seven calls in app/ once dropped out of rpc-names
   unnoticed. 245 files today, so five files of slack -- room to delete a
   module without a second edit here, and not room to lose a directory. Re-raise
   it when the count moves. */
const FLOOR = 240

/* Where a run can sit and still be part of the program. Everything else in a
   module -- and a comment is the only everything else that can hold a word --
   is not counted. */
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

/* The ones whose value is what ships rather than what is typed, so an escape
   is read as the character it stands for. The rest keep the source scan: an
   identifier and a regular expression are the characters themselves, and JSX
   text has no escapes of its own. */
const COOKED = new Set([
  ts.SyntaxKind.NoSubstitutionTemplateLiteral,
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
      if (!NOT_SOURCE.test(relPath(SRC, path))) yield* files(path)
      continue
    }
    const rel = relPath(SRC, path)
    if (A_TEST.test(basename(rel)) || rel.startsWith(NOT_THE_PAGE)) continue
    if (READ.test(rel)) yield rel
  }
}

/** The line of every run a module's program text -- not its comments -- carries. */
function inModule(rel, text) {
  const sf = ts.createSourceFile(
    rel,
    text,
    ts.ScriptTarget.ES2022,
    true,
    /\.[tj]sx$/.test(rel) ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )
  const runs = []
  const lineAt = (at) => sf.getLineAndCharacterOfPosition(at).line + 1
  const walk = (node) => {
    if (IN_THE_PROGRAM.has(node.kind)) {
      const start = node.getStart(sf)
      if (COOKED.has(node.kind)) {
        runs.push(...[...node.text.matchAll(CJK_RUN)].map(() => lineAt(start)))
      } else {
        for (const run of text.slice(start, node.getEnd()).matchAll(CJK_RUN)) runs.push(lineAt(start + run.index))
      }
    }
    ts.forEachChild(node, walk)
  }
  walk(sf)
  return runs.sort((a, b) => a - b)
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

const carrying = (text) => text.split('\n')
  .flatMap((line, i) => [...line.matchAll(CJK_RUN)].map(() => i + 1))

/** Every file with the line of each run it carries outside a comment. */
function found() {
  const hits = {}
  let scanned = 0
  for (const rel of files(SRC)) {
    scanned++
    const text = readFileSync(join(SRC, rel), 'utf8')
    /* Every module is parsed whether its bytes carry a CJK character or not:
       an escaped run carries none until the parser cooks it, and skipping the
       file on its raw text is how an escape would go unread. The text paths
       have nothing to cook, so there a file with no run can be skipped. */
    const runs = /\.([tj]sx?|mjs|cjs)$/.test(rel) ? inModule(rel, text)
      : !CJK.test(text) ? []
        : rel.endsWith('.css') ? carrying(withoutBlocks(text, '/*', '*/'))
          : rel.endsWith('.html') ? carrying(withoutBlocks(text, '<!--', '-->'))
            : carrying(text)
    if (runs.length) hits[rel] = runs
  }
  return { hits, scanned }
}

/** The lines a file's runs sit on, a line carrying several saying how many. */
const where = (runs) => [...new Set(runs)].map((line) => {
  const n = runs.filter((l) => l === line).length
  return n > 1 ? `${line} x${n}` : `${line}`
}).join(',')

describe('the words the page shows without a catalogue', () => {
  const { hits, scanned } = found()

  it('reads the tree it means to read', () => {
    expect(scanned, 'the scan lost a root: check the extensions and the skipped directories above')
      .toBeGreaterThan(FLOOR)
  })

  it('holds every file to its pin, and admits no new one', () => {
    const over = Object.entries(hits)
      .filter(([rel, runs]) => runs.length > (PINNED[rel] ?? 0))
      .map(([rel, runs]) => `${rel}:${where(runs)} -- ${runs.length} (pinned ${PINNED[rel] ?? 0})`)
    expect(over, 'put the words in i18n/messages.json and render them with t(key); do not raise a number here')
      .toEqual([])
  })

  it('holds every pin to what the file still carries', () => {
    /* A row that outran its file. Turning one literal of five into a key and
       leaving the 5 behind sells the other four back to the next reader, who
       reads the number as a debt that is still there. */
    const behind = Object.entries(hits)
      .filter(([rel, runs]) => rel in PINNED && runs.length < PINNED[rel])
      .map(([rel, runs]) => `${rel}: ${runs.length} now, pinned ${PINNED[rel]}, lower it`)
    expect(behind, 'a literal became a key: lower the row in PINNED to what the file carries now')
      .toEqual([])
  })

  it('pins nothing it does not count', () => {
    /* The same direction taken all the way: a file whose last literal became a
       catalogue key leaves a pin with nothing at all behind it. */
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
