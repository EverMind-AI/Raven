// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FixtureTransport } from '../../rpc/fixtureTransport'
import { setGateway } from '../../rpc/gateway'
import { resetSources, setSources } from '../../state/sources'
import * as history from './history'
import * as store from './store'

import type { ComposerSource } from './types'

/* The arrow keys at the dock's own handler: the field is uncontrolled, so the
 * only place these rules can be held is what `fieldKeydown` does to it. The
 * history module's own state machine is covered next door; this file is about
 * the conditions around it -- first/last logical line, selection, IME, the
 * slash palette, a queue row -- and about the two send paths recording text. */

const ta = (): HTMLTextAreaElement => document.getElementById('ta') as HTMLTextAreaElement

function source(overrides: Partial<ComposerSource> = {}): ComposerSource {
  return {
    meter: () => '',
    slash: [],
    slashName: (id) => id.replace(/^gui\./, ''),
    slashHelp: () => '',
    send: () => {},
    stop: () => {},
    ...overrides,
  }
}

async function boot(entries: string[]): Promise<FixtureTransport> {
  const transport = new FixtureTransport({
    'input.history.list': { entries },
    'input.history.append': { ok: true, appended: true },
  })
  setGateway(transport)
  await history.init()
  return transport
}

function type(value: string, caret = value.length): void {
  ta().value = value
  ta().setSelectionRange(caret, caret)
}

function press(key: string, init: KeyboardEventInit = {}): KeyboardEvent {
  const e = new KeyboardEvent('keydown', { key, cancelable: true, ...init })
  store.fieldKeydown(e)
  return e
}

beforeEach(() => {
  document.body.innerHTML =
    '<div class="chat"></div><div class="dock"><div class="dock-in"><div class="field">'
    + '<textarea id="ta"></textarea></div></div></div><button id="go"></button>'
  store._resetForTests()
  setSources({ composer: source() })
  setGateway(null)
  localStorage.clear()
})

afterEach(() => {
  resetSources()
  setGateway(null)
  vi.restoreAllMocks()
})

describe('when up recalls', () => {
  it('takes the newest entry from an empty field and leaves the caret at its end', async () => {
    await boot(['one', 'two words'])
    expect(press('ArrowUp').defaultPrevented).toBe(true)
    expect(ta().value).toBe('two words')
    expect(ta().selectionStart).toBe('two words'.length)
  })

  it('only fires from the first logical line of a multi-line entry', async () => {
    await boot(['older', 'first\nsecond'])
    press('ArrowUp')
    expect(ta().value).toBe('first\nsecond')

    expect(press('ArrowUp').defaultPrevented).toBe(false)
    expect(ta().value).toBe('first\nsecond')

    ta().setSelectionRange(0, 0)
    expect(press('ArrowUp').defaultPrevented).toBe(true)
    expect(ta().value).toBe('older')
  })

  it('stops at the oldest entry instead of wrapping', async () => {
    await boot(['one', 'two'])
    press('ArrowUp')
    press('ArrowUp')
    expect(ta().value).toBe('one')
    expect(press('ArrowUp').defaultPrevented).toBe(false)
    expect(ta().value).toBe('one')
  })
})

describe('when down advances', () => {
  it('restores the draft past the newest entry, with the caret at its end', async () => {
    await boot(['one', 'two'])
    type('my draft', 3)
    press('ArrowUp')
    press('ArrowDown')
    expect(ta().value).toBe('my draft')
    expect(ta().selectionStart).toBe('my draft'.length)
  })

  it('treats a restored empty draft as an action', async () => {
    await boot(['only'])
    press('ArrowUp')
    expect(press('ArrowDown').defaultPrevented).toBe(true)
    expect(ta().value).toBe('')
  })

  it('does nothing when the field is not browsing', async () => {
    await boot(['only'])
    type('typed')
    expect(press('ArrowDown').defaultPrevented).toBe(false)
    expect(ta().value).toBe('typed')
  })

  it('only fires from the last logical line of a multi-line entry', async () => {
    await boot(['older', 'first\nsecond'])
    press('ArrowUp')
    ta().setSelectionRange(2, 2)
    expect(press('ArrowDown').defaultPrevented).toBe(false)
    expect(ta().value).toBe('first\nsecond')
  })
})

