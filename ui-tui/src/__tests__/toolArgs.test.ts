// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import { argPreview } from '../lib/toolArgs.js'

describe('argPreview', () => {
  it('prefers the query over a numeric flag (web_search), regardless of key order', () => {
    expect(argPreview({ count: 10, query: 'hermes agent' })).toBe('hermes agent')
    expect(argPreview({ query: 'hermes agent', count: 10 })).toBe('hermes agent')
  })

  it('digs a question out of a nested blob instead of dumping JSON (ask_user)', () => {
    expect(
      argPreview({
        questions: [{ options: [{ name: 'A' }, { name: 'B' }], question: '你想调研的 Hermes 是哪个？' }]
      })
    ).toBe('你想调研的 Hermes 是哪个？')
  })

  it('reads the obvious argument for common tools', () => {
    expect(argPreview({ command: 'ls -la' })).toBe('ls -la')
    expect(argPreview({ path: 'src/app/chatStream.ts', limit: 20 })).toBe('src/app/chatStream.ts')
    expect(argPreview({ pattern: 'TODO', path: '.' })).toBe('TODO')
    expect(argPreview({ url: 'https://example.com' })).toBe('https://example.com')
    expect(argPreview({ prompt: 'a red fox in snow' })).toBe('a red fox in snow')
  })

  it('never returns a bare number or a JSON blob', () => {
    expect(argPreview({ count: 10, verbose: true })).toBe('')
    expect(argPreview({})).toBe('')
    // Falls back to the first reachable string when no preferred key matches.
    expect(argPreview({ misc: 'plain value' })).toBe('plain value')
  })
})
