// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import { resetSources, setSources } from '../../state/sources'
import * as tier from '../../state/tier'
import { domSnapshot } from '../../test/domSnapshot'
import { ModelApp } from './ModelPicker'
import * as store from './store';

import type { ModelSource, Provider } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const toastWriter = vi.hoisted(() => ({ items: [] as string[] }))
vi.mock('../../state/toast', () => ({
  show: (text: string) => { toastWriter.items.push(text) },
}))

const PROVIDERS: Provider[] = [
  { id: 'minimax', name: 'MiniMax (Global)', homepage: 'https://platform.minimax.io/', models: ['minimax-m3', 'minimax-m2'], on: true },
  /* Everything this one could serve is three models; two were added. The
     picker offers the two. */
  { id: 'anthropic', name: 'Anthropic', models: ['vendor/claude-opus-5', 'claude-sonnet-5'], on: true },
  { id: 'openai', name: 'OpenAI', models: ['gpt-5.2'], on: false },
  { id: 'empty', name: 'Nothing', models: [], on: true },
]

interface Harness {
  toasts: string[]
  persisted: string[]
  persistedProviders: string[]
  persistedScopes: string[]
  local: string[]
  settings: number
  providerSettings: string[]
  /* [model, provider, kind] per call: what the typed-id row asked the source to
     add, and what kind it stated for it. */
  added: Array<[string, string, string | undefined]>
  after: number
  model: () => string
}

function install(over: Partial<ModelSource> = {}, providers = PROVIDERS): Harness {
  const h: Harness = {
    toasts: [], persisted: [], persistedProviders: [], persistedScopes: [],
    local: [], settings: 0, providerSettings: [], added: [], after: 0, model: store.current,
  }
  toastWriter.items = h.toasts
  let last = store.current()
  store.subscribe(() => {
    const next = store.current()
    if (next !== last) { h.local.push(next); last = next }
  })
  const source: ModelSource = {
    providers: () => providers,
    persist: async (m, provider, scope) => {
      h.persisted.push(m)
      h.persistedProviders.push(provider)
      h.persistedScopes.push(scope)
    },
    addModel: async (m, provider, kind) => {
      h.added.push([m, provider, kind])
    },
    openSettings: () => {
      h.settings += 1
    },
    openProviderModels: (provider) => {
      h.providerSettings.push(provider)
    },
    ...over,
  }
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
  setSources({ model: source })
  document.body.innerHTML = '<button id="modelChip">chip</button>'
  return h
}

const mount = () => render(<ModelApp />, { container: document.body.appendChild(document.createElement('div')) })

const pick = (): HTMLElement | null => document.querySelector('.mpick')
/* The rows of a column, which since 2026-09-20 means the models in it: the
   typed-id row lives in the same list and is reached by `typedRow` instead, so
   every case that counts models keeps counting models. */
const rows = (col: string): HTMLElement[] =>
  [...document.querySelectorAll<HTMLElement>(`.mpick .${col} .model-group:not(.model-recent) .row:not(.model-typed):not(.model-more)`)]
