/** Tests for the reload-scoped view store: versioning, the cap, and where it writes. */

// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { slot } from './persist'

interface Note {
  run: string
}

const KEY = 'raven.gui.view.probe'

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
  sessionStorage.clear()
  localStorage.clear()
})

describe('view slot', () => {
  it('keeps what was written under a session key, and only that key', () => {
    const s = slot<Note>('probe', 1)

    s.write('s1', { run: 'a' })
    s.write('s2', { run: 'b' })

    expect(s.read('s1')).toEqual({ run: 'a' })
    expect(s.read('s2')).toEqual({ run: 'b' })
    expect(s.read('s3')).toBeNull()
  })

  it('writes to sessionStorage, so a fresh tab inherits nothing', () => {
    /* The whole reason this is not localStorage: the layout must survive a
       reload of THIS tab and reach no other. A localStorage write would also
       reach a second window on the same gateway, where it would fight that
       window's own layout. */
    slot<Note>('probe', 1).write('s1', { run: 'a' })

    expect(sessionStorage.getItem(KEY)).toContain('"run":"a"')
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('drops everything a bumped version wrote', () => {
    slot<Note>('probe', 1).write('s1', { run: 'a' })

    expect(slot<Note>('probe', 2).read('s1')).toBeNull()
  })

  it('drops a stored value that is not a box at all', () => {
    sessionStorage.setItem(KEY, 'not json{')

    expect(slot<Note>('probe', 1).read('s1')).toBeNull()
  })

  it('a write after a version bump replaces the old box rather than joining it', () => {
    const old = slot<Note>('probe', 1)
    old.write('s1', { run: 'a' })

    const next = slot<Note>('probe', 2)
    next.write('s2', { run: 'b' })

    expect(next.read('s2')).toEqual({ run: 'b' })
    /* Not merely unreadable through the new version: gone, so a third bump back
       to 1 cannot resurrect a shape nothing understands any more. */
    expect(old.read('s1')).toBeNull()
    expect(sessionStorage.getItem(KEY)).not.toContain('"run":"a"')
  })

  it('evicts the coldest session past the cap', () => {
    const s = slot<Note>('probe', 1, 2)
    vi.spyOn(Date, 'now').mockReturnValue(1000)
    s.write('cold', { run: 'a' })
    vi.spyOn(Date, 'now').mockReturnValue(2000)
    s.write('warm', { run: 'b' })
    vi.spyOn(Date, 'now').mockReturnValue(3000)

    s.write('new', { run: 'c' })

    expect(s.read('cold')).toBeNull()
    expect(s.read('warm')).toEqual({ run: 'b' })
    expect(s.read('new')).toEqual({ run: 'c' })
  })

  it('re-writing a key keeps it out of the eviction, however old the key is', () => {
    const s = slot<Note>('probe', 1, 2)
    vi.spyOn(Date, 'now').mockReturnValue(1000)
    s.write('first', { run: 'a' })
    vi.spyOn(Date, 'now').mockReturnValue(2000)
    s.write('second', { run: 'b' })
    /* The reader came back to the first conversation and moved a window: the
       entry is old, the activity is not. */
    vi.spyOn(Date, 'now').mockReturnValue(3000)
    s.write('first', { run: 'a2' })
    vi.spyOn(Date, 'now').mockReturnValue(4000)

    s.write('third', { run: 'c' })

    expect(s.read('first')).toEqual({ run: 'a2' })
    expect(s.read('second')).toBeNull()
  })

  it('forgetting the last key leaves no dead entry behind', () => {
    const s = slot<Note>('probe', 1)
    s.write('s1', { run: 'a' })

    s.forget('s1')

    expect(s.read('s1')).toBeNull()
    expect(sessionStorage.getItem(KEY)).toBeNull()
  })

  it('forgets one key without touching its neighbour', () => {
    const s = slot<Note>('probe', 1)
    s.write('s1', { run: 'a' })
    s.write('s2', { run: 'b' })

    s.forget('s1')

    expect(s.read('s1')).toBeNull()
    expect(s.read('s2')).toEqual({ run: 'b' })
  })

  it('survives storage that refuses to be written', () => {
    /* Private mode, or over quota. A layout that cannot be stored is a layout
       the next reload does without -- not a broken page. */
    const s = slot<Note>('probe', 1)
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })

    expect(() => s.write('s1', { run: 'a' })).not.toThrow()
  })

  it('survives storage that refuses to be read', () => {
    const s = slot<Note>('probe', 1)
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })

    expect(s.read('s1')).toBeNull()
  })

  it('two names do not share a box', () => {
    slot<Note>('probe', 1).write('s1', { run: 'a' })

    expect(slot<Note>('other', 1).read('s1')).toBeNull()
  })
})
