import { beforeEach, describe, expect, it } from 'vitest'

import type { DirectTurn, InstanceRow } from '../rpc/generated.js'

import {
  directKey,
  getDirectChat,
  getDirectTranscript,
  patchDirectChat,
  resetDirectChat,
  setDirectTranscript
} from '../app/directChatStore.js'
import { fetchDirectHistory, fetchInstances } from '../app/directChatSync.js'

const row = (agent: string, handle: string): InstanceRow =>
  ({ agent, createdAtMs: 1, handle, kind: 'acp', sessionKey: 's1', status: 'completed', updatedAtMs: 1 }) as InstanceRow

beforeEach(() => {
  resetDirectChat()
})

describe('fetchInstances', () => {
  it('replaces the strip with what the runtime reports', async () => {
    await fetchInstances(async () => ({ instances: [row('A', 'one')], pending_handoff_count: 2 }) as never, 's1')

    expect(getDirectChat().instances.map(r => r.handle)).toEqual(['one'])
    expect(getDirectChat().pendingHandoffCount).toBe(2)
  })

  it('keeps the strip when the read fails', async () => {
    // A quiet rpc answers null for a failed call, and emptying on that made
    // every chip vanish for one refresh interval while the instances were
    // still there.
    patchDirectChat({ instances: [row('A', 'one'), row('B', 'two')] })
    await fetchInstances(async () => null, 's1')

    expect(getDirectChat().instances.map(r => r.handle)).toEqual(['one', 'two'])
  })

  it('keeps the strip when the read throws', async () => {
    patchDirectChat({ instances: [row('A', 'one')] })
    await fetchInstances(async () => {
      throw new Error('gateway went away')
    }, 's1')

    expect(getDirectChat().instances.map(r => r.handle)).toEqual(['one'])
  })

  it('still empties the strip when the session genuinely has none', async () => {
    // Paired with the case above: "the call failed" and "there is nothing" have
    // to stay distinguishable, or the strip keeps a dead session's rows.
    patchDirectChat({ instances: [row('A', 'one')] })
    await fetchInstances(async () => ({ instances: [], pending_handoff_count: 0 }) as never, 's1')

    expect(getDirectChat().instances).toEqual([])
  })

  it('clears the strip when there is no session key', async () => {
    patchDirectChat({ instances: [row('A', 'one')] })
    await fetchInstances(async () => ({ instances: [row('A', 'one')] }) as never, null)

    expect(getDirectChat().instances).toEqual([])
  })
})

