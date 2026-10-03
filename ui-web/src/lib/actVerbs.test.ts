import { describe, expect, it } from 'vitest'

import { actName, argPath, firstErrLine, phraseOf, rawVerb, shortArg, splitMcp, verbIngOf, verbOf } from './actVerbs'

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

/* Seen live: a turn that looked at its settings, changed one and checked the
   plugins read as "plugin · raven config ×2", "raven config", "raven config". */
describe('actName', () => {
  it('words a bundled tool by what the call did, and leaves every other tool alone', () => {
    expect(actName('raven_config', { action: 'get', path: 'tools' })).toBe('raven_config_read')
    expect(actName('raven_config', '{"action": "set", "path": "a"}')).toBe('raven_config_change')
    expect(actName('raven_config', { action: 'restart' })).toBe('raven_config_restart')
    expect(actName('plugin', { action: 'list' })).toBe('plugin_read')
    expect(actName('plugin', { action: 'authorize', name: 'github' })).toBe('plugin_connect')
    expect(actName('raven_config', {})).toBe('raven_config')
    expect(actName('read_file', { action: 'get' })).toBe('read_file')
    expect(verbOf(actName('raven_config', { action: 'describe' }))).toBe('checked settings')
    expect(verbIngOf(actName('plugin', { action: 'connect' }))).toBe('connecting a plugin')
  })

  it('folds a run of them by kind of action, not by tool', () => {
    expect(phraseOf([
      { name: 'raven_config', args: { action: 'describe' } },
      { name: 'raven_config', args: { action: 'get', path: 'tools' } },
      { name: 'raven_config', args: { action: 'set', path: 'tools.exec.timeout' } },
      { name: 'plugin', args: { action: 'list' } },
    ])).toBe('checked settings 2 times · changed settings · checked plugins')
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
