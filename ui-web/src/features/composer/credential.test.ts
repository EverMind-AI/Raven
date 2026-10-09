// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { ESC_LABEL, chordLabel } from '../../lib/platform'
import { _resetForTests as sessionReset, setCurrent } from '../../lib/session'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import { _resetForTests as draftsReset } from '../../state/sheetDrafts'
import { _resetForTests as rackReset } from '../../state/sheetRack'
import { mountPageRoot } from '../../test/pageRoot'
import { _resetForTests as approveReset, open as openConfirm } from './approve'
import { _resetForTests as credentialReset, closeCredential, openCredential } from './credential'

import type { CredentialHandlers } from './credential'

let unmount: (() => void) | null = null

const rack = (): HTMLElement => document.getElementById('sheetRack')!
const sheets = (): HTMLElement[] => [...rack().querySelectorAll<HTMLElement>('.csheet')]
const opts = (): HTMLElement[] => [...rack().querySelectorAll<HTMLElement>('.opt')]
const field = (): HTMLInputElement => rack().querySelector<HTMLInputElement>('input[data-credential]')!
const key = (k: string): void => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }))
}
const tick = (): Promise<void> => new Promise((r) => setTimeout(r, 0))

const REQ = { requestId: 'cr-1', label: 'Tavily API key', note: '', replaces: false }

function handlers(reply: { ok?: boolean; error?: string } | Error = { ok: true }): CredentialHandlers & {
  sent: string[]; skipped: number
} {
  const h = {
    sent: [] as string[],
    skipped: 0,
    submit: async (value: string) => {
      h.sent.push(value)
      if (reply instanceof Error) throw reply
      return reply
    },
    skip: async () => { h.skipped += 1 },
  }
  return h
}

beforeEach(() => {
  sessionReset()
  setCurrent('a')
  rackReset()
  draftsReset()
  setTranslator((k) => k)
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
  document.body.innerHTML =
    '<div class="chat"><div class="dock"><div class="sheets" id="sheetRack"></div><div class="dock-in"></div></div></div>'
  unmount = mountPageRoot()
})

afterEach(() => {
  if (unmount) unmount()
  unmount = null
  credentialReset()
  approveReset()
  sessionReset()
  resetTranslator()
  document.body.innerHTML = ''
})

describe('the credential card', () => {
  it('asks for what the host named, in a masked field', () => {
    openCredential({ ...REQ, replaces: true }, handlers())
    expect(sheets().length).toBe(1)
    expect(rack().querySelector('.cp-ev-path')!.textContent).toBe('Tavily API key')
    expect(field().type).toBe('password')
    expect(rack().textContent).toContain('gui.confirm.cred.replaces')
    expect(sheets()[0]!.dataset.asks).toBe('1')
  })

  it('sends what was typed, clears the field at once, and goes when the host saved it', async () => {
    const h = handlers()
    openCredential(REQ, h)
    field().value = '  tvly-typed  '
    opts()[1]!.click()
    expect(field()?.value ?? '').toBe('')
    await tick()
    expect(h.sent).toEqual(['tvly-typed'])
    expect(sheets().length).toBe(0)
  })

  it('stays up with the reason when the host would not take the value', async () => {
    const h = handlers({ ok: false, error: 'That does not look like a Tavily key.' })
    openCredential(REQ, h)
    field().value = 'nope'
    opts()[1]!.click()
    await tick()
    expect(sheets().length).toBe(1)
    expect(rack().querySelector('[role="alert"]')!.textContent).toBe('That does not look like a Tavily key.')
    expect(field().value).toBe('')
  })

  it('says it was not sent when the call never got there', async () => {
    openCredential(REQ, handlers(new Error('socket closed')))
    field().value = 'tvly-typed'
    opts()[1]!.click()
    await tick()
    expect(rack().querySelector('[role="alert"]')!.textContent).toBe('gui.confirm.cred.unsent')
  })

  it('sends nothing for an empty field', async () => {
    const h = handlers()
    openCredential(REQ, h)
    opts()[1]!.click()
    await tick()
    expect(h.sent).toEqual([])
    expect(sheets().length).toBe(1)
  })

  /* The approval sheets' keys, drawn as caps: no row is numbered, and a digit
     answers nothing, in the field or out of it. */
  it('shows its keys as caps, not numbers, and answers no digit', async () => {
    const h = handlers()
    openCredential(REQ, h)
    expect(rack().querySelector('.opt .n')).toBeNull()
    expect(rack().querySelector('.body .opt')).toBeNull()
    expect(rack().querySelectorAll('.cp-acts .opt').length).toBe(2)
    expect(opts().map((o) => o.querySelector('kbd')?.textContent)).toEqual([ESC_LABEL, chordLabel()])
    field().value = 'tvly-typed'
    key('1')
    key('2')
    await tick()
    expect(h.sent).toEqual([])
    expect(h.skipped).toBe(0)
    expect(sheets().length).toBe(1)
  })

  it('saves on the send chord pressed outside the field', async () => {
    const h = handlers()
    openCredential(REQ, h)
    field().value = 'tvly-typed'
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', metaKey: true, bubbles: true }))
    await tick()
    expect(h.sent).toEqual(['tvly-typed'])
  })

  it('skips on the skip row, on Escape, and never saves on a digit typed into the field', async () => {
    const h = handlers()
    openCredential(REQ, h)
    field().dispatchEvent(new KeyboardEvent('keydown', { key: '1', bubbles: true }))
    await tick()
    expect(h.sent).toEqual([])
    key('Escape')
    await tick()
    expect(h.skipped).toBe(1)
    expect(sheets().length).toBe(0)

    const again = handlers()
    openCredential({ ...REQ, requestId: 'cr-2' }, again)
    opts()[0]!.click()
    await tick()
    expect(again.skipped).toBe(1)
  })

  /* Seen live: skipping the card with Escape also stopped the turn, which was
     about to tell the reader where the key can be entered instead. */
  it('keeps its Escape from reaching the page, which would interrupt the turn', async () => {
    const reached: string[] = []
    const onPage = (e: KeyboardEvent): void => { reached.push(e.key) }
    document.addEventListener('keydown', onPage)
    const h = handlers()
    openCredential(REQ, h)
    key('Escape')
    await tick()
    document.removeEventListener('keydown', onPage)
    expect(h.skipped).toBe(1)
    expect(reached).toEqual([])
  })

  it('draws one card per request, and credential.closed takes exactly that one down', () => {
    openCredential(REQ, handlers())
    openCredential(REQ, handlers())
    expect(sheets().length).toBe(1)
    closeCredential('someone-else')
    expect(sheets().length).toBe(1)
    closeCredential('cr-1')
    expect(sheets().length).toBe(0)
  })

  /* The turn is stopped on this card; a confirm sweeping it away would leave the
     host waiting out its deadline with nobody able to answer. */
  it('is left standing when another sheet arrives on the same conversation', () => {
    openCredential(REQ, handlers())
    openConfirm('rm -rf build/')
    expect(rack().querySelector('input[data-credential]')).not.toBeNull()
  })
})
