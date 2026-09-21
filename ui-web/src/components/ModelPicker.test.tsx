// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import { ModelPicker } from './ModelPicker'

import type { PickerProvider } from './ModelPicker'

const PROVIDERS: PickerProvider[] = [
  { id: 'anthropic', name: 'Anthropic', models: ['claude-opus-4-5', 'claude-sonnet-4-5'], labels: { 'claude-opus-4-5': { label: 'Opus', context_window: 200000 } } },
  { id: 'openrouter', name: 'OpenRouter', models: ['openai/gpt-4o'] },
]

function draw(over: Partial<Parameters<typeof ModelPicker>[0]> = {}) {
  const picks: Array<[string, string, boolean]> = []
  const closes: number[] = []
  render(
    <ModelPicker
      title="Chat"
      providers={PROVIDERS}
      current={{ model: 'openai/gpt-4o', provider: 'openrouter' }}
      onPick={(m, p, typed) => picks.push([m, p, typed])}
      onClose={() => closes.push(1)}
      emptyNote="none"
      {...over}
    />,
  )
  return { picks, closes }
}

const measured = Object.getOwnPropertyDescriptor(HTMLDivElement.prototype, 'getBoundingClientRect')

afterEach(() => {
  cleanup()
  resetTranslator()
  document.body.innerHTML = ''
  if (measured) Object.defineProperty(HTMLDivElement.prototype, 'getBoundingClientRect', measured)
})

