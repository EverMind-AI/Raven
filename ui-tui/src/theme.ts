// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { activeColorTier } from '@hermes/ink'

export interface ThemeColors {
  primary: string
  accent: string
  border: string
  text: string
  muted: string
  completionBg: string
  completionCurrentBg: string
  completionMetaBg: string
  completionMetaCurrentBg: string

  label: string
  ok: string
  error: string
  warn: string

  prompt: string
  sessionLabel: string
  sessionBorder: string

  statusBg: string
  statusFg: string
  statusGood: string
  statusWarn: string
  statusBad: string
  statusCritical: string
  selectionBg: string

  diffAdded: string
  diffRemoved: string
  diffAddedWord: string
  diffRemovedWord: string

  shellDollar: string
}

export interface ThemeBrand {
  name: string
  icon: string
  prompt: string
  welcome: string
  goodbye: string
  tool: string
  helpHeader: string
}

export interface Theme {
  color: ThemeColors
  brand: ThemeBrand
  bannerLogo: string
  bannerHero: string
  // Brand yellow ramp (light → dark), resolved for the active tier. Used for
  // the gradient banner art.
  yellow: readonly string[]
}

export type ColorScheme = 'dark' | 'light'

// ── Color math ───────────────────────────────────────────────────────
//
// Only the helpers the truecolor palettes themselves need. There is NO
// RGB->ANSI conversion at runtime: the reduced-tier palettes below are
// pre-derived literals (see scripts/gen-color-palettes.mjs), so a level-2
// terminal gets curated `ansi256(N)` values instead of chalk's lossy hex
// downsample (which collapsed the dark-green border onto an olive cube cell).

function parseHex(h: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{6})$/i.exec(h)

  if (!m) {
    return null
  }

  const n = parseInt(m[1]!, 16)

  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff]
}

function mix(a: string, b: string, t: number) {
  const pa = parseHex(a)
  const pb = parseHex(b)

  if (!pa || !pb) {
    return a
  }

  const lerp = (i: 0 | 1 | 2) => Math.round(pa[i] + (pb[i] - pa[i]) * t)

  return '#' + ((1 << 24) | (lerp(0) << 16) | (lerp(1) << 8) | lerp(2)).toString(16).slice(1)
}

// ── Brand ────────────────────────────────────────────────────────────

const BRAND: ThemeBrand = {
  name: 'Raven Agent',
  icon: '🐦‍⬛',
  prompt: '❯',
  welcome: 'Type your message or /help for commands.',
  goodbye: 'Goodbye! 🐦‍⬛',
  tool: '┊',
  helpHeader: '(^_^)? Commands'
}

const cleanPromptSymbol = (s: string | undefined, fallback: string) => {
  const cleaned = String(s ?? '')
    .replace(/\s+/g, ' ')
    .trim()

  return cleaned || fallback
}

// ── Brand yellow ramp (gradient logo / 3D shadow) ────────────────────
//
// A brand asset (raven-tui-design-system, "Brand ramp"), ordered light → dark.
// Used for the gradient banner art. The .50/.300/.500/.700/.900 stops are the
// documented title bands (docs/tui-color-problem/title-gradient-table.md);
// other stops are interpolated. The banner only reads the first few entries
// (hero bands) and falls back to the last, so ramp length isn't load-bearing.
//
// Truecolor carries an extra .600 stop for a smoother hero gradient (9 entries:
// [.50,.100,.300,.500,.600,.700,.900,.950,.990]); the reduced 256/16 tiers keep
// the 8-stop set ([.50,.100,.300,.500,.700,.900,.950,.990]) — the doc defines
// no .600 there. Dark and light carry DISTINCT scales at truecolor and 256
// (light re-derived around #B87900, not a dimmed dark scale); 16 is `yellow`.

const YELLOW_RAMP_TC_DARK: readonly string[] = [
  '#fff7c2', // 50
  '#fff0a4', // 100
  '#FFE573', // 300
  '#fbe23f', // 500
  '#e1c405', // 600
  '#c8a900', // 700
  '#8a6d00', // 900
  '#594600', // 950
  '#2d2300' // 990
]

