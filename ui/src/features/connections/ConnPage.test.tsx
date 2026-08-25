// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConnApp } from './ConnPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { ConnChannel, ConnQr, ConnSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

function chan(over: Partial<ConnChannel> = {}): ConnChannel {
  return { id: 'slack', name: 'Slack', on: false, ...over }
}

/* The island runs against the same two seams production wires up: a fake
   shell on window.RavenShell (T returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on window.DS.conn. */
function install(rows: ConnChannel[], over: Partial<ConnSource> = {}) {
  const calls: Array<[string, unknown]> = []
  const source: ConnSource = {
    rows: async () => rows,
    toggle: async (c, on) => {
      c.on = on
      calls.push(['toggle', on])
    },
    apply: async (c, patch, enable) => calls.push(['apply', { id: c.id, patch, enable }]),
    qr: async () => null,
    ...over,
  }
  const shellCalls: Array<[string, unknown]> = []
  const fakeShell: Shell = {
    T: (key, vars, fallback) =>
      key.startsWith('gui.connf.') ? (fallback ?? key) : vars ? `${key} ${JSON.stringify(vars)}` : key,
    /* Confirms immediately: the dialog itself is legacy chrome, not island. */
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: (id) => shellCalls.push(['showPage', id]),
  }
  window.RavenShell = fakeShell
  window.DS = { conn: source }
  document.body.innerHTML =
    '<section id="connPage"><div id="connBody"></div></section>' +
    '<div class="veil" id="connVeil" data-open="false"></div>'
  return { source, calls, shellCalls }
}

async function mount() {
  const view = render(<ConnApp />, { container: document.getElementById('connBody')! })
  await act(async () => {
    store.open()
  })
  return view
}

afterEach(() => {
  act(() => {
    store.closeDialog()
  })
  cleanup()
  vi.restoreAllMocks()
})

describe('connections island', () => {
  it('lists every catalogue row under the hero, in order', async () => {
    install([chan(), chan({ id: 'telegram', name: 'Telegram' }), chan({ id: 'email', key: 'gui.chan.email' })])
    await mount()
    expect(await screen.findByText('Slack')).toBeTruthy()
    expect(screen.getByText('Telegram')).toBeTruthy()
    expect(screen.getByText('gui.chan.email')).toBeTruthy()
    expect(screen.getByText('gui.conn.hero_sub')).toBeTruthy()
  })

  it('marks a row missing required fields as unset, with configure as its only control', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
    await mount()
    expect(await screen.findByText('gui.conn.unset')).toBeTruthy()
    expect(screen.queryAllByRole('switch')).toHaveLength(0)
  })

  it('flips the switch optimistically through the source', async () => {
    const { calls } = install([chan({ on: true })])
    await mount()
    const swi = (await screen.findAllByRole('switch'))[0]!
    expect(swi.getAttribute('aria-checked')).toBe('true')
    await act(async () => {
      swi.click()
    })
    expect(calls).toContainEqual(['toggle', false])
    expect(swi.getAttribute('aria-checked')).toBe('false')
  })

  it('takes the switch back when the source reverts and rejects handled', async () => {
    install([chan({ on: true })], {
      /* What the live source does: optimistic flip now, revert on the rpc
         failure, reject handled so the island only redraws. */
      toggle: (c, on) => {
        c.on = on
        return Promise.resolve().then(() => {
          c.on = !on
          throw { handled: true }
        })
      },
    })
    await mount()
    const swi = (await screen.findAllByRole('switch'))[0]!
    await act(async () => {
      swi.click()
    })
    expect(swi.getAttribute('aria-checked')).toBe('true')
  })

  it('opens the credential dialog from the row and closes it on the veil', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    const veil = document.getElementById('connVeil')!
    expect(veil.dataset.open).toBe('true')
    expect(document.getElementById('connDlgTitle')!.textContent).toBe('Slack')
    await act(async () => {
      veil.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    expect(veil.dataset.open).toBe('false')
  })

  it('saves only the fields the reader filled, folding the optional ones', async () => {
    const { calls } = install([
      chan({
        fields: [
          { key: 'bot_token', required: true, secret: true },
          { key: 'proxy', required: false },
        ],
        missing: ['bot_token'],
      }),
    ])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    const fold = screen.getByText('gui.conn.advanced {"n":1}')
    expect(fold.getAttribute('aria-expanded')).toBe('false')
    const body = document.getElementById('connDlgBody')!
    const token = body.querySelector<HTMLInputElement>('input[type="password"]')!
    token.value = '  tok-1  '
    await act(async () => {
      screen.getByText('gui.conn.connect').click()
    })
    expect(calls).toContainEqual(['apply', { id: 'slack', patch: { bot_token: 'tok-1' }, enable: true }])
    expect(document.getElementById('connVeil')!.dataset.open).toBe('false')
  })

  it('disconnects through the confirm, never as a toggle', async () => {
    const { calls } = install([chan({ on: true })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    await act(async () => {
      screen.getByText('gui.conn.disconnect').click()
    })
    expect(calls).toContainEqual(['apply', { id: 'slack', patch: {}, enable: false }])
  })

  it('walks the scan panel from waiting to the code to paired, then stops polling', async () => {
    vi.useFakeTimers()
    const answers: ConnQr[] = [
      { connected: false },
      { connected: false, qr: 'data:image/png;base64,QQ==' },
      { connected: true },
    ]
    let polls = 0
    const { source } = install([chan({ on: true, qrLogin: true, running: true, connected: false })], {
      qr: async () => {
        polls += 1
        return answers[Math.min(polls, answers.length) - 1]!
      },
    })
    const rowsSpy = vi.spyOn(source, 'rows')
    await mount()
    await act(async () => {
      screen.getByText('Slack').click()
    })
    expect(screen.getByText('gui.conn.qr_wait')).toBeTruthy()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(screen.getByText('gui.conn.qr_scan')).toBeTruthy()
    expect(document.querySelector('.qrshot img')).toBeTruthy()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(screen.getByText('gui.conn.qr_done')).toBeTruthy()
    expect(document.querySelector('.qrsay.ok')).toBeTruthy()
    expect(document.querySelector('.qrshot img')).toBeNull()
    expect(rowsSpy.mock.calls.length).toBeGreaterThan(0)
    const settled = polls
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000)
    })
    expect(polls).toBe(settled)
    vi.useRealTimers()
  })

  it('keeps the scan panel off an unpaired channel that is switched off', async () => {
    install([chan({ on: false, qrLogin: true })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    expect(document.querySelector('.qrbox')).toBeNull()
  })
})
