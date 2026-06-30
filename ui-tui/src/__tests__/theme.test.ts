// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { afterEach, describe, expect, it, vi } from 'vitest'

// `theme.js` reads `process.env` at module-load to compute DEFAULT_THEME,
// and `fromSkin` closes over DEFAULT_THEME.  A developer shell with
// RAVEN_TUI_THEME=light (or RAVEN_TUI_BACKGROUND set to something
// bright) would flip the base and turn these assertions into a local-
// only failure.  We sterilize the relevant env vars + dynamically
// import the module fresh so EVERY symbol that closes over the env
// (DEFAULT_THEME, DARK_THEME, LIGHT_THEME, fromSkin) is loaded against
// a known-empty environment.
//
// `detectLightMode` takes env as an explicit arg, so it's safe to import
// statically — but we stay consistent and dynamic-import it too.
const RELEVANT_ENV = [
  'RAVEN_TUI_LIGHT',
  'RAVEN_TUI_THEME',
  'RAVEN_TUI_BACKGROUND',
  'COLORFGBG',
  'COLORTERM',
  'TERM_PROGRAM'
] as const

async function importThemeWithEnv(env: Partial<Record<(typeof RELEVANT_ENV)[number], string>> = {}) {
  for (const key of RELEVANT_ENV) {
    vi.stubEnv(key, env[key] ?? '')
  }

  vi.resetModules()

  return import('../theme.js')
}

