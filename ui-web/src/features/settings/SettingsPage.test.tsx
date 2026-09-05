// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SettingsApp } from './SettingsPage'
import * as store from './store'
import * as lookStore from '../../shell/look'
import * as notifications from '../../shell/notifications'

import type { Shell } from '../../shell/bridge'
import type { SettingsSnapshot, SettingsSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* The connections page is opened by importing its island, so standing in for
   that module is how the manage button's second half is observed. */
const connOpens = vi.hoisted(() => ({ n: 0 }))
vi.mock('../connections/store', () => ({ open: () => { connOpens.n += 1 } }))

const toastWriter = vi.hoisted(() => ({ calls: [] as Array<[string, unknown]> }))
vi.mock('../../shell/toast', () => ({
  show: (text: string) => { toastWriter.calls.push(['toast', text]) },
}))

function snap(over: Partial<SettingsSnapshot> = {}): SettingsSnapshot {
  return {
    raw: {
      channels: { sendProgress: true, sendToolHints: false },
      cron: { defaultTimezone: 'Asia/Shanghai' },
      memory: { memoryTopK: 5 },
    },
    configPath: '~/.raven/config.json',
    everos: null,
    providers: [
      { id: 'anthropic', name: 'Anthropic', models: ['claude-opus-4-5'], on: true, kind: 'api_key' },
      { id: 'openai', name: 'OpenAI', models: [], on: false, kind: 'api_key' },
      /* Connected with nothing to lend: `on` means "usable", and these two are
         usable by a token file and by an address, with no key behind either. */
      { id: 'openai_codex', name: 'Codex', models: [], on: true, kind: 'oauth' },
      { id: 'ollama', name: 'Ollama', models: [], on: true, kind: 'local' },
    ],
    curProvider: 'anthropic',
    model: 'claude-opus-4-5',
    toolGroups: [
      { id: 'file', label: 'gui.toolgrp.file' },
      { id: 'net', label: 'gui.toolgrp.net' },
      { id: 'ask', label: 'gui.toolgrp.ask' },
    ],
    tools: [
      { id: 'read_file', name: 'read', group: 'file', reach: 'local', one: 'reads', on: true },
      { id: 'write_file', name: 'write', group: 'file', reach: 'local', one: 'writes', on: true, danger: true },
      { id: 'web_fetch', name: 'fetch', group: 'net', reach: 'net', one: 'fetches', on: true },
      { id: 'image_generate', name: 'draw', group: 'net', reach: 'net', one: 'draws', on: false, needs: 'key' },
    ],
    ...over,
  }
}

/* The island runs against the same two seams production wires up: a fake
   shell on window.RavenShell (T returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on window.DS.settings. */
function install(data: SettingsSnapshot = snap(), over: Partial<SettingsSource> = {}) {
  const calls: Array<[string, unknown]> = []
  const source: SettingsSource = {
    load: async () => data,
    set: async (key, value) => {
      calls.push(['set', { key, value }])
      return data
    },
    everosSet: async (section, fields, borrowFrom) => {
      calls.push(['everosSet', borrowFrom === undefined
        ? { section, fields }
        : { section, fields, borrowFrom }])
      return data
    },
    usage: async () => null,
    provider: async (op, params) => {
      calls.push(['provider', { op, params }])
      return data
    },
    model: () => data.model,
    version: () => null,
    checkUpdate: (btn) => { calls.push(['checkUpdate', btn]) },
    /* On the source now, not the shell: what a language flip means differs
       between the modes, so the source answers the pick. */
    setLang: (lang) => { calls.push(['setLang', lang]) },
    ...over,
  }
  const shellCalls: Array<[string, unknown]> = []
  toastWriter.calls = shellCalls
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    /* Confirms immediately: the dialog itself is legacy chrome, not island. */
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: (id) => shellCalls.push(['showPage', id]),
    openSet: () => shellCalls.push(['openSet', null]),
    closeSet: () => shellCalls.push(['closeSet', null]),
  }
  window.RavenShell = fakeShell
  /* The danger card's button is a SESSION operation offered from this page, so
     it goes out through DS.sessions rather than this page's own source. */
  const wiped: Array<null> = []
  window.DS = {
    settings: source,
    sessions: {
      snapshot: () => ({ rows: [{}, {}, {}], cur: null, busy: false }),
      replace: () => {},
      open: () => {},
      deleteAll: () => wiped.push(null),
    },
  }
  document.body.innerHTML =
    '<div class="snavlist" id="snavList"></div><h3 id="setTitle"></h3><p class="sub" id="setSub"></p>' +
    '<div class="spanels" id="spanels"></div>'
  return { source, calls, shellCalls, wiped }
}

