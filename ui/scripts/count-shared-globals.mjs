// The dependency-inversion ratchet. The live layer historically worked by
// REASSIGNING bindings it does not own (`SESS = []`, redefining drawList, ...).
// Every such write is one strand of the demo<->live coupling the DataSource
// seam is unwinding, so the count may only go DOWN -- and every move has to be
// recorded HERE, in the same change. EXPECTED below is checked for equality, not
// as a ceiling, which is the point: the number is the debt, in the diff, every
// time, in both directions.
//
// Equality rather than a ceiling for two reasons. Slack hides progress: a
// rebase that lowers the count leaves the gate green and nobody records the
// win, and the next regression then has room to fit under it unnoticed. And
// slack hid the loophole below -- restoring the old narrow definition dropped
// the count by one and still passed. Equality catches both.
//
// WHAT IS COUNTED, and why it is phrased as "does not own" rather than the
// narrower "the demo shell declares it":
//
//   a. The live layer assigns a name the DEMO shell declares. The original
//      definition, and still the bulk of it.
//   b. The live layer assigns a name NOTHING declares. Strictly worse than (a):
//      the live parts are one non-strict arrow IIFE, so such a write silently
//      lands on `window`, which is how it can still function -- and it would
//      THROW at load the day that IIFE became strict or an ES module. Reported
//      separately below for that reason.
//
// This file used to measure something narrower than what it claimed, and missed
// a quarter of the strands. Four parsing holes and one scoping one, each with a
// real escapee. Hole 4 was introduced by the fix for hole 2 and caught in
// review, which is the reason the fixtures below exist at all:
//
//   1. Only the FIRST declarator of a `let a = 1, b = 2` was read as declared.
//      demo/040-state.js opens with eight names on one line, so a live write to
//      any of the last seven was invisible. `busy` and `use` are written from
//      four live files and were not counted.
//   2. Only the FIRST assignment on a line was read. live/060-parked.js:59 is
//      `busy = pk.busy; use = pk.use; tl = pk.tl; raw = pk.raw; lastRun =
//      pk.lastRun; q = pk.q;` -- six writes, one seen. All six are counted now,
//      the last two only because of hole 5.
//   3. Deleting a demo declaration removed a live write from the count while
//      the write stayed. That is case (b): `drawBanner` lost its declaration
//      when the banner moved into the island bundle, and live/120-settings.js
//      still assigns it.
//   4. An INDENTED write matched only when the previous non-space character was
//      `;`, `{` or `}` -- so a comment line above it hid it. A statement under
//      an explanatory comment is the dominant shape in live/, which made a
//      fresh strand introduced that way invisible while every other shape was
//      caught. See the note on writes() below.
//   5. Ownership read over the concatenated layer, so one file's local silenced
//      another file's write. See the note on per-file resolution below.
//
// Hole 3 also means the count could be lowered by deleting a declaration and
// changing nothing, so the fix for it is what makes the gate trustworthy rather
// than merely stricter.
//
// The live layer's OWN bindings are excluded: its top-level state, its function
// locals, its parameters. That is what keeps an ordinary local reassignment
// inside a live function out of the count, rather than the accident that no demo
// file happened to declare the same short name.
//
// Ownership is resolved PER FILE, not over the concatenated layer, and the
// difference is two strands. The live parts are fragments of one IIFE, so a
// column-0 declaration in any of them is visible in all of them -- but a `const`
// inside a function is not, and reading the layer as one string let one file's
// local silence another file's write to a demo global of the same name. `q`
// (live/070-notify.js) and `raw` (live/090-extensions.js) are locals that were
// hiding the writes to the demo's `q` and `raw` at live/060-parked.js:59, which
// is the very line hole 2 is about. Per-file is still coarser than lexical
// scope -- two functions in one file share a namespace here -- so it is a floor
// on the true number, not the true number.
//
//   node ui/scripts/count-shared-globals.mjs         # report + assert
//   node ui/scripts/count-shared-globals.mjs --list  # name every strand
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

const EXPECTED = 13

const src = join(fileURLToPath(new URL('..', import.meta.url)), 'src')
const read = (dir) =>
  readdirSync(join(src, dir))
    .filter((f) => f.endsWith('.js'))
    .sort()
    .map((f) => readFileSync(join(src, dir, f), 'utf8'))
    .join('\n')

