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
//   6. A declarator list was split on every comma, so the parameters of a
//      column-0 arrow became declarations: `const mk = (t, c, x) => ...` claimed
//      `c` and `x`. In the LIVE layer that silences every write to such a name.
//      Seventeen names were phantom this way and none had a write behind it yet,
//      so unlike holes 1-5 this one had no escapee -- it was found by reading the
//      declaration set, not by the number moving. See declarators() below.
//
// Hole 3 also means the count could be lowered by deleting a declaration and
// changing nothing, so the fix for it is what makes the gate trustworthy rather
// than merely stricter.
//
// WHAT IS NOT COUNTED ABOVE, and is counted separately below: the live layer
// reaching INTO a container the demo layer declared instead of rebinding the
// name -- `TOOLS.length = 0` and then `TOOLS.push(...)`. Note that the two
// counts answer OPPOSITE questions about `+=`: a compound write is excluded from
// the rebind count because it does not rebind, and included in the container
// count because writing into a field is exactly the dependency there. `WS.turn
// += 1` escaped both until review pointed it out. Not a rebind, so it is
// none of the gate's original business, but the same demo<->live dependency: the
// demo's fixture arrays are where the live layer keeps its data, which is why
// they cannot be deleted with the renderers that read them. live/090's own
// header says so outright ("written into the demo's data arrays IN PLACE, so
// every existing renderer keeps working"). It went unmeasured for as long as it
// did because both gates look at names, and this coupling does not go through
// one.
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

const EXPECTED = 11
const EXPECTED_HELD = 8
const EXPECTED_VERBS = 59

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

/* Split a declarator list on the commas that separate declarators, which are the
   ones at bracket depth zero. A plain `split(',')` was hole 6: it cut inside a
   parameter list, an array literal or an object literal on the right-hand side,
   so `const mk = (t, c, x) => ...` registered `mk`, `c` and `x` as declarations.

   That direction is the dangerous one. A phantom declaration in the LIVE layer's
   top level silences every live write to that name -- the same escape as holes
   1-5, reached through the ownership test rather than through the write test.
   Seventeen names were phantom when this was found (`open`, `error`, `say`,
   `steps`, `H`, `PAD` among them); none had a write behind it yet, so the count
   did not move, which is exactly why it had to be found by reading rather than
   by the number. */
function declarators(list) {
  const out = []
  let depth = 0
  let buf = ''
  for (const ch of list) {
    if ('([{'.includes(ch)) depth++
    else if (')]}'.includes(ch)) depth--
    if (ch === ',' && depth === 0) { out.push(buf); buf = ''; continue }
    buf += ch
  }
  out.push(buf)
  return out
}

/* Names a layer declares at its top level -- column 0, the live parts being
   fragments of one IIFE whose bodies all sit there. Visible to every other part,
   which is what makes it right to collect these layer-wide. */
