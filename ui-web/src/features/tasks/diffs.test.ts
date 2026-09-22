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

  /* A node has no delete tool: the file goes under an `exec` the lane never
     sees the body of, so the only account of what was lost is the node's own
     last write of that path. */
  it('closes a removed file with every line of the node\'s last write of it', () => {
    const hunks = hunksForFile([
      tool('write_file', { path: '/w/a.md', content: 'one\ntwo\n' }),
      tool('write_file', { path: '/w/a.md', content: 'one\ntwo\nthree\n' }),
    ], '/w/a.md', 'delete')

    expect(hunks).toHaveLength(3)
    expect(hunks[2]).toMatchObject({ add: 0, del: 3 })
    expect(hunks[2]?.rows.map((r) => r[1])).toEqual(['one', 'two', 'three'])
  })

  /* The body a deletion closes on is the file as the node LEFT it: a write
     followed by an edit is the edited text, not the text the write put down. */
  it('closes a removed file on the text its later edits left, not on the last write', () => {
    const hunks = hunksForFile([
      tool('write_file', { path: '/w/a.md', content: 'old\n' }),
      tool('edit_file', { path: '/w/a.md', old_text: 'old', new_text: 'new' }),
    ], '/w/a.md', 'delete')

    expect(hunks).toHaveLength(3)
    expect(hunks[2]?.rows.map((r) => r[1])).toEqual(['new'])
  })

  /* An edit the followed body cannot take -- the tool matched loosely, or the
     text was never there -- means the final contents are not known; a body that
     might be wrong is worse than none. */
  it('closes with no body when an edit cannot be applied to what was followed', () => {
    const hunks = hunksForFile([
      tool('write_file', { path: '/w/a.md', content: 'old\n' }),
      tool('edit_file', { path: '/w/a.md', old_text: 'elsewhere', new_text: 'new' }),
    ], '/w/a.md', 'delete')

    expect(hunks).toHaveLength(2)
  })

  it('leaves a removed file the node never wrote with no hunk to close', () => {
    const hunks = hunksForFile(
      [tool('edit_file', { path: '/w/a.py', old_text: 'a\n', new_text: 'b\n' })], '/w/a.py', 'delete',
    )
    expect(hunks).toHaveLength(1)
    expect(hunks[0]).toMatchObject({ add: 1, del: 1 })
  })

  it('appends nothing for a file the node only wrote', () => {
    const hunks = hunksForFile([tool('write_file', { path: '/w/a.md', content: 'one\n' })], '/w/a.md', 'write')
    expect(hunks).toHaveLength(1)
  })

  it('answers nothing with no matching tool call at all', () => {
    expect(hunksForFile([{ kind: 'think', text: 'thinking' }], '/w/a.md')).toEqual([])
  })

  it('answers nothing for arguments that do not parse as an object', () => {
    const step: Extract<NodeStep, { kind: 'tool' }> = { kind: 'tool', id: 'c1', name: 'write_file', args: 'not json', result: null, ok: null }
    expect(hunksForFile([step], '/w/a.md')).toEqual([])
  })
})