const parts = (dir) =>
  readdirSync(join(src, dir))
    .filter((f) => f.endsWith('.js'))
    .sort()
    .map((f) => [f, readFileSync(join(src, dir, f), 'utf8')])

const demo = read('demo')
const liveParts = parts('live')
const live = liveParts.map(([, t]) => t).join('\n')

const NAME = '[A-Za-z_$][\\w$]*'
const first = (s) => (s.trim().match(new RegExp(`^(${NAME})`)) || [])[1]

/* Names a layer declares at its top level -- column 0, the live parts being
   fragments of one IIFE whose bodies all sit there. Visible to every other part,
   which is what makes it right to collect these layer-wide. */
function top(txt) {
  const s = new Set()
  const add = (n) => { if (n) s.add(n) }
  for (const m of txt.matchAll(new RegExp(`^(?:const|let|var)\\s+([^;\\n]*)`, 'gm'))) {
    for (const part of m[1].split(',')) add(first(part))
  }
  for (const m of txt.matchAll(new RegExp(`^(?:async\\s+)?function\\s+(${NAME})`, 'gm'))) add(m[1])
  return s
}

/* Names introduced INSIDE something in one file: an indented declaration, a
   function or arrow parameter, a catch binding, a loop variable. Collected per
   file because that is as far as they reach.

   Generous within the file, and the direction matters: a name wrongly treated
   as owned is not merely untracked, it is PERMANENTLY untracked -- every future
   write to it stays out of the gate. That is a gate passing when it should not,
   the same failure as the holes above, so the cases here are the ones that can
   be read off the syntax rather than guessed at. */
function inner(txt) {
  const s = new Set()
  const add = (n) => { if (n) s.add(n) }
  for (const m of txt.matchAll(new RegExp(`^[ \\t]+(?:const|let|var)\\s+([^;\\n]*)`, 'gm'))) {
    for (const part of m[1].split(',')) add(first(part))
  }
  for (const m of txt.matchAll(new RegExp(`(?:async\\s+)?function\\s*(${NAME})?\\s*\\(([^)]*)\\)`, 'g'))) {
    add(m[1])
    for (const p of m[2].split(',')) add(first(p))
  }
  for (const m of txt.matchAll(/\(([^)]*)\)\s*=>/g)) {
    for (const p of m[1].split(',')) add(first(p))
  }
  for (const m of txt.matchAll(new RegExp(`(${NAME})\\s*=>`, 'g'))) add(m[1])
  for (const m of txt.matchAll(new RegExp(`catch\\s*\\(\\s*(${NAME})`, 'g'))) add(m[1])
  for (const m of txt.matchAll(new RegExp(`for\\s*\\(\\s*(?:const|let|var)?\\s*(${NAME})\\s+(?:of|in)\\s`, 'g'))) {
    add(m[1])
  }
  return s
}

/* Assignments at the start of a statement: the beginning of a line, INDENT AND
   ALL, or just after a `;` or a brace. `[^=>]` after the `=` keeps `==`, `===`
   and `=>` out; compound writes (`+=`, `x.y =`) are not rebinds and are not
   counted.

   The `\\s*` on the line-start branch is hole 4, and it was mine: reading past
   a `;` or a brace is what hole 2 needed, but an anchor without it means an
   INDENTED assignment matches only when the previous non-space character is one
   of those -- and a comment line breaks that run. A statement under an
   explanatory comment is the dominant shape in live/, so a fresh strand
   introduced that way was invisible while every other shape was caught. */
const writes = (txt) => {
  const s = new Set()
  for (const m of txt.matchAll(new RegExp(`(?:^\\s*|[;{}]\\s*)(${NAME})\\s*=[^=>]`, 'gm'))) s.add(m[1])
  return s
}

/* What a layer writes but does not own, given what its whole top level declares.
   Whether the DEMO declares the name plays no part here -- that only decides
   which line of the report a strand lands on. */
const strands = (files, layerTop) => {
  const out = new Set()
  for (const [, txt] of files) {
    const mine = inner(txt)
    for (const n of writes(txt)) if (!layerTop.has(n) && !mine.has(n)) out.add(n)
  }
  return [...out].sort()
}

/* One file, for the fixtures and for reading a snippet as its own layer. */
const one = (txt) => strands([['<fixture>', txt]], top(txt))

