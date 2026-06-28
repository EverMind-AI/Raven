#!/usr/bin/env node
// One-off generator for the reduced-tier color palettes in src/theme.ts.
//
// The truecolor (tier 3) palettes are the source of truth, hand-authored as
// hex in theme.ts. The 256-color (tier 2) and 16-color (tier 1) palettes are
// DERIVED from them by this script and FROZEN as literals in theme.ts — so no
// RGB->ANSI conversion runs at app start-up. Re-run this and paste the output
// when the truecolor palette changes:
//
//   node scripts/gen-color-palettes.mjs
//
// 256-color mapping uses a hue-preserving algorithm (the same one hermes-ink
// historically used for legacy Apple Terminal) so dark greens stay green
// instead of collapsing onto the olive cube cell that chalk's naive
// rgbToAnsi256 picks. 16-color mapping is nearest-of-16 by RGB distance.

// --- truecolor source palettes (keep in sync with theme.ts) ---------------

const DARK = {
  primary: '#7CC950',
  accent: '#9ED66E',
  border: '#4F7A45',
  text: '#E6F2DD',
  muted: '#6FA05C',
  completionBg: '#273328',
  completionCurrentBg: '#3C5A3D',
  completionMetaBg: '#273328',
  completionMetaCurrentBg: '#3C5A3D',
  label: '#8FBF6B',
  ok: '#4caf50',
  error: '#ef5350',
  warn: '#ffa726',
  prompt: '#E6F2DD',
  sessionLabel: '#6FA05C',
  sessionBorder: '#6FA05C',
  statusBg: '#273328',
  statusFg: '#C8D6BF',
  statusGood: '#8FBC8F',
  statusWarn: '#FFD700',
  statusBad: '#FF8C00',
  statusCritical: '#FF6B6B',
  selectionBg: '#3C5A3D',
  diffAdded: 'rgb(220,255,220)',
  diffRemoved: 'rgb(255,220,220)',
  diffAddedWord: 'rgb(36,138,61)',
  diffRemovedWord: 'rgb(207,34,46)',
  shellDollar: '#4dabf7'
}

const LIGHT = {
  primary: '#2E6B2E',
  accent: '#3A7D3A',
  border: '#2F5F2F',
  text: '#273328',
  muted: '#3F6B3F',
  completionBg: '#EDF3EA',
  completionCurrentBg: '#C9DCC1',
  completionMetaBg: '#EDF3EA',
  completionMetaCurrentBg: '#C9DCC1',
  label: '#3F6B3F',
  ok: '#2E7D32',
  error: '#C62828',
  warn: '#E65100',
  prompt: '#273328',
  sessionLabel: '#3F6B3F',
  sessionBorder: '#3F6B3F',
  statusBg: '#EDF3EA',
  statusFg: '#2A3A2A',
  statusGood: '#2E7D32',
  statusWarn: '#8B6914',
  statusBad: '#D84315',
  statusCritical: '#B71C1C',
  selectionBg: '#CBE3C0',
  diffAdded: 'rgb(200,240,200)',
  diffRemoved: 'rgb(240,200,200)',
  diffAddedWord: 'rgb(27,94,32)',
  diffRemovedWord: 'rgb(183,28,28)',
  shellDollar: '#1565C0'
}

// --- parsing ---------------------------------------------------------------

function toRgb(value) {
  const hex = /^#([0-9a-f]{6})$/i.exec(value)
  if (hex) {
    const n = Number.parseInt(hex[1], 16)
    return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff]
  }
  const rgb = /^rgb\(\s?(\d+),\s?(\d+),\s?(\d+)\s?\)$/.exec(value)
  if (rgb) {
    return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])]
  }
  throw new Error(`unparseable color: ${value}`)
}

// --- 256-color (hue preserving) -------------------------------------------

function ansi256(red, green, blue) {
  const rn = red / 255
  const gn = green / 255
  const bn = blue / 255
  const max = Math.max(rn, gn, bn)
  const min = Math.min(rn, gn, bn)
  const lightness = (max + min) / 2
  const saturation =
    max === min ? 0 : lightness > 0.5 ? (max - min) / (2 - max - min) : (max - min) / (max + min)

  if (saturation < 0.15) {
    const gray = Math.round(lightness * 25)
    return gray === 0 ? 16 : gray === 25 ? 231 : 231 + gray
  }

  const sixRed = red < 95 ? red / 95 : 1 + (red - 95) / 40
  const sixGreen = green < 95 ? green / 95 : 1 + (green - 95) / 40
  const sixBlue = blue < 95 ? blue / 95 : 1 + (blue - 95) / 40

  return 16 + 36 * Math.round(sixRed) + 6 * Math.round(sixGreen) + Math.round(sixBlue)
}

// --- 16-color (nearest by RGB distance) -----------------------------------

const ANSI16 = [
  ['ansi:black', [0, 0, 0]],
  ['ansi:red', [128, 0, 0]],
  ['ansi:green', [0, 128, 0]],
  ['ansi:yellow', [128, 128, 0]],
  ['ansi:blue', [0, 0, 128]],
  ['ansi:magenta', [128, 0, 128]],
  ['ansi:cyan', [0, 128, 128]],
  ['ansi:white', [192, 192, 192]],
  ['ansi:blackBright', [128, 128, 128]],
  ['ansi:redBright', [255, 0, 0]],
  ['ansi:greenBright', [0, 255, 0]],
  ['ansi:yellowBright', [255, 255, 0]],
  ['ansi:blueBright', [0, 0, 255]],
  ['ansi:magentaBright', [255, 0, 255]],
  ['ansi:cyanBright', [0, 255, 255]],
  ['ansi:whiteBright', [255, 255, 255]]
]

function nearest16(red, green, blue) {
  let best = ANSI16[0][0]
  let bestScore = Number.POSITIVE_INFINITY
  for (const [name, [r, g, b]] of ANSI16) {
    const score = (r - red) ** 2 + (g - green) ** 2 + (b - blue) ** 2
    if (score < bestScore) {
      bestScore = score
      best = name
    }
  }
  return best
}

// --- emit ------------------------------------------------------------------

function block(palette, mode) {
  const lines = []
  for (const [key, value] of Object.entries(palette)) {
    const [r, g, b] = toRgb(value)
    const out = mode === '256' ? `ansi256(${ansi256(r, g, b)})` : nearest16(r, g, b)
    lines.push(`    ${key}: '${out}',`)
  }
  return lines.join('\n')
}

for (const [name, palette] of [['DARK', DARK], ['LIGHT', LIGHT]]) {
  for (const mode of ['256', '16']) {
    console.log(`// ${name}_${mode}`)
    console.log('  {')
    console.log(block(palette, mode))
    console.log('  },\n')
  }
}
