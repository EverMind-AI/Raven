// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as store from '../features/rail/store'
import { setTranslator } from '../i18n/t'
import * as find from '../state/find'
import * as pageStore from '../state/page'
import { resetSources, setSources } from '../state/sources'
import { SearchDialog } from './SearchDialog'

import type { RailSource, SessRow } from '../features/rail/types'

/* The rail island's row list is a snapshot the dialog reads, and opening a
   conversation goes through the source -- the two seams production wires. */
function mount(rows: SessRow[], term = ''): { calls: string[] } {
  const calls: string[] = []
  setSources({
    rail: {
      snapshot: () => ({ rows, cur: null, busy: false }),
      replace: () => {},
      open: (s: SessRow) => calls.push(s.id),
    } as unknown as RailSource,
  })
  store.set({ snap: { rows, cur: null, busy: false }, skel: false })
  find.set({ open: true, query: term })
  render(<SearchDialog />)
  return { calls }
}

const field = (): HTMLInputElement => document.getElementById('sfind') as HTMLInputElement
const veil = (): HTMLElement => document.getElementById('findVeil')!
const hits = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.chrome-find-hit')]
const marked = (): Array<string | null> => [...document.querySelectorAll('.chrome-find-hit mark')].map((m) => m.textContent)

beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
})

afterEach(() => {
  cleanup()
  find._resetForTests()
  resetSources()
  document.body.innerHTML = ''
})

describe('the search dialog', () => {
  it('counts the matching conversations', () => {
    mount([{ id: 'a', title: 'GTM research', last: 'made a table' }, { id: 'b', title: 'second task' }], 'gtm')
    expect(screen.getByText('gui.find.count {"n":1}')).toBeTruthy()
    expect(hits().length).toBe(1)
  })

  it('marks the matched run in the title and the excerpt', () => {
    mount([{ id: 'a', title: 'GTM research', last: 'made a table' }], 'gtm')
    expect(marked()).toEqual(['GTM'])
  })

  it('leaves a row with no last line to its title', () => {
    mount([{ id: 'a', title: 'a lone note' }], 'lone')
    expect(hits()[0]!.querySelector('.chrome-find-title')!.textContent).toBe('a lone note')
    expect(hits()[0]!.querySelector('.chrome-find-excerpt')).toBeNull()
  })

  it('says so when nothing matches', () => {
    mount([{ id: 'a', title: 'GTM research' }], 'zzz')
    expect(screen.getByText('gui.find.empty')).toBeTruthy()
    expect(hits()).toHaveLength(0)
  })

  it('treats a term of regex characters as text', () => {
    mount([{ id: 'a', title: '(a) notes' }], '(a')
    expect(document.querySelector('.chrome-find-title')!.textContent).toBe('(a) notes')
    expect(marked()).toEqual(['(a'])
  })

  it('moves the active row with the arrow keys and wraps', () => {
    mount([{ id: 'a', title: 'alpha' }, { id: 'b', title: 'beta' }], 'a')
    expect(document.getElementById('findhit-0')!.getAttribute('aria-selected')).toBe('true')
    fireEvent.keyDown(field(), { key: 'ArrowDown' })
    expect(document.getElementById('findhit-1')!.getAttribute('aria-selected')).toBe('true')
    fireEvent.keyDown(field(), { key: 'ArrowDown' })
    expect(document.getElementById('findhit-0')!.getAttribute('aria-selected')).toBe('true')
    fireEvent.keyDown(field(), { key: 'ArrowUp' })
    expect(document.getElementById('findhit-1')!.getAttribute('aria-selected')).toBe('true')
  })

  it('opens the active row on Enter and closes the dialog', () => {
    const h = mount([{ id: 'a', title: 'alpha' }, { id: 'b', title: 'beta' }], 'a')
    fireEvent.keyDown(field(), { key: 'ArrowDown' })
    fireEvent.keyDown(field(), { key: 'Enter' })
    expect(h.calls).toEqual(['b'])
    expect(find.get().open).toBe(false)
  })

  it('opens the clicked row', () => {
    const h = mount([{ id: 'a', title: 'alpha' }, { id: 'b', title: 'beta' }], 'a')
    fireEvent.click(hits()[0]!)
    expect(h.calls).toEqual(['a'])
    expect(find.get().open).toBe(false)
  })

  it('does nothing on Enter or the arrows until a term is typed', () => {
    const h = mount([{ id: 'a', title: 'alpha' }, { id: 'b', title: 'beta' }])
    fireEvent.keyDown(field(), { key: 'Enter' })
    fireEvent.keyDown(field(), { key: 'ArrowDown' })
    fireEvent.keyDown(field(), { key: 'ArrowDown' })
    fireEvent.keyDown(field(), { key: 'Enter' })
    expect(h.calls).toEqual([])
    expect(find.get().open).toBe(true)
  })

  it('keeps the active row scrolled into view', () => {
    const rows = Array.from({ length: 12 }, (_, i) => ({ id: `s${i}`, title: `session ${i}` }))
    mount(rows, 'session')
    const spy = vi.fn()
    HTMLElement.prototype.scrollIntoView = spy
    fireEvent.keyDown(field(), { key: 'ArrowDown' })
    fireEvent.keyDown(field(), { key: 'ArrowDown' })
    expect(spy).toHaveBeenLastCalledWith({ block: 'nearest' })
    expect(spy.mock.instances.at(-1)).toBe(document.getElementById('findhit-2'))
  })

  it('closes on Escape without letting the page see it', () => {
    mount([{ id: 'a', title: 'alpha' }], 'a')
    let seen = 0
    const listener = (): void => {
      seen += 1
    }
    document.addEventListener('keydown', listener)
    fireEvent.keyDown(field(), { key: 'Escape' })
    document.removeEventListener('keydown', listener)
    expect(find.get().open).toBe(false)
    expect(seen).toBe(0)
  })

  it('closes on a click beside the card', () => {
    mount([{ id: 'a', title: 'alpha' }], 'a')
    fireEvent.click(veil())
    expect(find.get().open).toBe(false)
  })

  it('ignores Enter while an input method is composing', () => {
    const h = mount([{ id: 'a', title: 'alpha' }], 'a')
    fireEvent.keyDown(field(), { key: 'Enter', isComposing: true })
    expect(h.calls).toEqual([])
    expect(find.get().open).toBe(true)
  })

  it('empties the field when it closes and focuses it when it opens', () => {
    mount([{ id: 'a', title: 'alpha' }])
    fireEvent.input(field(), { target: { value: 'gtm' } })
    expect(field().value).toBe('gtm')
    fireEvent.click(veil())
    expect(field().value).toBe('')
    expect(find.get().query).toBe('')
    act(() => {
      find.toggle(true)
    })
    expect(document.activeElement).toBe(field())
  })
})
