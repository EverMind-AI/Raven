// A class name is shared state, so every domain's classes carry that domain's
// prefix -- and what is unprefixed is borrowed from the page's own vocabulary
// on purpose.
//
//   node ui-web/scripts/check-class-namespace.mjs
//
// The knowledge dialog's buttons first shipped under `acts`, a class the
// transcript already owns: `.acts button` makes every button a 27px borderless
// icon square, it outranks `.mini`, and Cancel and Create rendered as two grey
// words touching each other. Nothing caught it -- happy-dom applies no
// stylesheet, so no rendering test can see a collision, and the CSS itself was
// valid.
//
// This used to state that rule page-wide and then check one island against one
// hard-coded prefix, over a 6,000-line global stylesheet. The prefixes are read
// from the domains themselves now (features/<domain>/manifest.ts: the directory
// name, or the `cssPrefix` six of them declare because they named their classes
// consistently before there was a rule), and the three debts the tree carries
// are pinned below and may only shrink:
//
//   LEGACY_SHARED -- a class two or more domains name that is not page
//                    vocabulary. A NEW one fails; an existing one reaching one
//                    more domain fails.
//   LEGACY_LOCAL  -- how many of a domain's own classes are still unprefixed.
//                    One more fails.
//   LEGACY_EXPR   -- how many unprefixed classes a domain names from inside a
//                    `className={...}` expression. One more fails.
//
// So the tree is green as it stands and no debt can grow, which is what makes
// features/<domain>/styles.css usable: a domain's new rules go in its own sheet
// under its own prefix (features/extAgents/styles.css says how), and the rules
// already in styles/page.css come across a domain at a time, each move lowering
// a number here.
//
// Reach: every class a domain's non-test .tsx and .ts files name. A class is
// written in four ways and all four are read --
//
//   className="a b"   the attribute, in either quote
//   class="a b"       the same attribute inside a string of markup, which is
//                     what `dangerouslySetInnerHTML` is handed
//   classList.add()   and its three siblings, the class set on an element by
//                     hand rather than declared in the markup
//   className={...}   the expression: every single-quoted, double-quoted and
//                     template-literal static chunk, split on whitespace, and
//                     the same again inside each `${...}` hole
//
// -- because a rule about names is worth only as much as the narrowest way of
// writing one. A token glued to an interpolation is a stem rather than a class
// (`kbf-${family}` names `.kbf-md`, never `.kbf-`), so it is not read as one.
//
// What is NOT read, and cannot be by reading text: a class that reaches the
// attribute as an identifier (`className={cls}`, `{...{ className }}`), and a
// class handed to a component under some other prop name (`cls="x"`). Neither
// end of those is a literal this check can hold to a prefix, and a count of the
// sites would not say which class went through them, so they are a limit rather
// than a list. `src/lib/prose.ts` is the largest of them: it builds markup for
// the whole shell and belongs to no namespace (CONTRIBUTING section 11).
//
// What an expression cannot tell apart is a class from a comparison operand:
// `state === 'bad' ? 'v err' : 'v'` names .v and .err and reads 'bad' as a class
// too. So the expression literals are counted in LEGACY_EXPR rather than mixed
// into the two lists above -- an operand there costs a row on one domain's count
// instead of a wrong verdict about a shared class -- and the prefix rule's
// inverse (a prefixed class the stylesheets define) holds over them as it does
// over the attribute literals.
//
// Two namespaces beyond features/, read the same way and pinned the same way:
//
//   chrome      -- src/chrome/** plus src/App.tsx and src/main.tsx, the page's
//                  own frame. A class it introduces is named `chrome-<rest>`,
//                  or exactly `chrome`.
//   components  -- src/components/**, the components more than one region
//                  renders. There the namespace is the file, because that is
//                  what owns the markup: Skeleton.tsx names `skeleton-<rest>`,
//                  SetupSheet.tsx names `setup-sheet-<rest>`, and a component's
//                  own root class may be the bare name (`agent-mark`). One file
//                  at a time, so each is held to its own name: ModelTags.tsx
//                  owning `.model-tags` does not let SetupSheet.tsx write it,
//                  and the row that fails names the borrower and the owner. A
//                  class belongs to the LONGEST of those names it carries, so
//                  an `Agent.tsx` cannot answer for `.agent-mark-row`.
//
// Every .tsx under src/ belongs to one of the three, which is checked rather
// than assumed: a component filed outside them all is markup nothing here
// reads, and so is a class nothing holds to a prefix.
//
// A name in SHARED passes in both, as it does in a domain, and so does a name
// already on LEGACY_SHARED: that row is the debt of a class two domains name,
// and counting the frame's use of it again would say the debt grew when nothing
// moved. LEGACY_SHARED itself stays scoped to features for the same reason --
// the frame renders the page every domain sits in, so most of the page-wide
// names it writes would read as one more domain and every row would move
// without a class changing. What these two still owe is pinned per namespace:
//
//   LEGACY_CHROME      -- how many names a namespace writes without carrying
//                         its prefix, a row per (class, prefix) pair: `chrome`
//                         has one prefix, so that is per class, and
//                         `components` has one per file, so a class two
//                         components write is a row on each of them.
//   LEGACY_CHROME_EXPR -- the same count inside a `className={...}` expression.
//   LEGACY_BORROWED    -- which LEGACY_SHARED rows the namespace helps itself
//                         to, by name, since those pass the two counts above.
//   UNSTYLED           -- a class the markup writes that page.css, the only
//                         sheet either namespace has, does not define.
//
// Down or gone, all four. A name the frame and one domain both write is a row
// on both sides, and either can retire its own: this counts what a namespace
// owes, not how many names the page has.
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { basename, dirname, join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const src = join(root, 'src')
const fail = (msg) => {
  console.error(`check-class-namespace: ${msg}`)
  process.exit(1)
}

/* A path from src/, spelled the way this file's messages spell one. */
const relOf = (path) => relative(src, path).split(sep).join('/')

// Classes every domain may use, because they are the page's own vocabulary
// rather than any one feature's: buttons, empty states, and the small text
// helpers. Adding to this list is a decision, which is the point of the list.
const SHARED = new Set([
  'mini', 'ghost', 'gold', 'bad', 'danger', 'empty-note', 'ttl', 'ds', 'who', 'mdl',
  'nm', 'st', 'hint', 'mono', 'car', 'x', 'th', 'td', 'err', 'fl', 'mi', 'dots',
  'panel', 'inline', 'scard', 'fset', 'nlmsg', 'foldrow', 'pickm',
  // The shell's markdown styling, shared on purpose: a document should read
  // the same whether it is opened in the transcript, the workspace or a
  // knowledge base, and that is one stylesheet rule rather than three.
  'prose',
  // Every vendor logo on the page wears this, from one component and one rule
  // in components/ProviderMark.tsx -- including its dark-mode handling, which an
  // island restyling the class for itself would get wrong.
  'provider-icon',
  // The state dot, which `bad` above is already two thirds of. A colour that
  // means one thing in one list and another two rows above it in the same panel
  // is worse than a colour that differs from a mock, so the three are one
  // vocabulary and one rule (features/tasks/store.ts's `dot`).
  'run', 'ok',
  // The desk panel's list chrome. Every panel drawn in that 340px slot is the
  // same list with a different row in it -- the tasks view replaces the agents
  // view inside it -- so these belong to the panel rather than to whichever
  // domain is filling it today. `bd` is the row's body and is only ever styled
  // as `.sarow .bd`, beside `.nm` and `.st` which are shared already; `wsempty`
  // is that panel's one empty state, which DeskEmpty draws for every tab.
  'salist', 'sarow', 'wsgrp', 'bd', 'wsempty',
  // The composer's own round send button, one rule in page.css: `SendGlyph`
  // (components/Ico.tsx) says outright that a sub-agent's own composer wears
  // the same glyph so the two cannot drift apart.
  'go',
  // The dropdown's own chrome, one rule in styles/page.css: the caret replaces
  // the native arrow so a select reads as the same control as the boxes beside
  // it. Settings and schedules each styling it for themselves is exactly how it
  // came to be 12.5px here and 13px there.
  'selw', 'sel',
  // The small text vocabulary of a settings page, one rule each in
  // styles/page.css: the mono caption over a block of fields, the grey footnote
  // under one, the amber-ruled how-to, the external link and the fold over a
  // section's optional half. Every section that fills the settings panel reads
  // with these, and the copy settings kept of the link had already lost its
  // icon, its gap and its focus ring.
  'cap2', 'dnote', 'howto', 'exlink', 'foldcap',
])

// Every class two or more domains name, with the number of domains that name
// it, as the tree stands. Down or gone: a higher count is a class spreading
// further, and a count this gate can no longer measure is a pin to delete.
//
// These are not shared vocabulary -- nobody decided them. `.n`, `.hd`, `.cap`,
// `.row`, `.key` are the names a writer reaches for first, and the `.pm*`
// family is one domain's rows that a second domain drew the same way. Either
// end of a row here can retire it: move the rule into one domain's styles.css
// and prefix it there, or put the name in SHARED on purpose.
//
// Twenty-five rows left this table without a class being renamed anywhere.
// Skills, plugins and knowledge were readers of most of the `.pm*`, `.hub*`
// and `.p*` names, so a name they shared with one other domain is that one
// domain's alone now and is counted on LEGACY_LOCAL instead; a name only they
// shared is gone with them. The page's total debt is unchanged; where it is
// counted is not.
const LEGACY_SHARED = {
  a: 3, btn: 2, cap: 2, chgs: 2, cmd: 2, ct: 2, d: 4, foot: 2, k: 3,
  gap: 2, 'ghost-ic': 3, h: 2, hd: 2, key: 2, lb: 3, n: 3, rm: 2, row: 2,
  shot: 3, sk: 2, skel: 2, step: 2, sz: 2, tipdn: 2, v: 3, w: 3, wkg: 3,
  wsnote: 2,
}

// How many of a domain's own classes -- the ones no other domain names -- still
// carry no prefix, as the tree stands. Down or gone, one domain at a time: the
// rules are in styles/page.css, and moving a domain's rules into its own
// styles.css is where the renames belong (page.css is where the boot goldens
// and the region goldens take their bytes from, so a rename without the move
// is a golden churn for nothing).
//
// A zero is a domain that is done, and it stays on the list: the entry is what
// says the count was measured rather than forgotten.
const LEGACY_LOCAL = {
  /* Up from 24 for `.hot`, which BrowserPage.tsx sets with classList rather
     than in the markup: a class this check had never read, not a new one. The
     two rows below moved the same way -- `.drop` and `.halt` in composer,
     `.caret` in transcript, the last of them written into a string of HTML.

     Several rows below rise again without a class being added anywhere.
     Skills, plugins and knowledge were three of the readers of the page's
     shared vocabulary -- `.lab`, `.rule`, `.meta`, `.seg` and the rest -- so a
     class those domains shared with one other is that one domain's alone now,
     and moves off the shared tally onto its own. The page's total debt is
     unchanged; where it is counted is not. */
  browser: 25,
  /* Up from 9, and none of the three is new code: an earlier branch deleted
     the old settings page, which also named `.icb`, `.other`, `.srow` and
     `.what`, so what the check read as two domains' it now reads as
     composer's own. Up one more with the playbooks page, which was the other
     reader of `.ic`. Down two with the permission sheet's redesign: its own
     parts carry the prefix, and `.note-in` and `.pattern-in` left with the old
     sheet. */
  /* Up one, and nothing was added: `.body` was shared with the schedules
     island's new-job sheet, and that sheet is gone -- a job being created
     stands in the section's own right column now. The name is this domain's
     alone, so it is counted here rather than on the shared tally. */
  composer: 11,
  connections: 4,
  /* Down from 17 with the schedules section: the page's hero, its filter
     chips and its own list and row classes went with the two-pane frame
     (src/components/TwoPane.tsx owns those names now), and `.swi` went with
     the switch that moved into it.

     Down to 2 with the section redrawn against the prototype: the job's form
     is the panel's own labelled blocks, its dropdowns are the page's, and its
     name box, instruction, run list and refusal note are prefixed rules in
     features/cron/styles.css. The new-job modal went with them, and `.body`,
     `.chev`, `.btn` and `.key` left the shared tally with it. */
  cron: 2,
  /* Up from 7, and every one of the six is a class that MOVED here rather than
     a new one: the pan-and-zoom viewport the playbook page carried became
     features/dag/Board.tsx so the task board could read it too, and its
     `.g*` classes came with it.

     Down one from 13 with the graph sheet: `.dsheet` was this domain's, and
     the sheet is the only thing that wrote it. Up two with the playbooks page
     gone: `.ag` and `.mk` were shared with it and are this domain's alone.

     Down to 7 with the default node box gone: `.ag`, `.mk`, `.wait`, `.lbl`,
     `.ln`, `.dagcap` and `.workv` were that box's own classes, and every
     caller now hands DagGraph a `renderNode` of its own. */
  dag: 7,
  desk: 5,
  /* Was none with the agent hub: every class the page writes carries its
     prefix, and its rules live in features/extAgents/styles.css. Up to three
     with the playbooks page gone -- `.kd`, `.pmhero` and `.sulist` were shared
     with it, so they are this domain's alone now and are counted here rather
     than on the shared tally. Nothing was added. Down to one with the
     onboarding wizard's agents step drawing the hub's rows: `.kd` and
     `.sulist` were the step's alone, and nothing names them now; `.pmhero`
     on the page's hero is what remains. */
  extAgents: 1,
  importSync: 0,
  installed: 0,
  /* Down from 14 with the memory section: the page's hero, its own list and
     row classes and the shared drawer's head went with the two-pane frame
     (src/components/TwoPane.tsx owns those names now). */
  memory: 8,
  /* `.empty` and the row's `.ct`, `.nm`, `.tick`: the picker's own list
     vocabulary, shared with the popovers beside it. */
  model: 4,
  onboard: 0,
  /* Down one: an empty group is its heading alone, so `.grp-empty` is gone. */
  rail: 9,
  settings: 0,
  /* Up one the same way: `.chev` was shared with the schedules island's run
     list, whose rows carry a prefixed stamp and note now instead of the
     page's own names. */
  subagents: 39,
  /* Down from 8: the node panel's inline "still running" line -- a plain
     `className="dot run"`, the one attribute-form use of `.dot` this domain
     had -- is gone with the line it lived on (the head already says a
     running node's state). Every remaining `.dot` here is written from
     inside a `className={...}` expression, which `LEGACY_EXPR` already
     counted. Down two more with the composer strip redrawn as one prefixed
     pill: `.runs` and `.trun` went with the per-task chips. Up one, and
     nothing was added: `.task` on the list row was built by expression only
     while the row lit itself from the strip's hover, and is a plain
     attribute now that the pairing is gone -- it moves here from
     `LEGACY_EXPR`, which drops it and `.hl`. */
  tasks: 6,
  /* Up from 74 for `.act`, `.dact` and `.pmdesc`, which the plugins page drew
     the same way the transcript does, and from 77 with the agent hub, where
     `.none` was shared with the agents page. Up three more with the playbooks
     page gone: `.ph`, `.sheet` and `.val` were shared with it.

     Down to 70 with the node panel: `.npanel`, `.nhd`, `.orun`, `.rows`,
     `.dep`, `.hold`, `.ins`, `.src`, `.val` and `.ph` left with it, and
     `.nid`, whose one rule was scoped to that panel, went from the card's
     grid.

     Up to 71 with the dag renderer's default node box gone: `.tm` was
     shared with it, and the timestamp on a folded turn's own head is this
     domain's alone now. */
  transcript: 68,
  /* Up from 37 with the playbooks page gone: `.t` was shared with it. */
  workspace: 38,
}

// The same count for the classes a domain names from inside a `className={...}`
// expression, and on the same terms: down or gone, a zero stays on the list.
// Separate from LEGACY_LOCAL because the two are not measured the same way -- an
// expression literal may be a comparison operand rather than a class (the header
// says why), so a row here is an upper bound on a debt where a row there is the
// debt. A name in both places is counted once, by LEGACY_LOCAL.
const LEGACY_EXPR = {
  browser: 2,
  composer: 3,
  connections: 2,
  cron: 0,
  /* Down from 3 with the default node box gone: `.id`, written only inside
     the ternary that chose its class by whether a node carried a summary,
     left with it. */
  dag: 2,
  desk: 2,
  extAgents: 0,
  importSync: 0,
  installed: 0,
  /* Down from 1 with the initial tile gone: `.pmtile`, written only from
     inside a `className={...}` expression, left with it. */
  memory: 0,
  model: 0,
  onboard: 0,
  rail: 2,
  settings: 0,
  /* Down from 5: `.task` moved to `LEGACY_LOCAL` and `.hl` went, with the
     hover pairing the composer strip no longer has a side for. */
  tasks: 3,
  subagents: 6,
  /* Down from 24 with the node panel: `.tpl`, written only from inside a
     `className={...}` expression, left with it. */
  transcript: 21,
  workspace: 7,
}

// The same two counts for the page frame and the shared components, whose
// classes are almost all still in styles/page.css: `chrome` is one namespace
// and `components` is one per file (the header says which). Down or gone.
//
// The frame's rules are the ones that cannot move a domain at a time -- there
// is no chrome/styles.css to move them into, and page.css is where the boot
// goldens take their bytes from -- so these two come down by renaming, in a
// commit that changes the DOM the goldens record.
const LEGACY_CHROME = {
  /* Up from 89: `.icb`, `.led`, `.note` and `.tick` were borrowed from names
     the old settings page shared with the frame, and that page is gone.
     Nothing new is written -- the four move off the borrowed list and on to
     this one. */
  /* Up one, and nothing was added: `.body` on the settings dialog was shared
     with the schedules island's new-job sheet, which is gone -- so the frame's
     own use of the name is counted here rather than borrowed from the shared
     tally. */
  chrome: 92,
  components: 36,
}

const LEGACY_CHROME_EXPR = {
  /* Up from 1 for `.hot`, `.risk`, `.rule` and `.warm`: four names written
     inside a `${...}` hole, which the expression reader used to skip over.
     Down one when `.rule` left with the approval sheet's prefix editor. */
  chrome: 3,
  /* Up from 8 by the same deletion: `.led`, written from inside an expression
     in SetupRow.tsx and SetupSheet.tsx, was shared with the old settings page
     and is these two files' own now. */
  /* Up from 10 with the agent hub: `warn` in SetupSheet.tsx passed as borrowed
     while the playbooks and agents pages both wrote the class; the agents page
     writes its own names now, so the name is playbooks' alone and the
     component's use of it counts here instead. */
  /* Down one with the letter tile gone: `.pmtile`, written from inside an
     expression in SetupRow.tsx, left with it. */
  components: 10,
}

// A class the markup writes that styles/page.css does not define -- page.css
// alone, because it is the only sheet either namespace has, and reading a
// domain's own sheet here would let the frame borrow a class that domain styles
// for itself and call it defined. Comments come out of the sheet first: a note
// saying a rule went away is the likeliest place for the name to outlive it.
//
// So: a rule renamed or deleted from under the markup, which no rendering test
// can see. Down or gone, and going away edits the served markup -- `.newrun` is
// in src/test/__golden__/region-app.txt -- so it is not a delete in a commit
// that changes no DOM. An empty list is a namespace that was measured and owes
// nothing.
//
// src/components/SetupSheet.tsx writes two more of these from inside an
// expression (`badtx`, `warntx`). The check below does not read an unprefixed
// expression literal, because that is where a comparison operand looks exactly
// like a class; both are real, and both go when that span's two states get a
// rule or a name.
const UNSTYLED = {
  /* `tipdn` is not a rule and never was: it is the flag state/tooltip.ts reads
     with `classList.contains` to place the hover pill below its control instead
     of above it. page.css names it in the comment over those rules, which is
     what used to answer for it here until the comments came out of the search --
     a marker class rather than a renamed rule, and one the tooltip placement
     tests cover. */
  chrome: ['newrun', 'tipdn'],
  components: [],
}

// Which LEGACY_SHARED rows each namespace borrows, by name. A name on one of
// those rows passes the two counts above on purpose -- counting the frame's use
// of a class two domains already name would say the debt grew when nothing
// moved -- so without this list the whole pool was free for the frame and the
// shared components to help themselves to.
//
// Down or gone, like the rest: a NEW borrow fails and a retired one is a pin to
// delete. Either end retires a row -- the domains prefix the class, or the
// namespace stops writing it.
//
// Names rather than a count, because a count would let one borrow be traded for
// another. Attribute and expression borrowings are one list: an expression is
// how a class is written as often as an attribute is (`'led' + ...` in
// SetupRow.tsx and SetupSheet.tsx), and keeping them apart would leave the
// expression form as the way around the list. `warn` in SetupSheet.tsx is the
// price of that: it is a state name being compared, which an expression cannot
// tell from a class, and it is counted on LEGACY_CHROME_EXPR above.
const LEGACY_BORROWED = {
  chrome: [
    'btn', 'cmd', 'foot', 'ghost-ic', 'hd', 'lb', 'n', 'tipdn',
  ],
  components: ['a', 'cap', 'hd', 'key', 'n', 'skel'],
}

const domains = readdirSync(join(src, 'features'))
  .filter((name) => statSync(join(src, 'features', name)).isDirectory())
  .sort()

/* The prefix from the domain's own declaration, read from the source text: a
   gate about names has no business importing twenty islands to learn them. */
const prefixOf = (domain) => {
  const text = readFileSync(join(src, 'features', domain, 'manifest.ts'), 'utf8')
  return text.match(/\bcssPrefix:\s*'([^']+)'/)?.[1] ?? domain
}

