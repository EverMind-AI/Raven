import { describe, expect, it } from 'vitest'

import { argPath, firstErrLine, phraseOf, rawVerb, shortArg, splitMcp, verbIngOf, verbOf } from './actVerbs'

describe('splitMcp', () => {
  it('splits an mcp_<server>_<tool> name into its server and the bare tool', () => {
    expect(splitMcp('mcp_github_search_issues')).toEqual({ srv: 'github', bare: 'search_issues' })
  })

  it('leaves a non-mcp name bare, with no server', () => {
    expect(splitMcp('write_file')).toEqual({ srv: null, bare: 'write_file' })
  })
})

describe('verbOf / verbIngOf', () => {
  it('reads the catalogue for a known tool, done and in flight', () => {
    expect(verbOf('write_file')).toBe('wrote')
    expect(verbIngOf('write_file')).toBe('writing')
    expect(verbOf('read_file')).toBe('read')
    expect(verbOf('exec')).toBe('ran command')
    expect(verbOf('web_fetch')).toBe('read page')
  })

  it('falls back to the bare tool name, underscores turned to spaces, for an MCP tool the catalogue does not carry', () => {
    const { bare } = splitMcp('mcp_github_search_issues')
    expect(rawVerb('mcp_github_search_issues')).toBe('search issues')
    expect(verbOf(bare)).toBe('search issues')
  })
})

describe('phraseOf', () => {
  it('folds repeats into the catalogue\'s counted phrase and keeps a single call as its verb alone', () => {
    expect(phraseOf([{ name: 'write_file' }, { name: 'write_file' }, { name: 'read_file' }]))
      .toBe('wrote 2 files · read')
  })
})

describe('firstErrLine', () => {
  it('picks the first error-shaped line over the first line', () => {
    expect(firstErrLine('starting up\nError: boom\nmore output')).toBe('Error: boom')
  })

  it('falls back to the first line when nothing looks like an error', () => {
    expect(firstErrLine('all good\nsecond line')).toBe('all good')
  })

  it('answers empty for no result', () => {
    expect(firstErrLine(null)).toBe('')
  })
})

describe('argPath', () => {
  it('reads the first path-shaped key present', () => {
    expect(argPath({ file_path: '/a.py' })).toBe('/a.py')
    expect(argPath({ path: '/b.py', file: '/c.py' })).toBe('/b.py')
    expect(argPath({})).toBe('')
  })
})

describe('shortArg', () => {
  it('clamps long text with an ellipsis', () => {
    expect(shortArg('a'.repeat(50), 10)).toBe('a'.repeat(9) + '…')
  })

  it('collapses internal whitespace', () => {
    expect(shortArg('a\n\tb   c')).toBe('a b c')
  })
})
