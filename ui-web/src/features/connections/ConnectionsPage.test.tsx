// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as detail from '../../state/detail'
import * as lang from '../../state/lang'
import { resetSources, setSources } from '../../state/sources'
import { domSnapshot } from '../../test/domSnapshot'
import { CHANNELS } from './catalogue'
import { ConnectionsApp } from './ConnectionsPage'
import * as store from './store'

import type { ConnChannel, ConnQr, ConnectionsSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* Slack's way in is a token, and the fixture has to say so: an entry with no
   required field is a scan-login entry by derivation, so a fieldless default
   would quietly make the standard row a different kind of channel than the
   tests that use it mean. The stand-in translator hands a key back as its own
   text, so a row keyed `Slack` reads as Slack. */
function chan(over: Partial<ConnChannel> = {}): ConnChannel {
  return {
    id: 'slack',
    key: 'Slack',
    on: false,
    fields: [{ key: 'bot_token', required: true, set: true }],
    missing: [],
    ...over,
  }
}

/* The island runs against the same two seams production wires up: a stand-in
   translator on setTranslator (it returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on sources.connections. */
function install(rows: ConnChannel[], over: Partial<ConnectionsSource> = {}) {
  const calls: Array<[string, unknown]> = []
  const source: ConnectionsSource = {
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
      return true
    },
    qr: async () => null,
    ...over,
  }
  const shellCalls: Array<[string, unknown]> = []
  setTranslator((key, vars, fallback) =>
  key.startsWith('gui.connf.') ? (fallback ?? key) : vars ? `${key} ${JSON.stringify(vars)}` : key)
  vi.spyOn(confirmStore, 'ask').mockImplementation((title, _b, _l, fn) => {
  shellCalls.push(['confirmAsk', title])
  fn()
  })
  setSources({ connections: source })
  document.body.innerHTML =
    '<div id="connectionsBody"></div><div id="menu" data-open="false"></div>' +
    '<aside class="detail" id="detail" data-open="false"><div class="body" id="dBody"></div></aside>'
  return { source, calls, shellCalls }
}

/* Types into a box the way a reader does: the value, then the event. The pane's
   connect is unavailable until every required box has something in it, and a
   value assigned straight onto the node fires nothing and tells the form
   nothing -- so a test that only assigns is testing a form that never saw the
   credential. */
function typeInto(box: HTMLInputElement, value: string): void {
  box.value = value
  box.dispatchEvent(new Event('input', { bubbles: true }))
}

/* The page as the rail opens it: the island in its own box, and the fetch
   opening it costs (features/connections/store.ts's `open`, minus the page
   switch, which is not what this file is about). */
async function mount() {
  const view = render(<ConnectionsApp />, { container: document.getElementById('connectionsBody')! })
  await act(async () => {
    await store.refresh(true)
  })
  return view
}

/* The page, and the sheet the shared drawer holds. Every card query is scoped
   to the page, because the picked channel carries the same name in its head. */
const side = (): HTMLElement => document.getElementById('connectionsBody')!
const main = (): HTMLElement => store.detailHost()
const sheetUp = (): boolean => detail.get().open
const rowsOf = (): HTMLElement[] => [...side().querySelectorAll<HTMLElement>('.su-card:not(.su-wcard)')]
const rowNamed = (name: string): HTMLElement =>
  rowsOf().find((r) => r.querySelector('.su-t')!.textContent === name)!
const tab = (key: string): HTMLElement =>
  [...side().querySelectorAll<HTMLElement>('.su-tab')].find((b) => b.firstChild?.textContent === key)!
/* Which of the two groups a card is in: whether the Connected tab shows it. */
const groupOf = (name: string): string => {
  act(() => tab('gui.conn.tab_on').click())
  const on = !!rowNamed(name)
  act(() => tab('gui.conn.tab_all').click())
  return on ? 'gui.conn.g_on' : 'gui.conn.g_off'
}
const countOf = (key: string): string => tab(key).querySelector('.su-tn')!.textContent!
/* What a card says about itself: a reason where one is owed, else the line
   under its name. */
const subOf = (name: string): string | null => {
  const foot = rowNamed(name).querySelector('.su-foot')!
  return foot.classList.contains('su-foot-quiet') ? rowNamed(name).querySelector('.su-one')!.textContent : foot.textContent
}
const footOf = (name: string): string => rowNamed(name).querySelector('.su-foot')!.textContent!
const lineOf = (name: string): string => rowNamed(name).querySelector('.su-one')!.textContent!
const ledOf = (name: string): string => rowNamed(name).querySelector('.su-nm > span:last-child:not(.su-t)')?.className ?? ''
/* Opening a channel is pressing its card. */
const openRow = (name: string): void => { rowNamed(name).click() }
/* The steps' own state, which is what draws the tick and the current mark --
   the titles alone read the same whether or not the sequence advances. */
const wizStates = (): Array<string | null> =>
  [...main().querySelectorAll('.suwiz .step')].map((s) => s.getAttribute('data-state'))
/* The sheet's head and the row its verb sits on. */
const paneHead = (): HTMLElement => main().querySelector('.su-head')!
const paneFoot = (): HTMLElement => main().querySelector('.sufoot')!

/* The drawer is closed, not reset: a close schedules the card's drop for after
   the fade, and that drop asks whether the drawer was opened since by its open
   count. Resetting the count to zero let the next case's first open read as
   the same open, and the last case's drop emptied its sheet mid-test. */