/* Directories the walk does not enter, by their path from src/ rather than by
   a bare name: `src/assets/` is not source, while a domain's own `assets/`
   would be, and a name-only test cannot tell the two apart. */
const SKIP = new Set(['assets', 'test/__golden__'])
const skipped = (path) => {
  const rel = relOf(path)
  return SKIP.has(rel) || /(^|\/)__snapshots__$/.test(rel)
}

/* A test file is one whose own name says so -- `Rail.test.tsx`, `test.tsx` --
   rather than any path with `.test.` somewhere along it. */
const isTest = (name) => /(^|\.)test\.[jt]sx?$/.test(name)

/* Every file of a namespace that can write a class: the JSX, and the modules
   beside it that reach for an element and set a class on it by hand. */
const sources = (dir, out = []) => {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (!skipped(path)) sources(path, out)
    } else if (/\.tsx?$/.test(entry.name) && !isTest(entry.name) && !entry.name.endsWith('.d.ts')) {
      out.push(path)
    }
  }
  return out
}

/* The walk above reads TypeScript, which is the whole tree only while there is
   no plain JavaScript in it. A `.js` under src/ would be markup this check
   never opened, and silence is the one answer it must not give. */
const javascript = (dir, out = []) => {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (!skipped(path)) javascript(path, out)
    } else if (/\.(js|jsx|mjs|cjs)$/.test(entry.name)) {
      out.push(relOf(path))
    }
  }
  return out
}
const plainJs = javascript(src)
if (plainJs.length) {
  fail(
    `${plainJs.join(', ')} under src/ is JavaScript, which this check's walk does not read: ` +
      'add the extension to `sources` in scripts/check-class-namespace.mjs, or move the file under src/assets/.',
  )
}

