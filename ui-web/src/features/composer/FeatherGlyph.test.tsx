// @vitest-environment happy-dom
/* The feather glyph's life on the page: when it asks for frames, when it
   stops, and which theme it paints. The engine's drawing is replaced by a
   recorder; its arithmetic has its own tests (lib/featherFall.test.ts). */
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FeatherGlyph } from './FeatherGlyph'

const drawFrame = vi.fn()
vi.mock('../../lib/featherFall', async (load) => ({
  ...(await load<typeof import('../../lib/featherFall')>()),
  featherSprites: () => ({}),
  drawFrame: (...args: unknown[]) => drawFrame(...args),
}))

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let frames = new Map<number, FrameRequestCallback>()
let nextId = 1
let hidden = false
const cancelled: number[] = []

const runFrame = (now = 16): void => {
  const pending = [...frames.entries()]
  frames = new Map()
  for (const [, cb] of pending) cb(now)
}
const flush = async (): Promise<void> => { await act(async () => { await Promise.resolve() }) }
const themeOfLastDraw = (): unknown => drawFrame.mock.calls.at(-1)?.[3]

beforeEach(() => {
  frames = new Map()
  nextId = 1
  hidden = false
  cancelled.length = 0
  drawFrame.mockClear()
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { const id = nextId++; frames.set(id, cb); return id })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => { cancelled.push(id); frames.delete(id) })
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ setTransform: () => {} } as never)
  document.documentElement.dataset.theme = 'light'
  document.documentElement.dataset.motion = 'on'
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  delete document.documentElement.dataset.theme
  delete document.documentElement.dataset.motion
})

describe('the feather glyph', () => {
  it('schedules no frame while hidden or after unmount', async () => {
    hidden = true
    const { unmount } = render(<FeatherGlyph />)
    expect(frames.size).toBe(0)

    hidden = false
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(frames.size).toBe(1)

    const pending = [...frames.keys()][0]!
    unmount()
    expect(cancelled).toContain(pending)
    expect(frames.size).toBe(0)
  })

  it('stops and resumes with the motion setting', async () => {
    render(<FeatherGlyph />)
    expect(frames.size).toBe(1)

    document.documentElement.dataset.motion = 'off'
    await flush()
    expect(frames.size).toBe(0)
    expect(drawFrame).toHaveBeenCalledTimes(1)

    document.documentElement.dataset.motion = 'on'
    await flush()
    expect(frames.size).toBe(1)
  })

  /* Browser zoom and a move to another display change the ratio without
     touching the 40x46 CSS box, so no resize is reported. */
  it('re-sets the backing size when the device pixel ratio changes', () => {
    vi.stubGlobal('devicePixelRatio', 1)
    render(<FeatherGlyph />)
    const canvas = document.querySelector('canvas.cp-feather') as HTMLCanvasElement
    expect(canvas.width).toBe(40)

    vi.stubGlobal('devicePixelRatio', 2)
    act(() => { runFrame(16) })
    expect(canvas.width).toBe(80)
    expect(canvas.height).toBe(92)
  })

  it('follows a theme switch without remounting', async () => {
    render(<FeatherGlyph />)
    const canvas = document.querySelector('canvas.cp-feather')
    act(() => { runFrame(16) })
    expect(themeOfLastDraw()).toBe('light')

    document.documentElement.dataset.theme = 'dark'
    await flush()
    act(() => { runFrame(32) })
    expect(themeOfLastDraw()).toBe('dark')
    expect(document.querySelector('canvas.cp-feather')).toBe(canvas)
    expect(canvas?.getAttribute('aria-hidden')).toBe('true')
  })
})
