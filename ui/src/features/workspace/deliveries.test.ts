/** Tests for the session's delivery registry and how it survives a park. */

// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import { setCurrent } from '../../shell/session'
import * as deliveries from './deliveries'
import * as workspace from './store'

import type { WorkspaceSource } from './types'

const manifest = (files: Array<Record<string, unknown>>): unknown => ({ raven_delivery: { files } })

afterEach(() => {
  deliveries.restore([])
})

describe('delivery registry', () => {
  it('reads a manifest and lists the newest turn first, manifest order within it', () => {
    deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md', title: 'A', size: 12 }]))
    deliveries.record(2, manifest([
      { path: '/w/b.csv', name: 'b.csv', size: 3 },
      { path: '/w/c.pdf', name: 'c.pdf', size: 4 },
    ]))

    expect(deliveries.list().map((row) => row.name)).toEqual(['b.csv', 'c.pdf', 'a.md'])
    expect(deliveries.ofTurn(1).map((row) => row.title)).toEqual(['A'])
    /* No title given: the file's own name is the one thing always there. */
    expect(deliveries.ofTurn(2)[0]?.title).toBe('b.csv')
    expect(deliveries.ofTurn(2)[0]?.ext).toBe('csv')
  })

  it('shows a re-delivered file once, at the turn that delivered it again', () => {
    deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md', size: 12 }]))
    deliveries.record(3, manifest([{ path: '/w/a.md', name: 'a.md', size: 40 }]))

    expect(deliveries.count()).toBe(1)
    expect(deliveries.list().map((row) => row.turn)).toEqual([3])
    expect(deliveries.byPath('/w/a.md')?.size).toBe(40)
    expect(deliveries.byPath('/w/a.md')?.turn).toBe(3)
  })

  /* The shelf collapses a path to its newest delivery; the turn that delivered
     the earlier copy still has to be able to say what IT delivered, because
     that is what its own card in the transcript renders -- and on a reload the
     whole history replays before anything is drawn. */
  it('leaves an earlier turn holding what that turn delivered', () => {
    deliveries.record(2, manifest([{ path: '/w/r.md', name: 'r.md', title: 'v1' }]))
    deliveries.record(5, manifest([{ path: '/w/r.md', name: 'r.md', title: 'v2' }]))

    expect(deliveries.ofTurn(2).map((row) => row.title)).toEqual(['v1'])
    expect(deliveries.ofTurn(5).map((row) => row.title)).toEqual(['v2'])
    expect(deliveries.list().map((row) => row.title)).toEqual(['v2'])
  })

  it('re-delivering within one turn refreshes that turn\'s row rather than doubling it', () => {
    deliveries.record(4, manifest([{ path: '/w/r.md', name: 'r.md', size: 10 }]))
    deliveries.record(4, manifest([{ path: '/w/r.md', name: 'r.md', size: 99 }]))

    expect(deliveries.ofTurn(4).map((row) => row.size)).toEqual([99])
  })

  it('takes the runtime word that a replayed file is gone, and the viewer\'s later', () => {
    deliveries.record(1, manifest([
      { path: '/w/a.md', name: 'a.md', missing: true },
      { path: '/w/b.md', name: 'b.md' },
    ]))
    expect(deliveries.byPath('/w/a.md')?.missing).toBe(true)
    expect(deliveries.byPath('/w/b.md')?.missing).toBe(false)

    deliveries.markMissing('/w/b.md')
    expect(deliveries.byPath('/w/b.md')?.missing).toBe(true)
  })

  /* One file, one answer: the turn cards must not still show it as present
     while the shelf shows it gone. */
  it('marks every turn that delivered the file, not just the newest', () => {
    deliveries.record(1, manifest([{ path: '/w/r.md', name: 'r.md' }]))
    deliveries.record(4, manifest([{ path: '/w/r.md', name: 'r.md' }]))

    deliveries.markMissing('/w/r.md')

    expect(deliveries.ofTurn(1)[0]?.missing).toBe(true)
    expect(deliveries.ofTurn(4)[0]?.missing).toBe(true)
  })

  it('notifies a subscriber when a delivery lands, and not for an empty manifest', () => {
    let seen = 0
    const stop = deliveries.subscribe(() => { seen += 1 })
    deliveries.record(1, manifest([]))
    deliveries.record(1, { raven_delivery: { files: [{ name: 'no path' }] } })
    expect(seen).toBe(0)
    deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md' }]))
    expect(seen).toBe(1)
    stop()
    deliveries.record(2, manifest([{ path: '/w/b.md', name: 'b.md' }]))
    expect(seen).toBe(1)
  })

  /* The registry is what answers after a reconnect: the transcript comes back
     from disk, and a turn still in flight was never written to it. */
  it('fills the shelf from the gateway registry, newest first', () => {
    deliveries.seed([
      { path: '/w/old.md', name: 'old.md', title: 'Older', size: 10, created_at: '2026-08-01T00:00:00Z' },
      { path: '/w/new.md', name: 'new.md', title: 'Newer', size: 20, created_at: '2026-08-02T00:00:00Z' },
    ])

    expect(deliveries.list().map((row) => row.title)).toEqual(['Newer', 'Older'])
    /* No turn to name: the registry knows the conversation delivered it, not
       where in the reading that was. */
    expect(deliveries.list()[0]?.turn).toBeNull()
    expect(deliveries.count()).toBe(2)
  })

  it('lets a turn keep the path the registry also has, and adds only the rest', () => {
    deliveries.record(4, manifest([{ path: '/w/a.md', name: 'a.md', title: 'from the turn' }]))
    deliveries.seed([
      { path: '/w/a.md', name: 'a.md', title: 'from the registry' },
      { path: '/w/b.md', name: 'b.md', title: 'only in the registry' },
    ])

    expect(deliveries.list().map((row) => row.title)).toEqual(['from the turn', 'only in the registry'])
    expect(deliveries.ofTurn(4).map((row) => row.title)).toEqual(['from the turn'])
    expect(deliveries.count()).toBe(2)
  })

  it('seeds the same registry twice without doubling it', () => {
    const files = [{ path: '/w/a.md', name: 'a.md' }]
    deliveries.seed(files)
    deliveries.seed(files)

    expect(deliveries.count()).toBe(1)
    expect(deliveries.list()).toHaveLength(1)
  })

  it('ignores an answer that is not a list', () => {
    deliveries.seed(null)
    deliveries.seed({ files: [] })
    expect(deliveries.list()).toEqual([])
  })

  /* The one way these rows can vanish without anybody noticing: a conversation
     switched away from is restored from the parked snapshot, never from a
     replay, so a snapshot that does not carry them loses the live turn's. */
  it('rides the workspace snapshot through a park and back', () => {
    deliveries.record(2, manifest([{ path: '/w/a.md', name: 'a.md', title: 'A' }]))
    const parked = workspace.snapshot()

    workspace.restore({ changes: [], urls: [], file: null, turn: 0, unseen: 0, deliveries: [] })
    expect(deliveries.count()).toBe(0)

    workspace.restore(parked)
    expect(deliveries.list().map((row) => row.title)).toEqual(['A'])
  })

  it('asks the source for the conversation, once, and takes what it answers', async () => {
    const asked: string[] = []
    window.DS = {
      workspace: {
        shortPath: (p: string) => p,
        hostPlatform: () => 'mac',
        deliverables: async (key: string) => {
          asked.push(key)
          return [{ path: '/w/a.md', name: 'a.md', title: 'recovered' }]
        },
      } as WorkspaceSource,
    }

    setCurrent('tui:s1')
    await workspace.loadDeliveries('tui:s1')

    expect(asked).toEqual(['tui:s1'])
    expect(deliveries.list().map((row) => row.title)).toEqual(['recovered'])
    setCurrent(null)
    window.DS = undefined
  })

  /* The reader can click another conversation while the answer is in flight,
     and the registry it would land in is that one's. */
  it('drops an answer for a conversation the reader has already left', async () => {
    let release: (rows: unknown) => void = () => {}
    setCurrent('tui:a')
    window.DS = {
      workspace: {
        shortPath: (p: string) => p,
        hostPlatform: () => 'mac',
        deliverables: () => new Promise((resolve) => { release = resolve }),
      } as WorkspaceSource,
    }

    const pending = workspace.loadDeliveries('tui:a')
    /* The switch: B's own desk, and B's own registry. */
    setCurrent('tui:b')
    release([{ path: '/a/secret.md', name: 'secret.md', title: "A's file" }])
    await pending

    expect(deliveries.list()).toEqual([])
    setCurrent(null)
    window.DS = undefined
  })

  /* A source without the verb is an older gateway; a source that throws is one
     that is there and unhappy. Neither may cost the shelf what it already has. */
  it('keeps what the turns gave it when the registry cannot be read', async () => {
    deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md', title: 'from the turn' }]))
    window.DS = {
      workspace: {
        shortPath: (p: string) => p,
        hostPlatform: () => 'mac',
        deliverables: async () => { throw new Error('nope') },
      } as WorkspaceSource,
    }

    await workspace.loadDeliveries('tui:s1')
    window.DS = { workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac' } as WorkspaceSource }
    await workspace.loadDeliveries('tui:s1')

    expect(deliveries.list().map((row) => row.title)).toEqual(['from the turn'])
    window.DS = undefined
  })

  it('is emptied by a workspace reset, which is what a session switch does', () => {
    deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md' }]))
    workspace.reset()
    expect(deliveries.list()).toEqual([])
  })
})