const recents = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.mpick .model-recent .row')]
const more = (): HTMLElement | null => document.querySelector<HTMLElement>('.mpick .model-more')
const typedRow = (): HTMLElement | null => document.querySelector<HTMLElement>('.mpick .model-typed')
const field = (): HTMLInputElement => document.querySelector('.mpick .find input')!
/* One group per provider, each headed by the provider's name and count. */
const groups = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.mpick .model-group:not(.model-recent)')]
const heads = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.mpick .model-group:not(.model-recent) .model-group-hd')]
const headNames = (): string[] => heads().map((h) => h.querySelector('.nm')!.textContent!)
const headCounts = (): string[] => heads().map((h) => h.querySelector('.ct')!.textContent!)

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
  resetTranslator()
  resetSources()
  document.body.innerHTML = ''
  localStorage.clear()
})
describe('the model picker', () => {
  it('renders nothing until it is asked for', () => {
    install()
    mount()
    expect(pick()).toBeNull()
  })

  it('offers every provider with an account, including one with nothing added', () => {
    /* Reversed 2026-09-20: a provider with a working key and an empty list used
       to be dropped here, which told a reader it was not connected and left a
       model set by onboarding with no group to be marked in. */
    install()
    mount()
    openIt()
    expect(headNames()).toEqual(['MiniMax (Global)', 'Anthropic', 'Nothing'])
    expect(headCounts()).toEqual(['2', '2', '0'])
    /* The account's mark, its name and a count: no link out to a marketing
       page, no connection dot -- every account listed here is connected. */
    expect(heads()[0]!.firstElementChild?.getAttribute('src')).toBe('assets/providers/minimax.svg')
    expect(heads()[0]!.querySelector('a, .provider-status')).toBeNull()
  })

  it('refuses to open with nothing authenticated, and says why', () => {
    const h = install({}, [{ id: 'x', name: 'X', models: ['m'], on: false }])
    mount()
    openIt()
    expect(pick()).toBeNull()
    expect(h.toasts).toEqual(['gui.picker.no_account'])
  })

  it('lists every provider\'s models under its own head, and marks the current one', () => {
    store.setCurrent('claude-sonnet-5')
    install()
    mount()
    openIt()
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual([
      'minimax-m3', 'minimax-m2', 'claude-opus-5', 'claude-sonnet-5',
    ])
    const ticked = rows('models').filter((b) => b.querySelector('.tick'))
    expect(ticked.map((b) => b.querySelector('.nm')!.textContent)).toEqual(['claude-sonnet-5'])
    expect(ticked[0]!.closest('.model-group')!.querySelector('.model-group-hd .nm')!.textContent).toBe('Anthropic')
  })

  it('shows a provider-qualified name without its vendor half', () => {
    store.setCurrent('vendor/claude-opus-5')
    install()
    mount()
    openIt()
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toContain('claude-opus-5')
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).not.toContain('vendor/claude-opus-5')
  })

  it('narrows to the groups with a hit, counting the hits', () => {
    install()
    mount()
    openIt()
    type('m2')
    /* A group the search left nothing in is dropped: the term is the way to a
       model, and a head with nothing under it is not on the way. */
    expect(headNames()).toEqual(['MiniMax (Global)'])
    expect(headCounts()).toEqual(['1'])
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual(['minimax-m2'])
    /* Clearing the term shows the whole list again, groups where they were. */
    type('')
    expect(headCounts()).toEqual(['2', '2', '0'])
    expect(rows('models')).toHaveLength(4)
  })

  it('matches the label as well as the id', () => {
    install({}, TAGGED)
    mount()
    openIt()
    type('opus 5')
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual(['Claude Opus 5'])
  })

  it('says no match when nothing anywhere matches, and still offers the typed id', () => {
    install()
    mount()
    openIt()
    type('nothing-like-this')
    expect(document.querySelector('.mpick .models > .empty')!.textContent).toBe('gui.picker.no_match')
    expect(typedRow()).not.toBeNull()
  })

  it('distinguishes an empty search result from a group with nothing of the kind', () => {
    install({}, [
      { id: 'a', name: 'A', models: ['one'], on: true },
      { id: 'b', name: 'B', models: [], on: true },
    ])
    mount()
    openIt()
    expect(groups().length).toBe(2)
    /* Two different empties, and the words are the whole difference: one is a
       list to go and build, said in its group, the other a term to delete. */
    expect(groups()[1]!.querySelector('.empty')!.textContent).toBe('gui.picker.empty_kind {"kind":"gui.model.type.text"}')
    type('nothing-like-this')
    expect(document.querySelector('.mpick .models > .empty')!.textContent).toBe('gui.picker.no_match')
    expect(groups()).toHaveLength(0)
  })

  it('offers a typed id beside the matches, and adds it to the provider before picking it', async () => {
    const h = install()
    mount()
    openIt()
    type('m2')
    /* The term matches a model AND is not one: both rows are right, because
       the reader may mean either. */
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual(['minimax-m2'])
    expect(typedRow()!.querySelector('.nm')!.textContent).toBe('gui.model.pick_use {"id":"m2"}')
    /* Added to the account the reader is already on, and the row says so. */
    expect(typedRow()!.querySelector('.ct')!.textContent).toBe('gui.model.pick_add_to {"name":"MiniMax (Global)"}')
    await act(async () => { fireEvent.click(typedRow()!) })
    expect(h.added).toEqual([['m2', 'minimax', 'text']])
    expect(h.persisted).toEqual(['m2'])
  })

  it('adds a typed id to the account on, when it spells the current model the other way', async () => {
    /* "The account the reader is already on" is found by looking the current
       model up in each column. By string it finds nobody, and the id silently
       joins whichever account happens to be listed first. */
    const h = install({}, [
      { id: 'minimax', name: 'MiniMax', on: true, models: ['minimax-m3'], configured: ['minimax-m3'] },
      { id: 'openrouter', name: 'OpenRouter', on: true, models: ['openrouter/my-model'], configured: ['openrouter/my-model'] },
    ])
    store.setCurrent('my-model')
    mount()
    openIt()
    type('brand-new')
    expect(typedRow()!.querySelector('.ct')!.textContent).toBe('gui.model.pick_add_to {"name":"OpenRouter"}')
    await act(async () => { fireEvent.click(typedRow()!) })
    expect(h.added).toEqual([['brand-new', 'openrouter', 'text']])
  })

  it('leaves the typed row out once the term is a model exactly', () => {
    install()
    mount()
    openIt()
    type('minimax-m2')
    expect(typedRow()).toBeNull()
  })

  it('adds a typed id as the kind its name says', async () => {
    const h = install()
    mount()
    openIt()
    type('my-team/bge-reranker-x')
    /* The name's own guess, the way registry_data.inferred_tags would read it. */
    await act(async () => { fireEvent.click(typedRow()!) })
    expect(h.added).toEqual([['my-team/bge-reranker-x', 'minimax', 'reranker']])
  })

  it('a slot opening adds a typed id as the slot kind, not what the name says', async () => {
    const picked: Array<[string, string, boolean, string]> = []
    install({}, [{ id: 'p', name: 'P', on: true, models: ['emb-1'], labels: { 'emb-1': { kind: 'embedding' } } }])
    mount()
    act(() => {
      store.open(document.getElementById('modelChip'), undefined, undefined,
        { kind: 'embedding', title: 'Embedding', pick: async (m, p, typed, kind) => { picked.push([m, p, typed, kind]) } })
    })
    type('plain-name')
    await act(async () => { fireEvent.click(typedRow()!) })
    expect(picked).toEqual([['plain-name', 'p', true, 'embedding']])
  })

  it('keeps its rendered shape', () => {
    install()
    const view = mount()
    openIt()
    expect(domSnapshot(view.container)).toMatchSnapshot()
  })
})

