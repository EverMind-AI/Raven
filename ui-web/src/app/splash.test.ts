// @vitest-environment happy-dom
/* Taking the splash down: the floor on its display time, the fade, and the two
 * shapes a caller asks for it in (the demo shell's 250, the boot's default).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const FLOOR = 600
const FADE = 560

/* Fresh module state per case: the clock the floor measures from is module
   scope, and a case that marked it must not be visible to one that does not. */
async function fresh(): Promise<typeof import('./splash')> {
  vi.resetModules()
  return import('./splash')
}

const splash = (): HTMLElement | null => document.getElementById('splash')

beforeEach(() => {
  vi.useFakeTimers()
  document.body.innerHTML = '<div id="splash" aria-hidden="true"></div>'
})

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('hiding the splash', () => {
  it('holds it for the floor, fades it, then takes it out', async () => {
    const { hideSplash, markStart } = await fresh()
    markStart()
    hideSplash()
    vi.advanceTimersByTime(FLOOR - 1)
    expect(splash()!.dataset.off).toBeUndefined()
    vi.advanceTimersByTime(1)
    expect(splash()!.dataset.off).toBe('1')
    vi.advanceTimersByTime(FADE - 1)
    expect(splash()).not.toBeNull()
    vi.advanceTimersByTime(1)
    expect(splash()).toBeNull()
  })

  it('counts the time it has already been up against the floor', async () => {
    const { hideSplash, markStart } = await fresh()
    markStart()
    vi.advanceTimersByTime(500)
    hideSplash()
    vi.advanceTimersByTime(100)
    expect(splash()!.dataset.off).toBe('1')
  })

  it('takes the floor the caller names -- 250 from the load handler', async () => {
    const { hideSplash, markStart } = await fresh()
    markStart()
    hideSplash(250)
    vi.advanceTimersByTime(249)
    expect(splash()!.dataset.off).toBeUndefined()
    vi.advanceTimersByTime(1)
    expect(splash()!.dataset.off).toBe('1')
  })

  /* What the connection surface asks for when the desktop shell answers: no
     floor at all. */
  it('lifts it on the next tick when asked for no floor', async () => {
    const { hideSplash, markStart } = await fresh()
    markStart()
    hideSplash(0)
    vi.advanceTimersByTime(0)
    expect(splash()!.dataset.off).toBe('1')
  })

  /* Before anything has marked the clock, the floor is already spent. */
  it('lifts it at once while the clock is unmarked', async () => {
    const { hideSplash } = await fresh()
    hideSplash()
    vi.advanceTimersByTime(0)
    expect(splash()!.dataset.off).toBe('1')
  })

  it('does nothing when the splash has already gone', async () => {
    const { hideSplash, markStart } = await fresh()
    markStart()
    document.body.innerHTML = ''
    hideSplash()
    expect(vi.getTimerCount()).toBe(0)
  })
})