describe('fetchDirectHistory', () => {
  const target = { agent: 'A', handle: 'one' }
  const key = directKey('A', 'one')

  const call = (id: string, name: string, args: Record<string, unknown>) => ({
    id,
    name,
    arguments: JSON.stringify(args)
  })

  const turn = (over: Partial<DirectTurn> & Pick<DirectTurn, 'role'>): DirectTurn => ({
    call_id: 'log-0',
    content: '',
    at_ms: 0,
    ...over
  })

  const answering = (turns: DirectTurn[]) => async () => ({ turns }) as never

  it('folds the record into the transcript on entering', async () => {
    await fetchDirectHistory(
      answering([
        turn({ role: 'user', content: 'look' }),
        turn({ role: 'assistant', tool_calls: [call('c1', 'read_file', { path: '/a.ts' })] }),
        turn({ role: 'tool', tool_call_id: 'c1', content: 'body' }),
        turn({ role: 'assistant', content: 'one file.' })
      ]),
      's1',
      target
    )

    const rows = getDirectTranscript(key)
    expect(rows.map(m => m.role)).toEqual(['user', 'assistant'])
    expect(rows[1]!.kind).toBe('episodes')
    expect(rows[1]!.episodes![0]!.tools[0]!.name).toBe('read_file')
  })

  it('declines to replace a transcript that already holds something', async () => {
    // A turn that streamed in during the fetch is newer than the record.
    setDirectTranscript(key, [{ role: 'user', text: 'mine' }])
    await fetchDirectHistory(answering([turn({ role: 'user', content: 'from disk' })]), 's1', target)

    expect(getDirectTranscript(key).map(m => m.text)).toEqual(['mine'])
  })

  it('replaces it anyway on a settled read, which is what that read is for', async () => {
    setDirectTranscript(key, [
      { role: 'user', text: 'ask' },
      { role: 'assistant', text: 'streamed' }
    ])
    await fetchDirectHistory(
      answering([
        turn({ role: 'user', content: 'ask' }),
        turn({ role: 'assistant', tool_calls: [call('c1', 'exec', { command: 'ls' })] }),
        turn({ role: 'tool', tool_call_id: 'c1', content: 'a b' }),
        turn({ role: 'assistant', content: 'two entries.' })
      ]),
      's1',
      target,
      'settled'
    )

    const rows = getDirectTranscript(key)
    expect(rows[1]!.kind).toBe('episodes')
    expect(rows[1]!.text).toBe('two entries.')
    expect(rows[1]!.episodes![0]!.tools[0]!.summary).toBe('ls')
  })

  it('rebuilds the running turn from the read, answer text and all', async () => {
    setDirectTranscript(key, [
      { role: 'user', text: 'earlier' },
      { kind: 'episodes', role: 'assistant', text: 'earlier answer', episodes: [] },
      { role: 'user', text: 'ask' },
      { role: 'assistant', text: 'partial repl' }
    ])

    await fetchDirectHistory(
      answering([
        turn({ role: 'user', content: 'earlier' }),
        turn({ role: 'assistant', content: 'earlier answer' }),
        turn({ role: 'user', live: true, content: 'ask' }),
        turn({ role: 'assistant', live: true, tool_calls: [call('c1', 'exec', { command: 'ls' })] }),
        turn({ role: 'tool', live: true, tool_call_id: 'c1', content: 'a b' }),
        turn({ role: 'assistant', live: true, content: 'the answer so far' })
      ]),
      's1',
      target,
      'live'
    )

    const rows = getDirectTranscript(key)
    // The settled turn, then the running one rebuilt from the read -- answer
    // text included, so the client keeps nothing of its own to reconcile.
    expect(rows.map(m => [m.role, m.kind ?? '', m.text])).toEqual([
      ['user', '', 'earlier'],
      ['assistant', 'episodes', 'earlier answer'],
      ['user', '', 'ask'],
      ['assistant', 'episodes', 'the answer so far']
    ])
    expect(rows[3]!.episodes![0]!.tools[0]!.summary).toBe('ls')
  })

  it('shows a turn this client never sent, which has no row of its own to anchor on', async () => {
    // A spawn or a DAG node. Anchoring on the last thing the user said put such
    // a turn's steps where the previous turn's answer was.
    setDirectTranscript(key, [
      { role: 'user', text: 'earlier' },
      { kind: 'episodes', role: 'assistant', text: 'earlier answer', episodes: [] }
    ])

    await fetchDirectHistory(
      answering([
        turn({ role: 'user', content: 'earlier' }),
        turn({ role: 'assistant', content: 'earlier answer' }),
        turn({ role: 'user', live: true, content: 'spawned task' }),
        turn({ role: 'assistant', live: true, tool_calls: [call('c1', 'read_file', { path: '/a.ts' })] })
      ]),
      's1',
      target,
      'live'
    )

    const rows = getDirectTranscript(key)
    expect(rows.map(m => [m.role, m.text])).toEqual([
      ['user', 'earlier'],
      ['assistant', 'earlier answer'],
      ['user', 'spawned task'],
      ['assistant', '']
    ])
    expect(rows[1]!.text).toBe('earlier answer')
    expect(rows[3]!.episodes![0]!.tools[0]!.name).toBe('read_file')
  })

  it('replaces the previous snapshot rather than appending to it', async () => {
    setDirectTranscript(key, [])
    const steps = (n: number) => [
      turn({ role: 'user', live: true, content: 'ask' }),
      ...Array.from({ length: n }, (_, i) => [
        turn({ role: 'assistant', live: true, tool_calls: [call(`c${i}`, 'exec', { command: `cmd${i}` })] }),
        turn({ role: 'tool', live: true, tool_call_id: `c${i}`, content: 'ok' })
      ]).flat()
    ]

    await fetchDirectHistory(answering(steps(1)), 's1', target, 'live')
    await fetchDirectHistory(answering(steps(3)), 's1', target, 'live')

    const rows = getDirectTranscript(key)
    expect(rows).toHaveLength(2)
    expect(rows[1]!.episodes!.flatMap(e => e.tools).map(t => t.summary)).toEqual(['cmd0', 'cmd1', 'cmd2'])
  })

  it('ignores settled rows on a live read and live rows on a settled one', async () => {
    setDirectTranscript(key, [{ role: 'user', text: 'ask' }])
    const mixed = [
      turn({ role: 'user', content: 'ask' }),
      turn({ role: 'assistant', content: 'on record' }),
      turn({ role: 'assistant', live: true, tool_calls: [call('c1', 'exec', { command: 'ls' })] })
    ]

    await fetchDirectHistory(answering(mixed), 's1', target, 'live')
    expect(getDirectTranscript(key).at(-1)!.episodes![0]!.tools[0]!.summary).toBe('ls')

    await fetchDirectHistory(answering(mixed), 's1', target, 'settled')
    const rows = getDirectTranscript(key)
    expect(rows.at(-1)!.text).toBe('on record')
    expect(rows.flatMap(m => m.episodes ?? []).flatMap(e => e.tools)).toEqual([])
  })

  it('leaves the view alone when the read has no step to add', async () => {
    // A transport with no per-step visibility reports none for the whole turn,
    // so this is not "the turn ended" -- and the prompt and the reply streaming
    // in are the client's own, with nowhere else to come back from.
    setDirectTranscript(key, [
      { role: 'user', text: 'ask' },
      { role: 'assistant', text: 'streaming' }
    ])
    await fetchDirectHistory(answering([]), 's1', target, 'live')

    expect(getDirectTranscript(key).map(m => m.text)).toEqual(['ask', 'streaming'])
  })

  it('keeps the transcript when the read throws', async () => {
    setDirectTranscript(key, [{ role: 'user', text: 'ask' }])
    await fetchDirectHistory(
      async () => {
        throw new Error('gateway went away')
      },
      's1',
      target,
      'live'
    )

    expect(getDirectTranscript(key).map(m => m.text)).toEqual(['ask'])
  })
})