const YELLOW_RAMP_TC_LIGHT: readonly string[] = [
  '#F6DA8B',
  '#EBC76C',
  '#D9A83A',
  '#B87900', // 500
  '#935F00', // 600
  '#935F00', // 700
  '#684300',
  '#432B00',
  '#221600'
]

const YELLOW_RAMP_256_DARK: readonly string[] = [
  'ansi256(229)',
  'ansi256(229)',
  'ansi256(228)',
  'ansi256(220)', // 500
  'ansi256(178)',
  'ansi256(94)',
  'ansi256(58)',
  'ansi256(234)'
]

const YELLOW_RAMP_256_LIGHT: readonly string[] = [
  'ansi256(222)',
  'ansi256(222)',
  'ansi256(179)',
  'ansi256(136)',
  'ansi256(94)',
  'ansi256(58)',
  'ansi256(58)',
  'ansi256(234)'
]

const YELLOW_RAMP_16: readonly string[] = [
  'ansi:yellow',
  'ansi:yellow',
  'ansi:yellow',
  'ansi:yellow',
  'ansi:yellow',
  'ansi:yellow',
  'ansi:yellow',
  'ansi:yellow'
]

function yellowRamp(tier: 0 | 1 | 2 | 3, scheme: ColorScheme): readonly string[] {
  if (tier === 2) {
    return scheme === 'light' ? YELLOW_RAMP_256_LIGHT : YELLOW_RAMP_256_DARK
  }
  if (tier === 1) {
    return YELLOW_RAMP_16
  }
  return scheme === 'light' ? YELLOW_RAMP_TC_LIGHT : YELLOW_RAMP_TC_DARK
}

// ── Tier 3: truecolor (source of truth) ──────────────────────────────

export const DARK_THEME: Theme = {
  color: {
    primary: '#fbe23f',
    accent: '#fbe23f',
    border: '#2d333b',
    text: '#FFF5EA',
    muted: '#858482',
    completionBg: '#000000',
    completionCurrentBg: '#2a260c',
    completionMetaBg: '#080808',
    completionMetaCurrentBg: '#221d08',

    label: '#858482',
    ok: '#3ee07a',
    error: '#ec6a5e',
    warn: '#f5a623',

    prompt: '#fbe23f',
    sessionLabel: '#858482',
    sessionBorder: '#2d333b',

    statusBg: '#000000',
    statusFg: '#999999',
    statusGood: '#3ee07a',
    statusWarn: '#f5a623',
    statusBad: '#ec6a5e',
    statusCritical: '#ec6a5e',
    selectionBg: '#8a6d00',

    diffAdded: '#13260f',
    diffRemoved: '#2a1416',
    diffAddedWord: '#86c957',
    diffRemovedWord: '#f85149',
    shellDollar: '#fbe23f'
  },

  brand: BRAND,

  bannerLogo: '',
  bannerHero: '',
  yellow: YELLOW_RAMP_TC_DARK
}

// Light-terminal palette: darker, higher-contrast values that stay legible on
// white backgrounds. Same shape as DARK_THEME so `fromSkin` still layers on
// top cleanly (#11300).
export const LIGHT_THEME: Theme = {
  color: {
    primary: '#B87900',
    accent: '#B87900',
    border: '#d0d7de',
    text: '#24201a',
    muted: '#57606a',
    completionBg: '#f6f8fa',
    completionCurrentBg: '#fff8e7',
    completionMetaBg: '#eef1f4',
    completionMetaCurrentBg: '#ffefc2',

    label: '#6e7681',
    ok: '#1f7a33',
    error: '#cf222e',
    warn: '#a05500',

    prompt: '#B87900',
    sessionLabel: '#6e7681',
    sessionBorder: '#d0d7de',

    statusBg: '#f6f8fa',
    statusFg: '#57606a',
    statusGood: '#1f7a33',
    statusWarn: '#a05500',
    statusBad: '#cf222e',
    statusCritical: '#cf222e',
    selectionBg: '#ffdf85',

    diffAdded: '#e6f7dd',
    diffRemoved: '#ffe3e0',
    diffAddedWord: '#3f6f1f',
    diffRemovedWord: '#c0282f',
    shellDollar: '#B87900'
  },

  brand: BRAND,

  bannerLogo: '',
  bannerHero: '',
  yellow: YELLOW_RAMP_TC_LIGHT
}

