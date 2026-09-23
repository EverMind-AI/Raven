// @vitest-environment happy-dom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as detail from '../../state/detail'
import * as pageStore from '../../state/page'
import { resetSources, setSources } from '../../state/sources'
import { domSnapshot } from '../../test/domSnapshot'
import { ExtAgentsApp } from './ExtAgentsPage'
import * as store from './store'

import type { ExtAgentActArgs, ExtAgentRow, ExtAgentsSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const toastWriter = vi.hoisted(() => ({ items: [] as string[] }))
vi.mock('../../state/toast', () => ({
  show: (text: string) => {
    toastWriter.items.push(text)
  },
}))

/* The host's provider list, which the built-in row's pill picks from. */
const hostModels = vi.hoisted(() => ({
  providers: [] as Array<{ id: string; name: string; models: string[]; configured?: string[]; on: boolean }>,
  loads: 0,
}))
vi.mock('../model/source', () => ({
  defaultProviders: () => hostModels.providers,
  loadDefaultProviders: async () => {
    hostModels.loads += 1
  },
}))

function row(over: Partial<ExtAgentRow> = {}): ExtAgentRow {
  return {
    name: 'claude_code',
    preset: 'claude_code',
    kind: 'acp',
    configured: true,
    enabled: true,
    probe_status: 'ready',
    probe_detail: '',
    has_api_key: false,
    description: 'Claude Code CLI',
    test_running: false,
    last_test_ok: null,
    last_test_at_ms: null,
    last_test_detail: '',
    upgrade_to: null,
    ...over,
  }
}

/* The island runs against the same two seams production wires: a stand-in
   translator on setTranslator (it returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on sources.extAgents. A write
   can be told to refuse, which is how the refusal states are reached. */
function install(rows: ExtAgentRow[], over: Partial<ExtAgentsSource> & { refuse?: (op: string) => string | null } = {}) {
  const acts: Array<[string, string, ExtAgentActArgs]> = []
  const loads: boolean[] = []
  const { refuse, ...rest } = over
  const source: ExtAgentsSource = {
    load: async (probe) => {
      loads.push(!!probe)
      return rows
    },
    act: async (op, r, args) => {
      acts.push([op, r.name, args || {}])
      const why = refuse ? refuse(op) : null
      if (why) throw { data: { detail: why } }
      /* Both real sources answer with the row as the write left it -- the rpc
         one by reloading the roster, the fixture one by mutating in place. */
      if (op === 'toggle') r.enabled = !!(args as { enabled?: boolean } | undefined)?.enabled
      if (op === 'connect') {
        r.configured = true
        r.enabled = true
      }
      if (op === 'update' && args?.description !== undefined) r.description = args.description
      return rows
    },
    ...rest,
  }
  const toasts: string[] = []
  toastWriter.items = toasts
  const confirms: string[] = []
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation((title, _b, _l, fn) => {
    confirms.push(title)
    fn()
  })
  setSources({ extAgents: source })
  document.body.innerHTML =
    '<section id="extAgentsPage"><div id="extAgentsBody"></div></section>' +
    '<aside id="detail" data-open="false"><b id="dTitle">—</b><div id="dBody"></div></aside>' +
    '<div id="menu" data-open="false"></div>'
  return { source, acts, loads, toasts, confirms }
}

async function mount() {
  const view = render(<ExtAgentsApp />, { container: document.getElementById('extAgentsBody')! })
  await act(async () => {
    store.open()
  })
  return view
}

const rowsOf = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.extAgents-row')]
const rowNamed = (name: string): HTMLElement =>
  rowsOf().find((r) => r.querySelector('.extAgents-t')!.textContent === name)!
const sectionOf = (name: string): string =>
  rowNamed(name).closest('.extAgents-sec')!.querySelector('.extAgents-hd b')!.textContent!
const sectionLabels = (): string[] => [...document.querySelectorAll('.extAgents-hd b')].map((b) => b.textContent!)
const controlOf = (name: string): string | null => rowNamed(name).querySelector('.extAgents-ctl')!.textContent
const buttonOf = (name: string): HTMLButtonElement | null => rowNamed(name).querySelector('.extAgents-ctl button')
const lineOf = (name: string): string | null => rowNamed(name).querySelector('.extAgents-one')!.textContent
const ledOf = (name: string): string | null => rowNamed(name).querySelector('.extAgents-nm .extAgents-led')?.className ?? null
const namesIn = (label: string): string[] => {
  const sec = [...document.querySelectorAll('.extAgents-sec')].find((s) => s.querySelector('.extAgents-hd b')!.textContent === label)!
  return [...sec.querySelectorAll('.extAgents-t')].map((n) => n.textContent!)
}

const sheet = (): HTMLElement | null => document.querySelector('#dBody .extAgents-sheet')
const sheetName = (): string | null => sheet()?.querySelector('h3')?.textContent ?? null
const sheetStatus = (): string | null => sheet()?.querySelector('.extAgents-by')?.textContent ?? null
const sheetActs = (): Array<string | null> => [...(sheet()?.querySelectorAll('.extAgents-act button') ?? [])].map((b) => b.textContent)
const sheetTextarea = (): HTMLTextAreaElement | null => sheet()?.querySelector('textarea') ?? null

async function openSheet(name: string): Promise<void> {
  await act(async () => {
    rowNamed(name).click()
  })
}

const click = async (el: Element | null | undefined): Promise<void> => {
  expect(el, 'the control under test').toBeTruthy()
  await act(async () => {
    ;(el as HTMLElement).click()
  })
}

/* Enter on a control, the way a keyboard reaches it: keydown first (which is
   what the row listens for), then the activation the browser derives from it.
   `preventDefault` on the keydown is exactly what suppresses that activation,
   so the order matters and a plain `.click()` would not see the bug. */
const pressEnter = async (el: Element | null | undefined): Promise<void> => {
  expect(el, 'the control under test').toBeTruthy()
  await act(async () => {
    const target = el as HTMLElement
    const ev = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
    const prevented = !target.dispatchEvent(ev)
    if (!prevented) target.click()
  })
}

/* React installs its own `value` setter on the element, so assigning `.value`
   and firing `input` leaves its state untouched and the field reverts on the
   next render. The native setter is what React's onChange reads back. */
const typeInto = async (el: Element | null | undefined, text: string): Promise<void> => {
  expect(el, 'the field under test').toBeTruthy()
  const field = el as HTMLInputElement | HTMLTextAreaElement
  const proto = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')!.set!
  await act(async () => {
    setter.call(field, text)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

/* React delegates `onBlur` from the root and listens for `focusout`, not for
   the non-bubbling `blur`. */
const blur = async (el: Element | null | undefined): Promise<void> => {
  expect(el, 'the field under test').toBeTruthy()
  await act(async () => {
    ;(el as HTMLElement).dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
}

afterEach(() => {
  /* The real close path, not the island's closer alone: `sheetDismissed` arms a
     drop keyed on the drawer's generation, and only a close makes the next
     open a new generation. */
  act(() => {
    detail.close()
  })
  cleanup()
  resetSources()
  store._resetForTests()
})

describe('the three sections', () => {
  it('files every row under connected, available or not installed', async () => {
    install([
      row({ name: 'Raven', preset: undefined, kind: 'builtin', builtin: true, configured: false, probe_status: 'unknown' }),
      row({ name: 'Raven-Code', preset: undefined, vendored: true, configured: false, enabled: true }),
      row({ name: 'on_one' }),
      row({ name: 'off_one', enabled: false }),
      row({ name: 'hermes', preset: 'hermes', configured: false, enabled: false, probe_status: 'attention' }),
      row({ name: 'codex', preset: 'codex', configured: false, enabled: false, probe_status: 'missing' }),
      row({ name: 'mirothinker', preset: 'mirothinker', kind: 'openai', configured: false, enabled: false, probe_status: 'missing' }),
      row({ name: 'Raven-PPT', preset: undefined, vendored: true, configured: false, enabled: false, probe_status: 'attention' }),
    ])
    await mount()
    expect(sectionLabels()).toEqual(['gui.agent.g_on', 'gui.agent.g_avail', 'gui.agent.g_missing'])
    expect(namesIn('gui.agent.g_on')).toEqual(['Raven', 'Raven-Code', 'on_one'])
    /* Raven's own first, then the server's order. An endpoint whose probe says
       unreachable is not something to install; a shipped product is never
       absent, only unready, and both stay connectable. */
    expect(namesIn('gui.agent.g_avail')).toEqual(['Raven-PPT', 'off_one', 'hermes', 'mirothinker'])
    expect(namesIn('gui.agent.g_missing')).toEqual(['codex'])
  })

  it('always draws the connected section, and the other two only with rows in them', async () => {
    install([row({ name: 'codex', preset: 'codex', configured: false, enabled: false, probe_status: 'missing' })])
    await mount()
    expect(sectionLabels()).toEqual(['gui.agent.g_on', 'gui.agent.g_missing'])
    expect(document.querySelector('.extAgents-sec .extAgents-n')!.textContent).toBe('0')
  })

  it('re-measures availability every time the page opens', async () => {
    const { loads } = install([row()])
    await mount()
    await act(async () => {
      store.open()
    })
    expect(loads).toEqual([true, true])
  })
})

describe('a row', () => {
  it('says who it is in one line: the catalogue for a known agent, the probe or the transport otherwise', async () => {
    install([
      row({ name: 'claude_code' }),
      row({ name: 'Raven-Code', preset: undefined, vendored: true, configured: false }),
      row({ name: 'homemade', preset: undefined, configured: true, enabled: false, probe_status: 'attention', probe_detail: 'found, untested' }),
      row({ name: 'plain', preset: undefined, configured: true, enabled: true }),
      row({ name: 'moved', preset: undefined, configured: true, enabled: false, upgrade_to: 'acp' }),
    ])
    await mount()
    expect(lineOf('claude_code')).toBe('gui.agent.short_claude_code')
    expect(lineOf('Raven-Code')).toBe('gui.agent.short_raven_code')
    expect(lineOf('homemade')).toBe('found, untested')
    expect(lineOf('plain')).toBe('gui.agent.kind_acp')
    expect(lineOf('moved')).toBe('gui.agent.kind_acp · gui.agent.tag_stale')
  })

  it('carries one control, by state, and none for the built-in loop', async () => {
    install([
      row({ name: 'Raven', preset: undefined, kind: 'builtin', builtin: true, configured: false, probe_status: 'unknown' }),
      row({ name: 'on_one' }),
      row({ name: 'off_one', enabled: false }),
      row({ name: 'codex', preset: 'codex', configured: false, enabled: false, probe_status: 'missing' }),
    ])
    await mount()
    expect(controlOf('Raven')).toBe('')
    expect(controlOf('on_one')).toBe('gui.agent.disconnect')
    expect(controlOf('off_one')).toBe('gui.agent.connect')
    expect(controlOf('codex')).toBe('gui.agent.go_install')
    const texts = new Set(rowsOf().flatMap((r) => [...r.querySelectorAll('button')].map((b) => b.textContent)))
    expect(texts).toEqual(new Set(['gui.agent.disconnect', 'gui.agent.connect', 'gui.agent.go_install']))
  })

  it('lights the dot only for a working agent, gold when the probe has a caveat', async () => {
    install([
      row({ name: 'on_one' }),
      row({ name: 'on_warn', probe_status: 'attention' }),
      row({ name: 'off_one', enabled: false }),
      row({ name: 'codex', preset: 'codex', configured: false, enabled: false, probe_status: 'missing' }),
    ])
    await mount()
    expect(ledOf('on_one')).toBe('extAgents-led')
    expect(ledOf('on_warn')).toBe('extAgents-led extAgents-led-warn')
    expect(ledOf('off_one')).toBeNull()
    expect(ledOf('codex')).toBeNull()
  })

  it('opens the sheet from the row, by mouse and by keyboard, but not from its control', async () => {
    const { acts } = install([row({ name: 'on_one' }), row({ name: 'off_one', enabled: false })])
    await mount()
    await pressEnter(rowNamed('on_one'))
    expect(sheetName()).toBe('on_one')
    await act(async () => {
      detail.close()
    })
    await pressEnter(buttonOf('off_one'))
    expect(acts).toEqual([['toggle', 'off_one', { enabled: true }]])
    expect(sheetName()).not.toBe('off_one')
  })
})

describe('connecting', () => {
  it('adds a preset, switches a configured row back on, and migrates a stale one after asking', async () => {
    const { acts, confirms } = install([
      row({ name: 'hermes', preset: 'hermes', configured: false, enabled: false, probe_status: 'attention' }),
      row({ name: 'off_one', enabled: false }),
      row({ name: 'moved', configured: true, enabled: false, upgrade_to: 'acp' }),
    ])
    await mount()
    await click(buttonOf('hermes'))
    await click(buttonOf('off_one'))
    await click(buttonOf('moved'))
    expect(acts).toEqual([
      ['connect', 'hermes', {}],
      ['toggle', 'off_one', { enabled: true }],
      ['migrate', 'moved', {}],
    ])
    expect(confirms).toEqual(['gui.agent.migrate_do'])
  })

  it('sends a key-less endpoint to the sheet instead of writing', async () => {
    const { acts } = install([row({ name: 'mirothinker', preset: 'mirothinker', kind: 'openai', configured: false, enabled: false })])
    await mount()
    await click(buttonOf('mirothinker'))
    expect(acts).toEqual([])
    expect(sheetName()).toBe('mirothinker')
  })

  it('shows the row testing for the length of a switch-on, with nothing else to press', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const rows = [row({ name: 'off_one', enabled: false })]
    install(rows, {
      act: async (op, r, args) => {
        await gate
        if (op === 'toggle') r.enabled = !!(args as { enabled?: boolean } | undefined)?.enabled
        return rows
      },
    })
    await mount()
    await click(buttonOf('off_one'))
    expect(controlOf('off_one')).toBe('gui.agent.testing')
    expect(buttonOf('off_one')).toBeNull()
    expect(lineOf('off_one')).toBe('gui.agent.testing')
    expect(ledOf('off_one')).toBe('extAgents-led extAgents-led-busy')
    await act(async () => {
      release()
      await gate
    })
    expect(sectionOf('off_one')).toBe('gui.agent.g_on')
    expect(controlOf('off_one')).toBe('gui.agent.disconnect')
  })

  it('says testing on the sheet button too, since that is what the wait is', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const rows = [row({ name: 'off_one', enabled: false })]
    install(rows, {
      act: async (op, r, args) => {
        await gate
        if (op === 'toggle') r.enabled = !!(args as { enabled?: boolean } | undefined)?.enabled
        return rows
      },
    })
    await mount()
    await openSheet('off_one')
    await click(sheet()!.querySelector('.extAgents-act button'))
    expect(sheetActs()).toEqual(['gui.agent.testing'])
    await act(async () => {
      release()
      await gate
    })
  })

  /* The same `pending` covers every write this page makes, so the word has to
     come from which write it is. A disconnect reaches no gate at all -- the
     server pings only on the way on -- so it is neither testing the agent nor
     connecting to it. */
  it('says disconnecting while a disconnect is in flight', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const rows = [row({ name: 'on_one', configured: true, enabled: true })]
    install(rows, {
      act: async (op, r, args) => {
        await gate
        if (op === 'toggle') r.enabled = !!(args as { enabled?: boolean } | undefined)?.enabled
        return rows
      },
    })
    await mount()
    await click(buttonOf('on_one'))
    expect(controlOf('on_one')).toBe('gui.agent.disconnecting')
    await act(async () => {
      release()
      await gate
    })
  })

  it('keeps a refusal on the row in red with a retry, and does not toast it', async () => {
    let refusals = 1
    const { acts, toasts } = install([row({ name: 'off_one', enabled: false })], {
      refuse: () => (refusals-- > 0 ? 'it did not answer a test message' : null),
    })
    await mount()
    await click(buttonOf('off_one'))
    expect(lineOf('off_one')).toBe('it did not answer a test message')
    expect(rowNamed('off_one').querySelector('.extAgents-one')!.className).toContain('extAgents-one-bad')
    expect(controlOf('off_one')).toBe('gui.retry')
    expect(ledOf('off_one')).toBe('extAgents-led extAgents-led-bad')
    expect(toasts).toEqual([])
    await click(buttonOf('off_one'))
    expect(acts).toEqual([
      ['toggle', 'off_one', { enabled: true }],
      ['toggle', 'off_one', { enabled: true }],
    ])
    expect(sectionOf('off_one')).toBe('gui.agent.g_on')
  })

  it('still toasts when the load that opens the page is the thing that fails', async () => {
    const { toasts } = install([], {
      load: async () => {
        throw { data: { detail: 'gateway away' } }
      },
    })
    await mount()
    expect(toasts).toEqual(['gui.agent.failed {"detail":"gateway away"}'])
  })
})

describe('disconnecting', () => {
  it('marks the row unavailable and moves it to the available section', async () => {
    const { acts } = install([row({ name: 'on_one' })])
    await mount()
    await click(buttonOf('on_one'))
    expect(acts).toEqual([['toggle', 'on_one', { enabled: false }]])
    expect(sectionOf('on_one')).toBe('gui.agent.g_avail')
    expect(controlOf('on_one')).toBe('gui.agent.connect')
  })
})

describe('the sheet', () => {
  it('names the agent, says who makes it, and offers disconnect and test for a connected one', async () => {
    install([row({ name: 'claude_code' })])
    await mount()
    await openSheet('claude_code')
    expect(sheetName()).toBe('claude_code')
    expect(sheetStatus()).toBe('gui.agent.hd_on_by {"by":"Anthropic"}')
    expect(sheetActs()).toEqual(['gui.agent.disconnect', 'gui.agent.test_label'])
  })

  it('offers nothing to press on the built-in loop', async () => {
    install([row({ name: 'Raven', preset: undefined, kind: 'builtin', builtin: true, configured: false, probe_status: 'unknown' })])
    await mount()
    await openSheet('Raven')
    expect(sheetActs()).toEqual([])
    expect(sheetStatus()).toBe('gui.agent.hd_on_by {"by":"gui.agent.by_raven"}')
    expect(sheetTextarea()!.readOnly).toBe(true)
  })

  it('reads the last verdict into the status line', async () => {
    install([
      row({ name: 'passed', last_test_ok: true, last_test_at_ms: 1 }),
      row({ name: 'failed', last_test_ok: false, last_test_at_ms: 1, last_test_detail: 'it returned nothing' }),
    ])
    await mount()
    await openSheet('passed')
    expect(sheetStatus()).toBe('gui.agent.hd_on_tested')
    await act(async () => {
      detail.close()
    })
    await openSheet('failed')
    expect(sheetStatus()).toBe('gui.agent.hd_test_bad {"detail":"it returned nothing"}')
    expect(sheet()!.querySelector('.extAgents-by')!.className).toContain('extAgents-by-bad')
  })

  it('runs a test from its action bar, says so while it runs, and offers to stop it', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const rows = [row({ name: 'claude_code' })]
    const { acts } = install(rows, {
      act: async (op, r) => {
        acts.push([op, r.name, {}])
        if (op === 'test') await gate
        return rows
      },
    })
    await mount()
    await openSheet('claude_code')
    await click(sheet()!.querySelectorAll('.extAgents-act button')[1])
    expect(sheetStatus()).toBe('gui.agent.testing_head')
    /* A cli test can run for two minutes on the agent's own quota, so the
       running state keeps a control: Stop, in place of the Test that started it. */
    expect(sheetActs()).toEqual(['gui.agent.disconnect', 'gui.stop'])
    expect((sheet()!.querySelectorAll('.extAgents-act button')[1] as HTMLButtonElement).disabled).toBe(false)
    await click(sheet()!.querySelectorAll('.extAgents-act button')[1])
    expect(acts.map((a) => a[0])).toEqual(['test', 'test_cancel'])
    await act(async () => {
      release()
      await gate
    })
    expect(sheetActs()).toEqual(['gui.agent.disconnect', 'gui.agent.test_label'])
  })

  it('writes what the agent is good at when the field is left, and keeps the old text when it is left blank', async () => {
    const { acts } = install([row({ name: 'claude_code', description: 'Claude Code CLI' })])
    await mount()
    await openSheet('claude_code')
    expect(sheetTextarea()!.value).toBe('Claude Code CLI')
    await typeInto(sheetTextarea(), 'Fixes the failing test')
    await blur(sheetTextarea())
    expect(acts).toEqual([['update', 'claude_code', { description: 'Fixes the failing test' }]])
    await typeInto(sheetTextarea(), '   ')
    await blur(sheetTextarea())
    expect(acts.length).toBe(1)
    expect(sheetTextarea()!.value).toBe('Fixes the failing test')
  })

  it('holds a description typed for a preset until the connect that writes the row', async () => {
    const { acts } = install([row({ name: 'hermes', preset: 'hermes', configured: false, enabled: false, probe_status: 'attention' })])
    await mount()
    await openSheet('hermes')
    await typeInto(sheetTextarea(), 'Odd jobs')
    await blur(sheetTextarea())
    expect(acts).toEqual([])
    expect(sheetActs()).toEqual(['gui.agent.connect'])
    await click(sheet()!.querySelector('.extAgents-act button'))
    expect(acts).toEqual([['connect', 'hermes', { description: 'Odd jobs' }]])
  })

  it('takes a key for an endpoint and connects with it, once one is typed', async () => {
    const { acts } = install([row({ name: 'mirothinker', preset: 'mirothinker', kind: 'openai', configured: false, enabled: false })])
    await mount()
    await openSheet('mirothinker')
    const go = sheet()!.querySelector('.extAgents-act button') as HTMLButtonElement
    expect(go.textContent).toBe('gui.agent.connect')
    expect(go.disabled).toBe(true)
    const field = sheet()!.querySelector('.extAgents-fld input')
    await typeInto(field, ' sk-1 ')
    expect((sheet()!.querySelector('.extAgents-act button') as HTMLButtonElement).disabled).toBe(false)
    await click(sheet()!.querySelector('.extAgents-act button'))
    expect(acts).toEqual([['connect', 'mirothinker', { api_key: 'sk-1' }]])
  })

  it('shows an absent agent how to get installed, and re-checks the machine on request', async () => {
    const rows = [row({ name: 'qwen', preset: 'qwen_code', configured: false, enabled: false, probe_status: 'missing' })]
    const { loads } = install(rows)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } })
    await mount()
    await openSheet('qwen')
    expect(sheetStatus()).toBe('gui.agent.hd_missing_by {"by":"gui.agent.by_qwen_code"}')
    expect(sheet()!.querySelector('.extAgents-cmd code')!.textContent).toBe('npm install -g @qwen-code/qwen-code')
    expect(sheet()!.querySelector<HTMLAnchorElement>('.extAgents-site')!.href).toBe('https://github.com/QwenLM/qwen-code')
    expect(sheetActs()).toEqual(['gui.agent.recheck'])
    await click(sheet()!.querySelector('.extAgents-cmd button'))
    expect(sheet()!.querySelector('.extAgents-cmd button')!.textContent).toBe('gui.agent.copied')
    await click(sheet()!.querySelector('.extAgents-act button'))
    expect(loads).toEqual([true, true])
    expect(sheet()!.querySelector('.extAgents-probe')!.textContent).toBe('gui.agent.still_missing')
    rows[0]!.probe_status = 'attention'
    await click(sheet()!.querySelector('.extAgents-act button'))
    expect(sheet()!.querySelector('.extAgents-probe')).toBeNull()
    expect(sectionOf('qwen')).toBe('gui.agent.g_avail')
    expect(sheetActs()).toEqual(['gui.agent.connect'])
  })

  it('names a refused agent on its row instead of offering to connect it', async () => {
    const { acts } = install([
      row({ name: 'codex', preset: 'codex', configured: false, enabled: false, probe_status: 'attention', needs_auth: true }),
    ])
    await mount()
    expect(sectionOf('codex')).toBe('gui.agent.g_avail')
    expect(buttonOf('codex')!.textContent).toBe('gui.agent.unauthorized')
    expect(buttonOf('codex')!.disabled).toBe(true)
    await click(buttonOf('codex'))
    expect(acts).toEqual([])
    /* The sheet says the same, and offers the one press that can take it
       back: a Test re-measures the handshake. */
    await openSheet('codex')
    expect(sheetActs()).toEqual(['gui.agent.unauthorized', 'gui.agent.test_label'])
    const [named, test] = [...sheet()!.querySelectorAll<HTMLButtonElement>('.extAgents-act button')]
    expect(named!.disabled).toBe(true)
    await click(test)
    expect(acts.map((a) => a.slice(0, 2))).toEqual([['test', 'codex']])
  })

  it('keeps its rendered shape, list', async () => {
    install([
      row({ name: 'Raven', preset: undefined, kind: 'builtin', builtin: true, configured: false, probe_status: 'unknown' }),
      row({ name: 'claude_code' }),
      row({ name: 'hermes', preset: 'hermes', configured: false, enabled: false, probe_status: 'attention' }),
      row({ name: 'codex', preset: 'codex', configured: false, enabled: false, probe_status: 'missing' }),
    ])
    await mount()
    expect(domSnapshot(document.getElementById('extAgentsBody')!)).toMatchSnapshot()
  })

  it('keeps its rendered shape, sheet', async () => {
    install([row({ name: 'claude_code' })])
    await mount()
    await openSheet('claude_code')
    expect(domSnapshot(document.getElementById('dBody')!)).toMatchSnapshot()
  })

  /* A test that failed has to say so wherever it was pressed. The sheet offers
     Test from two states, and only one of them used to render the verdict: an
     unauthorized row whose handshake now passes but whose agent still cannot
     answer would drop the label, offer a plain Connect, and say nothing about
     the test that had just failed. */
  it('says a failed test failed on a row that is not connected', async () => {
    const r = row({ name: 'ua', configured: false, enabled: false, needs_auth: true, probe_status: 'attention' })
    const rows = [r]
    install(rows, {
      act: async (op) => {
        if (op === 'test') {
          r.needs_auth = false
          r.probe_status = 'ready'
          r.last_test_ok = false
          r.last_test_detail = 'it connected and then answered nothing'
        }
        return rows
      },
    })
    await mount()
    await openSheet('ua')
    await click([...sheet()!.querySelectorAll('.extAgents-act button')].find((b) => b.textContent === 'gui.agent.test_label'))
    expect(sheetStatus()).toBe('gui.agent.hd_test_bad {"detail":"it connected and then answered nothing"}')
  })
})

