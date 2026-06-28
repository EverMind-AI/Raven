// Diagnostic color renderers for `raven tui --print-colors` (flat swatches)
// and `--preview-colors` (tokens shown in their real UI contexts).
//
// Both use the SAME colorize path the UI uses, so output reflects exactly how
// the active (or forced via --color) tier renders. Re-run with a different
// `--color <tier>` to compare tiers.

import { colorize } from '@hermes/ink'

import { ravenLogo } from '../banner.js'
import type { Theme, ThemeColors } from '../theme.js'

const TIER_NAMES: Record<number, string> = {
  0: 'none',
  1: '16-color',
  2: '256-color',
  3: 'truecolor'
}

const fg = (s: string, color: string) => colorize(s, color, 'foreground')
// Background + foreground in one cell — the pairing real components use for
// completion rows, the status bar, selections, and diff lines.
const fgbg = (s: string, fgColor: string, bgColor: string) =>
  colorize(colorize(s, bgColor, 'background'), fgColor, 'foreground')

export function renderColorSwatches(theme: Theme, tier: 0 | 1 | 2 | 3): string {
  const roles = Object.keys(theme.color) as (keyof ThemeColors)[]
  const labelWidth = Math.max(...roles.map(r => r.length))

  const lines: string[] = []
  lines.push(`Raven TUI palette — tier ${tier} (${TIER_NAMES[tier] ?? 'unknown'})`)
  lines.push('')

  for (const role of roles) {
    const value = theme.color[role]
    const label = role.padEnd(labelWidth)
    const swatch = colorize('████████', value, 'foreground')
    lines.push(`  ${label}  ${swatch}  ${value}`)
  }

  lines.push('')
  lines.push('Force a tier to compare:  raven tui --color <truecolor|256|16|none> --print-colors')

  return lines.join('\n') + '\n'
}

/**
 * Render the tokens in the contexts they're actually used — so the designer
 * sees real fg/bg pairings (status bar, completion rows, diff lines) and the
 * banner gradient, not just isolated swatches.
 */
export function renderColorPreview(theme: Theme, tier: 0 | 1 | 2 | 3): string {
  const c = theme.color
  const out: string[] = []
  const section = (title: string) => {
    out.push('')
    out.push(fg(`── ${title} `.padEnd(60, '─'), c.muted))
  }

  out.push(`Raven TUI color usage preview — tier ${tier} (${TIER_NAMES[tier] ?? 'unknown'})`)

  section('Banner (yellow ramp)')
  for (const [color, text] of ravenLogo(theme.yellow)) {
    out.push('  ' + fg(text, color))
  }

  section('Prompt & input')
  out.push('  ' + fg('❯', c.prompt) + ' ' + fg('ask me something…', c.muted))
  out.push('  ' + fg('$', c.shellDollar) + ' ' + fg('git status', c.text))

  section('Text roles')
  out.push(
    '  ' +
      [
        fg('primary', c.primary),
        fg('accent / link', c.accent),
        fg('body text', c.text),
        fg('muted', c.muted),
        fg('label', c.label)
      ].join('   ')
  )

  section('Semantic')
  out.push('  ' + [fg('✓ ok', c.ok), fg('⚠ warn', c.warn), fg('✗ error', c.error)].join('   '))

  section('Status bar (fg on statusBg)')
  out.push(
    '  ' +
      fgbg(' ● READY ', c.statusGood, c.statusBg) +
      fgbg(' main ', c.statusFg, c.statusBg) +
      fgbg(' ⚠ 2 ', c.statusWarn, c.statusBg) +
      fgbg(' ✗ 1 ', c.statusBad, c.statusBg) +
      fgbg(' ‼ FATAL ', c.statusCritical, c.statusBg)
  )

  section('Completion menu (row bg + meta column)')
  out.push('  ' + fgbg(' /help    ', c.text, c.completionBg) + fgbg(' show commands ', c.muted, c.completionMetaBg))
  out.push(
    '  ' +
      fgbg(' /model   ', c.text, c.completionCurrentBg) +
      fgbg(' switch model  ', c.muted, c.completionMetaCurrentBg) +
      fg('  ← current', c.muted)
  )
  out.push('  ' + fgbg(' /clear   ', c.text, c.completionBg) + fgbg(' reset session ', c.muted, c.completionMetaBg))

  section('Selection')
  out.push('  ' + fg('normal ', c.text) + fgbg('selected text', c.text, c.selectionBg) + fg(' normal', c.text))

  section('Session box (border / sessionLabel / sessionBorder)')
  out.push('  ' + fg('╭────────────────────────────╮', c.sessionBorder))
  out.push(
    '  ' +
      fg('│ ', c.sessionBorder) +
      fg('Session ', c.sessionLabel) +
      fg('a1b2c3d4', c.accent) +
      fg('           │', c.sessionBorder)
  )
  out.push('  ' + fg('╰────────────────────────────╯', c.sessionBorder))

  section('Diff (line bg = diffAdded/Removed, word fg = diff*Word)')
  out.push('  ' + fgbg('+ added line of code', c.diffAddedWord, c.diffAdded))
  out.push('  ' + fgbg('- removed line of code', c.diffRemovedWord, c.diffRemoved))

  out.push('')
  out.push(fg('Force a tier:  raven tui --color <truecolor|256|16|none> --preview-colors', c.muted))

  return out.join('\n') + '\n'
}
