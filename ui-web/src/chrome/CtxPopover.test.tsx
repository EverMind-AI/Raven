// @vitest-environment happy-dom
/* What the context panel says about the window.
 *
 * The panel is the ring's other half: the same numbers, said as an analysis.
 * So the first thing it has to get right is not to disagree with the ring --
 * the headline is the ring's own number, and the parts under it are what the
 * loop measured on the request it sent.
 */
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { resetTranslator, setCode, t } from '../i18n/t'
import * as ctx from '../state/ctxWindow'
import { CtxChip } from './CtxChip'
import { CtxPopover } from './CtxPopover'

const mount = (): void => {
  render(
    <>
      <CtxChip />
      <CtxPopover />
    </>,
    { container: document.body.appendChild(document.createElement('div')) },
  )
}

const panel = (): HTMLElement => document.getElementById('ctxPop') as HTMLElement
const chip = (): HTMLButtonElement => document.getElementById('ctxChip') as HTMLButtonElement
const text = (sel: string): string => panel().querySelector(sel)?.textContent ?? ''

afterEach(() => {
  cleanup()
  ctx._resetForTests()
  resetTranslator()
  setCode('en')
  document.body.innerHTML = ''
})

describe('the context panel', () => {
  it('opens when the ring is clicked, and closes when it is clicked again', () => {
    mount()
    act(() => { ctx.set(100_000, 200_000) })
    expect(panel().dataset.open).toBe('false')
    act(() => { chip().click() })
    expect(panel().dataset.open).toBe('true')
    expect(chip().getAttribute('aria-expanded')).toBe('true')
    act(() => { chip().click() })
    expect(panel().dataset.open).toBe('false')
  })

  it('closes when a click lands outside it', () => {
    mount()
    act(() => { ctx.set(100_000, 200_000) })
    act(() => { chip().click() })
    expect(panel().dataset.open).toBe('true')
    act(() => { fireEvent.pointerDown(document.body) })
    expect(panel().dataset.open).toBe('false')
  })

  it('says how full the window is, in the ring\'s own numbers', () => {
    mount()
    act(() => { ctx.set(175_600, 200_000) })
    expect(text('.chrome-ctx-used')).toBe('175.6K')
    expect(text('.chrome-ctx-of')).toBe('of 200K tokens')
    expect(text('.chrome-ctx-pct')).toBe('88%')
    expect(text('.chrome-ctx-left')).toBe('24.4K left')
  })

  it('lists the parts the turn divided', () => {
    mount()
    act(() => {
      ctx.set(175_600, 200_000, {
        system: 4_200,
        tools: 12_800,
        conversation: 108_600,
        memory: 2_100,
        skills: 3_300,
      })
    })
    const rows = Array.from(panel().querySelectorAll('.chrome-ctx-row'))
    expect(rows.map((r) => r.querySelector('.chrome-ctx-name')?.textContent)).toEqual([
      'System prompt',
      'Tools',
      'Conversation',
      'Memory',
      'Skills',
    ])
    expect(rows.map((r) => r.querySelector('.chrome-ctx-num')?.textContent)).toEqual([
      '4.2K',
      '12.8K',
      '108.6K',
      '2.1K',
      '3.3K',
    ])
  })

  it('says so when no turn has divided the window', () => {
    mount()
    act(() => { ctx.set(100_000, 200_000) })
    expect(panel().querySelector('.chrome-ctx-none')?.textContent).toBeTruthy()
    expect(panel().querySelector('.chrome-ctx-rows')).toBe(null)
  })

  it('draws the fill as cells, coloured by the part they belong to', () => {
    mount()
    act(() => {
      ctx.set(200_000, 200_000, {
        system: 50_000,
        tools: 50_000,
        conversation: 60_000,
        memory: 25_000,
        skills: 15_000,
      })
    })
    const cells = Array.from(panel().querySelectorAll('.chrome-ctx-cell'))
    expect(cells).toHaveLength(112)
    expect(cells.filter((c) => c.getAttribute('data-part') === 'system')).toHaveLength(28)
    expect(cells.filter((c) => c.getAttribute('data-part') === 'tools')).toHaveLength(28)
    expect(cells.filter((c) => c.getAttribute('data-part') === 'conversation')).toHaveLength(34)
    expect(cells.filter((c) => c.getAttribute('data-part') === 'memory')).toHaveLength(14)
    expect(cells.filter((c) => c.getAttribute('data-part') === 'skills')).toHaveLength(8)
  })

  /* The catalogue's zh words, read rather than copied: the source stays English,
     and the en strings taken first make the case show the zh column was read. */
  it('answers in the language the page is in', () => {
    const en = { title: t('gui.ctx.title'), left: t('gui.ctx.left', { n: '100K' }) }
    setCode('zh')
    mount()
    act(() => { ctx.set(100_000, 200_000) })
    expect(text('.chrome-ctx-title')).toBe(t('gui.ctx.title'))
    expect(text('.chrome-ctx-title')).not.toBe(en.title)
    expect(text('.chrome-ctx-left')).toBe(t('gui.ctx.left', { n: '100K' }))
    expect(text('.chrome-ctx-left')).not.toBe(en.left)
  })
})
