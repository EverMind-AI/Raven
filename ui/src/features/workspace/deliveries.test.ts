/** Tests for the session's delivery registry and how it survives a park. */

import { afterEach, describe, expect, it } from 'vitest'

import * as deliveries from './deliveries'
import * as workspace from './store'

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

  it('is emptied by a workspace reset, which is what a session switch does', () => {
    deliveries.record(1, manifest([{ path: '/w/a.md', name: 'a.md' }]))
    workspace.reset()
    expect(deliveries.list()).toEqual([])
  })
})