describe('the model picker, where it lands', () => {
  /* happy-dom measures every box as zero, so the three that decide the
     placement are given the rects they have on the running page. Stubbed on the
     prototype because the popover is created during the render that then
     measures it -- there is no moment in between to reach the node. */
  function measure(sizes: Map<string, [number, number]>): void {
    const real = Element.prototype.getBoundingClientRect
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      for (const [selector, [top, height]] of sizes) {
        if (this.matches(selector)) {
          return { top, bottom: top + height, left: 40, right: 40, width: 0, height, x: 40, y: top } as DOMRect
        }
      }
      return real.call(this)
    })
  }

  /* The composer, as page.html nests it: the chip is on the card's bottom bar,
     which is the whole reason the card and not the chip is what has to be
     cleared. */
  function onTheCard(): HTMLElement {
    document.body.innerHTML = `
      <div class="dock-in">
        <div class="field"><textarea></textarea></div>
        <div class="under"><button id="modelChip">chip</button></div>
      </div>`
    return document.getElementById('modelChip')!
  }

  it('hangs off the chip by its lower edge, 6px above the chip', () => {
    install()
    const chip = onTheCard()
    Object.defineProperty(document.documentElement, 'clientHeight', { value: 600, configurable: true })
    measure(new Map([['.mpick', [0, 300]], ['.dock-in', [436, 126]], ['#modelChip', [518, 21]]]))
    mount()
    /* The composer's opening names no anchor and measures the chip by id. */
    openIt()
    /* Pinned by the bottom, 600 - 518 + 6, so a search that shortens the list
       shrinks the panel upward and leaves no gap over the chip; the top is the
       panel's own to find. */
    expect(pick()!.style.bottom).toBe('88px')
    expect(pick()!.style.top).toBe('auto')
    expect(chip.id).toBe('modelChip')
  })

  it('drops below the chip when there is no room above it', () => {
    install()
    onTheCard()
    Object.defineProperty(document.documentElement, 'clientHeight', { value: 900, configurable: true })
    measure(new Map([['.mpick', [0, 300]], ['.dock-in', [60, 126]], ['#modelChip', [70, 21]]]))
    mount()
    openIt()
    /* 70 + 21 + 6, under the chip, and by the top this time. */
    expect(pick()!.style.top).toBe('97px')
    expect(pick()!.style.bottom).toBe('auto')
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
    expect(h.persistedProviders).toEqual(['minimax'])
    expect(h.persistedScopes).toEqual(['session'])
    expect(h.toasts).toEqual(['gui.model.pick_switched {"name":"minimax-m2"}'])
  })

  it('a switch from the settings default control is scoped to the default, not the session', async () => {
    const h = install()
    mount()
    /* An anchor is how the settings default-model control opens the picker; the
       composer chip opens with none. The scope rides that difference so the
       default control changes agents.defaults even while a conversation is open. */
    openIt(document.getElementById('modelChip')!)
    await act(async () => {
      rows('models')[1]!.click()
    })
    expect(h.persisted).toEqual(['minimax-m2'])
    expect(h.persistedProviders).toEqual(['minimax'])
    expect(h.persistedScopes).toEqual(['default'])
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
    expect(h.toasts).toEqual(['gui.op.switch_failed {"detail":"no such model"}'])
  })

  it('tells the caller after every local change, forward and back', async () => {
    // A session switch (the composer chip, no anchor) is the optimistic one:
    // the caller is told on the forward change and again on the rollback.
    const h = install({ persist: async () => Promise.reject(new Error('boom')) })
    mount()
    openIt(null, () => {
      h.after += 1
    })
    await act(async () => {
      rows('models')[1]!.click()
    })
    expect(h.after).toBe(2)
    expect(h.toasts).toEqual(['gui.op.switch_failed {"detail":"boom"}'])
  })

  it('says a staged pick is staged, not switched', async () => {
    // A draft has no session yet; the source stages the pick and says so, and
    // the toast must not claim an applied switch that a later write can refuse.
    const h = install({ persist: async () => 'staged' as const })
    mount()
    openIt()
    await act(async () => {
      rows('models')[1]!.click()
    })
    expect(store.current()).toBe('minimax-m2')
    expect(h.toasts).toEqual(['gui.model.pick_staged {"name":"minimax-m2"}'])
  })

  it('a refused default switch commits nothing locally and does not roll back', async () => {
    // The default control (an anchor) is not optimistic: it reflects the new
    // default only once the write lands, so a refusal leaves the settings row
    // untouched and the caller is not pinged with a forward/back pair.
    const h = install({ persist: async () => Promise.reject(new Error('boom')) })
    mount()
    openIt(document.getElementById('modelChip')!, () => {
      h.after += 1
    })
    await act(async () => {
      rows('models')[1]!.click()
    })
    expect(h.after).toBe(0)
    expect(h.toasts).toEqual(['gui.op.switch_failed {"detail":"boom"}'])
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
    expect(h.persistedProviders).toEqual(['anthropic'])
    expect(h.persistedScopes).toEqual(['session'])
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
    resetSources()
    mount()
    expect(() => openIt()).not.toThrow()
    expect(pick()).toBeNull()
  })
})

