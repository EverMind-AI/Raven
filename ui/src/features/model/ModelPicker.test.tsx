// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ModelPickerApp } from './ModelPicker'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { ModelSource, Provider } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const toastWriter = vi.hoisted(() => ({ items: [] as string[] }))
vi.mock('../../shell/toast', () => ({
  show: (text: string) => { toastWriter.items.push(text) },
}))

const PROVIDERS: Provider[] = [
  { id: 'minimax', name: 'MiniMax', models: ['minimax-m3', 'minimax-m2'], on: true },
  { id: 'anthropic', name: 'Anthropic', models: ['vendor/claude-opus-5', 'claude-sonnet-5'], on: true },
  { id: 'openai', name: 'OpenAI', models: ['gpt-5.2'], on: false },
  { id: 'empty', name: 'Nothing', models: [], on: true },
]

interface Harness {
  toasts: string[]
  persisted: string[]
  local: string[]
  settings: number
  after: number
  model: () => string
}

function install(over: Partial<ModelSource> = {}, providers = PROVIDERS): Harness {
  const h: Harness = { toasts: [], persisted: [], local: [], settings: 0, after: 0, model: store.current }
  toastWriter.items = h.toasts
  let last = store.current()
  store.subscribe(() => {
    const next = store.current()
    if (next !== last) { h.local.push(next); last = next }
  })
  const source: ModelSource = {
    providers: () => providers,
    persist: async (m) => {
      h.persisted.push(m)
    },
    openSettings: () => {
      h.settings += 1
    },
    ...over,
  }
  const shell: Shell = {
    T: (key) => key,
    confirmAsk: () => {},
    showPage: () => {},
  }
  window.RavenShell = shell
  window.DS = { model: source }
  document.body.innerHTML = '<button id="modelChip">chip</button>'
  return h
}

const mount = () => render(<ModelPickerApp />, { container: document.body.appendChild(document.createElement('div')) })

const pick = (): HTMLElement | null => document.querySelector('.mpick')
const rows = (col: string): HTMLElement[] => [...document.querySelectorAll<HTMLElement>(`.mpick .${col} .row`)]
const field = (): HTMLInputElement => document.querySelector('.mpick .find input')!

const openIt = (anchor?: HTMLElement | null, after?: () => void) =>
  act(() => {
    store.open(anchor, after)
  })

/* fireEvent.change, not a hand-built input event: setting .value directly
   bypasses React's value tracker, so the component never sees the keystroke and
   every search assertion below would pass against an unchanged list. */
const type = (text: string) =>
  act(() => {
    fireEvent.change(field(), { target: { value: text } })
  })

afterEach(() => {
  act(() => store._resetForTests())
  cleanup()
  delete window.RavenShell
  delete window.DS
  document.body.innerHTML = ''
})

describe('the model picker', () => {
  it('renders nothing until it is asked for', () => {
    install()
    mount()
    expect(pick()).toBeNull()
  })

  it('offers only providers with an account and something to offer', () => {
    install()
    mount()
    openIt()
    expect(rows('provs').map((b) => b.querySelector('.nm')!.textContent)).toEqual(['MiniMax', 'Anthropic'])
  })

  it('refuses to open with nothing authenticated, and says why', () => {
    const h = install({}, [{ id: 'x', name: 'X', models: ['m'], on: false }])
    mount()
    openIt()
    expect(pick()).toBeNull()
    expect(h.toasts).toEqual(['gui.picker.no_account'])
  })

  it('opens on the provider holding the current model, and marks it', () => {
    store.setCurrent('claude-sonnet-5')
    install()
    mount()
    openIt()
    expect(rows('provs')[1]!.getAttribute('aria-selected')).toBe('true')
    expect(rows('provs')[1]!.querySelector('.tick')!.textContent).toBe('•')
    const ticked = rows('models').find((b) => b.querySelector('.tick'))!
    expect(ticked.querySelector('.nm')!.textContent).toBe('claude-sonnet-5')
  })

  it('shows a provider-qualified name without its vendor half', () => {
    store.setCurrent('vendor/claude-opus-5')
    install()
    mount()
    openIt()
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual([
      'claude-opus-5',
      'claude-sonnet-5',
    ])
  })

  it('narrows each provider in place, keeping the column and counting hits', () => {
    install()
    mount()
    openIt()
    type('m2')
    const provs = rows('provs')
    expect(provs.map((b) => b.querySelector('.ct')!.textContent)).toEqual(['1', '0'])
    /* The provider with no hits dims rather than disappearing: what is installed
       must not move around while the reader types. */
    expect(provs[1]!.className).toContain('dim')
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual(['minimax-m2'])
  })

  it('moves the selection off a provider a search emptied', () => {
    install()
    mount()
    openIt()
    type('sonnet')
    expect(rows('provs')[1]!.getAttribute('aria-selected')).toBe('true')
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual(['claude-sonnet-5'])
  })

  it('keeps the moved selection after the term is cleared', () => {
    install()
    mount()
    openIt()
    type('sonnet')
    type('')
    /* The move is the reader's now, not the term's. Clearing the field is how
       you browse the rest of the provider a search just found for you, so the
       column has to stay where the search put it -- and show that provider's
       full list, not its one hit. */
    expect(rows('provs')[1]!.getAttribute('aria-selected')).toBe('true')
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual([
      'claude-opus-5',
      'claude-sonnet-5',
    ])
  })

  it('moves nothing when the term matches nothing at all', () => {
    /* Opened on the second provider on purpose. Starting on the first one makes
       this case unfalsifiable: there is no column below it to be wrongly moved
       to, so a version that moved the selection anywhere it liked would land
       back on it and the assertion would hold either way. */
    store.setCurrent('claude-sonnet-5')
    install()
    mount()
    openIt()
    type('nothing-like-this')
    type('')
    /* There is no better column to move to, so the selection must not wander --
       a typo on the way to a search must not relocate the reader. */
    expect(rows('provs')[1]!.getAttribute('aria-selected')).toBe('true')
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual([
      'claude-opus-5',
      'claude-sonnet-5',
    ])
  })

  it('says no match rather than showing an empty column', () => {
    install()
    mount()
    openIt()
    type('nothing-like-this')
    expect(document.querySelector('.mpick .models .empty')!.textContent).toBe('gui.picker.no_match')
  })

  it('distinguishes an empty search result from an empty provider', () => {
    install({}, [
      { id: 'a', name: 'A', models: ['one'], on: true },
      { id: 'b', name: 'B', models: [], on: true },
    ])
    mount()
    openIt()
    /* The second provider is filtered out of the columns entirely, so the only
       empty message reachable here is the search one. */
    expect(rows('provs').length).toBe(1)
  })
})

