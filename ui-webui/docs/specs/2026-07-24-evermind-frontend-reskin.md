# EverMind reskin of `ui-webui/frontend` — design

- Date: 2026-07-24
- Status: proposed (awaiting spec review)
- Scope: `ui-webui/frontend` (the whole SPA) — visual/design-language only. No behavior,
  routing, data, or API changes. Backend/service/BFF untouched.
- Goal: restyle the web app from its current neutral-gray + violet + Geist + rounded look
  to the EverMind design language (warm parchment/ink/gold, Atkinson + Cormorant + Bungee +
  Brygada, square-by-default geometry, hairline dividers, quiet whitespace), applied as a
  token backbone plus a bounded set of signature moments.
- Design language reference: `.claude/skills/evermind-web-design/SKILL.md` +
  `references/tokens.css` (canonical palette/type/shape values).

---

## 1. Context — current state (from survey)

The frontend is **~95% token-driven**, which makes a token remap the dominant lever:

- **Zero hardcoded hex/rgb/oklch in any `.tsx`.** Every component uses semantic shadcn
  Tailwind classes (`bg-background`, `text-muted-foreground`, `border-border`, `bg-primary`,
  `rounded-md`, …). The only real stylesheet is `src/index.css`; the only inline color is
  `TourCard.tsx` using `var(--card)` (itself a token).
- **All design tokens live in `src/index.css`** as shadcn oklch neutral-gray variables plus a
  `--radius` scale and `@theme --font-sans`. The "violet accent" (`--accent-bg/-border`
  `rgba(170,59,255)`, dark `--sidebar-primary` oklch violet) is **dead** — no consumers. The
  real accent today is `--primary` = near-black ink.
- **Legacy vars are dead:** `--text`, `--text-h`, `--bg`, `--code-bg`, `--accent-bg/-border`,
  `--social-bg`, `--shadow`, `--sans/--heading/--mono`, and the `#social` rule have no
  consumers. Safe to delete.
- **Dark mode is 100% dead code.** There is no `next-themes` `ThemeProvider` mounted anywhere;
  `useTheme()` runs provider-less, nothing toggles `.dark` on `<html>`, so the entire `.dark{}`
  block and all 69 `dark:` variants never fire. The app is light-only today.
- **Radius is a single `--radius` multiplier** (`sm=0.6x … 4xl=2.6x`) — it cannot express
  EverMind's mixed 0 / 12 / 16 / 999 system without being decoupled.
- **Fonts:** Geist Variable via `@fontsource-variable/geist`. All four EverMind faces are
  available offline as `@fontsource` packages at 5.3.0 (verified via `npm view`).
- **Non-token surfaces** (enumerated in §10): Tailwind `prose`, React Flow `--xy-*`,
  react-diff-view, sonner `richColors`, traffic-light status literals, a few `bg-white`/
  `bg-black/10` and hardcoded radius literals.

Route/page inventory (9): `chat` (primary), `schedule`, `credential`, `knowledge`,
`subagents`, `raven-subagents`, `raven-cron`, `raven-channels`, `setup` (full-screen onboarding).

## 2. Decisions

Scoping (confirmed with user):

- **Depth:** token remap backbone + EverMind signature moments (not a pure repaint, not a
  full per-page redesign).
- **Scope:** whole app (shell + all 9 pages).
- **Themes:** both light + dark, mapped to EverMind (light = parchment, dark = ink). Requires
  activating the dormant theme system.
- **Fonts:** offline via `@fontsource` npm packages (matches offline-first + AGENTS.md §7).

Brand forks (confirmed with user):

- **Accent strategy — ink primary + gold accent.** `--primary` = ink (primary buttons become
  the EverMind ink pill with cream text); **gold is the single accent**, applied to focus rings,
  active nav pill, checked switch/checkbox, calendar selection, tabs underline, and
  running/active status via a dedicated gold token + a bounded set of targeted component edits.
