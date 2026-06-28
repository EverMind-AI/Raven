// SPDX-License-Identifier: MIT
// Portions Copyright (c) original ink contributors (vadimdemedes/ink, MIT).
// Portions Copyright (c) 2025 Nous Research (hermes-agent / hermes-ink, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-{hermes-agent,ink}.txt.

import chalk from 'chalk'

import type { Color, TextStyles } from './styles.js'

/**
 * xterm.js (VS Code, Cursor, code-server, Coder) has supported truecolor
 * since 2017, but code-server/Coder containers often don't set
 * COLORTERM=truecolor. chalk's supports-color doesn't recognize
 * TERM_PROGRAM=vscode (it only knows iTerm.app/Apple_Terminal), so it falls
 * through to the -256color regex → level 2. At level 2, chalk.rgb()
 * downgrades to the nearest 6×6×6 cube color: rgb(215,119,87) → idx 174
 * rgb(215,135,135) — washed-out salmon.
 *
 * Gated on level === 2 (not < 3) to respect NO_COLOR / FORCE_COLOR=0 —
 * those yield level 0 and are an explicit "no colors" request. Desktop VS
 * Code sets COLORTERM=truecolor itself, so this is a no-op there (already 3).
 *
 * Must run BEFORE the tmux clamp — if tmux is running inside a VS Code
 * terminal, tmux's passthrough limitation wins and we want level 2.
 */
function boostChalkLevelForXtermJs(): boolean {
  if (process.env.TERM_PROGRAM === 'vscode' && chalk.level === 2) {
    chalk.level = 3

    return true
  }

  return false
}

/**
 * tmux parses truecolor SGR (\e[48;2;r;g;bm) into its cell buffer correctly,
 * but its client-side emitter only re-emits truecolor to the outer terminal if
 * the outer terminal advertises Tc/RGB capability (via terminal-overrides).
 * Default tmux config doesn't set this, so tmux emits the cell to iTerm2/etc
 * WITHOUT the bg sequence — outer terminal's buffer has bg=default → black on
 * dark profiles. Clamping to level 2 makes chalk emit 256-color (\e[48;5;Nm),
 * which tmux passes through cleanly. grey93 (255) is visually identical to
 * rgb(240,240,240).
 *
 * Users who HAVE set `terminal-overrides ,*:Tc` get a technically-unnecessary
 * downgrade, but the visual difference is imperceptible. Querying
 * `tmux show -gv terminal-overrides` to detect this would add a subprocess on
 * startup — not worth it.
 *
 * $TMUX is a pty-lifecycle env var set by tmux itself; it never comes from
 * globalSettings.env, so reading it here is correct. chalk is a singleton, so
 * this clamps ALL truecolor output (fg+bg+hex) across the entire app.
 */
function clampChalkLevelForTmux(): boolean {
  if (process.env.TMUX && chalk.level > 2) {
    chalk.level = 2

    return true
  }

  return false
}

/**
 * Terminal.app before macOS Tahoe 26 silently mangles 24-bit SGR — it
 * approximates RGB to its 256-color palette, so e.g. a bright lime renders
 * olive. Many shells export `COLORTERM=truecolor` globally (for tmux/other
 * terminals), which makes supports-color over-report level 3 here even though
 * Terminal.app can't honor it. Clamp to 256 so colors render faithfully out of
 * the box; Tahoe users (or anyone who really wants RGB) opt back in with an
 * explicit `--color truecolor`, which sets HERMES_TUI_LEVEL and bypasses this.
 */
export function shouldClampForAppleTerminal(
  env: NodeJS.ProcessEnv = process.env,
  level: number = chalk.level
): boolean {
  return env.TERM_PROGRAM === 'Apple_Terminal' && level > 2
}

function clampChalkLevelForAppleTerminal(): boolean {
  if (shouldClampForAppleTerminal()) {
    chalk.level = 2

    return true
  }

  return false
}

/**
 * Explicit color-tier override. `HERMES_TUI_LEVEL=0|1|2|3` pins chalk's level
 * EXACTLY, unlike `FORCE_COLOR` which only acts as a floor (supports-color
 * still returns 3 when `COLORTERM=truecolor`, so `FORCE_COLOR=2` can't force a
 * *downgrade*). The app sets this from `--color` / `RAVEN_TUI_COLOR`. When
 * present it is authoritative and the boost/clamp corrections are skipped.
 */
function applyExplicitLevel(): boolean {
  const raw = (process.env.HERMES_TUI_LEVEL ?? '').trim()

  if (!/^[0-3]$/.test(raw)) {
    return false
  }

  chalk.level = Number(raw) as 0 | 1 | 2 | 3

  return true
}

// Computed once at module load — terminal/tmux environment doesn't change mid-session.
// Order matters: an explicit level wins outright; otherwise boost first, then
// the tmux / Apple Terminal clamps can still drop RGB to 256. Exported for
// debugging — tree-shaken if unused.
export const CHALK_EXPLICIT_LEVEL = applyExplicitLevel()
export const CHALK_BOOSTED_FOR_XTERMJS = !CHALK_EXPLICIT_LEVEL && boostChalkLevelForXtermJs()
export const CHALK_CLAMPED_FOR_TMUX = !CHALK_EXPLICIT_LEVEL && clampChalkLevelForTmux()
export const CHALK_CLAMPED_FOR_APPLE_TERMINAL = !CHALK_EXPLICIT_LEVEL && clampChalkLevelForAppleTerminal()

