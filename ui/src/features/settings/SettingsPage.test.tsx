// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SettingsApp } from './SettingsPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { SettingsSnapshot, SettingsSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

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
    ],
    curProvider: 'anthropic',
    model: 'claude-opus-4-5',
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
    everosSet: async (section, fields) => {
      calls.push(['everosSet', { section, fields }])
      return data
    },
    usage: async () => null,
    provider: async (op, params) => {
      calls.push(['provider', { op, params }])
      return data
    },
    model: () => data.model,
    ...over,
  }
  const shellCalls: Array<[string, unknown]> = []
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    toast: (text) => shellCalls.push(['toast', text]),
    menuAt: (_x, _y, items) => shellCalls.push(['menuAt', items]),
    /* Confirms immediately: the dialog itself is legacy chrome, not island. */
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: (id) => shellCalls.push(['showPage', id]),
    openSet: () => shellCalls.push(['openSet', null]),
    closeSet: () => shellCalls.push(['closeSet', null]),
    openConn: () => shellCalls.push(['openConn', null]),
    sessionCount: () => 3,
  }
  window.RavenShell = fakeShell
  /* The danger card's button is a SESSION operation offered from this page, so
     it goes out through DS.sessions rather than this page's own source. */
  const wiped: Array<null> = []
  window.DS = {
    settings: source,
    sessions: { snapshot: () => ({ rows: [], cur: null, busy: false }), deleteAll: () => wiped.push(null) },
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
})