// ── Tier 2: 256-color (per design tokens, docs/tui-color-problem/tokens.md) ──

const DARK_256_COLORS: ThemeColors = {
  primary: 'ansi256(221)',
  accent: 'ansi256(221)',
  border: 'ansi256(236)',
  text: 'ansi256(255)',
  muted: 'ansi256(102)',
  completionBg: 'ansi256(16)',
  completionCurrentBg: 'ansi256(234)',
  completionMetaBg: 'ansi256(232)',
  completionMetaCurrentBg: 'ansi256(234)',
  label: 'ansi256(102)',
  ok: 'ansi256(78)',
  error: 'ansi256(203)',
  warn: 'ansi256(214)',
  prompt: 'ansi256(221)',
  sessionLabel: 'ansi256(102)',
  sessionBorder: 'ansi256(236)',
  statusBg: 'ansi256(16)',
  statusFg: 'ansi256(246)',
  statusGood: 'ansi256(78)',
  statusWarn: 'ansi256(214)',
  statusBad: 'ansi256(203)',
  statusCritical: 'ansi256(203)',
  selectionBg: 'ansi256(58)',
  diffAdded: 'ansi256(234)',
  diffRemoved: 'ansi256(234)',
  diffAddedWord: 'ansi256(107)',
  diffRemovedWord: 'ansi256(203)',
  shellDollar: 'ansi256(221)'
}

const LIGHT_256_COLORS: ThemeColors = {
  primary: 'ansi256(136)',
  accent: 'ansi256(136)',
  border: 'ansi256(188)',
  text: 'ansi256(234)',
  muted: 'ansi256(59)',
  completionBg: 'ansi256(231)',
  completionCurrentBg: 'ansi256(230)',
  completionMetaBg: 'ansi256(255)',
  completionMetaCurrentBg: 'ansi256(229)',
  label: 'ansi256(243)',
  ok: 'ansi256(29)',
  error: 'ansi256(160)',
  warn: 'ansi256(130)',
  prompt: 'ansi256(136)',
  sessionLabel: 'ansi256(243)',
  sessionBorder: 'ansi256(188)',
  statusBg: 'ansi256(231)',
  statusFg: 'ansi256(59)',
  statusGood: 'ansi256(29)',
  statusWarn: 'ansi256(130)',
  statusBad: 'ansi256(160)',
  statusCritical: 'ansi256(160)',
  selectionBg: 'ansi256(222)',
  diffAdded: 'ansi256(194)',
  diffRemoved: 'ansi256(224)',
  diffAddedWord: 'ansi256(64)',
  diffRemovedWord: 'ansi256(124)',
  shellDollar: 'ansi256(136)'
}

// ── Tier 1: 16-color (per design tokens, docs/tui-color-problem/tokens.md) ──
//
// Two caveats vs the token spec, which a single color string can't encode:
//   - `reverse` highlights (completionCurrentBg/completionMetaCurrentBg/
//     selectionBg) fall back to brightBlack — the spec's stated alternative.
//   - statusCritical's `+ bold` is dropped (bold is a text style, not a
//     color), leaving it `red` like the spec's base.

const DARK_16_COLORS: ThemeColors = {
  primary: 'ansi:yellowBright',
  accent: 'ansi:yellowBright',
  border: 'ansi:blackBright',
  text: 'ansi:white',
  muted: 'ansi:blackBright',
  completionBg: 'ansi:black',
  completionCurrentBg: 'ansi:blackBright',
  completionMetaBg: 'ansi:black',
  completionMetaCurrentBg: 'ansi:blackBright',
  label: 'ansi:blackBright',
  ok: 'ansi:greenBright',
  error: 'ansi:redBright',
  warn: 'ansi:yellow',
  prompt: 'ansi:yellowBright',
  sessionLabel: 'ansi:blackBright',
  sessionBorder: 'ansi:blackBright',
  statusBg: 'ansi:black',
  statusFg: 'ansi:blackBright',
  statusGood: 'ansi:greenBright',
  statusWarn: 'ansi:yellow',
  statusBad: 'ansi:redBright',
  statusCritical: 'ansi:red',
  selectionBg: 'ansi:yellow',
  diffAdded: 'ansi:blackBright',
  diffRemoved: 'ansi:blackBright',
  diffAddedWord: 'ansi:green',
  diffRemovedWord: 'ansi:red',
  shellDollar: 'ansi:yellowBright'
}

