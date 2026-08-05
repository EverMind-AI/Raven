# EverMind `ui-webui/frontend` Reskin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle the whole `ui-webui/frontend` SPA to the EverMind design language (warm parchment/ink/gold, Atkinson + Cormorant + Bungee + Brygada, square-by-default geometry, hairline dividers) via a token backbone plus bounded signature moments — no behavior/routing/API changes.

**Architecture:** The app is ~95% token-driven: one stylesheet (`src/index.css`) holds every design token and every component uses semantic shadcn classes. So the bulk of the reskin is a token remap that cascades globally; the rest is an enumerated list of non-cascading fixes (radius scale, traffic-light status literals, third-party CSS, hardcoded white/black) and hand-built signature components.

**Tech Stack:** React 19, Vite 8, Tailwind v4 (`@tailwindcss/vite`), shadcn/Radix, `@fontsource*`, `next-themes`, `@xyflow/react` (React Flow), `react-diff-view`, `sonner`, `@tailwindcss/typography`, `unplugin-icons` + `@iconify-json/solar`, `@lobehub/icons`.

**Spec:** `ui-webui/docs/specs/2026-07-24-evermind-frontend-reskin.md` (read it first).

## Global Constraints

- **Package manager: pnpm only** in this subtree (never npm/yarn). Run from `ui-webui/`.
- **Verify gate (every task ends here):**
  - `pnpm -C frontend lint` → **0 errors** (pre-existing warnings are ok).
  - `pnpm -C frontend build` → `tsc -b` + `vite build` complete with no errors.
  - **Visual spot-check** (task-specific, noted per task): run `pnpm -C frontend dev` and view the named route(s) in **both** light and dark. Backend-independent surfaces (setup, empty states, static chrome) verify without the API; data-backed pages verify structure/color even if lists are empty.
- **No JS unit tests exist here** — the Verify gate above replaces the usual TDD cycle. Do not scaffold a test runner.
- **Prettier config is authoritative:** tabs, width 4, single quotes, semicolons, print width 100 (`.prettierrc`). Match it; do not reformat untouched lines. `pnpm format` is available; husky+lint-staged run it pre-commit.
- **i18n:** if a header adds/renames copy, edit `src/i18n/locales/{en,zh}.json` with **targeted** edits only — never `json.dump`/full-file rewrites. Count strings use i18next `_one`/`_other`.
- **Keep `@` alias + `path`/`next/navigation` shims** in `vite.config.ts` — do not remove.
- **React Flow:** memoize layout on a value signature; never hand freshly-built node arrays each render.
- **No new committed assets** (AGENTS.md §7): no new images/SVGs/fonts committed. Fonts are npm deps (live in `node_modules`); the starfield is CSS gradients. The two brand SVGs (`raven_logo.svg`, `favicon.svg`) already exist — edit only, do not add.
- **English-only** source comments; match surrounding comment density (mostly none).
- **Commits (AGENTS.md §3.4):** do **not** commit unless the user explicitly says so. Commit steps below are documented checkpoints; when authorized, use the given Conventional-Commits message and append the trailer `Co-authored-by: Claude (claude-opus-4-8) <noreply@anthropic.com>`. Squash-merge means per-commit bodies are dropped, so the trailer is what carries attribution.
- **Branch (AGENTS.md §2.2):** confirm the base with the user (default `main`) and cut a `feat/...` branch **before** the first edit. Current branch is `feat/gateway-web-channel`; do not assume it is the base.

### EverMind palette reference (exact values used throughout)

```
canvas #FCFCF5   panel #F2F1E9   panel-warm #EBE8DD   panel-deep #E6E0CC
ink #1C1B1B      ink-deep #050505
text #1C1B1B     text-serif #423924   text-muted #70695A   text-faint #8F887B   text-fainter #ABABA2   on-dark #F2F1E9
gold #D99400     gold-light #F7CB72   gold-olive #9C7C1E   glow rgba(252,191,78,.58)
hairline (light) rgba(49,48,46,.08)   hairline (dark) rgba(242,241,233,.10)
destructive (warm terracotta) light #B4472B   dark #C75B3E
tile tints: mint #E5E7DB, cream #F2F0E9, peach #F0E7DE, lavender #E7E5EC
fonts: "Atkinson Hyperlegible Next" / "Cormorant Garamond" / "Bungee Shade" / "Brygada 1918"
```

---

## Task 1: Typography — install EverMind faces + wire tokens

**Files:**
- Modify: `ui-webui/frontend/package.json` (deps)
- Modify: `ui-webui/frontend/src/index.css:1-6` (imports), `:127-168` (`@theme`), `@layer base`

**Interfaces:**
- Produces: `@theme` tokens `--font-sans`, `--font-heading`, `--font-serif`, `--font-display`, `--font-label`; base heading rules; `.em-accent` utility. All later tasks assume these exist.

- [ ] **Step 1: Add fonts, remove Geist**

```bash
cd ui-webui/frontend
pnpm add @fontsource-variable/atkinson-hyperlegible-next @fontsource-variable/cormorant-garamond @fontsource/bungee-shade @fontsource-variable/brygada-1918
pnpm remove @fontsource-variable/geist
```

- [ ] **Step 2: Replace the font import block** in `src/index.css` (currently line 4 `@import '@fontsource-variable/geist';`)

```css
@import '@fontsource-variable/atkinson-hyperlegible-next';
@import '@fontsource-variable/cormorant-garamond';
@import '@fontsource/bungee-shade';
@import '@fontsource-variable/brygada-1918';
```

