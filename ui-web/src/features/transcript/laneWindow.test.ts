/* The Lane Window's arithmetic: which stretch of a lane is mounted, counted in
 * turns and always cut at a segment that opens one. */
import { describe, expect, it } from 'vitest'

import { endIndex, mountedTurns, opens, revealWin, startIndex, tailFrom, widenFrom, widenTo } from './laneWindow'

import type { Seg } from './types'

const seg = (id: number, kind: Seg['kind'], extra: Record<string, unknown> = {}): Seg =>
  ({ id, v: 0, kind, ...extra }) as unknown as Seg

/* Openings at ids 2, 5, 8 and 10 (indexes 1, 4, 7, 9): a leading note, a mid-turn
   ask that opens nothing, and a delivered result that opens a turn of its own. */
const SEGS: Seg[] = [
  seg(1, 'note'),
  seg(2, 'ask'), seg(3, 'fold'), seg(4, 'answer'),
  seg(5, 'ask'), seg(6, 'ask', { midTurn: true }), seg(7, 'answer'),
  seg(8, 'sdlv'), seg(9, 'answer'),
  seg(10, 'ask'), seg(11, 'answer'),
]

/* Ten turns: question `n * 10` opens turn n, its answer is `n * 10 + 1`. */
const TEN: Seg[] = Array.from({ length: 10 }, (_, i) => [seg((i + 1) * 10, 'ask'), seg((i + 1) * 10 + 1, 'answer')]).flat()

describe('opens', () => {
  it('is a question that opened a turn, or a delivered result', () => {
    expect(SEGS.filter(opens).map((s) => s.id)).toEqual([2, 5, 8, 10])
  })
})

describe('startIndex and endIndex', () => {
  it('reach the ends of the lane for 0 and for an id the lane no longer holds', () => {
    expect(startIndex(SEGS, 0)).toBe(0)
    expect(startIndex(SEGS, 999)).toBe(0)
    expect(endIndex(SEGS, 0)).toBe(SEGS.length)
    expect(endIndex(SEGS, 999)).toBe(SEGS.length)
  })

  it('are the indexes of the openings the window starts and stops at', () => {
    expect(startIndex(SEGS, 8)).toBe(7)
    expect(endIndex(SEGS, 8)).toBe(7)
  })
})

describe('tailFrom', () => {
  it('starts the window at the opening of the last N turns', () => {
    expect(tailFrom(SEGS, 2)).toBe(8)
    expect(tailFrom(SEGS, 3)).toBe(5)
  })

  it('mounts the whole lane when it holds no more turns than the window', () => {
    expect(tailFrom(SEGS, 4)).toBe(0)
    expect(tailFrom(SEGS, 20)).toBe(0)
    expect(tailFrom([], 20)).toBe(0)
  })
})

describe('widenFrom', () => {
  it('moves the start N turns earlier', () => {
    expect(widenFrom(SEGS, 8, 1)).toBe(5)
    expect(widenFrom(SEGS, 10, 2)).toBe(5)
  })

  it('reaches the start of the lane, leading notes included, once no N turns are left', () => {
    expect(widenFrom(SEGS, 8, 2)).toBe(0)
    expect(widenFrom(SEGS, 0, 5)).toBe(0)
  })
})

describe('widenTo', () => {
  it('moves the end N turns later', () => {
    expect(widenTo(SEGS, 5, 1)).toBe(8)
    expect(widenTo(SEGS, 5, 2)).toBe(10)
  })

  it('reaches the tail once no N turns are left, and stays there', () => {
    expect(widenTo(SEGS, 5, 3)).toBe(0)
    expect(widenTo(SEGS, 0, 1)).toBe(0)
  })
})

describe('revealWin', () => {
  it('keeps the window when the segment is already mounted, or unknown', () => {
    expect(revealWin(TEN, { from: 90, to: 0 }, 91, 3)).toEqual({ from: 90, to: 0 })
    expect(revealWin(TEN, { from: 90, to: 0 }, 999, 3)).toEqual({ from: 90, to: 0 })
    expect(revealWin(TEN, { from: 20, to: 50 }, 41, 3)).toEqual({ from: 20, to: 50 })
  })

  it('mounts N turns from the one that holds a segment far above the window', () => {
    expect(revealWin(TEN, { from: 90, to: 0 }, 21, 3)).toEqual({ from: 20, to: 50 })
  })

  it('grows the window up to a segment whose N turns reach it', () => {
    expect(revealWin(TEN, { from: 90, to: 0 }, 71, 3)).toEqual({ from: 70, to: 0 })
    expect(revealWin(TEN, { from: 90, to: 0 }, 61, 3)).toEqual({ from: 60, to: 0 })
  })

  it('mounts N turns from the one that holds a segment far below the window', () => {
    expect(revealWin(TEN, { from: 20, to: 50 }, 71, 3)).toEqual({ from: 70, to: 100 })
    expect(revealWin(TEN, { from: 20, to: 50 }, 101, 3)).toEqual({ from: 100, to: 0 })
  })

  it('grows the window down to a segment just past its end', () => {
    expect(revealWin(TEN, { from: 20, to: 50 }, 51, 3)).toEqual({ from: 20, to: 80 })
  })

  it('starts at the lane itself for a segment before the first opening', () => {
    expect(revealWin(SEGS, { from: 8, to: 0 }, 1, 1)).toEqual({ from: 0, to: 5 })
  })
})

describe('mountedTurns', () => {
  it('counts the openings inside the window', () => {
    expect(mountedTurns(SEGS, { from: 8, to: 0 })).toBe(2)
    expect(mountedTurns(SEGS, { from: 0, to: 0 })).toBe(4)
    expect(mountedTurns(TEN, { from: 20, to: 50 })).toBe(3)
  })
})