/* The expression after each `className={`, to its balanced closing brace --
   quotes and nested `${}` skipped, so a brace inside a string does not end it. */
const expressions = (text) => {
  const out = []
  let at = text.indexOf('className={')
  while (at !== -1) {
    let i = at + 'className={'.length
    const start = i
    let depth = 1
    while (i < text.length && depth > 0) {
      const ch = text[i]
      if (ch === '{') depth += 1
      else if (ch === '}') depth -= 1
      else if (ch === "'" || ch === '"' || ch === '`') i = endOfString(text, i)
      i += 1
    }
    out.push(text.slice(start, i - 1))
    at = text.indexOf('className={', i)
  }
  return out
}

/* The index of the quote that closes the one at `i`. */
const endOfString = (text, i) => {
  const quote = text[i]
  let j = i + 1
  while (j < text.length && text[j] !== quote) {
    if (text[j] === '\\') { j += 2; continue }
    if (quote === '`' && text[j] === '$' && text[j + 1] === '{') { j = endOfGroup(text, j + 1) + 1; continue }
    j += 1
  }
  return j
}

/* The index of the bracket that closes the one at `i` -- a `${`'s brace, or a
   call's parenthesis -- with quotes skipped so a bracket inside a string does
   not end it. */
const endOfGroup = (text, i) => {
  const open = text[i]
  const close = open === '{' ? '}' : ')'
  let j = i + 1
  let depth = 1
  while (j < text.length && depth > 0) {
    const ch = text[j]
    if (ch === open) depth += 1
    else if (ch === close) depth -= 1
    else if (ch === "'" || ch === '"' || ch === '`') j = endOfString(text, j)
    j += 1
  }
  return j - 1
}

