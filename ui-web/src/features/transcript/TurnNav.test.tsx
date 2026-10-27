// @vitest-environment happy-dom
/* The turn nav beside the transcript: one mark per turn, a card naming the turn
 * on hover, and a click that takes the reader to it.
 *
 * Driven through the island's own verbs (mount.ts's ask, the lane store), the
 * way the session pipeline drives it, and read off the DOM. happy-dom has no
 * layout, so where a position matters the case pins the rects it needs and
 * says so; which mark reads as "here" is the pure `readingTurn`, asserted on
 * its own.
 */
import { act, fireEvent } from '@testing-library/react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import catalog from '../../../../i18n/messages.json'
import * as lang from '../../state/lang'
import { hold as holdHost } from '../../state/session/hosts'
import { resetSources, setSources } from '../../state/sources'
import * as mount from './mount'
import * as store from './store'
import * as tail from './tail'
import { TranscriptApp } from './TranscriptApp'
import { readingTurn } from './TurnNav'

import type { Root } from 'react-dom/client'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let root: Root | null = null

const $ = <T extends Element = HTMLElement>(sel: string): T | null => document.querySelector<T>(sel)
const marks = (): HTMLButtonElement[] => [...document.querySelectorAll<HTMLButtonElement>('.transcript-navmark')]
const stage = (): HTMLElement => $('#stage') as HTMLElement
const laneHost = (): HTMLElement => $('#stage > [data-tsl]') as HTMLElement
/* A message catalogue entry in the other language, filled the way t() fills
   it: the expectation is read from the catalogue file rather than spelled
   here. */
const zh = (key: string, n: number): string =>
  String((catalog.ui as Record<string, { zh?: string }>)[key]?.zh).replace('{n}', String(n))

/* The host swap is seen through a MutationObserver, whose records arrive in a
   microtask after the change: let them land inside act so the re-render
   commits. */
async function settle(fn: () => void = () => {}): Promise<void> {
  await act(async () => {
    fn()
    await new Promise((r) => setTimeout(r, 0))
  })
}

beforeEach(async () => {
  setSources({
    transcript: { clean: (t) => String(t ?? '').trim(), okOf: () => true },
    prose: { pathOf: () => null, linkTargetOf: () => null },
  })
  document.body.innerHTML = '<div class="chat">'
    + '<div class="scroll" id="scroll"><div class="col" id="stage"></div></div>'
    + '<div id="turnNav"></div></div>'
  tail._resetForTests()
  root = createRoot($('#turnNav') as HTMLElement)
  /* The domain's root, as src/main.tsx mounts it, rather than the nav alone:
     the language subscription is the root's. */
  await settle(() => root!.render(<TranscriptApp />))
})

afterEach(() => {
  act(() => root?.unmount())
  root = null
  vi.useRealTimers()
  vi.restoreAllMocks()
  resetSources()
  document.body.innerHTML = ''
})

