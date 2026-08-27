// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConnApp } from './ConnPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { ConnChannel, ConnQr, ConnSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* Slack's way in is a token, and the fixture has to say so: an entry with no
   required field is a scan-login entry by derivation, so a fieldless default
   would quietly make the standard row a different kind of channel than the
   tests that use it mean. */
function chan(over: Partial<ConnChannel> = {}): ConnChannel {
  return {
    id: 'slack',
    name: 'Slack',
    on: false,
    fields: [{ key: 'bot_token', required: true, set: true }],
    missing: [],
    ...over,
  }
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
    /* Both real sources leave the row's flag where the write put it -- the rpc
       one by reloading status, the fixture one by mutating in place. A fake that
       only recorded the call left the row looking untouched, which is not a
       state the page can be in. */
    apply: async (c, patch, enable) => {
      calls.push(['apply', { id: c.id, patch, enable }])
      c.on = enable
    },
    qr: async () => null,
    ...over,
  }
  const shellCalls: Array<[string, unknown]> = []
  const fakeShell: Shell = {
    T: (key, vars, fallback) =>
      key.startsWith('gui.connf.') ? (fallback ?? key) : vars ? `${key} ${JSON.stringify(vars)}` : key,
    /* Confirms immediately: the dialog itself is legacy chrome, not island. */
    /* Records as well as confirms: "no dialog stands between the reader and the
       switch" is a claim only a spy can carry. */
    confirmAsk: (title, _b, _l, fn) => {
      shellCalls.push(['confirmAsk', title])
      fn()
    },
    showPage: (id) => shellCalls.push(['showPage', id]),
  }
  window.RavenShell = fakeShell
  window.DS = { conn: source }
  document.body.innerHTML =
    '<section id="connPage"><div id="connBody"></div></section>' +
    '<div class="veil" id="connVeil" data-open="false"></div>' +
    '<div id="menu" data-open="false"></div>'
  return { source, calls, shellCalls }
}

const rowsOf = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.surow')]
const rowNamed = (name: string): HTMLElement =>
  rowsOf().find((r) => r.querySelector('.nm b')!.textContent === name)!
const groupOf = (name: string): string | null =>
  rowNamed(name).closest('.sugrp')!.querySelector('.hd b')!.textContent
const subOf = (name: string): string | null => rowNamed(name).querySelector('.sufacts')?.textContent ?? null
const dotOf = (name: string): string | null => rowNamed(name).querySelector('.nm .led')!.className
/* The row's one control, whatever it is. */
const rowBtn = (name: string): HTMLElement => rowNamed(name).querySelector('.suact button')!
/* The steps' own state, which is what draws the tick and the current mark --
   the titles alone read the same whether or not the sequence advances. */
const wizStates = (): Array<string | null> =>
  [...document.querySelectorAll('#connVeil .suwiz .step')].map((s) => s.getAttribute('data-state'))

/* The card and its three parts. Head and foot are the card's own children and
   the body is the only thing that scrolls, so the name of what is being edited
   and the button that acts on it are both always on screen. Anchoring these
   helpers on the card rather than on #connDlgBody is what makes a part that
   slid back into the scroller fail here. */
const card = (): HTMLElement => document.querySelector('#connVeil .sheet')!
const cardHead = (): HTMLElement => card().querySelector(':scope > .suhead')!
const cardFoot = (): HTMLElement => card().querySelector(':scope > .sufoot')!

const rowActs = (name: string): Array<string | null> =>
  [...rowNamed(name).querySelectorAll('.suact button')].map((b) => b.getAttribute('role') === 'switch' ? 'switch' : b.textContent)

/* The dialog's overflow menu, through the shared #menu host production draws
   into. */
