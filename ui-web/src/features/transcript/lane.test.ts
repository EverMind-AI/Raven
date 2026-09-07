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

/* A run in flight is redrawn from the last thing it said on every poll. The
   redraw used to truncate the provisional segments by count, but `history`
   ends by folding the steps it drew into the turn's existing fold -- out of the
   truncation's reach -- so every poll left another copy of each in-flight tool
   call inside the fold: 53 real calls were drawn 271 times. */
describe('a delegated run redrawn while in flight', () => {
  const call = (id: string, name: string, text = '') => [
    { role: 'assistant' as const, text, tool_calls: [{ id, name, arguments: '{}' }] },
    { role: 'tool' as const, tool_call_id: id, text: 'ok' },
  ]
  const drawn = (lane: ReturnType<typeof store.newLane>): string[] => {
    const names: string[] = []
    lane.segs.forEach((s) => {
      if (s.kind === 'step') s.calls.forEach((c) => names.push(c.name))
      if (s.kind === 'fold') s.steps.forEach((st) => st.calls.forEach((c) => names.push(c.name)))
    })
    return names
  }

  it('draws each tool call once across polls', () => {
    const lane = store.newLane('agent:one', false)
    /* The first call is settled by the narration before the second, so its
       step is folded on the first poll; the second call is provisional and
       lands in that same fold on every poll until the run says it is done. */
    const two = [
      { role: 'user' as const, text: 'job' },
      ...call('c1', 'read_file', 'reading'),
      ...call('c2', 'find', 'searching'),
    ]
    store.agentPaintLane(lane, { messages: two, status: 'run' }, { key: 'run-1' })
    store.agentPaintLane(lane, { messages: two, status: 'run' }, { key: 'run-1' })
    store.agentPaintLane(lane, { messages: two, status: 'run' }, { key: 'run-1' })
    store.agentPaintLane(lane, { messages: [...two, { role: 'assistant', text: 'done' }], status: 'ok' }, { key: 'run-1' })

    expect(drawn(lane)).toEqual(['read_file', 'find'])
  })

  it('keeps a fold the reader opened open across polls', () => {
    /* The fold over an in-flight turn lives in the provisional rows, which are
       redrawn from scratch on every poll -- so a fold opened by the reader was
       a new, closed object one poll later. The same goes for the rows inside
       it: a call the reader unfolded closed with it. */
    const lane = store.newLane('agent:one', false)
    const msgs = [
      { role: 'user' as const, text: 'job' },
      { role: 'assistant' as const, reasoning_content: 'think', text: 'reading the brief', tool_calls: [{ id: 'c1', name: 'read_file', arguments: '{}' }] },
      { role: 'tool' as const, tool_call_id: 'c1', text: 'ok' },
    ]
    const foldOf = () => lane.segs.find((s) => s.kind === 'fold')
    store.agentPaintLane(lane, { messages: msgs, status: 'run' }, { key: 'run-1' })
    const first = foldOf()
    expect(first && first.kind === 'fold' && first.open).toBe(false)
    if (!first || first.kind !== 'fold') throw new Error('no fold drawn')
    store.toggleFold(lane, first)
    /* The thought and the call are two rows: the answer between them seals the
       thought's step, so the tool result opens a step of its own. */
    const callAt = first.steps.findIndex((s) => s.calls.length > 0)
    store.toggleCall(lane, first.steps[callAt]!.calls[0]!)

    store.agentPaintLane(lane, { messages: [...msgs, { role: 'assistant', reasoning_content: 'more' }], status: 'run' }, { key: 'run-1' })
    const again = foldOf()
    if (!again || again.kind !== 'fold') throw new Error('fold gone')
    expect(again).not.toBe(first)
    expect(again.open).toBe(true)
    expect(again.steps[callAt]!.calls[0]!.open).toBe(true)
    expect(drawn(lane)).toEqual(['read_file'])
  })
})
