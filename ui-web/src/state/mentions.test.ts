// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as mentions from './mentions'

/* What a conversation is pointed at, and when that reaches the engine.
 *
 * Two lives, the way the working directory has two: a draft holds its pick
 * until there is a session to write it to, and a conversation writes straight
 * through. The tests below are mostly about which of the two is happening.
 */

const calls: Array<[string, unknown]> = []
let answer: (method: string, params: unknown) => unknown = () => ({ bases: [] })

vi.mock('../rpc/gateway', () => ({
  gateway: () => ({
    call: async (method: string, params: unknown) => {
      calls.push([method, params])
      return answer(method, params)
    },
  }),
}))

beforeEach(() => {
  mentions._resetForTests()
  /* A draft unless a case says otherwise: the default reader is the page's own
     pointer, which no test stands up. */
  mentions.useSession(() => null)
  calls.length = 0
  answer = () => ({ bases: [] })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('what a conversation is pointed at', () => {
  it('reads the bases once, not every time the menu opens', async () => {
    /* The same read the knowledge page makes, for a list of three names that
       has not changed since the last time it was drawn. */
    answer = () => ({ bases: [{ id: 'kb-a', name: 'handbook', documents: 3 }] })

    await mentions.load()
    await mentions.load()

    expect(calls.filter(([m]) => m === 'knowledge.bases.list')).toHaveLength(1)
    expect(mentions.get().bases).toEqual([{ id: 'kb-a', name: 'handbook', documents: 3 }])
  })

  it('waits rather than showing an empty list while it reads', async () => {
    /* Null is not the empty list: one is "wait", the other is "this machine
       has no bases", and a menu that showed the same for both would be lying
       half the time. */
    expect(mentions.get().bases).toBeNull()

    await mentions.load()

    expect(mentions.get().bases).toEqual([])
  })

  it('says why the bases could not be read', async () => {
    answer = () => {
      throw new Error('no gateway')
    }

    await mentions.load()

    expect(mentions.get().failed).toBe('no gateway')
  })

  it('holds a draft pick rather than writing it nowhere', () => {
    /* There is no session to write to yet; the create is the one moment the
       engine takes one. */
    mentions.pick('kb-a')
    mentions.pick('kb-b')

    expect(mentions.staged()).toEqual(['kb-a', 'kb-b'])
    expect(calls.filter(([m]) => m === 'session.set_knowledge')).toEqual([])
  })

  it('writes straight through once there is a conversation', async () => {
    /* A reader who attaches a base mid-way means the next turn, not the next
       conversation. */
    mentions.useSession(() => 'tui:1')

    mentions.pick('kb-a')
    await vi.waitFor(() => expect(calls.some(([m]) => m === 'session.set_knowledge')).toBe(true))

    expect(calls.at(-1)).toEqual(['session.set_knowledge', { session_id: 'tui:1', knowledge_bases: ['kb-a'] }])
  })

  it('unticks by picking again, and says so as a whole list', async () => {
    /* The method replaces rather than adds, so what goes over the wire is
       everything that is still ticked. */
    mentions.useSession(() => 'tui:1')
    mentions.pick('kb-a')
    mentions.pick('kb-b')
    await vi.waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2))

    mentions.pick('kb-a')

    await vi.waitFor(() =>
      expect(calls.at(-1)).toEqual(['session.set_knowledge', { session_id: 'tui:1', knowledge_bases: ['kb-b'] }]),
    )
    expect(mentions.count()).toBe(1)
  })

  it('keeps the tick when the write fails', async () => {
    /* The selection is what the panel shows and what the next turn reads.
       Undoing a click to report a gateway blip would lose the one thing the
       reader was trying to say. */
    mentions.useSession(() => 'tui:1')
    answer = () => {
      throw new Error('gone')
    }

    mentions.pick('kb-a')
    await vi.waitFor(() => expect(calls.some(([m]) => m === 'session.set_knowledge')).toBe(true))

    expect(mentions.get().picked).toEqual(['kb-a'])
  })

  it('drops the pick with the draft, and keeps the list of bases', async () => {
    /* An invisible choice must not cross from one conversation to another.
       The bases are a fact about the machine, not about the conversation. */
    answer = () => ({ bases: [{ id: 'kb-a', name: 'handbook', documents: 1 }] })
    await mentions.load()
    mentions.pick('kb-a')

    mentions.clearStaged()

    expect(mentions.staged()).toEqual([])
    expect(mentions.get().bases).toHaveLength(1)
  })

  it('takes up what a conversation already carries', () => {
    mentions.adopt(['kb-b', 'kb-a'])

    expect(mentions.get().picked).toEqual(['kb-b', 'kb-a'])
  })

  it('opens on the two rows and goes into the bases from there', async () => {
    mentions.toggle()
    expect(mentions.get()).toMatchObject({ open: true, panel: null })

    mentions.openBases()
    expect(mentions.get().panel).toBe('bases')
    /* And opening the panel is what reads them: a menu nobody opened spends
       no round trip. */
    await vi.waitFor(() => expect(calls.some(([m]) => m === 'knowledge.bases.list')).toBe(true))

    mentions.openRows()
    expect(mentions.get().panel).toBeNull()
  })

  it('shuts on the second press, from either panel', () => {
    mentions.toggle()
    mentions.openBases()

    mentions.toggle()

    expect(mentions.get()).toMatchObject({ open: false, panel: null })
  })

  it('clears every base at once', async () => {
    mentions.useSession(() => 'tui:1')
    mentions.pick('kb-a')
    await vi.waitFor(() => expect(calls.length).toBeGreaterThan(0))

    mentions.clear()

    await vi.waitFor(() =>
      expect(calls.at(-1)).toEqual(['session.set_knowledge', { session_id: 'tui:1', knowledge_bases: [] }]),
    )
  })

  it('writes nothing when there was nothing to clear', () => {
    mentions.useSession(() => 'tui:1')

    mentions.clear()

    expect(calls).toEqual([])
  })
})
