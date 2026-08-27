// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import { argPreview, dagPromptTemplates } from '../lib/toolArgs.js'

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

  it('previews a delegation call by its prompt template', () => {
    expect(
      argPreview({
        task_summary: 'checking the plan',
        prompt_template: 'follow this: {{ ref:plan.md }}',
        subagent: 'raven'
      })
    ).toBe('follow this: {{ ref:plan.md }}')
  })

  it('never returns a bare number or a JSON blob', () => {
    expect(argPreview({ count: 10, verbose: true })).toBe('')
    expect(argPreview({})).toBe('')
    // Falls back to the first reachable string when no preferred key matches.
    expect(argPreview({ misc: 'plain value' })).toBe('plain value')
  })
})

describe('dagPromptTemplates', () => {
  it('keys each node prompt by its node id', () => {
    expect(
      dagPromptTemplates({
        nodes: [
          { id: 'fetch', agent: 'Coder', prompt_template: 'read the i18n messages' },
          { id: 'report', agent: 'Writer', prompt_template: 'summarise {{ fetch.output }}' }
        ]
      })
    ).toEqual({ fetch: 'read the i18n messages', report: 'summarise {{ fetch.output }}' })
  })

  it('accepts the camelCase spelling humans write in a graph file', () => {
    // The JSON Schema handed to the model is snake_case; the file form reviewed
    // in git is camelCase, and both reach the client as authored.
    expect(dagPromptTemplates({ nodes: [{ id: 'a', promptTemplate: 'do the thing' }] })).toEqual({
      a: 'do the thing'
    })
  })

  it('skips a node with no usable id or template', () => {
    expect(
      dagPromptTemplates({
        nodes: [
          { id: 'ok', prompt_template: 'yes' },
          { prompt_template: 'no id' },
          { id: 'blank', prompt_template: '  ' },
          'junk',
          null
        ]
      })
    ).toEqual({ ok: 'yes' })
  })

  it('is empty for a call that carries no node list', () => {
    expect(dagPromptTemplates({})).toEqual({})
    expect(dagPromptTemplates({ nodes: 'not a list' })).toEqual({})
  })
})