/* One fixture per hole this file used to have, checked on every run. A gate that
   silently under-reports is worse than no gate, and the way that happened was a
   parsing hole invisible from the number alone -- so the holes are pinned here
   rather than remembered. Synthetic on purpose: asserting against the real
   sources would only re-state whatever they currently say. */
const fail = (what, want, got) => {
  console.error(`count-shared-globals: self-check failed on ${JSON.stringify(what)}`)
  console.error(`  expected [${want}], got [${got}]`)
  process.exit(1)
}

/* top(): what a layer declares where every part of it can see. Hole 1 lived
   here -- only the first declarator of a comma list was read, and
   demo/040-state.js opens with eight names on one line. */
for (const [text, want] of [
  ['let a = 1, busy = false, kids = [];', ['a', 'busy', 'kids']],
  ['function f(one, two) {}', ['f']],
  // Indented: inside something, so not the layer's top level.
  ['function f() {\n  const hidden = 1;\n}', ['f']],
]) {
  const got = [...top(text)].sort()
  if (String(got) !== String(want.slice().sort())) fail(text, want.slice().sort(), got)
}

/* inner(): what one file introduces inside something. Its reach ends at the
   file, which is why it is collected per file rather than layer-wide -- a local
   here used to silence another file's write to a demo global of the same name. */
for (const [text, want] of [
  ['function f(one, two) {}', ['f', 'one', 'two']],
  // g and h are column-0 declarations, so they are top()'s, not inner()'s.
  ['const g = (x, y) => x;\nconst h = z => z;', ['x', 'y', 'z']],
  ['try { 1 } catch (err) {}\nfor (const it of list) {}', ['err', 'it']],
  ['function f() {\n  const local = 1;\n}', ['f', 'local']],
]) {
  const got = [...inner(text)].sort()
  if (String(got) !== String(want.slice().sort())) fail(text, want.slice().sort(), got)
}

/* A local in one file must NOT silence a write in another -- the whole reason
   ownership is resolved per file. `q` and `raw` were escaping exactly this way. */
{
  const files = [['a.js', 'function f() {\n  const shared = 1;\n}'], ['b.js', 'shared = 2;']]
  const got = strands(files, new Set())
  if (String(got) !== String(['shared'])) fail('a local in another file', ['shared'], got)
}

/* strands(): what a layer writes but does not own. Hole 2 was reading only the
   first assignment on a line; hole 3 was letting a write escape once its
   declaration was deleted. Both are about this function, and the last three
   cases are the ones that must stay OUT. */
for (const [text, want] of [
  ['x = 1; y = 2; z = 3;', ['x', 'y', 'z']],
  ['gone = function () {};', ['gone']],
  // Hole 4: an indented write under a comment, the dominant shape in live/.
  ['/* why this is here */\n  under_block = 1;', ['under_block']],
  ['// why this is here\n  under_line = 1;', ['under_line']],
  ['function f(t) {\n  let s = 1;\n  s = 2; t = 3;\n}', []],
  ['const g = (n) => {\n  n = 1;\n};', []],
  ['if (k === 1) {}\nconst h = (k) => k;\nk.field = 2;', []],
]) {
  const got = one(text)
  if (String(got) !== String(want)) fail(text, want, got)
}

const demoTop = top(demo)
const names = strands(liveParts, top(live))
const implicit = names.filter((n) => !demoTop.has(n))

if (process.argv.includes('--list')) {
  for (const n of names) console.log(implicit.includes(n) ? `${n}  (declared nowhere)` : n)
}
console.log(`live writes to bindings it does not own: ${names.length} (expected ${EXPECTED})`)
if (implicit.length) {
  console.log(`  of those, declared nowhere -- an implicit window write: ${implicit.join(', ')}`)
}
if (names.length > EXPECTED) {
  console.error(`count-shared-globals: the coupling GREW to ${names.length}. New code must go`
    + ' through the DataSource seam, not a shared global. Run with --list to see which.')
  process.exit(1)
}
if (names.length < EXPECTED) {
  console.error(`count-shared-globals: the coupling is down to ${names.length}. Set EXPECTED to`
    + ' that in this same change -- an unrecorded win is room for the next regression to hide in.')
  process.exit(1)
}