/* This block's own providers. The shared list above is what every assertion
   before it measures, and hanging a label on one of those rows renames the text
   half those tests read. */
const TAGGED: Provider[] = [
  {
    id: 'anthropic',
    name: 'Anthropic',
    models: ['vendor/claude-opus-5', 'claude-sonnet-5'],
    on: true,
    labels: {
      'vendor/claude-opus-5': {
        label: 'Claude Opus 5',
        capabilities: ['reasoning', 'function-call', 'image-recognition'],
        input_modalities: ['text', 'image'],
        output_modalities: ['text'],
        context_window: 1000000,
      },
    },
  },
]

/* The sub-agent tier rides the picker as one row, for the composer's opening
   only: a settings row edits the default model, which has no conversation to
   carry a tier (state/tier.ts). */
describe('the tier row', () => {
  const MENU = [
    { id: 'medium', name: 'Medium', description: 'Faster and cheaper, for small, well-defined tasks.' },
    { id: 'high', name: 'High', description: 'A balance of speed and quality.' },
    { id: 'max', name: 'Max', description: 'Deepest reasoning and full sub-agent effort, for complex or open-ended work.' },
  ]
  const seg = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.mpick .model-seg button')]

  afterEach(() => { tier._resetForTests() })

  it('offers every rung as one segmented control, the one in force pressed, and asks for the one clicked', async () => {
    install()
    const asked: Array<string | null> = []
    setSources({
      model: (await import('./store')).source(),
      tier: {
        read: async () => { asked.push(null); return { mode: 'high', availableModes: MENU } },
        set: async (mode) => { asked.push(mode); return { mode, availableModes: MENU } },
      },
    })
    await act(async () => { await tier.load() })
    mount()
    openIt()
    expect(seg().map((b) => b.textContent)).toEqual(['gui.tier.medium', 'gui.tier.high', 'gui.tier.max'])
    expect(seg().map((b) => b.getAttribute('aria-checked'))).toEqual(['false', 'true', 'false'])
    /* One explanation, behind the question mark, through the page's tooltip;
       the rungs and the row itself carry none. */
    expect(seg().map((b) => b.title)).toEqual(['', '', ''])
    expect(document.querySelector('.mpick .model-tier')!.getAttribute('title')).toBeNull()
    const help = document.querySelector('.mpick .model-tier-help')!
    expect(help.getAttribute('data-tip')).toBe('gui.tier.scope')
    /* A sentence, so the pill wraps it rather than running one line across the window. */
    expect(help.hasAttribute('data-tip-wrap')).toBe(true)
    await act(async () => { fireEvent.click(seg()[2]!) })
    expect(asked).toEqual([null, 'max'])
    expect(seg().map((b) => b.getAttribute('aria-checked'))).toEqual(['false', 'false', 'true'])
    /* The picker stays up: a tier is not a pick of a model. */
    expect(pick()).not.toBeNull()
  })

  it('draws no row before the catalogue answers, and none for a settings slot', async () => {
    install()
    mount()
    openIt()
    expect(document.querySelector('.mpick .model-tier')).toBeNull()
    act(() => store.close())
    setSources({
      model: (await import('./store')).source(),
      tier: {
        read: async () => ({ mode: 'high', availableModes: MENU }),
        set: async (mode) => ({ mode, availableModes: MENU }),
      },
    })
    await act(async () => { await tier.load() })
    act(() => {
      store.open(document.getElementById('modelChip'), undefined, undefined,
        { kind: 'text', title: 'Chat', pick: async () => {} })
    })
    expect(document.querySelector('.mpick .model-tier')).toBeNull()
  })
})