async function importThemeWithCleanEnv() {
  return importThemeWithEnv()
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('DEFAULT_THEME', () => {
  it('has brand defaults', async () => {
    const { DEFAULT_THEME } = await importThemeWithCleanEnv()

    expect(DEFAULT_THEME.brand.name).toBe('Raven Agent')
    expect(DEFAULT_THEME.brand.icon).toBe('🐦‍⬛')
    expect(DEFAULT_THEME.brand.prompt).toBe('❯')
    expect(DEFAULT_THEME.brand.tool).toBe('┊')
  })

  it('has color palette', async () => {
    const { DEFAULT_THEME } = await importThemeWithCleanEnv()

    expect(DEFAULT_THEME.color.primary).toBe('#fbe23f')
    expect(DEFAULT_THEME.color.error).toBe('#ec6a5e')
  })
})

describe('LIGHT_THEME', () => {
  it('avoids bright-yellow accents unreadable on white backgrounds (#11300)', async () => {
    const { LIGHT_THEME } = await importThemeWithCleanEnv()

    expect(LIGHT_THEME.color.primary).not.toBe('#FFD700')
    expect(LIGHT_THEME.color.accent).not.toBe('#FFBF00')
    expect(LIGHT_THEME.color.muted).not.toBe('#B8860B')
    expect(LIGHT_THEME.color.statusWarn).not.toBe('#FFD700')
  })

  it('keeps the same shape as DARK_THEME', async () => {
    const { DARK_THEME, LIGHT_THEME } = await importThemeWithCleanEnv()

    expect(Object.keys(LIGHT_THEME.color).sort()).toEqual(Object.keys(DARK_THEME.color).sort())
    expect(LIGHT_THEME.brand).toEqual(DARK_THEME.brand)
  })
})

describe('brand yellow ramp (title-gradient-table.md)', () => {
  // Truecolor order is [.50,.100,.300,.500,.600,.700,.900,.950,.990] — the
  // extra .600 puts .700 at index 5 and .900 at index 6. The 256 tier keeps the
  // 8-stop set ([.50,.100,.300,.500,.700,.900,.950,.990]) with .700 at index 4.
  it('keeps the documented dark title bands', async () => {
    const { DARK_THEME } = await importThemeWithCleanEnv()

    expect(DARK_THEME.yellow[0]).toBe('#fff7c2') // .50
    expect(DARK_THEME.yellow[2]).toBe('#FFE573') // .300
    expect(DARK_THEME.yellow[3]).toBe('#fbe23f') // .500
    expect(DARK_THEME.yellow[5]).toBe('#c8a900') // .700
    expect(DARK_THEME.yellow[6]).toBe('#8a6d00') // .900
  })

  it('gives light its own gold ramp re-derived around #B87900, not the dark scale', async () => {
    const { DARK_THEME, LIGHT_THEME } = await importThemeWithCleanEnv()

    expect(LIGHT_THEME.yellow[0]).toBe('#F6DA8B') // .50
    expect(LIGHT_THEME.yellow[2]).toBe('#D9A83A') // .300
    expect(LIGHT_THEME.yellow[3]).toBe('#B87900') // .500
    expect(LIGHT_THEME.yellow[5]).toBe('#935F00') // .700
    expect(LIGHT_THEME.yellow[6]).toBe('#684300') // .900

    expect(LIGHT_THEME.yellow).not.toEqual(DARK_THEME.yellow)
  })

  it('carries scheme-specific 256-color ramps for the documented title bands', async () => {
    const { resolveTheme } = await importThemeWithCleanEnv()

    const dark = resolveTheme('dark', 2).yellow
    const light = resolveTheme('light', 2).yellow

    // 256 is the 8-stop set: .50/.300/.500/.700/.900 → indices 0/2/3/4/5.
    expect([dark[0], dark[2], dark[3], dark[4], dark[5]]).toEqual([
      'ansi256(229)',
      'ansi256(228)',
      'ansi256(220)',
      'ansi256(178)',
      'ansi256(94)'
    ])
    // yellow.300 and yellow.500 must not collapse onto the same 256 index.
    expect(dark[2]).not.toBe(dark[3])
    expect([light[0], light[2], light[3], light[4], light[5]]).toEqual([
      'ansi256(222)',
      'ansi256(179)',
      'ansi256(136)',
      'ansi256(94)',
      'ansi256(58)'
    ])

    expect(light).not.toEqual(dark)
  })

  it('keeps 16-color yellow for both schemes', async () => {
    const { resolveTheme } = await importThemeWithCleanEnv()

    expect(resolveTheme('light', 1).yellow).toEqual(resolveTheme('dark', 1).yellow)
    expect(resolveTheme('light', 1).yellow[0]).toBe('ansi:yellow')
  })
})

describe('cursorColorHex (OSC 12 hardware cursor)', () => {
  it('returns a truecolor hex even when the theme tier is 256/16 (default dark)', async () => {
    const { cursorColorHex, resolveTheme } = await importThemeWithCleanEnv()

    // tiers 1/2 store ansi indices, but OSC 12 needs an RGB color.
    expect(cursorColorHex(resolveTheme('dark', 3))).toBe('#fbe23f') // truecolor primary passed through
    expect(cursorColorHex(resolveTheme('dark', 2))).toBe('#fbe23f')
    expect(cursorColorHex(resolveTheme('dark', 1))).toBe('#fbe23f')
  })

  it('uses the light title color across all tiers when the scheme is light', async () => {
    const { cursorColorHex, resolveTheme } = await importThemeWithEnv({ RAVEN_TUI_THEME: 'light' })

    expect(cursorColorHex(resolveTheme('light', 3))).toBe('#B87900')
    expect(cursorColorHex(resolveTheme('light', 2))).toBe('#B87900')
    expect(cursorColorHex(resolveTheme('light', 1))).toBe('#B87900')
  })
})

describe('DEFAULT_THEME aliasing', () => {
  it('defaults to DARK_THEME when nothing signals light', async () => {
    const { DEFAULT_THEME, DARK_THEME: DARK } = await importThemeWithCleanEnv()

    expect(DEFAULT_THEME).toBe(DARK)
  })
})

describe('detectLightMode', () => {
  it('returns false on empty env', async () => {
    const { detectLightMode } = await importThemeWithCleanEnv()

    expect(detectLightMode({})).toBe(false)
  })

  it('stays dark on Apple Terminal when no stronger signal is present', async () => {
    const { detectLightMode } = await importThemeWithCleanEnv()

    // TERM_PROGRAM alone is no longer a light signal: Terminal.app ships both
    // light and dark profiles and emits no COLORFGBG, so it defaults to dark.
    expect(detectLightMode({ TERM_PROGRAM: 'Apple_Terminal' })).toBe(false)
  })

  it('honors RAVEN_TUI_LIGHT on/off', async () => {
    const { detectLightMode } = await importThemeWithCleanEnv()

    expect(detectLightMode({ RAVEN_TUI_LIGHT: '1' })).toBe(true)
    expect(detectLightMode({ RAVEN_TUI_LIGHT: 'true' })).toBe(true)
    expect(detectLightMode({ RAVEN_TUI_LIGHT: 'on' })).toBe(true)
    expect(detectLightMode({ RAVEN_TUI_LIGHT: '0' })).toBe(false)
    expect(detectLightMode({ RAVEN_TUI_LIGHT: 'off' })).toBe(false)
  })

  it('sniffs COLORFGBG bg slots 7 and 15 as light (#11300)', async () => {
    const { detectLightMode } = await importThemeWithCleanEnv()

    expect(detectLightMode({ COLORFGBG: '0;15' })).toBe(true)
    expect(detectLightMode({ COLORFGBG: '0;default;15' })).toBe(true)
    expect(detectLightMode({ COLORFGBG: '0;7' })).toBe(true)
    expect(detectLightMode({ COLORFGBG: '15;0' })).toBe(false)
    expect(detectLightMode({ COLORFGBG: '7;default;0' })).toBe(false)
  })

  it('falls through on malformed COLORFGBG with empty/non-numeric trailing field', async () => {
    const { detectLightMode } = await importThemeWithCleanEnv()
    // `Number('')` is 0, so `'15;'` would have been read as bg=0
    // (authoritative dark) and incorrectly blocked TERM_PROGRAM.
    // The strict /^\d+$/ guard makes these fall through instead.
    const allowList = new Set(['Apple_Terminal'])

    expect(detectLightMode({ COLORFGBG: '15;', TERM_PROGRAM: 'Apple_Terminal' }, allowList)).toBe(true)
    expect(detectLightMode({ COLORFGBG: 'default;default', TERM_PROGRAM: 'Apple_Terminal' }, allowList)).toBe(true)
    // Without an allow-list match, fall-through still defaults to dark.
    expect(detectLightMode({ COLORFGBG: '15;' })).toBe(false)
  })

  it('lets RAVEN_TUI_LIGHT=0 override a light COLORFGBG', async () => {
    const { detectLightMode } = await importThemeWithCleanEnv()

    expect(detectLightMode({ COLORFGBG: '0;15', RAVEN_TUI_LIGHT: '0' })).toBe(false)
  })

  it('honors RAVEN_TUI_THEME=light/dark as a symmetric explicit override', async () => {
    const { detectLightMode } = await importThemeWithCleanEnv()

    expect(detectLightMode({ RAVEN_TUI_THEME: 'light' })).toBe(true)
    expect(detectLightMode({ RAVEN_TUI_THEME: 'dark' })).toBe(false)
    expect(detectLightMode({ COLORFGBG: '0;15', RAVEN_TUI_THEME: 'dark' })).toBe(false)
    expect(detectLightMode({ COLORFGBG: '15;0', RAVEN_TUI_THEME: 'light' })).toBe(true)
  })

  it('uses RAVEN_TUI_BACKGROUND luminance when COLORFGBG is missing', async () => {
    const { detectLightMode } = await importThemeWithCleanEnv()

    expect(detectLightMode({ RAVEN_TUI_BACKGROUND: '#ffffff' })).toBe(true)
    expect(detectLightMode({ RAVEN_TUI_BACKGROUND: '#000000' })).toBe(false)
    expect(detectLightMode({ RAVEN_TUI_BACKGROUND: '#1e1e1e' })).toBe(false)
    // Three-char hex normalises like CSS.
    expect(detectLightMode({ RAVEN_TUI_BACKGROUND: '#fff' })).toBe(true)
    // Garbage falls through to the default-dark path.
    expect(detectLightMode({ RAVEN_TUI_BACKGROUND: 'not-a-colour' })).toBe(false)
  })

  it('rejects partially-invalid hex instead of silently truncating', async () => {
    const { detectLightMode } = await importThemeWithCleanEnv()
    // `parseInt('fffgff'.slice(2,4), 16)` would return 15 — the strict
    // regex must reject these inputs so they fall through to default-
    // dark instead of producing a false-positive light reading.
    expect(detectLightMode({ RAVEN_TUI_BACKGROUND: '#fffgff' })).toBe(false)
    expect(detectLightMode({ RAVEN_TUI_BACKGROUND: 'ffggff' })).toBe(false)
    expect(detectLightMode({ RAVEN_TUI_BACKGROUND: '#xyz' })).toBe(false)
    // Wrong length also rejected (no implicit padding/truncation).
    expect(detectLightMode({ RAVEN_TUI_BACKGROUND: '#fffff' })).toBe(false)
    expect(detectLightMode({ RAVEN_TUI_BACKGROUND: '#fffffff' })).toBe(false)
  })

  it('treats COLORFGBG as authoritative when present so it dominates the TERM_PROGRAM allow-list', async () => {
    const { detectLightMode } = await importThemeWithCleanEnv()
    // Injecting the allow-list keeps this precedence rule explicit even if
    // production defaults change.
    const allowList = new Set(['Apple_Terminal'])

    // Sanity: the allow-list alone WOULD turn this terminal light.
    expect(detectLightMode({ TERM_PROGRAM: 'Apple_Terminal' }, allowList)).toBe(true)

    // Dark COLORFGBG must beat the allow-list.
    expect(detectLightMode({ COLORFGBG: '15;0', TERM_PROGRAM: 'Apple_Terminal' }, allowList)).toBe(false)
  })
})

describe('applyDetectedBackground (OSC 11 reply)', () => {
  it('parses 16-bit rgb: replies and flips the scheme to light on a bright bg', async () => {
    const { applyDetectedBackground, currentScheme } = await importThemeWithCleanEnv()

    expect(currentScheme()).toBe('dark')

    const res = applyDetectedBackground('rgb:ffff/ffff/ffff')

    expect(res).toEqual({ changed: true, scheme: 'light' })
    expect(currentScheme()).toBe('light')
  })

  it('parses 8-bit rgb: and #rrggbb dark replies as dark', async () => {
    const { applyDetectedBackground } = await importThemeWithCleanEnv()

    expect(applyDetectedBackground('rgb:1e/1e/2e')).toEqual({ changed: false, scheme: 'dark' })
    expect(applyDetectedBackground('#1e1e2e')).toEqual({ changed: false, scheme: 'dark' })
  })

  it('caches the measured color into RAVEN_TUI_BACKGROUND', async () => {
    const { applyDetectedBackground } = await importThemeWithCleanEnv()

    applyDetectedBackground('rgb:eaea/eaea/eaea')

    expect(process.env.RAVEN_TUI_BACKGROUND).toBe('#eaeaea')
  })

  it('lets an explicit RAVEN_TUI_THEME override the measured background', async () => {
    const { applyDetectedBackground, currentScheme } = await importThemeWithEnv({ RAVEN_TUI_THEME: 'dark' })

    // Bright measured bg, but the explicit dark override wins (precedence is
    // reused from detectLightMode).
    expect(applyDetectedBackground('rgb:ffff/ffff/ffff')).toEqual({ changed: false, scheme: 'dark' })
    expect(currentScheme()).toBe('dark')
  })

  it('returns null and leaves the scheme untouched on an unparseable reply', async () => {
    const { applyDetectedBackground, currentScheme } = await importThemeWithCleanEnv()

    expect(applyDetectedBackground('not-a-color')).toBeNull()
    expect(currentScheme()).toBe('dark')
  })
})

describe('fromSkin', () => {
  // `fromSkin` closes over DEFAULT_THEME (which is env-derived), so we
  // must dynamic-import it after sterilizing env — otherwise an ambient
  // RAVEN_TUI_THEME=light would flip the base palette and make these
  // assertions order-dependent on the developer's shell.

  it('overrides banner colors', async () => {
    const { fromSkin } = await importThemeWithCleanEnv()

    expect(fromSkin({ banner_title: '#FF0000' }, {}).color.primary).toBe('#FF0000')
  })

  it('preserves unset colors', async () => {
    const { DEFAULT_THEME, fromSkin } = await importThemeWithCleanEnv()

    expect(fromSkin({ banner_title: '#FF0000' }, {}).color.accent).toBe(DEFAULT_THEME.color.accent)
  })

  it('derives completion current background from resolved completion background', async () => {
    const { fromSkin } = await importThemeWithCleanEnv()

    const theme = fromSkin({ banner_accent: '#000000', completion_menu_bg: '#ffffff' }, {})

    expect(theme.color.completionBg).toBe('#ffffff')
    expect(theme.color.completionCurrentBg).toBe('#bfbfbf')
  })

  it('uses active completion color as the selection highlight fallback', async () => {
    const { fromSkin } = await importThemeWithCleanEnv()

    const theme = fromSkin({ completion_menu_current_bg: '#123456' }, {})

    expect(theme.color.selectionBg).toBe('#123456')
  })

  it('maps completion meta background colors from skins', async () => {
    const { fromSkin } = await importThemeWithCleanEnv()

    const theme = fromSkin({
      completion_menu_meta_bg: '#111111',
      completion_menu_meta_current_bg: '#222222'
    }, {})

    expect(theme.color.completionMetaBg).toBe('#111111')
    expect(theme.color.completionMetaCurrentBg).toBe('#222222')
  })

  it('lets selection_bg override completion highlight colors', async () => {
    const { fromSkin } = await importThemeWithCleanEnv()

    const theme = fromSkin({ completion_menu_current_bg: '#123456', selection_bg: '#654321' }, {})

    expect(theme.color.selectionBg).toBe('#654321')
  })

  it('overrides branding', async () => {
    const { fromSkin } = await importThemeWithCleanEnv()
    const { brand } = fromSkin({}, { agent_name: 'TestBot', prompt_symbol: '$' })

    expect(brand.name).toBe('TestBot')
    expect(brand.prompt).toBe('$')
  })

  it('normalizes skin prompt symbols to trimmed single-line text', async () => {
    const { DEFAULT_THEME, fromSkin } = await importThemeWithCleanEnv()

    expect(fromSkin({}, { prompt_symbol: ' ⚔ ❯ \n' }).brand.prompt).toBe('⚔ ❯')
    expect(fromSkin({}, { prompt_symbol: ' Ψ > \n' }).brand.prompt).toBe('Ψ >')
    expect(fromSkin({}, { prompt_symbol: '\n\t' }).brand.prompt).toBe(DEFAULT_THEME.brand.prompt)
  })

  it('defaults for empty skin', async () => {
    const { DEFAULT_THEME, fromSkin } = await importThemeWithCleanEnv()

    expect(fromSkin({}, {}).color).toEqual(DEFAULT_THEME.color)
    expect(fromSkin({}, {}).brand.icon).toBe(DEFAULT_THEME.brand.icon)
  })

  it('passes banner logo/hero', async () => {
    const { fromSkin } = await importThemeWithCleanEnv()

    expect(fromSkin({}, {}, 'LOGO', 'HERO').bannerLogo).toBe('LOGO')
    expect(fromSkin({}, {}, 'LOGO', 'HERO').bannerHero).toBe('HERO')
  })

  it('maps ui_ color keys + cascades to status', async () => {
    const { fromSkin } = await importThemeWithCleanEnv()
    const { color } = fromSkin({ ui_ok: '#008000' }, {})

    expect(color.ok).toBe('#008000')
    expect(color.statusGood).toBe('#008000')
  })
})

describe('resolveTheme', () => {
  it('returns the hex palette for truecolor and no-color tiers', async () => {
    const { resolveTheme, DARK_THEME, LIGHT_THEME } = await importThemeWithCleanEnv()

    expect(resolveTheme('dark', 3)).toBe(DARK_THEME)
    expect(resolveTheme('dark', 0)).toBe(DARK_THEME)
    expect(resolveTheme('light', 3)).toBe(LIGHT_THEME)
  })

  it('uses curated ansi256 values at tier 2', async () => {
    const { resolveTheme } = await importThemeWithCleanEnv()

    expect(resolveTheme('dark', 2).color.primary).toBe('ansi256(221)')
    expect(resolveTheme('light', 2).color.primary).toBe('ansi256(136)')
  })

  it('uses named 16-color values at tier 1', async () => {
    const { resolveTheme } = await importThemeWithCleanEnv()

    expect(resolveTheme('dark', 1).color.primary).toBe('ansi:yellowBright')
    expect(resolveTheme('dark', 1).color.accent).toBe('ansi:yellowBright')
  })

  it('keeps the same color-role shape across every tier', async () => {
    const { resolveTheme, DARK_THEME } = await importThemeWithCleanEnv()
    const roles = Object.keys(DARK_THEME.color).sort()

    for (const tier of [0, 1, 2, 3] as const) {
      expect(Object.keys(resolveTheme('dark', tier).color).sort()).toEqual(roles)
      expect(Object.keys(resolveTheme('light', tier).color).sort()).toEqual(roles)
    }
  })
})
