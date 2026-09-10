// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
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
      { id: 'anthropic', name: 'Anthropic', homepage: 'https://anthropic.com/', models: ['claude-opus-4-5'], on: true, kind: 'api_key' },
      { id: 'openai', name: 'OpenAI', models: [], on: false, kind: 'api_key' },
      /* Connected with nothing to lend: `on` means "usable", and these two are
         usable by a token file and by an address, with no key behind either. */
      { id: 'openai_codex', name: 'Codex', models: [], on: true, kind: 'oauth' },
      { id: 'ollama', name: 'Ollama', models: [], on: false, kind: 'local', acceptsKey: true },
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

/* Fill a field the way a person does. Assigning `.value` fires nothing, and the
   controls that watch their field -- the connect button's disabled state --
   never hear about it. */
function type(field: HTMLInputElement, value: string): void {
  field.value = value
  field.dispatchEvent(new Event('input', { bubbles: true }))
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
  /* `#setModal` is the dialog the add-model drawer portals into: it is
     positioned against that box rather than the window, so a harness without
     it would render the drawer nowhere. */
  document.body.innerHTML =
    '<div id="setModal"><div class="snavlist" id="snavList"></div><h3 id="setTitle"></h3>' +
    '<p class="sub" id="setSub"></p><div class="spanels" id="spanels"></div></div>'
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
    /* Queried through the default-model button rather than by text: the pane
       on the right lists the selected provider's models, so the same id is on
       the page twice and a bare text match is ambiguous. */
    const shown = (): string | undefined =>
      document.querySelector<HTMLElement>('.pickm .mono')?.textContent ?? undefined
    expect(shown()).toBe('claude-opus-4-5')
    await act(async () => {
      document.querySelector<HTMLButtonElement>('.pickm')!.click()
    })
    expect(shown()).toBe('claude-sonnet-5')
  })

  it('moves the default badge with a cross-provider pick, without reopening the page', async () => {
    /* The default is a (model, provider) pair. Re-reading only the model after
       a pick left the badge on the old provider until a reload -- so a pick
       that crosses providers must move both halves through the callback. */
    let picked = false
    install(snap(), {
      model: () => (picked ? 'openai/gpt-5.2' : 'claude-opus-4-5'),
      defaultProvider: () => (picked ? 'openai' : 'anthropic'),
      pickModel: (_anchor, after) => {
        picked = true
        after()
      },
    })
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    /* On the rail, so which provider holds the default is answerable without
       selecting each one in turn. */
    const badged = () =>
      [...document.querySelectorAll('.mrail .mrow')].find((row) => row.querySelector('.tagm'))
        ?.querySelector('.nm')
        ?.textContent
    expect(badged()).toBe('Anthropic')
    await act(async () => {
      document.querySelector<HTMLButtonElement>('.pickm')!.click()
    })
    expect(badged()).toBe('OpenAI')
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
    /* Nothing to open: the rail selects and the pane is already a form. The
       connected provider is first, so OpenAI has to be picked. */
    const openai = [...document.querySelectorAll<HTMLButtonElement>('.mrail .mrow')].find(
      (row) => row.querySelector('.nm')?.textContent === 'OpenAI',
    )!
    await act(async () => {
      openai.click()
    })
    expect(document.querySelector('.mpanel .mtitle')?.textContent).toContain('OpenAI')
    const connect = screen.getByText('gui.model.connect') as HTMLButtonElement
    expect(connect.disabled).toBe(true)
    const key = document.querySelector<HTMLInputElement>('.mpanel .pform input[type="password"]')!
    await act(async () => type(key, ' sk-x '))
    expect((screen.getByText('gui.model.connect') as HTMLButtonElement).disabled).toBe(false)
    await act(async () => {
      screen.getByText('gui.model.connect').click()
    })
    expect(calls).toContainEqual(['provider', { op: 'save_key', params: { slug: 'openai', api_key: 'sk-x' } }])
  })

  it('resets LM Studio to the backend-provided default endpoint', async () => {
    const local = snap({
      providers: [{
        id: 'lm_studio',
        name: 'LM Studio',
        models: [],
        on: false,
        kind: 'local',
        needsBase: true,
        defaultApiBase: 'http://localhost:1234/v1',
      }],
      curProvider: '',
    })
    const { calls } = install(local)
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    const base = document.querySelector<HTMLInputElement>('.mpanel .pform input[type="text"]')!
    expect(base.value).toBe('http://localhost:1234/v1')
    base.value = 'http://localhost:9999/v1'
    const update = screen.getByRole('button', { name: 'gui.model.update' })
    expect(update.getAttribute('title')).toBe('gui.model.update')
    expect(update.textContent).toBe('')
    expect(update.querySelector('.update-icon')).toBeTruthy()
    await act(async () => update.click())
    expect(base.value).toBe('http://localhost:1234/v1')
    expect(calls).toEqual([])
  })

  /* A local server that merely accepts a token is a complete submission with the
     key field empty -- the address is what gets saved. */
  it('keeps the button live for a local server whose key is optional', async () => {
    const { calls } = install(
      snap({
        providers: [{
          id: 'lm_studio',
          name: 'LM Studio',
          models: [],
          on: false,
          kind: 'local',
          needsBase: true,
          acceptsKey: true,
          defaultApiBase: 'http://localhost:1234/v1',
        }],
        curProvider: '',
      }),
    )
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    const connect = screen.getByText('gui.model.connect') as HTMLButtonElement
    expect(connect.disabled).toBe(false)
    await act(async () => connect.click())
    expect(calls).toContainEqual([
      'provider',
      { op: 'save_key', params: { slug: 'lm_studio', api_key: '', api_base: 'http://localhost:1234/v1' } },
    ])
  })

  it('connects MiniMax CN with its regional endpoint and API key', async () => {
    const cn = snap({
      providers: [{
        id: 'minimax_cn_api',
        name: 'MiniMax (CN)',
        models: ['minimax-cn-api/MiniMax-M3'],
        on: false,
        kind: 'endpoint',
        defaultApiBase: 'https://api.minimaxi.com/v1/',
      }],
      curProvider: '',
    })
    const { calls } = install(cn)
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    const form = document.querySelector<HTMLElement>('.mpanel .pform')!
    const base = form.querySelector<HTMLInputElement>('input[type="text"]')!
    expect(base.value).toBe('https://api.minimaxi.com/v1/')
    const reset = form.querySelector<HTMLButtonElement>('button.update')!
    expect(reset).toBeTruthy()
    base.value = 'https://proxy.example/v1'
    await act(async () => reset.click())
    expect(base.value).toBe('https://api.minimaxi.com/v1/')
    base.value = 'https://proxy.example/v1'
    await act(async () => type(form.querySelector<HTMLInputElement>('input[type="password"]')!, 'K-CN'))
    await act(async () => {
      screen.getByText('gui.model.connect').click()
    })
    expect(calls).toContainEqual([
      'provider',
      {
        op: 'save_key',
        params: { slug: 'minimax_cn_api', api_key: 'K-CN', api_base: 'https://proxy.example/v1' },
      },
    ])
  })

  it('shows the shipped host for a key provider and copies what the field holds', async () => {
    const written: string[] = []
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: (text: string) => (written.push(text), Promise.resolve()) },
    })
    const keyed = snap({
      providers: [{
        id: 'dashscope',
        name: 'Alibaba Cloud',
        models: [],
        on: false,
        kind: 'key',
        defaultApiBase: 'https://dashscope.aliyuncs.com/compatible-mode/v1/',
      }],
      curProvider: '',
    })
    install(keyed)
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    const field = document.querySelector<HTMLElement>('.mpanel .pform .hostfield')!
    const base = field.querySelector<HTMLInputElement>('input[type="text"]')!
    expect(base.value).toBe('https://dashscope.aliyuncs.com/compatible-mode/v1/')

    const copy = field.querySelector<HTMLButtonElement>('button.hcopy')!
    expect(copy.getAttribute('data-tip')).toBe('gui.model.copy')
    base.value = 'https://proxy.example/v1'
    await act(async () => copy.click())
    expect(written).toEqual(['https://proxy.example/v1'])
    expect(field.querySelector('button.hcopy')!.getAttribute('data-tip')).toBe('gui.model.copied')
  })

  /* A shipped address is shown to answer "where does this go". Writing it back
     unasked would turn the label into a config override -- and dashscope's is
     display-only on purpose. */
  it('sends the host only once it differs from the shipped default', async () => {
    const keyed = snap({
      providers: [{
        id: 'dashscope',
        name: 'Alibaba Cloud',
        models: [],
        on: false,
        kind: 'key',
        defaultApiBase: 'https://dashscope.aliyuncs.com/compatible-mode/v1/',
      }],
      curProvider: '',
    })
    const { calls } = install(keyed)
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    await act(async () =>
      type(document.querySelector<HTMLInputElement>('.mpanel .pform input[type="password"]')!, 'sk-a'),
    )
    await act(async () => {
      screen.getByText('gui.model.connect').click()
    })
    expect(calls).toContainEqual(['provider', { op: 'save_key', params: { slug: 'dashscope', api_key: 'sk-a' } }])

    const base = document.querySelector<HTMLInputElement>('.mpanel .pform input[type="text"]')!
    base.value = 'https://proxy.example/v1'
    await act(async () =>
      type(document.querySelector<HTMLInputElement>('.mpanel .pform input[type="password"]')!, 'sk-b'),
    )
    await act(async () => {
      screen.getByText('gui.model.connect').click()
    })
    expect(calls).toContainEqual([
      'provider',
      { op: 'save_key', params: { slug: 'dashscope', api_key: 'sk-b', api_base: 'https://proxy.example/v1' } },
    ])
  })

  it('leaves the host field off a provider that ships no address', async () => {
    const bare = snap({
      providers: [{ id: 'openai', name: 'OpenAI', models: [], on: false, kind: 'key' }],
      curProvider: '',
    })
    install(bare)
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    expect(document.querySelector('.mpanel .pform .hostfield')).toBeNull()
    expect(document.querySelector('.mpanel .pform [data-sec="host"]')).toBeNull()
  })

  /* Four MiniMax sections -- global and CN, each by key and by OAuth -- are one
     vendor as far as the rail is concerned. */
  const withMiniMax = (over: Partial<{ cnOn: boolean }> = {}) =>
    snap({
      providers: [
        { id: 'anthropic', name: 'Anthropic', models: [], on: true, kind: 'api_key' },
        { id: 'minimax', name: 'MiniMax (Global)', models: [], on: false, kind: 'api_key' },
        { id: 'openai', name: 'OpenAI', models: [], on: false, kind: 'api_key' },
        { id: 'minimax_cn_api', name: 'MiniMax (CN)', models: [], on: !!over.cnOn, kind: 'endpoint' },
        { id: 'minimax_global', name: 'MiniMax Global (OAuth)', models: [], on: false, kind: 'oauth' },
        { id: 'minimax_cn', name: 'MiniMax CN (OAuth)', models: [], on: false, kind: 'oauth' },
      ],
      curProvider: 'anthropic',
    })

  const railNames = (): string[] =>
    [...document.querySelectorAll<HTMLElement>('.mrail > .mrow, .mrail > .mgrp > .mrow.gh')].map(
      (row) => row.querySelector('.nm')?.textContent ?? '',
    )

  it('folds the MiniMax sections into one rail row that opens on demand', async () => {
    install(withMiniMax())
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    expect(railNames()).toEqual(['Anthropic', 'MiniMax', 'OpenAI'])
    const head = document.querySelector<HTMLButtonElement>('.mrail .mrow.gh')!
    expect(head.getAttribute('aria-expanded')).toBe('false')
    expect(head.querySelector('.gc')?.textContent).toBe('4')
    expect(document.querySelector('.mgsub')).toBeNull()

    await act(async () => head.click())
    expect([...document.querySelectorAll('.mgsub .mrow .nm')].map((n) => n.textContent)).toEqual([
      'MiniMax (Global)',
      'MiniMax (CN)',
      'MiniMax Global (OAuth)',
      'MiniMax CN (OAuth)',
    ])
  })

  /* One connected member used to lift itself and leave its three siblings in
     the unconnected half -- the same vendor in two places. */
  it('lifts the whole MiniMax group when one of its sections is connected', async () => {
    install(withMiniMax({ cnOn: true }))
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    expect(railNames()).toEqual(['Anthropic', 'MiniMax', 'OpenAI'])
    const head = document.querySelector<HTMLButtonElement>('.mrail .mrow.gh')!
    expect(head.querySelector('.provider-status')?.className).toBe('provider-status on')
  })

  it('shows no dot on the group until a section is connected', async () => {
    install(withMiniMax())
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    expect(document.querySelector('.mrail .mrow.gh .provider-status')).toBeNull()
  })

  it('leaves a lone family member as a plain row', async () => {
    install(
      snap({
        providers: [
          { id: 'anthropic', name: 'Anthropic', models: [], on: true, kind: 'api_key' },
          { id: 'minimax', name: 'MiniMax (Global)', models: [], on: false, kind: 'api_key' },
        ],
        curProvider: 'anthropic',
      }),
    )
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    expect(document.querySelector('.mrail .mrow.gh')).toBeNull()
    expect(railNames()).toEqual(['Anthropic', 'MiniMax (Global)'])
  })

  it('opens the group the pane starts on so the selection is never hidden', async () => {
    install(
      snap({
        providers: [
          { id: 'minimax_cn_api', name: 'MiniMax (CN)', models: [], on: true, kind: 'endpoint' },
          { id: 'minimax', name: 'MiniMax (Global)', models: [], on: false, kind: 'api_key' },
          { id: 'openai', name: 'OpenAI', models: [], on: false, kind: 'api_key' },
        ],
        curProvider: 'minimax_cn_api',
      }),
    )
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    expect(document.querySelector('.mrail .mrow.gh')!.getAttribute('aria-expanded')).toBe('true')
    const open = [...document.querySelectorAll<HTMLElement>('.mgsub .mrow')].find(
      (row) => row.getAttribute('aria-current') === 'true',
    )!
    expect(open.querySelector('.nm')?.textContent).toBe('MiniMax (CN)')
    expect(document.querySelector('.mpanel .mtitle')?.textContent).toContain('MiniMax (CN)')
  })

  it('puts provider branding first and connection state at the far edge', async () => {
    install()
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    const rows = [...document.querySelectorAll<HTMLElement>('.mrail .mrow')]
    const anthropic = rows.find(row => row.querySelector('.nm')?.textContent === 'Anthropic')!
    const openai = rows.find(row => row.querySelector('.nm')?.textContent === 'OpenAI')!

    expect(anthropic.firstElementChild?.getAttribute('src')).toBe('assets/providers/anthropic.svg')
    expect(anthropic.querySelector('.provider-status')?.className).toBe('provider-status on')
    expect(openai.querySelector('.provider-status')).toBeNull()

    /* The homepage link moved into the pane: an anchor inside the rail's
       button is neither valid markup nor clickable as a link. */
    const title = document.querySelector<HTMLElement>('.mpanel .mtitle')!
    expect(title.querySelector('.provider-link')?.getAttribute('href')).toBe('https://anthropic.com/')
    expect(title.querySelector('.provider-link')?.getAttribute('aria-label')).toBe('Anthropic homepage')
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

  it('writes the default permission mode pick through settings.set', async () => {
    const { calls } = install()
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.perm').click()
    })
    await act(async () => {
      screen.getByText('gui.perm.smart').click()
    })
    expect(calls).toContainEqual(['set', { key: 'permissions.mode', value: 'smart' }])
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
    expect(calls).toContainEqual(['set', { key: 'tools.web.providers.jina.apiKey', value: 'jina-key' }])
  })

  it('files a web key under the vendor the row selects, and the vendor pick is its own write', async () => {
    const { calls } = install(
      snap({
        raw: { tools: { web: { fetch: { provider: 'tavily' }, providers: { tavily: { apiKey: 'tv' } } } } },
      }),
    )
    await mount()
    await toolset()
    const net = document.querySelectorAll('#spanels .scard')[1]!
    /* Set: the chip follows the selected vendor's slot, not Jina's. */
    expect(net.querySelector('.trow .nm .kchip')!.className).toBe('kchip')
    await act(async () => {
      net.querySelector<HTMLElement>('.trow .ctl .mini.ghost')!.click()
    })
    const row = document.querySelector<HTMLElement>('#spanels .tkrow.tkey')!
    const pick = row.querySelector<HTMLSelectElement>('select')!
    expect(pick.value).toBe('tavily')
    expect([...pick.options].map((o) => o.value)).toEqual(['jina', 'anysearch', 'tavily', 'exa', 'firecrawl'])
    /* The pick is its own write, to the provider key, never to a key slot. */
    await act(async () => {
      fireEvent.change(pick, { target: { value: 'exa' } })
    })
    expect(calls).toContainEqual(['set', { key: 'tools.web.fetch.provider', value: 'exa' }])
    /* A write re-reads the snapshot, so the editor is queried afresh. */
    const again = document.querySelector<HTMLElement>('#spanels .tkrow.tkey')!
    const field = again.querySelector<HTMLInputElement>('input[type="password"]')!
    field.value = 'tv-2'
    await act(async () => {
      again.querySelector<HTMLElement>('.mini')!.click()
    })
    expect(calls).toContainEqual(['set', { key: 'tools.web.providers.tavily.apiKey', value: 'tv-2' }])
  })

  /* An upgraded config keeps its key in the pre-vendor leaf, which the tools
     read after the slot. The row counted that as configured while Clear wrote
     only the slot, so the credential survived being cleared -- and a pasted
     replacement, once cleared, fell back to the older secret. */
  it('clears the pre-vendor leaf an upgraded config still keeps the key in', async () => {
    const { calls } = install(snap({ raw: { tools: { web: { jinaApiKey: 'legacy-jina' } } } }))
    await mount()
    await toolset()
    const net = document.querySelectorAll('#spanels .scard')[1]!
    /* Set, on the strength of the leaf alone: the slot holds nothing. */
    expect(net.querySelector('.trow .nm .kchip')!.className).toBe('kchip')
    await act(async () => {
      net.querySelector<HTMLElement>('.trow .ctl .mini.ghost')!.click()
    })
    const row = document.querySelector<HTMLElement>('#spanels .tkrow.tkey')!
    await act(async () => {
      row.querySelector<HTMLElement>('.mini.ghost')!.click()
    })
    expect(calls).toEqual([
      ['set', { key: 'tools.web.providers.jina.apiKey', value: '' }],
      ['set', { key: 'tools.web.jinaApiKey', value: '' }],
    ])
  })

  it('clears both paths when a replacement was pasted over a legacy key', async () => {
    const { calls } = install(
      snap({ raw: { tools: { web: { jinaApiKey: 'legacy-jina', providers: { jina: { apiKey: 'new-jina' } } } } } }),
    )
    await mount()
    await toolset()
    const net = document.querySelectorAll('#spanels .scard')[1]!
    await act(async () => {
      net.querySelector<HTMLElement>('.trow .ctl .mini.ghost')!.click()
    })
    const row = document.querySelector<HTMLElement>('#spanels .tkrow.tkey')!
    await act(async () => {
      row.querySelector<HTMLElement>('.mini.ghost')!.click()
    })
    expect(calls).toEqual([
      ['set', { key: 'tools.web.providers.jina.apiKey', value: '' }],
      ['set', { key: 'tools.web.jinaApiKey', value: '' }],
    ])
  })

  it('leaves a vendor with no pre-vendor leaf a single write', async () => {
    const { calls } = install(
      snap({
        raw: { tools: { web: { fetch: { provider: 'tavily' }, providers: { tavily: { apiKey: 'tv' } } } } },
      }),
    )
    await mount()
    await toolset()
    const net = document.querySelectorAll('#spanels .scard')[1]!
    await act(async () => {
      net.querySelector<HTMLElement>('.trow .ctl .mini.ghost')!.click()
    })
    const row = document.querySelector<HTMLElement>('#spanels .tkrow.tkey')!
    await act(async () => {
      row.querySelector<HTMLElement>('.mini.ghost')!.click()
    })
    expect(calls).toEqual([['set', { key: 'tools.web.providers.tavily.apiKey', value: '' }]])
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

async function openImageSettings() {
  await act(async () => { screen.getByText('gui.set.pg.toolset').click() })
  const row = screen.getByText('draw').closest('.trow')!
  await act(async () => { row.querySelector<HTMLButtonElement>('.ctl .mini.ghost')!.click() })
}

describe('image model selection', () => {
  it('defaults to sunburst and saves one model/quality pair', async () => {
    const { calls } = install()
    await mount()
    await openImageSettings()
    const picker = screen.getByLabelText('gui.caps.image_model') as HTMLSelectElement
    expect(picker.value).toBe('0')
    expect(picker.options).toHaveLength(11)
    expect(Array.from(picker.options).some((option) => option.text.includes('gemini'))).toBe(false)
    await act(async () => { fireEvent.change(picker, { target: { value: '4' } }) })
    expect(calls).toEqual([['set', {
      key: 'tools.media.image', value: { model: 'openai/gpt-image-2', quality: 'high' },
    }]])
  })

  it('clears gpt quality when choosing a different preset', async () => {
    const { calls } = install(snap({ raw: { tools: { media: { image: { model: 'openai/gpt-image-2', quality: 'high' } } } } }))
    await mount()
    await openImageSettings()
    await act(async () => { fireEvent.change(screen.getByLabelText('gui.caps.image_model'), { target: { value: '9' } }) })
    expect(calls).toEqual([['set', {
      key: 'tools.media.image', value: { model: 'x-ai/grok-imagine-image-2.0', quality: '' },
    }]])
  })

  it('edits a custom id without writing until save, and clears quality', async () => {
    const { calls } = install()
    await mount()
    await openImageSettings()
    await act(async () => { fireEvent.change(screen.getByLabelText('gui.caps.image_model'), { target: { value: 'custom' } }) })
    expect(calls).toEqual([])
    await act(async () => { fireEvent.change(screen.getByLabelText('gui.caps.custom_model'), { target: { value: '  vendor/new-image  ' } }) })
    await act(async () => { screen.getByText('gui.save').click() })
    expect(calls).toEqual([['set', {
      key: 'tools.media.image', value: { model: 'vendor/new-image', quality: '' },
    }]])
  })

  it('preserves an existing custom model when opening the editor', async () => {
    const { calls } = install(snap({ raw: { tools: { media: { image: { model: 'vendor/existing-image', quality: '' } } } } }))
    await mount()
    await openImageSettings()
    expect((screen.getByLabelText('gui.caps.image_model') as HTMLSelectElement).value).toBe('custom')
    expect((screen.getByLabelText('gui.caps.custom_model') as HTMLInputElement).value).toBe('vendor/existing-image')
    expect(calls).toEqual([])
  })

  it('retains the selected preset when saving fails', async () => {
    install(snap(), { set: async () => { throw new Error('save failed') } })
    await mount()
    await openImageSettings()
    const picker = screen.getByLabelText('gui.caps.image_model') as HTMLSelectElement
    await act(async () => { fireEvent.change(picker, { target: { value: '4' } }) })
    const current = screen.getByLabelText('gui.caps.image_model') as HTMLSelectElement
    expect(current.value).toBe('0')
    expect(current.disabled).toBe(false)
  })
})

it('shows the saved image model after the settings snapshot refreshes', async () => {
  let data = snap()
  install(data, { set: async (_key, value) => {
    data = { ...data, raw: { tools: { media: { image: value } } } }
    return data
  } })
  await mount()
  await openImageSettings()
  await act(async () => { fireEvent.change(screen.getByLabelText('gui.caps.image_model'), { target: { value: '4' } }) })
  expect((screen.getByLabelText('gui.caps.image_model') as HTMLSelectElement).value).toBe('4')
})

it.each([
  [{ model: 'openai/gpt-image-2' }, '3'],
  [{ model: 'openai/gpt-image-2.5-sunburst' }, '0'],
  [{ model: 'openai/gpt-image-2.5-flare', quality: '' }, '1'],
  [{ model: 'openai/gpt-image-2', quality: '' }, 'custom'],
])('preserves absent versus explicitly empty image quality: %j', async (image, selected) => {
  install(snap({ raw: { tools: { media: { image } } } }))
  await mount()
  await openImageSettings()
  expect((screen.getByLabelText('gui.caps.image_model') as HTMLSelectElement).value).toBe(selected)
})

describe('reported usage', () => {
  it.each([null, 0, 0.84])('renders reported cost %s with cache and missing data', async (cost) => {
    const total = {
      calls: 3, input_tokens: 100, output_tokens: 20, cost_usd: cost,
      cache_read_tokens: 60, cache_write_tokens: null,
      cost_missing_calls: 1, cache_read_missing_calls: 0,
      cache_write_missing_calls: 2, legacy_cost_calls: 1,
    }
    install(snap(), { usage: async () => ({
      days: 30, llm: { total, models: [{ model: 'test-model', ...total }] },
      tools: { total: 0, counts: [] },
    }) })
    await mount()
    expect(screen.getByText('gui.set.usg.cache_read')).toBeTruthy()
    expect(screen.queryByText(/gui.set.usg.cache_write/)).toBeNull()
    expect(screen.getByText('gui.set.usg.legacy')).toBeTruthy()
    expect(screen.getAllByText(/gui.set.usg.cost_missing/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/gui.set.usg.cache_missing/)).toBeNull()
    if (cost === null) expect(screen.queryByText(/\$0/)).toBeNull()
    else expect(screen.getAllByText(cost === 0 ? /\$0 ·/ : /\$0.8400/).length).toBeGreaterThan(0)
  })
})

it('shows unknown tokens for an image model that only reports money', async () => {
  const total = {
    calls: 1, input_tokens: null, output_tokens: null, cost_usd: 0.2,
    input_missing_calls: 1, output_missing_calls: 1,
    cache_read_tokens: null, cache_write_tokens: null,
    cost_missing_calls: 0, cache_read_missing_calls: 1,
    cache_write_missing_calls: 1, legacy_cost_calls: 0,
  }
  install(snap(), { usage: async () => ({
    days: 30, llm: { total, models: [{ model: 'image-model', ...total }] },
    tools: { total: 1, counts: [{ name: 'image_generate', count: 1 }] },
  }) })
  await mount()
  const input = screen.getByText('gui.set.usg.in').closest('.stat')!
  const output = screen.getByText('gui.set.usg.out').closest('.stat')!
  expect(input.querySelector('.v')!.textContent).toBe('gui.set.usg.unknown')
  expect(output.querySelector('.v')!.textContent).toBe('gui.set.usg.unknown')
  expect(screen.getAllByText(/\$0.2000/).length).toBeGreaterThan(0)
  expect(screen.queryByText(/· 0 tok/)).toBeNull()
})

it.each([null, 0, 0.75])('renders persisted reported cost %s without treating unknown as free', async (cost) => {
  const total = {
    calls: 2, input_tokens: 100, output_tokens: 20, cost_usd: cost,
    cache_read_tokens: null, cache_write_tokens: null,
    cost_missing_calls: 1, cache_read_missing_calls: 2,
    cache_write_missing_calls: 2, legacy_cost_calls: 1,
  }
  install(snap(), { usage: async () => ({
    days: 30, llm: { total, models: [{ model: 'reported-model', ...total }] },
    tools: { total: 0, counts: [] },
  }) })
  await mount()
  expect(screen.getByText('gui.set.usg.legacy')).toBeTruthy()
  expect(screen.getAllByText(/gui.set.usg.cost_missing/).length).toBeGreaterThan(0)
  if (cost === null) {
    expect(screen.queryByText(/\$0/)).toBeNull()
    expect(screen.getAllByText(/gui.set.usg.unknown/).length).toBeGreaterThan(0)
  } else {
    expect(screen.getAllByText(cost === 0 ? /\$0 ·/ : /\$0.7500/).length).toBeGreaterThan(0)
  }
})

describe('the models pane', () => {
  const openModels = async (over: Partial<SettingsSnapshot> = {}) => {
    const h = install(snap(over))
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    return h
  }
  const railNames = (): (string | null | undefined)[] =>
    [...document.querySelectorAll('.mrail .mrow .nm')].map((n) => n.textContent)

  it('leaves the rail where the reader left it when a provider is picked', async () => {
    await openModels()
    const rail = document.querySelector<HTMLElement>('.mrail')!
    rail.scrollTop = 240

    const ollama = [...document.querySelectorAll<HTMLButtonElement>('.mrail .mrow')].find(
      (row) => row.querySelector('.nm')?.textContent === 'Ollama',
    )!
    await act(async () => {
      ollama.click()
    })

    /* The same element, not a rebuilt one. `epoch` remounts the whole panel so
       uncontrolled fields restart from freshly loaded values, and a remounted
       rail is a new node at the top -- picking a provider near the bottom of
       forty threw the list back to the first. */
    expect(document.querySelector('.mrail')).toBe(rail)
    expect(rail.scrollTop).toBe(240)
    /* The pane still follows the pick: it is keyed by the provider it shows. */
    expect(document.querySelector('.mpanel .mtitle')?.textContent).toContain('Ollama')
  })

  it('keeps the rail where the reader left it after updating a local host', async () => {
    const local = snap({
      providers: [{
        id: 'lm_studio',
        name: 'LM Studio',
        models: [],
        on: false,
        kind: 'local',
        needsBase: true,
        defaultApiBase: 'http://localhost:1234/v1',
      }],
      curProvider: '',
    })
    const { calls } = await openModels(local)
    const rail = document.querySelector<HTMLElement>('.mrail')!
    rail.scrollTop = 240

    const update = screen.getByRole('button', { name: 'gui.model.update' })
    await act(async () => update.click())

    const updatedRail = document.querySelector<HTMLElement>('.mrail')!
    expect(updatedRail).toBe(rail)
    expect(updatedRail.scrollTop).toBe(240)
    expect(calls).toEqual([])
  })

  it('ranks the connected providers ahead of the unconfigured ones', async () => {
    await openModels()
    /* "Which of these can I actually use" is what this page is opened with, so
       the three connected rows come first -- and in their original order
       within each half, so saving a key does not reshuffle the rail under the
       cursor. */
    expect(railNames()).toEqual(['Anthropic', 'Codex', 'OpenAI', 'Ollama'])
  })

  it('opens on the first connected provider and swaps the pane on a click', async () => {
    await openModels()
    expect(document.querySelector('.mpanel .mtitle')?.textContent).toContain('Anthropic')
    expect(document.querySelector('.mrail .mrow[aria-current="true"] .nm')?.textContent).toBe('Anthropic')

    const ollama = [...document.querySelectorAll<HTMLButtonElement>('.mrail .mrow')].find(
      (row) => row.querySelector('.nm')?.textContent === 'Ollama',
    )!
    await act(async () => {
      ollama.click()
    })
    expect(document.querySelector('.mpanel .mtitle')?.textContent).toContain('Ollama')
    /* Ollama is address-first but may also send a bearer token to a protected
       remote server, so its key field is optional beside the required host. */
    expect(document.querySelector('.mpanel input[type="password"]')).toBeTruthy()
  })

  const CATALOGUE = {
    models: [
      { id: 'deepseek/deepseek-v4-pro', label: 'DeepSeek V4 Pro', kind: 'text', added: true,
        capabilities: ['reasoning', 'function-call'], context_window: 128000 },
      { id: 'deepseek/deepseek-v4-flash', label: 'DeepSeek V4 Flash', kind: 'text', added: false,
        capabilities: ['function-call'] },
      { id: 'deepseek/deepseek-embed', label: 'DeepSeek Embed', kind: 'embedding', added: false,
        capabilities: ['embedding'] },
    ],
    status: 'ok',
  }
  const openCatalogue = async (over: Partial<SettingsSource> = {}, snapOver: Partial<SettingsSnapshot> = {}) => {
    const h = install(snap(snapOver), { fetchModels: async () => CATALOGUE, ...over })
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    await act(async () => {
      document.querySelectorAll<HTMLButtonElement>('.mpanel .mbtns button')[0]!.click()
    })
    return h
  }
  const catRows = (): (string | null | undefined)[] =>
    [...document.querySelectorAll('.mdrawer .mlist .mitem .nm')].map((n) => n.textContent)

  it('opens the fetched catalogue in the same drawer the + button uses', async () => {
    await openCatalogue()
    expect(document.querySelector('#setModal > .mdrawer .dpane')).toBeTruthy()
    expect(document.querySelector('.mdrawer header b')?.textContent).toContain('Anthropic')
    expect(document.querySelector('.mdrawer header .ct')?.textContent).toBe('3')
    expect(catRows()).toEqual(['DeepSeek V4 Pro', 'DeepSeek V4 Flash', 'DeepSeek Embed'])
    /* A row already in the provider's list offers the way out, not the way in. */
    const [added, absent] = [...document.querySelectorAll<HTMLButtonElement>('.mdrawer .mitem .rm')]
    expect(added!.textContent).toBe('−')
    expect(absent!.textContent).toBe('+')
  })

  it('narrows by search and by kind, counting against the search', async () => {
    await openCatalogue()
    const kind = (label: string): HTMLButtonElement =>
      [...document.querySelectorAll<HTMLButtonElement>('.mkind')].find(
        (b) => b.querySelector('span')?.textContent === label,
      )!
    expect(kind('gui.model.kind_all').querySelector('i')?.textContent).toBe('3')
    expect(kind('gui.model.type.embedding').querySelector('i')?.textContent).toBe('1')

    await act(async () => {
      fireEvent.change(document.querySelector<HTMLInputElement>('.msearch')!, { target: { value: 'flash' } })
    })
    /* The counts follow the term: a filter row whose numbers ignore the search
       tells the reader nothing about what pressing it will show. */
    expect(kind('gui.model.kind_all').querySelector('i')?.textContent).toBe('1')
    expect(catRows()).toEqual(['DeepSeek V4 Flash'])

    await act(async () => {
      fireEvent.change(document.querySelector<HTMLInputElement>('.msearch')!, { target: { value: '' } })
    })
    await act(async () => {
      kind('gui.model.type.embedding').click()
    })
    expect(catRows()).toEqual(['DeepSeek Embed'])
  })

  it('adds one row with its tags and flips it where it stands', async () => {
    const { calls } = await openCatalogue()
    await act(async () => {
      [...document.querySelectorAll<HTMLButtonElement>('.mdrawer .mitem .rm')][1]!.click()
    })
    expect(calls).toContainEqual([
      'provider',
      {
        op: 'add_model',
        params: {
          slug: 'anthropic',
          model: 'deepseek/deepseek-v4-flash',
          label: 'DeepSeek V4 Flash',
          capabilities: ['function-call'],
        },
      },
    ])
    /* The list is the drawer's own state, so a snapshot refresh does not touch
       it -- without the flip the row a person just added still offers to. */
    expect([...document.querySelectorAll('.mdrawer .mitem .rm')].map((b) => b.textContent)).toEqual(['−', '−', '+'])
  })

  const groups = (): (string | null | undefined)[] =>
    [...document.querySelectorAll('.mdrawer .mgroup .gn')].map((g) => g.textContent)

  it('cuts the fetched list into vendor groups when the ids are filed under vendors', async () => {
    await openCatalogue(
      {
        fetchModels: async () => ({
          models: [
            { id: 'siliconflow/BAAI/bge-m3', label: 'BGE M3', kind: 'embedding', added: false },
            { id: 'siliconflow/BAAI/bge-reranker', label: 'BGE Reranker', kind: 'reranker', added: false },
            { id: 'siliconflow/Qwen/Qwen3-32B', label: 'Qwen3 32B', kind: 'text', added: false },
          ],
          status: 'ok',
        }),
      },
      { providers: [{ id: 'siliconflow', name: 'SiliconFlow', on: true, kind: 'api_key', models: [], configured: [] }] },
    )
    expect(groups()).toEqual(['BAAI', 'Qwen'])
    /* Published with capitals, so it is shown exactly as published. */
    expect([...document.querySelectorAll('.mdrawer .mgroup .gc')].map((g) => g.textContent)).toEqual(['2', '1'])
    /* Every row is still there, in the order it arrived. */
    expect(catRows()).toEqual(['BGE M3', 'BGE Reranker', 'Qwen3 32B'])
  })

  it('spells a lowercased vendor the way the vendor does', async () => {
    await openCatalogue(
      {
        fetchModels: async () => ({
          models: [
            { id: 'openrouter/openai/gpt-6', label: 'GPT-6', kind: 'text', added: false },
            { id: 'openrouter/x-ai/grok-4', label: 'Grok 4', kind: 'text', added: false },
            { id: 'openrouter/deepseek-ai/DeepSeek-V3', label: 'DeepSeek V3', kind: 'text', added: false },
            { id: 'openrouter/novita/some-model', label: 'Some Model', kind: 'text', added: false },
          ],
          status: 'ok',
        }),
      },
      { providers: [{ id: 'openrouter', name: 'OpenRouter', on: true, kind: 'api_key', models: [], configured: [] }] },
    )
    /* Gateways hand ids over lowercased, and a heading reading "openai" is a
       path fragment rather than a name. The last one has no entry in the table
       and no capital of its own, so it is title-cased. */
    expect([...document.querySelectorAll('.mdrawer .mgroup .gn')].map((g) => g.textContent)).toEqual([
      'OpenAI',
      'xAI',
      'DeepSeek',
      'Novita',
    ])
  })

  it('keeps an acronym an acronym when the id arrives lowercased', async () => {
    await openCatalogue(
      {
        fetchModels: async () => ({
          models: [
            /* SiliconFlow publishes `BAAI/bge-m3` and OpenRouter publishes
               `baai/bge-m3`; they are the same vendor and title-casing the
               second one read as "Baai". */
            { id: 'openrouter/baai/bge-m3', label: 'BGE M3', kind: 'embedding', added: false },
            { id: 'openrouter/thudm/glm-4', label: 'GLM-4', kind: 'text', added: false },
            { id: 'openrouter/ibm-granite/granite-4', label: 'Granite 4', kind: 'text', added: false },
          ],
          status: 'ok',
        }),
      },
      { providers: [{ id: 'openrouter', name: 'OpenRouter', on: true, kind: 'api_key', models: [], configured: [] }] },
    )
    expect([...document.querySelectorAll('.mdrawer .mgroup .gn')].map((g) => g.textContent)).toEqual([
      'BAAI',
      'THUDM',
      'IBM Granite',
    ])
  })

  const openGrouped = () =>
    openCatalogue(
      {
        fetchModels: async () => ({
          models: [
            { id: 'siliconflow/BAAI/bge-m3', label: 'BGE M3', kind: 'embedding', added: false },
            { id: 'siliconflow/BAAI/bge-reranker', label: 'BGE Reranker', kind: 'reranker', added: true },
            { id: 'siliconflow/Qwen/Qwen3-32B', label: 'Qwen3 32B', kind: 'text', added: false },
          ],
          status: 'ok',
        }),
      },
      { providers: [{ id: 'siliconflow', name: 'SiliconFlow', on: true, kind: 'api_key', models: [], configured: [] }] },
    )
  const groupHead = (name: string): HTMLElement =>
    [...document.querySelectorAll<HTMLElement>('.mdrawer .mgroup')].find(
      (g) => g.querySelector('.gn')?.textContent === name,
    )!

  it('folds a vendor away and brings it back', async () => {
    await openGrouped()
    expect(catRows()).toEqual(['BGE M3', 'BGE Reranker', 'Qwen3 32B'])

    await act(async () => {
      groupHead('BAAI').querySelector<HTMLButtonElement>('.gt')!.click()
    })
    /* A gateway lists a hundred models under a dozen vendors and the reader
       wants one of them; folding is how the other eleven get out of the way.
       The heading stays -- with its count, so what was folded is still legible. */
    expect(catRows()).toEqual(['Qwen3 32B'])
    expect(groupHead('BAAI').querySelector('.gc')?.textContent).toBe('2')
    expect(groupHead('BAAI').querySelector('.gt')?.getAttribute('aria-expanded')).toBe('false')

    await act(async () => {
      groupHead('BAAI').querySelector<HTMLButtonElement>('.gt')!.click()
    })
    expect(catRows()).toEqual(['BGE M3', 'BGE Reranker', 'Qwen3 32B'])
  })

  it('adds a whole vendor at once, and offers nothing where there is nothing left', async () => {
    const { calls } = await openGrouped()
    await act(async () => {
      groupHead('BAAI').querySelector<HTMLButtonElement>('.ga')!.click()
    })
    /* Only the one that was missing: the group button adds what is not there,
       it does not re-add what is. */
    const added = calls.filter(([op, a]) => op === 'provider' && (a as { op: string }).op === 'add_model')
    expect(added.length).toBe(1)
    expect((added[0]![1] as { params: { model: string } }).params.model).toBe('siliconflow/BAAI/bge-m3')
    expect(groupHead('BAAI').querySelector<HTMLButtonElement>('.ga')!.disabled).toBe(true)
  })

  it('gives the added list a fold and no group add, since there is nothing to add there', async () => {
    await openModels({
      providers: [
        {
          id: 'siliconflow', name: 'SiliconFlow', on: true, kind: 'api_key', models: [],
          configured: ['siliconflow/BAAI/bge-m3', 'siliconflow/Qwen/Qwen3-32B'],
        },
      ],
    })
    const head = [...document.querySelectorAll<HTMLElement>('.mpanel .mgroup')][0]!
    expect(head.querySelector('.ga')).toBeNull()
    await act(async () => {
      head.querySelector<HTMLButtonElement>('.gt')!.click()
    })
    expect([...document.querySelectorAll('.mpanel .mlist .mitem .nm')].map((n) => n.textContent)).toEqual([
      'Qwen3-32B',
    ])
  })

  it('gives each model on a flat shelf its own maker mark', async () => {
    await openCatalogue(
      {
        fetchModels: async () => ({
          models: [
            { id: 'dashscope/qwen-plus', label: 'Qwen Plus', kind: 'text', added: false },
            { id: 'dashscope/deepseek-v4-flash', label: 'DeepSeek V4 Flash', kind: 'text', added: false },
            { id: 'dashscope/glm-5.2', label: 'GLM 5.2', kind: 'text', added: false },
            { id: 'dashscope/text-embedding-v4', label: 'Text Embedding v4', kind: 'embedding', added: false },
          ],
          status: 'ok',
        }),
      },
      { providers: [{ id: 'dashscope', name: 'Alibaba Cloud', on: true, kind: 'api_key', models: [], configured: [] }] },
    )
    /* Alibaba Cloud publishes flat ids, so there is no namespace to read and
       every row wore the same provider mark. The last one is Alibaba's own
       model and correctly keeps it. */
    expect([...document.querySelectorAll('.mdrawer .mitem img')].map((i) => i.getAttribute('src'))).toEqual([
      'assets/providers/qwen.svg',
      'assets/providers/deepseek.svg',
      'assets/providers/zai.svg',
      'assets/providers/alibabacloud.svg',
    ])
  })

  it('does not repeat the vendor its heading already names', async () => {
    await openCatalogue(
      {
        fetchModels: async () => ({
          models: [
            /* No registry row, so the id is the name -- and it leads with the
               provider and the vendor, both of which are already on screen. */
            { id: 'siliconflow/ByteDance-Seed/Seed-OSS-36B', label: '', kind: 'text', added: false },
            /* Upstream writes the vendor into the name for some of these. */
            { id: 'siliconflow/BAAI/bge-large-zh', label: 'BAAI: BGE Large ZH v1.5', kind: 'embedding', added: false },
            { id: 'siliconflow/BAAI/bge-m3', label: 'BGE M3', kind: 'embedding', added: false },
          ],
          status: 'ok',
        }),
      },
      { providers: [{ id: 'siliconflow', name: 'SiliconFlow', on: true, kind: 'api_key', models: [], configured: [] }] },
    )
    expect(catRows()).toEqual(['Seed-OSS-36B', 'BGE Large ZH v1.5', 'BGE M3'])
    /* The full id is still one hover away, since that is what a request
       carries and what the vendor's docs are keyed by. */
    expect(document.querySelector('.mdrawer .mitem')?.getAttribute('title')).toBe(
      'siliconflow/ByteDance-Seed/Seed-OSS-36B',
    )
  })

  it('still names an ungrouped model without its provider', async () => {
    await openCatalogue(
      {
        fetchModels: async () => ({
          models: [{ id: 'deepseek/deepseek-v4-pro', label: '', kind: 'text', added: false }],
          status: 'ok',
        }),
      },
      { providers: [{ id: 'deepseek', name: 'DeepSeek', on: true, kind: 'api_key', models: [], configured: [] }] },
    )
    /* No heading to be redundant with, but the panel is already titled with
       the provider. */
    expect(catRows()).toEqual(['deepseek-v4-pro'])
  })

  it('leaves a list alone when there is nothing to group it by', async () => {
    await openCatalogue({
      fetchModels: async () => ({
        models: [
          { id: 'anthropic/claude-opus-5', label: 'Claude Opus 5', kind: 'text', added: false },
          { id: 'anthropic/claude-sonnet-5', label: 'Claude Sonnet 5', kind: 'text', added: false },
        ],
        status: 'ok',
      }),
    })
    /* One heading over the whole list names what the panel already says. */
    expect(groups()).toEqual([])
    expect(catRows()).toEqual(['Claude Opus 5', 'Claude Sonnet 5'])
  })

  it('groups the added list the same way', async () => {
    await openModels({
      providers: [
        {
          id: 'siliconflow', name: 'SiliconFlow', on: true, kind: 'api_key',
          models: [],
          configured: ['siliconflow/BAAI/bge-m3', 'siliconflow/Qwen/Qwen3-32B', 'siliconflow/Qwen/Qwen3-8B'],
        },
      ],
    })
    expect([...document.querySelectorAll('.mpanel .mgroup .gn')].map((g) => g.textContent)).toEqual(['BAAI', 'Qwen'])
    expect([...document.querySelectorAll('.mpanel .mlist .mitem .nm')].map((n) => n.textContent)).toEqual([
      'bge-m3',
      'Qwen3-32B',
      'Qwen3-8B',
    ])
  })

  it('holds its place while a row is added', async () => {
    await openCatalogue()
    await act(async () => {
      fireEvent.change(document.querySelector<HTMLInputElement>('.msearch')!, { target: { value: 'deepseek v4' } })
    })
    const pane = document.querySelector('.mdrawer .dpane')
    expect(catRows()).toEqual(['DeepSeek V4 Pro', 'DeepSeek V4 Flash'])

    await act(async () => {
      [...document.querySelectorAll<HTMLButtonElement>('.mdrawer .mitem .rm')][1]!.click()
    })
    /* A write bumps `epoch`, which remounts the settings panel so its
       uncontrolled fields restart from the reloaded values. The drawer lives
       outside that subtree for exactly this: remounted, it replayed its
       slide-in and threw away the search on every row added. */
    expect(document.querySelector('.mdrawer .dpane')).toBe(pane)
    expect(document.querySelector<HTMLInputElement>('.msearch')!.value).toBe('deepseek v4')
    expect(catRows()).toEqual(['DeepSeek V4 Pro', 'DeepSeek V4 Flash'])
  })

  it('closes with the page it belongs to when another section is opened', async () => {
    await openCatalogue()
    expect(document.querySelector('.mdrawer')).toBeTruthy()
    await act(async () => {
      screen.getByText('gui.set.pg.perm').click()
    })
    /* Left open across a tab change it would come back over whatever section
       is showing, addressed to a provider nobody is looking at any more. */
    expect(document.querySelector('.mdrawer')).toBeNull()
  })

  it('adds every row the filter is showing, one write at a time', async () => {
    const { calls } = await openCatalogue()
    await act(async () => {
      document.querySelector<HTMLButtonElement>('.mdrawer footer .mini:not(.ghost)')!.click()
    })
    /* Sequential, because each write rewrites the config file: fired together
       they race and the last writer wins with a list missing the rest. */
    const added = calls.filter(([op, a]) => op === 'provider' && (a as { op: string }).op === 'add_model')
    expect(added.length).toBe(2)
    expect(document.querySelector<HTMLButtonElement>('.mdrawer footer .mini:not(.ghost)')!.disabled).toBe(true)
  })

  it('says why the vendor did not answer instead of showing an empty list', async () => {
    await openCatalogue({ fetchModels: async () => ({ models: [], status: 'unauthorized', error: 'HTTP 401' }) })
    /* A blank list would claim the provider serves nothing, which is a
       different and much calmer statement than "the key was refused". */
    expect(document.querySelector('.mdrawer .perr')?.textContent).toBe('HTTP 401')
    expect(document.querySelector('.mdrawer .mlist')).toBeNull()
  })

  it('shows the bundled catalogue with a note when the provider was not asked', async () => {
    await openCatalogue({
      fetchModels: async () => ({
        models: [{ id: 'deepseek/deepseek-v4-pro', label: 'DeepSeek V4 Pro', kind: 'text', added: false, source: 'registry' }],
        status: 'not_configured',
        error: 'api_key is empty',
      }),
    })
    /* A provider with no key yet is the ordinary state of one being set up.
       The rows are real; only their currency is in question, so this is a note
       beside them and not an error instead of them. */
    expect(document.querySelector('.mdrawer .mnote')?.textContent).toContain('gui.model.cat_note_key')
    expect(document.querySelector('.mdrawer .perr')).toBeNull()
    expect(catRows()).toEqual(['DeepSeek V4 Pro'])
  })

  it('reports a demo source that cannot fetch rather than pretending to', async () => {
    const h = install(snap())
    await mount()
    await act(async () => {
      screen.getByText('gui.set.pg.model').click()
    })
    await act(async () => {
      document.querySelectorAll<HTMLButtonElement>('.mpanel .mbtns button')[0]!.click()
    })
    expect(document.querySelector('.mdrawer .perr')?.textContent).toBe('gui.set.not_live')
    expect(h.calls.filter(([op]) => op === 'provider')).toEqual([])
  })

  it('lists each model as a row with its icons and a way out, and nothing when there are none', async () => {
    await openModels({
      providers: [
        {
          id: 'anthropic', name: 'Anthropic', on: true, kind: 'api_key',
          /* The offer and the list are different questions: `models` is what
             the picker may show (a curated shortlist folded in), `configured`
             is what this section actually holds. */
          models: ['claude-opus-5', 'claude-sonnet-5'],
          configured: ['claude-opus-5'],
          labels: { 'claude-opus-5': { label: 'Claude Opus 5', capabilities: ['reasoning'] } },
        },
        { id: 'openai', name: 'OpenAI', models: ['gpt-5.2'], configured: [], on: true, kind: 'api_key' },
      ],
    })
    const rows = [...document.querySelectorAll('.mlist .mitem')]
    expect(rows.length).toBe(1)
    const row = rows[0]!
    expect(row.querySelector('.nm')?.textContent).toBe('Claude Opus 5')
    expect(row.querySelector('.model-tag use')?.getAttribute('href')).toBe('#mtag-reasoning')
    expect(row.querySelector('.rm')).toBeTruthy()

    const openai = [...document.querySelectorAll<HTMLButtonElement>('.mrail .mrow')].find(
      (r) => r.querySelector('.nm')?.textContent === 'OpenAI',
    )!
    await act(async () => {
      openai.click()
    })
    /* Blank, not a form: adding is the drawer's job now, and an inline field
       beside a `+` that opens one is two doors to the same room. And blank
       even though the picker would offer this provider a model -- the section
       lists what was added to it, not what could be. */
    expect(document.querySelector('.mlist')).toBeNull()
    expect(document.querySelector('[data-sec="models"] input')).toBeNull()
  })

  const openDrawer = async (over: Partial<SettingsSnapshot> = {}) => {
    const h = await openModels(over)
    await act(async () => {
      document.querySelectorAll<HTMLButtonElement>('.mpanel .mbtns button')[1]!.click()
    })
    return h
  }
  const toggle = (group: number, label: string): HTMLButtonElement => {
    const grp = document.querySelectorAll<HTMLElement>('.mdrawer .grp')[group]!
    return [...grp.querySelectorAll<HTMLButtonElement>('button.mtog')].find((b) => b.textContent === label)!
  }
  const pressed = (group: number): (string | null)[] =>
    [...document.querySelectorAll<HTMLElement>('.mdrawer .grp')[group]!.querySelectorAll('button.mtog')]
      .filter((b) => b.getAttribute('aria-pressed') === 'true')
      .map((b) => b.textContent)

  it('opens the drawer from + and names the provider it will write to', async () => {
    await openDrawer()
    const pane = document.querySelector('.mdrawer .dpane')!
    expect(pane.getAttribute('aria-label')).toBe('gui.model.add_title')
    expect(pane.querySelector('header .who')?.textContent).toBe('Anthropic')
    /* Portalled into the dialog, not into the scrolling panel: it is a sheet
       over the whole settings box. */
    expect(document.querySelector('#setModal > .mdrawer')).toBeTruthy()
  })

  it('asks four short questions, each drawn with the icon its answer becomes', async () => {
    await openDrawer()
    const groups = [...document.querySelectorAll('.mdrawer .grp')].map((g) => g.querySelector('.gt')?.textContent)
    /* Three groups, and no output row: the kind already answers what a model
       writes, and asking twice only creates a pair that can disagree. */
    expect(groups).toEqual(['gui.model.add_type', 'gui.model.add_caps', 'gui.model.add_in'])
    expect(document.querySelectorAll('.mdrawer .grp button.mtog').length).toBe(9)
    /* Every toggle draws from the same sprite the list rows use: ticking a
       brain and then seeing a sparkle would be two names for one thing. */
    const first = document.querySelector('.mdrawer .grp button.mtog')!
    expect(first.querySelector('use')?.getAttribute('href')).toBe('#mtag-text')
    expect(first.textContent).toBe('gui.model.type.text')
  })

  it('takes one kind at a time and starts on text', async () => {
    await openDrawer()
    expect(pressed(0)).toEqual(['gui.model.type.text'])
    await act(async () => {
      toggle(0, 'gui.model.type.embedding').click()
    })
    /* A model is one kind of thing, so picking a second replaces the first
       rather than adding to it. */
    expect(pressed(0)).toEqual(['gui.model.type.embedding'])
  })

  it('clears and closes the other rows when the kind answers with numbers', async () => {
    await openDrawer()
    await act(async () => {
      toggle(1, 'gui.model.cap.reasoning').click()
      toggle(2, 'gui.model.in.vision').click()
    })
    expect(pressed(1)).toEqual(['gui.model.cap.reasoning'])

    await act(async () => {
      toggle(0, 'gui.model.type.embedding').click()
    })
    /* An embedding model does not reason and calls no tools. Cleared and shut,
       not merely cleared: left open, the form could state one that does, and
       the icon row would then draw it. */
    expect(pressed(1)).toEqual([])
    expect(pressed(2)).toEqual([])
    expect(toggle(1, 'gui.model.cap.reasoning').disabled).toBe(true)
    expect(toggle(2, 'gui.model.in.vision').disabled).toBe(true)

    /* And they open again for a kind that answers with a turn. */
    await act(async () => {
      toggle(0, 'gui.model.type.text').click()
    })
    expect(toggle(1, 'gui.model.cap.reasoning').disabled).toBe(false)
  })

  it('sends the id and the stated tags, with the rest derived from the kind', async () => {
    const { calls } = await openDrawer()
    const id = document.querySelector<HTMLInputElement>('.mdrawer input')!
    await act(async () => {
      fireEvent.change(id, { target: { value: ' my-finetune-v3 ' } })
    })
    await act(async () => {
      toggle(1, 'gui.model.cap.reasoning').click()
      toggle(2, 'gui.model.in.vision').click()
    })
    await act(async () => {
      document.querySelector<HTMLButtonElement>('.mdrawer footer .mini:not(.ghost)')!.click()
    })
    expect(calls).toContainEqual([
      'provider',
      {
        op: 'add_model',
        params: {
          slug: 'anthropic',
          model: 'my-finetune-v3',
          /* Vision was ticked once and reaches the wire as the capability it
             means, beside the modality it was ticked as. */
          capabilities: ['reasoning', 'image-recognition'],
          input_modalities: ['text', 'image'],
          output_modalities: ['text'],
        },
      },
    ])
    /* Landed, so the drawer is done. */
    expect(document.querySelector('.mdrawer')).toBeNull()
  })

  it('records an embedding model as answering in vectors', async () => {
    const { calls } = await openDrawer()
    await act(async () => {
      fireEvent.change(document.querySelector<HTMLInputElement>('.mdrawer input')!, { target: { value: 'e5-large' } })
    })
    await act(async () => {
      toggle(0, 'gui.model.type.embedding').click()
    })
    await act(async () => {
      document.querySelector<HTMLButtonElement>('.mdrawer footer .mini:not(.ghost)')!.click()
    })
    const sent = calls.find(([op, args]) => op === 'provider' && (args as { op: string }).op === 'add_model')!
    expect((sent[1] as { params: Record<string, unknown> }).params).toMatchObject({
      capabilities: ['embedding'],
      output_modalities: ['vector'],
    })
  })

  it('will not send an empty id, and closes on cancel without writing', async () => {
    const { calls } = await openDrawer()
    expect(document.querySelector<HTMLButtonElement>('.mdrawer footer .mini:not(.ghost)')!.disabled).toBe(true)
    await act(async () => {
      document.querySelector<HTMLButtonElement>('.mdrawer footer .mini.ghost')!.click()
    })
    expect(document.querySelector('.mdrawer')).toBeNull()
    expect(calls.filter(([op]) => op === 'provider')).toEqual([])
  })

  it('links the vendor model docs beside the list, and only where there are any', async () => {
    await openModels({
      providers: [
        { id: 'deepseek', name: 'DeepSeek', homepage: 'https://deepseek.com/', docs: 'https://api-docs.deepseek.com/', models: [], on: true, kind: 'api_key' },
        { id: 'openai', name: 'OpenAI', models: [], on: true, kind: 'api_key' },
      ],
    })
    /* Addressed through the models block: the key block carries a link of the
       same shape ("Get API Key"), and the two answer different questions. */
    const docs = document.querySelector('[data-sec="models"] .mdocs')!
    expect(docs.getAttribute('href')).toBe('https://api-docs.deepseek.com/')
    /* Drawn, not written -- so the name has to survive somewhere a reader who
       cannot see the drawing will find it. */
    expect(docs.querySelector('svg')).toBeTruthy()
    expect(docs.textContent).toBe('')
    expect(docs.getAttribute('aria-label')).toBe('gui.model.docs')

    const openai = [...document.querySelectorAll<HTMLButtonElement>('.mrail .mrow')].find(
      (row) => row.querySelector('.nm')?.textContent === 'OpenAI',
    )!
    await act(async () => {
      openai.click()
    })
    /* No registry docs link for this one, so the row simply has no link --
       the homepage is not a model index and must not stand in for one. */
    expect(document.querySelector('[data-sec="models"] .mdocs')).toBeNull()
  })

  it('keeps the default-model card above the split', async () => {
    await openModels()
    const card = document.querySelector('#spanels .scard')!
    const split = document.querySelector('#spanels .msplit')!
    expect(card.querySelector('.pickm')).toBeTruthy()
    expect(card.compareDocumentPosition(split) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