describe('the names', () => {
  it('shows the label where there is one and keeps the id on the title', () => {
    install({}, TAGGED)
    mount()
    openIt()
    const [named, bare] = rows('models')
    expect(named!.querySelector('.nm')!.textContent).toBe('Claude Opus 5')
    expect(named!.getAttribute('title')).toBe('claude-opus-5')
    expect(bare!.querySelector('.nm')!.textContent).toBe('claude-sonnet-5')
  })

  it('draws the capabilities and the window beside the name', () => {
    install({}, TAGGED)
    mount()
    openIt()
    /* The registry publishes three capabilities and a 1M window for the first
       row, and the row is where a reader compares them before picking. The
       row a catalogue says nothing about carries no badge at all. */
    const [tagged, bare] = rows('models')
    expect(tagged!.querySelectorAll('.model-tag')).toHaveLength(3)
    expect(tagged!.querySelector('.model-window')!.textContent).toBe('1M')
    expect(bare!.querySelector('.model-tags')).toBeNull()
  })

  it('carries the sprite the icons resolve against', () => {
    install({}, TAGGED)
    mount()
    openIt()
    /* Every icon above is a `use` of a symbol defined once per surface. The
       picker drew them against a sprite nothing rendered after the composer
       rework, which is a row of blank 13px boxes. */
    expect(document.querySelector('.mpick .model-tag-defs #mtag-reasoning')).not.toBeNull()
  })

  it('keeps its rendered shape', () => {
    install({}, TAGGED)
    const view = mount()
    openIt()
    expect(domSnapshot(view.container)).toMatchSnapshot()
  })
})

/* A gateway account brings a few hundred models under one head. The list
   folds each long group to FOLD rows plus a row saying how many more, and the
   search is the way to anything the fold hides. */
describe('the fold', () => {
  const MANY = Array.from({ length: 20 }, (_, i) => `router/model-${String(i).padStart(2, '0')}`)
  const ROUTER: Provider[] = [
    { id: 'router', name: 'Router', on: true, models: MANY, configured: MANY },
    { id: 'small', name: 'Small', on: true, models: ['tiny'], configured: ['tiny'] },
  ]

  it('shows six of a long group and a row for the rest, and unfolds on a click', () => {
    install({}, ROUTER)
    mount()
    openIt()
    expect(headCounts()).toEqual(['20', '1'])
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual([
      'model-00', 'model-01', 'model-02', 'model-03', 'model-04', 'model-05', 'tiny',
    ])
    expect(more()!.textContent).toBe('gui.picker.show_all {"n":"20"}')
    act(() => { fireEvent.click(more()!) })
    expect(rows('models')).toHaveLength(21)
    expect(more()).toBeNull()
  })

  it('keeps the current model visible under the fold', () => {
    store.setCurrent('router/model-17')
    install({}, ROUTER)
    mount()
    openIt()
    const names = rows('models').map((b) => b.querySelector('.nm')!.textContent)
    expect(names.slice(0, 7)).toEqual(['model-00', 'model-01', 'model-02', 'model-03', 'model-04', 'model-05', 'model-17'])
    expect(rows('models')[6]!.querySelector('.tick')).not.toBeNull()
    /* Six shown plus the current one: thirteen more behind the row. */
    expect(more()!.textContent).toBe('gui.picker.show_all {"n":"20"}')
  })

  it('keeps it visible when the conversation spells it without the vendor half', () => {
    /* The same mixed spellings the column de-dups by `sameModel`. Pinning by
       string leaves the running model behind the fold, where the reader has no
       way to know it is the one in force. */
    store.setCurrent('model-17')
    install({}, ROUTER)
    mount()
    openIt()
    const names = rows('models').map((b) => b.querySelector('.nm')!.textContent)
    expect(names.slice(0, 7)).toEqual(['model-00', 'model-01', 'model-02', 'model-03', 'model-04', 'model-05', 'model-17'])
    expect(rows('models')[6]!.querySelector('.tick')).not.toBeNull()
  })

  it('does not fold while searching: the term is the way to a hidden model', () => {
    install({}, ROUTER)
    mount()
    openIt()
    type('model-1')
    expect(rows('models')).toHaveLength(10)
    expect(more()).toBeNull()
  })
})

/* The last picks made from the composer head the list: the way back to the
   three or four a reader actually moves between (./recent.ts). */