/* Every class token of the static strings in one expression. A chunk that an
   interpolation runs into is a stem, not a class, so the token on that side of
   it is dropped; anything unquoted is an identifier, which is a variable. */
const tokensIn = (expr) => {
  const out = []
  const take = (text, glueStart, glueEnd) => {
    const parts = text.split(/\s+/)
    if (glueStart) parts[0] = ''
    if (glueEnd) parts[parts.length - 1] = ''
    for (const c of parts) if (c) out.push(c)
  }
  let i = 0
  while (i < expr.length) {
    const ch = expr[i]
    if (ch === "'" || ch === '"') {
      const end = endOfString(expr, i)
      take(expr.slice(i + 1, end), false, false)
      i = end + 1
      continue
    }
    if (ch === '`') {
      const end = endOfString(expr, i)
      let at = i + 1
      let glueStart = false
      while (at < end) {
        const hole = expr.indexOf('${', at)
        if (hole === -1 || hole >= end) break
        take(expr.slice(at, hole), glueStart, true)
        /* The hole holds an expression of its own, so it is read as one:
           `` `x ${on ? 'a' : 'b'}` `` names .a and .b, and skipping the hole
           made a whole class of names invisible. The chunks on either side keep
           the stem rule -- they are glued to whatever the hole answers. */
        const close = endOfGroup(expr, hole + 1)
        for (const c of tokensIn(expr.slice(hole + 2, close))) out.push(c)
        glueStart = true
        at = close + 1
      }
      take(expr.slice(at, end), glueStart, false)
      i = end + 1
      continue
    }
    i += 1
  }
  return out
}