describe('the model picker, choosing', () => {
  it('closes, sets locally, persists, and says what happened', async () => {
    const h = install()
    mount()
    openIt()
    await act(async () => {
      rows('models')[1]!.click()
    })
    expect(pick()).toBeNull()
    expect(h.local).toEqual(['minimax-m2'])
    expect(store.current()).toBe('minimax-m2')
    expect(h.persisted).toEqual(['minimax-m2'])
    expect(h.toasts).toEqual(['已切换到 minimax-m2'])
  })

  it('rolls the pick back when the write is refused, and says that too', async () => {
    const h = install({ persist: async () => Promise.reject({ data: { detail: 'no such model' } }) })
    mount()
    openIt()
    await act(async () => {
      rows('models')[1]!.click()
    })
    /* Forward then back: the chip must never be left claiming a model the
       config did not take. */
    expect(h.local).toEqual(['minimax-m2', 'minimax-m3'])
    expect(h.model()).toBe('minimax-m3')
    expect(h.toasts).toEqual(['切换失败：no such model'])
  })

  it('tells the caller after every local change, forward and back', async () => {
    const h = install({ persist: async () => Promise.reject(new Error('boom')) })
    mount()
    const anchor = document.getElementById('modelChip')!
    openIt(anchor, () => {
      h.after += 1
    })
    await act(async () => {
      rows('models')[1]!.click()
    })
    expect(h.after).toBe(2)
    expect(h.toasts).toEqual(['切换失败：boom'])
  })

  it('takes the first hit on enter', async () => {
    const h = install()
    mount()
    openIt()
    type('sonnet')
    await act(async () => {
      field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    })
    expect(h.persisted).toEqual(['claude-sonnet-5'])
  })
})

describe('the model picker, closing', () => {
  it('closes on escape', () => {
    install()
    mount()
    openIt()
    act(() => {
      field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    })
    expect(pick()).toBeNull()
  })

  it('leaves escape alone while an input method is composing', () => {
    install()
    mount()
    openIt()
    act(() => {
      field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, keyCode: 229 }))
    })
    expect(pick()).toBeTruthy()
  })

  it('closes on a pointer down outside, but not on the button that opened it', () => {
    install()
    mount()
    const anchor = document.getElementById('modelChip')!
    openIt(anchor)
    act(() => {
      anchor.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    })
    expect(pick()).toBeTruthy()
    act(() => {
      document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    })
    expect(pick()).toBeNull()
  })

  it('offers the settings door only when opened from the chip', () => {
    const h = install()
    mount()
    openIt()
    const foot = document.querySelector('.mpick .foot button') as HTMLElement
    expect(foot.textContent).toBe('gui.picker.manage')
    act(() => foot.click())
    expect(pick()).toBeNull()
    expect(h.settings).toBe(1)
    /* Opened from a settings row, the page it would take you to is the page you
       are already on. */
    openIt(document.getElementById('modelChip'))
    expect(document.querySelector('.mpick .foot')).toBeNull()
  })

  it('does nothing at all on a page with no source installed', () => {
    install()
    delete window.DS
    mount()
    expect(() => openIt()).not.toThrow()
    expect(pick()).toBeNull()
  })
})