afterEach(() => {
  act(() => detail.close())
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

describe('connections island', () => {
  it('waits as the cards it becomes rather than as an empty grid', async () => {
    /* `!loaded && !rows.length` used to draw nothing, so the seconds before the
       adapters answered looked exactly like "there are no channels". */
    let land: ((r: ConnChannel[]) => void) | null = null
    install([], { rows: () => new Promise((resolve) => { land = resolve }) })
    render(<ConnectionsApp />, { container: document.getElementById('connectionsBody')! })
    await act(async () => { void store.refresh(true); await Promise.resolve() })
    const wait = side().querySelector('.su-grid[aria-busy="true"]')!
    expect(wait.querySelectorAll('.su-wcard').length).toBe(CHANNELS.length)

    await act(async () => { land!([chan()]); await Promise.resolve() })
    expect(side().querySelector('[aria-busy="true"]')).toBeNull()
    expect(rowNamed('Slack')).toBeTruthy()
  })

  it('lists every catalogue row, grouped by whether it is in service', async () => {
    install([
      chan({ on: true, running: true }),
      chan({ id: 'telegram', key: 'Telegram' }),
      chan({ id: 'email', key: 'gui.chan.email' }),
    ])
    await mount()
    expect(await screen.findByText('Slack')).toBeTruthy()
    expect(screen.getByText('Telegram')).toBeTruthy()
    expect(screen.getByText('gui.chan.email')).toBeTruthy()
    expect(groupOf('Slack')).toBe('gui.conn.g_on')
    expect(groupOf('Telegram')).toBe('gui.conn.g_off')
  })

  /* The addable group is ordered by what it costs to get in, so the quickest
     way to a working entry is the first row of it rather than wherever the
     catalogue happened to put it. */
  it('puts the cheapest way in first, and says what each costs', async () => {
    install([
      chan({ id: 'email', key: 'gui.chan.email', fields: [
        { key: 'imap_host', required: true }, { key: 'imap_user', required: true },
        { key: 'smtp_host', required: true }, { key: 'smtp_user', required: true },
      ], missing: ['imap_host', 'imap_user', 'smtp_host', 'smtp_user'] }),
      chan({ id: 'telegram', key: 'Telegram', fields: [{ key: 'token', required: true }], missing: ['token'] }),
      chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true }),
    ])
    await mount()
    expect(rowsOf().map((r) => r.querySelector('.su-t')!.textContent))
      .toEqual(['gui.chan.weixin', 'Telegram', 'gui.chan.email'])
    expect(subOf('gui.chan.weixin')).toBe('gui.conn.line_scan')
    expect(subOf('Telegram')).toBe('gui.conn.line_creds {"n":"1"}')
    expect(subOf('gui.chan.email')).toBe('gui.conn.line_creds {"n":"4"}')
  })

  /* Whether a channel signs in by scanning is a static fact about it, but the
     gateway only reports `qr_login` for an adapter that is already up -- so an
     entry nobody has switched on yet, which is the whole of the addable group,
     never carries the flag. The schema is the honest source: no required field
     means no form to fill, and the only way in is signing in. */
  it('reads a scan-login entry off its schema, not off the live flag', async () => {
    install([
      chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: false, fields: [{ key: 'route_tag' }] }),
      chan({ id: 'telegram', key: 'Telegram', fields: [{ key: 'token', required: true }] }),
    ])
    await mount()
    expect(await screen.findByText('gui.chan.weixin')).toBeTruthy()
    expect(subOf('gui.chan.weixin')).toBe('gui.conn.line_scan')
    /* And it sorts ahead of the cheapest form. */
    expect(rowsOf().map((r) => r.querySelector('.su-t')!.textContent)).toEqual(['gui.chan.weixin', 'Telegram'])
  })

  /* The row's second line, which is the whole of what a row says about itself:
     a reason where something went wrong, then the state, and otherwise what it
     costs to get in. The colour is the reading, and the five states are not
     two -- "the gateway could not be asked" is not "off", and "running but not
     paired" is not "receiving". */
  describe('what the row says about its own state', () => {
    /* Five states underneath, three said: connected, not yet, and set up but
       failed. Waiting on a code is not yet; nobody to ask is the page's
       notice, not a card's. */
    it('says three things for the five states underneath', async () => {
      install([
        chan({ id: 'a', key: 'A', on: true, running: true, connected: true, who: 'me' }),
        chan({ id: 'b', key: 'B', on: true, running: false }),
        chan({ id: 'c', key: 'C', on: true, running: true, connected: false, qrLogin: true }),
        chan({ id: 'd', key: 'D', on: true }),
        chan({ id: 'e', key: 'E', on: false }),
      ], { hostRunning: () => true })
      await mount()
      expect([footOf('A'), ledOf('A'), lineOf('A')]).toEqual(['gui.conn.foot_on', 'su-led', 'gui.conn.as_you {"who":"me"}'])
      expect([footOf('B'), ledOf('B'), lineOf('B')]).toEqual(['gui.conn.foot_bad', 'su-led su-led-bad', 'gui.conn.line_bad_creds'])
      for (const quiet of ['C', 'D', 'E']) {
        expect([footOf(quiet), ledOf(quiet)], quiet).toEqual(['gui.conn.foot_off', ''])
      }
      /* Configured: switched off says so, switched on does not claim it is off. */
      expect(lineOf('E')).toBe('gui.conn.line_ready')
      expect(lineOf('D')).toBe('gui.conn.line_saved')
    })

    /* The sheet carries the gateway's reason, where there is one. */
    it('names the reason in the sheet of an entrance that would not start', async () => {
      install([chan({ on: true, running: false, refusal: 'gui.conn.out_bad_config' })], { hostRunning: () => true })
      await mount()
      await act(async () => { openRow('Slack') })
      expect(paneHead().querySelector('.su-by')!.textContent).toBe('gui.conn.st_bad_why {"why":"gui.conn.out_bad_config"}')
    })

    it('asks a scan entrance that would not start to sign in again', async () => {
      install([chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [], on: true, running: false })], {
        hostRunning: () => true,
      })
      await mount()
      expect(lineOf('gui.chan.weixin')).toBe('gui.conn.line_bad_scan')
    })

    /* The one card asking for something leads the rest. */
    it('puts a failed entrance ahead of the ones still to connect', async () => {
      install([
        chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [] }),
        chan({ on: true, running: false }),
      ], { hostRunning: () => true })
      await mount()
      expect(rowsOf().map((r) => r.querySelector('.su-t')!.textContent)).toEqual(['Slack', 'gui.chan.weixin'])
    })

    it('says how many credentials are still missing, where some are', async () => {
      install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
      await mount()
      expect(subOf('Slack')).toBe('gui.conn.line_creds {"n":"1"}')
    })
  })

  /* The heart of it: pressing the button is a decision, not an arrival. An
     entrance reaches "in service" by receiving, and nothing else -- a made-up
     token used to look exactly like a working one, because the flag was the
     whole test. */
  describe('what counts as being in service', () => {
    it('leaves a switched-on entrance where it was when nothing started', async () => {
      install([chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [] })])
      await mount()
      expect(groupOf('gui.chan.weixin')).toBe('gui.conn.g_off')
      await act(async () => { openRow('gui.chan.weixin') })
      /* The pane's own button is the write, and it changes nothing out here:
         nothing started, so nothing is in service. */
      await act(async () => {
        ;(paneFoot().querySelector('button') as HTMLElement).click()
      })
      expect(groupOf('gui.chan.weixin')).toBe('gui.conn.g_off')
      expect(footOf('gui.chan.weixin')).toBe('gui.conn.foot_off')
    })

    /* And no group in between. An adapter up and waiting on a code is not in
       service, and a heading of its own for that middle moment is a state
       nobody can act on -- signing in happens in the pane, which is open while
       it happens. */
    it('keeps an entrance whose code is up out of a group of its own', async () => {
      install([
        chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [], on: true, running: true, connected: false }),
      ])
      await mount()
      expect(groupOf('gui.chan.weixin')).toBe('gui.conn.g_off')
      expect(countOf('gui.conn.tab_on')).toBe('0')
    })

    it('keeps an entrance with made-up credentials out of in service', async () => {
      /* The adapter refused to start, which is all a bad token looks like from
         here. */
      install([chan({ on: true, running: false, fields: [{ key: 'bot_token', required: true, set: true }] })])
      await mount()
      expect(groupOf('Slack')).toBe('gui.conn.g_off')
      expect(footOf('Slack')).toBe('gui.conn.foot_bad')
    })

    it('keeps a scan entrance out of in service until it is paired', async () => {
      install([chan({ id: 'weixin', key: 'gui.chan.weixin', on: true, running: true, connected: false, qrLogin: true })])
      await mount()
      expect(groupOf('gui.chan.weixin')).toBe('gui.conn.g_off')
    })

    it('promotes it the moment the adapter reports receiving', async () => {
      install([chan({ on: true, running: true, connected: true })])
      await mount()
      expect(groupOf('Slack')).toBe('gui.conn.g_on')
    })

    /* The pane used to close on the press, which is what made a rejected
       credential indistinguishable from an accepted one. */
    it('keeps the form up after the press, with the state and a retry', async () => {
      install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
      await mount()
      await act(async () => { openRow('Slack') })
      await act(async () => {
        typeInto(main().querySelector<HTMLInputElement>('#connDlgBody input')!, 'made-up')
      })
      await act(async () => {
        ;(paneFoot().querySelector('button.key') as HTMLElement).click()
      })
      expect(main().querySelector('#connDlgBody')).toBeTruthy()
      expect(paneFoot().querySelector('button.key')!.textContent).toBe('gui.conn.retry')
    })

    it('hands the column back to the list once the entrance is receiving', async () => {
      const rows = [chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })]
      install(rows, {
        /* What the gateway answers when the credentials were good. */
        apply: async (c) => {
          c.on = true
          c.running = true
          c.connected = true
          return true
        },
      })
      await mount()
      await act(async () => { openRow('Slack') })
      await act(async () => {
        typeInto(main().querySelector<HTMLInputElement>('#connDlgBody input')!, 'a-real-token')
      })
      await act(async () => {
        ;(paneFoot().querySelector('button.key') as HTMLElement).click()
      })
      expect(sheetUp()).toBe(false)
      expect(groupOf('Slack')).toBe('gui.conn.g_on')
    })

    /* The card a reader opens to correct a rotated token is open on an entrance
       that is already receiving, and "receiving" was the whole guard: the press
       closed the card on a state that had been true before it, reporting an
       answer nothing had given. The answer is the write's completion. */
    it('does not close the form on the press, before the write has been answered', async () => {
      const slack = chan({ on: true, running: true, connected: true })
      let written!: () => void
      const writing = new Promise<void>((resolve) => { written = resolve })
      install([slack], { apply: async () => { await writing; return true } })
      await mount()
      await act(async () => { openRow('Slack') })
      await act(async () => {
        typeInto(main().querySelector<HTMLInputElement>('#connDlgBody input')!, 'rotated-token')
      })
      await act(async () => {
        ;(paneFoot().querySelector('button.key') as HTMLElement).click()
      })
      expect(main().querySelector('#connDlgBody')).toBeTruthy()

      await act(async () => {
        written()
        await new Promise((resolve) => setTimeout(resolve, 0))
      })
      expect(sheetUp()).toBe(false)
    })

    /* A rebuild that finishes before the status is re-read shows no down state
       in between: live before, live after. The form still has to close, so the
       write's completion, not a transition, is what it keys on. */
    it('closes the form once the correction is applied and the entrance reads live', async () => {
      const slack = chan({ on: true, running: true, connected: true })
      install([slack], { apply: async () => true })
      await mount()
      await act(async () => { openRow('Slack') })
      await act(async () => {
        typeInto(main().querySelector<HTMLInputElement>('#connDlgBody input')!, 'rotated-token')
      })
      await act(async () => {
        ;(paneFoot().querySelector('button.key') as HTMLElement).click()
        await new Promise((resolve) => setTimeout(resolve, 0))
      })
      expect(sheetUp()).toBe(false)
    })

    /* And the reason the card exists: a correction the adapter could not start
       on leaves the reader with the state line, not with a closed card. */
    it('keeps the form up when the applied correction leaves the entrance down', async () => {
      const slack = chan({ on: true, running: true, connected: true })
      install([slack], {
        apply: async (c) => {
          c.running = false
          c.connected = false
          return true
        },
      })
      await mount()
      await act(async () => { openRow('Slack') })
      await act(async () => {
        typeInto(main().querySelector<HTMLInputElement>('#connDlgBody input')!, 'rotated-token')
      })
      await act(async () => {
        ;(paneFoot().querySelector('button.key') as HTMLElement).click()
        await new Promise((resolve) => setTimeout(resolve, 0))
      })
      expect(main().querySelector('#connDlgBody')).toBeTruthy()
    })

    /* A write the gateway refused (validation, an unreachable rpc) leaves the row
       exactly as it was, live included. Closing on that would report the old
       state as the answer to a save that never landed. */
    it('keeps the form up when the write itself was refused on a live entrance', async () => {
      const slack = chan({ on: true, running: true, connected: true })
      install([slack], { apply: async () => false })
      await mount()
      await act(async () => { openRow('Slack') })
      await act(async () => {
        typeInto(main().querySelector<HTMLInputElement>('#connDlgBody input')!, 'rotated-token')
      })
      await act(async () => {
        ;(paneFoot().querySelector('button.key') as HTMLElement).click()
        await new Promise((resolve) => setTimeout(resolve, 0))
      })
      expect(main().querySelector('#connDlgBody')).toBeTruthy()
    })
  })

  /* What the page knows before the reader presses anything: whether there is a
     host at all. With none, connecting starts no adapter and mints no code, and
     saying that up front is what keeps the press from being the way to find
     out. */
  describe('when nothing is running that could host an adapter', () => {
    /* Said once for the page, and no card claims it: every entrance is deaf
       for the same reason, including one whose adapter reads as down. */
    it('says it once over the grid, and leaves every card quiet', async () => {
      install([chan({ on: true }), chan({ id: 'telegram', key: 'Telegram', on: true, running: false })], {
        hostRunning: () => false,
      })
      await mount()
      expect(side().querySelector('.su-notice')!.textContent).toBe('gui.conn.host_down')
      expect([footOf('Slack'), footOf('Telegram')]).toEqual(['gui.conn.foot_off', 'gui.conn.foot_off'])
    })

    /* Where the source cannot say, it claims nothing either way. */
    it('draws no notice when the source cannot tell', async () => {
      install([chan({ on: true })])
      await mount()
      expect(side().querySelector('.su-notice')).toBeNull()
      expect(footOf('Slack')).toBe('gui.conn.foot_off')
    })

    it('tells a scan pane there is no code coming before the press, not after', async () => {
      install([chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [] })], {
        hostRunning: () => false,
      })
      await mount()
      await act(async () => { openRow('gui.chan.weixin') })
      const steps = [...main().querySelectorAll('.suwiz .step')]
      expect(steps[1]!.querySelector('.sd')!.textContent).toBe('gui.conn.w2_blocked')
      /* Unpressed: the write has not happened, and the pane said so anyway.
         Pressing connect is still the step the reader is on. */
      expect(steps[0]!.getAttribute('data-state')).toBe('now')
    })

    /* One sentence for two states was wrong in the commoner one: "Raven is not
       running" over a page the gateway itself was serving. */
    it('separates an adapter that gave up from nothing running at all', async () => {
      const { calls } = install(
        [chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [], on: true, running: false })],
        { hostRunning: () => true },
      )
      await mount()
      await act(async () => { openRow('gui.chan.weixin') })
      const steps = [...main().querySelectorAll('.suwiz .step')]
      expect(steps[1]!.querySelector('.sd')!.textContent).toBe('gui.conn.w2_down')
      const retry = [...paneFoot().querySelectorAll('button')].find((b) => b.textContent === 'gui.conn.w_retry')
      expect(retry, 'the pane offers a retry').toBeTruthy()
      await act(async () => {
        ;(retry as HTMLElement).click()
      })
      expect(calls).toContainEqual(['apply', { id: 'weixin', patch: {}, enable: true }])
    })

    /* A code being waited on is not a failure: nothing to retry, and the panel
       is what the reader is looking at. */
    it('offers no retry while a code is actually up', async () => {
      install([
        chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [], on: true, running: true, connected: false }),
      ])
      await mount()
      await act(async () => { openRow('gui.chan.weixin') })
      expect([...paneFoot().querySelectorAll('button')].map((b) => b.textContent)).toEqual(['gui.conn.disconnect'])
    })

    it('says nothing of the kind while the source has not answered', async () => {
      install([chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [] })])
      await mount()
      await act(async () => { openRow('gui.chan.weixin') })
      const steps = [...main().querySelectorAll('.suwiz .step')]
      expect(steps[1]!.querySelector('.sd')).toBeNull()
    })
  })


  /* The page-level fact the write can change too: `host` was written only on
     section entry, so a gateway that came up since then left the pane telling
     the reader nothing was running it. */
  it('re-reads whether anything hosts an adapter after a write', async () => {
    let up = false
    install([chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [], on: true, running: false })], {
      hostRunning: () => up,
      apply: async () => {
        up = true
        return true
      },
    })
    await mount()
    await act(async () => { openRow('gui.chan.weixin') })
    expect(main().querySelectorAll('.suwiz .step')[1]!.querySelector('.sd')!.textContent)
      .toBe('gui.conn.w2_blocked')
    await act(async () => {
      ;[...paneFoot().querySelectorAll('button')].find((b) => b.textContent === 'gui.conn.w_retry')!.click()
    })
    expect(main().querySelectorAll('.suwiz .step')[1]!.querySelector('.sd')!.textContent)
      .toBe('gui.conn.w2_down')
  })

  /* The row's press opens the pane, and does nothing else. It briefly did the
     write itself, so a press was an attempt rather than a form -- but joining is
     scanning a code or handing over a credential, both of which live in the
     pane, so a press out here could only ever be half the errand. */
  it('opens the pane from the row and attempts nothing on the way', async () => {
    const { calls } = install([
      chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [] }),
      chan({ fields: [{ key: 'bot_token', required: true, set: true }], missing: [] }),
    ])
    await mount()
    expect(sheetUp()).toBe(false)
    await act(async () => { openRow('gui.chan.weixin') })
    expect(paneHead().querySelector('.su-meta h3')!.textContent).toBe('gui.chan.weixin')
    expect(calls.filter((x) => x[0] === 'apply' || x[0] === 'toggle')).toEqual([])
    /* Including the entrance that has everything it needs: no write, no start,
       nothing claimed. */
    await act(async () => { openRow('Slack') })
    expect(paneHead().querySelector('.su-meta h3')!.textContent).toBe('Slack')
    expect(calls.filter((x) => x[0] === 'apply' || x[0] === 'toggle')).toEqual([])
  })

  /* "Connect" is unavailable until there is something to connect WITH. Pressing
     it with an empty box wrote nothing, started nothing and left the pane
     exactly as it was -- the press was the only feedback and it meant nothing. */
  describe('a credential that has to actually exist', () => {
    const key = (): HTMLButtonElement => paneFoot().querySelector('button.key')!

    it('is unavailable while a required box is empty, and available once it is not', async () => {
      const { calls } = install([
        chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] }),
      ])
      await mount()
      await act(async () => { openRow('Slack') })
      expect(key().hasAttribute('disabled')).toBe(true)
      /* And pressing it does nothing, which is the point of the attribute. */
      await act(async () => {
        key().click()
      })
      expect(calls.filter((x) => x[0] === 'apply')).toEqual([])

      await act(async () => {
        typeInto(main().querySelector<HTMLInputElement>('#connDlgBody input')!, 'tok')
      })
      expect(key().hasAttribute('disabled')).toBe(false)
      await act(async () => {
        key().click()
      })
      expect(calls).toContainEqual(['apply', { id: 'slack', patch: { bot_token: 'tok' }, enable: true }])
    })

    /* Whitespace is not a credential. */
    it('does not count a box with nothing but spaces in it', async () => {
      install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
      await mount()
      await act(async () => { openRow('Slack') })
      await act(async () => {
        typeInto(main().querySelector<HTMLInputElement>('#connDlgBody input')!, '   ')
      })
      expect(key().hasAttribute('disabled')).toBe(true)
    })

    /* Emptied again is empty again: the check is recomputed, not latched. */
    it('goes back to unavailable when the box is cleared', async () => {
      install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
      await mount()
      await act(async () => { openRow('Slack') })
      const box = main().querySelector<HTMLInputElement>('#connDlgBody input')!
      await act(async () => {
        typeInto(box, 'tok')
      })
      expect(key().hasAttribute('disabled')).toBe(false)
      await act(async () => {
        typeInto(box, '')
      })
      expect(key().hasAttribute('disabled')).toBe(true)
    })

    /* Credentials already on file are credentials: an entrance configured last
       week is pressable the moment its pane opens, with every box left blank
       (blank means "keep", never "erase"). */
    it('counts a credential the config already holds', async () => {
      const { calls } = install([
        chan({ fields: [{ key: 'bot_token', required: true, set: true }], missing: [] }),
      ])
      await mount()
      await act(async () => { openRow('Slack') })
      expect(key().hasAttribute('disabled')).toBe(false)
      await act(async () => {
        key().click()
      })
      expect(calls).toContainEqual(['apply', { id: 'slack', patch: {}, enable: true }])
    })

    /* A scan entrance has no credential and no form: its wizard's button is not
       this one, and gating it on an empty field list would have made signing in
       impossible. */
    it('leaves a scan entrance pressable, having nothing to fill', async () => {
      install([chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [] })])
      await mount()
      await act(async () => { openRow('gui.chan.weixin') })
      expect((paneFoot().querySelector('button') as HTMLButtonElement).hasAttribute('disabled')).toBe(false)
    })
  })

  /* Which would leave a switch nobody can take back, so the pane carries it:
     the row's control is about what the row is, the pane has room to say what
     the button does. */
  it('keeps a way back to off in the pane of an entrance that never started', async () => {
    const { calls } = install([chan({ on: true, running: false, fields: [{ key: 'bot_token', required: true, set: true }] })])
    await mount()
    await act(async () => { openRow('Slack') })
    const off = [...paneFoot().querySelectorAll('button')].find((b) => b.textContent === 'gui.conn.disconnect')
    expect(off, 'the pane offers disconnect').toBeTruthy()
    await act(async () => {
      ;(off as HTMLElement).click()
    })
    expect(calls).toContainEqual(['apply', { id: 'slack', patch: {}, enable: false }])
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
    await act(async () => { openRow('Slack') })
    const fold = screen.getByText('gui.conn.advanced {"n":1}')
    expect(fold.getAttribute('aria-expanded')).toBe('false')
    /* Closed means gone, not hidden: left in the tree it still took a row of
       the body's grid, which is a gap under the fold with nothing in it. */
    expect(main().querySelector('#connDlgBody .suadv .sufield')).toBeNull()
    const body = main().querySelector('#connDlgBody')!
    const token = body.querySelector<HTMLInputElement>('input[type="password"]')!
    await act(async () => {
      typeInto(token, '  tok-1  ')
    })
    await act(async () => {
      ;(paneFoot().querySelector('button.key') as HTMLElement).click()
    })
    expect(calls).toContainEqual(['apply', { id: 'slack', patch: { bot_token: 'tok-1' }, enable: true }])
  })

  /* Where the credentials come from, for the channels that have one place to
     get them. A mail host has no open platform to link to, so it has none. */
  it('links to the console that issues the credentials, where there is one', async () => {
    install([chan({ id: 'telegram', key: 'Telegram', fields: [{ key: 'token', required: true }], missing: ['token'] })])
    await mount()
    await act(async () => { openRow('Telegram') })
    const jump = main().querySelector<HTMLAnchorElement>('#connDlgBody a.su-guide')!
    expect(jump.href).toBe('https://t.me/BotFather')
    expect(jump.target).toBe('_blank')
    /* The name sits in the sentence with its own spaces only in a Chinese one;
       the stand-in language here is not, so it is passed as it is. */
    expect(jump.querySelector('.su-guide-t')!.textContent).toBe('gui.conn.guide {"name":"Telegram"}')
  })

  /* A Latin name inside a Chinese sentence takes a space either side; a
     Chinese name takes none. The Chinese name is escaped: source added in a PR stays
     ASCII (scripts/check_source_language.py). */
  it('spaces a Latin name inside a Chinese sentence, and only there', async () => {
    const FEISHU = '\u98de\u4e66'
    install([
      chan({ id: 'telegram', key: 'Telegram', fields: [{ key: 'token', required: true }], missing: ['token'] }),
      chan({ id: 'feishu', key: FEISHU, fields: [{ key: 'app_id', required: true }], missing: ['app_id'] }),
    ])
    act(() => lang.set('zh'))
    try {
      await mount()
      await act(async () => { openRow('Telegram') })
      expect(main().querySelector('.su-guide-t')!.textContent).toBe('gui.conn.guide {"name":" Telegram "}')
      await act(async () => { openRow(FEISHU) })
      expect(main().querySelector('.su-guide-t')!.textContent).toBe(`gui.conn.guide {"name":"${FEISHU}"}`)
    } finally {
      act(() => lang._resetForTests())
    }
  })

  it('has no link for a channel whose credentials are not issued anywhere', async () => {
    install([chan({ id: 'email', key: 'gui.chan.email', fields: [{ key: 'imap_host', required: true }], missing: ['imap_host'] })])
    await mount()
    await act(async () => { openRow('gui.chan.email') })
    expect(main().querySelector('#connDlgBody a.jump')).toBeNull()
  })

  /* The raw config key was printed as a second grey line under every box,
     saying the same thing in worse words. It rides on the label's tooltip now,
     so it is still reachable when a support answer names the key. */
  it('keeps the raw field key off the form and on the label', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true, label: 'Bot token' }], missing: ['bot_token'] })])
    await mount()
    await act(async () => { openRow('Slack') })
    const body = main().querySelector('#connDlgBody')!
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
    await act(async () => { openRow('Slack') })
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

  /* A login the adapter gave up on leaves its last code pending, and the row
     behind this panel goes on saying the entrance is up until the list is
     re-read -- so the panel is the last place that can catch a dead code, and
     the only one still on screen to offer the retry. */
  it('drops a code the adapter is no longer behind and carries the retry itself', async () => {
    vi.useFakeTimers()
    const answers: ConnQr[] = [
      { connected: false, running: true, qr: 'data:image/png;base64,QQ==' },
      { connected: false, running: false, qr: 'data:image/png;base64,QQ==' },
    ]
    let polls = 0
    const { calls, source } = install(
      [
        chan({
          id: 'weixin',
          key: 'gui.chan.weixin',
          fields: [],
          on: true,
          qrLogin: true,
          running: true,
          connected: false,
        }),
      ],
      { hostRunning: () => true, qr: async () => answers[Math.min(polls++, answers.length - 1)]! },
    )
    await mount()
    await act(async () => { openRow('gui.chan.weixin') })
    expect(document.querySelector('.qrshot img')).toBeTruthy()

    const rowsSpy = vi.spyOn(source, 'rows')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(document.querySelector('.qrshot img')).toBeNull()
    expect(screen.getByText('gui.conn.w2_down')).toBeTruthy()
    /* And the row the reader closes this on is asked for again, so what the
       list says about the entrance stops contradicting the panel. */
    expect(rowsSpy.mock.calls.length).toBeGreaterThan(0)

    const retry = [...document.querySelectorAll<HTMLButtonElement>('.qrbox button')].find(
      (b) => b.textContent === 'gui.conn.w_retry',
    )!
    expect(retry, 'the panel offers a retry').toBeTruthy()
    await act(async () => { retry.click() })
    expect(calls).toContainEqual(['apply', { id: 'weixin', patch: {}, enable: true }])
    expect(screen.getByText('gui.conn.qr_wait')).toBeTruthy()
    vi.useRealTimers()
  })

  it('keeps the scan panel off an unpaired channel that is switched off', async () => {
    install([chan({ on: false, qrLogin: true })])
    await mount()
    await act(async () => { openRow('Slack') })
    expect(document.querySelector('.qrbox')).toBeNull()
  })

  /* Nothing connected yet is the ordinary first run: the tab still stands,
     and says so in its own words rather than as an empty grid. */
  it('says so on the Connected tab while nothing is connected', async () => {
    install([chan({ missing: ['bot_token'] })])
    await mount()
    expect(countOf('gui.conn.tab_on')).toBe('0')
    act(() => tab('gui.conn.tab_on').click())
    expect(rowsOf()).toEqual([])
    expect(side().querySelector('.su-empty')!.textContent).toBe('gui.conn.none_on')
  })

  /* One fact, once. The head counts what is missing, so the body carries no
     second "credentials 0 / 1" caption over the same boxes. */
  it('counts what is missing in the head alone', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
    await mount()
    await act(async () => { openRow('Slack') })
    expect(paneHead().querySelector('.su-by')!.textContent).toBe('gui.conn.st_missing {"n":1}')
    expect(main().querySelector('#connDlgBody .sucreds')).toBeNull()
  })

  /* "Set, leave blank to keep" is said once for the form, not in every box. */
  it('says once that a saved box left blank is kept', async () => {
    install([chan({ on: true, running: true, fields: [
      { key: 'bot_token', required: true, set: true }, { key: 'app_token', required: true, set: true },
    ] })])
    await mount()
    await act(async () => { openRow('Slack') })
    expect(main().querySelectorAll('.su-keep')).toHaveLength(1)
    /* A connected entrance is here to rotate a secret, not to learn where
       secrets come from. */
    expect(main().querySelector('.su-guide')).toBeNull()
    expect([...main().querySelectorAll('input')].map((i) => i.placeholder)).toEqual(['gui.conn.field_set', 'gui.conn.field_set'])
  })

  /* Backing out and connecting sit at the two ends of the foot. */
  it('keeps disconnect apart from the verb that connects', async () => {
    install([chan({ on: true, running: true })])
    await mount()
    await act(async () => { openRow('Slack') })
    const kids = [...paneFoot().children]
    expect(kids[0]!.textContent).toBe('gui.conn.disconnect')
    expect(kids.at(-1)!.className).toContain('key')
  })

  /* Scanning is the one way in the body does not spell out: the wizard has no
     credential caption to carry it. */
  it('says so in the header where nothing else does', async () => {
    install([chan({ id: 'weixin', key: 'gui.chan.weixin', qrLogin: true, fields: [] })])
    await mount()
    await act(async () => { openRow('gui.chan.weixin') })
    expect(paneHead().querySelector('.su-by')!.textContent).toBe('gui.conn.cost_scan_line')
  })

  /* One fact, once, the other way round: what the row's own second line says is
     what the header says, so the form under it does not repeat it. */
  it('states the entrance once, in the header', async () => {
    install([chan({ on: true, running: true })])
    await mount()
    await act(async () => { openRow('Slack') })
    expect(paneHead().querySelector('.su-by')!.textContent).toBe('gui.conn.st_live')
    expect(main().querySelector('#connDlgBody .sustate')).toBeNull()
  })

  it('wears the entrance s own app icon, in its row and in its header', async () => {
    install([chan()])
    await mount()
    expect(rowNamed('Slack').querySelector('.channel-mark img')!.getAttribute('src')).toBe('assets/channels/slack.png')
    await act(async () => { openRow('Slack') })
    expect(paneHead().querySelector('.channel-mark img')!.getAttribute('src')).toBe('assets/channels/slack.png')
  })

  /* The foot says only what the head cannot: that something typed is not
     saved yet. The missing count is the head's, and is not said twice. */
  it('says an edit is unsaved, and leaves the missing count to the head', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true, secret: true }], missing: ['bot_token'] })])
    await mount()
    await act(async () => { openRow('Slack') })
    const body = main().querySelector('#connDlgBody')!
    expect(paneHead().querySelector('.su-by')!.textContent).toBe('gui.conn.st_missing {"n":1}')
    expect(paneFoot().querySelector('.n')!.textContent).toBe('')
    const box = body.querySelector<HTMLInputElement>('input[type="password"]')!
    await act(async () => {
      typeInto(box, 'tok')
    })
    expect(paneFoot().querySelector('.n')!.textContent).toBe('gui.conn.foot_dirty')
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
    await act(async () => { openRow('gui.chan.email') })
    const body = main().querySelector('#connDlgBody')!
    expect([...body.querySelectorAll('.sugsub')].map((g) => g.textContent)).toEqual([
      'gui.conn.g_imap',
      'gui.conn.g_smtp',
    ])
    const counts = [...body.querySelectorAll('.sugsub')].map(
      (g) => g.parentElement!.querySelectorAll('.sufield').length,
    )
    expect(counts).toEqual([2, 1])
  })

  it('leaves a channel that is one thing ungrouped', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
    await mount()
    await act(async () => { openRow('Slack') })
    expect(main().querySelector('#connDlgBody .sugsub')).toBeNull()
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
    await act(async () => { openRow('Slack') })
    await act(async () => {
      ;(main().querySelector('#connDlgBody .sucap') as HTMLElement).click()
    })
    const label = main().querySelector('#connDlgBody .suadv .sufield label')!
    expect(label.textContent).toBe('workspace')
    expect(label.getAttribute('title')).toBe(prose)
  })

  /* Backing out of a scan is the same errand as leaving the list: the entrance
     goes out of service. It used to be its own word, "cancel connecting". */
  it('backs out of a scan with the same word the list uses', async () => {
    const { calls } = install([
      chan({ id: 'weixin', key: 'gui.chan.weixin', on: true, qrLogin: true, running: true, connected: false }),
    ])
    await mount()
    await act(async () => { openRow('gui.chan.weixin') })
    /* The wizard's own foot has one button; backing out is it. */
    expect(paneFoot().querySelector('button')!.textContent).toBe('gui.conn.disconnect')
    await act(async () => {
      ;(paneFoot().querySelector('button') as HTMLElement).click()
    })
    expect(calls).toContainEqual(['apply', { id: 'weixin', patch: {}, enable: false }])
  })

  /* Signing in by phone is a sequence, and a channel merely switched off used
     to present an empty form: no code, no way to ask for one, nothing said. */
  it('walks a scan channel through its three steps instead of an empty form', async () => {
    const { calls } = install([chan({ id: 'weixin', key: 'gui.chan.weixin', on: false, qrLogin: true, fields: [] })])
    await mount()
    await act(async () => { openRow('gui.chan.weixin') })
    const body = main()
    expect([...body.querySelectorAll('.suwiz .step .st')].map((n) => n.textContent)).toEqual([
      'gui.conn.w1_idle',
      'gui.conn.w2',
      'gui.conn.w3',
    ])
    expect(wizStates()).toEqual(['now', 'idle', 'idle'])
    /* The code's place is drawn before there is a code, and polls nothing. */
    expect(body.querySelector('.su-qrph')!.textContent).toBe('gui.conn.qr_idle')
    expect(body.querySelector('.qrbox')).toBeNull()
    expect(body.querySelector('.sufield')).toBeNull()
    /* The list's verb, not a third one: it read "turn the entry on", which is
       also what step 1 above it says. */
    expect(paneFoot().querySelector('button.key')!.textContent).toBe('gui.conn.connect')
    await act(async () => {
      ;(paneFoot().querySelector('button.key') as HTMLElement).click()
    })
    expect(calls).toContainEqual(['apply', { id: 'weixin', patch: {}, enable: true }])
  })

  /* The adapter belongs to the app process, which builds its channel set at
     launch -- so turning the entry on is not what produces a code, and the
     step says what is actually left rather than spinning. */
  it('names what is still stopping the code, when the entry is on and its adapter is not', async () => {
    install([chan({ id: 'weixin', key: 'gui.chan.weixin', on: true, running: false, qrLogin: true, fields: [] })])
    await mount()
    await act(async () => { openRow('gui.chan.weixin') })
    const body = main()
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
    await act(async () => { openRow('gui.chan.weixin') })
    expect(main().querySelector('.suwiz .sd')!.textContent).toBe('gui.conn.w2_blocked')
    expect(wizStates()).toEqual(['done', 'idle', 'idle'])
  })

  it('drops the wizard once the entry is paired', async () => {
    install([chan({ id: 'weixin', key: 'gui.chan.weixin', on: true, running: true, connected: true, qrLogin: true, fields: [] })])
    await mount()
    await act(async () => { openRow('gui.chan.weixin') })
    expect(main().querySelector('.suwiz')).toBeNull()
    expect(paneHead().querySelector('.su-by')!.textContent).toBe('gui.conn.st_live')
  })

  /* The corner is the card's one press, and it is the card's own press: it
     opens the sheet and writes nothing, since getting in is a code or a
     credential and both are in the sheet. A connected card has nothing to add. */
  it('opens the sheet from the corner and writes nothing on the way', async () => {
    const { calls } = install([chan(), chan({ id: 'telegram', key: 'Telegram', on: true, running: true })])
    await mount()
    expect(rowNamed('Telegram').querySelector('.su-cbtn')).toBeNull()
    await act(async () => { (rowNamed('Slack').querySelector('.su-cbtn') as HTMLElement).click() })
    expect(sheetUp()).toBe(true)
    expect(paneHead().querySelector('.su-meta h3')!.textContent).toBe('Slack')
    expect(calls).toEqual([])
  })

  it('keeps its rendered shape, list', async () => {
    install([
      chan({ on: true, running: true }),
      chan({ id: 'telegram', key: 'Telegram' }),
      chan({ id: 'email', key: 'gui.chan.email' }),
    ])
    await mount()
    await screen.findByText('Slack')
    expect(domSnapshot(document.getElementById('connectionsBody')!)).toMatchSnapshot()
  })

  it('keeps its rendered shape, the picked channel', async () => {
    install([chan({ fields: [{ key: 'bot_token', required: true }], missing: ['bot_token'] })])
    await mount()
    await act(async () => { openRow('Slack') })
    expect(domSnapshot(main())).toMatchSnapshot()
  })
})
