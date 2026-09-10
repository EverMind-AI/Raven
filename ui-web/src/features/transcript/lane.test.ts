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

  it('keeps the fold state the reader chose across polls', () => {
    /* The fold over an in-flight turn lives in the provisional rows, which are
       redrawn from scratch on every poll -- so the state the reader left it in
       was a new, default object one poll later (a new object still; only its
       id is carried over now, see `adoptIdentity`). The same goes for the rows
       inside it: a call the reader unfolded closed again with it.

       Driven from the reader CLOSING it, because a delegated fold is born open
       (see `collapse`): the runtime's default is not what this is about, and a
       test that re-opened an open fold would pass without the restore. */
    const lane = store.newLane('agent:one', false)
    const msgs = [
      { role: 'user' as const, text: 'job' },
      { role: 'assistant' as const, reasoning_content: 'think', text: 'reading the brief', tool_calls: [{ id: 'c1', name: 'read_file', arguments: '{}' }] },
      { role: 'tool' as const, tool_call_id: 'c1', text: 'ok' },
    ]
    const foldOf = () => lane.segs.find((s) => s.kind === 'fold')
    store.agentPaintLane(lane, { messages: msgs, status: 'run' }, { key: 'run-1' })
    const first = foldOf()
    expect(first && first.kind === 'fold' && first.open).toBe(true)
    if (!first || first.kind !== 'fold') throw new Error('no fold drawn')
    store.toggleFold(lane, first)
    /* Thought, narration and call are one row: the narration is a line on the
       way rather than the turn's answer, so it does not seal the step and the
       tool result lands in the same one. */
    const callAt = first.steps.findIndex((s) => s.calls.length > 0)
    store.toggleCall(lane, first.steps[callAt]!.calls[0]!)

    store.agentPaintLane(lane, { messages: [...msgs, { role: 'assistant', reasoning_content: 'more' }], status: 'run' }, { key: 'run-1' })
    const again = foldOf()
    if (!again || again.kind !== 'fold') throw new Error('fold gone')
    expect(again).not.toBe(first)
    expect(again.open).toBe(false)
    expect(again.steps[callAt]!.calls[0]!.open).toBe(true)
    expect(drawn(lane)).toEqual(['read_file'])
  })
})

/* The rows a poll redraws keep their identity.
 *
 * A run in flight is redrawn from its newest committed row on every poll, and
 * `history` mints every row it draws a fresh id -- so the same sentence came
 * back as a new element, and an element's entrance animation runs on mount.
 * Measured against the shipped renderer: a growing answer was a different node
 * after every poll. Driven through the store, because the id IS the React key:
 * a kept id is a kept node. */
