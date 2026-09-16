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

/* ---- what the panel records, and how it counts turns -------------------- */
/* Both still live in the page layer (src/legacy/demo/100-workspace.js) and
   neither was driven before. Characterisation ahead of the rewrite that moves
   the bookkeeping into this feature. */

interface Shared {
  changes: Array<Record<string, unknown>>
  urls: Array<Record<string, unknown>>
  file: string | null
  turn: number
  unseen: number
}

async function panel() {
  const { loadPart, looseQuery } = await import('../../../scripts/legacy-part.mjs')
  const calls: unknown[][] = []
  const shared: Shared = { changes: [], urls: [], file: null, turn: 0, unseen: 0 }
  const part = await loadPart(() => import('../../legacy/demo/100-workspace.js'), {
    fakes: {
      'demo/010-kernel.js': {
        $: looseQuery(),
        T: (key: string) => key,
        HOST_PLATFORM: 'mac',
      },
      'demo/110-subagents.js': {
        wsOnTool: (name: string, args: unknown, replay: boolean) => calls.push(['tool', name, args, replay]),
        wsOnToolDone: (name: string, _args: unknown, _ok: boolean, _preview: string, _took: unknown, diff: string) =>
          calls.push(['done', name, diff]),
      },
      'src/shell/toast': { show: () => {} },
    },
    islands: {
      workspace: {
        shared: () => shared,
        hunkFromEdit: () => ({ add: 1, del: 0, rows: [] }),
        hunkFromWrite: () => ({ add: 1, del: 0, rows: [] }),
        hunkFromUnified: () => ({ add: 1, del: 0, rows: [] }),
        notifyDesk: () => {},
        reset: () => {},
      },
      subagents: { reset: () => {} },
    },
  })
  part.install()
  /* The path shortener is the workspace source's, which the live part installs
     -- the offline page reads the same one now, so nothing registers a
     fixture stand-in for it any more. Only the one verb the record needs. */
  const { sources } = await import('../../state/sources')
  sources.workspace = { shortPath: (path: string) => path } as unknown as NonNullable<typeof sources.workspace>
  return { part, shared, calls }
}

describe('rebuilding the panel from a stored conversation', () => {
  it('counts a turn per delegated result and per user message with text, and nothing else', async () => {
    /* A live client advances on turn.started; without the same step on replay a
       reloaded session files the delegated reaction's files under its parent's
       turn. An origin-only entry (cron, sentinel) opens no turn either way. */
    const { part, shared } = await panel()

    part.wsOnHistory([
      { role: 'user', text: 'first ask' },
      { role: 'user', delegated: { label: 'qc' } },
      { role: 'user', text: '   ' },
      { role: 'user' },
      { role: 'assistant', text: 'an answer' },
    ])

    expect(shared.turn).toBe(2)
  })

  it('replays each stored call with its arguments, and swaps in the stored diff', async () => {
    const { part, calls } = await panel()

    part.wsOnHistory([
      { role: 'assistant', tool_calls: [
        { id: 'c1', name: 'edit_file', arguments: '{"path":"a.py"}' },
        { id: 'c2', name: 'exec', arguments: 'not json' },
      ] },
      { role: 'tool', tool_call_id: 'c1', diff: '@@ -1 +1 @@' },
    ])

    expect(calls).toEqual([
      ['tool', 'edit_file', { path: 'a.py' }, true],
      ['done', 'edit_file', '@@ -1 +1 @@'],
    ])
  })

  it('marks everything it restored as already read', async () => {
    /* Nothing counts as unread: none of it arrived while the reader was away. */
    const { part, shared } = await panel()
    shared.changes.push({ key: 'a.py', seen: false, turn: 0 })
    shared.urls.push({ url: 'https://example.com', at: 'just now' })

    part.wsOnHistory([])

    expect(shared.changes[0]!.seen).toBe(true)
    expect(shared.urls[0]!.at).toBe('gui.ws.turn_earlier')
    expect(shared.unseen).toBe(0)
  })
})

describe('recording a change', () => {
  it('keeps one row per path per turn, adding up its hunks', async () => {
    /* Five edits to one file is one changed file with five hunks, which is how
       a person thinks about it. */
    const { part, shared } = await panel()

    part.wsRecordChange('/w/a.py', 'edit', { add: 2, del: 1 })
    part.wsRecordChange('/w/a.py', 'write', { add: 3, del: 0 })

    expect(shared.changes).toHaveLength(1)
    const row = shared.changes[0] as { add: number; del: number; hunks: unknown[]; kind: string; name: string }
    expect(row.add).toBe(5)
    expect(row.del).toBe(1)
    expect(row.hunks).toHaveLength(2)
    /* A write anywhere in the row wins the kind. */
    expect(row.kind).toBe('write')
    expect(row.name).toBe('a.py')
  })

  it('folds the row it opened for itself when the next one arrives', async () => {
    /* The newest change is the one you came here to read, so it arrives
       expanded -- and `auto` marks it as opened by us, so the next arrival
       folds it back without touching a row the reader opened on purpose. */
    const { part, shared } = await panel()

    part.wsRecordChange('/w/a.py', 'edit', { add: 1, del: 0 })
    const first = shared.changes[0] as { open: boolean; auto: boolean }
    expect(first).toMatchObject({ open: true, auto: true })

    part.wsRecordChange('/w/b.py', 'edit', { add: 1, del: 0 })

    expect(shared.changes.map((c) => c.key)).toEqual(['/w/b.py', '/w/a.py'])
    expect(first).toMatchObject({ open: false, auto: false })
    expect(shared.changes[0]).toMatchObject({ open: true, auto: true })
  })
})
