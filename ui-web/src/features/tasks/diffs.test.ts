import { describe, expect, it } from 'vitest'

import { hunksForFile } from './diffs'

import type { NodeStep } from './types'

const tool = (name: string, args: object): Extract<NodeStep, { kind: 'tool' }> =>
  ({ kind: 'tool', id: 'c1', name, args: JSON.stringify(args), result: 'ok', ok: true })

describe('hunksForFile', () => {
  it('reads a write as all-added, the same convention record.ts draws a plain write with', () => {
    const hunks = hunksForFile([tool('write_file', { path: '/w/a.md', content: 'one\ntwo\n' })], '/w/a.md')
    expect(hunks).toHaveLength(1)
    expect(hunks[0]).toMatchObject({ add: 2, del: 0 })
  })

  it('reads an edit as the real before/after slice', () => {
    const hunks = hunksForFile(
      [tool('edit_file', { path: '/w/a.py', old_text: 'a\nb\nc\n', new_text: 'a\nB\nc\n' })],
      '/w/a.py',
    )
    expect(hunks).toHaveLength(1)
    expect(hunks[0]).toMatchObject({ add: 1, del: 1 })
  })

  it('ignores a step against a different path', () => {
    expect(hunksForFile([tool('write_file', { path: '/w/other.md', content: 'x' })], '/w/a.md')).toEqual([])
  })

  it('matches a workspace-relative record against an absolute tool path, and the reverse', () => {
    const write = tool('write_file', { path: '/w/work/a.md', content: 'one\n' })
    expect(hunksForFile([write], 'work/a.md')).toHaveLength(1)
    expect(hunksForFile([tool('write_file', { path: 'work/a.md', content: 'one\n' })], '/w/work/a.md')).toHaveLength(1)
  })

  it('does not let a bare basename match a different directory', () => {
    expect(hunksForFile([tool('write_file', { path: '/w/other/a.md', content: 'x' })], 'work/a.md')).toEqual([])
    expect(hunksForFile([tool('write_file', { path: '/w/xwork/a.md', content: 'x' })], 'work/a.md')).toEqual([])
  })

  it('keeps every touch of the path in call order, so the counts sum to the folded chip', () => {
    const hunks = hunksForFile([
      tool('edit_file', { path: '/w/a.py', old_text: 'a\nb\n', new_text: 'a\nB\n' }),
      tool('write_file', { path: '/w/other.md', content: 'x' }),
      tool('edit_file', { path: '/w/a.py', old_text: 'a\nB\n', new_text: 'a\nB\nc\n' }),
    ], '/w/a.py')
    expect(hunks.map((h) => [h.add, h.del])).toEqual([[1, 1], [1, 0]])
  })

  it('answers nothing with no matching tool call at all', () => {
    expect(hunksForFile([{ kind: 'think', text: 'thinking' }], '/w/a.md')).toEqual([])
  })

  it('answers nothing for arguments that do not parse as an object', () => {
    const step: Extract<NodeStep, { kind: 'tool' }> = { kind: 'tool', id: 'c1', name: 'write_file', args: 'not json', result: null, ok: null }
    expect(hunksForFile([step], '/w/a.md')).toEqual([])
  })
})