- [ ] **Step 3: Rewire the font tokens** in the `@theme inline` block (currently lines 128-129)

```css
	--font-sans: 'Atkinson Hyperlegible Next', 'Atkinson Hyperlegible', sans-serif;
	--font-heading: 'Atkinson Hyperlegible Next', 'Atkinson Hyperlegible', sans-serif;
	--font-serif: 'Cormorant Garamond', serif;
	--font-display: 'Bungee Shade', sans-serif;
	--font-label: 'Brygada 1918', serif;
```

- [ ] **Step 4: Add heading + accent rules** to `@layer base` (after the existing `html { @apply font-sans; }`)

```css
	h1,
	h2,
	h3,
	h4 {
		font-weight: 500;
	}
	h1 {
		letter-spacing: -0.05em;
		line-height: 1.02;
	}
	h2 {
		letter-spacing: -0.02em;
	}
	.em-accent {
		font-family: var(--font-serif);
		font-style: italic;
		font-weight: 500;
		color: var(--gold-olive);
	}
```

- [ ] **Step 5: Verify gate.** Visual: any route — confirm body/headings render in Atkinson Hyperlegible (not Geist), no missing-font FOUT, build clean.

- [ ] **Step 6: Commit (when authorized)**

```
feat(ui-webui): adopt EverMind typefaces via fontsource
```

---

## Task 2: Light-theme token remap + dead-code cleanup

**Files:**
- Modify: `ui-webui/frontend/src/index.css` — `:root` (lines 17-106), `@media (prefers-color-scheme: dark)` (108-125), `@theme inline` (127-168)

**Interfaces:**
- Produces: warm `:root` shadcn tokens; new brand tokens `--gold`, `--gold-light`, `--gold-olive`, `--gold-foreground`, `--glow`; `@theme` color utilities `bg-gold`/`text-gold`/`text-gold-olive`/`bg-gold-light`. Later tasks use these utilities.

- [ ] **Step 1: Replace the `:root` legacy header** (lines 17-45 — the `--text/--text-h/--bg/--border/--code-bg/--accent*/--social-bg/--shadow/--sans/--heading/--mono` block, the `letter-spacing: 0.18px`, and the `color/background` lines) with only the still-needed base declarations:

```css
:root {
	color-scheme: light dark;
	font-synthesis: none;
	text-rendering: optimizeLegibility;
	-webkit-font-smoothing: antialiased;
	-moz-osx-font-smoothing: grayscale;

	@media (max-width: 1024px) {
		font-size: 16px;
	}

	/* EverMind brand tokens (accent + glow) */
	--gold: #d99400;
	--gold-light: #f7cb72;
	--gold-olive: #9c7c1e;
	--gold-foreground: #1c1b1b;
	--glow: rgba(252, 191, 78, 0.58);
```

(Keep the closing `}` of `:root` after the shadcn block replaced in Step 2. Remove the global `letter-spacing`, the `color: var(--text)`, and `background: var(--bg)`.)

- [ ] **Step 2: Replace the shadcn light values** (current lines 47-105) with the warm axis:

```css
	--background: #fcfcf5;
	--foreground: #1c1b1b;
	--card: #f2f1e9;
	--card-foreground: #1c1b1b;
	--popover: #f2f1e9;
	--popover-foreground: #1c1b1b;
	--primary: #1c1b1b;
	--primary-foreground: #f2f1e9;
	--secondary: #f2f1e9;
	--secondary-foreground: #1c1b1b;
	--muted: #ebe8dd;
	--muted-foreground: #70695a;
	--accent: #ebe8dd;
	--accent-foreground: #1c1b1b;
	--destructive: #b4472b;
	--border: rgba(49, 48, 46, 0.08);
	--input: rgba(49, 48, 46, 0.12);
	--ring: #d99400;
	--chart-1: #d99400;
	--chart-2: #9c7c1e;
	--chart-3: #70695a;
	--chart-4: #8f887b;
	--chart-5: #423924;
	--radius: 0px;
	--sidebar: #f2f1e9;
	--sidebar-foreground: #1c1b1b;
	--sidebar-primary: #1c1b1b;
	--sidebar-primary-foreground: #f2f1e9;
	--sidebar-accent: rgba(217, 148, 0, 0.14);
	--sidebar-accent-foreground: #1c1b1b;
	--sidebar-border: rgba(49, 48, 46, 0.08);
	--sidebar-ring: #d99400;
```

- [ ] **Step 3: Delete dead OS-preference theme + dead rule.** Remove the entire `@media (prefers-color-scheme: dark) { :root { … } #social … }` block (current lines 108-125). (Real dark theme is authored in Task 4 as the `.dark` class.)

- [ ] **Step 4: Expose brand colors as utilities** — add to the `@theme inline` block:

```css
	--color-gold: var(--gold);
	--color-gold-light: var(--gold-light);
	--color-gold-olive: var(--gold-olive);
	--color-gold-foreground: var(--gold-foreground);
```

- [ ] **Step 5: Verify gate.** Visual: chat + a config page in light — parchment canvas, ink text, gold focus ring on an input, no violet/gray-blue anywhere, no pure white panels (except the known `bg-white` spots fixed in Task 3/5).

- [ ] **Step 6: Commit (when authorized)**

```
feat(ui-webui): remap design tokens to EverMind warm palette
```

---

## Task 3: Shape system — square-by-default radius + flat elevation

