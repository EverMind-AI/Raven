// @vitest-environment happy-dom
/* The transcript store's lane bookkeeping, driven directly: the numbering is
   what is under test and a rendered card would only show it second-hand. */

import { beforeEach, describe, expect, it } from 'vitest'

import * as store from './store'

beforeEach(() => {
  window.RavenShell = {
    T: (key: string) => key,
  } as unknown as typeof window.RavenShell
  window.DS = {
    transcript: { clean: (t: string) => t, okOf: () => true },
  } as unknown as typeof window.DS
  store._resetForTests()
})

/* A delegated stream is painted a slice at a time, and every slice's turns have
   to keep counting from where the last one stopped. Driven through the store
   rather than the DOM: the numbering is what is under test, and a card would
   only show it second-hand. */
describe('a delegated lane painted in slices', () => {
  const delivery = (path: string) => ({
    role: 'tool' as const,
    name: 'deliver_files',
    tool_call_id: `c-${path}`,
    text: 'ok',
    metadata: { raven_delivery: { files: [{ path, name: path }] } },
  })

  it('files each turn\'s deliveries under its own turn', () => {
    /* `history` counts turns from zero over what it is given, and a poll hands it
       only the newly appended rows -- so both turns landed on the same key and
       each card showed the other's file as well as its own. */
    store._resetForTests()
    const lane = store.newLane('agent:one', false)

    store.agentPaintLane(lane, {
      messages: [{ role: 'user', text: 'first job' }, delivery('first.md')],
      status: 'ok',
    }, { key: 'run-1' })
    store.agentPaintLane(lane, {
      messages: [
        { role: 'user', text: 'first job' }, delivery('first.md'),
        { role: 'user', text: 'second job' }, delivery('second.md'),
      ],
      status: 'ok',
    }, { key: 'run-1' })

    expect(store.deliveriesOf(lane, 1).map((r) => r.path)).toEqual(['first.md'])
    expect(store.deliveriesOf(lane, 2).map((r) => r.path)).toEqual(['second.md'])
  })

  it('starts a new stream from one again', () => {
    /* A different key is a different run in the same pane; its first turn is
       turn one, not a continuation of whatever was there before. */
    store._resetForTests()
    const lane = store.newLane('agent:one', false)

    store.agentPaintLane(lane, {
      messages: [{ role: 'user', text: 'first job' }, delivery('first.md')],
      status: 'ok',
    }, { key: 'run-1' })
    store.agentPaintLane(lane, {
      messages: [{ role: 'user', text: 'other job' }, delivery('other.md')],
      status: 'ok',
    }, { key: 'run-2' })

    expect(store.deliveriesOf(lane, 1).map((r) => r.path)).toEqual(['other.md'])
  })
})
