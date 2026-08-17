import { describe, expect, it } from 'vitest'

import { chipColor, chipsForWidth, cycleTarget } from '../components/instanceChips.js'
import { resolveTheme } from '../theme.js'

// Every palette a terminal can land on, not just the default: the regression
// was invisible in one of them and total in the other two.
const THEMES = Object.fromEntries(
  (['dark', 'light'] as const).flatMap(scheme =>
    ([0, 1, 2, 3] as const).map(tier => [`${scheme}/${tier}`, resolveTheme(scheme, tier)])
  )
)

const row = (agent: string, handle: string, status: string, updatedAtMs: number) => ({
  agent,
  createdAtMs: 0,
  handle,
  kind: 'cli',
  sessionKey: 's1',
  status,
  updatedAtMs
})

describe('chipsForWidth', () => {
  it('always leads with the main-agent chip', () => {
    const { chips } = chipsForWidth([], null, 80)
    expect(chips[0]?.label).toBe('Raven')
    expect(chips[0]?.target).toBeNull()
  })

  it('orders instances by when they first appeared, and keeps that order', () => {
    // Not by `updatedAtMs`: it bumps on every status change, and with several
    // instances answering at once the strip would resort under the user --
    // the active chip sliding sideways mid-conversation.
    const first = { ...row('A', 'first', 'completed', 9), createdAtMs: 1 }
    const second = { ...row('B', 'second', 'completed', 1), createdAtMs: 2 }

    const { chips } = chipsForWidth([second, first], null, 120)
    expect(chips.slice(1).map(c => c.target?.agent)).toEqual(['A', 'B'])

    // B answers, so its updatedAtMs overtakes A's. The strip must not move.
    const bumped = { ...second, updatedAtMs: 99 }
    expect(
      chipsForWidth([first, bumped], null, 120)
        .chips.slice(1)
        .map(c => c.target?.agent)
    ).toEqual(['A', 'B'])
  })

  it('marks a running instance', () => {
    const { chips } = chipsForWidth([row('A', 'h', 'running', 1)], null, 120)
    expect(chips[1]?.running).toBe(true)
  })

  it('marks a pending instance as running too', () => {
    // It is queued behind the concurrency gate, so a reply is still coming.
    const { chips } = chipsForWidth([row('A', 'h', 'pending', 1)], null, 120)
    expect(chips[1]?.running).toBe(true)
  })

  it('does not mark an interrupted instance as running', () => {
    const { chips } = chipsForWidth([row('A', 'h', 'interrupted', 1)], null, 120)
    expect(chips[1]?.running).toBe(false)
  })

  it('marks the active chip', () => {
    const { chips } = chipsForWidth([row('A', 'h', 'completed', 1)], { agent: 'A', handle: 'h' }, 120)
    expect(chips[1]?.active).toBe(true)
    expect(chips[0]?.active).toBe(false)
  })

  it('marks the main chip active on the main conversation', () => {
    const { chips } = chipsForWidth([row('A', 'h', 'completed', 1)], null, 120)
    expect(chips[0]?.active).toBe(true)
    expect(chips[1]?.active).toBe(false)
  })

  it('truncates from the right and reports the overflow', () => {
    const rows = Array.from({ length: 12 }, (_, i) => row(`agent${i}`, `handle${i}`, 'completed', i))
    const { chips, overflow } = chipsForWidth(rows, null, 40)
    expect(overflow).toBeGreaterThan(0)
    expect(chips.length).toBeLessThan(rows.length + 1)
  })

  it('keeps the active chip even when it does not fit', () => {
    // A strip that hides where you are is worse than a truncated one.
    const rows = Array.from({ length: 12 }, (_, i) => row(`agent${i}`, `handle${i}`, 'completed', 100 - i))
    const { chips } = chipsForWidth(rows, { agent: 'agent11', handle: 'handle11' }, 40)
    expect(chips.some(c => c.target?.agent === 'agent11')).toBe(true)
  })

  it('always keeps the main chip, which is the way back', () => {
    const rows = Array.from({ length: 12 }, (_, i) => row(`agent${i}`, `handle${i}`, 'completed', i))
    const { chips } = chipsForWidth(rows, null, 1)
    expect(chips[0]?.target).toBeNull()
  })
})

describe('cycleTarget', () => {
  const chips = chipsForWidth([row('A', 'one', 'completed', 2), row('B', 'two', 'completed', 1)], null, 120).chips

  it('steps right from the main chip to the most recent instance', () => {
    expect(cycleTarget(chips, 1)).toEqual({ agent: 'A', handle: 'one' })
  })

  it('wraps left from the main chip to the last instance', () => {
    expect(cycleTarget(chips, -1)).toEqual({ agent: 'B', handle: 'two' })
  })

  it('wraps right from the last instance back to the main chip', () => {
    const fromLast = chipsForWidth(
      [row('A', 'one', 'completed', 2), row('B', 'two', 'completed', 1)],
      { agent: 'B', handle: 'two' },
      120
    ).chips
    expect(cycleTarget(fromLast, 1)).toBeNull()
  })

  it('returns the main chip for an empty strip', () => {
    expect(cycleTarget([], 1)).toBeNull()
  })
})

describe('chipsForWidth — what is addressable', () => {
  const dagNode = (agent: string, runId: string, nodeId: string) => ({
    agent,
    createdAtMs: 0,
    handle: `${runId}/${nodeId}`,
    kind: 'dag-node',
    nodeId,
    runId,
    sessionKey: 's1',
    status: 'completed',
    updatedAtMs: 9
  })

  it('leaves dag-node rows out of the strip', () => {
    // Their handle is minted by the runner, and continuing one has no resume
    // story; a chip would offer a chat that cannot work.
    const { chips } = chipsForWidth(
      [dagNode('Coder', 'run-1', 'build'), row('Coder', 'build', 'completed', 8)],
      null,
      120
    )

    expect(chips.map(c => c.label)).toEqual(['Raven', 'Coder/build'])
  })

  it('does not count a dropped dag-node row as overflow', () => {
    const { overflow } = chipsForWidth([dagNode('Coder', 'run-1', 'build')], null, 120)

    expect(overflow).toBe(0)
  })

  it('shows the strip as empty when a session only ever ran a dag', () => {
    const { chips } = chipsForWidth([dagNode('Coder', 'run-1', 'a'), dagNode('Writer', 'run-1', 'b')], null, 120)

    expect(chips).toHaveLength(1)
  })
})

describe('which chip the strip paints as the one you are on', () => {
  it('gives the active chip a colour no inactive chip is painted in', () => {
    // The regression: the strip asked for `label` on the active chip and
    // `muted` on the rest, and those are the same value in the default dark
    // theme and in the 256-colour one -- so every chip rendered identically and
    // bold was the only surviving cue.
    for (const [name, theme] of Object.entries(THEMES)) {
      expect(theme.color.accent, name).not.toBe(theme.color.muted)
    }
  })

  it('paints the active and inactive chips differently in every palette', () => {
    const { chips } = chipsForWidth([row('A', 'one', 'completed', 2)], { agent: 'A', handle: 'one' }, 120)
    const [main, active] = chips

    expect(active?.active).toBe(true)
    for (const [name, theme] of Object.entries(THEMES)) {
      expect(chipColor(active!, theme), name).not.toBe(chipColor(main!, theme))
    }
  })
})