describe('the turn nav', () => {
  it('draws nothing for a single turn, then one mark per turn', async () => {
    await settle(() => { mount.ask('first question') })
    expect($('.transcript-nav')).toBeNull()
    await settle(() => { mount.ask('second question') })
    expect(marks()).toHaveLength(2)
    await settle(() => { mount.ask('third question') })
    expect(marks()).toHaveLength(3)
  })

  it('scrolls a strip too long for its band without a scrollbar beside it', async () => {
    /* The overlay scrollbar (src/chrome/behaviour/scrollbars.ts) draws a thumb
       for every scroller unless it declares it wants none; the strip is its
       own indicator of where the reader is, so it declares. */
    await settle(() => {
      mount.ask('first question')
      mount.ask('second question')
    })
    expect($('.transcript-navlist')?.dataset.thumb).toBe('none')
  })

  it('keeps the mark being read centred in a strip too long for its band', async () => {
    await settle(() => {
      for (const n of [1, 2, 3, 4, 5, 6]) mount.ask(`question ${n}`)
    })
    /* The strip's geometry, which happy-dom does not lay out: 10px marks
       under 12px of fade, a 40px window over 84px of marks. */
    const list = $('.transcript-navlist') as HTMLElement
    Object.defineProperty(list, 'clientHeight', { configurable: true, value: 40 })
    Object.defineProperty(list, 'scrollHeight', { configurable: true, value: 84 })
    Object.defineProperty(list, 'scrollTop', { configurable: true, writable: true, value: 0 })
    marks().forEach((m, i) => {
      Object.defineProperty(m, 'offsetTop', { configurable: true, value: 12 + i * 10 })
      Object.defineProperty(m, 'offsetHeight', { configurable: true, value: 10 })
    })
    const scroll = $('#scroll') as HTMLElement
    vi.spyOn(scroll, 'getBoundingClientRect').mockReturnValue({ top: 0, height: 1000 } as DOMRect)
    Object.defineProperty(scroll, 'scrollHeight', { configurable: true, value: 9000 })
    Object.defineProperty(scroll, 'clientHeight', { configurable: true, value: 1000 })
    const tops = [-900, -700, -500, 100, 800, 1500]
    stage().querySelectorAll<HTMLElement>('[data-ask]').forEach((el, i) => {
      vi.spyOn(el, 'getBoundingClientRect').mockImplementation(() => ({ top: tops[i] }) as DOMRect)
    })

    await settle(() => { scroll.dispatchEvent(new Event('scroll')) })
    await settle()
    expect(marks()[3]!.getAttribute('aria-current')).toBe('true')
    /* The fourth mark's centre (12 + 30 + 5 = 47) on the window's (20). */
    expect(list.scrollTop).toBe(27)

    /* Near either end the strip stops at its edge rather than centring past it. */
    tops.splice(0, 6, -900, -700, -500, -300, -100, 100)
    await settle(() => { scroll.dispatchEvent(new Event('scroll')) })
    await settle()
    expect(marks()[5]!.getAttribute('aria-current')).toBe('true')
    /* Its centre would want 47; the strip ends at 84 - 40. */
    expect(list.scrollTop).toBe(44)
  })

  it('opens no mark for a message merged into the turn that was running', async () => {
    await settle(() => {
      mount.ask('first question')
      mount.ask('and also this', null, { midTurn: true })
      mount.ask('second question')
    })
    expect(marks()).toHaveLength(2)
    expect(marks().map((m) => m.getAttribute('aria-label'))).toEqual([
      'Turn 1: first question',
      'Turn 2: second question',
    ])
  })

  it('says which turn, how long ago and what was asked while the pointer is on its mark', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    const t0 = new Date('2026-10-26T08:00:00Z').getTime()
    vi.setSystemTime(t0)
    await settle(() => { mount.ask('how do I fix these problems?') })
    vi.setSystemTime(t0 + 5 * 60_000)
    await settle(() => { mount.ask('now push it') })
    vi.setSystemTime(t0 + 65 * 60_000)

    expect($('.transcript-navtip')).toBeNull()
    act(() => { fireEvent.mouseEnter(marks()[0]!) })
    expect($('.transcript-navwhen')?.textContent).toBe('Turn 1 · 1h ago')
    expect($('.transcript-navsaid')?.textContent).toBe('how do I fix these problems?')

    act(() => { fireEvent.mouseLeave(marks()[0]!) })
    expect($('.transcript-navtip')).toBeNull()

    /* The keyboard reaches the same card: a mark is a button. */
    act(() => { fireEvent.focus(marks()[1]!) })
    expect($('.transcript-navwhen')?.textContent).toBe('Turn 2 · 1h ago')
    expect($('.transcript-navsaid')?.textContent).toBe('now push it')
  })

  it('leaves the time off a turn whose entry carried no usable stamp', async () => {
    await settle(() => {
      mount.ask('restored without a stamp', '10-23 16:13')
      mount.ask('live one')
    })
    act(() => { fireEvent.mouseEnter(marks()[0]!) })
    expect($('.transcript-navwhen')?.textContent).toBe('Turn 1')
  })

  it('says its words in the language the reader picks', async () => {
    await settle(() => {
      mount.ask('first question')
      mount.ask('second question')
    })
    expect(marks()[0]!.getAttribute('aria-label')).toBe('Turn 1: first question')
    try {
      act(() => { lang.set('zh') })
      expect(zh('gui.transcript.nav_turn', 1)).not.toBe('Turn 1')
      expect(marks()[0]!.getAttribute('aria-label')).toBe(`${zh('gui.transcript.nav_turn', 1)}: first question`)
      act(() => { fireEvent.mouseEnter(marks()[1]!) })
      expect($('.transcript-navwhen')?.textContent?.startsWith(zh('gui.transcript.nav_turn', 2))).toBe(true)
    } finally {
      act(() => { lang.set('en') })
    }
    expect(marks()[0]!.getAttribute('aria-label')).toBe('Turn 1: first question')
  })

  it('names what opened a turn nobody typed', async () => {
    await settle(() => {
      mount.ask('first question')
      store.askAuto(mount.mainLane(), 'heartbeat', '')
    })
    act(() => { fireEvent.mouseEnter(marks()[1]!) })
    expect($('.transcript-navsaid')?.textContent).toBe('Heartbeat')
  })

  it('takes the reader to the turn and stops following the tail', async () => {
    await settle(() => {
      mount.ask('first question')
      mount.ask('second question')
    })
    const scroll = $('#scroll') as HTMLElement
    const turns = [...stage().querySelectorAll<HTMLElement>('[data-ask]')]
    expect(turns).toHaveLength(2)
    /* Layout the case needs and happy-dom cannot give: the scroller starts
       52px down the window and is 900px into the transcript, and the first
       question sits 300px above the scroller's top edge. */
    vi.spyOn(scroll, 'getBoundingClientRect').mockReturnValue({ top: 52, height: 600 } as DOMRect)
    vi.spyOn(turns[0]!, 'getBoundingClientRect').mockReturnValue({ top: -248 } as DOMRect)
    scroll.scrollTop = 900
    const to = vi.spyOn(scroll, 'scrollTo').mockImplementation(() => {})
    expect(tail.isStuck()).toBe(true)

    act(() => { fireEvent.click(marks()[0]!) })

    expect(to).toHaveBeenCalledTimes(1)
    /* 900 + (-248 - 52), less the lead that keeps the question off the edge. */
    expect((to.mock.calls[0]![0] as ScrollToOptions).top).toBe(900 - 300 - 16)
    expect(tail.isStuck()).toBe(false)
  })

  it('marks the turn being read as the reader scrolls', async () => {
    await settle(() => {
      mount.ask('first question')
      mount.ask('second question')
      mount.ask('third question')
    })
    const scroll = $('#scroll') as HTMLElement
    const turns = [...stage().querySelectorAll<HTMLElement>('[data-ask]')]
    vi.spyOn(scroll, 'getBoundingClientRect').mockReturnValue({ top: 0, height: 1000 } as DOMRect)
    Object.defineProperty(scroll, 'scrollHeight', { configurable: true, value: 5000 })
    Object.defineProperty(scroll, 'clientHeight', { configurable: true, value: 1000 })
    const tops = [-1500, 200, 1400]
    turns.forEach((el, i) => {
      vi.spyOn(el, 'getBoundingClientRect').mockImplementation(() => ({ top: tops[i] }) as DOMRect)
    })

    await settle(() => { scroll.dispatchEvent(new Event('scroll')) })
    await settle()
    expect(marks().map((m) => m.getAttribute('aria-current'))).toEqual([null, 'true', null])

    tops.splice(0, 3, -2600, -900, 300)
    await settle(() => { scroll.dispatchEvent(new Event('scroll')) })
    await settle()
    expect(marks().map((m) => m.getAttribute('aria-current'))).toEqual([null, null, 'true'])
  })

  it('follows the conversation on the stage, not one held off it', async () => {
    await settle(() => {
      mount.ask('first question')
      mount.ask('second question')
    })
    expect(marks()).toHaveLength(2)

    /* Leaving a conversation mid-turn takes its host off the stage and files it
       (state/session/residency.ts); its lane is still alive, and must not be
       what the nav draws for the conversation that replaces it. */
    const host = laneHost()
    holdHost(host)
    await settle(() => { host.remove() })
    expect($('.transcript-nav')).toBeNull()

    await settle(() => {
      mount.ask('another conversation')
      mount.ask('its second turn')
      mount.ask('its third turn')
    })
    expect(marks()).toHaveLength(3)

    /* Coming back puts the held host back, and the nav with it. */
    await settle(() => {
      laneHost().remove()
      stage().appendChild(host)
    })
    expect(marks()).toHaveLength(2)
  })

  it('goes when the stage is wiped for a new conversation', async () => {
    await settle(() => {
      mount.ask('first question')
      mount.ask('second question')
    })
    expect(marks()).toHaveLength(2)
    await settle(() => { stage().innerHTML = '' })
    expect($('.transcript-nav')).toBeNull()
  })

  it('marks a turn the model chose not to answer by its question, live and after a reload', async () => {
    /* A silent turn has its question and one quiet line, and no answer block:
       the mark is the question's, so it needs nothing the answer would carry. */
    await settle(() => {
      mount.ask('anything new?')
      const st = mount.step()
      st.sayDelta('Checking the logs.')
      st.seal()
      st.dropReply()
      mount.note('Raven chose not to reply.', '', { quiet: true })
      mount.finishTurn(null, [st], '3s', true)
      mount.ask('and now?')
    })
    expect($('.answer')).toBeNull()
    expect(marks().map((m) => m.getAttribute('aria-label'))).toEqual(['Turn 1: anything new?', 'Turn 2: and now?'])
    act(() => { fireEvent.mouseEnter(marks()[0]!) })
    expect($('.transcript-navsaid')?.textContent).toBe('anything new?')

    await settle(() => { stage().innerHTML = '' })
    const t0 = Date.now() - 60_000
    await settle(() => {
      mount.history([
        { role: 'user', text: 'anything new?', timestamp: new Date(t0).toISOString() },
        { role: 'assistant', text: '[SILENT]', silent: true, timestamp: new Date(t0 + 5_000).toISOString() },
        { role: 'user', text: 'and now?', timestamp: new Date(t0 + 20_000).toISOString() },
        { role: 'assistant', text: 'Still nothing.', timestamp: new Date(t0 + 25_000).toISOString() },
      ])
    })
    expect(marks().map((m) => m.getAttribute('aria-label'))).toEqual(['Turn 1: anything new?', 'Turn 2: and now?'])
    expect(document.body.textContent).not.toContain('[SILENT]')
  })
})

describe('readingTurn', () => {
  it('is the last question at or above the reading line', () => {
    expect(readingTurn([-500, 120, 900], 300, false)).toBe(1)
    expect(readingTurn([-500, -20, 299], 300, false)).toBe(2)
  })

  it('is the first turn while every question is still below the line', () => {
    expect(readingTurn([400, 900], 300, false)).toBe(0)
  })

  it('is the last turn once the reader is at the tail', () => {
    expect(readingTurn([-900, -400, 700], 300, true)).toBe(2)
  })

  it('is no turn when there are none', () => {
    expect(readingTurn([], 300, false)).toBe(-1)
  })
})
