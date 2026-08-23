/**
 * Color-capability override, applied BEFORE chalk / supports-color initialize.
 *
 * Detection of the actual terminal capability is left to chalk (mainstream
 * `supports-color`), corrected by hermes-ink's vscode/tmux level tweaks. This
 * module only handles user *overrides*, by translating them into the
 * `HERMES_TUI_LEVEL` env var that hermes-ink reads to pin chalk's level
 * EXACTLY. (FORCE_COLOR is only a floor — supports-color still returns 3 when
 * COLORTERM=truecolor, so it can't force a downgrade to 256/16.)
 *
 * Channels:
 *   - `RAVEN_TUI_COLOR` = auto | truecolor | 256 | 16 | none  (the `--color`
 *     flag forwards here).
 *   - `RAVEN_TUI_TRUECOLOR` = 1/true/... — legacy alias for
 *     `RAVEN_TUI_COLOR=truecolor`.
 *
 * Precedence (highest first): NO_COLOR > RAVEN_TUI_COLOR > legacy
 * RAVEN_TUI_TRUECOLOR > chalk auto-detect. Per product decision, a typed
 * `--color` does NOT outrank `NO_COLOR`.
 */

export type ColorTier = 0 | 1 | 2 | 3

const TRUE_RE = /^(?:1|true|yes|on)$/i

/**
 * Parse the requested tier from the Raven color env vars. Returns the
 * forced tier, or `null` for "auto" (defer to chalk's detection).
 */
export function parseColorOverride(env: NodeJS.ProcessEnv = process.env): ColorTier | null {
  const raw = (env.RAVEN_TUI_COLOR ?? '').trim().toLowerCase()

  switch (raw) {
    case 'none':
    case 'off':
    case '0':
      return 0
    case '16':
    case 'ansi':
    case '1':
      return 1
    case '256':
    case 'ansi256':
    case '2':
      return 2
    case 'truecolor':
    case '24bit':
    case 'rgb':
    case '3':
      return 3
    case '':
    case 'auto':
      break
    default:
      break
  }

  // Legacy alias: RAVEN_TUI_TRUECOLOR=1 == RAVEN_TUI_COLOR=truecolor.
  if (TRUE_RE.test((env.RAVEN_TUI_TRUECOLOR ?? '').trim())) {
    return 3
  }

  return null
}

/**
 * Apply the override to `env` by setting `HERMES_TUI_LEVEL` (mutates in
 * place). Must run before chalk / hermes-ink are imported. Returns the tier
 * that was pinned, or `null` when left on auto.
 */
export function applyColorOverride(env: NodeJS.ProcessEnv = process.env): ColorTier | null {
  // NO_COLOR (any value, per no-color.org) beats every override, including a
  // typed --color — colors off.
  if ('NO_COLOR' in env) {
    env.HERMES_TUI_LEVEL = '0'
    env.FORCE_COLOR = '0'
    return 0
  }

  const tier = parseColorOverride(env)

  if (tier === null) {
    return null
  }

  // Pin chalk's level exactly (hermes-ink reads HERMES_TUI_LEVEL).
  env.HERMES_TUI_LEVEL = String(tier)
  // Also drop FORCE_COLOR to off for `none` so any non-hermes chalk goes dark.
  if (tier === 0) {
    env.FORCE_COLOR = '0'
  }

  return tier
}

applyColorOverride()

export {}