const LIGHT_16_COLORS: ThemeColors = {
  primary: 'ansi:yellow',
  accent: 'ansi:yellow',
  border: 'ansi:blackBright',
  text: 'ansi:black',
  muted: 'ansi:blackBright',
  completionBg: 'ansi:white',
  completionCurrentBg: 'ansi:blackBright',
  completionMetaBg: 'ansi:white',
  completionMetaCurrentBg: 'ansi:blackBright',
  label: 'ansi:blackBright',
  ok: 'ansi:green',
  error: 'ansi:red',
  warn: 'ansi:yellow',
  prompt: 'ansi:yellow',
  sessionLabel: 'ansi:blackBright',
  sessionBorder: 'ansi:blackBright',
  statusBg: 'ansi:white',
  statusFg: 'ansi:blackBright',
  statusGood: 'ansi:green',
  statusWarn: 'ansi:yellow',
  statusBad: 'ansi:red',
  statusCritical: 'ansi:red',
  selectionBg: 'ansi:magentaBright',
  diffAdded: 'ansi:blackBright',
  diffRemoved: 'ansi:blackBright',
  diffAddedWord: 'ansi:green',
  diffRemovedWord: 'ansi:red',
  shellDollar: 'ansi:yellow'
}

const DARK_256: Theme = { ...DARK_THEME, color: DARK_256_COLORS, yellow: YELLOW_RAMP_256_DARK }
const DARK_16: Theme = { ...DARK_THEME, color: DARK_16_COLORS, yellow: YELLOW_RAMP_16 }
const LIGHT_256: Theme = { ...LIGHT_THEME, color: LIGHT_256_COLORS, yellow: YELLOW_RAMP_256_LIGHT }
const LIGHT_16: Theme = { ...LIGHT_THEME, color: LIGHT_16_COLORS, yellow: YELLOW_RAMP_16 }

/**
 * Pick the palette for a scheme + color tier. Tier 3 (truecolor) and tier 0
 * (no color — chalk strips the codes anyway) both use the hex palette, so the
 * truecolor `Theme` reference is returned unchanged for identity checks.
 */
export function resolveTheme(scheme: ColorScheme, tier: 0 | 1 | 2 | 3): Theme {
  if (scheme === 'light') {
    if (tier === 2) {
      return LIGHT_256
    }
    if (tier === 1) {
      return LIGHT_16
    }
    return LIGHT_THEME
  }

  if (tier === 2) {
    return DARK_256
  }
  if (tier === 1) {
    return DARK_16
  }
  return DARK_THEME
}

// ── Light/dark detection ─────────────────────────────────────────────

const TRUE_RE = /^(?:1|true|yes|on)$/
const FALSE_RE = /^(?:0|false|no|off)$/

// TERM_PROGRAM fallback allow-list for terminals whose default profile is
// light and which may not expose COLORFGBG. Empty by default: a TERM_PROGRAM
// alone can't tell a light profile from a dark one (Terminal.app ships both
// and emits no COLORFGBG either way), and dark profiles are common, so an
// undetectable terminal stays dark unless an explicit signal (RAVEN_TUI_THEME
// / RAVEN_TUI_LIGHT / RAVEN_TUI_BACKGROUND / COLORFGBG) says light. Still
// injectable so tests can exercise the precedence rules.
const LIGHT_DEFAULT_TERM_PROGRAMS = new Set<string>([])

// Best-effort RGB → luminance check.  Currently only accepts a 3- or
// 6-digit hex value (with or without a leading `#`); the env var name
// `RAVEN_TUI_BACKGROUND` is intentionally generic so a future OSC11
// query helper can cache its answer there too, but additional formats
// (rgb()/hsl()/named colours) would need explicit parsing here first.
const LUMA_LIGHT_THRESHOLD = 0.6