**Files:**
- Modify: `src/index.css` `@theme inline` radius scale (lines 161-167)
- Modify: `src/components/ui/badge.tsx:8`, `checkbox.tsx:12`, `tooltip.tsx:47`
- Modify: shadows in `ui/popover.tsx:27`, `ui/select.tsx:66`, `ui/dropdown-menu.tsx:37,225`, `ui/sheet.tsx:57`, `ui/sidebar.tsx:237,298`, `ui/tabs.tsx:57`, `pages/schedule/schedule-card.tsx:25`
- Modify: overlays in `ui/dialog.tsx:32`, `ui/sheet.tsx:32`, `ui/drawer.tsx:32`

**Interfaces:**
- Consumes: Task 2 (`--radius: 0px` already set in `:root`).
- Produces: radius tokens — square (0) for `sm/md/lg`, `--radius-xl: 12px` (card/panel), `--radius-2xl/3xl: 16px` (tile), `--radius-4xl: 999px` (pill). Later tasks rely on `rounded-xl`=card, `rounded-2xl`=tile.

Note: setting these to **absolute** values makes the many `rounded-[min(var(--radius-md),Npx)]` (button/toggle/select) and `rounded-[calc(var(--radius)-Npx)]` (input-group) resolve to 0 automatically — no per-file edit needed for those. Only truly hardcoded literals below need manual edits.

- [ ] **Step 1: Rewrite the radius scale** in `@theme inline` (replace lines 161-167):

```css
	--radius-sm: 0px;
	--radius-md: 0px;
	--radius-lg: 0px;
	--radius-xl: 12px;
	--radius-2xl: 16px;
	--radius-3xl: 16px;
	--radius-4xl: 999px;
```

- [ ] **Step 2: Pin the badge pill.** `ui/badge.tsx:8` — replace `rounded-4xl` with `rounded-full`.

- [ ] **Step 3: Square the hardcoded control literals.** `ui/checkbox.tsx:12` `rounded-[4px]` → `rounded-none`; `ui/tooltip.tsx:47` arrow `rounded-[2px]` → `rounded-none`.

- [ ] **Step 4: Flatten elevation.** Remove the `shadow-sm`/`shadow-md`/`shadow-lg` utilities at the listed lines and, where the surface had no border, add the hairline `ring-1 ring-foreground/10` (popover/select/dropdown/sheet content). For `schedule-card.tsx:25` replace `hover:shadow-md` with `hover:border-gold/40`.

- [ ] **Step 5: Ink-tint overlays.** In `ui/dialog.tsx:32`, `ui/sheet.tsx:32`, `ui/drawer.tsx:32` replace `bg-black/10` with `bg-[rgba(28,27,27,0.35)]`.

- [ ] **Step 6: Verify gate.** Visual: open a dialog, a dropdown/select, and a card — corners square on buttons/inputs/menu-rows, 12px on card/dialog containers, badges are pills, no drop shadows, overlay reads as an ink scrim.

- [ ] **Step 7: Commit (when authorized)**

```
feat(ui-webui): square-default radius scale and flat hairline elevation
```

---

## Task 4: Activate light + dark themes (next-themes)

**Files:**
- Modify: `src/main.tsx` (mount provider), `src/App.tsx` (provider wrap + drop `richColors` prep is Task 7)
- Modify: `src/components/layout/AppSidebar.tsx` (footer theme toggle)
- Modify: `src/index.css` — author the `.dark { … }` block (lines 170-202)
- Modify: `src/components/dag/DagGraph.tsx:227` (colorMode reads provider — already does; verify), `src/components/ui/sonner.tsx` theme wiring is Task 7

**Interfaces:**
- Consumes: Task 2 tokens.
- Produces: a working `class`-based theme with a toggle; `.dark` token values. Third-party dark (React Flow, sonner) is finished in Tasks 7-8.

- [ ] **Step 1: Mount the provider.** In `src/main.tsx`, wrap the app with `next-themes`:

```tsx
import { ThemeProvider } from 'next-themes';
// …
<ThemeProvider attribute="class" defaultTheme="light" enableSystem={false} disableTransitionOnChange>
	{/* existing tree */}
</ThemeProvider>
```

(Wrap outside the existing `TooltipProvider`. `enableSystem={false}` avoids the OS-desync the removed legacy path had.)

- [ ] **Step 2: Author the `.dark` block** — replace the current `.dark { … }` (lines 170-202) with EverMind ink values:

```css
.dark {
	--background: #050505;
	--foreground: #f2f1e9;
	--card: #1c1b1b;
	--card-foreground: #f2f1e9;
	--popover: #1c1b1b;
	--popover-foreground: #f2f1e9;
	--primary: #f2f1e9;
	--primary-foreground: #1c1b1b;
	--secondary: #2a2724;
	--secondary-foreground: #f2f1e9;
	--muted: #2a2724;
	--muted-foreground: #ababa2;
	--accent: #2a2724;
	--accent-foreground: #f2f1e9;
	--destructive: #c75b3e;
	--border: rgba(242, 241, 233, 0.1);
	--input: rgba(242, 241, 233, 0.14);
	--ring: #d99400;
	--chart-1: #d99400;
	--chart-2: #f7cb72;
	--chart-3: #ababa2;
	--chart-4: #8f887b;
	--chart-5: #f2f1e9;
	--sidebar: #1c1b1b;
	--sidebar-foreground: #f2f1e9;
	--sidebar-primary: #f2f1e9;
	--sidebar-primary-foreground: #1c1b1b;
	--sidebar-accent: rgba(217, 148, 0, 0.2);
	--sidebar-accent-foreground: #f2f1e9;
	--sidebar-border: rgba(242, 241, 233, 0.1);
	--sidebar-ring: #d99400;
}
```

