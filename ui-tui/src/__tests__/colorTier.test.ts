// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { describe, expect, it } from 'vitest'

import { applyColorOverride, parseColorOverride } from '../lib/colorTier.js'

describe('parseColorOverride', () => {
  it('maps named + numeric tiers', () => {
    expect(parseColorOverride({ RAVEN_TUI_COLOR: 'truecolor' })).toBe(3)
    expect(parseColorOverride({ RAVEN_TUI_COLOR: '24bit' })).toBe(3)
    expect(parseColorOverride({ RAVEN_TUI_COLOR: '256' })).toBe(2)
    expect(parseColorOverride({ RAVEN_TUI_COLOR: '16' })).toBe(1)
    expect(parseColorOverride({ RAVEN_TUI_COLOR: 'none' })).toBe(0)
    expect(parseColorOverride({ RAVEN_TUI_COLOR: '3' })).toBe(3)
  })

  it('treats auto / unset / unknown as auto (null)', () => {
    expect(parseColorOverride({ RAVEN_TUI_COLOR: 'auto' })).toBeNull()
    expect(parseColorOverride({})).toBeNull()
    expect(parseColorOverride({ RAVEN_TUI_COLOR: 'wat' })).toBeNull()
  })

  it('honors the legacy RAVEN_TUI_TRUECOLOR alias', () => {
    expect(parseColorOverride({ RAVEN_TUI_TRUECOLOR: '1' })).toBe(3)
    expect(parseColorOverride({ RAVEN_TUI_TRUECOLOR: '0' })).toBeNull()
  })

  it('lets RAVEN_TUI_COLOR win over the legacy alias', () => {
    expect(parseColorOverride({ RAVEN_TUI_COLOR: '256', RAVEN_TUI_TRUECOLOR: '1' })).toBe(2)
  })
})

describe('applyColorOverride', () => {
  it('pins HERMES_TUI_LEVEL to the requested tier', () => {
    const env: NodeJS.ProcessEnv = { RAVEN_TUI_COLOR: '256' }
    expect(applyColorOverride(env)).toBe(2)
    expect(env.HERMES_TUI_LEVEL).toBe('2')
  })

  it('forces colors off for none', () => {
    const env: NodeJS.ProcessEnv = { RAVEN_TUI_COLOR: 'none' }
    expect(applyColorOverride(env)).toBe(0)
    expect(env.HERMES_TUI_LEVEL).toBe('0')
    expect(env.FORCE_COLOR).toBe('0')
  })

  it('leaves the env untouched on auto', () => {
    const env: NodeJS.ProcessEnv = {}
    expect(applyColorOverride(env)).toBeNull()
    expect('HERMES_TUI_LEVEL' in env).toBe(false)
  })

  it('NO_COLOR beats a typed --color (forces off)', () => {
    const env: NodeJS.ProcessEnv = { NO_COLOR: '1', RAVEN_TUI_COLOR: 'truecolor' }
    expect(applyColorOverride(env)).toBe(0)
    expect(env.HERMES_TUI_LEVEL).toBe('0')
  })

  it('pins an explicit truecolor downgrade target regardless of FORCE_COLOR', () => {
    const env: NodeJS.ProcessEnv = { FORCE_COLOR: '3', RAVEN_TUI_COLOR: '256' }
    expect(applyColorOverride(env)).toBe(2)
    expect(env.HERMES_TUI_LEVEL).toBe('2')
  })
})