describe('the recent picks', () => {
  it('heads the list with the last picks, latest first and each once, each naming its account', async () => {
    const h = install()
    mount()
    openIt()
    expect(recents()).toHaveLength(0)
    await act(async () => { rows('models')[1]!.click() })
    openIt()
    await act(async () => { rows('models')[3]!.click() })
    openIt()
    await act(async () => { rows('models')[1]!.click() })
    expect(h.persisted).toEqual(['minimax-m2', 'claude-sonnet-5', 'minimax-m2'])
    openIt()
    expect(recents().map((b) => b.querySelector('.nm')!.textContent)).toEqual(['minimax-m2', 'claude-sonnet-5'])
    /* The account's name on the row, not its mark: the marks live on the group
       heads, once per account, and a row with one where its neighbours have
       none read as a different kind of row. */
    expect(recents().map((b) => b.querySelector('.ct')!.textContent)).toEqual(['MiniMax (Global)', 'Anthropic'])
    expect(recents()[0]!.querySelector('.provider-icon')).toBeNull()
    expect(document.querySelector('.mpick .model-recent .model-group-hd .nm')!.textContent).toBe('gui.picker.recent')
    /* The recent row is a pick like any other. */
    await act(async () => { recents()[1]!.click() })
    expect(h.persisted.at(-1)).toBe('claude-sonnet-5')
    expect(h.persistedProviders.at(-1)).toBe('anthropic')
  })

  it('shows a recent pick only while its account still lists it, and not while searching', async () => {
    install()
    mount()
    openIt()
    await act(async () => { rows('models')[1]!.click() })
    openIt()
    expect(recents()).toHaveLength(1)
    type('m')
    expect(recents()).toHaveLength(0)
    act(() => store.close())
    install({}, [{ id: 'anthropic', name: 'Anthropic', models: ['claude-sonnet-5'], on: true }])
    openIt()
    expect(recents()).toHaveLength(0)
  })

  it('remembers picks from the composer only, not from a settings slot', async () => {
    install()
    mount()
    openIt(document.getElementById('modelChip')!)
    await act(async () => { rows('models')[1]!.click() })
    openIt()
    expect(recents()).toHaveLength(0)
  })
})

