// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import { resetSources, setSources } from '../../state/sources'
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
  [...document.querySelectorAll<HTMLElement>(`.mpick .${col} .row:not(.model-typed)`)]
const typedRow = (): HTMLElement | null => document.querySelector<HTMLElement>('.mpick .model-typed')
const field = (): HTMLInputElement => document.querySelector('.mpick .find input')!
const providerSelected = (index: number): string | null | undefined =>
  rows('provs')[index]?.querySelector('.model-provider-action')?.getAttribute('aria-selected')

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
       model set by onboarding with no column to be marked in. */
    install()
    mount()
    openIt()
    /* 'NNothing': a provider the mark table has no drawing for gets a lettered
       tile, and the tile's letter sits inside the name element. */
    expect(rows('provs').map((b) => b.querySelector('.nm')!.textContent)).toEqual(['MiniMax (Global)', 'Anthropic', 'NNothing'])
    expect(rows('provs')[0]!.querySelector('.nm')?.firstElementChild?.getAttribute('src')).toBe('assets/providers/minimax.svg')
    /* A name, not a link: the row's whole job is to change the column beside
       it, and an anchor in the middle of it sent the reader out to a marketing
       page instead of selecting the provider they clicked. */
    expect(rows('provs')[0]!.querySelector('a')).toBeNull()
    expect(rows('provs')[0]!.lastElementChild?.className).toBe('provider-status on')
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
    expect(providerSelected(1)).toBe('true')
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
    expect(provs.map((b) => b.querySelector('.ct')!.textContent)).toEqual(['1', '0', '0'])
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
    expect(providerSelected(1)).toBe('true')
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
    expect(providerSelected(1)).toBe('true')
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
    expect(providerSelected(1)).toBe('true')
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

  it('distinguishes an empty search result from a column with nothing of the kind', () => {
    install({}, [
      { id: 'a', name: 'A', models: ['one'], on: true },
      { id: 'b', name: 'B', models: [], on: true },
    ])
    mount()
    openIt()
    expect(rows('provs').length).toBe(2)
    /* Two different empty columns, and the words are the whole difference: one
       is a term to delete, the other a list to go and build. */
    type('nothing-like-this')
    expect(document.querySelector('.mpick .models .empty')!.textContent).toBe('gui.picker.no_match')
    type('')
    act(() => { fireEvent.click(rows('provs')[1]!.querySelector('.model-provider-action')!) })
    expect(document.querySelector('.mpick .models .empty')!.textContent).toBe(
      'gui.picker.empty_kind {"kind":"gui.model.type.text"}',
    )
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
    await act(async () => { fireEvent.click(typedRow()!) })
    expect(h.added).toEqual([['m2', 'minimax', 'text']])
    expect(h.persisted).toEqual(['m2'])
  })

  it('leaves the typed row out once the term is a model exactly', () => {
    install()
    mount()
    openIt()
    type('minimax-m2')
    expect(typedRow()).toBeNull()
  })

  it('states the kind on the chip, cycles it, and sends what it says', async () => {
    const h = install()
    mount()
    openIt()
    type('my-team/bge-reranker-x')
    const chip = (): HTMLElement => typedRow()!.querySelector<HTMLElement>('.model-kind')!
    /* The name's own guess, the way registry_data.inferred_tags would read it. */
    expect(chip().textContent).toBe('gui.model.type.reranker')
    act(() => { fireEvent.click(chip()) })
    expect(chip().textContent).toBe('gui.model.type.audio')
    await act(async () => { fireEvent.click(typedRow()!) })
    expect(h.added).toEqual([['my-team/bge-reranker-x', 'minimax', 'audio']])
  })

  it('a slot opening starts the chip on the slot kind, not on the name', () => {
    install({}, [{ id: 'p', name: 'P', on: true, models: ['emb-1'], labels: { 'emb-1': { kind: 'embedding' } } }])
    mount()
    act(() => {
      store.open(document.getElementById('modelChip'), undefined, undefined,
        { kind: 'embedding', title: 'Embedding', pick: async () => {} })
    })
    type('plain-name')
    expect(typedRow()!.querySelector('.model-kind')!.textContent).toBe('gui.model.type.embedding')
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

  it('opens above the composer card, not above the chip on it', () => {
    install()
    const chip = onTheCard()
    measure(new Map([['.mpick', [0, 300]], ['.dock-in', [436, 126]], ['#modelChip', [518, 21]]]))
    mount()
    openIt(chip)
    /* 436 - 300 - 8. Off the chip it was 210, which put three quarters of the
       popover over the field the reader types in. */
    expect(pick()!.style.top).toBe('128px')
  })

  it('drops below the whole card when there is no room above it', () => {
    install()
    const chip = onTheCard()
    /* A card near the top of a tall window: nothing fits above it, so the
       popover goes under -- under the CARD, or it would cover the bar the chip
       itself sits on. */
    Object.defineProperty(document.documentElement, 'clientHeight', { value: 900, configurable: true })
    measure(new Map([['.mpick', [0, 300]], ['.dock-in', [60, 126]], ['#modelChip', [142, 21]]]))
    mount()
    openIt(chip)
    /* 60 + 126 + 8, clear of the card's lower edge. Off the chip it was 171 --
       eight pixels under the chip and straight over the bar beside it. */
    expect(pick()!.style.top).toBe('194px')
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

describe('the capability icons', () => {
  it('draws one icon per published capability, and a window badge beside them', () => {
    install({}, TAGGED)
    mount()
    openIt()
    const row = rows('models')[0]!
    const drawn = [...row.querySelectorAll('.model-tag use')].map((u) => u.getAttribute('href'))
    expect(drawn).toEqual(['#mtag-reasoning', '#mtag-function-call', '#mtag-image-recognition'])
    expect(row.querySelector('.model-window')!.textContent).toBe('1M')
  })

  it('draws nothing for a model the registry knows nothing about', () => {
    install({}, TAGGED)
    mount()
    openIt()
    /* Absence is "unknown", not "cannot": the second Anthropic row has no
       label entry at all and must come back with no icons rather than with a
       row of crossed-out ones. */
    expect(rows('models')[1]!.querySelector('.model-tags')).toBeNull()
  })

  it('shows the label where there is one and keeps the id on the title', () => {
    install({}, TAGGED)
    mount()
    openIt()
    const [named, bare] = rows('models')
    expect(named!.querySelector('.nm')!.textContent).toBe('Claude Opus 5')
    expect(named!.getAttribute('title')).toContain('claude-opus-5')
    expect(bare!.querySelector('.nm')!.textContent).toBe('claude-sonnet-5')
  })

  it('defines each symbol once for the whole popover', () => {
    install({}, TAGGED)
    mount()
    openIt()
    /* `use` resolves the first definition of an id, so a second copy would be
       dead markup repeated on every open. */
    expect(document.querySelectorAll('.mpick .model-tag-defs').length).toBe(1)
  })

  it('keeps its rendered shape', () => {
    install({}, TAGGED)
    const view = mount()
    openIt()
    expect(domSnapshot(view.container)).toMatchSnapshot()
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
    expect(rows('provs').map((b) => b.querySelector('.nm')!.textContent)).toEqual(['Anthropic', 'OpenAI', 'NNothing'])
    expect(rows('provs').map((b) => b.querySelector('.ct')!.textContent)).toEqual(['1', '1', '0'])
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
