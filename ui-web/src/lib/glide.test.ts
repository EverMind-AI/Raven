// @vitest-environment happy-dom
/* The panel glide, laid out by hand: happy-dom lays nothing out, so the
 * panel's `offsetHeight` is a getter that answers the pinned height while one
 * is set and the content's own height otherwise, and logs what it was asked
 * under -- which is how a test sees the glide start from the old height. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { GLIDE_MS, glide } from './glide'

let natural = 300
let reads: string[] = []
let panel: HTMLDivElement
let content: HTMLDivElement
let live = true

const flush = async (): Promise<void> => {
  await Promise.resolve()
  await Promise.resolve()
}
const grow = async (to: number): Promise<void> => {
  natural = to
  content.appendChild(document.createElement('p'))
  await flush()
}
const ended = (property = 'height'): void => {
  panel.dispatchEvent(Object.assign(new Event('transitionend'), { propertyName: property }))
}

beforeEach(() => {
  natural = 300
  reads = []
  live = true
  panel = document.createElement('div')
  content = document.createElement('div')
  panel.appendChild(content)
  document.body.appendChild(panel)
  Object.defineProperty(panel, 'offsetHeight', {
    configurable: true,
    get: () => {
      reads.push(panel.style.height)
      return panel.style.height ? parseFloat(panel.style.height) : natural
    },
  })
})

afterEach(() => {
  panel.remove()
  vi.useRealTimers()
})

describe('glide', () => {
  it('pins the old height, then hands the panel the new one to transition to', async () => {
    const g = glide(panel, content, () => live)
    await grow(420)
    expect(reads).toContain('300px')
    expect(panel.style.height).toBe('420px')
    expect(panel.dataset.glide).toBe('true')
    ended()
    expect(panel.style.height).toBe('')
    expect(panel.dataset.glide).toBeUndefined()
    g.stop()
  })

  it('lands on its own transition only, not on a child one', async () => {
    const g = glide(panel, content, () => live)
    await grow(420)
    ended('opacity')
    content.dispatchEvent(Object.assign(new Event('transitionend', { bubbles: true }), { propertyName: 'height' }))
    expect(panel.style.height).toBe('420px')
    g.stop()
  })

  it('re-aims a glide under way from where it has got to', async () => {
    const g = glide(panel, content, () => live)
    await grow(420)
    /* Half way there, as the transition would have it. */
    panel.style.height = '360px'
    reads = []
    await grow(500)
    expect(reads).toContain('360px')
    expect(panel.style.height).toBe('500px')
    g.stop()
  })

  it('does not move a panel that is not live, nor one the change left the same height', async () => {
    const g = glide(panel, content, () => live)
    live = false
    await grow(420)
    expect(panel.style.height).toBe('')
    live = true
    await grow(420.4)
    expect(panel.style.height).toBe('')
    expect(panel.dataset.glide).toBeUndefined()
    g.stop()
  })

  it('takes a new card at its own height, and glides the next change from there', async () => {
    const frames: FrameRequestCallback[] = []
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => frames.push(cb))
    const g = glide(panel, content, () => live)
    g.settle()
    await grow(520)
    expect(panel.style.height).toBe('')
    frames.shift()!(0)
    reads = []
    await grow(600)
    expect(reads).toContain('520px')
    expect(panel.style.height).toBe('600px')
    g.stop()
    vi.restoreAllMocks()
  })

  it('jumps for a reader who asked for less motion', async () => {
    vi.spyOn(window, 'matchMedia').mockImplementation(
      (query: string) => ({ matches: query.includes('reduce'), media: query }) as MediaQueryList,
    )
    const g = glide(panel, content, () => live)
    await grow(420)
    expect(panel.style.height).toBe('')
    g.stop()
    vi.restoreAllMocks()
  })

  it('lets go of the pin on a timer when no transitionend comes', async () => {
    vi.useFakeTimers()
    const g = glide(panel, content, () => live)
    await grow(420)
    expect(panel.style.height).toBe('420px')
    vi.advanceTimersByTime(GLIDE_MS + 100)
    expect(panel.style.height).toBe('')
    g.stop()
  })

  it('stops watching, and leaves no pin behind', async () => {
    const g = glide(panel, content, () => live)
    await grow(420)
    g.stop()
    expect(panel.style.height).toBe('')
    await grow(600)
    expect(panel.style.height).toBe('')
  })
})