// Strict allow-list: parseInt(..., 16) silently truncates at the first
// non-hex character (e.g. `fffgff` would parse as `fff` and yield a
// false-positive "white" reading), so reject anything that doesn't match
// the canonical 3- or 6-digit shape up front.
const HEX_3_RE = /^[0-9a-f]{3}$/
const HEX_6_RE = /^[0-9a-f]{6}$/

function backgroundLuminance(raw: string): null | number {
  const v = raw.trim().toLowerCase()

  if (!v) {
    return null
  }

  const hex = v.startsWith('#') ? v.slice(1) : v

  const rgb = HEX_6_RE.test(hex)
    ? [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)]
    : HEX_3_RE.test(hex)
      ? [parseInt(hex[0]! + hex[0]!, 16), parseInt(hex[1]! + hex[1]!, 16), parseInt(hex[2]! + hex[2]!, 16)]
      : null

  if (!rgb) {
    return null
  }

  // Rec. 709 luma — close enough for "is this background bright".
  return (0.2126 * rgb[0]! + 0.7152 * rgb[1]! + 0.0722 * rgb[2]!) / 255
}

// Pick light vs dark with ordered, explainable signals (#11300):
//
//   1. `RAVEN_TUI_LIGHT` boolean — `1`/`true`/`yes`/`on` → light;
//      `0`/`false`/`no`/`off` → dark.  Either explicit value wins
//      regardless of any later signal.
//   2. `RAVEN_TUI_THEME` named override — `light` / `dark` win over
//      every signal below.
//   3. `RAVEN_TUI_BACKGROUND` hex hint (3- or 6-digit) — luminance
//      ≥ LUMA_LIGHT_THRESHOLD → light.
//   4. `COLORFGBG` last field — XFCE / rxvt / Terminal.app emit
//      slot 7 or 15 on light profiles; 0–15 ranges are otherwise
//      treated as authoritatively dark so the TERM_PROGRAM
//      allow-list below cannot override an explicit dark profile.
//   5. `TERM_PROGRAM` light-default allow-list (empty by default; see
//      LIGHT_DEFAULT_TERM_PROGRAMS).
//
// Anything we can't decide stays dark — the default Raven palette
// is the dark one.
export function detectLightMode(
  env: NodeJS.ProcessEnv = process.env,
  // Injectable so tests can prove the COLORFGBG-over-TERM_PROGRAM
  // precedence rule even though the production allow-list is empty.
  lightDefaultTermPrograms: ReadonlySet<string> = LIGHT_DEFAULT_TERM_PROGRAMS
): boolean {
  const lightFlag = (env.RAVEN_TUI_LIGHT ?? '').trim().toLowerCase()

  if (TRUE_RE.test(lightFlag)) {
    return true
  }

  if (FALSE_RE.test(lightFlag)) {
    return false
  }

  const themeFlag = (env.RAVEN_TUI_THEME ?? '').trim().toLowerCase()

  if (themeFlag === 'light') {
    return true
  }

  if (themeFlag === 'dark') {
    return false
  }

  const bgHint = backgroundLuminance(env.RAVEN_TUI_BACKGROUND ?? '')

  if (bgHint !== null) {
    return bgHint >= LUMA_LIGHT_THRESHOLD
  }

  const colorfgbg = (env.COLORFGBG ?? '').trim()

  if (colorfgbg) {
    // Validate as a decimal integer before coercing — `Number('')` is 0,
    // so a malformed `COLORFGBG='15;'` would otherwise look like an
    // authoritative dark slot and incorrectly block the TERM_PROGRAM
    // allow-list.  Anything that isn't pure digits falls through.
    const lastField = colorfgbg.split(';').at(-1) ?? ''

    if (/^\d+$/.test(lastField)) {
      const bg = Number(lastField)

      if (bg === 7 || bg === 15) {
        return true
      }

      // Slots 0–6 and 8–14 are the dark half of the 0–15 ANSI range.
      // When COLORFGBG is set we trust it as authoritative — a non-light
      // value here shouldn't get overridden by the TERM_PROGRAM allow-list.
      if (bg >= 0 && bg < 16) {
        return false
      }
    }
  }

  const termProgram = (env.TERM_PROGRAM ?? '').trim()

  return lightDefaultTermPrograms.has(termProgram)
}