describe('what the picker offers', () => {
  it('offers the models that were added, not everything the vendor publishes', () => {
    /* A provider's `models` is the offer chain -- its own list plus a curated
       shortlist plus a catalogue -- which is what onboarding needs before
       anything has been added. Choosing a default is the other case: you pick
       from the list somebody built in settings. */
    install({}, [
      { id: 'anthropic', name: 'Anthropic', on: true, models: ['opus', 'sonnet', 'haiku'], configured: ['opus'] },
    ])
    mount()
    openIt()
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual(['opus'])
  })

  it('lists a provider that has an account but nothing added yet', () => {
    /* Counted 1 and 1: the first has nothing added, so a text opening offers
       the registry's shortlist (see the wizard case above); the second offers
       what was added. Zero is for a provider with neither. */
    install({}, [
      { id: 'anthropic', name: 'Anthropic', on: true, models: ['opus'], configured: [] },
      { id: 'openai', name: 'OpenAI', on: true, models: ['gpt'], configured: ['gpt'] },
      { id: 'empty', name: 'Nothing', on: true, models: [], configured: [] },
    ])
    mount()
    openIt()
    expect(headNames()).toEqual(['Anthropic', 'OpenAI', 'Nothing'])
    expect(headCounts()).toEqual(['1', '1', '0'])
  })

  it('lists the current model in its provider column even when the list does not carry it', () => {
    /* onboarding and the CLI set agents.defaults.model without adding it to
       the provider, so the model the chip names has to be somewhere it can be
       seen marked. */
    install({}, [{ id: 'p1', name: 'P1', on: true, models: ['a'], configured: ['a'], current: true }])
    store.setCurrent('b')
    mount()
    openIt()
    expect(rows('models').map((x) => x.querySelector('.nm')!.textContent)).toEqual(['b', 'a'])
    expect(rows('models')[0]!.querySelector('.tick')).not.toBeNull()
  })

  it('stops pinning the current model under the account the session started on once another lists it', () => {
    /* The wire's is_current flag names the provider the session STARTED on and
       goes stale on a switch: the model picked from MiniMax was drawn again,
       ticked, under Anthropic. Pinning only when no connected account lists
       the model is what keeps one model in one place. */
    install({}, [
      { id: 'anthropic', name: 'Anthropic', on: true, models: ['claude'], configured: ['claude'], current: true },
      { id: 'minimax', name: 'MiniMax', on: true, models: ['minimax-m2'], configured: ['minimax-m2'] },
    ])
    store.setCurrent('minimax-m2')
    mount()
    openIt()
    expect(rows('models').map((x) => x.querySelector('.nm')!.textContent)).toEqual(['claude', 'minimax-m2'])
    expect(rows('models').filter((x) => x.querySelector('.tick'))).toHaveLength(1)
  })

  it('does not list the current model twice when its two spellings differ', () => {
    /* A role stores the spelling it was handed; `model.add_model` stores the
       one it derived. Comparing the strings drew one model as two rows, both
       reading the same because the provider half is not shown. */
    install({}, [{
      id: 'openrouter', name: 'OpenRouter', on: true,
      models: ['openrouter/my-embedder'], configured: ['openrouter/my-embedder'],
      labels: { 'openrouter/my-embedder': { kind: 'embedding' } },
    }])
    mount()
    act(() => {
      store.open(document.getElementById('modelChip'), undefined, undefined, {
        kind: 'embedding', title: 'Embedding', pick: async () => {},
        current: { model: 'my-embedder', provider: 'openrouter' },
      })
    })
    expect(rows('models').map((x) => x.querySelector('.nm')!.textContent)).toEqual(['my-embedder'])
  })

  it('pins the running model under the account it is on, even where another lists it too', () => {
    /* Onboarding and the CLI can set a model without adding it to the account's
       list, so the pin is what puts it somewhere it can be seen marked. The
       rule that drops the pin once another account lists the model exists
       because the wire's flag went stale on a switch; where the page has been
       told which account outright, that guess is not needed and marking the
       account that merely lists the model names the wrong one. */
    install({}, [
      { id: 'anthropic', name: 'Anthropic', on: true, models: ['claude-opus-5'], configured: ['claude-opus-5'], current: true },
      { id: 'openrouter', name: 'OpenRouter', on: true, models: ['fable-5'], configured: ['fable-5'] },
    ])
    store.setCurrent('fable-5', 'anthropic')
    mount()
    openIt()
    const ticked = rows('models').filter((x) => x.querySelector('.tick'))
    expect(ticked).toHaveLength(1)
    expect(ticked[0]!.closest('.model-group')!.querySelector('.model-group-hd .nm')!.textContent).toBe('Anthropic')
  })

  it('marks the account the pick was made from, not another that lists the same id', async () => {
    /* A gateway and a direct vendor can both list one id. The pick names an
       account; what the page kept was the id alone, so reopening marked both
       rows and the reader could not tell which account the conversation is on. */
    install({}, [
      { id: 'anthropic', name: 'Anthropic', on: true, models: ['claude-opus-5'], configured: ['claude-opus-5'] },
      { id: 'openrouter', name: 'OpenRouter', on: true, models: ['claude-opus-5'], configured: ['claude-opus-5'] },
    ])
    mount()
    openIt()
    await act(async () => { fireEvent.click(rows('models')[1]!) })
    openIt()
    const ticked = rows('models').filter((x) => x.querySelector('.tick'))
    expect(ticked).toHaveLength(1)
    expect(ticked[0]!.closest('.model-group')!.querySelector('.model-group-hd .nm')!.textContent).toBe('OpenRouter')
  })

  it('marks it when the account spells it with a name it used to answer to', () => {
    /* The same mixed spellings, one rename apart: `merge_key` strips any prefix
       the provider answers to, and the row carries that set so this page can
       ask the identity question the backend answers. */
    install({}, [{
      id: 'zai', name: 'Z.ai', on: true, routes: ['zai', 'zhipu'],
      models: ['zhipu/glm-4.6'], configured: ['zhipu/glm-4.6'],
    }])
    store.setCurrent('zai/glm-4.6')
    mount()
    openIt()
    expect(rows('models')).toHaveLength(1)
    expect(rows('models')[0]!.querySelector('.tick')).not.toBeNull()
  })

  it('marks the current model when the account spells it the other way', () => {
    /* The de-dup above is by `sameModel`, so the one row that survives carries
       the provider's spelling while the conversation carries the bare one. A
       tick compared by string then marks nothing, and the reader is left with
       no sign of which model is running. */
    install({}, [{
      id: 'openrouter', name: 'OpenRouter', on: true,
      models: ['openrouter/my-model'], configured: ['openrouter/my-model'],
    }])
    store.setCurrent('my-model')
    mount()
    openIt()
    expect(rows('models').map((x) => x.querySelector('.nm')!.textContent)).toEqual(['my-model'])
    expect(rows('models')[0]!.querySelector('.tick')).not.toBeNull()
  })

  it('offers the registry shortlist for a text opening on a provider with nothing added', () => {
    /* The first-run wizard's whole model step: a vendor is connected and a
       chat model picked before anyone has built a list. An empty column there
       is the step. */
    install({}, [{ id: 'anthropic', name: 'Anthropic', on: true, models: ['opus', 'sonnet'], configured: [] }])
    mount()
    openIt()
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual(['opus', 'sonnet'])
  })

  it('does not fall back for a kind the wizard never asks for', () => {
    /* An embedding slot on a provider with nothing added should say so, not
       offer the vendor's whole catalogue filtered to whatever happens to be
       tagged -- "nothing here yet, type an id" is the honest answer. */
    install({}, [{
      id: 'anthropic', name: 'Anthropic', on: true, configured: [],
      models: ['emb-1'], labels: { 'emb-1': { kind: 'embedding' } },
    }])
    mount()
    act(() => {
      store.open(document.getElementById('modelChip'), undefined, undefined,
        { kind: 'embedding', title: 'Embedding', pick: async () => {} })
    })
    expect(rows('models')).toHaveLength(0)
    expect(document.querySelector('.mpick .models .empty')!.textContent).toBe(
      'gui.picker.empty_kind {"kind":"gui.model.type.embedding"}',
    )
  })

  it('falls back to the offer for a source that never learned the difference', () => {
    /* The demo layer and any older source hand back only `models`; emptying
       their picker would be a worse answer than offering what they have. */
    install({}, [{ id: 'anthropic', name: 'Anthropic', on: true, models: ['opus'] }])
    mount()
    openIt()
    expect(rows('models').map((b) => b.querySelector('.nm')!.textContent)).toEqual(['opus'])
  })
})

