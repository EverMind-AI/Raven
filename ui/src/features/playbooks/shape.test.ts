import { describe, expect, it } from 'vitest'

import { cardPlan, columns, ROOMY, TIGHT } from './shape'

import type { PlaybookNodeShape } from './types'

/* A chain, a fan-out, whatever the caller asks for -- built from ids alone,
   since the shape is all `depends_on`. */
const chain = (n: number): PlaybookNodeShape[] =>
  Array.from({ length: n }, (_, i) => ({ id: 's' + i, depends_on: i ? ['s' + (i - 1)] : [] }))

const fan = (width: number): PlaybookNodeShape[] => [
  { id: 'head', depends_on: [] },
  ...Array.from({ length: width }, (_, i) => ({ id: 'f' + i, depends_on: ['head'] })),
  { id: 'tail', depends_on: Array.from({ length: width }, (_, i) => 'f' + i) },
]

describe('the card diagram plan', () => {
  it('lays a fan-out out as one column, not one per branch', () => {
    const cols = columns(fan(3))
    expect(cols.map((c) => c.length)).toEqual([1, 3, 1])
  })

  it('draws a short graph at the roomy metric and does not stretch it', () => {
    const plan = cardPlan(chain(2))
    expect(plan.metric).toBe(ROOMY)
    expect(plan.cells).toHaveLength(2)
    expect(plan.hiddenSteps).toBe(0)
    expect(plan.clipAt).toBeNull()
    /* One step is one box in a mostly empty box, which is the truth about that
       playbook -- scaling it up to fill the card would not be. */
    expect(plan.width).toBeLessThan(120)
  })

  it('switches to the tight metric before it starts hiding steps', () => {
    const plan = cardPlan(chain(5))
    expect(plan.metric).toBe(TIGHT)
    expect(plan.cells).toHaveLength(5)
    expect(plan.hiddenSteps).toBe(0)
  })

  it('clips a long graph and counts what it clipped', () => {
    const plan = cardPlan(chain(10))
    expect(plan.metric).toBe(TIGHT)
    expect(plan.clipAt).not.toBeNull()
    /* The caption states the real totals, so the drawing is allowed to stop --
       but the count has to add up. */
    expect(plan.cells.length + plan.hiddenSteps).toBe(10)
    expect(plan.steps).toBe(10)
    expect(plan.columns).toBe(10)
  })

  it('clips a layer taller than the box and marks the remainder in that column', () => {
    const plan = cardPlan(fan(8))
    const marks = plan.rowOverflow
    expect(marks).toHaveLength(1)
    expect(marks[0]?.n).toBeGreaterThan(0)
    expect(plan.cells.length + plan.hiddenSteps).toBe(10)
  })

  it('keeps every drawing inside the fixed box', () => {
    for (const nodes of [chain(1), chain(4), chain(12), fan(2), fan(9)]) {
      const plan = cardPlan(nodes)
      expect(plan.width).toBeLessThanOrEqual(268)
      expect(plan.height).toBeLessThanOrEqual(84)
      for (const cell of plan.cells) {
        expect(cell.x + plan.metric.W).toBeLessThanOrEqual(268)
        expect(cell.y + plan.metric.H).toBeLessThanOrEqual(84)
      }
    }
  })

  it('reports parallelism only when two steps really run side by side', () => {
    expect(cardPlan(chain(3)).parallel).toBe(false)
    expect(cardPlan(fan(2)).parallel).toBe(true)
  })
})
