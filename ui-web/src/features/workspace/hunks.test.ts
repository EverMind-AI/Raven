import { describe, expect, it } from 'vitest'

import { fromEdit, fromUnified, fromWrite } from './hunks'

describe('workspace hunk builders', () => {
  it('folds distant unchanged edit context around the changed lines', () => {
    const hunk = fromEdit(
      'a\nb\nc\nd\nold\ne\nf\ng\nh',
      'a\nb\nc\nd\nnew\ne\nf\ng\nh',
    )
    expect(hunk).toEqual({
      add: 1,
      del: 1,
      rows: [
        ['gap', ['a']], ['ctx', 'b'], ['ctx', 'c'], ['ctx', 'd'],
        ['del', 'old'], ['add', 'new'],
        ['ctx', 'e'], ['ctx', 'f'], ['ctx', 'g'], ['gap', ['h']],
      ],
    })
  })

  it('keeps forty write lines and folds the rest without counting a trailing split', () => {
    const lines = Array.from({ length: 41 }, (_, i) => `line ${i + 1}`)
    const hunk = fromWrite(lines.join('\n') + '\n')
    expect(hunk.add).toBe(41)
    expect(hunk.del).toBe(0)
    expect(hunk.rows).toHaveLength(41)
    expect(hunk.rows[39]).toEqual(['add', 'line 40', null, 40])
    expect(hunk.rows[40]).toEqual(['gap', ['line 41']])
  })

  it('drops file headers and numbers unified diff rows from the hunk header', () => {
    const hunk = fromUnified([
      '--- a/file', '+++ b/file', '@@ -2,2 +2,3 @@',
      ' same', '-old', '+new', '+extra',
    ])
    expect(hunk).toEqual({
      add: 2,
      del: 1,
      rows: [
        ['hunk', '@@ -2,2 +2,3 @@'],
        ['ctx', 'same', 2, 2],
        ['del', 'old', 3, null],
        ['add', 'new', null, 3],
        ['add', 'extra', null, 4],
      ],
    })
  })
})