/**
 * The effective color tier, i.e. chalk's final level after the boost/clamp
 * corrections above: 3 = truecolor, 2 = 256-color, 1 = 16-color, 0 = none.
 *
 * This is the single source of truth for color capability. The app reads it
 * to pick a per-tier palette whose values need no further conversion — at
 * tier 2 we hand chalk `ansi256(N)` and at tier 1 `ansi:<name>`, both of which
 * chalk emits verbatim, so dark greens never collapse onto an olive cube cell
 * the way `chalk.hex()` downsampling at level 2 does.
 */
export function activeColorTier(): 0 | 1 | 2 | 3 {
  return chalk.level
}

export type ColorType = 'foreground' | 'background'

const RGB_REGEX = /^rgb\(\s?(\d+),\s?(\d+),\s?(\d+)\s?\)$/
const ANSI_REGEX = /^ansi256\(\s?(\d+)\s?\)$/

export const colorize = (str: string, color: string | undefined, type: ColorType): string => {
  if (!color) {
    return str
  }

  if (color.startsWith('ansi:')) {
    const value = color.substring('ansi:'.length)

    switch (value) {
      case 'black':
        return type === 'foreground' ? chalk.black(str) : chalk.bgBlack(str)

      case 'red':
        return type === 'foreground' ? chalk.red(str) : chalk.bgRed(str)

      case 'green':
        return type === 'foreground' ? chalk.green(str) : chalk.bgGreen(str)

      case 'yellow':
        return type === 'foreground' ? chalk.yellow(str) : chalk.bgYellow(str)

      case 'blue':
        return type === 'foreground' ? chalk.blue(str) : chalk.bgBlue(str)

      case 'magenta':
        return type === 'foreground' ? chalk.magenta(str) : chalk.bgMagenta(str)

      case 'cyan':
        return type === 'foreground' ? chalk.cyan(str) : chalk.bgCyan(str)

      case 'white':
        return type === 'foreground' ? chalk.white(str) : chalk.bgWhite(str)

      case 'blackBright':
        return type === 'foreground' ? chalk.blackBright(str) : chalk.bgBlackBright(str)

      case 'redBright':
        return type === 'foreground' ? chalk.redBright(str) : chalk.bgRedBright(str)

      case 'greenBright':
        return type === 'foreground' ? chalk.greenBright(str) : chalk.bgGreenBright(str)

      case 'yellowBright':
        return type === 'foreground' ? chalk.yellowBright(str) : chalk.bgYellowBright(str)

      case 'blueBright':
        return type === 'foreground' ? chalk.blueBright(str) : chalk.bgBlueBright(str)

      case 'magentaBright':
        return type === 'foreground' ? chalk.magentaBright(str) : chalk.bgMagentaBright(str)

      case 'cyanBright':
        return type === 'foreground' ? chalk.cyanBright(str) : chalk.bgCyanBright(str)

      case 'whiteBright':
        return type === 'foreground' ? chalk.whiteBright(str) : chalk.bgWhiteBright(str)
    }
  }

  if (color.startsWith('#')) {
    return type === 'foreground' ? chalk.hex(color)(str) : chalk.bgHex(color)(str)
  }

  if (color.startsWith('ansi256')) {
    const matches = ANSI_REGEX.exec(color)

    if (!matches) {
      return str
    }

    const value = Number(matches[1])

    return type === 'foreground' ? chalk.ansi256(value)(str) : chalk.bgAnsi256(value)(str)
  }

  if (color.startsWith('rgb')) {
    const matches = RGB_REGEX.exec(color)

    if (!matches) {
      return str
    }

    const firstValue = Number(matches[1])
    const secondValue = Number(matches[2])
    const thirdValue = Number(matches[3])

    return type === 'foreground'
      ? chalk.rgb(firstValue, secondValue, thirdValue)(str)
      : chalk.bgRgb(firstValue, secondValue, thirdValue)(str)
  }

  return str
}

/**
 * Apply TextStyles to a string using chalk.
 * This is the inverse of parsing ANSI codes - we generate them from structured styles.
 * Theme resolution happens at component layer, not here.
 */
export function applyTextStyles(text: string, styles: TextStyles): string {
  let result = text

  // Apply styles in reverse order of desired nesting.
  // chalk wraps text so later calls become outer wrappers.
  // Desired order (outermost to innermost):
  //   background > foreground > text modifiers
  // So we apply: text modifiers first, then foreground, then background last.

  if (styles.inverse) {
    result = chalk.inverse(result)
  }

  if (styles.strikethrough) {
    result = chalk.strikethrough(result)
  }

  if (styles.underline) {
    result = chalk.underline(result)
  }

  if (styles.italic) {
    result = chalk.italic(result)
  }

  if (styles.bold) {
    result = chalk.bold(result)
  }

  if (styles.dim) {
    result = chalk.dim(result)
  }

  if (styles.color) {
    // Color is now always a raw color value (theme resolution happens at component layer)
    result = colorize(result, styles.color, 'foreground')
  }

  if (styles.backgroundColor) {
    // backgroundColor is now always a raw color value
    result = colorize(result, styles.backgroundColor, 'background')
  }

  return result
}

/**
 * Apply a raw color value to text.
 * Theme resolution should happen at component layer, not here.
 */
export function applyColor(text: string, color: Color | undefined): string {
  if (!color) {
    return text
  }

  return colorize(text, color, 'foreground')
}
