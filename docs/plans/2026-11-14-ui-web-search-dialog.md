# ui-web search dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ui-web's inline session-search row with a centered search dialog opened by `#findBtn` and by Cmd+K.

**Architecture:** `state/find.ts` keeps only `{open, query}` and is written by the dialog's input and by the rail's button; `chrome/SearchDialog.tsx` renders the veil and the result list from the rail store's snapshot; `features/rail/store.ts` exposes one `openSession()` used by both a rail row and a dialog result.

**Tech Stack:** React 19 + TypeScript, vitest + happy-dom + @testing-library/react, plain CSS in `ui-web/src/styles/page.css`.

**Spec:** `docs/specs/2026-11-13-ui-web-search-dialog-design.md`

## Global Constraints

- All comments in source are English, and added only where the code cannot say the thing itself.
- Run tests with `cd ui-web && npx vitest run <path>`; type-check with `npm run type-check`; CSS and class gates with `node scripts/check-css.mjs` and `node scripts/check-class-namespace.mjs`.
- Import direction (`src` ranks in `ui-web/scripts/gates/import-direction.test.mjs`): `state/` may not import `features/`; `chrome/` may.
- New classes in `chrome/` must carry the `chrome-` prefix (`scripts/check-class-namespace.mjs`), and every class the markup writes must be defined in `page.css`.
- `page.css` may not carry a color literal outside a `--token:` definition (it must go through `var(--…)` or a `color-mix()` of tokens).
- Never remove `gui.rail.search_hits` or `gui.rail.no_hits` from `i18n/messages.json`: the TUI reads both (`ui-tui/src/i18n/messages.generated.ts`).
- The search input stays uncontrolled — no `value` prop — so the reader's caret and IME are untouched.
- No new dependencies.

## Review Focus