- [ ] **Step 3: Add a theme toggle** to `AppSidebar.tsx` footer (a new `SidebarMenuItem` above Settings), using `useTheme()` and Solar sun/moon icons:

```tsx
import { useTheme } from 'next-themes';
import IconSun from '~icons/solar/sun-2-bold-duotone';
import IconMoon from '~icons/solar/moon-bold-duotone';
// inside component:
const { resolvedTheme, setTheme } = useTheme();
// in SidebarFooter menu, before Settings:
<SidebarMenuItem>
	<SidebarMenuButton
		tooltip={{ children: t('common.toggleTheme'), hidden: false }}
		onClick={() => setTheme(resolvedTheme === 'dark' ? 'light' : 'dark')}
		className="px-2"
	>
		{resolvedTheme === 'dark' ? <IconSun /> : <IconMoon />}
	</SidebarMenuButton>
</SidebarMenuItem>
```

- [ ] **Step 4: Add i18n key** `common.toggleTheme` to `src/i18n/locales/en.json` (`"Toggle theme"`) and `zh.json` (`"切换主题"`) via targeted edits.

- [ ] **Step 5: Verify gate.** Visual: toggle theme from the sidebar — light parchment ↔ dark ink flips across chat + a config page; `<html>` gets/loses `class="dark"`; gold accent persists in both. (React Flow canvas + toasts will still be off until Tasks 7-8 — note but don't block.)

- [ ] **Step 6: Commit (when authorized)**

```
feat(ui-webui): activate class-based EverMind light/dark theme with sidebar toggle
```

---

## Task 5: Single-accent status color language + native controls

**Files:**
- Modify: `src/components/badge/StatusBadge.tsx:36,48`
- Modify: `src/components/chat/tool-renderers/_shared.tsx:19,22,211,216`
- Modify: `src/components/chat/tool-renderers/DiffPreview.tsx:89,92,114,116`
- Modify: `src/components/panel/McpPanel.tsx:97`
- Modify: `src/components/subagent/SubagentInstanceMonitor.tsx:73-76`
- Modify: `src/components/knowledge/KnowledgeDocumentsPanel.tsx:66`
- Modify: `src/components/chat/WorkingDirectoryControl.tsx:74,78`
- Modify: `src/pages/subagent/index.tsx:601` (native checkbox)

**Interfaces:**
- Consumes: Task 2 (`--destructive` warm, `text-gold`/`text-gold-olive`/`bg-gold` utilities).
- Produces: one warm status vocabulary used app-wide — running/active = gold, completed = gold-olive, failed = destructive.

- [ ] **Step 1: StatusBadge.** `:36` completed `bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-300` → `bg-gold-olive/10 text-gold-olive`; `:48` failed `bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300 border-none` → `bg-destructive/10 text-destructive border-none`.

- [ ] **Step 2: Tool state icons + diff stats.** `_shared.tsx` `:19,:211` `text-emerald-600 dark:text-emerald-400` → `text-gold-olive`; `:22,:216` `text-red-600 dark:text-red-400` → `text-destructive`.

- [ ] **Step 3: Diff rows/gutters.** `DiffPreview.tsx` `:89` insert `bg-emerald-500/10` → `bg-gold-olive/12`; `:92` delete `bg-red-500/10` → `bg-destructive/12`; `:114` `text-emerald-600 dark:text-emerald-400` → `text-gold-olive`; `:116` `text-red-600 dark:text-red-400` → `text-destructive`.

- [ ] **Step 4: MCP health dot.** `McpPanel.tsx:97` ternary `bg-green-500 : bg-red-500` → `bg-gold : bg-destructive`.

- [ ] **Step 5: Subagent instance glyphs.** `SubagentInstanceMonitor.tsx:73-76` completed `text-green-600` → `text-gold-olive`, failed `text-red-600` → `text-destructive`, running `text-blue-600 animate-pulse` → `text-gold animate-pulse`.

- [ ] **Step 6: Upload ready badge.** `KnowledgeDocumentsPanel.tsx:66` `bg-emerald-500/10 text-emerald-700 dark:text-emerald-400` → `bg-gold-olive/10 text-gold-olive`.

- [ ] **Step 7: Working-dir invalid state.** `WorkingDirectoryControl.tsx:74` `border-red-500 focus:border-red-500` → `border-destructive focus:border-destructive`; `:78` `text-red-500` → `text-destructive`.

- [ ] **Step 8: Native checkbox accent.** `pages/subagent/index.tsx:601` — add `accent-[var(--gold)]` to the `<input type="checkbox">` className.

- [ ] **Step 9: Verify gate.** Visual: a StatusBadge in each state, a tool-call ok/fail row, a diff preview (add/remove tint warm), the MCP panel health dot — no green/blue/emerald anywhere; failures read warm terracotta; the subagent checkbox is gold when checked.

- [ ] **Step 10: Commit (when authorized)**

```
feat(ui-webui): unify status colors onto the gold/ink single-accent axis
```

---

## Task 6: Themed chat markdown (Tailwind Typography `prose`)

**Files:**
- Modify: `src/index.css` (add a `prose` token-mapping block)

**Interfaces:**
- Consumes: Task 2 tokens.
- Produces: warm `--tw-prose-*` for `.prose` (light) + `.dark .prose`. Fixes the largest content surface (assistant replies in `MessageBubble.tsx:521`).

- [ ] **Step 1: Add the prose override block** to `src/index.css` (after `@layer base`):

```css
.prose {
	--tw-prose-body: var(--foreground);
	--tw-prose-headings: var(--foreground);
	--tw-prose-lead: var(--muted-foreground);
	--tw-prose-links: var(--gold-olive);
	--tw-prose-bold: var(--foreground);
	--tw-prose-counters: var(--muted-foreground);
	--tw-prose-bullets: var(--muted-foreground);
	--tw-prose-hr: var(--border);
	--tw-prose-quotes: var(--foreground);
	--tw-prose-quote-borders: var(--gold);
	--tw-prose-captions: var(--muted-foreground);
	--tw-prose-code: var(--foreground);
	--tw-prose-pre-code: var(--foreground);
	--tw-prose-pre-bg: var(--muted);
	--tw-prose-th-borders: var(--border);
	--tw-prose-td-borders: var(--border);
}
.dark .prose {
	--tw-prose-body: var(--foreground);
	--tw-prose-headings: var(--foreground);
	--tw-prose-links: var(--gold-light);
	--tw-prose-bold: var(--foreground);
	--tw-prose-quote-borders: var(--gold);
	--tw-prose-pre-bg: var(--muted);
	--tw-prose-code: var(--foreground);
	--tw-prose-pre-code: var(--foreground);
	--tw-prose-hr: var(--border);
	--tw-prose-th-borders: var(--border);
	--tw-prose-td-borders: var(--border);
	--tw-prose-bullets: var(--muted-foreground);
	--tw-prose-counters: var(--muted-foreground);
}
```

- [ ] **Step 2: Verify gate.** Visual: a chat reply containing a heading, a link, a bullet list, a blockquote, an inline `code`, and a fenced code block — all read on the warm axis in light AND dark (no gray Typography defaults; readable in dark).

- [ ] **Step 3: Commit (when authorized)**

```
feat(ui-webui): theme chat markdown prose onto EverMind tokens
```

---

## Task 7: Warm toasts (sonner) + diff base CSS + tour scrim

**Files:**
- Modify: `src/App.tsx:29,86` (drop `richColors`, use token wrapper)
- Modify: `src/components/ui/sonner.tsx` (wire theme + warm vars)
- Modify: `src/index.css` (react-diff-view base overrides + sonner success/error vars)
- Modify: `src/App.tsx:77` (Onborda `shadowRgb`)

**Interfaces:**
- Consumes: Task 4 (`next-themes` mounted).
- Produces: token-driven toasts; warm diff base; ink tour scrim.

- [ ] **Step 1: Use the token-aware Toaster.** In `App.tsx`, replace both `<Toaster richColors position=… />` with the local wrapper and drop `richColors`:

```tsx
import { Toaster } from '@/components/ui/sonner';
// …
<Toaster position="top-right" />
```

- [ ] **Step 2: Wire the wrapper theme + warm accent vars.** In `ui/sonner.tsx`, ensure `theme` comes from `next-themes` `useTheme()` (`resolvedTheme`) and set toast CSS vars to tokens; remove the dead `cn-toast` class. Add success/error accents:

```tsx
style={
	{
		'--normal-bg': 'var(--popover)',
		'--normal-text': 'var(--popover-foreground)',
		'--normal-border': 'var(--border)',
		'--border-radius': '12px',
		'--success-bg': 'var(--popover)',
		'--success-text': 'var(--gold-olive)',
		'--error-bg': 'var(--popover)',
		'--error-text': 'var(--destructive)',
	} as React.CSSProperties
}
```

- [ ] **Step 3: Warm the diff base CSS.** Add to `src/index.css` (react-diff-view leaks base insert/delete tints):

```css
.diff-code-insert,
.diff-gutter-insert {
	background: color-mix(in srgb, var(--gold-olive) 12%, transparent);
}
.diff-code-delete,
.diff-gutter-delete {
	background: color-mix(in srgb, var(--destructive) 12%, transparent);
}
```

- [ ] **Step 4: Ink tour scrim.** `App.tsx` Onborda — add `shadowRgb="28,27,27"` alongside `shadowOpacity="0.6"`.

- [ ] **Step 5: Verify gate.** Visual: trigger a success + an error toast (e.g. save a config page) — warm panels, gold/terracotta accents, correct theme; a diff preview shows warm base tints; start the tour — ink scrim, not black.

- [ ] **Step 6: Commit (when authorized)**

```
feat(ui-webui): warm sonner toasts, diff base tints, and tour scrim
```

---

## Task 8: DAG / React Flow full reskin

**Files:**
- Modify: `src/components/dag/DagGraph.tsx:19-40` (`STATUS_STYLE`), `:99` (node shadow/shape), `:165` (error pre), `:227` (colorMode)
- Modify: `src/components/dag/layoutDag.ts:103` (running edge)
- Modify: `src/index.css` (add a `.react-flow` `--xy-*` override block)
- Modify: `src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx:169-175` (card header eyebrow) + optional KPI strip from `deriveDag` summary

**Interfaces:**
- Consumes: Task 5 (status vocabulary), Task 4 (theme).
- Produces: single-gold DAG on a warm canvas.

- [ ] **Step 1: Recolor node status.** In `DagGraph.tsx` `STATUS_STYLE`: running `border-blue-500 text-blue-600 dark:text-blue-400` → `border-gold text-gold`; completed `border-emerald-500 …` → `border-gold-olive text-gold-olive`; failed `border-red-500 …` → `border-destructive text-destructive`; pending/skipped keep `border-border` (add `opacity-60` for skipped). Error `<pre>` `:165` `text-red-600 dark:text-red-400` → `text-destructive`.

- [ ] **Step 2: Flatten + square nodes.** `DagGraph.tsx:99` remove `shadow-sm`; keep `rounded-md` (now 0 = square) — node is a square hairline card.

- [ ] **Step 3: Gold running edge.** `layoutDag.ts:103` running edges already `animated: true`; add `style: { stroke: 'var(--gold)' }` to the running edge object (and a subtle `strokeWidth: 2`).

- [ ] **Step 4: React Flow `--xy-*` override block** in `src/index.css`:

```css
.react-flow {
	--xy-edge-stroke-default: var(--border);
	--xy-edge-stroke-selected: var(--gold);
	--xy-background-pattern-dots-color-default: color-mix(in srgb, var(--muted-foreground) 40%, transparent);
	--xy-node-boxshadow-selected-default: 0 0 0 2px var(--glow);
	--xy-selection-background-color-default: color-mix(in srgb, var(--gold) 10%, transparent);
	--xy-handle-border-color-default: var(--border);
	--xy-controls-button-background-color-default: var(--popover);
	--xy-controls-button-background-color-hover-default: var(--accent);
	--xy-controls-button-color-default: var(--foreground);
	--xy-controls-button-border-color-default: var(--border);
}
.react-flow.dark {
	--xy-background-color-default: var(--card);
}
.react-flow__controls-button {
	border-radius: 0;
	box-shadow: none;
}
.react-flow__edge.animated path {
	stroke: var(--gold);
}
```

- [ ] **Step 5: Card header eyebrow.** `RunSubagentDagRenderer.tsx:170-175` — render the label as a gold eyebrow, e.g. wrap in a `<span className="em-kicker text-gold">` style (uppercase, `[ DAG ]` or `SUBAGENTS · N NODES`). Optionally add a hairline KPI strip above the canvas from the already-parsed `deriveDag` summary (`total/completed/failed/skipped`) as `text-gold` numerals + faint labels divided by hairlines.

- [ ] **Step 6: Verify gate.** Visual: an inline DAG run (or a fixture) — nodes square + hairline, running = gold + animated gold edge, completed = olive, failed = terracotta; dot grid + controls warm; select a node → gold glow ring; works in light AND dark.

- [ ] **Step 7: Commit (when authorized)**

```
feat(ui-webui): reskin DAG viz to single-gold on a warm React Flow canvas
```

---

## Task 9: Sidebar signature — gold active pill + warm rail

**Files:**
- Modify: `src/components/ui/sidebar.tsx:449` (active menu-button state)
- Modify: `src/components/layout/AppSidebar.tsx:61` (logo wrapper), group divider
- Modify: `src/index.css:221-256` (`.app-nav-rail` icon gold/ink literals)

**Interfaces:**
- Consumes: Tasks 2/4 (`--sidebar-*` tokens).
- Produces: EverMind pill-nav rail.

- [ ] **Step 1: Gold active pill.** `ui/sidebar.tsx` `SidebarMenuButton` — for `data-[active=true]` change the highlight to a pill: add `data-[active=true]:rounded-full data-[active=true]:bg-sidebar-accent data-[active=true]:text-sidebar-accent-foreground` (the `--sidebar-accent` is the gold-tint from Task 2). Keep non-active rows square.

- [ ] **Step 2: Unify nav-icon gold.** `src/index.css` `.app-nav-rail` block — replace `#e3a43b` → `#d99400` (both occurrences) and `#141414` → `#1c1b1b` (both), keeping the `.icon-flip` inverse and `.dark` overrides consistent.

- [ ] **Step 3: Square the brand mark + add a group divider.** `AppSidebar.tsx:61` drop `rounded-lg` on the `RavenLogo` wrapper (square the mark). Between the two `SidebarGroup`s add a hairline divider (`<div className="mx-2 my-1 border-t border-sidebar-border" />` or a `SidebarSeparator`).

- [ ] **Step 4: Verify gate.** Visual: the rail — warm panel bg, single hairline right border, active route is a gold pill behind an ink icon, nav icons are brand gold `#d99400`, hairline divides the two groups; correct in light + dark.

- [ ] **Step 5: Commit (when authorized)**

```
feat(ui-webui): EverMind pill-nav sidebar rail with gold active state
```

---

## Task 10: Editorial empty states (shared)

**Files:**
- Modify: `src/components/ui/empty.tsx:10,34,62` (container, media tile, title)
- Verify consumers: `src/components/chat/Empty.tsx`, `src/components/panel/PanelEmpty.tsx`

**Interfaces:**
- Consumes: Tasks 1/2 (`.em-accent`, `font-label`, `bg-gold`, tile radius).
- Produces: one editorial empty-state look reused across ~10 surfaces.

- [ ] **Step 1: Frameless container.** `ui/empty.tsx:10` — replace `rounded-xl border-dashed` with a hairline/frameless treatment (`border-0` + generous padding); keep centered layout.

- [ ] **Step 2: Pastel-gold icon tile.** `:34` EmptyMedia `icon` variant `size-8 rounded-lg bg-muted text-foreground` → `size-12 rounded-[16px] bg-gold/10 text-gold-olive` (16px tile, soft gold).

- [ ] **Step 3: Mixed-typeface title support.** `:62` EmptyTitle — ensure it renders children with `font-heading` weight-500 so callers can pass a `<span className="em-accent">` accent word; no forced casing.

- [ ] **Step 4: Verify gate.** Visual: chat empty state + an empty config/panel list — pastel-gold 16px tile, hairline (no dashed box), title can carry an italic-serif accent word; light + dark.

- [ ] **Step 5: Commit (when authorized)**

```
feat(ui-webui): editorial empty states with pastel-gold icon tiles
```

---

## Task 11: Page-header eyebrows + mixed-typeface titles

**Files:**
- Modify: `src/pages/raven-channels/index.tsx:142-151`, `raven-cron/index.tsx:107-115`, `raven-subagents/index.tsx:134-143`, `credential/index.tsx:74-77`, `knowledge/index.tsx:143-144`, `subagent/index.tsx:317+345`, `schedule/index.tsx:85`
- Reuse: an `.em-kicker` / eyebrow utility in `src/index.css`

**Interfaces:**
- Consumes: Tasks 1/2 (`.em-accent`, `text-gold`, `font-label`).
- Produces: consistent gold-eyebrow + mixed-typeface H1s across pages.

- [ ] **Step 1: Add the eyebrow utility** to `src/index.css`:

```css
.em-kicker {
	color: var(--gold);
	font-weight: 500;
	text-transform: uppercase;
	letter-spacing: 0.04em;
	font-size: 0.8rem;
}
```

- [ ] **Step 2: Apply per page header.** For each listed header, above the H1 add a gold eyebrow (`<div className="em-kicker">Raven Config · Channels</div>` etc.) and wrap one word of the H1 in `<span className="em-accent">…</span>`. Keep existing icon; set H1 to weight-500 (`font-medium`, drop `font-semibold`). Example (raven-channels):

```tsx
<div className="em-kicker">Raven Config · Channels</div>
<h1 className="text-xl font-medium">
	Messaging <span className="em-accent">channels</span>
</h1>
```

- [ ] **Step 3: i18n.** If eyebrow/title copy is user-facing, add targeted keys to `en.json`/`zh.json` rather than hardcoding (follow each page's existing i18n usage; if a page currently hardcodes English, match that pattern).

- [ ] **Step 4: Verify gate.** Visual: each of the 7 headers shows a gold eyebrow + an italic-serif accent word, weight-500; light + dark.

- [ ] **Step 5: Commit (when authorized)**

```
feat(ui-webui): gold eyebrow + mixed-typeface page headers
```

---

## Task 12: Setup starfield hero + Bungee wordmark

**Files:**
- Modify: `src/pages/setup/index.tsx` (full-screen route)
- Add utilities to `src/index.css` (`.em-hero--dark`, `.em-wordmark`)

**Interfaces:**
- Consumes: Task 1 (`--font-display`), Task 2 (ink/gold tokens).
- Produces: the marquee onboarding screen.

- [ ] **Step 1: Add hero + wordmark utilities** to `src/index.css`:

```css
.em-hero--dark {
	background:
		radial-gradient(1px 1px at 20% 30%, var(--gold-light) 50%, transparent 51%),
		radial-gradient(1px 1px at 70% 60%, var(--gold-light) 50%, transparent 51%),
		radial-gradient(1px 1px at 45% 80%, var(--gold) 50%, transparent 51%),
		radial-gradient(1px 1px at 85% 25%, var(--gold-light) 50%, transparent 51%),
		#1c1b1b;
	color: #f2f1e9;
}
.em-wordmark {
	font-family: var(--font-display);
	text-transform: uppercase;
	letter-spacing: -0.03em;
	font-size: clamp(2.5rem, 8vw, 5rem);
	line-height: 1;
	text-align: center;
}
```

- [ ] **Step 2: Rebuild the setup layout.** Wrap the full-screen container in `em-hero--dark`, center: a gold eyebrow (`SETUP · CONNECT`), the `<div className="em-wordmark">RAVEN</div>`, a mixed-typeface subhead (`Connect your <span class="em-accent">server</span>`), then the existing two fields (server URL, username) on the ink canvas with hairline dividers and a single primary (now ink→ on dark: use the ghost/cream outline) button. Preserve all existing form state/handlers and `onComplete`.

- [ ] **Step 3: Dark-on-dark controls.** On the ink hero, inputs get hairline underlines (`border-b border-white/20`) and the submit button uses a cream/gold treatment (ghost outline `border border-white/40 text-[#f2f1e9]` or a gold fill) — do not use the light ink-button on ink.

- [ ] **Step 4: Verify gate.** Visual: `/setup` — ink starfield, Bungee `RAVEN` wordmark, gold eyebrow, italic-serif accent word, fields legible on dark, submit works and routes onward. (This screen is theme-independent — always the dark hero.)

- [ ] **Step 5: Commit (when authorized)**

```
feat(ui-webui): EverMind starfield setup hero with Bungee wordmark
```

---

## Task 13: Chat composer + tool boxes + working shimmer

**Files:**
- Modify: `src/components/chat/TextInput.tsx:267` (composer), `:372,:392` (send/attach)
- Modify: `src/components/chat/MessageBubble.tsx:489-509` (tool-chain summary), `:511` (group body), `:765-793` (stat micro-row)
- Modify: `src/components/chat/ConfirmCard.tsx:58` (`bg-white`)
- Modify: tool-renderer framed bodies (`_shared.tsx:192`, `DefaultRenderer.tsx`, `BashRenderer.tsx`, etc.) — square + hairline (mostly auto via radius; verify)

**Interfaces:**
- Consumes: Tasks 2/3/5.
- Produces: editorial chat surface with a single gold accent on the composer.

- [ ] **Step 1: Composer.** `TextInput.tsx:267` `rounded-2xl` → `rounded-none` with a hairline top divider separating it from the transcript (`border-t border-border`); keep internal padding.

- [ ] **Step 2: Send = single gold accent.** `:372` send button → gold fill (`bg-gold text-gold-foreground`), keep it the one round action (`rounded-full`); `:392` attach stays ghost + square.

- [ ] **Step 3: ConfirmCard white box.** `ConfirmCard.tsx:58` `bg-white` → `bg-background`.

- [ ] **Step 4: Tool-chain summary + shimmer.** `MessageBubble.tsx:489-509` render the summary line as a gold eyebrow over a hairline (not a ghost button); set the running shimmer color to the gold glow via `--shimmer-color: var(--glow)` on the running element.

- [ ] **Step 5: Stat micro-row.** `:765-793` (duration + token counts, already `tabular-nums`) → hairline stat strip: numerals `text-gold`, faint labels, middot separators.

- [ ] **Step 6: Verify gate.** Visual: chat with a live/streamed reply — squared composer with hairline divider + gold send button; tool-chain summary as gold eyebrow; gold shimmer while a tool runs; ConfirmCard inner box is warm (not white); stat row reads as hairline metrics; light + dark.

- [ ] **Step 7: Commit (when authorized)**

```
feat(ui-webui): editorial chat composer, tool summaries, and gold shimmer
```

---

## Task 14: RouteError + ProviderIcon warm tiles + KPI strips

**Files:**
- Modify: `src/components/error/RouteError.tsx`
- Modify: `src/components/ProviderIcon.tsx:94-101` (tile wrapper)
- Modify: `src/pages/schedule/schedule-detail-drawer.tsx:139-153` (KPI grid), `src/pages/credential/index.tsx:89-91` (counts)

**Interfaces:**
- Consumes: Tasks 1/2 (`.em-kicker`, `.em-accent`, `font-label`, tile radius).
- Produces: editorial error screen, warm provider tiles, hairline KPI strips.

- [ ] **Step 1: RouteError.** Rebuild as an editorial full-screen: gold `[ error ]` kicker, mixed-typeface headline, the detail `<pre>` as a hairline panel-warm block (`bg-muted border border-border` at 12px), square recovery buttons. Preserve the existing error object handling and reset/navigate actions.

- [ ] **Step 2: ProviderIcon warm tile.** `ProviderIcon.tsx:94-101` — wrap the `@lobehub` brand mark in a 16px warm square tile: `<span className="grid size-9 place-items-center rounded-[16px] bg-panel-cream …">` keeping the brand `.Avatar` inside (recognizable), replacing the circular treatment. (Define a `bg-panel-cream`/use `bg-muted` if no dedicated token.)

- [ ] **Step 3: Schedule drawer KPI grid.** `schedule-detail-drawer.tsx:139-153` — convert the 7 `font-mono` boxed rows into a hairline stat grid: `font-label` uppercase labels, ink values, gold accent on the key metric (e.g. frequency), hairline row dividers, drop the boxed `rounded-md ring` look and `font-mono`.

- [ ] **Step 4: Credential counts.** `credential/index.tsx:89-91` — render `Configured (N)` as a small hairline stat (gold numeral + faint label), optionally alongside Available/OAuth counts.

- [ ] **Step 5: Verify gate.** Visual: trigger a route error (bad path) → editorial screen; credential page → provider marks in warm square tiles + hairline count; schedule detail drawer → hairline KPI grid (no mono boxes); light + dark.

- [ ] **Step 6: Commit (when authorized)**

```
feat(ui-webui): editorial error screen, provider tiles, and hairline KPI strips
```

---

## Task 15: Final verification pass

**Files:** none (verification only).

- [ ] **Step 1: Lint.** `pnpm -C frontend lint` → 0 errors.
- [ ] **Step 2: Build.** `pnpm -C frontend build` → clean.
- [ ] **Step 3: Full visual sweep.** `pnpm -C frontend dev`; walk all 9 routes in **light and dark**: chat (empty + reply + inline DAG), schedule (calendar + list + detail drawer), credential (list + provider form), knowledge, subagents, raven-subagents, raven-cron, raven-channels, setup. Confirm: warm axis only (no gray-blue/violet/pure-white/pure-black), single gold accent (+ terracotta for danger), square controls / 12px cards / 16px tiles / pill chips, hairline dividers, flat elevation, correct fonts, toasts + dialogs + drawers on-brand, theme toggle flips everything cleanly.
- [ ] **Step 4: Diff hygiene.** `git diff` — confirm no stray Prettier reformat noise, no removed shims, no committed assets, English-only comments.
- [ ] **Step 5: Commit (when authorized)** — if any final polish edits were made:

```
style(ui-webui): final EverMind reskin polish pass
```

---

## Self-review notes

- **Spec coverage:** §3 token remap → Tasks 2,4; §4 fonts → Task 1; §5 shape → Task 3; §6 accent/status → Tasks 2,5,8; §7 theme activation → Task 4; §8 third-party (prose/React Flow/diff/sonner) → Tasks 6,7,8; §9 signatures (setup/sidebar/empty/headers/KPI/chat/DAG/RouteError/provider) → Tasks 8-14; §10 non-cascading worklist → distributed across Tasks 3,5,7,8,13,14; §13 verification → Task 15. §11 deferred (favicon/dead svgs) intentionally not tasked.
- **Type/name consistency:** brand utilities (`bg-gold`, `text-gold`, `text-gold-olive`, `bg-gold-light`, `gold-foreground`) are defined in Task 2 (`@theme`) and only consumed in later tasks; radius `rounded-xl`=12px / `rounded-2xl`=16px / `rounded-full`=pill fixed in Task 3 and relied on consistently after.
- **Ordering:** foundation (1-3) → theme activation (4) → status (5) → content/third-party (6-8) → signatures (9-14) → verify (15). Each task is independently lint+build-verifiable; theme-dependent third-party tasks (7,8) follow Task 4.
```
