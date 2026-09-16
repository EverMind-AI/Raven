// @vitest-environment happy-dom
/* The four path resolvers the workspace source answers with, and the one host
 * question the other three lean on. "Looks like a path" is not enough to make
 * one clickable -- a link that opens onto an error is worse than plain text --
 * so each of these is a rule about what is provable without a round trip.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  hostIsLocal, liveLinkTargetOf, livePathOf, relToWorkspace, relToWsRoot, setShortener, wsSetRoot,
} from './source'
import { islands } from '../../islands'

const local = (hostname: string): void => {
  vi.spyOn(globalThis, 'location', 'get').mockReturnValue({ hostname } as Location)
}

let changesBefore: typeof islands.workspace.changes

beforeEach(() => {
  changesBefore = islands.workspace.changes
  wsSetRoot('')
  setShortener((p) => p)
})

afterEach(() => {
  islands.workspace.changes = changesBefore
  vi.restoreAllMocks()
})

describe('a path relative to the workspace', () => {
  it('strips the workspace prefix, wherever the workspace itself lives', () => {
    expect(relToWorkspace('/home/a/.raven/workspace/notes.md')).toBe('notes.md')
    expect(relToWorkspace('/srv/workspace/deep/notes.md')).toBe('deep/notes.md')
  })

  /* An absolute path that is not under a workspace is not workspace-relative,
     and calling it one would point the viewer at the wrong file. */
  it('refuses an absolute path that names no workspace', () => {
    expect(relToWorkspace('/etc/hosts')).toBe(null)
    expect(relToWorkspace('~/notes.md')).toBe(null)
  })

  it('takes a relative path as it stands, without its leading dot', () => {
    expect(relToWorkspace('./notes.md')).toBe('notes.md')
    expect(relToWorkspace('deep/notes.md')).toBe('deep/notes.md')
  })
})

describe('a path relative to the session root', () => {
  /* The root comes from the session's own init bundle, so before a session is
     open there is nothing to shorten against. */
  it('answers nothing until a session root is known', () => {
    expect(relToWsRoot('/repo/src/a.ts')).toBe(null)
  })

  it('strips the root, and only when the path is really under it', () => {
    wsSetRoot('/repo/')
    expect(relToWsRoot('/repo/src/a.ts')).toBe('src/a.ts')
    expect(relToWsRoot('/repository/src/a.ts')).toBe(null)
    expect(relToWsRoot('/repo')).toBe(null)
  })

  it('forgets the root when it is handed something that is not one', () => {
    wsSetRoot('/repo')
    wsSetRoot(undefined)
    expect(relToWsRoot('/repo/src/a.ts')).toBe(null)
  })
})

describe('whether the gateway host is the reader own desktop', () => {
  it('is true only for a loopback address', () => {
    local('127.0.0.1')
    expect(hostIsLocal()).toBe(true)
    local('localhost')
    expect(hostIsLocal()).toBe(true)
    local('[::1]')
    expect(hostIsLocal()).toBe(true)
    local('raven.example.com')
    expect(hostIsLocal()).toBe(false)
  })
})

describe('a bare path in prose', () => {
  it('links a workspace path, relative to the workspace', () => {
    expect(livePathOf('/home/a/.raven/workspace/notes.md')).toBe('notes.md')
  })

  /* A line and column reference is how a tool names a place in a file; the
     file is what a click can open. */
  it('drops a line and column reference before resolving', () => {
    expect(livePathOf('/srv/workspace/a.ts:12:3')).toBe('a.ts')
  })

  it('refuses anything with whitespace in it, and the empty string', () => {
    expect(livePathOf('a file.md')).toBe(null)
    expect(livePathOf('   ')).toBe(null)
  })

  /* The second provable case: Raven touched that file this session, wherever
     it lives. Everything else stays plain text. */
  it('links a file this session changed, and nothing else', () => {
    islands.workspace.changes = () => [{ key: '/elsewhere/report.md' }] as ReturnType<typeof islands.workspace.changes>
    expect(livePathOf('/elsewhere/report.md')).toBe('/elsewhere/report.md')
    expect(livePathOf('/elsewhere/other.md')).toBe(null)
  })

  it('matches a changed file by the short name the panel shows it under', () => {
    islands.workspace.changes = () => [{ key: '/repo/src/a.ts' }] as ReturnType<typeof islands.workspace.changes>
    setShortener((p) => p.replace('/repo/', ''))
    expect(livePathOf('src/a.ts')).toBe('/repo/src/a.ts')
  })
})

describe('a markdown link target', () => {
  beforeEach(() => {
    local('127.0.0.1')
  })

  it('takes an absolute or home-relative file', () => {
    expect(liveLinkTargetOf('/repo/notes.md')).toEqual({ p: '/repo/notes.md', dir: false })
    expect(liveLinkTargetOf('~/notes.md')).toEqual({ p: '~/notes.md', dir: false })
  })

  /* Models write file:// out of habit; it is stripped rather than rejected. */
  it('strips a file:// scheme', () => {
    expect(liveLinkTargetOf('file:///repo/notes.md')).toEqual({ p: '/repo/notes.md', dir: false })
  })

  it('needs a separator: a bare word is not a path', () => {
    expect(liveLinkTargetOf('notes.md')).toBe(null)
    expect(liveLinkTargetOf('dir/notes.md')).toEqual({ p: 'dir/notes.md', dir: false })
  })

  it('refuses whitespace and the characters a url would carry', () => {
    expect(liveLinkTargetOf('/repo/a b.md')).toBe(null)
    expect(liveLinkTargetOf('/repo/<a>.md')).toBe(null)
  })

  /* Segments take any non-separator character: deliverables are routinely
     named in the reader's own language. */
  it('takes a segment that is not spelt in latin letters', () => {
    expect(liveLinkTargetOf('/repo/HANDOFF-x.md')?.p).toBe('/repo/HANDOFF-x.md')
    expect(liveLinkTargetOf('/repo/a%b!c.md')?.p).toBe('/repo/a%b!c.md')
  })

  it('reads a trailing slash, and a missing extension, as a folder', () => {
    expect(liveLinkTargetOf('/repo/src/')).toEqual({ p: '/repo/src', dir: true })
    expect(liveLinkTargetOf('/repo/src')).toEqual({ p: '/repo/src', dir: true })
  })

  /* A folder only reaches the host's file manager, which is only there when
     the gateway is this desktop. Anywhere else, plain text beats a dead link. */
  it('refuses a folder when the gateway is somebody else machine', () => {
    local('raven.example.com')
    expect(liveLinkTargetOf('/repo/src/')).toBe(null)
    expect(liveLinkTargetOf('/repo/notes.md')).toEqual({ p: '/repo/notes.md', dir: false })
  })
})
