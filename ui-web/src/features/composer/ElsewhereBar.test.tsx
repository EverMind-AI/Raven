// @vitest-environment happy-dom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { _resetForTests as sessionReset, setCurrent } from '../../lib/session'
import * as pageStore from '../../state/page'
import { _resetForTests as rackReset, add, remove } from '../../state/sheetRack'
import { resetSources, setSources } from '../../state/sources'
import { _resetForTests as elsewhereReset, ElsewhereBar, installElsewhere, waitingElsewhere } from './ElsewhereBar'

import type { RailSource, SessRow } from '../rail/types'
import type { Root } from 'react-dom/client'

/* A sheet that asks, docked in one conversation: what a pending approval or
   question looks like to the rack. */
function asking(key: string): HTMLElement {
  const el = document.createElement('div')
  el.className = 'csheet'
  el.dataset.asks = '1'
  add(el, key)
  return el
}

const opened: string[] = []
const rows: SessRow[] = [{ id: 'a', title: 'A' }, { id: 'b', title: 'B' }] as SessRow[]

let root: Root | null = null
let host: HTMLElement | null = null

beforeEach(() => {
  sessionReset()
  rackReset()
  elsewhereReset()
  opened.length = 0
  setTranslator((key) => key)
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  setSources({ rail: {
    snapshot: () => ({ rows, cur: 'a', busy: false }),
    replace: () => {},
    open: (s: SessRow) => { opened.push(s.id) },
  } as unknown as RailSource })
  setCurrent('a')
  installElsewhere()
  document.body.innerHTML = '<div class="dock"><div class="sheets" id="sheetRack"></div></div>'
  host = document.createElement('div')
  host.hidden = true
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root?.unmount())
  root = null
  host = null
  resetSources()
  resetTranslator()
  sessionReset()
  document.body.innerHTML = ''
})

const paint = (): void => act(() => { root!.render(<ElsewhereBar host={host!} />) })

describe('the line that says another conversation is waiting', () => {
  it('names a conversation the reader is not looking at, and only while it asks', () => {
    expect(waitingElsewhere()).toEqual([])
    const sheet = asking('b')
    expect(waitingElsewhere()).toEqual(['b'])
    remove(sheet)
    expect(waitingElsewhere()).toEqual([])
  })

  it('says nothing about the open conversation, and follows the reader as they switch', () => {
    asking('a')
    expect(waitingElsewhere()).toEqual([])
    act(() => setCurrent('b'))
    expect(waitingElsewhere()).toEqual(['a'])
    act(() => setCurrent('a'))
    expect(waitingElsewhere()).toEqual([])
  })

  it('leaves out the draft, and keeps a conversation the list does not hold yet', () => {
    asking('(draft)')
    /* Not on disk until its first turn ends -- and its first turn is what is
       waiting. Opening it takes a detached row, as a reconnect does. */
    asking('ghost')
    expect(waitingElsewhere()).toEqual(['ghost'])
    paint()
    act(() => host!.querySelector('button')!.click())
    expect(opened).toEqual(['ghost'])
  })

  it('shows its host only while somebody is waiting, and opens that conversation on the button', () => {
    paint()
    expect(host!.hidden).toBe(true)
    act(() => { asking('b') })
    expect(host!.hidden).toBe(false)
    expect(host!.textContent).toBe('gui.confirm.elsewheregui.confirm.elsewhere_go')

    act(() => host!.querySelector('button')!.click())
    expect(opened).toEqual(['b'])
    expect(pageStore.show).toHaveBeenCalledWith(null)
    /* Now that conversation is the open one, so nobody else is waiting. */
    expect(host!.hidden).toBe(true)
  })
})