describe('a poll and what the reader has open', () => {
  const target = { agent: 'A', handle: 'one' }
  const key = directKey('A', 'one')

  const step = (i: number, result: string) => [
    {
      call_id: `l${i}a`,
      role: 'assistant' as const,
      content: '',
      at_ms: 0,
      live: true,
      tool_calls: [{ id: `c${i}`, name: 'exec', arguments: JSON.stringify({ command: `cmd${i}` }) }]
    },
    { call_id: `l${i}b`, role: 'tool' as const, content: result, at_ms: 0, live: true, tool_call_id: `c${i}` }
  ]

  const read = (turns: unknown[]) => async () => ({ turns }) as never

  it('leaves a row that did not change as the very same object', async () => {
    // The renderer keys a row by its object, so a fresh object for an unchanged
    // row remounts it -- and a remount is what was closing the tool call the
    // reader had just opened, every poll.
    const settled = [{ call_id: 's0', role: 'user' as const, content: 'earlier', at_ms: 1 }]

    await fetchDirectHistory(read([...settled, ...step(1, 'one')]), 's1', target, 'live')
    const first = getDirectTranscript(key)

    await fetchDirectHistory(read([...settled, ...step(1, 'one')]), 's1', target, 'live')
    const second = getDirectTranscript(key)

    expect(second[0]).toBe(first[0])
    expect(second[1]).toBe(first[1])
  })

  it('replaces only the row whose content really moved', async () => {
    const settled = [{ call_id: 's0', role: 'user' as const, content: 'earlier', at_ms: 1 }]

    await fetchDirectHistory(read([...settled, ...step(1, 'one')]), 's1', target, 'live')
    const first = getDirectTranscript(key)

    // A second call lands: the episodes row changes, the user row does not.
    await fetchDirectHistory(read([...settled, ...step(1, 'one'), ...step(2, 'two')]), 's1', target, 'live')
    const second = getDirectTranscript(key)

    expect(second[0]).toBe(first[0])
    expect(second[1]).not.toBe(first[1])
    expect(second[1]!.episodes!.flatMap(e => e.tools).map(t => t.summary)).toEqual(['cmd1', 'cmd2'])
  })
})