const DEFAULT_LIGHT_MODE = detectLightMode()
const DEFAULT_SCHEME: ColorScheme = DEFAULT_LIGHT_MODE ? 'light' : 'dark'

export const DEFAULT_THEME: Theme = resolveTheme(DEFAULT_SCHEME, activeColorTier())

// Scheme detected at runtime by the OSC 11 background-color probe (see
// applyDetectedBackground). Null until — or unless — the terminal answers the
// query. When set it wins over the env-sniffed DEFAULT_SCHEME for every theme
// built afterwards, so a late reply re-themes the whole app.
let detectedScheme: ColorScheme | null = null

/** Effective light/dark scheme: the OSC 11 probe result if we have one, else
 *  the env-sniffed default. Theme builders read this (not DEFAULT_SCHEME) so a
 *  late probe reply takes effect when the theme is rebuilt. */
export function currentScheme(): ColorScheme {
  return detectedScheme ?? DEFAULT_SCHEME
}

/** Curated per-scheme palette for the current scheme + color tier, with no
 *  skin applied. Used to rebuild the theme when the probe flips the scheme
 *  before any gateway skin has arrived. */
export function resolveCurrentDefaultTheme(): Theme {
  return resolveTheme(currentScheme(), activeColorTier())
}

/** Truecolor hex for the OSC 12 hardware-cursor color. OSC 12 takes an RGB
 *  value, so we use the hex primary regardless of the text color tier — a
 *  256/16 terminal still renders its cursor in truecolor. A skin's hex primary
 *  (tier 3) is honored; otherwise the curated per-scheme title color. */
export function cursorColorHex(theme: Theme): string {
  const p = theme.color.primary

  return p.startsWith('#') ? p : currentScheme() === 'light' ? LIGHT_THEME.color.primary : DARK_THEME.color.primary
}

// Parse an OSC 11 background reply payload into a #rrggbb hex string.
// xterm-class terminals answer `rgb:RRRR/GGGG/BBBB` (1-4 hex digits per
// channel, scaled to that channel's max); a few reply `#RRGGBB`. Anything
// else returns null so the caller keeps the env-based scheme.
function oscColorToHex(data: string): null | string {
  const s = data.trim().toLowerCase()
  const m = /^rgba?:([0-9a-f]{1,4})\/([0-9a-f]{1,4})\/([0-9a-f]{1,4})/.exec(s)

  if (m) {
    const scale = (h: string) => Math.round((parseInt(h, 16) / (16 ** h.length - 1)) * 255)

    return '#' + [m[1]!, m[2]!, m[3]!].map(h => scale(h).toString(16).padStart(2, '0')).join('')
  }

  const hex = s.startsWith('#') ? s.slice(1) : s

  if (HEX_6_RE.test(hex)) {
    return '#' + hex
  }

  if (HEX_3_RE.test(hex)) {
    return '#' + [...hex].map(c => c + c).join('')
  }

  return null
}

/**
 * Fold an OSC 11 background-color reply into light/dark detection.
 *
 * Caches the parsed color into RAVEN_TUI_BACKGROUND and re-runs
 * detectLightMode() so the existing precedence rules apply unchanged — an
 * explicit RAVEN_TUI_THEME / RAVEN_TUI_LIGHT still wins over the measured
 * background. Returns the resolved scheme and whether it differs from the
 * scheme that was in effect (so the caller knows whether to re-theme), or
 * null when the reply isn't a color we can parse.
 */
export function applyDetectedBackground(oscData: string): { changed: boolean; scheme: ColorScheme } | null {
  const hex = oscColorToHex(oscData)

  if (!hex) {
    return null
  }

  process.env.RAVEN_TUI_BACKGROUND = hex
  const scheme: ColorScheme = detectLightMode() ? 'light' : 'dark'
  const changed = scheme !== currentScheme()
  detectedScheme = scheme

  return { changed, scheme }
}