function top(txt) {
  const s = new Set()
  const add = (n) => { if (n) s.add(n) }
  for (const m of txt.matchAll(new RegExp(`^(?:const|let|var)\\s+([^;\\n]*)`, 'gm'))) {
    for (const part of declarators(m[1])) add(first(part))
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
    for (const part of declarators(m[1])) add(first(part))
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

/* Mutating a container rather than rebinding the name. The four shapes the live
   layer actually uses on a demo array or object: emptying it, a mutating method,
   a field, an index.

   Read-only methods are deliberately absent. Live code READS demo globals
   constantly -- `TOOLS.filter(...)`, `SESS.find(...)` -- and that is a coupling
   too, but a far larger and differently-shaped one; folding it in here would
   produce a number nobody can act on. This counts the writes. */
const MUTATORS = ['push', 'pop', 'shift', 'unshift', 'splice', 'sort', 'reverse',
  'fill', 'set', 'delete', 'clear', 'add']

const mutates = (txt) => {
  const s = new Set()
  const shapes = [
    new RegExp(`(${NAME})\\.(?:${MUTATORS.join('|')})\\(`, 'g'),
    /* A field, which covers `.length = 0` as well -- `length` is a field name,
       so a separate pattern for emptying an array matched nothing the field
       pattern had not already matched. There was one here until a mutation run
       showed removing it changed no number and broke no fixture. */
    new RegExp(`(${NAME})\\.${NAME}\\s*=[^=>]`, 'g'),
    new RegExp(`(${NAME})\\[[^\\]]+\\]\\s*=[^=>]`, 'g'),
    /* Every field at once, with the container's name nowhere on a left-hand
       side. `Object.assign(WS, pk.ws)` overwrites all five of WS's fields and
       matched none of the shapes above. */
    new RegExp(`Object\\.assign\\(\\s*(${NAME})`, 'g'),
    /* A compound field write. `writes()` deliberately excludes `+=` because its
       question is whether a NAME was rebound, and `x.f += 1` does not rebind
       anything -- but that is the opposite of the question here, where writing
       into a field is precisely the coupling. `WS.turn += 1` was invisible for
       want of this. */
    new RegExp(`(${NAME})\\.${NAME}\\s*(?:\\+\\+|--|[-+*/|&^]=)`, 'g'),
  ]
  for (const re of shapes) for (const m of txt.matchAll(re)) s.add(m[1])
  return s
}

/* Containers the live layer mutates in place. The name must be one the DEMO
   layer declares -- unlike the rebind count above, which also reports a write to
   a name nothing declares. The asymmetry is not an oversight: an undeclared
   rebind silently lands on `window` and keeps working, so it is a strand; an
   undeclared `FOO.push(...)` throws, so it cannot be live code. Requiring the
   demo declaration is also what keeps `window.x = 1` and `document.title = ...`
   out, without a list of platform names to maintain. */
const containers = (files, layerTop, demoNames) => {
  const out = new Set()
  for (const [, txt] of files) {
    const mine = inner(txt)
    for (const n of mutates(txt)) {
      if (demoNames.has(n) && !layerTop.has(n) && !mine.has(n)) out.add(n)
    }
  }
  return [...out].sort()
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

/* The third direction, and the only one that runs the other way. Both counts
   above measure the LIVE layer writing something the demo layer owns. This one
   measures a migrated island asking the legacy layer a question: every member of
   `interface Shell` is one answer an island cannot get from its own source, and
   `demo/155-bridge.js` publishes every one of them.

   So the islands do not stand on their own -- they stand on this many answers
   from the layer the migration exists to retire, and until now nothing watched
   the number, which is how it grew quietly.

   NOT an argument for deleting verbs: some are permanent seams by design (`T` has
   to come from wherever the catalogue lives). It is the same argument the two
   counts above make -- the number belongs in the diff of the change that moves
   it, in both directions.

   Members, not callables: 60 members are 63 callables, because two of those
   slots (`look` and `ntf`) are namespaces holding five methods between them.
   Counting members keeps the unit the same as the thing a reviewer sees added to
   the interface, and makes the number a floor on the questions being asked
   rather than the exact count -- the same compromise, and for the same reason,
   as the per-file ownership above. */
const bridgeTs = readFileSync(join(src, 'shell', 'bridge.ts'), 'utf8')

function shellVerbs(txt) {
  /* Comments out first, so a `{` or a member-shaped sentence inside one cannot
     move the depth or be counted. Both comment forms appear in this interface. */
  const clean = txt.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')
  const open = clean.indexOf('export interface Shell {')
  if (open < 0) return null
  const body = clean.slice(clean.indexOf('{', open) + 1)
  const out = []
  let depth = 0
  for (const line of body.split('\n')) {
    /* Depth is read BEFORE the line is counted: a member sits at depth 0 of the
       body, and a signature that opens and closes its brackets on one line --
       which is every member today -- never hides the next one.
       Parens count as well as braces, and that is not symmetry for its own
       sake. A member whose PARAMETER LIST is wrapped across lines would
       otherwise put each parameter at depth 0 and have it counted as a verb:
       `agentStagePaint?(` is 114 characters against a printWidth of 120, so
       nothing wraps it today. Wrap it by hand and, without this, the count reads
       63: that member takes three parameters, and each of the three lands at
       depth 0 and matches the member shape -- `box`, `ctx` and `opts`, measured.
       CI would then blame a reformat for adding three verbs. */
    if (depth === 0) {
      const m = line.match(new RegExp(`^\\s+(${NAME})\\??\\s*[(:]`))
      if (m) out.push(m[1])
    }
    for (const ch of line) {
      if (ch === '{' || ch === '(') depth++
      else if (ch === '}' || ch === ')') depth--
    }
    if (depth < 0) break // the interface's own closing brace
  }
  return out.sort()
}

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

/* declarators(): hole 6. The first case is the shape that caused it, and the
   next two are the other right-hand sides a comma appears in -- an array and an
   object literal -- because a fix that only knew about parentheses would still
   split those. The last is what the function exists for in the first place, and
   has to keep working. */
for (const [text, want] of [
  ['const mk = (t, c, x) => x;', ['mk']],
  ['const pair = [a, b];', ['pair']],
  ['const o = { one: 1, two: 2 };', ['o']],
  ['let a = 1, b = 2, c = 3;', ['a', 'b', 'c']],
]) {
  const got = [...top(text)].sort()
  if (String(got) !== String(want.slice().sort())) fail(text, want.slice().sort(), got)
}

/* containers(): the four mutating shapes are counted, reads are not, and the
   name has to be one the demo declares -- which is what keeps the platform's
   own objects out without naming them. The `window` case is the one that would
   flood the number if the ownership test were the looser "does not own" the
   rebind count uses. */
for (const [text, want] of [
  ['LIST.push(1);', ['LIST']],
  // Still counted, now through the field shape rather than one of its own.
  ['LIST.length = 0;', ['LIST']],
  ['MAP.set(k, v);', ['MAP']],
  ['CFG.field = 1;', ['CFG']],
  ['SEEN[k] = 1;', ['SEEN']],
  ['Object.assign(CFG, next);', ['CFG']],
  ['CFG.turn += 1;', ['CFG']],
  ['CFG.turn++;', ['CFG']],
  // Copying OUT of a container is a read; the target is what gets written.
  ['const snap = Object.assign({}, CFG);', []],
  // Reads: a coupling, but not this one, and folding them in makes the number unusable.
  ['const n = LIST.filter((x) => x).length;\nLIST.find((x) => x);', []],
  // Not declared by the demo layer, so not a demo container.
  ['window.thing = 1;\ndocument.title = "x";', []],
]) {
  const demoNames = new Set(['LIST', 'MAP', 'CFG', 'SEEN'])
  const got = containers([['<fixture>', text]], top(text), demoNames)
  if (String(got) !== String(want)) fail(text, want, got)
}

/* shellVerbs(): the members of one interface in a .ts file, which is a different
   parsing problem from everything above -- comments carry braces, and a
   signature can open and close an object type on its own line. The last case is
   the one that would silently halve the number: a member after one of those. */
for (const [text, want] of [
  ['export interface Shell {\n  a(): void\n  b?(x: string): void\n}', ['a', 'b']],
  ['export interface Shell {\n  look?: { get(): L; set(p: P): void }\n  z(): void\n}', ['look', 'z']],
  // A comment holding a brace and a line that reads like a member.
  ['export interface Shell {\n  /* not a member: { x(): void */\n  a(): void\n}', ['a']],
  ['export interface Shell {\n  // b(): void\n  a(): void\n}', ['a']],
  // An inline object type in a signature must not swallow what follows it.
  ['export interface Shell {\n  a?(o?: { k?: string; n?: number }): void\n  b(): void\n}', ['a', 'b']],
  /* A wrapped PARAMETER LIST, which is the paren half of the same problem: each
     parameter would otherwise sit at depth 0 and be counted as a verb. */
  ['export interface Shell {\n  a?(\n    box: HTMLElement,\n    ctx: unknown,\n  ): void\n  z(): void\n}',
    ['a', 'z']],
  /* A nested member spread over lines, which is what the depth tracking is for.
     Every member in the interface today opens and closes its braces on one line,
     so nothing else here exercises it -- and a mutation run confirmed that:
     removing the depth test broke no other fixture and moved no number. Reformat
     `look` across lines and, without it, `get` and `set` would each be counted
     as a verb of their own. */
  ['export interface Shell {\n  look?: {\n    get(): L\n    set(p: P): void\n  }\n  z(): void\n}',
    ['look', 'z']],
]) {
  const got = shellVerbs(text)
  if (String(got) !== String(want.slice().sort())) fail(text, want.slice().sort(), got)
}
/* No interface, no number: reported as a failure rather than as zero. */
if (shellVerbs('export interface Other {\n  a(): void\n}') !== null) {
  fail('an absent Shell interface', ['null'], 'not null')
}

const demoTop = top(demo)
const names = strands(liveParts, top(live))
const implicit = names.filter((n) => !demoTop.has(n))
const held = containers(liveParts, top(live), demoTop)
const verbs = shellVerbs(bridgeTs)
if (verbs === null) {
  console.error('count-shared-globals: `export interface Shell {` not found in shell/bridge.ts.'
    + ' The verb count cannot be taken, so it is not being taken -- fix the reader rather than'
    + ' letting a gate report zero.')
  process.exit(1)
}

if (process.argv.includes('--list')) {
  for (const n of names) console.log(implicit.includes(n) ? `${n}  (declared nowhere)` : n)
  for (const n of held) console.log(`${n}  (container, mutated in place)`)
  for (const n of verbs) console.log(`${n}  (shell verb)`)
}
console.log(`live writes to bindings it does not own: ${names.length} (expected ${EXPECTED})`)
if (implicit.length) {
  console.log(`  of those, declared nowhere -- an implicit window write: ${implicit.join(', ')}`)
}
console.log(`demo containers the live layer fills in place: ${held.length} (expected ${EXPECTED_HELD})`)
console.log(`shell verbs the islands ask the legacy layer for: ${verbs.length} (expected ${EXPECTED_VERBS})`)

/* Both numbers, one rule, and the message says which way it moved. Equality in
   both directions for the reason the header gives: an unrecorded win is room for
   the next regression to hide in. */
const gate = (what, got, want, hint) => {
  if (got === want) return false
  if (got > want) {
    console.error(`count-shared-globals: ${what} GREW to ${got}. ${hint}`
      + ' Run with --list to see which.')
  } else {
    console.error(`count-shared-globals: ${what} is down to ${got}. Set its EXPECTED to that`
      + ' in this same change -- an unrecorded win is room for the next regression to hide in.')
  }
  return true
}

const bad = [
  gate('the coupling', names.length, EXPECTED,
    'New code must go through the DataSource seam, not a shared global.'),
  gate('the containers held in common', held.length, EXPECTED_HELD,
    'A live list belongs on a DS source, not in one of the demo\'s fixture arrays.'),
  gate('the shell verb count', verbs.length, EXPECTED_VERBS,
    'An island should read what it needs from its DS source, not ask the legacy shell.'),
].some(Boolean)
if (bad) process.exit(1)