describe('the model pill', () => {
  const choices = [
    { value: 'v/opus', name: 'Opus', group: 'Anthropic' },
    { value: 'v/sonnet', name: 'Sonnet', group: 'Anthropic' },
    { value: 'o/gpt', name: 'GPT', group: 'OpenAI' },
  ]
  const builtin = (over: Partial<ExtAgentRow> = {}): ExtAgentRow =>
    row({
      name: 'Raven',
      preset: undefined,
      kind: 'builtin',
      builtin: true,
      configured: false,
      probe_status: 'unknown',
      own: true,
      model_source: 'raven',
      ...over,
    })
  const pill = (): HTMLButtonElement | null => sheet()?.querySelector('.extAgents-pm') ?? null

  /* The write can take a minute now that the server proves a new model, and a
     row reading "Testing" beside the model it is leaving reads as though the
     old one is what is being tested. */
  it('shows the model it is switching to while the write is in flight', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const r = row({ name: 'Coded', kind: 'acp', model_source: 'agent', model: 'v/opus', model_choices: choices })
    const rows = [r]
    install(rows, {
      act: async (op) => {
        if (op === 'model') await gate
        return rows
      },
    })
    await mount()
    await openSheet('Coded')
    expect(pill()!.textContent).toContain('Opus')

    await click(pill())
    await click(modelButton('Sonnet'))

    expect(pill()!.textContent).toContain('Sonnet')
    await act(async () => {
      release()
      await gate
    })
  })

  it('shows the model cleared while the clear is in flight', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const r = row({ name: 'Coded', kind: 'acp', model_source: 'agent', model: 'v/opus', model_choices: choices })
    const rows = [r]
    install(rows, {
      act: async (op) => {
        if (op === 'model') await gate
        return rows
      },
    })
    await mount()
    await openSheet('Coded')

    await click(sheet()!.querySelector('.extAgents-pill button[aria-label="gui.agent.model_clear"]'))

    expect(pill()!.textContent).not.toContain('Opus')
    expect(pill()!.textContent).toContain('gui.agent.model_own_default')
    await act(async () => {
      release()
      await gate
    })
  })
  const picker = (): HTMLElement | null => sheet()?.querySelector('.model-picker') ?? null
  const modelButton = (label: string): HTMLButtonElement | undefined =>
    [...(picker()?.querySelectorAll<HTMLButtonElement>('.model-picker-model') ?? [])].find((b) => b.textContent!.includes(label))

  it('is not drawn for a server that has no model field', async () => {
    install([row()])
    await mount()
    await openSheet('claude_code')
    expect(sheet()!.querySelector('.extAgents-pill')).toBeNull()
  })

  it('says a third party runs on its own default, opens the menu it advertised, and sends the pick verbatim', async () => {
    const { acts } = install([row({ model_source: 'agent', model_choices: choices })])
    await mount()
    await openSheet('claude_code')
    expect(pill()!.textContent).toBe('gui.agent.model_own_default⌄')
    expect(pill()!.disabled).toBe(false)
    expect(sheet()!.querySelector('.extAgents-mx')).toBeNull()
    await click(pill())
    expect(picker()!.textContent).toContain('Anthropic')
    expect(picker()!.textContent).toContain('OpenAI')
    await click(modelButton('Sonnet'))
    expect(picker()).toBeNull()
    expect(acts).toEqual([['model', 'claude_code', { model: 'v/sonnet' }]])
  })

  it('shows the chosen model by the name the agent gave it, and clears back to the default', async () => {
    const { acts } = install([row({ model_source: 'agent', model_choices: choices, model: 'v/sonnet' })])
    await mount()
    await openSheet('claude_code')
    expect(pill()!.querySelector('.extAgents-mid')!.textContent).toBe('Sonnet')
    expect(pill()!.querySelector('.extAgents-mpv')!.textContent).toBe('Anthropic')
    await click(sheet()!.querySelector('.extAgents-mx'))
    expect(acts).toEqual([['model', 'claude_code', { clear_model: true }]])
  })

  it("draws the built-in row on raven's own connected providers and sends the provider with the pick", async () => {
    hostModels.providers = [
      { id: 'openrouter', name: 'OpenRouter', models: ['anthropic/claude-sonnet-5'], on: true },
      { id: 'dark', name: 'Not connected', models: ['x'], on: false },
    ]
    const { acts } = install([builtin()])
    await mount()
    await openSheet('Raven')
    expect(pill()!.textContent).toBe('gui.agent.model_follow⌄')
    await click(pill())
    expect(picker()!.textContent).toContain('OpenRouter')
    expect(picker()!.textContent).not.toContain('Not connected')
    await click(modelButton('anthropic/claude-sonnet-5'))
    expect(acts).toEqual([['model', 'Raven', { model: 'anthropic/claude-sonnet-5', provider: 'openrouter' }]])
  })

  it('splits a stored host id into the model and the provider it is stored under', async () => {
    hostModels.providers = [{ id: 'openrouter', name: 'OpenRouter', models: ['anthropic/claude-sonnet-5'], on: true }]
    install([builtin({ model: 'openrouter/anthropic/claude-sonnet-5' })])
    await mount()
    await openSheet('Raven')
    expect(pill()!.querySelector('.extAgents-mid')!.textContent).toBe('anthropic/claude-sonnet-5')
    expect(pill()!.querySelector('.extAgents-mpv')!.textContent).toBe('OpenRouter')
    expect(sheet()!.querySelector('.extAgents-mx')).not.toBeNull()
  })

  it('matches a stored prefix to the host provider it spells with a hyphen', async () => {
    /* `stored_model_id` writes the public spelling (`openai-codex`); `model.options`
       keys the same provider by its config slug (`openai_codex`). */
    hostModels.providers = [{ id: 'openai_codex', name: 'OpenAI Codex', models: ['gpt-5-codex'], on: true }]
    install([builtin({ model: 'openai-codex/gpt-5-codex' })])
    await mount()
    await openSheet('Raven')
    expect(pill()!.querySelector('.extAgents-mid')!.textContent).toBe('gpt-5-codex')
    expect(pill()!.querySelector('.extAgents-mpv')!.textContent).toBe('OpenAI Codex')
    await click(pill())
    const current = picker()!.querySelector('.model-picker-model[aria-pressed="true"]')
    expect(current?.textContent).toContain('gpt-5-codex')
  })

  it('offers no typed id: the write takes the menu exactly, and adds to nothing', async () => {
    install([row({ model_source: 'agent', model_choices: choices })])
    await mount()
    await openSheet('claude_code')
    await click(pill())
    await typeInto(picker()!.querySelector('input'), 'brand-new-model')
    expect(picker()!.querySelector('.model-picker-add')).toBeNull()
  })

  it('asks for the host list once when it has not landed yet', async () => {
    hostModels.providers = []
    hostModels.loads = 0
    install([builtin()])
    await mount()
    await openSheet('Raven')
    await click(pill())
    expect(hostModels.loads).toBe(1)
    expect(picker()!.textContent).toContain('gui.agent.model_no_provider')
  })

  it("wears a disabled pill for a row with no menu and none of raven's to fall back on", async () => {
    install([
      row({ name: 'Coder', preset: undefined, kind: 'cli', model_source: 'fixed' }),
      row({ name: 'codex', preset: 'codex', kind: 'acp', model_source: 'agent', model_choices: [] }),
    ])
    await mount()
    await openSheet('Coder')
    expect(pill()!.textContent).toBe('gui.agent.model_managed')
    expect(pill()!.disabled).toBe(true)
    await openSheet('codex')
    expect(sheetName()).toBe('codex')
    expect(pill()!.textContent).toBe('gui.agent.model_managed')
    expect(pill()!.disabled).toBe(true)
  })

  it("lets one of raven's own rows that advertised no menu pick from raven's providers", async () => {
    /* The server sends `raven` for such a row: it runs on this raven's
       catalogue, so "follows the main Raven" is where it starts, not where it
       is stuck. */
    hostModels.providers = [{ id: 'openrouter', name: 'OpenRouter', models: ['z-ai/glm-5.3-flash'], on: true }]
    const { acts } = install([
      row({ name: 'Raven-Code', preset: undefined, kind: 'acp', own: true, model_source: 'raven', model_choices: [] }),
    ])
    await mount()
    await openSheet('Raven-Code')
    expect(pill()!.textContent).toBe('gui.agent.model_follow⌄')
    expect(pill()!.disabled).toBe(false)
    await click(pill())
    expect(picker()!.textContent).toContain('OpenRouter')
    await click(modelButton('z-ai/glm-5.3-flash'))
    expect(acts).toEqual([['model', 'Raven-Code', { model: 'z-ai/glm-5.3-flash', provider: 'openrouter' }]])
  })

  it("offers one of raven's own the column the composer offers, shortlist and all", async () => {
    /* A connected vendor with nothing added yet: the composer's column falls
       back to the registry's shortlist, and this picker read the added list
       alone -- a vendor with four models on one and none on the other. */
    hostModels.providers = [
      { id: 'gemini', name: 'Gemini', models: ['gemini-2.5-pro', 'gemini-2.5-flash'], configured: [], on: true },
      { id: 'deepseek', name: 'DeepSeek', models: ['deepseek-v4-pro', 'deepseek-v4-flash'], configured: ['deepseek-v4-pro'], on: true },
    ]
    const { acts } = install([
      row({ name: 'Raven-Code', preset: undefined, kind: 'acp', own: true, model_source: 'raven', model_choices: [] }),
    ])
    await mount()
    await openSheet('Raven-Code')
    await click(pill())
    expect(picker()!.textContent).toContain('Gemini')
    expect(modelButton('gemini-2.5-pro')).toBeDefined()
    expect(modelButton('gemini-2.5-flash')).toBeDefined()
    await click([...picker()!.querySelectorAll('.model-picker-prov')].find((b) => b.textContent!.includes('DeepSeek')))
    expect(modelButton('deepseek-v4-pro')).toBeDefined()
    expect(modelButton('deepseek-v4-flash')).toBeUndefined()
    await click(modelButton('deepseek-v4-pro'))
    expect(acts).toEqual([['model', 'Raven-Code', { model: 'deepseek-v4-pro', provider: 'deepseek' }]])
  })

  it("draws a host id one of raven's own kept across a re-measure under the provider it is stored on", async () => {
    /* The pick was made while the row's menu was empty and its rule `raven`;
       the handshake behind the page has given it one since, so it reads under
       `agent` now. The stored value did not move, and it is still what the row
       dispatches with, so neither does the way it is drawn. */
    hostModels.providers = [{ id: 'openrouter', name: 'OpenRouter', models: ['anthropic/claude-opus-5'], on: true }]
    const { acts } = install([
      row({
        name: 'Raven-Code',
        preset: undefined,
        kind: 'acp',
        own: true,
        model_source: 'agent',
        model_choices: choices,
        model: 'openrouter/anthropic/claude-opus-5',
      }),
    ])
    await mount()
    await openSheet('Raven-Code')
    expect(pill()!.querySelector('.extAgents-mid')!.textContent).toBe('anthropic/claude-opus-5')
    expect(pill()!.querySelector('.extAgents-mpv')!.textContent).toBe('OpenRouter')
    await click(sheet()!.querySelector('.extAgents-mx'))
    expect(acts).toEqual([['model', 'Raven-Code', { clear_model: true }]])
  })

  it("does not show an endpoint's configured model as a pick, and still lets a menuless acp row clear one", async () => {
    const { acts } = install([
      row({ name: 'mirothinker', preset: 'mirothinker', kind: 'openai', model_source: 'fixed', model: 'miro-1' }),
      row({ name: 'quiet', preset: undefined, kind: 'acp', model_source: 'agent', model_choices: [], model: 'v/old' }),
    ])
    await mount()
    await openSheet('mirothinker')
    expect(pill()!.textContent).toBe('gui.agent.model_managed')
    expect(sheet()!.querySelector('.extAgents-mx')).toBeNull()
    await openSheet('quiet')
    expect(sheetName()).toBe('quiet')
    expect(pill()!.querySelector('.extAgents-mid')!.textContent).toBe('v/old')
    expect(pill()!.disabled).toBe(true)
    await click(sheet()!.querySelector('.extAgents-mx'))
    expect(acts).toEqual([['model', 'quiet', { clear_model: true }]])
  })

  it('lets a shipped product be tested, under its own source', async () => {
    const { acts } = install([row({ name: 'Raven-Code', preset: undefined, kind: 'acp', vendored: true, configured: false, own: true })])
    await mount()
    await openSheet('Raven-Code')
    expect(sheetActs()).toEqual(['gui.agent.disconnect', 'gui.agent.test_label'])
    await click(sheet()!.querySelectorAll('.extAgents-act button')[1])
    expect(acts.map((a) => a.slice(0, 2))).toEqual([['test', 'Raven-Code']])
  })
})