describe('model picker', () => {
  it('marks every provider row with its own mark, not just its name', () => {
    /* The icons were dropped on the grounds that the table lived across a
       forbidden import edge. It does not: `ProviderMark` is in this same
       layer, and the gate only forbids `components -> features`. The composer's
       picker and onboarding both show them, so the settings picker was the
       only surface without. */
    draw()
    const rows = document.querySelectorAll('.model-picker-prov')
    expect(rows.length).toBe(2)
    for (const row of rows) {
      expect(row.querySelector('img, svg, [class*=mark], [class*=ico]'), row.textContent || '').toBeTruthy()
    }
  })

  it('opens on the current provider, shows its models with names and windows, and marks the current one', () => {
    setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
    draw()
    expect(screen.getByText('OpenRouter').closest('button')!.getAttribute('aria-current')).toBe('true')
    expect(screen.getByText('openai/gpt-4o').closest('button')!.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(screen.getByText('Anthropic'))
    expect(screen.getByText('Opus')).toBeTruthy()
    expect(screen.getByText('200k')).toBeTruthy()
  })

  it('picks a listed model as not typed, and a typed id as typed against the shown provider', () => {
    setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
    const { picks } = draw()
    fireEvent.click(screen.getByText('openai/gpt-4o'))
    const box = screen.getByPlaceholderText('gui.model.pick_search')
    fireEvent.change(box, { target: { value: 'sonnet' } })
    /* The filter moves to the provider that has a hit. */
    expect(screen.getByText('claude-sonnet-4-5')).toBeTruthy()
    fireEvent.change(box, { target: { value: 'brand-new-model' } })
    fireEvent.click(screen.getByText('gui.model.pick_use {"id":"brand-new-model"}'))
    expect(picks).toEqual([['openai/gpt-4o', 'openrouter', false], ['brand-new-model', 'anthropic', true]])
  })

  it('Enter takes the first match, Escape closes, and an exact typed id is not offered twice', () => {
    setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
    const { picks, closes } = draw()
    const box = screen.getByPlaceholderText('gui.model.pick_search')
    fireEvent.change(box, { target: { value: 'openai/gpt-4o' } })
    expect(screen.queryByText('gui.model.pick_use {"id":"openai/gpt-4o"}')).toBeNull()
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(picks).toEqual([['openai/gpt-4o', 'openrouter', false]])
    fireEvent.keyDown(box, { key: 'Escape' })
    expect(closes).toEqual([1])
  })

  it('offers listed ids only when the caller cannot take a typed one', () => {
    setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
    const { picks } = draw({ allowTyped: false })
    const box = screen.getByPlaceholderText('gui.model.pick_search')
    fireEvent.change(box, { target: { value: 'brand-new-model' } })
    expect(screen.queryByText('gui.model.pick_use {"id":"brand-new-model"}')).toBeNull()
    expect(screen.getByText('gui.model.pick_none')).toBeTruthy()
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(picks).toEqual([])
    fireEvent.change(box, { target: { value: 'sonnet' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(picks).toEqual([['claude-sonnet-4-5', 'anthropic', false]])
  })

  it('says why the list is empty when no provider can serve the role', () => {
    draw({ providers: [], current: null })
    expect(screen.getByText('none')).toBeTruthy()
  })
})

/* An anchor whose rect the placement can read, inside a dialog with a rect of
   its own. happy-dom measures everything as zero, so the geometry under test
   has to be the geometry this states. */
function anchored(rowTop: number): HTMLButtonElement {
  const dialog = document.createElement('div')
  dialog.className = 'smodal'
  dialog.getBoundingClientRect = () => ({ top: 100, bottom: 780, left: 200, right: 1200, width: 1000, height: 680, x: 200, y: 100, toJSON: () => ({}) }) as DOMRect
  const row = document.createElement('button')
  row.getBoundingClientRect = () => ({ top: rowTop, bottom: rowTop + 30, left: 700, right: 900, width: 200, height: 30, x: 700, y: rowTop, toJSON: () => ({}) }) as DOMRect
  dialog.appendChild(row)
  document.body.appendChild(dialog)
  return row
}

/* The panel's own size, which `anchorRow` reads back after parking it at 0,0.
   400 tall is what the settings stylesheet gives it. */
function sized(): void {
  Object.defineProperty(HTMLDivElement.prototype, 'getBoundingClientRect', {
    configurable: true,
    writable: true,
    value(this: HTMLDivElement) {
      if (!this.classList.contains('model-picker')) return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
      const top = parseFloat(this.style.top || '0')
      const left = parseFloat(this.style.left || '0')
      return { top, bottom: top + 400, left, right: left + 560, width: 560, height: 400, x: left, y: top, toJSON: () => ({}) } as DOMRect
    },
  })
}

describe('the picker floats against the row that opened it', () => {
  it('hangs below a row with room under it', () => {
    sized()
    const row = anchored(200)
    draw({ anchor: row })
    const pop = screen.getByRole('dialog') as HTMLDivElement
    expect(pop.style.position).toBe('fixed')
    /* 230 is the row's bottom, plus the 6px gap. */
    expect(pop.style.top).toBe('236px')
  })

  it('flips above a row the panel would overrun', () => {
    sized()
    /* Bottom at 730, and 400 more would end at 1136 -- past the dialog's 780. */
    const row = anchored(700)
    draw({ anchor: row })
    const pop = screen.getByRole('dialog') as HTMLDivElement
    /* The row's top less the panel and the gap. */
    expect(pop.style.top).toBe('294px')
  })

  it('closes once the row it names has scrolled away, and not before', () => {
    sized()
    let top = 200
    const row = anchored(top)
    row.getBoundingClientRect = () => ({ top, bottom: top + 30, left: 700, right: 900, width: 200, height: 30, x: 700, y: top, toJSON: () => ({}) }) as DOMRect
    const { closes } = draw({ anchor: row })
    document.dispatchEvent(new Event('scroll', { bubbles: true }))
    expect(closes, 'a scroll that did not move the row is not a reason to close').toEqual([])
    top = 120
    document.dispatchEvent(new Event('scroll', { bubbles: true }))
    expect(closes).toEqual([1])
  })

  it('re-places rather than closes when a resize slides the row sideways', () => {
    sized()
    /* The dialog is min(1000px, 94vw), so a narrower window moves the row
       horizontally with its top unchanged. Closing on that would take the panel
       away for a gesture that did not touch the list. */
    let left = 700
    const row = anchored(200)
    row.getBoundingClientRect = () => ({ top: 200, bottom: 230, left, right: left + 200, width: 200, height: 30, x: left, y: 200, toJSON: () => ({}) }) as DOMRect
    const { closes } = draw({ anchor: row })
    const pop = screen.getByRole('dialog') as HTMLDivElement
    /* Clamped off the dialog's right edge rather than left at the row's own
       700: 700 plus the panel's 560 would end past the dialog's 1200. */
    expect(pop.style.left).toBe('632px')
    left = 420
    act(() => { window.dispatchEvent(new Event('resize')) })
    expect(closes, 'a resize moves the dialog around the row, it does not take the row away').toEqual([])
    expect(pop.style.left).toBe('420px')
  })

  it('does not read a height resize as a row that scrolled away', () => {
    sized()
    /* The dialog is min(680px, 88vh) and centred, so a shorter window moves the
       row vertically without anything scrolling. The panel follows; the guard's
       baseline has to follow with it, or the next scroll compares against where
       the row used to be. That scroll is the likely one: the model list in this
       panel is its own scroller and the listener is on the document in capture
       phase, so choosing a model reaches it. */
    let top = 200
    const row = anchored(top)
    row.getBoundingClientRect = () => ({ top, bottom: top + 30, left: 700, right: 900, width: 200, height: 30, x: 700, y: top, toJSON: () => ({}) }) as DOMRect
    const { closes } = draw({ anchor: row })
    top = 140
    act(() => { window.dispatchEvent(new Event('resize')) })
    expect(closes).toEqual([])
    document.dispatchEvent(new Event('scroll', { bubbles: true }))
    expect(closes, 'the row has not moved since the panel was re-placed').toEqual([])
    /* A real scroll after that still closes it. */
    top = 60
    document.dispatchEvent(new Event('scroll', { bubbles: true }))
    expect(closes).toEqual([1])
  })

  it('leaves an unanchored panel where the caller put it', () => {
    sized()
    draw()
    const pop = screen.getByRole('dialog') as HTMLDivElement
    expect(pop.style.position).toBe('')
  })
})
