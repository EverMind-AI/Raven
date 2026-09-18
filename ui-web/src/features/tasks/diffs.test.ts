import { describe, expect, it } from 'vitest'

import { hunkForFile } from './diffs'

import type { NodeStep } from './types'

const tool = (name: string, args: object): Extract<NodeStep, { kind: 'tool' }> =>
  ({ kind: 'tool', id: 'c1', name, args: JSON.stringify(args), result: 'ok', ok: true })

describe('hunkForFile', () => {
  it('reads a write as all-added, the same convention record.ts draws a plain write with', () => {
    const hunk = hunkForFile([tool('write_file', { path: '/w/a.md', content: 'one\ntwo\n' })], '/w/a.md')
    expect(hunk).toMatchObject({ add: 2, del: 0 })
  })

  it('reads an edit as the real before/after slice', () => {
    const hunk = hunkForFile(
      [tool('edit_file', { path: '/w/a.py', old_text: 'a\nb\nc\n', new_text: 'a\nB\nc\n' })],
      '/w/a.py',
    )
    expect(hunk).toMatchObject({ add: 1, del: 1 })
  })

  it('ignores a step against a different path', () => {
    expect(hunkForFile([tool('write_file', { path: '/w/other.md', content: 'x' })], '/w/a.md')).toBeNull()
  })

  it('takes the last call against the path when a node touched it twice', () => {
    const hunk = hunkForFile([
      tool('write_file', { path: '/w/a.md', content: 'first\n' }),
      tool('write_file', { path: '/w/a.md', content: 'first\nsecond\n' }),
    ], '/w/a.md')
    expect(hunk).toMatchObject({ add: 2, del: 0 })
  })

  it('answers null with no matching tool call at all', () => {
    expect(hunkForFile([{ kind: 'think', text: 'thinking' }], '/w/a.md')).toBeNull()
  })

  it('answers null for arguments that do not parse as an object', () => {
    const step: Extract<NodeStep, { kind: 'tool' }> = { kind: 'tool', id: 'c1', name: 'write_file', args: 'not json', result: null, ok: null }
    expect(hunkForFile([step], '/w/a.md')).toBeNull()
  })
})
