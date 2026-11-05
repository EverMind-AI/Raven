import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { FixtureTransport } from '../../rpc/fixtureTransport'
import { setGateway } from '../../rpc/gateway'
import * as history from './history'

/* The recall state machine and its wire adapter. The file itself is the
 * server's (raven/input_history.py, held to the TUI's format by its own
 * tests); what this side owns is the in-memory copy, the boundaries of the
 * arrow walk, and the draft that comes back when the walk goes past the newest
 * entry. */

beforeEach(() => {
  history._resetForTests()
  setGateway(null)
})

afterEach(() => {
  setGateway(null)
})

function transport(entries: string[]): FixtureTransport {
  return new FixtureTransport({
    'input.history.list': { entries },
    'input.history.append': { ok: true, appended: true },
  })
}

async function load(entries: string[]): Promise<FixtureTransport> {
  const t = transport(entries)
  setGateway(t)
  await history.init()
  return t
}

describe('loading the shared history', () => {
  it('keeps the file order, oldest first, and asks for the TUI cap', async () => {
    const t = await load(['oldest', 'middle', 'newest'])
    expect(history.entries()).toEqual(['oldest', 'middle', 'newest'])
    expect(t.calls).toEqual([{ method: 'input.history.list', params: { limit: history.MAX } }])
  })

  it('starts empty when the gateway cannot answer, rather than throwing', async () => {
    setGateway(new FixtureTransport({}))
    await expect(history.init()).resolves.toBeUndefined()
    expect(history.entries()).toEqual([])
  })

  it('loads once: a second init does not ask again', async () => {
    const t = await load(['a'])
    await history.init()
    expect(t.calls.filter((c) => c.method === 'input.history.list')).toHaveLength(1)
  })

  it('merges an append that landed while the list was still in flight', async () => {
    let release!: (value: { entries: string[] }) => void
    const pending = new Promise<{ entries: string[] }>((resolve) => { release = resolve })
    const t = new FixtureTransport({
      'input.history.list': () => pending,
      'input.history.append': { ok: true, appended: true },
    })
    setGateway(t)
    const loading = history.init()
    history.append('typed early')
    release({ entries: ['from the file'] })
    await loading
    expect(history.entries()).toEqual(['from the file', 'typed early'])
  })
})

describe('what an append keeps', () => {
  it('trims, skips blank, and records what the server was sent', async () => {
    const t = await load([])
    history.append('  hello  ')
    history.append('   ')
    expect(history.entries()).toEqual(['hello'])
    expect(t.calls.filter((c) => c.method === 'input.history.append'))
      .toEqual([{ method: 'input.history.append', params: { text: 'hello' } }])
  })

  it('drops only an adjacent repeat', async () => {
    await load(['a', 'b'])
    history.append('a')
    expect(history.entries()).toEqual(['a', 'b', 'a'])
    history.append('a')
    expect(history.entries()).toEqual(['a', 'b', 'a'])
  })

  it('keeps the newest 1000 entries', async () => {
    await load(Array.from({ length: history.MAX }, (_, i) => `p${i}`))
    history.append('newest')
    expect(history.entries()).toHaveLength(history.MAX)
    expect(history.entries()[0]).toBe('p1')
    expect(history.entries().at(-1)).toBe('newest')
  })

  it('keeps the in-memory copy when the write is refused', async () => {
    setGateway(new FixtureTransport({ 'input.history.list': { entries: [] } }))
    await history.init()
    history.append('kept here')
    await Promise.resolve()
    expect(history.entries()).toEqual(['kept here'])
  })
})

describe('walking the history', () => {
  it('recalls the newest first and stops at the oldest without wrapping', async () => {
    await load(['one', 'two'])
    expect(history.navigate(-1, '')).toBe('two')
    expect(history.navigate(-1, 'two')).toBe('one')
    expect(history.navigate(-1, 'one')).toBeNull()
  })

  it('walks forward and restores the draft past the newest', async () => {
    await load(['one', 'two'])
    expect(history.navigate(-1, 'my draft')).toBe('two')
    expect(history.navigate(-1, 'two')).toBe('one')
    expect(history.navigate(1, 'one')).toBe('two')
    expect(history.navigate(1, 'two')).toBe('my draft')
    expect(history.navigate(1, 'my draft')).toBeNull()
  })

  it('restores an empty draft as a real answer, not as "no action"', async () => {
    await load(['only'])
    expect(history.navigate(-1, '')).toBe('only')
    expect(history.navigate(1, 'only')).toBe('')
  })

  it('does nothing when there is no history', async () => {
    await load([])
    expect(history.navigate(-1, 'typed')).toBeNull()
    expect(history.navigate(1, 'typed')).toBeNull()
  })
})

describe('editing a recalled entry', () => {
  it('turns the edit into the draft, so down past the newest brings it back', async () => {
    await load(['recalled'])
    expect(history.navigate(-1, 'first draft')).toBe('recalled')
    history.detachEdited('edited')
    expect(history.navigate(1, 'edited')).toBeNull()
    expect(history.navigate(-1, 'edited')).toBe('recalled')
    expect(history.navigate(1, 'recalled')).toBe('edited')
  })

  it('does not detach while the entry is untouched', async () => {
    await load(['one', 'two'])
    expect(history.navigate(-1, 'draft')).toBe('two')
    history.detachEdited('two')
    expect(history.navigate(-1, 'two')).toBe('one')
  })
})

describe('reset', () => {
  it('ends the browse and forgets the draft it captured', async () => {
    await load(['one', 'two'])
    expect(history.navigate(-1, 'first draft')).toBe('two')
    history.reset()
    expect(history.navigate(-1, 'second draft')).toBe('two')
    expect(history.navigate(1, 'two')).toBe('second draft')
  })
})