// ── Skin → Theme ─────────────────────────────────────────────────────

function skinColors(colors: Record<string, string>): ThemeColors {
  const base = (currentScheme() === 'light' ? LIGHT_THEME : DARK_THEME).color
  const c = (k: string) => colors[k]
  const hasSkinColors = Object.keys(colors).length > 0

  const accent = c('ui_accent') ?? c('banner_accent') ?? base.accent
  const bannerAccent = c('banner_accent') ?? c('banner_title') ?? base.accent
  const muted = c('banner_dim') ?? base.muted
  const completionBg = c('completion_menu_bg') ?? base.completionBg

  const completionCurrentBg =
    c('completion_menu_current_bg') ??
    (hasSkinColors ? mix(completionBg, bannerAccent, 0.25) : base.completionCurrentBg)

  // Meta columns cascade off the matching skin key, then fall back to the
  // palette's own (distinct) meta value — so an empty skin reproduces the
  // default theme exactly while a skin that sets only the main completion bg
  // still carries it into the meta column.
  const completionMetaBg = c('completion_menu_meta_bg') ?? c('completion_menu_bg') ?? base.completionMetaBg
  const completionMetaCurrentBg =
    c('completion_menu_meta_current_bg') ??
    c('completion_menu_current_bg') ??
    (hasSkinColors ? completionCurrentBg : base.completionMetaCurrentBg)

  return {
    primary: c('ui_primary') ?? c('banner_title') ?? base.primary,
    accent,
    border: c('ui_border') ?? c('banner_border') ?? base.border,
    text: c('ui_text') ?? c('banner_text') ?? base.text,
    muted,
    completionBg,
    completionCurrentBg,
    completionMetaBg,
    completionMetaCurrentBg,

    label: c('ui_label') ?? base.label,
    ok: c('ui_ok') ?? base.ok,
    error: c('ui_error') ?? base.error,
    warn: c('ui_warn') ?? base.warn,

    prompt: c('prompt') ?? c('banner_text') ?? base.prompt,
    sessionLabel: c('session_label') ?? base.sessionLabel,
    sessionBorder: c('session_border') ?? base.sessionBorder,

    statusBg: base.statusBg,
    statusFg: base.statusFg,
    statusGood: c('ui_ok') ?? base.statusGood,
    statusWarn: c('ui_warn') ?? base.statusWarn,
    statusBad: base.statusBad,
    statusCritical: base.statusCritical,
    selectionBg:
      c('selection_bg') ?? c('completion_menu_current_bg') ?? (hasSkinColors ? completionCurrentBg : base.selectionBg),

    diffAdded: base.diffAdded,
    diffRemoved: base.diffRemoved,
    diffAddedWord: base.diffAddedWord,
    diffRemovedWord: base.diffRemovedWord,
    shellDollar: c('shell_dollar') ?? base.shellDollar
  }
}

export function fromSkin(
  colors: Record<string, string>,
  branding: Record<string, string>,
  bannerLogo = '',
  bannerHero = '',
  toolPrefix = '',
  helpHeader = ''
): Theme {
  const d = DEFAULT_THEME

  const brand: ThemeBrand = {
    name: branding.agent_name ?? d.brand.name,
    icon: d.brand.icon,
    prompt: cleanPromptSymbol(branding.prompt_symbol, d.brand.prompt),
    welcome: branding.welcome ?? d.brand.welcome,
    goodbye: branding.goodbye ?? d.brand.goodbye,
    tool: toolPrefix || d.brand.tool,
    helpHeader: branding.help_header ?? (helpHeader || d.brand.helpHeader)
  }

  // Skins are authored in truecolor hex. The reduced tiers can't represent
  // arbitrary hex, so fall back to the curated built-in palette for that tier
  // (per product decision) — only branding + banner art carry over. The hex
  // path covers tier 3 and, harmlessly, tier 0 (codes are stripped anyway).
  const tier = activeColorTier()
  const color = tier === 1 || tier === 2 ? resolveTheme(currentScheme(), tier).color : skinColors(colors)

  return { color, brand, bannerLogo, bannerHero, yellow: yellowRamp(tier, currentScheme()) }
}