async function mount() {
  const view = render(<SettingsApp />, { container: document.getElementById('spanels')! })
  await act(async () => {
    await store.open()
  })
  return view
}

const change = async (input: HTMLInputElement, value: string) => {
  await act(async () => {
    input.value = value
    input.dispatchEvent(new Event('change'))
  })
}

afterEach(() => {
  cleanup()
  store.reset()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
  lookStore.load()
  notifications.setEnabled(false)
})

describe('settings island', () => {
  it('renders the nav groups, lands on the usage tab, and titles the header', async () => {
    install()
    await mount()
    expect(screen.getByText('gui.set.grp.me')).toBeTruthy()
    expect(screen.getByText('gui.set.grp.agent')).toBeTruthy()
    expect(screen.getByText('gui.set.grp.env')).toBeTruthy()
    const cur = document.querySelector('#snavList [aria-current="true"]')!
    expect(cur.textContent).toContain('gui.set.pg.usage')
    expect(document.getElementById('setTitle')!.textContent).toBe('gui.set.pg.usage')
    /* The fixture's null usage answer is the demo's no-data note. */
    expect(await screen.findByText('gui.set.nodata')).toBeTruthy()
  })

  /* The one destructive button on the page, and it had no coverage: it used to
     leave through a shell verb, and now it leaves through DS.sessions. Either
     way what matters is that it goes out at all, and only after the confirm. */
  it('wipes every session through the session source, from the data page', async () => {
    const h = install()
    await mount()
    act(() => {
      store.setTab('data')
    })
    expect(h.wiped).toEqual([])
    const btn = screen.getByText('gui.set.delete_all')
    await act(async () => {
      btn.click()
    })
    expect(h.wiped).toHaveLength(1)
  })

  it('reads the counters when the dialog opens, never when it is shut', async () => {
    vi.useFakeTimers()
    let asked = 0
    let up = false
    install(snap(), {
      usage: async () => {
        asked += 1
        return null
      },
    })
    const sh = window.RavenShell!
    sh.setIsOpen = () => up
    /* Mounting the root is what a page load does, Settings untouched. */
    render(<SettingsApp />, { container: document.getElementById('spanels')! })
    await act(async () => {
      await Promise.resolve()
    })
    expect(asked).toBe(0)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(45000)
    })
    expect(asked).toBe(0)
    /* Opening it. The veil goes up inside open(), so the fake follows it. */
    sh.openSet = () => {
      up = true
    }
    await act(async () => {
      await store.open()
    })
    expect(asked).toBe(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15000)
    })
    expect(asked).toBe(2)
    /* Shut again: the tree stays mounted, so only the guard can stop it. */
    up = false
    await act(async () => {
      await vi.advanceTimersByTimeAsync(45000)
    })
    expect(asked).toBe(2)
  })

  it('switches tab from the nav and draws the values the snapshot holds', async () => {
    install()
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.channel').click()
    })
    const switches = screen.getAllByRole('switch')
    expect(switches[0]!.getAttribute('aria-checked')).toBe('true')
    expect(switches[1]!.getAttribute('aria-checked')).toBe('false')
    expect(document.getElementById('setTitle')!.textContent).toBe('gui.set.pg.channel')
  })

  /* The picker is legacy chrome, so the island cannot watch it: after a pick
     it has to re-read the model from the source or the card keeps showing the
     old one. Nothing exercised that callback, because the fixture had no
     picker at all and pickDefault returned early. */
  it('re-reads the model from the source after the picker changes it', async () => {
    /* The new model is held OUTSIDE the snapshot object the store loaded.
       Mutating that object instead would let the assertion pass without the
       re-read, because the store holds it by reference. */
    let picked = false
    install(snap(), {
      model: () => (picked ? 'anthropic/claude-sonnet-5' : 'claude-opus-4-5'),
      pickModel: (_anchor, after) => {
        picked = true
        after()
      },
    })
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    expect(screen.getByText('claude-opus-4-5')).toBeTruthy()
    await act(async () => {
      document.querySelector<HTMLButtonElement>('.pickm')!.click()
    })
    expect(screen.getByText('claude-sonnet-5')).toBeTruthy()
  })

  /* Both halves of the manage button, because neither was pinned: closeSet
     could be cut and every test stayed green, and the page it lands on only
     became a direct import when the round-trip verbs were retired. */
  it.each([
    ['gui.set.pg.channel'],
    ['gui.set.pg.proact'],
  ])('closes the dialog and opens connections, from %s', async (page) => {
    const { shellCalls } = install()
    await mount()
    await act(async () => {
      screen.getByText(page).click()
    })
    shellCalls.length = 0
    connOpens.n = 0
    const manage = screen.getAllByText('gui.set.chn.manage')
    expect(manage).toHaveLength(1)
    await act(async () => {
      manage[0]!.click()
    })
    expect(shellCalls).toContainEqual(['closeSet', null])
    expect(connOpens.n).toBe(1)
  })

  /* The language pick was a shell verb and is a source verb now, because what a
     flip MEANS differs between the modes: live persists config.language, which
     also drives the TUI and the language the agent replies in, while the offline
     page repaints and has nowhere to persist to. Nothing pinned the wiring
     before this. */
  it('asks the source to change the language, from the appearance page', async () => {
    const { calls } = install()
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.look').click()
    })
    await act(async () => {
      screen.getByText('gui.set.language_en').click()
    })
    expect(calls).toEqual([['setLang', 'en']])
  })

  it('persists an appearance pick through the modern look owner', async () => {
    install()
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.look').click()
    })
    await act(async () => {
      screen.getByText('gui.set.theme_dark').click()
    })
    expect(lookStore.get().theme).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('forces the notification test through the modern notification owner', async () => {
    const shown: string[] = []
    class FakeNotification {
      static permission: NotificationPermission = 'granted'

      constructor(title: string) {
        shown.push(title)
      }
    }
    vi.stubGlobal('Notification', FakeNotification)
    notifications.setEnabled(true)
    install()
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.notify').click()
    })
    await act(async () => {
      screen.getByRole('button', { name: 'gui.set.ntf.test' }).click()
    })
    expect(shown).toEqual(['gui.set.ntf.test_body'])
  })

  it('asks the settings source to check for an update, from the about page', async () => {
    const { calls } = install(snap(), { version: () => '0.1.7' })
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.about').click()
    })
    expect(screen.getByText('0.1.7')).toBeTruthy()
    const btn = screen.getByText<HTMLButtonElement>('gui.set.check_update')
    await act(async () => {
      btn.click()
    })
    expect(calls).toContainEqual(['checkUpdate', btn])
  })

  it('writes a switch flip through the source and redraws from the answer', async () => {
    const flipped = snap()
    ;(flipped.raw.channels as Record<string, unknown>).sendProgress = false
    const { calls } = install(snap(), { set: async (key, value) => (calls.push(['set', { key, value }]), flipped) })
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.channel').click()
    })
    await act(async () => {
      screen.getAllByRole('switch')[0]!.click()
    })
    expect(calls).toContainEqual(['set', { key: 'channels.sendProgress', value: false }])
    expect(screen.getAllByRole('switch')[0]!.getAttribute('aria-checked')).toBe('false')
  })

  it('commits a text field on change with its normalizer applied', async () => {
    const { calls } = install()
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.channel').click()
    })
    await change(screen.getByPlaceholderText<HTMLInputElement>('Asia/Shanghai'), ' UTC ')
    expect(calls).toContainEqual(['set', { key: 'cron.defaultTimezone', value: 'UTC' }])
  })

  it('resets an out-of-range number without writing, and writes a valid one', async () => {
    const { calls } = install()
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.memory').click()
    })
    const topk = screen.getByDisplayValue<HTMLInputElement>('5')
    await change(topk, '999')
    expect(calls.filter(([op]) => op === 'set')).toHaveLength(0)
    expect(topk.value).toBe('5')
    await change(topk, '7')
    expect(calls).toContainEqual(['set', { key: 'memory.memoryTopK', value: 7 }])
  })

  it('speaks the fixture refusal in the row and puts the typed value back', async () => {
    install(snap(), {
      set: async () => {
        throw { notLive: true }
      },
    })
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.channel').click()
    })
    const tz = screen.getByPlaceholderText<HTMLInputElement>('Asia/Shanghai')
    await change(tz, 'UTC')
    expect(document.querySelector('.crow .nlmsg')!.textContent).toBe('gui.set.not_live')
    expect(tz.value).toBe('Asia/Shanghai')
  })

  it('saves an everos role through the source and folds the editor', async () => {
    const { calls } = install()
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.memory').click()
    })
    expect(screen.getAllByText('gui.set.unset').length).toBe(4)
    await act(async () => {
      screen.getAllByText('gui.set.mem.setup')[0]!.click()
    })
    const form = document.querySelector('.mrole .ff')!
    const model = form.querySelector<HTMLInputElement>('input[type="text"]')!
    model.value = 'gpt-5-mini'
    await act(async () => {
      screen.getByText('gui.set.mem.save').click()
    })
    expect(calls).toContainEqual(['everosSet', { section: 'llm', fields: { model: 'gpt-5-mini' } }])
    expect(document.querySelector('.mrole .ff')).toBeNull()
  })

    it('borrows a connected account instead of asking for the key again', async () => {
      const { calls } = install()
      await mount()
      await act(async () => {
        screen.getByText('gui.set.pg.memory').click()
      })
      await act(async () => {
      screen.getAllByText('gui.set.mem.setup')[0]!.click()
    })
    const form = document.querySelector('.mrole .ff')!
    /* Only the connected one is offered: an unconnected provider would be a
       choice that fails on save for a reason the row cannot show. */
    const pick = form.querySelector<HTMLSelectElement>('select.mlend')!
    expect(Array.from(pick.options).map((o) => o.value)).toEqual(['', 'anthropic'])

    form.querySelector<HTMLInputElement>('input[type="text"]')!.value = 'text-embedding-3-large'
    await act(async () => {
      pick.value = 'anthropic'
      pick.dispatchEvent(new Event('change', { bubbles: true }))
    })
    /* The address and key fields are gone: the server fills both, and a field
       the reader can type into that is overwritten on save is a lie. */
    expect(document.querySelector('.mrole .ff input[type="password"]')).toBeNull()
    expect(document.querySelector('.mrole .ff .mnote')).toBeTruthy()

    await act(async () => {
      screen.getByText('gui.set.mem.save').click()
    })
    /* The name travels; the key does not, because this page never had it. */
    expect(calls).toContainEqual(['everosSet', {
      section: 'llm',
      fields: { model: 'text-embedding-3-large' },
      borrowFrom: 'anthropic',
    }])
  })

    it('still takes a key typed by hand when no account is borrowed', async () => {
      const { calls } = install()
      await mount()
      await act(async () => {
        screen.getByText('gui.set.pg.memory').click()
      })
      await act(async () => {
      screen.getAllByText('gui.set.mem.setup')[0]!.click()
    })
    const form = document.querySelector('.mrole .ff')!
    form.querySelector<HTMLInputElement>('input[type="text"]')!.value = 'm'
    form.querySelector<HTMLInputElement>('input[type="password"]')!.value = 'sk-typed'
    await act(async () => {
      screen.getByText('gui.set.mem.save').click()
    })
    expect(calls).toContainEqual(['everosSet', {
      section: 'llm',
      fields: { model: 'm', api_key: 'sk-typed' },
    }])
  })

  it('connects a provider from its card and refuses an empty key in the form', async () => {
    const { calls } = install()
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    /* The connected card offers manage, so the one connect button is the
       openai card's opener; once open, the card shows collapse and the only
       connect left is the form's save. */
    await act(async () => {
      screen.getByText('gui.model.connect').click()
    })
    expect(document.querySelector('.pcard.open')).toBeTruthy()
    await act(async () => {
      screen.getByText('gui.model.connect').click()
    })
    expect(document.querySelector('.perr')!.textContent).toBe('gui.model.need_key')
    /* The panel remounted on the validation redraw; query the live form. */
    const key = document.querySelector<HTMLInputElement>('.pcard.open .pform input[type="password"]')!
    key.value = ' sk-x '
    await act(async () => {
      screen.getByText('gui.model.connect').click()
    })
    expect(calls).toContainEqual(['provider', { op: 'save_key', params: { slug: 'openai', api_key: 'sk-x' } }])
  })

  it('writes the reasoning effort pick through settings.set', async () => {
    const { calls } = install()
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    await act(async () => {
      screen.getByText('gui.set.mdl.eff_high').click()
    })
    expect(calls).toContainEqual(['set', { key: 'agents.defaults.reasoningEffort', value: 'high' }])
  })

  /* ---- the toolset panel, which used to be drawn by the legacy layer ---- */

  const toolset = async () => {
    await act(async () => {
      screen.getByText('gui.set.pg.toolset').click()
    })
  }

  it('draws one card per group that has tools, counting the ones switched on', async () => {
    install()
    await mount()
    await toolset()
    const cards = [...document.querySelectorAll('#spanels .scard')]
    /* Three groups in the snapshot, and `ask` holds nothing -- an empty group
       is not an empty card, it is no card. */
    expect(cards.map((c) => c.querySelector('.ch .t')!.textContent)).toEqual(['gui.toolgrp.file', 'gui.toolgrp.net'])
    expect(cards[0]!.querySelector('.ch .d')!.textContent).toBe('gui.caps.tool_on {"on":2,"all":2}')
    expect(cards[1]!.querySelector('.ch .d')!.textContent).toBe('gui.caps.tool_on {"on":1,"all":2}')
    expect([...cards[0]!.querySelectorAll('.fset .trow .nm span:first-child')].map((n) => n.textContent))
      .toEqual(['read', 'write'])
    /* The badge takes both halves from the shared reach catalogue. */
    const badge = cards[1]!.querySelector<HTMLElement>('.trow .bdgs .kd')!
    expect(badge.textContent).toBe('gui.reach.net')
    expect(badge.title).toBe('gui.reach.net_hint')
  })

  /* The source row owns the live accessor that writes tools.disabledTools, so
     the flip has to assign that row rather than a copy the island keeps. */
  it('flips a tool by assigning on the source row, so the live accessor persists it', async () => {
    const data = snap()
    const writes: boolean[] = []
    let on = true
    Object.defineProperty(data.tools[0]!, 'on', {
      get: () => on,
      set: (v: boolean) => {
        on = v
        writes.push(v)
      },
    })
    const h = install(data)
    await mount()
    await toolset()
    const row = document.querySelectorAll('#spanels .scard')[0]!.querySelector('.trow')!
    await act(async () => {
      row.querySelector<HTMLElement>('.ctl .swi')!.click()
    })
    expect(writes).toEqual([false])
    /* And the row repaints from the accessor, rather than from a stale copy. */
    const again = document.querySelectorAll('#spanels .scard')[0]!.querySelector('.trow')!
    expect(again.className).toBe('trow off')
    expect(again.querySelector('.swi')!.getAttribute('aria-checked')).toBe('false')
    expect(h.shellCalls).toContainEqual(['toast', 'gui.caps.disabled_x {"name":"read"}'])
  })

  it('offers no switch for a tool withheld for want of a key', async () => {
    install()
    await mount()
    await toolset()
    const rows = [...document.querySelectorAll('#spanels .scard')[1]!.querySelectorAll('.trow')]
    const needs = rows[1]!
    expect(needs.querySelector('.ctl .pnote')!.textContent).toBe('gui.caps.needs_key')
    expect(needs.querySelector('.ctl .swi')).toBeNull()
  })

  it('writes a tool credential to its whitelisted key, and only when one was typed', async () => {
    const { calls } = install()
    await mount()
    await toolset()
    const net = document.querySelectorAll('#spanels .scard')[1]!
    /* The chip reads unset before the write: the key is absent from raw. */
    expect(net.querySelector('.trow .nm .kchip')!.className).toBe('kchip off')
    /* Folded until asked for: an editor per key-taking tool, always open, is
       four password fields nobody opened. */
    expect(document.querySelector('#spanels .tkrow')).toBeNull()
    await act(async () => {
      net.querySelector<HTMLElement>('.trow .ctl .mini.ghost')!.click()
    })
    const row = document.querySelector<HTMLElement>('#spanels .tkrow.tkey')!
    const field = row.querySelector<HTMLInputElement>('input[type="password"]')!
    /* An empty field is not a write. Whitespace only is the same thing. */
    field.value = '   '
    await act(async () => {
      row.querySelector<HTMLElement>('.mini')!.click()
    })
    expect(calls).toEqual([])
    field.value = '  jina-key  '
    await act(async () => {
      row.querySelector<HTMLElement>('.mini')!.click()
    })
    expect(calls).toContainEqual(['set', { key: 'tools.web.jinaApiKey', value: 'jina-key' }])
  })

  /* Where the refusal lands, which is not where the click did. A tool row has
     no `.crow` around it, so the legacy nlSay walked up to the `.scard` and
     appended there -- and only on the card whose row refused. */
  it('speaks a refused credential write on the card, not in the row', async () => {
    install(snap(), {
      set: async () => {
        throw { notLive: true }
      },
    })
    await mount()
    await toolset()
    const cards = [...document.querySelectorAll<HTMLElement>('#spanels .scard')]
    await act(async () => {
      cards[1]!.querySelector<HTMLElement>('.trow .ctl .mini.ghost')!.click()
    })
    const row = document.querySelector<HTMLElement>('#spanels .tkrow.tkey')!
    row.querySelector<HTMLInputElement>('input[type="password"]')!.value = 'k'
    await act(async () => {
      row.querySelector<HTMLElement>('.mini')!.click()
    })
    const live = [...document.querySelectorAll<HTMLElement>('#spanels .scard')]
    expect(live[1]!.querySelector('.nlmsg')!.textContent).toBe('gui.set.not_live')
    /* Last child of the card, which is where appending to the host put it --
       not tucked inside the row list. */
    expect(live[1]!.lastElementChild!.className).toBe('nlmsg')
    expect(live[1]!.querySelector('.tkrow .nlmsg')).toBeNull()
    expect(live[0]!.querySelector('.nlmsg')).toBeNull()
  })
})
