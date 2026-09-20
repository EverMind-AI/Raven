// @vitest-environment happy-dom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
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

afterEach(() => {
  cleanup()
  resetTranslator()
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

  it('says why the list is empty when no provider can serve the role', () => {
    draw({ providers: [], current: null })
    expect(screen.getByText('none')).toBeTruthy()
  })
})
