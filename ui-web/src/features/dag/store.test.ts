/** Tests for what the dag store leaves behind for a reload to find. */

// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { fold, forget, releaseFold, run, saved, set, takeFold, touch, _resetForTests } from './store'

import type { DagRun } from './types'

const graph = (id: string, over: Partial<DagRun> = {}): DagRun => ({
  run_id: id,
  session: 'a',
  order: ['one'],
  nodes: new Map([
    ['one', { id: 'one', subagent: 'Researcher', depends_on: [], status: 'pending', started_at: null, ended_at: null }],
  ]),
  summary: null,
  done: false,
  folded: false,
  ...over,
})

beforeEach(() => {
  sessionStorage.clear()
  _resetForTests()
})

afterEach(() => {
  _resetForTests()
  sessionStorage.clear()
})

describe('what a reload finds', () => {
  it('notes the run a conversation is watching, and nothing about its nodes', () => {
    set('a', graph('r1'))

    expect(saved('a')).toEqual({ run: 'r1', folded: false })
    /* The statuses are deliberately absent: they come from `dag.get` on the way
       back in, because a stored copy would resume the run frozen at the reload. */
    expect(JSON.stringify(sessionStorage.getItem('raven.gui.view.dag'))).not.toContain('pending')
  })

  it('files each conversation under its own key', () => {
    set('a', graph('r1'))
    set('b', graph('r2'))

    expect(saved('a')).toEqual({ run: 'r1', folded: false })
    expect(saved('b')).toEqual({ run: 'r2', folded: false })
  })

  it('records a fold the reader asked for', () => {
    set('a', graph('r1'))

    fold('a', true)

    expect(saved('a')).toEqual({ run: 'r1', folded: true })
  })

  it('records the fold a finished run gives itself', () => {
    /* `dag.run_completed` folds the sheet by writing the flag on the run it
       holds and calling touch() -- it does not go through fold(). A note taken
       only in fold() would come back unfolded on every completed run. */
    const d = graph('r1')
    set('a', d)

    d.folded = true
    d.done = true
    touch()

    expect(saved('a')).toEqual({ run: 'r1', folded: true })
  })

  it('leaves nothing behind when the sheet is closed', () => {
    /* The difference between closing a sheet and switching away from one: the
       rack detaches the second and keeps it, and this must not bring the first
       one back on the next reload. */
    set('a', graph('r1'))

    forget('a')

    expect(saved('a')).toBeNull()
    expect(run('a')).toBeNull()
  })

  it('closing one sheet leaves another conversation note alone', () => {
    set('a', graph('r1'))
    set('b', graph('r2'))

    forget('a')

    expect(saved('a')).toBeNull()
    expect(saved('b')).toEqual({ run: 'r2', folded: false })
  })

  it('has nothing to say about a conversation that never had a sheet', () => {
    expect(saved('nobody')).toBeNull()
  })
})

describe('a fold taken on the reader behalf', () => {
  it('is not still claimed against the run that replaces it', () => {
    /* The claim is about one run under one key. Replace the run while the
       question still stands and the claim outlives what it described: the new
       graph is never folded for the question, because `takeFold` sees the key
       as already taken. */
    set('a', graph('r1'))
    takeFold('a')
    expect(run('a')!.folded).toBe(true)

    set('a', graph('r2'))
    takeFold('a')

    expect(run('a')!.folded).toBe(true)
  })

  it('is not still claimed after the sheet is dismissed', () => {
    /* `forget` means the run this claim was about is gone. Left behind, the key
       stays claimed and the next run to appear under it never steps aside. */
    set('a', graph('r1'))
    takeFold('a')
    forget('a')

    set('a', graph('r2'))
    takeFold('a')

    expect(run('a')!.folded).toBe(true)
  })

  it('does not unfold a run it never folded', () => {
    /* The other half: with the claim dropped, the release must not reach into a
       run it has no claim on. */
    set('a', graph('r1'))
    takeFold('a')
    set('a', graph('r2'))
    fold('a', true)

    releaseFold('a')

    expect(run('a')!.folded).toBe(true)
  })
})
