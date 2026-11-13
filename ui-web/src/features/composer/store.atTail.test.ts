// @vitest-environment happy-dom
/* The composer's scroll listeners read the scroller's own geometry. With a
   Lane Window that stops short of the tail, that geometry is the window's, not
   the conversation's: reaching the window's bottom must not turn tail-follow
   on, or the next append replaces the window with the conversation's last 20
   turns and the reader is moved to the tail. */
import { act } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import { setSources } from '../../state/sources'
import { installWsPane } from '../../test/wsPaneHarness'
import * as mount from '../transcript/mount'
import * as store from '../transcript/store'
import * as tail from '../transcript/tail'
import * as composer from './store'

import type { HistoryMessage, TranscriptSource } from '../transcript/types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const wire = (): void => {
  setTranslator((key) => `en:${key}`)
  installWsPane()
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation((_t, _b, _l, fn) => fn())
  const source: TranscriptSource = {
    clean: (t) => String(t == null ? '' : t).trim(),
    okOf: (_n, p) => !/^\s*(error|traceback|failed)\b/i.test(p),
  }
  setSources({ transcript: source })
  document.body.innerHTML = '<div id="scroll"><div class="col" id="stage"></div></div>'
    + '<button class="backpill chrome-toppill" id="toppill" hidden></button>'
    + '<button class="backpill" id="backpill" hidden></button>'
}

/* Fifty question-and-answer turns a minute apart. */
const turns = (n: number): HistoryMessage[] => {
  const t0 = Date.parse('2026-11-13T08:00:00Z')
  const out: HistoryMessage[] = []
  for (let i = 1; i <= n; i += 1) {
    const at = new Date(t0 + i * 60_000).toISOString()
    out.push({ role: 'user', text: `question ${i}`, timestamp: at })
    out.push({ role: 'assistant', text: `answer ${i}`, timestamp: at })
  }
  return out
}

const scroller = (): HTMLElement => document.getElementById('scroll') as HTMLElement
const asks = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('#stage [data-ask]')]
const firstAsk = (): string => (asks()[0]?.querySelector('.msg.me')?.textContent || '').trim()
const idOf = (n: number): number =>
  mount.shownLane()!.segs.find((s) => s.kind === 'ask' && s.body === `question ${n}`)!.id

/* The scroller's size, which happy-dom does not lay out. */
const size = (height: number, client: number, top: number): void => {
  const sc = scroller()
  Object.defineProperty(sc, 'scrollHeight', { configurable: true, value: height })
  Object.defineProperty(sc, 'clientHeight', { configurable: true, value: client })
  sc.scrollTop = top
}

beforeEach(() => {
  store._resetForTests()
  tail._resetForTests()
  composer._resetForTests()
  wire()
})

describe('the composer at the end of a Lane Window', () => {
  it('does not turn tail-follow on at the bottom of a window short of the tail', () => {
    act(() => { mount.history(turns(50)) })
    act(() => { mount.reveal(idOf(5)) })
    expect(firstAsk()).toBe('question 5')
    /* What goTo does after a jump: the reader is no longer following. */
    tail.setStuck(false)
    /* The reader scrolls to the bottom of the twenty turns the jump mounted. */
    size(5000, 500, 4500)
    act(() => { composer.scrolled() })
    expect(tail.isStuck()).toBe(false)
    /* The next append is a streaming frame's `poke` in production. It must not
       run `windowToTail`: the window stays on the turn the reader is reading. */
    act(() => { mount.answer('a late reply') })
    expect(firstAsk()).toBe('question 5')
    expect(asks()).toHaveLength(20)
  })

  it('still offers the way down to the tail at the bottom of a window short of it', () => {
    act(() => { mount.history(turns(50)) })
    act(() => { mount.reveal(idOf(5)) })
    tail.setStuck(false)
    size(5000, 500, 4500)
    /* The scroll that reaches the window's bottom also paints the pills. */
    act(() => { composer.scrolled() })
    expect((document.getElementById('backpill') as HTMLElement).hidden).toBe(false)
  })

  it('takes the reader back to the tail when the down pill is clicked', () => {
    act(() => { mount.history(turns(50)) })
    act(() => { mount.reveal(idOf(5)) })
    tail.setStuck(false)
    size(5000, 500, 4500)
    act(() => { composer.scrolled() })
    const to = vi.spyOn(scroller(), 'scrollTo').mockImplementation(() => {})
    act(() => { composer.pillClick() })
    expect(tail.isStuck()).toBe(true)
    expect(firstAsk()).toBe('question 31')
    expect(to).toHaveBeenCalled()
  })

  it('still turns tail-follow on at the bottom of the conversation', () => {
    act(() => { mount.history(turns(50)) })
    tail.setStuck(false)
    size(5000, 500, 4500)
    act(() => { composer.scrolled() })
    expect(tail.isStuck()).toBe(true)
  })
})
