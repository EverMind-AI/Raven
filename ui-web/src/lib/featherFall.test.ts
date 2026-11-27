// @vitest-environment happy-dom
/* The feather stream's arithmetic: its rhythm, its spacing over time, the
   blur it softens the sprites with, and the two page facts it reads. */
import { afterEach, describe, expect, it } from 'vitest'

import { boxBlur, featherTheme, motionAllowed, seedStream, stepStream, TAIL_STREAM } from './featherFall'

/* A small deterministic generator, so a run is the same run every time. */
function seeded(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6D2B79F5) >>> 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const W = 40
const H = 46
const DT = 1 / 60

afterEach(() => {
  delete document.documentElement.dataset.theme
  delete document.documentElement.dataset.motion
})

describe('the feather stream', () => {
  it('drops one feather every 0.8 s and never leaves the window empty', () => {
    const rnd = seeded(7)
    const fs = seedStream(TAIL_STREAM, W, H, rnd)
    const top = H * 0.18
    const bottom = H * 0.82
    let fewest = Infinity
    let most = 0
    const arrivals: number[] = []
    for (let step = 1; step <= 30 * 60; step++) {
      const before = fs.map((f) => f.y)
      stepStream(fs, TAIL_STREAM, W, H, DT, rnd)
      const t = step * DT
      fs.forEach((f, i) => { if (before[i]! < top && f.y >= top) arrivals.push(t) })
      const seen = fs.filter((f) => f.y > top && f.y < bottom).length
      if (t > 1) { fewest = Math.min(fewest, seen); most = Math.max(most, seen) }
    }
    expect(fewest).toBeGreaterThanOrEqual(1)
    expect(most).toBeLessThanOrEqual(2)
    const gaps = arrivals.slice(1).map((a, i) => a - arrivals[i]!)
    expect(gaps.length).toBeGreaterThan(30)
    for (const gap of gaps) {
      expect(gap).toBeGreaterThanOrEqual(0.78)
      expect(gap).toBeLessThanOrEqual(0.84)
    }
  })

  it('keeps the spacing over a long run', () => {
    const rnd = seeded(11)
    const fs = seedStream(TAIL_STREAM, W, H, rnd)
    for (let step = 0; step < 600 * 60; step++) stepStream(fs, TAIL_STREAM, W, H, DT, rnd)
    const span = H + 2 * TAIL_STREAM.size[1]
    const ys = fs.map((f) => ((f.y % span) + span) % span).sort((a, b) => a - b)
    const gaps = ys.map((y, i) => (i ? y - ys[i - 1]! : y + span - ys[ys.length - 1]!))
    const even = span / fs.length
    for (const gap of gaps) expect(Math.abs(gap - even)).toBeLessThan(1)
  })
})

describe('the sprite blur', () => {
  it('blurs without moving mass', () => {
    /* A small solid block, the way a stroke is: a lone pixel spreads into
       fractions below half a step, and the byte rounding would be what the
       sum measured. */
    const w = 15
    const h = 15
    const data = new Uint8ClampedArray(w * h * 4)
    for (let y = 6; y <= 8; y++) for (let x = 6; x <= 8; x++) data[(y * w + x) * 4 + 3] = 255
    const centre = (7 * w + 7) * 4
    boxBlur(data, w, h, 1)
    let sum = 0
    for (let i = 3; i < data.length; i += 4) sum += data[i]!
    expect(Math.abs(sum - 9 * 255) / (9 * 255)).toBeLessThan(0.02)
    expect(data[centre + 3]).toBeLessThan(255)
    expect(data[(1 * w + 1) * 4 + 3]).toBe(0)
  })
})

describe('what the page tells the feathers', () => {
  it('reads the theme and the motion setting', () => {
    const root = document.documentElement
    root.dataset.theme = 'dark'
    expect(featherTheme(root, false)).toBe('dark')
    root.dataset.theme = 'light'
    expect(featherTheme(root, true)).toBe('light')
    delete root.dataset.theme
    expect(featherTheme(root, true)).toBe('dark')
    expect(featherTheme(root, false)).toBe('light')

    expect(motionAllowed(root, false)).toBe(true)
    root.dataset.motion = 'off'
    expect(motionAllowed(root, false)).toBe(false)
    root.dataset.motion = 'on'
    expect(motionAllowed(root, true)).toBe(false)
  })
})