describe('the picker with nothing to offer', () => {
  it('opens for a connected account with no models added, rather than refusing', () => {
    /* Reversed 2026-09-20 with the rule above: "go and build a list" is now
       answered inside the picker -- an empty column that says so, and a row to
       type an id into -- so it is no longer a dead end. */
    const h = install({}, [{ id: 'anthropic', name: 'Anthropic', on: true, models: [], configured: [] }])
    mount()
    openIt()
    expect(pick()).not.toBeNull()
    expect(h.providerSettings).toEqual([])
    expect(h.toasts).toEqual([])
    expect(document.querySelector('.mpick .models .empty')!.textContent).toBe(
      'gui.picker.empty_kind {"kind":"gui.model.type.text"}',
    )
  })

  it('says "no models for" when the one vendor a slot allows is not connected', () => {
    /* The remaining shape of that message: a media role may only run on
       OpenRouter, so "no account" would send the reader to add any key at all. */
    const h = install({}, [{ id: 'openrouter', name: 'OpenRouter', on: false, models: ['x'], configured: ['x'] }])
    mount()
    act(() => { store.open(undefined, undefined, undefined, { kind: 'image', providers: ['openrouter'] }) })
    expect(pick()).toBeNull()
    expect(h.providerSettings).toEqual(['openrouter'])
    expect(h.toasts).toEqual(['gui.picker.no_models_for {"name":"OpenRouter"}'])
  })

  it('still says "no account" when nothing is connected at all', () => {
    const h = install({}, [{ id: 'anthropic', name: 'Anthropic', on: false, models: ['opus'], configured: ['opus'] }])
    mount()
    openIt()
    expect(pick()).toBeNull()
    expect(h.toasts).toEqual(['gui.picker.no_account'])
  })
})

/* What a reader who cannot see the list is told. The tick is a glyph and the
   grouping is a heading, so both are shape alone until the markup states them. */
describe('the model picker, to a screen reader', () => {
  it('names the popover after what is being chosen', () => {
    install()
    mount()
    openIt()
    expect(pick()!.getAttribute('aria-label')).toBe('gui.picker.title')
  })

  it('names it after the slot instead, when a slot opened it', () => {
    install()
    mount()
    act(() => { store.open(undefined, undefined, undefined, { kind: 'text', title: 'Planner model' }) })
    expect(pick()!.getAttribute('aria-label')).toBe('Planner model')
  })

  it('states the current model as the checked one of a set', () => {
    store.setCurrent('claude-sonnet-5')
    install()
    mount()
    openIt()
    expect(rows('models').map((b) => b.getAttribute('role'))).toEqual(['radio', 'radio', 'radio', 'radio'])
    expect(rows('models').map((b) => b.getAttribute('aria-checked'))).toEqual(['false', 'false', 'false', 'true'])
  })

  it('gives each account its own set, named after the account', () => {
    install()
    mount()
    openIt()
    /* Per account rather than one set for the picker: the same model can be
       listed under Recent and under its account, and one set may hold one
       checked member. An account with nothing to list owns no set. */
    expect(groups().map((g) => g.getAttribute('role'))).toEqual(['radiogroup', 'radiogroup', null])
    expect(groups().map((g) => g.getAttribute('aria-label'))).toEqual(['MiniMax (Global)', 'Anthropic', null])
  })

  it('leaves the tick out of the row\'s spoken name, now that the state is said', () => {
    /* Chrome builds a radio's name from its contents, so a glyph left visible
       to it is read out beside the checked state it duplicates. */
    store.setCurrent('claude-sonnet-5')
    install()
    mount()
    openIt()
    const ticked = rows('models').find((b) => b.querySelector('.tick'))!
    expect(ticked.querySelector('.tick')!.getAttribute('aria-hidden')).toBe('true')
  })

  it('leaves the show-all row out of the set it sits in', () => {
    /* It reveals more of the account's radios rather than being one: a row that
       answers to the set's state would be offered as a model to pick. */
    const many = Array.from({ length: 8 }, (_, i) => `router/model-${String(i)}`)
    install({}, [{ id: 'router', name: 'Router', on: true, models: many, configured: many }])
    mount()
    openIt()
    expect(more()).not.toBeNull()
    expect(more()!.getAttribute('role')).toBeNull()
    expect(more()!.getAttribute('aria-checked')).toBeNull()
  })

  it('gives the recent group a set of its own', async () => {
    install()
    mount()
    openIt()
    await act(async () => { rows('models')[3]!.click() })
    openIt()
    const recent = document.querySelector<HTMLElement>('.mpick .model-recent')!
    expect(recent.getAttribute('role')).toBe('radiogroup')
    expect(recent.getAttribute('aria-label')).toBe('gui.picker.recent')
    expect(recents().map((b) => b.getAttribute('aria-checked'))).toEqual(['true'])
  })
})