/* `className="a b"` and `className='a b'`: a class name holds no quote, so one
   pattern reads both, and a `{` in the value is the expression form below. */
const ATTRIBUTE = /className=(["'])([^"'{]+)\1/g

/* The same attribute inside a string of markup -- what `dangerouslySetInnerHTML`
   is handed, and what a helper building HTML writes. The quote may be escaped,
   because the string around it may be quoted the same way. */
const MARKUP = /\bclass=\\?(["'])([^"'{\\]+)\\?\1/g

/* A class set on an element by hand, which no attribute anywhere says. */
const CLASS_LIST = /\bclassList\.(?:add|remove|toggle|replace)\(/g

/* Every class one file writes, in two piles. The attribute forms and the
   hand-set ones are classes and nothing else; a `className={...}` expression is
   where a literal may be a comparison operand instead, which is the whole
   reason the two are counted apart.
   `classList` sits in the first pile because its four verbs take class names.
   Their arguments are read the way an expression is, so a comparison inside
   one (`toggle('x', mode === 'dark')`) would be read as a class -- no call
   does that today, and the alternative is not reading the call at all. */
const written = (text) => {
  const attr = []
  const expr = []
  for (const pattern of [ATTRIBUTE, MARKUP]) {
    for (const m of text.matchAll(pattern)) {
      for (const c of m[2].trim().split(/\s+/)) if (c) attr.push(c)
    }
  }
  for (const m of text.matchAll(CLASS_LIST)) {
    const open = m.index + m[0].length - 1
    for (const c of tokensIn(text.slice(open + 1, endOfGroup(text, open)))) attr.push(c)
  }
  for (const e of expressions(text)) {
    for (const c of tokensIn(e)) expr.push(c)
  }
  return { attr, expr }
}

const prefixes = new Map(domains.map((domain) => [domain, prefixOf(domain)]))
const named = new Map()
const inExpr = new Map()
const read = []
for (const domain of domains) {
  const files = sources(join(src, 'features', domain))
  const used = new Set()
  const fromExpr = new Set()
  for (const file of files) {
    const { attr, expr } = written(readFileSync(file, 'utf8'))
    for (const c of attr) used.add(c)
    for (const c of expr) fromExpr.add(c)
  }
  read.push(...files)
  /* The .tsx alone, because a domain may be all modules and no markup:
     features/installed is one shared read and two pages' worth of rows. */
  const rendering = files.filter((file) => file.endsWith('.tsx')).length
  if (rendering && !used.size) {
    fail(`features/${domain} renders ${rendering} component(s) and names no class, which means this check is reading the wrong files`)
  }
  named.set(domain, used)
  inExpr.set(domain, fromExpr)
}

const owners = new Map()
for (const [domain, used] of named) {
  for (const c of used) {
    if (!owners.has(c)) owners.set(c, [])
    owners.get(c).push(domain)
  }
}
if (!owners.size) fail('no domain names a class, which means this check is reading the wrong tree')

const spreading = []
const unpinnedShared = []
const localOver = []
const local = new Map(domains.map((domain) => [domain, []]))
for (const [c, ds] of owners) {
  if (SHARED.has(c)) continue
  if (ds.length >= 2) {
    const pinned = LEGACY_SHARED[c]
    if (pinned === undefined) unpinnedShared.push(`.${c} (${ds.join(', ')})`)
    else if (ds.length > pinned) spreading.push(`.${c}: ${ds.length} domains, pinned ${pinned} (${ds.join(', ')})`)
    continue
  }
  const domain = ds[0]
  if (!c.startsWith(prefixes.get(domain))) local.get(domain).push(c)
}
for (const domain of domains) {
  const found = local.get(domain).length
  const pinned = LEGACY_LOCAL[domain]
  if (pinned === undefined) localOver.push(`${domain}: ${found} unprefixed, not on the list`)
  else if (found > pinned) {
    localOver.push(`${domain}: ${found} unprefixed, pinned ${pinned} -- ${local.get(domain).sort().map((c) => `.${c}`).join(', ')}`)
  }
}

/* The same, for the literals inside `className={...}`. A name the domain also
   writes as an attribute is already on one of the two lists above. */
const exprOver = []
const fromExpr = new Map(domains.map((domain) => [domain, []]))
for (const [domain, used] of inExpr) {
  for (const c of used) {
    if (SHARED.has(c) || named.get(domain).has(c)) continue
    if (!c.startsWith(prefixes.get(domain))) fromExpr.get(domain).push(c)
  }
}
for (const domain of domains) {
  const found = fromExpr.get(domain).length
  const pinned = LEGACY_EXPR[domain]
  if (pinned === undefined) exprOver.push(`${domain}: ${found} unprefixed in expressions, not on the list`)
  else if (found > pinned) {
    exprOver.push(`${domain}: ${found} unprefixed in expressions, pinned ${pinned} -- ${fromExpr.get(domain).sort().map((c) => `.${c}`).join(', ')}`)
  }
}

if (unpinnedShared.length) {
  fail(
    `${unpinnedShared.join(', ')} ${unpinnedShared.length === 1 ? 'is named' : 'are named'} by more than one ` +
      'domain and is not the page\'s vocabulary. Give the class the owning domain\'s prefix and put its ' +
      'rule in that domain\'s styles.css, or add it to SHARED because every domain should look the same here.',
  )
}
if (spreading.length) {
  fail(
    `${spreading.join('; ')}. A class already shared reached one more domain: prefix it for the new ` +
      'domain rather than borrowing another\'s name.',
  )
}
if (localOver.length) {
  fail(
    `${localOver.join('; ')}. A class a domain introduces carries that domain's prefix ` +
      '(features/<domain>/manifest.ts declares it, or the directory name is it) and its rule goes in ' +
      'features/<domain>/styles.css.',
  )
}
if (exprOver.length) {
  fail(
    `${exprOver.join('; ')}. Same rule inside a className expression as outside one: the domain's ` +
      'prefix, and the rule in features/<domain>/styles.css.',
  )
}

/* Down or gone, the other half: a pin the tree has outgrown has to come off,
   or the numbers stop meaning anything a year from now. */
const stale = []
for (const [c, pinned] of Object.entries(LEGACY_SHARED)) {
  const ds = owners.get(c) ?? []
  if (ds.length < 2) stale.push(`.${c}: ${ds.length} domain(s) now, delete the pin`)
  else if (ds.length < pinned) stale.push(`.${c}: ${ds.length} domains now, pinned ${pinned}, lower it`)
}
for (const domain of domains) {
  const pinned = LEGACY_LOCAL[domain]
  if (pinned !== undefined && local.get(domain).length < pinned) {
    stale.push(`${domain}: ${local.get(domain).length} unprefixed now, pinned ${pinned}, lower it`)
  }
  const pinnedExpr = LEGACY_EXPR[domain]
  if (pinnedExpr !== undefined && fromExpr.get(domain).length < pinnedExpr) {
    stale.push(`${domain}: ${fromExpr.get(domain).length} unprefixed in expressions now, pinned ${pinnedExpr}, lower it`)
  }
}
for (const domain of new Set([...Object.keys(LEGACY_LOCAL), ...Object.keys(LEGACY_EXPR)])) {
  if (!prefixes.has(domain)) stale.push(`${domain}: no such domain, delete the pin`)
}
if (stale.length) fail(`${stale.join('; ')}. All three lists above are down-or-gone.`)

/* A sheet without its comments, and without the two places a dot is text
   rather than a selector: a note saying a rule went away is the likeliest place
   for the name to survive it, and `content: '.x'` draws the characters. */
const selectors = (text) => text
  .replace(/\/\*[\s\S]*?\*\//g, ' ')
  .replace(/\burl\([^)]*\)/g, ' ')
  .replace(/\bcontent\s*:[^;}]*/g, ' ')

const page = selectors(readFileSync(join(src, 'styles/page.css'), 'utf8'))
const sheets = [page]
for (const domain of domains) {
  try { sheets.push(selectors(readFileSync(join(src, 'features', domain, 'styles.css'), 'utf8'))) } catch { /* not every domain has one */ }
}
const css = sheets.join('\n')
/* A class name is `[\w-]`, but a token read out of an expression need not be
   one, and an unescaped `.` or `(` in the pattern would match a rule that says
   something else. */
const definedIn = (text) => (c) => new RegExp(`\\.${c.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\w-])`).test(text)
const styled = definedIn(css)

/* And the reverse of the prefix rule: a class a domain owns must be a class the
   stylesheets define. A prefixed name nothing styles is a rule that was renamed
   or deleted from under the markup, which no rendering test can see either. */
const undefined_ = []
for (const domain of domains) {
  for (const c of new Set([...named.get(domain), ...inExpr.get(domain)])) {
    if (!c.startsWith(prefixes.get(domain))) continue
    if (!styled(c)) undefined_.push(`features/${domain} uses .${c}`)
  }
}
if (undefined_.length) fail(`${undefined_.join(', ')}, which no stylesheet defines`)

/* The page frame and the shared components, on the same three rules: the
   namespace's prefix, the same two debts, and the stylesheets defining what the
   markup names. The frame is one namespace over many files; a component is a
   namespace of its own, so `PascalCase.tsx` is read as the prefix its classes
   carry. */
const kebab = (file) => basename(file).replace(/\.tsx?$/, '').replace(/([a-z\d])([A-Z])/g, '$1-$2').toLowerCase()
const carries = (c, prefix) => c === prefix || c.startsWith(`${prefix}-`)
const NAMESPACES = [
  {
    name: 'chrome',
    reach: 'src/chrome/**, src/App.tsx and src/main.tsx',
    files: [...sources(join(src, 'chrome')), join(src, 'App.tsx'), join(src, 'main.tsx')],
    prefix: () => 'chrome',
  },
  {
    name: 'components',
    reach: 'src/components/**',
    files: sources(join(src, 'components')),
    prefix: (file) => kebab(file),
  },
]

/* Neither namespace has a sheet of its own, so page.css is where its rules
   are -- what the failure message says, and what section 7 of CONTRIBUTING
   says. Reading a domain's sheet here would let the frame borrow a class a
   domain styles for itself and call it defined. */
const onPage = definedIn(page)

const nsOver = []
const nsExprOver = []
const nsBorrowed = []
const nsDead = []
const nsStale = []
let nsClasses = 0
let nsLiterals = 0
for (const ns of NAMESPACES) {
  /* Each name with the prefixes of the files that write it, and each prefix
     with its files: two components may name the same class, and it is that
     component's own prefix each of them is held to. */
  const attrNames = new Map()
  const exprNames = new Map()
  const filesOf = new Map()
  /* And the files themselves, per name, so a row can say where to go: `chrome`
     answers for twenty-odd files under one prefix, and the prefix alone told a
     reader nothing about which of them to open. */
  const wrote = new Map()
  const add = (into, c, prefix, file) => {
    if (!into.has(c)) into.set(c, new Set())
    into.get(c).add(prefix)
    const key = `${prefix} ${c}`
    if (!wrote.has(key)) wrote.set(key, new Set())
    wrote.get(key).add(basename(file))
  }
  for (const file of ns.files) {
    const prefix = ns.prefix(file)
    if (!filesOf.has(prefix)) filesOf.set(prefix, [])
    filesOf.get(prefix).push(basename(file))
    const { attr, expr } = written(readFileSync(file, 'utf8'))
    for (const c of attr) add(attrNames, c, prefix, file)
    for (const c of expr) add(exprNames, c, prefix, file)
  }
  if (!attrNames.size) {
    fail(`${ns.reach} name no class, which means this check is reading the wrong files`)
  }
  nsClasses += attrNames.size
  nsLiterals += exprNames.size

  /* The file a prefix speaks for, where that is one file: `components` is a
     namespace per file, so a row can name the owner of a borrowed class.
     `chrome` is one namespace over many files and answers under its own name. */
  const whose = (prefix) => {
    const files = filesOf.get(prefix) ?? []
    return files.length === 1 ? files[0] : ns.name
  }
  /* A class belongs to the LONGEST namespace prefix it carries, not to any
     prefix it happens to start with: `agent-mark-row` is AgentMark.tsx's, and
     an `Agent.tsx` beside it would otherwise own every class of the component
     whose name its own is the stem of. */
  const ours = [...new Set(ns.files.map(ns.prefix))]
  const owner = (c) => ours.filter((p) => carries(c, p)).sort((a, b) => b.length - a.length)[0]
  const at = (c, prefix) => [...(wrote.get(`${prefix} ${c}`) ?? [])].sort().join(' and ')
  const anywhere = (c) => [...new Set(ours.flatMap((p) => [...(wrote.get(`${p} ${c}`) ?? [])]))].sort().join(' and ')
  const row = (c, prefix) => {
    const lenders = ours.filter((p) => p !== prefix && carries(c, p)).map(whose)
    if (lenders.length) return `${at(c, prefix)} borrows .${c} from ${lenders.join(' and ')}`
    return `.${c} in ${at(c, prefix)}`
  }
  /* A row per (class, prefix) pair the class does not belong to, not per class:
     the prefixes naming one class are separate namespaces, so the component
     that owns the name cannot answer for a second one borrowing it. An
     attribute row covers the pair it counted, not every use of the name. */
  const owed = (from, already) => {
    const out = []
    for (const [c, where] of from) {
      if (SHARED.has(c) || LEGACY_SHARED[c] !== undefined) continue
      for (const prefix of where) {
        if (owner(c) === prefix || already?.get(c)?.has(prefix)) continue
        out.push(row(c, prefix))
      }
    }
    return out.sort()
  }
  /* Whether the namespace has a prefix the name carries, which is how an
     expression literal is told from a comparison operand -- a question about
     the token, not about who may write it. */
  const anyPrefix = (c) => owner(c) !== undefined
  const debt = owed(attrNames)
  const exprDebt = owed(exprNames, attrNames)
  const ratchet = (found, pinned, what, into) => {
    if (pinned === undefined) into.push(`${ns.name}: ${found.length} ${what}, not on the list`)
    else if (found.length > pinned) {
      into.push(`${ns.name}: ${found.length} ${what}, pinned ${pinned} -- ${found.join(', ')}`)
    } else if (found.length < pinned) {
      nsStale.push(`${ns.name}: ${found.length} ${what} now, pinned ${pinned}, lower it`)
    }
  }
  ratchet(debt, LEGACY_CHROME[ns.name], 'unprefixed', nsOver)
  ratchet(exprDebt, LEGACY_CHROME_EXPR[ns.name], 'unprefixed in expressions', nsExprOver)

  /* The LEGACY_SHARED rows the namespace writes, by name. Those names pass the
     two ratchets above on purpose -- counting the frame's use of a class two
     domains already name would say the debt grew when nothing moved -- which
     left the whole pool borrowable at will. The borrowings are the pin
     instead: a name here is one the frame or a component already writes, and a
     name not here is a new borrow. */
  const borrows = [...new Set([...attrNames.keys(), ...exprNames.keys()])]
    .filter((c) => !SHARED.has(c) && LEGACY_SHARED[c] !== undefined && owner(c) === undefined)
    .sort()
  const pinnedBorrows = LEGACY_BORROWED[ns.name]
  if (pinnedBorrows === undefined) {
    nsBorrowed.push(`${ns.name} has no LEGACY_BORROWED list of its own`)
  } else {
    for (const c of borrows) {
      if (!pinnedBorrows.includes(c)) nsBorrowed.push(`${ns.name} writes .${c} in ${anywhere(c)}`)
    }
    for (const c of pinnedBorrows) {
      if (!borrows.includes(c)) nsStale.push(`${ns.name}: .${c} is not borrowed any more, delete the pin`)
    }
  }

  /* Every attribute literal, because an attribute is a class and nothing else,
     plus the expression literals that carry the prefix -- the rest of an
     expression is where an operand is indistinguishable from a class. */
  const dead = [...attrNames.keys()].filter((c) => !onPage(c))
  for (const [c] of exprNames) {
    if (!attrNames.has(c) && anyPrefix(c) && !onPage(c)) dead.push(c)
  }
  dead.sort()
  const pinnedDead = UNSTYLED[ns.name]
  if (pinnedDead === undefined) {
    nsDead.push(`${ns.name} has no UNSTYLED list of its own`)
  } else {
    for (const c of dead) {
      if (!pinnedDead.includes(c)) nsDead.push(`${ns.name} writes .${c} in ${anywhere(c)}, which page.css does not define`)
    }
    for (const c of pinnedDead) if (!dead.includes(c)) nsStale.push(`${ns.name}: .${c} is styled or gone now, delete the pin`)
  }
}
const namespaces = new Set(NAMESPACES.map((ns) => ns.name))
const pinnedNames = [
  ...Object.keys(LEGACY_CHROME), ...Object.keys(LEGACY_CHROME_EXPR),
  ...Object.keys(LEGACY_BORROWED), ...Object.keys(UNSTYLED),
]
for (const name of new Set(pinnedNames)) {
  if (!namespaces.has(name)) nsStale.push(`${name}: no such namespace, delete the pin`)
}

/* And every file that can write a class is inside one of the namespaces above.
   A component filed anywhere else -- `src/state/Stray.tsx` -- is markup no list
   here reads, so its classes answer to no prefix and nothing notices. The
   modules that are not components stay out: `lib/prose.ts` builds markup and is
   a known exception (CONTRIBUTING section 11), not a namespace. */
const claimed = new Set([...read, ...NAMESPACES.flatMap((ns) => ns.files)])
const unclaimed = sources(src).filter((file) => file.endsWith('.tsx') && !claimed.has(file)).map(relOf)

if (nsOver.length) {
  fail(
    `${nsOver.join('; ')}. A class the page's frame introduces is named 'chrome-...' and a shared ` +
      "component's is named after its own file ('skeleton-...' for Skeleton.tsx), with the rule in " +
      'src/styles/page.css, which is the sheet both namespaces are styled from.',
  )
}
if (nsExprOver.length) {
  fail(
    `${nsExprOver.join('; ')}. Same rule inside a className expression as outside one: the ` +
      "namespace's prefix -- 'chrome-...' for the frame, the component's own file name for a " +
      'shared component.',
  )
}
if (nsBorrowed.length) {
  fail(
    `${nsBorrowed.join('; ')}, which is a class LEGACY_SHARED already carries for two or more ` +
      'domains. Borrowing one more spreads it further without moving a number: name the class for ' +
      'this namespace instead, or add it to SHARED because every region should look the same here.',
  )
}
if (nsDead.length) {
  fail(
    `${nsDead.join('; ')}. Take the class out of the markup, in a commit that says which golden ` +
      'lines moved, or pin it in UNSTYLED with the reason it is still written.',
  )
}
if (unclaimed.length) {
  fail(
    `${unclaimed.join(', ')} renders markup no class namespace reads, so nothing holds its classes ` +
      'to a prefix. A component belongs to features/<domain>/, src/chrome/ or src/components/; if it ' +
      'belongs where it is, this check needs a namespace for that root.',
  )
}
if (nsStale.length) fail(`${nsStale.join('; ')}. The chrome and components lists are down-or-gone too.`)

const classes = [...named.values()].reduce((n, used) => n + used.size, 0)
const literals = [...inExpr.values()].reduce((n, used) => n + used.size, 0)
console.log(
  `check-class-namespace: OK (${classes} classes and ${literals} expression literals across ` +
    `${domains.length} domains; ${Object.keys(LEGACY_SHARED).length} shared, ` +
    `${Object.values(LEGACY_LOCAL).reduce((a, b) => a + b, 0)} unprefixed and ` +
    `${Object.values(LEGACY_EXPR).reduce((a, b) => a + b, 0)} unprefixed in expressions pinned. ` +
    `${nsClasses} classes and ${nsLiterals} expression literals across the ` +
    `${NAMESPACES.length} namespaces beyond features/, chrome and components; ` +
    `${Object.values(LEGACY_CHROME).reduce((a, b) => a + b, 0)} unprefixed, ` +
    `${Object.values(LEGACY_CHROME_EXPR).reduce((a, b) => a + b, 0)} unprefixed in expressions, ` +
    `${Object.values(LEGACY_BORROWED).reduce((n, list) => n + list.length, 0)} borrowed from ` +
    `LEGACY_SHARED and ${Object.values(UNSTYLED).reduce((n, list) => n + list.length, 0)} unstyled pinned)`,
)