- A term with regex metacharacters (`(`, `*`, `\`, `[`) must highlight as literal text and must not throw.
- An IME composition in progress (Chinese input) must not let Enter open a conversation or Escape close the dialog.
- A session with no `last` line must render its title alone, never an empty or "undefined" excerpt.
- Opening a result from a module page must leave that page first (`page.show(null)`), not open a conversation behind it.
- Closing by scrim click must leave the field visibly empty on the next open — the DOM value, not just the store.

---

### Task 1: One place that opens a conversation

**Files:**
- Modify: `ui-web/src/features/rail/store.ts` (imports at the top, `open()` at 150)
- Modify: `ui-web/src/features/rail/RailPage.tsx:109-117` (`Row`'s `go`)
- Test: `ui-web/src/features/rail/RailPage.test.tsx`

**Interfaces:**
- Produces: `export function openSession(s: SessRow): void` in `features/rail/store.ts` — leaves any module page, then makes `s` current and opens it, unless it is already current.
- Consumes: `current` and `setCurrent` from `src/lib/session.ts`, `page.show` from `src/state/page.ts`, and the store's own `open(s)`.

- [ ] **Step 1: Write the two cases**

In `RailPage.test.tsx`, next to the other row cases:

```ts
it('opens the clicked conversation, leaving any module page first', () => {
  const h = install({ rows: [row({ id: 'a' }), row({ id: 'b', title: 'second task' })], cur: 'a' })
  rowByTitle(mount(), 'second task').click()
  expect(h.calls).toEqual([['showPage', null], ['openSession', 'b']])
})

it('does not re-open the conversation already current', () => {
  const h = install({ rows: [row({ id: 'a' })], cur: 'a' })
  rowByTitle(mount(), 'GTM research').click()
  expect(h.calls).toEqual([['showPage', null]])
})
```

- [ ] **Step 2: Run them**

Run: `cd ui-web && npx vitest run src/features/rail/RailPage.test.tsx`
Expected: PASS — these pin the behavior the extraction must preserve.

- [ ] **Step 3: Extract**

In `features/rail/store.ts`:

```ts
export function openSession(s: SessRow): void {
  page.show(null)
  if (s.id !== current()) {
    setCurrent(s.id)
    open(s)
  }
}
```

Add `current` to the existing `lib/session` import and `import * as page from '../../state/page'` (it already imports `navState` from that module — fold the two into that import).

In `RailPage.tsx`, `Row.go` becomes:

```ts
const go = (): void => {
  if (editing) return
  openSession(s)
}
```

with `openSession` imported from `./store` (the file already imports `* as store`; call `store.openSession(s)` instead of adding a named import).

- [ ] **Step 4: Run the case again**

Run: `cd ui-web && npx vitest run src/features/rail/RailPage.test.tsx`
Expected: PASS, both cases.

- [ ] **Step 5: Commit**

```bash
git add ui-web/src/features/rail/store.ts ui-web/src/features/rail/RailPage.tsx ui-web/src/features/rail/RailPage.test.tsx
git commit -m "refactor(ui-web): open a conversation through one rail function"
```

---

### Task 2: The search dialog component

**Files:**
- Create: `ui-web/src/chrome/SearchDialog.tsx`
- Create: `ui-web/src/chrome/SearchDialog.test.tsx`
- Modify: `ui-web/src/styles/page.css` (append a block after the `.veil` rules at 5909-5921)
- Modify: `i18n/messages.json` (the `ui` section, near `gui.search_sessions`)

**Interfaces:**
- Produces: `export function SearchDialog(): JSX.Element` — renders `#findVeil`, reads `state/find.ts` and `features/rail/store.ts`, calls `features/rail/store.ts`'s `openSession(row)` on a choice.
- Consumes: `find.get/subscribe/set/toggle` (`state/find.ts`), `store.get/subscribe` and `store.openSession` (`features/rail/store.ts`), `composing` (`features/composer/store.ts`), `t` (`i18n/t.ts`), `lang.attr` (`state/lang`), `Icon` (`components/Icon`).

**Markup** (this exact shape is what the golden in Task 3 pins):

```tsx
<div className="veil" id="findVeil" data-open={s.open ? 'true' : 'false'}>
  <div className="chrome-find-card" role="dialog" aria-modal="true" aria-label={lang.attr('gui.search_sessions')}>
    <div className="chrome-find-field">
      <span className="chrome-find-ic"><Icon icon={Search01Icon} size={16} /></span>
      <input id="sfind" placeholder={t('gui.search_sessions')} aria-label={lang.attr('gui.search_sessions')}
             role="combobox" aria-expanded={rows.length > 0} aria-controls="findList"
             aria-activedescendant={rows.length ? `findhit-${active}` : undefined} autoComplete="off" />
      <button className="chrome-find-clr" id="sclr" hidden={!s.query} aria-label={lang.attr('gui.clear_search')}>&#10005;</button>
    </div>
    {/* count line + list, only while a term is typed */}
  </div>
</div>
```

The list part, rendered only when `s.query` is non-empty:

```tsx
<div className="chrome-find-count" id="findCount">{t('gui.find.count', { n: rows.length })}</div>
<div className="chrome-find-list" id="findList" role="listbox" aria-labelledby="findCount">
  {/* one per row, or the empty note */}
  <button className="chrome-find-hit" role="option" id={`findhit-${i}`} aria-selected={i === active}>
    <span className="chrome-find-title">{…}</span>
    <span className="chrome-find-excerpt">{…}</span>
  </button>
</div>
```

and, when `rows.length === 0`, `<div className="chrome-find-empty">{t('gui.find.empty')}</div>` in place of the list.

**Behavior:**
- `rows = (snap?.rows ?? []).filter(r => !q || r.title.toLowerCase().includes(q) || (r.last ?? '').toLowerCase().includes(q))`, `q = s.query`.
- `active` is React state, reset to `0` whenever `s.query` changes.
- Input `onInput`: `find.set({ query: e.currentTarget.value.trim().toLowerCase() })`.
- Input `onKeyDown`: `if (composing(e)) return`; `Escape` → `e.stopPropagation(); find.toggle(false)`; with rows, `ArrowDown`/`ArrowUp` → `preventDefault()` and move `active` with wrap-around; `Enter` → `preventDefault(); choose(rows[active])`.
- `choose(row)`: `openSession(row); find.toggle(false)`.
- Scrim: the existing `useScrim` pattern is in `App.tsx` and not exported — attach a `useEffect` click listener to `#findVeil` that closes when `e.target === el`.
- On `s.open` changing: `true` → `field.current?.focus()`; `false` → clear the DOM value and `find.set({ query: '' })`.
- Highlight helper in this file:

```ts
function parts(text: string, term: string): Array<{ text: string; hit: boolean }>
```

splits case-insensitively on `term` after escaping regex metacharacters; returns one non-hit part when `term` is empty. The component renders hit parts as `<mark>`.

**Copy** in `i18n/messages.json`:

```json
"gui.find.count": { "en": "{n} matching conversations", "zh": "匹配的会话 · {n}" },
"gui.find.empty": { "en": "No conversation matches that.", "zh": "没有匹配的会话。" }
```

**Styles** (append to `page.css`; every color through a token):

```css
/* ---- the search dialog ------------------------------------------- */
#findVeil[data-open="true"] { align-items: start; padding-top: 20vh; }
.chrome-find-card { width: min(640px, 100vw - 32px); background: var(--ink);
  border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); overflow: hidden; }
.chrome-find-field { display: flex; align-items: center; gap: 8px; height: 44px;
  padding: 0 12px; border-bottom: 1px solid var(--line-soft); }
.chrome-find-ic { display: grid; color: var(--faint); }
.chrome-find-field input { flex: 1; min-width: 0; border: 0; outline: 0; background: none;
  font-size: 14px; color: var(--text); }
.chrome-find-field input::placeholder { color: var(--faint); }
.chrome-find-clr { color: var(--faint); padding: 2px 4px; border-radius: 4px; }
.chrome-find-clr:hover { color: var(--text); }
.chrome-find-count { padding: 8px 16px; font-size: 11px; color: var(--faint);
  background: var(--surface); border-bottom: 1px solid var(--line-soft); }
.chrome-find-list { max-height: min(26rem, 60vh); overflow: auto; }
.chrome-find-hit { display: block; width: 100%; padding: 12px 16px; text-align: left;
  border-bottom: 1px solid var(--line-soft); }
.chrome-find-hit:last-child { border-bottom: 0; }
.chrome-find-hit:hover,
.chrome-find-hit[aria-selected="true"] { background: color-mix(in srgb, var(--warm) 12%, transparent); }
.chrome-find-title { display: block; font-size: 13.5px; color: var(--text); }
.chrome-find-excerpt { display: block; margin-top: 2px; font-size: 12px; color: var(--muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chrome-find-hit mark { background: var(--amber-dp); color: inherit; border-radius: 3px; padding: 0 1px; }
.chrome-find-empty { padding: 24px 16px; text-align: center; font-size: 12.5px; color: var(--muted); }
```

- [ ] **Step 1: Write the failing tests**

`chrome/SearchDialog.test.tsx` — `// @vitest-environment happy-dom`, `setTranslator((key, vars) => vars ? \`${key} ${JSON.stringify(vars)}\` : key)`, and this harness:

```ts
function mount(rows: SessRow[], term = ''): { calls: unknown[] } {
  const calls: unknown[] = []
  setSources({ rail: {
    snapshot: () => ({ rows, cur: null, busy: false }),
    replace: () => {}, open: (s: SessRow) => calls.push(s.id),
  } as unknown as RailSource })
  store.set({ snap: { rows, cur: null, busy: false }, skel: false })
  find.set({ open: true, query: term })
  render(<SearchDialog />)
  return { calls }
}
```

`field()` is `document.getElementById('sfind') as HTMLInputElement`. `afterEach` unmounts, calls `find._resetForTests()` and `resetSources()`.

Cases, each asserting exactly:

1. `counts the matching conversations` — rows `[{title:'GTM research', last:'made a table'}, {title:'second task'}]`, term `gtm` → `screen.getByText('gui.find.count {"n":1}')`, and `document.querySelectorAll('.chrome-find-hit')` has length 1.
2. `marks the matched run in the title and the excerpt` — term `gtm`, title `GTM research` → `[...document.querySelectorAll('.chrome-find-hit mark')].map(m => m.textContent)` equals `['GTM']` (case-insensitive match on a lowercased term).
3. `leaves a row with no last line to its title` — row `{title:'a'}`, term `a` → the hit has one child (`chrome-find-title`) and no `.chrome-find-excerpt`.
4. `says so when nothing matches` — term `zzz` → `screen.getByText('gui.find.empty')`.
5. `treats a term of regex characters as text` — term `(a` → no throw; the rendered title still reads `(a) notes`.
6. `moves the active row with the arrow keys and wraps` — two rows, term `a`; `fireEvent.keyDown(field(), { key: 'ArrowDown' })` → `#findhit-1` has `aria-selected="true"`; again → `#findhit-0`.
7. `opens the active row on Enter` — rows `[{id:'a',title:'GTM research'}, {id:'b',title:'second task'}]`, term `t` (both match); ArrowDown then Enter → `calls` equals `['b']` and `find.get().open` is false.
8. `opens the clicked row` — click `#findhit-0` → `calls` equals `['a']`.
9. `closes on Escape without letting the page see it` — a document listener counting `keydown`; `fireEvent.keyDown(field(), { key: 'Escape' })` → `find.get().open` false and the document count 0.
10. `closes on a click beside the card` — `fireEvent.click(document.getElementById('findVeil')!)` → open false.
11. `ignores Enter while an input method is composing` — a row matching the term, `fireEvent.keyDown(field(), { key: 'Enter', isComposing: true })` → `calls` empty, still open.
12. `empties the field when it closes and focuses it when it opens` — type into the field; `fireEvent.click(document.getElementById('findVeil')!)` closes it; the field's `value` is `''` and `find.get().query` is `''`; then `find.toggle(true)` inside `act` → `document.activeElement` is the field.

- [ ] **Step 2: Run them to see them fail**

Run: `cd ui-web && npx vitest run src/chrome/SearchDialog.test.tsx`
Expected: FAIL — `SearchDialog` does not exist.

- [ ] **Step 3: Implement the component, copy and styles** as specified above.

- [ ] **Step 4: Run them to green, and the gates**

```bash
cd ui-web && npx vitest run src/chrome/SearchDialog.test.tsx
node scripts/check-css.mjs
node scripts/check-class-namespace.mjs
npm run type-check
```

Expected: the case file passes; both gates print `OK`; `tsc` exits 0.

- [ ] **Step 5: Commit**

```bash
git add ui-web/src/chrome/SearchDialog.tsx ui-web/src/chrome/SearchDialog.test.tsx ui-web/src/styles/page.css i18n/messages.json
git commit -m "feat(ui-web): add the session search dialog"
```

---

### Task 3: Mount the dialog and drop the inline row

**Files:**
- Modify: `ui-web/src/App.tsx` (import + render after `<ExportVeil />` at 382)
- Modify: `ui-web/src/chrome/Rail.tsx:190-217` (delete `FindRow`), `:203` (`<FindRow />` render), `:81-90` (`findBtn`)
- Modify: `ui-web/src/chrome/Rail.test.tsx` (cases at 97-103, 155-166, 227, 317)
- Modify: `ui-web/src/main.tsx:96,116` (drop `find.onChange` and `find.install`)
- Modify: `ui-web/src/test/regions.test.ts` (`REGIONS`)
- Create: `ui-web/src/test/__golden__/region-findVeil.txt`

**Interfaces:**
- Consumes: `SearchDialog` from Task 2; `find.toggle`, `find.get().open` — both exist already, unchanged.

- [ ] **Step 1: Update the rail's cases first**

In `Rail.test.tsx`: delete the line `expect(el('findBox').hidden).toBe(true)`; keep `findBtn`'s `aria-expanded="false"` and `#sfind`'s non-empty placeholder (the field now lives in the dialog, which the page root renders); in the case that clicks `#findBtn`, expect `document.getElementById('findVeil')!.dataset.open` to be `'true'` and `findBtn`'s `aria-expanded` `'true'` instead of `findBox.hidden`.

Run: `cd ui-web && npx vitest run src/chrome/Rail.test.tsx` → FAIL on the veil assertion.

- [ ] **Step 2: Wire it**

- `App.tsx`: `import { SearchDialog } from './chrome/SearchDialog'` and render `<SearchDialog />` immediately after `<ExportVeil />`.
- `Rail.tsx`: delete `FindRow` and its render site; `#findBtn` keeps `aria-expanded={s.open}` and `onClick={() => find.toggle()}`.
- `main.tsx`: delete `find.onChange(rail.draw)` (96) and `find.install()` (116). The dialog now owns both the field's input and its keys, and two sets of listeners on one `#sfind` is one set too many.
- `regions.test.ts`: add `'findVeil'` after `'exportVeil'` in `REGIONS`.

- [ ] **Step 3: Mint the golden**

Run: `cd ui-web && npx vitest run src/test/regions.test.ts`
Expected: PASS on the first developer-machine run, writing `src/test/__golden__/region-findVeil.txt`. Read it and confirm it holds `div#findVeil.veil[data-open=false]` and the card, then keep it.

- [ ] **Step 4: Run the page's suites**

```bash
cd ui-web && npx vitest run src/chrome src/App.test.tsx src/test/regions.test.ts src/state
```

Expected: PASS. `App.test.tsx:189` still finds `#sfind` with the search placeholder.

- [ ] **Step 5: Commit**

```bash
git add ui-web/src/App.tsx ui-web/src/chrome/Rail.tsx ui-web/src/chrome/Rail.test.tsx ui-web/src/main.tsx ui-web/src/test/regions.test.ts ui-web/src/test/__golden__/region-findVeil.txt
git commit -m "feat(ui-web): open the search dialog from the rail"
```

---

### Task 4: The rail stops filtering, and the store shrinks

**Files:**
- Modify: `ui-web/src/state/find.ts` (whole file)
- Modify: `ui-web/src/state/find.test.ts` (whole file)
- Modify: `ui-web/src/features/rail/RailPage.tsx:394-408`
- Modify: `ui-web/src/features/rail/RailPage.test.tsx` (mock at 26-30, cases at 479-517)
- Modify: `ui-web/src/main.tsx:26` (the now-unused `find` import)

**Interfaces:**
- Produces: `state/find.ts` exporting exactly `FindState` (`{ open, query }`), `get`, `subscribe`, `_resetForTests`, `set(patch: Partial<FindState>)`, `toggle(force?: boolean)`. `term()`, `clear()`, `onChange()` and `install()` are gone.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Stop filtering, and watch the two cases fail**

In `RailPage.tsx`: delete the `term as findTerm` import, the `query`/`hit` consts and `const rows = snap.rows.filter(hit)` (use `snap.rows`), the `if (query && !rows.length)` early return, and the `if (query) { … search_hits … }` branch. The grouped list is the only branch left.

Run: `cd ui-web && npx vitest run src/features/rail/RailPage.test.tsx`
Expected: FAIL on `answers a search with hits and with the empty note` and `reads the term on every draw, not once at mount`.

- [ ] **Step 2: Delete the cases that described the old behavior**

In `RailPage.test.tsx`: delete the `found` hoisted object and the `vi.mock('../../state/find', …)` line, every `found.term = …` assignment, and those two cases. The imported-row case at 479 keeps its assertions minus its `found.term = 'bug'` line.

Run the file again → PASS.

- [ ] **Step 3: Write the store's cases against its final API**

Replace `state/find.test.ts` with store-only cases (no DOM, no `mountPageRoot`):

```ts
it('opens and closes on toggle, and honours a forced state', …)
it('drops the term when it closes', …)             // toggle(true); set({query:'gtm'}); toggle(false) → query ''
it('costs no redraw when closing an empty row', …) // a subscribe counter stays at 0 after toggle(false) on an empty term
it('merges a patch and ignores one that changes nothing', …)
```

Run: `cd ui-web && npx vitest run src/state/find.test.ts` → PASS on the old module too. These are the surviving contract, not a new behavior; the removal below is what must not break them.

- [ ] **Step 4: Shrink `state/find.ts`**

Keep the module docstring's first sentence and rewrite it for the dialog. The body:

```ts
export interface FindState {
  /** Whether the dialog is showing. */
  readonly open: boolean
  /** What the dialog filters by, lowercased and trimmed at write time. */
  readonly query: string
}

const store = makeStore<FindState>({ open: false, query: '' })
export const { get, subscribe, _resetForTests } = store

export function set(next: Partial<FindState>): void { … }   // no-op when nothing changes

export function toggle(force?: boolean): void {
  if (force ?? !get().open) { set({ open: true }); return }
  set({ open: false, query: '' })
}
```

`toggle(false)` on an already-empty, already-closed store must not notify subscribers. Drop the `composing` import along with the keydown handler it guarded.

- [ ] **Step 5: Run it green, and check nothing reaches for the removed names**

In `main.tsx`: delete the `find` import at 26 (its two calls went in Task 3).

```bash
cd ui-web && npx vitest run src/state/find.test.ts src/features/rail src/chrome
grep -rn "find.install\|find.onChange\|findTerm\|\bterm()" src/ | grep -v "state/find.ts"
npm run type-check
```

Expected: PASS; the grep prints nothing; `tsc` 0.

- [ ] **Step 6: Commit**

```bash
git add ui-web/src/state/find.ts ui-web/src/state/find.test.ts ui-web/src/features/rail/RailPage.tsx ui-web/src/features/rail/RailPage.test.tsx ui-web/src/main.tsx
git commit -m "refactor(ui-web): keep the search term for the dialog alone"
```

---

### Task 5: Cmd+K

**Files:**
- Modify: `ui-web/src/state/globalListeners.ts:88-107`
- Test: `ui-web/src/state/globalListeners.test.ts` (the ORDER row's label)

**Interfaces:**
- Consumes: `find.toggle` from `state/find.ts`.

- [ ] **Step 1: Write the failing case**

In `globalListeners.test.ts`, add to the existing shortcut coverage:

```ts
it('opens the search dialog on Cmd+K and on Cmd+F', () => {
  for (const key of ['k', 'f']) {
    document.dispatchEvent(new KeyboardEvent('keydown', { key, metaKey: true, bubbles: true, cancelable: true }))
    expect(find.get().open).toBe(true)
    find.toggle(false)
  }
})
```

- [ ] **Step 2: Run it**

Run: `cd ui-web && npx vitest run src/state/globalListeners.test.ts` → FAIL on `k`.

- [ ] **Step 3: Implement**

In `onEscapeOrder`, replace the Cmd/Ctrl+F branch with one branch for both keys:

```ts
if ((e.metaKey || e.ctrlKey) && (e.key.toLowerCase() === 'k' || e.key.toLowerCase() === 'f')) {
  e.preventDefault(); toggleFind(true)
}
```

Drop the `setRail(true)` call and the now-unused `railOpen` import if nothing else reads it. Update the handler's comment ("three shortcuts" → "four") and the ORDER row's description string in `globalListeners.test.ts` to `'the Escape order and its four shortcuts'`.

- [ ] **Step 4: Run green**

Run: `cd ui-web && npx vitest run src/state/globalListeners.test.ts` → PASS.

- [ ] **Step 5: Commit**

```bash
git add ui-web/src/state/globalListeners.ts ui-web/src/state/globalListeners.test.ts
git commit -m "feat(ui-web): open search on Cmd+K"
```

---

### Task 6: Escape takes the dialog back

**Files:**
- Modify: `ui-web/src/state/escapeOrder.ts` (`ABOVE`, 82-93; imports at 29-41)
- Test: `ui-web/src/state/escapeOrder.test.ts` (`LAYER_IDS` 49-67, `LAYERS` 131+, comments, pair count 281)

**Interfaces:**
- Consumes: `find.toggle` from `state/find.ts`.

- [ ] **Step 1: Write the failing case**

In `escapeOrder.test.ts`, insert `'#findVeil'` after `'.lightbox'` in `LAYER_IDS` and add its fixture row — the dialog writes `data-open` only while it is mounted, so the store is what answers both ways here:

```ts
'#findVeil': {
  up: () => { find.set({ open: true }); flag('findVeil')() },
  taken: () => !find.get().open,
},
```

with `flag`'s element created in the same setup that creates `#veil` and `#exportVeil`, and `import * as find from './find'` at the top. Then change:

- the doc comment "The seventeen" → "The eighteen";
- `it('has no seventeenth entry, …')` → `'has no eighteenth entry, …'`;
- `expect(pairs).toHaveLength(133)` → `150` (18 layers choose 2 = 153, minus the 3 shared closes);
- the case name "a hundred and thirty-three pairs" → "a hundred and fifty pairs".

Run: `cd ui-web && npx vitest run src/state/escapeOrder.test.ts` → FAIL (no `#findVeil` layer).

- [ ] **Step 2: Implement**

In `escapeOrder.ts`, import `* as find from './find'` and add to `ABOVE`, right after the lightbox entry:

```ts
{ id: '#findVeil', isOpen: flagged('findVeil'), close: () => find.toggle(false) },
```

- [ ] **Step 3: Run green**

Run: `cd ui-web && npx vitest run src/state/escapeOrder.test.ts` → PASS.

- [ ] **Step 4: Commit**

```bash
git add ui-web/src/state/escapeOrder.ts ui-web/src/state/escapeOrder.test.ts
git commit -m "feat(ui-web): let Escape take the search dialog back"
```

---

### Task 7: Whole-branch verification

**Files:** none.

- [ ] **Step 1: Run the full suite and the gates**

```bash
cd ui-web && npm run gen:check
npx vitest run
node scripts/check-css.mjs
node scripts/check-class-namespace.mjs
npm run type-check
npm run lint
npm run build
python3 build.py
node scripts/check-page.mjs
```

Expected: every command exits 0; both gates print `OK`; `vite build` writes the bundle and `build.py` assembles `dist/index.html`.

- [ ] **Step 2: Look at it**

Run: `raven web` (the page build is `npm run build` then `python3 ui-web/build.py`), open the page, and confirm by eye and by keyboard: `#findBtn` opens the card; Cmd+K opens it; typing filters; ArrowUp/Down moves; Enter opens; Escape and a click beside the card close it; the rail's list is unfiltered.

- [ ] **Step 3: Commit anything the checks turned up**

```bash
git add -A ui-web && git commit -m "chore(ui-web): settle the search dialog against the gates"
```
