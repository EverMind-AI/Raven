import { describe, expect, it } from 'vitest'

import { applyEdit, fromDelete, fromEdit, fromUnified, fromWrite, toUnified } from './hunks'

describe('diff hunk builders', () => {
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

  /* The mirror of the write above: the file's own lines, all taken out, and
     numbered on the old side because that is the side they were on. */
  it('turns a deleted file into all-del rows numbered on the old side', () => {
    const hunk = fromDelete('one\ntwo\nthree\n')
    expect(hunk).toEqual({
      add: 0,
      del: 3,
      rows: [['del', 'one', 1, null], ['del', 'two', 2, null], ['del', 'three', 3, null]],
    })
  })

  /* The runtime stores a removed file's line count, not its body, and counts
     an empty file as no lines at all -- so counting one here would make the
     same deletion read as -1 live and -0 after a reload. */
  it('counts an empty removed file as no lines rather than one', () => {
    expect(fromDelete('')).toEqual({ add: 0, del: 0, rows: [] })
  })

  it('keeps forty deleted lines and folds the rest without counting a trailing split', () => {
    const lines = Array.from({ length: 41 }, (_, i) => `line ${i + 1}`)
    const hunk = fromDelete(lines.join('\n') + '\n')
    expect(hunk.add).toBe(0)
    expect(hunk.del).toBe(41)
    expect(hunk.rows).toHaveLength(41)
    expect(hunk.rows[39]).toEqual(['del', 'line 40', 40, null])
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

describe('toUnified', () => {
  it('drops an edit hunk\'s folded context and anchors an unnumbered run at line 1', () => {
    const hunk = fromEdit(
      'a\nb\nc\nd\nold\ne\nf\ng\nh',
      'a\nb\nc\nd\nnew\ne\nf\ng\nh',
    )
    expect(toUnified('file.py', [hunk])).toBe([
      'diff --git a/file.py b/file.py',
      '@@ -1,7 +1,7 @@',
      ' b', ' c', ' d', '-old', '+new', ' e', ' f', ' g',
    ].join('\n'))
  })

  it('renders a pure write as -0,0 against the write cap, dropping the folded tail', () => {
    const lines = Array.from({ length: 41 }, (_, i) => `line ${i + 1}`)
    const hunk = fromWrite(lines.join('\n') + '\n')
    const out = toUnified('new.txt', [hunk])
    expect(out.split('\n').slice(0, 2)).toEqual(['diff --git a/new.txt b/new.txt', '@@ -0,0 +1,40 @@'])
    expect(out.split('\n')).toHaveLength(42)
    expect(out.split('\n')[41]).toBe('+line 40')
  })

  it('reuses a unified hunk\'s own header verbatim instead of recomputing it', () => {
    const hunk = fromUnified([
      '--- a/file', '+++ b/file', '@@ -2,2 +2,3 @@',
      ' same', '-old', '+new', '+extra',
    ])
    expect(toUnified('file', [hunk])).toBe([
      'diff --git a/file b/file',
      '@@ -2,2 +2,3 @@',
      ' same', '-old', '+new', '+extra',
    ].join('\n'))
  })

  it('gives each hunk in the list its own header, in order', () => {
    const first = fromUnified(['@@ -1,1 +1,1 @@', '-a', '+b'])
    const second = fromUnified(['@@ -9,1 +9,1 @@', '-c', '+d'])
    expect(toUnified('file', [first, second])).toBe([
      'diff --git a/file b/file',
      '@@ -1,1 +1,1 @@', '-a', '+b',
      '@@ -9,1 +9,1 @@', '-c', '+d',
    ].join('\n'))
  })

  it('is just the file header for a change with no hunks', () => {
    expect(toUnified('empty.txt', [])).toBe('diff --git a/empty.txt b/empty.txt')
  })
})

describe('applyEdit', () => {
  it('replaces the one occurrence, every occurrence with replace_all, and gives up otherwise', () => {
    expect(applyEdit('a\nb\n', 'a', 'A', false)).toBe('A\nb\n')
    expect(applyEdit('a\na\n', 'a', 'A', true)).toBe('A\nA\n')
    expect(applyEdit('a\na\n', 'a', 'A', false)).toBeNull()
    expect(applyEdit('a\nb\n', 'zzz', 'A', false)).toBeNull()
    expect(applyEdit('a\nb\n', '', 'A', true)).toBeNull()
  })
})
