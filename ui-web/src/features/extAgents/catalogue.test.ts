import { beforeEach, describe, expect, it } from 'vitest'

import { setTranslator } from '../../i18n/t'
import { byOf, installOf, isOwnRow, shortOf } from './catalogue'

import type { ExtAgentRow } from './types'

function row(over: Partial<ExtAgentRow> = {}): ExtAgentRow {
  return {
    name: 'claude_code',
    preset: 'claude_code',
    kind: 'acp',
    configured: false,
    enabled: false,
    probe_status: 'ready',
    probe_detail: '',
    has_api_key: false,
    description: '',
    test_running: false,
    last_test_ok: null,
    last_test_at_ms: null,
    last_test_detail: '',
    ...over,
  }
}

beforeEach(() => {
  setTranslator((key) => key)
})

describe('the agent catalogue', () => {
  it('answers a preset by its preset, whatever the row is called', () => {
    const r = row({ name: 'my coder', preset: 'claude_code' })
    expect(shortOf(r)).toBe('gui.agent.short_claude_code')
    expect(byOf(r)).toBe('Anthropic')
    expect(installOf(r).cmd).toContain('@anthropic-ai/claude-code')
    expect(isOwnRow(r)).toBe(false)
  })

  it("answers one of Raven's own by name, even when this install registered it as a config row", () => {
    const r = row({ name: 'Raven-Code', preset: undefined, kind: 'acp', configured: true, enabled: true })
    expect(isOwnRow(r)).toBe(true)
    expect(shortOf(r)).toBe('gui.agent.short_raven_code')
    expect(byOf(r)).toBe('gui.agent.by_raven')
    expect(installOf(r)).toEqual({ site: undefined, cmd: undefined })
  })

  it('translates a vendor whose name is in the catalogue and prints an ASCII brand verbatim', () => {
    expect(byOf(row({ preset: 'qwen_code' }))).toBe('gui.agent.by_qwen_code')
    expect(byOf(row({ preset: 'grok' }))).toBe('xAI')
  })

  it('says nothing about a row nobody catalogued', () => {
    const r = row({ name: 'my-agent', preset: undefined })
    expect(shortOf(r)).toBe('')
    expect(byOf(r)).toBe('')
    expect(installOf(r)).toEqual({})
    expect(isOwnRow(r)).toBe(false)
  })

  it('knows the built-in row is Raven whatever the wire calls it', () => {
    const r = row({ name: 'raven', preset: undefined, kind: 'builtin', builtin: true })
    expect(isOwnRow(r)).toBe(true)
    expect(shortOf(r)).toBe('gui.agent.short_raven')
    expect(byOf(r)).toBe('gui.agent.by_raven')
    expect(installOf(r)).toEqual({ site: undefined, cmd: undefined })
  })
})