describe('what gets in the way', () => {
  it('lets an IME own the arrows', async () => {
    await boot(['x'])
    const e = press('ArrowUp', { isComposing: true })
    expect(e.defaultPrevented).toBe(false)
    expect(ta().value).toBe('')
    expect(store.composing({ keyCode: 229 })).toBe(true)
  })

  it('lets the slash palette eat the arrows first', async () => {
    const fn = vi.fn()
    setSources({
      composer: source({
        slash: [{ id: 'gui.clear', fn }, { id: 'gui.compress', fn }],
      }),
    })
    await boot([])
    type('/c')
    store.fieldInput()
    expect(store.get().slashOpen).toBe(true)

    expect(press('ArrowDown').defaultPrevented).toBe(true)
    expect(store.get().slashSel).toBe(1)
    expect(press('ArrowUp').defaultPrevented).toBe(true)
    expect(store.get().slashSel).toBe(0)
    expect(ta().value).toBe('/c')
  })

  it('stays out of a queue row being edited', async () => {
    await boot(['one'])
    store.editRow(0)
    type('')
    expect(press('ArrowUp').defaultPrevented).toBe(false)
    expect(ta().value).toBe('')
  })

  it('does not navigate while a selection exists', async () => {
    await boot(['one'])
    type('abc')
    ta().setSelectionRange(0, 2)
    expect(press('ArrowUp').defaultPrevented).toBe(false)
    expect(ta().value).toBe('abc')
  })
})

describe('the send paths', () => {
  it('records what was typed, not the generated attachment note', async () => {
    const send = vi.fn()
    setSources({ composer: source({ send }) })
    await boot([])

    type('hello')
    store.fireSend()
    expect(send).toHaveBeenCalledWith('hello')

    store.addPaths([{ path: '/tmp/x.png', url: 'http://127.0.0.1:1/file?path=%2Ftmp%2Fx.png' }])
    type('')
    store.fireSend()
    expect(String(send.mock.calls[1]?.[0])).toContain('/tmp/x.png')
    expect(history.entries()).toEqual(['hello'])
  })

  it('does not record a send that beforeSend vetoes', async () => {
    const send = vi.fn()
    setSources({ composer: source({ send, beforeSend: () => true }) })
    await boot([])
    type('blocked')
    store.fireSend()
    expect(send).not.toHaveBeenCalled()
    expect(history.entries()).toEqual([])
  })

  it('records a slash command like any other submitted line', async () => {
    const fn = vi.fn()
    setSources({ composer: source({ slash: [{ id: 'gui.clear', fn }] }) })
    await boot([])
    type('/clear')
    store.fieldInput()
    press('Enter')
    expect(fn).toHaveBeenCalled()
    expect(ta().value).toBe('')
    expect(history.entries()).toEqual(['/clear'])
  })

  it('records the command it ran, not the prefix that opened the palette', async () => {
    const fn = vi.fn()
    setSources({ composer: source({ slash: [{ id: 'gui.compress', fn }] }) })
    await boot([])
    type('/c')
    store.fieldInput()
    press('Enter')
    expect(fn).toHaveBeenCalled()
    expect(history.entries()).toEqual(['/compress'])
  })
})

describe('a recalled slash command', () => {
  it('reopens the palette, so Enter runs it instead of sending it as prompt text', async () => {
    const fn = vi.fn()
    const send = vi.fn()
    setSources({ composer: source({ slash: [{ id: 'gui.clear', fn }], send }) })
    await boot(['/clear'])

    press('ArrowUp')
    expect(ta().value).toBe('/clear')
    expect(store.slashIsOpen()).toBe(true)

    press('Enter')
    expect(fn).toHaveBeenCalledTimes(1)
    expect(send).not.toHaveBeenCalled()
  })
})

describe('a conversation switch', () => {
  it('ends a browse, so the next up starts from the newest entry again', async () => {
    await boot(['old', 'new'])
    press('ArrowUp')
    expect(ta().value).toBe('new')

    store.loadDraft('conversation-b')
    expect(ta().value).toBe('')

    press('ArrowUp')
    expect(ta().value).toBe('new')
  })
})