describe('a delegated run redrawn while in flight keeps its rows', () => {
  const idsOf = (lane: ReturnType<typeof store.newLane>) => lane.segs.map((s) => `${s.kind}#${s.id}`)

  it('keeps the answer row while the answer grows', () => {
    const lane = store.newLane('agent:grow', false)
    const at = (text: string) => ({ messages: [{ role: 'user' as const, text: 'Q' }, { role: 'assistant' as const, text }], status: 'run' })
    store.agentPaintLane(lane, at('The quick'), { key: 'g1' })
    const first = idsOf(lane)
    store.agentPaintLane(lane, at('The quick brown'), { key: 'g1' })
    store.agentPaintLane(lane, at('The quick brown fox'), { key: 'g1' })
    expect(idsOf(lane)).toEqual(first)
    const answer = lane.segs.find((s) => s.kind === 'answer')
    expect(answer && answer.kind === 'answer' ? answer.text : null).toBe('The quick brown fox')
  })

  it('keeps the fold, its steps and their calls when a new step arrives', () => {
    const lane = store.newLane('agent:steps', false)
    const msgs = [
      { role: 'user' as const, text: 'job' },
      { role: 'assistant' as const, reasoning_content: 'think', text: 'reading', tool_calls: [{ id: 'c1', name: 'read_file', arguments: '{}' }] },
      { role: 'tool' as const, tool_call_id: 'c1', text: 'ok' },
    ]
    store.agentPaintLane(lane, { messages: msgs, status: 'run' }, { key: 's1' })
    const fold = lane.segs.find((s) => s.kind === 'fold')
    if (!fold || fold.kind !== 'fold') throw new Error('no fold drawn')
    const before = { fold: fold.id, steps: fold.steps.map((st) => st.id), calls: fold.steps.flatMap((st) => st.calls.map((c) => c.id)) }

    store.agentPaintLane(lane, {
      messages: [...msgs, { role: 'assistant', reasoning_content: 'more', text: 'and searching', tool_calls: [{ id: 'c2', name: 'find', arguments: '{}' }] }],
      status: 'run',
    }, { key: 's1' })
    const again = lane.segs.find((s) => s.kind === 'fold')
    if (!again || again.kind !== 'fold') throw new Error('fold gone')
    expect(again.id).toBe(before.fold)
    expect(again.steps.slice(0, before.steps.length).map((st) => st.id)).toEqual(before.steps)
    expect(again.steps.flatMap((st) => st.calls.map((c) => c.id)).slice(0, before.calls.length)).toEqual(before.calls)
    /* The new step is new: a kept id is for a row that was there. */
    expect(again.steps.length).toBe(before.steps.length + 1)
    expect(before.steps).not.toContain(again.steps[again.steps.length - 1]!.id)
  })

  it('keeps the steps a poll appended into an already-folded turn', () => {
    /* The other place provisional rows live: not past the committed prefix but
       inside it, appended to a fold that was committed before the model said
       anything. The redraw truncates that fold back to its committed steps and
       draws the appended ones again, so they have their own path to keep. */
    const lane = store.newLane('agent:appended', false)
    const silent = [
      { role: 'user' as const, text: 'job' },
      { role: 'assistant' as const, text: '', tool_calls: [{ id: 'c1', name: 'list_dir', arguments: '{}' }] },
      { role: 'tool' as const, tool_call_id: 'c1', text: 'ok' },
    ]
    const spoke = [
      { role: 'assistant' as const, text: 'now reading', tool_calls: [{ id: 'c2', name: 'read_file', arguments: '{}' }] },
      { role: 'tool' as const, tool_call_id: 'c2', text: 'body' },
    ]
    store.agentPaintLane(lane, { messages: silent, status: 'run' }, { key: 'a1' })
    store.agentPaintLane(lane, { messages: [...silent, ...spoke], status: 'run' }, { key: 'a1' })
    const folds = lane.segs.filter((s): s is Extract<typeof s, { kind: 'fold' }> => s.kind === 'fold')
    expect(folds).toHaveLength(1)
    const stepIds = folds[0]!.steps.map((st) => st.id)
    expect(stepIds.length).toBeGreaterThan(1)

    store.agentPaintLane(lane, {
      messages: [...silent, ...spoke, { role: 'assistant', text: 'and one more', tool_calls: [{ id: 'c3', name: 'find', arguments: '{}' }] }],
      status: 'run',
    }, { key: 'a1' })
    const again = lane.segs.filter((s): s is Extract<typeof s, { kind: 'fold' }> => s.kind === 'fold')
    expect(again).toHaveLength(1)
    expect(again[0]!.steps.slice(0, stepIds.length).map((st) => st.id)).toEqual(stepIds)
    expect(again[0]!.steps.length).toBe(stepIds.length + 1)
  })

  it('gives a row that became a different kind a new identity', () => {
    /* A text nothing followed was the turn's answer; a call after it makes it
       narration inside the fold. That is a different row, and it should arrive
       as one rather than inherit the answer's node. */
    const lane = store.newLane('agent:kind', false)
    const first = [{ role: 'user' as const, text: 'Q' }, { role: 'assistant' as const, text: 'let me look' }]
    store.agentPaintLane(lane, { messages: first, status: 'run' }, { key: 'k1' })
    const answer = lane.segs.find((s) => s.kind === 'answer')
    if (!answer) throw new Error('no answer drawn')
    store.agentPaintLane(lane, {
      messages: [
        { role: 'user', text: 'Q' },
        { role: 'assistant', text: 'let me look', tool_calls: [{ id: 'c1', name: 'read_file', arguments: '{}' }] },
        { role: 'tool', tool_call_id: 'c1', text: 'body' },
      ],
      status: 'run',
    }, { key: 'k1' })
    expect(lane.segs.some((s) => s.kind === 'answer')).toBe(false)
    expect(lane.segs.map((s) => s.id)).not.toContain(answer.id)
  })
})

