/** Tests for the session root and the path a delivery lookup compares on. */

import { describe, expect, it } from 'vitest'

import { absPath, relToWsRoot, wsSetRoot } from './root'

describe('the session root', () => {
  it('forgets the root when it is handed something that is not one', () => {
    wsSetRoot('/repo')
    wsSetRoot(undefined)
    expect(relToWsRoot('/repo/src/a.ts')).toBe(null)
  })

  it('strips the root, and only when the path is really under it', () => {
    wsSetRoot('/repo')
    expect(relToWsRoot('/repo/src/a.ts')).toBe('src/a.ts')
    expect(relToWsRoot('/repository/src/a.ts')).toBe(null)
  })

  /* A session can be rooted at the filesystem root -- an engine started by the
     desktop shell inherits it -- and a lone slash is a root, not an unknown
     one. */
  it('keeps a root that is only a slash', () => {
    wsSetRoot('/')
    expect(relToWsRoot('/out/a.md')).toBe('out/a.md')
    expect(relToWsRoot('/outside/a.md')).toBe('outside/a.md')
  })
})

describe('the path a delivery lookup compares on', () => {
  it('leaves a path that is already absolute alone', () => {
    wsSetRoot('/repo')
    expect(absPath('/repo/out/a.md')).toBe('/repo/out/a.md')
    expect(absPath('D:/repo/out/a.md')).toBe('D:/repo/out/a.md')
  })

  it('resolves a relative path against the session root', () => {
    wsSetRoot('/repo')
    expect(absPath('out/a.md')).toBe('/repo/out/a.md')
  })

  it('resolves a relative path against a root that is only a slash', () => {
    wsSetRoot('/')
    expect(absPath('out/a.md')).toBe('/out/a.md')
  })

  /* Nothing to resolve against: the root is learned from the session's own
     init bundle, and until it arrives a relative path is all the page has. */
  it('resolves nothing when the root is not known', () => {
    wsSetRoot('')
    expect(absPath('out/a.md')).toBe('out/a.md')
  })

  /* The registry keys what the gateway resolved, which on Windows carries
     backslashes; the comparison has to see one spelling. */
  it('gives the separators one shape', () => {
    wsSetRoot('D:\\repo')
    expect(absPath('D:\\repo\\out\\a.md')).toBe('D:/repo/out/a.md')
    expect(absPath('out\\a.md')).toBe('D:/repo/out/a.md')
  })

  /* A path the server resolves itself: the panel never rewrites it, and the
     gateway roots an upload against agent home rather than the session's
     directory. */
  it('leaves a home-relative path alone', () => {
    wsSetRoot('/repo')
    expect(absPath('~/notes.md')).toBe('~/notes.md')
  })
})