- **Status/danger — gold/ink + one warm red.** running/active = gold (+ shimmer/glow),
  completed = muted olive/ink, failed/danger = one warm-tuned terracotta red (the sole
  sanctioned second hue, for legibility). Diffs: additions warm-olive, deletions warm-red.
- **Provider icons — brand logos in warm tiles.** Keep recognizable `@lobehub/icons` brand
  marks, wrapped in a 16px warm/pastel square icon tile.

## 3. Foundation — `index.css` token remap (backbone)

Remap the shadcn light block (`:root`) and author the `.dark` block to the warm axis. Target
values (from `references/tokens.css`):

| shadcn token | light | dark |
|---|---|---|
| `--background` | canvas `#FCFCF5` | ink-deep `#050505` |
| `--card` / `--popover` | panel `#F2F1E9` | ink `#1C1B1B` |
| `--card-foreground` / `--popover-foreground` | ink `#1C1B1B` | cream `#F2F1E9` |
| `--foreground` | ink `#1C1B1B` | cream `#F2F1E9` |
| `--primary` | ink `#1C1B1B` | cream `#F2F1E9` |
| `--primary-foreground` | cream `#F2F1E9` | ink `#1C1B1B` |
| `--secondary` / `--secondary-foreground` | panel `#F2F1E9` / ink | warm-ink `#2A2724` / cream |
| `--muted` / `--accent` (hover surface) | panel-warm `#EBE8DD` | warm-ink `#2A2724` |
| `--muted-foreground` | taupe `#70695A` | faint `#ABABA2` |
| `--accent-foreground` | ink `#1C1B1B` | cream `#F2F1E9` |
| `--border` / `--input` | hairline `rgba(49,48,46,.08)` | `rgba(242,241,233,.10)` |
| `--ring` | **gold `#D99400`** | gold `#D99400` |
| `--destructive` | warm terracotta (tuned) | warm terracotta (tuned) |
| `--sidebar` | panel `#F2F1E9` | ink `#1C1B1B` |
| `--sidebar-foreground` | ink | cream |
| `--sidebar-primary` | ink (fix dark violet leftover) | cream |
| `--sidebar-accent` | gold-tint pill bg | gold-tint |
| `--sidebar-border` | hairline | hairline |

New brand tokens (added to `:root`/`.dark` and exposed via `@theme` where needed):

- `--gold: #D99400`, `--gold-light: #F7CB72`, `--gold-olive: #9C7C1E`,
  `--glow: rgba(252,191,78,.58)`.
- `--gold-foreground` (ink) for text on gold fills.

Cleanup in the same pass:

- Delete dead legacy vars + the `#social` rule + the legacy `@media (prefers-color-scheme)` block.
- Remove global `letter-spacing: 0.18px` (`index.css:34`); drop the `:root { color; background }`
  off-axis defaults (`:root` lines 36-37) in favor of the `body @apply` tokens.

## 4. Typography

- Add `@fontsource-variable/atkinson-hyperlegible-next`, `@fontsource-variable/cormorant-garamond`,
  `@fontsource/bungee-shade`, `@fontsource-variable/brygada-1918` via `pnpm add`. Remove
  `@fontsource-variable/geist`.
- `@import` the four packages in `index.css`; replace the Geist import.
- `@theme`: `--font-sans` → `"Atkinson Hyperlegible Next", "Atkinson Hyperlegible", sans-serif`;
  keep `--font-heading` distinct (same family, reserved for the tight-tracking treatment); add
  `--font-serif` (Cormorant Garamond), `--font-display` (Bungee Shade), `--font-label`
  (Brygada 1918). Optionally add an explicit `--font-mono` token (currently missing; mono chips
  fall back to the default stack — acceptable, EverMind prescribes no mono).
- Heading rules: weight **500**; large sizes get `-0.05em` tracking. A reusable `.em-accent`
  utility (italic Cormorant, gold-olive) for the one accent word in mixed-typeface headlines.

## 5. Shape system — decouple the radius scale

Replace the single-`--radius`-multiplier `@theme` block with deliberate values:

- **Square (0):** default/`sm`/`md`/`lg` → controls (buttons, inputs, selects, textareas, tabs,
  dropdown/select rows, tool boxes).
- **Card 12px:** a fixed `--radius-card` (map `xl`) → cards, dialogs, popovers, sheets, drawers,
  confirm cards, config record cards.
- **Tile 16px:** a fixed `--radius-tile` (map `2xl`) → icon tiles.
- **Pill 999px:** pin `badge.tsx` to `rounded-full`; gold active nav pill; keep switch tracks,
  drawer/resizable handles, status dots, and progress tracks round.

Hardcoded radius literals to fix manually (won't follow the token change) — see §10.

Elevation: replace `shadow-sm/md/lg` (popover, select, dropdown, sheet, sidebar, tabs active,
DAG node, schedule card) with hairline borders (`ring-1 ring-foreground/10` style). Ink-tint the
`bg-black/10` overlays (dialog/sheet/drawer) to an ink scrim.

## 6. Accent & status color language

- **Ink primary buttons, gold accent.** `--primary` = ink handles primary buttons. Gold accent
  is applied through `--ring` (focus glow everywhere) plus targeted edits on selection controls:
  switch checked track, checkbox checked, calendar `today`/selected, `tabs` line-variant active
  underline (`after:bg-foreground` → gold), active sidebar nav pill.
- **Status system (single warm axis + one red):**
  - running/active → gold (`#D99400`) + gold shimmer/glow
  - completed/done → muted olive / ink (filled, differentiated by weight not hue)
  - failed/error/danger → warm terracotta `--destructive`
  - pending → faint hairline; skipped → dashed hairline at reduced opacity
- **Diffs:** additions → warm-olive tint, deletions → warm-red tint (replaces
  `emerald-500/10` / `red-500/10`); gutter markers follow.
- Reconcile every traffic-light literal (§10) to this system in lockstep so the whole app reads
  one restrained gold+ink(+red) status language.

## 7. Theme activation (light + dark)

Dark is currently dead — this is net-new wiring, not a repaint:

- Mount `next-themes` `ThemeProvider` (`attribute="class"`, `defaultTheme="light"`,
  `enableSystem={false}` to avoid the OS-desync the legacy path had) in `main.tsx`/`App.tsx`.
- Add a **theme toggle** to the sidebar footer (`AppSidebar.tsx`), Solar sun/moon duotone icon,
  consistent with the existing rail-icon treatment.
- Author all `.dark` token values (§3) for ink surfaces + gold accent + cream text.
- Reconcile third-party theme coupling: `DagGraph` `colorMode` and `sonner` `theme` should read
  the provider's resolved theme.
- Remove the legacy `@media (prefers-color-scheme: dark)` token path so there is a single
  class-based theme source of truth.

## 8. Third-party surfaces (own CSS — must be themed by hand)

1. **Tailwind Typography `prose`** (chat assistant markdown — the biggest content surface;
   currently gray defaults, near-invisible in dark). Add `--tw-prose-*` overrides mapping
   body/headings/links/bold/code/pre-bg/pre-code/quotes/borders/th-borders to warm tokens, with
   a dark variant. **High priority.**
2. **React Flow `--xy-*`** (inline DAG canvas). Add a `.react-flow` override block: edge stroke,
   animated running edge → gold + glow, background dot color, controls button bg/border/radius
   (square + hairline, drop shadow), minimap bg/mask/node fill, selection ring → gold glow, dark
   pane bg → ink panel, handle border.
3. **react-diff-view** base CSS: warm the insert/delete cell tints + selection.
4. **sonner**: drop `richColors` in `App.tsx`; wire the currently-dead token-aware wrapper
   (`ui/sonner.tsx`) or override sonner's `--normal-*` / `--success-*` / `--error-*` vars →
   flat warm panels with a gold accent bar (success/info) and terracotta (error).

## 9. Signature moments (character layer)

- **Setup screen** (`pages/setup`, full-screen route) — marquee: **dark starfield hero** (CSS
  radial-gradient stars, no asset — honors AGENTS.md §7), **Bungee Shade `RAVEN` wordmark** (the
  one allowed wordmark), gold eyebrow (`SETUP · CONNECT`), mixed-typeface headline, the two
  existing fields on canvas with hairline dividers (drop the plain Card box).
- **Sidebar** — gold **active pill** nav chip (`sidebar.tsx` active state + `--sidebar-accent`);
  warm panel rail + single hairline right border; hairline divider between the two nav groups;
  unify the nav-icon gold literal `#e3a43b` → brand `#D99400` and ink `#141414` → `#1C1B1B`
  (`index.css` `.app-nav-rail` block).
- **Editorial empty states** — one change to shared `ui/empty.tsx` (+ `chat/Empty`, `PanelEmpty`)
  re-skins ~10 surfaces: pastel-gold 16px icon tile, gold bracketed kicker
  (`[ no channels yet ]`), a mixed-typeface line, dashed box → hairline/frameless.
- **Page headers** (channels, cron, subagents, credential, knowledge, schedule, subagent) — gold
  eyebrow / `[ kicker ]` + one italic-Cormorant accent word in the H1, weight 500.
- **Hairline KPI/stat strips** where data already exists: schedule detail drawer (7 mono rows →
  Brygada labels + ink values + gold key metric), credential counts, DAG summary (parsed in
  `deriveDag` but never rendered), task/permission panel counts, chat message stat micro-row.
- **Pill chips** — `badge` → true pills for status/tags; top-bar model/permission/panel chips as
  a pill cluster; subagent presets as pill chips.
- **Chat** — squared editorial composer separated from the transcript by a hairline top divider,
  with the send button as the single gold-fill accent (attach stays ghost); assistant messages
  flat + hairline (keep the existing transparent full-width bubble); tool/code boxes square +
  hairline; gold "working" shimmer via `--shimmer-color`.
- **DAG** — single-gold status system (§6), gold animated running edge, square hairline nodes,
  optional hairline KPI summary strip from the parsed summary.
- **RouteError** — editorial full-screen error: gold `[ error ]` kicker, mixed-typeface headline,
  detail `<pre>` as a hairline panel-warm block, square recovery buttons.
- **ProviderIcon** — keep `@lobehub` brand marks, wrap each in a 16px warm square icon tile.

## 10. Non-cascading manual worklist (must-touch, from survey)

Everything below will NOT follow the token remap and needs an explicit edit.

| Area | File(s) | Item |
|---|---|---|
| Radius scale | `src/index.css` (`@theme`) | decouple single-`--radius` → square/12/16/pill |
| Radius literals | `ui/badge.tsx` | `rounded-4xl` → `rounded-full` (pin pill) |
| Radius literals | `ui/checkbox.tsx`, `ui/tooltip.tsx` | `rounded-[4px]`, arrow `rounded-[2px]` |
| Radius literals | `ui/button.tsx`, `ui/toggle.tsx`, `ui/select.tsx` | `rounded-[min(var(--radius-md),N)]` caps |
| Radius literals | `ui/input-group.tsx` | `rounded-[calc(var(--radius)-Npx)]` offsets |
| Radius literals | `pages/schedule/index.tsx` | `rounded-t-3xl` → 12/16; bare `rounded` items |
| Radius literals | `pages/subagent/index.tsx` | bare `rounded` on select/skeleton/panel |
| White/black | `chat/ConfirmCard.tsx` | `bg-white` inner box → token |
| White/black | `pages/schedule/index.tsx` | `bg-white` sheet → `bg-card`/panel |
| White/black | `ui/dialog.tsx`, `ui/sheet.tsx`, `ui/drawer.tsx` | `bg-black/10` overlay → ink scrim |
| Status literals | `dag/DagGraph.tsx` | `STATUS_STYLE` blue/emerald/red + error pre |
| Status literals | `chat/tool-renderers/_shared.tsx` | tool ok/fail emerald/red (×4) |
| Status literals | `chat/tool-renderers/DiffPreview.tsx` | insert/delete emerald/red + gutters |
| Status literals | `chat/WorkingDirectoryControl.tsx` | invalid-path `border/text-red-500` |
| Status literals | `badge/StatusBadge.tsx` | completed green / failed red |
| Status literals | `knowledge/KnowledgeDocumentsPanel.tsx` | `ready` emerald |
| Status literals | `panel/McpPanel.tsx` | health dot green/red |
| Status literals | `subagent/SubagentInstanceMonitor.tsx` | green/red/blue glyphs + unicode chars |
| Native controls | `pages/subagent/index.tsx` | native checkbox (`accent-color`) + native selects |
| Fonts | `src/index.css`, `package.json` | swap Geist → 4 EverMind faces + `@theme` |
| Nav icons | `src/index.css` `.app-nav-rail` | gold `#e3a43b`→`#D99400`, ink `#141414`→`#1C1B1B` |
| Theme | `main.tsx`/`App.tsx`, `AppSidebar.tsx` | mount ThemeProvider + toggle + author `.dark` |
| Third-party | `dag/DagGraph.tsx` (`--xy-*`) | React Flow canvas override block |
| Third-party | `chat/MessageBubble.tsx` + `index.css` | `prose` → warm token mapping (+dark) |
| Third-party | `chat/tool-renderers/DiffPreview.tsx` | react-diff-view base tints |
| Third-party | `App.tsx`, `ui/sonner.tsx` | drop `richColors`, wire warm toasts |
| Tour | `App.tsx` (Onborda), `tour/TourCard.tsx` | ink scrim `shadowRgb`; verify `var(--card)` arrow |
| Icons | `ProviderIcon.tsx` | brand marks in 16px warm tiles |
| Shadows | `pages/schedule/schedule-card.tsx` | `hover:shadow-md` → gold hairline hover |

## 11. Out of scope / deferred

- `favicon.svg` re-brand and dead `line-corner.svg` / `line-vertical.svg` removal (avoid asset
  churn under AGENTS.md §7; favicon is optional/deferred).
- No new committed image/SVG/font assets (starfield is CSS; fonts are npm deps in `node_modules`).
- No layout/IA/behavior changes; i18n strings edited only if a header adds copy (targeted edits
  per ui-webui CLAUDE.md, never `json.dump`).

## 12. Constraints & conventions

- AGENTS.md: English-only comments/commits; §7 no committed assets > rules; commit only when the
  user says so (this spec is written but not committed).
- ui-webui CLAUDE.md: pnpm only; Prettier tabs/width-4/single-quotes/semis/print-100; keep `@`
  alias + `path`/`next/navigation` shims; i18n targeted edits; React Flow memo-on-signature; the
  sidebar logo and favicon are independent assets.
- Branch: implementation should be cut from a confirmed base (default `main`) per AGENTS.md §2.2
  — to be confirmed at the writing-plans/execution boundary, not during spec authoring.

## 13. Verification

- `pnpm -C frontend lint` — 0 errors (pre-existing warnings ok).
- `pnpm -C frontend build` — `tsc -b` + `vite build` clean.
- Visual pass across all 9 routes in **both** light and dark, including: chat empty + a live
  reply (prose), an inline DAG run, a config page with records + empty state, the setup hero,
  toasts, and a dialog/drawer.

## 14. Suggested phasing (for the implementation plan)

1. **Foundation** — fonts + token remap (light) + radius decoupling + dead-code cleanup. Biggest
   visible shift, lowest risk; most of the app reskins here.
2. **Theme activation** — provider + toggle + `.dark` tokens + third-party theme coupling.
3. **Non-cascading fixes** — status color language, overlays, radius literals, native controls.
4. **Third-party surfaces** — prose, React Flow, react-diff-view, sonner.
5. **Signature moments** — setup hero, sidebar pill nav, empty states, page eyebrows, KPI strips,
   chat composer, DAG, RouteError, provider tiles.
6. **Verify** — lint + build + visual pass (light + dark).