/* One instance's conversation reads in the order it happened.
 *
 * The renderer lifted the last thing the model said out of the fold and
 * re-emitted it under the turn's fold. On a settled record that is invisible --
 * the last thing said IS the last thing that happened -- but a turn whose text
 * introduced a tool call drew that text BELOW the call, and a running turn is
 * redrawn from its newest line on every poll, so the line the model had just
 * written sat under the work that came after it, every two seconds.
 *
 * `flow` is the whole surface flattened back into one sequence, which is the
 * only shape that can state the rule: what the payload said, in the order it
 * said it. */
describe('a delegated lane draws the payload in order', () => {
  interface StepLike { say: string; calls: Array<{ name: string }> }
  const flow = (lane: ReturnType<typeof store.newLane>): string[] => {
    const out: string[] = []
    const step = (st: StepLike): void => {
      if (st.say) out.push(`say:${st.say}`)
      st.calls.forEach((c) => out.push(`call:${c.name}`))
    }
    lane.segs.forEach((s) => {
      if (s.kind === 'ask') out.push(`ask:${s.body}`)
      else if (s.kind === 'answer') out.push(`answer:${s.text}`)
      else if (s.kind === 'step') step(s)
      else if (s.kind === 'fold') s.steps.forEach(step)
    })
    return out
  }

  const READ = [
    { role: 'user' as const, text: 'Q' },
    { role: 'assistant' as const, text: 'let me look', tool_calls: [{ id: 'c1', name: 'read_file', arguments: '{}' }] },
    { role: 'tool' as const, tool_call_id: 'c1', text: 'body' },
  ]

  it('leaves a narration line above the call it introduced', () => {
    const lane = store.newLane('agent:order', false)
    store.agentPaintLane(lane, { messages: READ, status: 'ok' }, { key: 'r1' })

    expect(flow(lane)).toEqual(['ask:Q', 'say:let me look', 'call:read_file'])
  })

  it('still gives the turn its answer when the model has the last word', () => {
    /* The other half of the rule: a text nothing follows IS the answer, and
       keeps the row that says so. */
    const lane = store.newLane('agent:answer', false)
    store.agentPaintLane(lane, {
      messages: [...READ, { role: 'assistant', text: 'FINAL' }],
      status: 'ok',
    }, { key: 'r2' })

    expect(flow(lane)).toEqual(['ask:Q', 'say:let me look', 'call:read_file', 'answer:FINAL'])
    expect(lane.segs.filter((s) => s.kind === 'answer')).toHaveLength(1)
  })

  it('does not pin a running turn\'s newest line under the work that followed it', () => {
    const lane = store.newLane('agent:live', false)
    const upto = (n: number) => ({
      messages: [
        ...READ,
        { role: 'assistant' as const, text: 'now the tests', tool_calls: [{ id: 'c2', name: 'exec', arguments: '{}' }] },
        { role: 'tool' as const, tool_call_id: 'c2', text: '17 passed' },
      ].slice(0, n),
      status: 'run',
    })
    store.agentPaintLane(lane, upto(4), { key: 'r3' })
    store.agentPaintLane(lane, upto(5), { key: 'r3' })

    expect(flow(lane)).toEqual([
      'ask:Q', 'say:let me look', 'call:read_file', 'say:now the tests', 'call:exec',
    ])
  })

  it('opens the fold over a delegated turn', () => {
    /* This pane IS the sub-agent's work; the steps are what the reader came
       for, so it is not put behind a click. Every turn's, not only the last --
       one instance's conversation is meant to be readable at once. */
    const lane = store.newLane('agent:open', false)
    store.agentPaintLane(lane, {
      messages: [
        ...READ, { role: 'assistant', text: 'FIRST' },
        { role: 'user', text: 'Q2' },
        { role: 'assistant', text: 'again', tool_calls: [{ id: 'c9', name: 'exec', arguments: '{}' }] },
        { role: 'tool', tool_call_id: 'c9', text: 'ok' },
        { role: 'assistant', text: 'SECOND' },
      ],
      status: 'ok',
    }, { key: 'r4' })

    const folds = lane.segs.filter((s) => s.kind === 'fold')
    expect(folds).toHaveLength(2)
    expect(folds.map((f) => f.kind === 'fold' && f.open)).toEqual([true, true])
  })
})
