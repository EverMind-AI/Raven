// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { render } from 'ink-testing-library'
import React from 'react'
import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '../types.js'

import { ravenLogo, ravenLogoWord, RAVEN_WORD_WIDTH } from '../banner.js'
import { Branding, formatProvider, SessionPanel, StartupLoader } from '../components/branding.js'
import { DEFAULT_THEME } from '../theme.js'

describe('Branding', () => {
  it('does not contain hermes brand', () => {
    const { lastFrame } = render(<Branding />)
    expect(lastFrame()?.toLowerCase()).not.toContain('hermes')
  })

  it('renders without throwing when invoked with no props', () => {
    // The chosen layout (full / stacked / compact) depends on terminal width;
    // all three must render cleanly.
    expect(() => render(<Branding />)).not.toThrow()
  })
})

describe('SessionPanel', () => {
  it('recommends the real raven upgrade command', () => {
    const info: SessionInfo = {
      model: 'anthropic/claude-sonnet-4-6',
      skills: {},
      tools: {},
      update_behind: 1
    }
    const { lastFrame } = render(<SessionPanel info={info} maxCols={80} sid="test" t={DEFAULT_THEME} />)
    expect(lastFrame()).toContain('raven upgrade')
    expect(lastFrame()).not.toContain('raven update')
  })
})

describe('StartupLoader', () => {
  it('renders a spinner frame and the first startup message', () => {
    const { lastFrame } = render(<StartupLoader t={DEFAULT_THEME} />)
    expect(lastFrame()).toContain('summoning raven')
    expect(lastFrame()).toMatch(/[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]/)
  })
})

// The wordmark itself is width-independent; exercise the pure builders so these
// don't depend on ink-testing's terminal columns.
describe('banner wordmark', () => {
  const ramp = DEFAULT_THEME.yellow

  it('ravenLogo renders the 8-row block wordmark', () => {
    const lines = ravenLogo(ramp)
    expect(lines.length).toBe(8)
    expect(lines.map(([, text]) => text).join('')).toContain('█')
  })

  it('ravenLogoWord renders just RAVEN within one-word width', () => {
    const lines = ravenLogoWord(ramp)
    expect(lines.length).toBe(8)
    expect(lines.map(([, text]) => text).join('')).toContain('█')
    const maxWidth = Math.max(...lines.map(([, text]) => [...text].length))
    expect(maxWidth).toBeLessThanOrEqual(RAVEN_WORD_WIDTH)
  })

  it('RAVEN word is 68 columns wide', () => {
    expect(RAVEN_WORD_WIDTH).toBe(68)
  })
})

describe('formatProvider', () => {
  it('returns LUT value for known anthropic slug', () => {
    expect(formatProvider('anthropic', 'claude-sonnet-4-6')).toBe('Anthropic')
  })

  it('parses model_id prefix when slug is "auto" (LiteLLM dispatch)', () => {
    expect(formatProvider('auto', 'openrouter/qwen/qwen3.6-plus')).toBe('OpenRouter')
  })

  it('returns LUT value for qwen slug', () => {
    expect(formatProvider('qwen', 'qwen-max')).toBe('Qwen')
  })

  it('returns em-dash fallback when slug empty and model_id has no slash prefix', () => {
    expect(formatProvider('', 'sonnet')).toBe('—')
  })

  it('returns em-dash fallback when slug is "auto" and model_id is empty', () => {
    expect(formatProvider('auto', '')).toBe('—')
  })

  it('returns canonical OpenAI (not Openai) for openai slug', () => {
    expect(formatProvider('openai', 'gpt-4')).toBe('OpenAI')
  })

  it('falls back to capitalize for unknown providers', () => {
    expect(formatProvider('xyz', 'xyz-foo')).toBe('Xyz')
  })
})