async function pickMenu(label: string): Promise<void> {
  await act(async () => {
    ;(document.querySelector('#connDlgBody .sumenu') as HTMLElement).click()
  })
  const item = [...document.querySelectorAll<HTMLElement>('#menu button')].find((b) => b.textContent === label)
  expect(item, `menu item ${label}`).toBeTruthy()
  await act(async () => {
    item!.click()
  })
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
  it('lists every catalogue row, grouped by whether it is in service', async () => {
    install([
      chan({ on: true, running: true }),
      chan({ id: 'telegram', name: 'Telegram' }),
      chan({ id: 'email', key: 'gui.chan.email' }),
    ])
    await mount()
    expect(await screen.findByText('Slack')).toBeTruthy()
    expect(screen.getByText('Telegram')).toBeTruthy()
    expect(screen.getByText('gui.chan.email')).toBeTruthy()
    expect(groupOf('Slack')).toBe('gui.conn.g_on')
    expect(groupOf('Telegram')).toBe('gui.conn.g_off')
    /* The page's own prose is gone: the title, and nothing under it. */
    expect(document.getElementById('connBody')!.textContent).not.toContain('gui.conn.hero_sub')
  })

  /* The addable group is ordered by what it costs to get in, so the quickest
     way to a working entry is the first row of it rather than wherever the
     catalogue happened to put it. */
  it('puts the cheapest way in first, and says what each costs', async () => {
    install([
      chan({ id: 'email', key: 'gui.chan.email', fields: [
        { key: 'imap_host', required: true }, { key: 'imap_user', required: true },
        { key: 'smtp_host', required: true }, { key: 'smtp_user', required: true },
      ] }),
      chan({ id: 'telegram', name: 'Telegram', fields: [{ key: 'token', required: true }] }),
      chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true }),
    ])
    await mount()
    const order = [...document.querySelectorAll('.surow .nm b')].map((b) => b.textContent)
    expect(order).toEqual(['gui.chan.weixin', 'Telegram', 'gui.chan.email'])
    expect(rowNamed('gui.chan.weixin').querySelector('.kd')!.textContent).toBe('gui.conn.cost_scan')
    expect(rowNamed('Telegram').querySelector('.kd')!.textContent).toBe('gui.conn.cost_n {"n":"1"}')
    expect(rowNamed('gui.chan.email').querySelector('.kd')!.textContent).toBe('gui.conn.cost_n {"n":"4"}')
  })

  /* Whether a channel signs in by scanning is a static fact about it, but the
     gateway only reports `qr_login` for an adapter that is already up -- so an
     entry nobody has switched on yet, which is the whole of the addable group,
     never carries the flag. The schema is the honest source: no required field
     means no form to fill, and the only way in is signing in. */
  it('reads a scan-login entry off its schema, not off the live flag', async () => {
    install([
      chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: false, fields: [{ key: 'route_tag' }] }),
      chan({ id: 'telegram', name: 'Telegram', fields: [{ key: 'token', required: true }] }),
    ])
    await mount()
    expect(await screen.findByText('gui.chan.weixin')).toBeTruthy()
    expect(rowNamed('gui.chan.weixin').querySelector('.kd')!.textContent).toBe('gui.conn.cost_scan')
    /* And it sorts ahead of the cheapest form. */
    expect([...document.querySelectorAll('.surow .nm b')].map((b) => b.textContent))
      .toEqual(['gui.chan.weixin', 'Telegram'])
  })

  it('offers the way in and says the cost as a badge, not a sentence', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
    await mount()
    expect(await screen.findByText('Slack')).toBeTruthy()
    expect(rowActs('Slack')).toEqual(['gui.conn.connect'])
    expect(rowNamed('Slack').querySelector('.kd')!.textContent).toBe('gui.conn.cost_n {"n":"1"}')
  })

  /* Health is the dot, not a sentence: the group heading says whether the
     entrance is in service, the badge says what it costs to get in and the
     button says what to do. A fourth line in grey said nothing new. */
  describe('the row carries no second line', () => {
    it('draws none, in any state', async () => {
      install([
        chan({ id: 'a', name: 'A', on: true, running: true, connected: true, who: 'me' }),
        chan({ id: 'b', name: 'B', on: true, running: false }),
        chan({ id: 'e', name: 'E', on: false, missing: ['bot_token'] }),
      ])
      await mount()
      /* By name, not by text: the tile beside it carries the same initial. */
      expect(rowNamed('A')).toBeTruthy()
      expect(rowsOf().map((r) => r.querySelector('.sufacts'))).toEqual([null, null, null])
      expect(subOf('B')).toBeNull()
      expect(document.querySelector('#connBody')!.textContent).not.toContain('gui.conn.st_down')
    })

    /* Five states, and they are not two: "the gateway could not be asked" is
       not "off", and "running but not paired" is not "receiving". The dot is
       where that survives on a row; the card says it in words. */
    it('still tells the five states apart by colour', async () => {
      install([
        chan({ id: 'a', name: 'A', on: true, running: true, connected: true, who: 'me' }),
        chan({ id: 'b', name: 'B', on: true, running: false }),
        chan({ id: 'c', name: 'C', on: true, running: true, connected: false, qrLogin: true }),
        chan({ id: 'd', name: 'D', on: true }),
        chan({ id: 'e', name: 'E', on: false }),
      ])
      await mount()
      expect(dotOf('A')).toBe('led')
      expect(dotOf('B')).toBe('led bad')
      expect(dotOf('C')).toBe('led warn')
      expect(dotOf('D')).toBe('led warn')
      expect(dotOf('E')).toBe('led off')
    })

    it('keeps the sentence in the card, where there is room for it', async () => {
      install([chan({ on: true, running: true })])
      await mount()
      await act(async () => {
        ;(await screen.findByText('Slack')).click()
      })
      expect(document.querySelector('#connDlgBody .sustate')!.textContent).toContain('gui.conn.st_live')
    })
  })

  /* The heart of it: pressing the button is a decision, not an arrival. An
     entrance reaches "in service" by receiving, and nothing else -- a made-up
     token used to look exactly like a working one, because the flag was the
     whole test. */
  describe('what counts as being in service', () => {
    it('puts a freshly switched-on entrance under connecting, not in service', async () => {
      install([
        chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [] }),
      ])
      await mount()
      expect(groupOf('gui.chan.weixin')).toBe('gui.conn.g_off')
      /* The row's button opens the card; the card's own button is the write. */
      await act(async () => {
        rowBtn('gui.chan.weixin').click()
      })
      expect(groupOf('gui.chan.weixin')).toBe('gui.conn.g_off')
      await act(async () => {
        ;(cardFoot().querySelector('button') as HTMLElement).click()
      })
      /* One group, not two: the code has not been scanned yet. */
      expect(groupOf('gui.chan.weixin')).toBe('gui.conn.g_pending')
    })

    it('keeps an entrance with made-up credentials out of in service', async () => {
      /* The adapter refused to start, which is all a bad token looks like from
         here. */
      install([chan({ on: true, running: false, fields: [{ key: 'bot_token', required: true, set: true }] })])
      await mount()
      expect(groupOf('Slack')).toBe('gui.conn.g_pending')
      expect(rowNamed('Slack').querySelector('.kd')!.textContent).toBe('gui.conn.tag_down')
    })

    it('keeps a scan entrance out of in service until it is paired', async () => {
      install([chan({ id: 'weixin', key: 'gui.chan.weixin', on: true, running: true, connected: false, qrLogin: true })])
      await mount()
      expect(groupOf('gui.chan.weixin')).toBe('gui.conn.g_pending')
    })

    it('promotes it the moment the adapter reports receiving', async () => {
      install([chan({ on: true, running: true, connected: true })])
      await mount()
      expect(groupOf('Slack')).toBe('gui.conn.g_on')
      /* And nothing on the row claims anything more. */
      expect(rowNamed('Slack').querySelector('.kd')).toBeNull()
    })

    /* The card used to close on the press, which is what made a rejected
       credential indistinguishable from an accepted one. */
    it('keeps the card up after the press, with the state line and a retry', async () => {
      install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
      await mount()
      await act(async () => {
        rowBtn('Slack').click()
      })
      const box = document.querySelector<HTMLInputElement>('#connDlgBody input')!
      box.value = 'made-up'
      await act(async () => {
        ;(cardFoot().querySelector('button') as HTMLElement).click()
      })
      expect(document.getElementById('connVeil')!.dataset.open).toBe('true')
      expect(document.querySelector('#connDlgBody .sustate')).toBeTruthy()
      expect(cardFoot().querySelector('button')!.textContent).toBe('gui.conn.retry')
    })

    it('closes the card once the entrance is receiving', async () => {
      const rows = [chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })]
      install(rows, {
        /* What the gateway answers when the credentials were good. */
        apply: async (c) => {
          c.on = true
          c.running = true
          c.connected = true
        },
      })
      await mount()
      await act(async () => {
        rowBtn('Slack').click()
      })
      const box = document.querySelector<HTMLInputElement>('#connDlgBody input')!
      box.value = 'a-real-token'
      await act(async () => {
        ;(cardFoot().querySelector('button') as HTMLElement).click()
      })
      expect(document.getElementById('connVeil')!.dataset.open).toBe('false')
      expect(groupOf('Slack')).toBe('gui.conn.g_on')
    })
  })

  /* One sentence about the gateway, at the top, instead of the same sentence
     repeated down twelve rows -- and it counts what is receiving, not what is
     switched on. Counting the switch is how the page came to say "receiving on
     1 entrance" over a card that said the adapter had never started. */
  describe('the gateway line', () => {
    const bar = (): HTMLElement => document.querySelector('#connBody > .sustate')!

    it('counts only the entrances that are actually receiving', async () => {
      install([
        chan({ on: true, running: true }),
        chan({ id: 'tg', name: 'Telegram', on: true, running: true }),
      ])
      await mount()
      expect(bar().textContent).toBe('gui.conn.gw_live {"n":"2"}')
      expect(bar().querySelector('.led')!.className).toBe('led')
    })

    /* The state the user hit: one entrance enabled, its adapter never started,
       and the page called it receiving. It says nothing now -- the connecting
       group carries that, and saying it twice made the top of the page a place
       for bad news. */
    it('says nothing when an entrance is switched on but not receiving', async () => {
      install([chan({ id: 'weixin', key: 'gui.chan.weixin', on: true, qrLogin: true, fields: [] })])
      await mount()
      expect(document.querySelector('#connBody > .sustate')).toBeNull()
      expect(groupOf('gui.chan.weixin')).toBe('gui.conn.g_pending')
    })

    it('counts only the receiving half when some are and some are not', async () => {
      install([chan({ on: true, running: true }), chan({ id: 'tg', name: 'Telegram', on: true, running: false })])
      await mount()
      expect(bar().textContent).toBe('gui.conn.gw_live {"n":"1"}')
    })
  })

  /* The group heading says the entrance was handed to Raven; the badge says how
     far that actually got. Without it the only signal was a coloured dot. */
  it('says on the row why an entrance in service is not receiving', async () => {
    install([
      chan({ id: 'a', name: 'A', on: true, running: true, connected: true }),
      chan({ id: 'b', name: 'B', on: true, running: false }),
      chan({ id: 'c', name: 'C', on: true, running: true, connected: false, qrLogin: true }),
      chan({ id: 'd', name: 'D', on: true }),
    ])
    await mount()
    const tag = (n: string): string | null => rowNamed(n).querySelector('.kd')?.textContent ?? null
    expect(tag('A')).toBeNull()
    expect(tag('B')).toBe('gui.conn.tag_down')
    expect(tag('C')).toBe('gui.conn.tag_unpaired')
    expect(tag('D')).toBe('gui.conn.tag_unknown')
  })

  /* Two verbs, the same pair the agents page offers. The switch is gone: a flag
     is the mechanism, not the errand. */
  it('takes an entrance out of service straight from the row, with no dialog', async () => {
    const { calls, shellCalls } = install([chan({ on: true, running: true })])
    await mount()
    expect(rowActs('Slack')).toEqual(['gui.conn.disconnect'])
    expect(document.querySelectorAll('[role="switch"]').length).toBe(0)
    await act(async () => {
      rowBtn('Slack').click()
    })
    expect(calls).toContainEqual(['toggle', false])
    expect(shellCalls.filter((c) => c[0] === 'confirmAsk')).toEqual([])
    /* Out of service, the row moves to the addable group and offers the way
       back in -- the same word it offers on an entrance never connected. */
    expect(groupOf('Slack')).toBe('gui.conn.g_off')
    expect(rowActs('Slack')).toEqual(['gui.conn.connect'])
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
    await act(async () => {
      rowBtn('Slack').click()
    })
    /* Reverted: the flag is back on, so the row offers disconnect again. */
    expect(groupOf('Slack')).toBe('gui.conn.g_pending')
    expect(rowActs('Slack')).toEqual(['gui.conn.disconnect'])
  })

  it('opens the credential dialog from the row and closes it on a click outside', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    const veil = document.getElementById('connVeil')!
    expect(veil.dataset.open).toBe('true')
    expect(cardHead().querySelector('.meta b')!.textContent).toBe('Slack')
    await act(async () => {
      document.getElementById('connBody')!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    })
    expect(veil.dataset.open).toBe('false')
  })

  /* A press inside the card is not a click-away. Closing on any mousedown is
     how the card would vanish the moment a credential box was focused. */
  it('stays open when the press lands inside the card', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    await act(async () => {
      document
        .querySelector('#connDlgBody input')!
        .dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    })
    expect(document.getElementById('connVeil')!.dataset.open).toBe('true')
  })

  /* The card is modal: the scrim covers the page and takes the press, so a
     click outside closes the card and does not also operate what it covered --
     pressing the rail behind an open card must not navigate, and pressing
     another row's disconnect must not disconnect it. The press closes; the next
     one acts. */
  it('is modal: the press that closes it does not also work the list behind', async () => {
    const { calls } = install([
      chan({ id: 'a', name: 'A', on: true, running: true }),
      chan({ id: 'telegram', name: 'Telegram', fields: [{ key: 'token', required: true }], missing: ['token'] }),
    ])
    await mount()
    await act(async () => {
      rowBtn('Telegram').click()
    })
    expect(cardHead().querySelector('.meta b')!.textContent).toBe('Telegram')
    /* Aim at the in-service row's disconnect, which is behind the scrim. */
    await act(async () => {
      const b = rowBtn('A')
      b.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    })
    expect(document.getElementById('connVeil')!.dataset.open).toBe('false')
    expect(calls.filter((c) => c[0] === 'toggle')).toEqual([])
    /* And the next press does act. */
    await act(async () => {
      rowBtn('A').click()
    })
    expect(calls).toContainEqual(['toggle', false])
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
    /* Closed means gone, not hidden: left in the tree it still took a row of
       the body's grid, which is a gap under the fold with nothing in it. */
    expect(document.querySelector('#connDlgBody .suadv .sufield')).toBeNull()
    const body = document.getElementById('connDlgBody')!
    const token = body.querySelector<HTMLInputElement>('input[type="password"]')!
    token.value = '  tok-1  '
    /* The dialog's own save, not the row's way-in button behind it: both read
       "connect", which is right -- they are the same intent at two depths. */
    await act(async () => {
      ;(cardFoot().querySelector('button') as HTMLElement).click()
    })
    expect(calls).toContainEqual(['apply', { id: 'slack', patch: { bot_token: 'tok-1' }, enable: true }])
  })

  /* Disconnect is a row control now, so the card has nothing left for a menu to
     hold -- and an overflow menu with one item in it was a menu for its own
     sake. */
  it('leaves no menu on the card', async () => {
    install([chan({ on: true, running: true })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    const face = [...card().querySelectorAll('button')].map((b) => b.textContent)
    expect(face.length).toBeGreaterThan(0)
    expect(card().querySelector('.sumenu')).toBeNull()
  })

  /* Where the credentials come from, for the channels that have one place to
     get them. A mail host has no open platform to link to, so it has none. */
  it('links to the console that issues the credentials, where there is one', async () => {
    install([chan({ id: 'telegram', name: 'Telegram', fields: [{ key: 'token', required: true }], missing: ['token'] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Telegram')).click()
    })
    const jump = document.querySelector<HTMLAnchorElement>('#connDlgBody a.jump')!
    expect(jump.href).toBe('https://t.me/BotFather')
    expect(jump.target).toBe('_blank')
    expect(document.querySelector('#connDlgBody .sucreds .n')!.textContent).toBe('0 / 1')
  })

  it('has no link for a channel whose credentials are not issued anywhere', async () => {
    install([chan({ id: 'email', key: 'gui.chan.email', fields: [{ key: 'imap_host', required: true }], missing: ['imap_host'] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.chan.email')).click()
    })
    expect(document.querySelector('#connDlgBody a.jump')).toBeNull()
  })

  /* The raw config key was printed as a second grey line under every box,
     saying the same thing in worse words. It rides on the label's tooltip now,
     so it is still reachable when a support answer names the key. */
  it('keeps the raw field key off the form and on the label', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true, label: 'Bot token' }], missing: ['bot_token'] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    const body = document.getElementById('connDlgBody')!
    expect(body.querySelector('.sufield label')!.textContent).toBe('Bot token')
    expect(body.querySelector('.sufield label')!.getAttribute('title')).toBe('bot_token')
    expect(body.querySelector('.hint')).toBeNull()
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

  /* Nothing in service yet is the ordinary first run, and a heading over an
     empty box saying so was the page explaining itself. */
  it('leaves out the in-service group until something is in it', async () => {
    install([chan({ missing: ['bot_token'] })])
    await mount()
    expect(await screen.findByText('Slack')).toBeTruthy()
    expect([...document.querySelectorAll('#connBody .sugrp .hd b')].map((b) => b.textContent)).toEqual([
      'gui.conn.g_off',
    ])
    expect(document.querySelector('#connBody .empty-note')).toBeNull()
  })

  /* The addable rows are still cost-ordered; the caption that said so is gone. */
  it('does not caption the ordering', async () => {
    install([chan({ on: true, running: true }), chan({ id: 'telegram', name: 'Telegram' })])
    await mount()
    expect(document.querySelector('#connBody .sugrp .hd .tool')).toBeNull()
    expect(document.querySelector('#connBody .free')).toBeNull()
  })

  /* The same centred card every module's detail is. It was briefly a side
     drawer, which made this the one set-up surface with a shape of its own. */
  it('draws the configure surface as the shared centred card', async () => {
    install([chan({ missing: ['bot_token'] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    /* Exactly `sheet`: a modifier here is a second shape for the same errand. */
    expect(document.querySelector('#connVeil .sheet')!.className).toBe('sheet')
  })

  it('closes the drawer from its own head', async () => {
    install([chan({ missing: ['bot_token'] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    await act(async () => {
      ;(cardHead().querySelector('.suclose') as HTMLElement).click()
    })
    expect(document.getElementById('connVeil')!.dataset.open).toBe('false')
  })

  /* The three-part card, which is the whole reason the head and the foot were
     lifted out of the scroller: mail specs six required credentials and fifteen
     optional ones, so an unfolded body is longer than the window. Pinning the
     foot inside it with `sticky` was the previous answer, and it left the field
     under the bar showing through the gutter. */
  it('keeps the head and the button out of the scroller', async () => {
    install([
      chan({
        fields: [
          { key: 'bot_token', required: true },
          { key: 'proxy', required: false },
        ],
        missing: ['bot_token'],
      }),
    ])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    expect([...card().children].map((n) => n.className)).toEqual(['suhead', 'subody', 'sufoot'])
    const body = document.getElementById('connDlgBody')!
    expect(body.querySelector('.suhead')).toBeNull()
    expect(body.querySelector('.sufoot')).toBeNull()
  })

  it('gives the scan wizard the same three parts', async () => {
    install([chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.chan.weixin')).click()
    })
    expect([...card().children].map((n) => n.className)).toEqual(['suhead', 'subody suwiz', 'sufoot'])
  })

  /* One fact, once. The credential count is the form's own caption, so the head
     does not print it a second line above -- it said "needs 1 credential" over a
     block already headed "credentials 0 / 1". */
  it('leaves the credential count to the form that counts it', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    expect(cardHead().querySelector('.l2')).toBeNull()
    expect(document.querySelector('#connDlgBody .sucreds .n')!.textContent).toBe('0 / 1')
    /* The row's cost badge does not travel into the head either: it is for
       scanning a list of twelve. */
    expect(cardHead().querySelector('.kd')).toBeNull()
  })

  /* Scanning is the one way in the body does not spell out: the wizard has no
     credential caption to carry it. */
  it('says so in the head where nothing else does', async () => {
    install([chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.chan.weixin')).click()
    })
    expect(cardHead().querySelector('.l2')!.textContent).toBe('gui.conn.cost_scan_line')
  })

  it('leaves the head silent once the state line has something to say', async () => {
    install([chan({ on: true, running: true })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    expect(cardHead().querySelector('.l2')).toBeNull()
    expect(document.querySelector('#connDlgBody .sustate')!.textContent).toContain('gui.conn.st_live')
  })

  /* The note beside the save button used to be an empty span. */
  it('says where the form stands, beside the button that acts on it', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true, secret: true }], missing: ['bot_token'] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    const body = document.getElementById('connDlgBody')!
    expect(cardFoot().querySelector('.n')!.textContent).toBe('gui.conn.foot_need {"n":"1"}')
    const box = body.querySelector<HTMLInputElement>('input[type="password"]')!
    await act(async () => {
      box.value = 'tok'
      box.dispatchEvent(new Event('input', { bubbles: true }))
    })
    expect(cardFoot().querySelector('.n')!.textContent).toBe('gui.conn.foot_dirty')
  })

  /* Mail is two servers. One flat column of six boxes left the reader counting
     which three belonged to which. */
  it('splits mail into the two servers it is', async () => {
    install([
      chan({
        id: 'email',
        key: 'gui.chan.email',
        fields: [
          { key: 'imap_host', required: true },
          { key: 'imap_username', required: true },
          { key: 'smtp_host', required: true },
        ],
        missing: ['imap_host', 'imap_username', 'smtp_host'],
      }),
    ])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.chan.email')).click()
    })
    const body = document.getElementById('connDlgBody')!
    expect([...body.querySelectorAll('.sugsub')].map((g) => g.textContent)).toEqual([
      'gui.conn.g_imap',
      'gui.conn.g_smtp',
    ])
    const groups = [...body.querySelectorAll('.sugsub')].map(
      (g) => g.parentElement!.querySelectorAll('.sufield').length,
    )
    expect(groups).toEqual([2, 1])
  })

  it('leaves a channel that is one thing ungrouped', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    expect(document.querySelector('#connDlgBody .sugsub')).toBeNull()
  })

  /* A schema description is a label when it was written for a reader and a
     paragraph when it was written for a developer. The paragraph belongs on the
     tooltip: in the label position it is the standing grey the page was cleared
     of, printed under every optional box. */
  it('keeps a developer paragraph out of the label position', async () => {
    const prose =
      'Absolute path this channel reads and writes files in. Leave empty for the default under the agent home.'
    install([
      chan({
        fields: [
          { key: 'bot_token', required: true, set: true },
          { key: 'workspace', required: false, label: prose },
        ],
        missing: [],
      }),
    ])
    await mount()
    await act(async () => {
      ;(await screen.findByText('Slack')).click()
    })
    await act(async () => {
      ;(document.querySelector('#connDlgBody .sucap') as HTMLElement).click()
    })
    const label = document.querySelector('#connDlgBody .suadv .sufield label')!
    expect(label.textContent).toBe('workspace')
    expect(label.getAttribute('title')).toBe(prose)
  })

  /* A scan entry has no form, so `configured` is true of it from the start and
     the row offered to switch it on -- naming the mechanism instead of the
     errand that is about to happen. */
  /* One word for every way in, whatever the entrance needs behind it: a code, a
     form, or just the flag it already has. */
  it('offers the same way-in word to every addable entrance', async () => {
    install([
      chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [] }),
      chan({ fields: [{ key: 'bot_token', required: true, set: true }], missing: [] }),
      chan({ id: 'telegram', name: 'Telegram', fields: [{ key: 'token', required: true }], missing: ['token'] }),
    ])
    await mount()
    expect(rowActs('gui.chan.weixin')).toEqual(['gui.conn.connect'])
    expect(rowActs('Slack')).toEqual(['gui.conn.connect'])
    expect(rowActs('Telegram')).toEqual(['gui.conn.connect'])
  })

  /* Backing out of a scan is the same errand as leaving the list: the entrance
     goes out of service. It used to be its own word, "cancel connecting". */
  it('backs out of a scan with the same word the list uses', async () => {
    const { calls } = install([
      chan({ id: 'weixin', key: 'gui.chan.weixin', on: true, qrLogin: true, running: true, connected: false }),
    ])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.chan.weixin')).click()
    })
    expect(cardFoot().querySelector('button')!.textContent).toBe('gui.conn.disconnect')
    await act(async () => {
      ;(cardFoot().querySelector('button') as HTMLElement).click()
    })
    expect(calls).toContainEqual(['apply', { id: 'weixin', patch: {}, enable: false }])
  })

  /* Signing in by phone is a sequence, and a channel merely switched off used
     to present an empty form: no code, no way to ask for one, nothing said. */
  it('walks a scan channel through its three steps instead of an empty form', async () => {
    const { calls } = install([chan({ id: 'weixin', key: 'gui.chan.weixin', on: false, qrLogin: true, fields: [] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.chan.weixin')).click()
    })
    const body = document.getElementById('connDlgBody')!
    expect([...body.querySelectorAll('.suwiz .step .st')].map((n) => n.textContent)).toEqual([
      'gui.conn.w1_idle',
      'gui.conn.w2',
      'gui.conn.w3',
    ])
    expect(wizStates()).toEqual(['idle', 'idle', 'idle'])
    expect(body.querySelector('.sufield')).toBeNull()
    /* The list's verb, not a third one: it read "turn the entry on", which is
       also what step 1 above it says. */
    expect(cardFoot().querySelector('button')!.textContent).toBe('gui.conn.connect')
    await act(async () => {
      ;(cardFoot().querySelector('button') as HTMLElement).click()
    })
    expect(calls).toContainEqual(['apply', { id: 'weixin', patch: {}, enable: true }])
  })

  /* The adapter belongs to the app process, which builds its channel set at
     launch -- so turning the entry on is not what produces a code, and the
     step says what is actually left rather than spinning. */
  it('names what is still stopping the code, when the entry is on and its adapter is not', async () => {
    install([chan({ id: 'weixin', key: 'gui.chan.weixin', on: true, running: false, qrLogin: true, fields: [] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.chan.weixin')).click()
    })
    const body = document.getElementById('connDlgBody')!
    expect([...body.querySelectorAll('.suwiz .step .st')].map((n) => n.textContent)).toEqual([
      'gui.conn.w1_done',
      'gui.conn.w2',
      'gui.conn.w3',
    ])
    expect(wizStates()).toEqual(['done', 'idle', 'idle'])
    expect(body.querySelector('.suwiz .sd')!.textContent).toBe('gui.conn.w2_blocked')
    expect(body.querySelector('.qrbox')).toBeNull()
  })

  /* The gateway reports nothing when it cannot be asked, and the reader who
     just turned the entry on is owed the same sentence either way. */
  it('names it too when the gateway could not be asked at all', async () => {
    install([chan({ id: 'weixin', key: 'gui.chan.weixin', on: true, qrLogin: true, fields: [] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.chan.weixin')).click()
    })
    const body = document.getElementById('connDlgBody')!
    expect(body.querySelector('.suwiz .sd')!.textContent).toBe('gui.conn.w2_blocked')
    expect(wizStates()).toEqual(['done', 'idle', 'idle'])
  })

  it('drops the wizard once the entry is paired', async () => {
    install([chan({ id: 'weixin', key: 'gui.chan.weixin', on: true, running: true, connected: true, qrLogin: true, fields: [] })])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.chan.weixin')).click()
    })
    const body = document.getElementById('connDlgBody')!
    expect(body.querySelector('.suwiz')).toBeNull()
    expect(body.querySelector('.sustate')!.textContent).toContain('gui.conn.st_live')
  })
})
