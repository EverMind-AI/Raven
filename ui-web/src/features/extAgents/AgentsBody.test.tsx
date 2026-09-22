// @vitest-environment happy-dom
/* The onboarding wizard's agents step: the hub's rows in the two sections a
 * first run decides on, the writes a row's one control makes, and what the
 * step counts as done.
 */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import { resetSources, setSources } from '../../state/sources'
import { AgentsStepBody } from './AgentsBody'
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

function row(over: Partial<ExtAgentRow> = {}): ExtAgentRow {
  return {
    name: 'claude_code',
    preset: 'claude_code',
    kind: 'acp',
    configured: false,
    enabled: false,
    probe_status: 'ready',
    probe_detail: '',
    has_api_key: false,
    description: '',
    test_running: false,
    last_test_ok: null,
    last_test_at_ms: null,
    last_test_detail: '',
    ...over,
  }
}

/* Same seam production wires: a stand-in translator on setTranslator (it
   returns its key, so tests assert catalogue keys, not translations) and a
   fixture source on sources.extAgents. `refuse` names rows whose write the
   source rejects with that sentence. */
function install(rows: ExtAgentRow[], refuse: Record<string, string> = {}) {
  const acts: Array<[string, string, ExtAgentActArgs]> = []
  const loads: boolean[] = []
  const source: ExtAgentsSource = {
    load: async (probe) => {
      loads.push(!!probe)
      return rows
    },
    act: async (op, r, args) => {
      acts.push([op, r.name, args || {}])
      if (refuse[r.name]) throw { data: { detail: refuse[r.name] } }
      if (op === 'toggle') r.enabled = !!(args as { enabled?: boolean } | undefined)?.enabled
      if (op === 'connect') { r.configured = true; r.enabled = true }
      return rows
    },
  }
  toastWriter.items = []
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ extAgents: source })
  return { source, acts, loads }
}

const rowsOf = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.extAgents-row')]
const rowNamed = (name: string): HTMLElement => rowsOf().find((r) => r.querySelector('.extAgents-t')!.textContent === name)!
const control = (name: string): HTMLElement => rowNamed(name).querySelector('.extAgents-ctl')!.firstElementChild as HTMLElement
const sectionOf = (name: string): string => rowNamed(name).closest('.extAgents-sec')!.querySelector('.extAgents-hd b')!.textContent!
const sectionNamed = (label: string): HTMLElement | null =>
  [...document.querySelectorAll<HTMLElement>('.extAgents-sec')].find((g) => g.querySelector('.extAgents-hd b')!.textContent === label) ?? null
const sectionCount = (label: string): string | null => sectionNamed(label)?.querySelector('.extAgents-n')?.textContent ?? null

const mounted = async (rows: ExtAgentRow[], refuse: Record<string, string> = {}) => {
  const h = install(rows, refuse)
  render(<AgentsStepBody />)
  await act(async () => {
    await store.load(true)
  })
  return h
}

afterEach(() => {
  cleanup()
  resetSources()
  store._resetForTests()
  confirmStore._resetForTests()
})

describe('the onboarding wizard\'s agents step', () => {
  it('shows a scanning placeholder before the first answer lands', async () => {
    let resolveLoad: (rows: ExtAgentRow[]) => void = () => {}
    install([])
    setSources({ extAgents: { load: () => new Promise((res) => (resolveLoad = res)), act: async () => [] } })
    render(<AgentsStepBody />)
    await act(async () => {
      void store.load(true)
    })
    expect(screen.getByText('gui.agent.setup_scanning')).toBeTruthy()
    expect(sectionCount('gui.agent.g_avail')).toBe('0')

    await act(async () => {
      resolveLoad([])
    })
    expect(screen.queryByText('gui.agent.setup_scanning')).toBeNull()
  })

  it('draws the hub\'s two sections, available first, with the hub\'s labels and counts', async () => {
    await mounted([
      row({ name: 'preset_a', configured: false, enabled: false }),
      row({ name: 'switched_on', configured: true, enabled: true }),
      row({ name: 'shipped', vendored: true, configured: false, enabled: true }),
    ])
    const labels = [...document.querySelectorAll('.extAgents-sec .extAgents-hd b')].map((b) => b.textContent)
    expect(labels).toEqual(['gui.agent.g_avail', 'gui.agent.g_on'])
    expect(sectionCount('gui.agent.g_avail')).toBe('1')
    expect(sectionCount('gui.agent.g_on')).toBe('2')
    expect(sectionOf('preset_a')).toBe('gui.agent.g_avail')
    expect(sectionOf('shipped')).toBe('gui.agent.g_on')
    expect(control('preset_a').textContent).toBe('gui.agent.connect')
    expect(control('switched_on').textContent).toBe('gui.agent.disconnect')
    /* Raven's own first inside a section, the way the hub orders it. */
    expect(rowsOf().map((r) => r.querySelector('.extAgents-t')!.textContent)).toEqual(['preset_a', 'shipped', 'switched_on'])
  })

  it('leaves out the built-in loop, an openai endpoint and a command this machine has not got', async () => {
    await mounted([
      row({ name: 'Raven', kind: 'builtin', builtin: true, enabled: true }),
      row({ name: 'miro', kind: 'openai', configured: true, enabled: true, has_api_key: true }),
      row({ name: 'gone', kind: 'cli', configured: false, enabled: false, probe_status: 'missing' }),
    ])
    expect(rowsOf()).toEqual([])
    expect(sectionNamed('gui.agent.g_avail')).toBeNull()
    expect(sectionNamed('gui.agent.g_on')).toBeNull()
  })

  it('names a refused row instead of offering to connect it', async () => {
    const { acts } = await mounted([row({ name: 'refused', kind: 'acp', probe_status: 'attention', needs_auth: true })])
    expect(sectionCount('gui.agent.g_avail')).toBe('1')
    const btn = control('refused') as HTMLButtonElement
    expect(btn.textContent).toBe('gui.agent.unauthorized')
    expect(btn.disabled).toBe(true)
    await act(async () => {
      fireEvent.click(btn)
    })
    expect(acts).toEqual([])
  })

  it('a row is a plain row: nothing to press but its one control, and no sheet opens', async () => {
    await mounted([row({ name: 'preset_a' })])
    const r = rowNamed('preset_a')
    expect(r.getAttribute('role')).toBeNull()
    expect(r.getAttribute('tabindex')).toBeNull()
    await act(async () => {
      fireEvent.click(r)
    })
    expect(store.get().sheet).toBeNull()
  })

  it('connects a preset through act(connect) and a switched-off configured row through act(toggle)', async () => {
    const { acts } = await mounted([
      row({ name: 'preset_a', configured: false, enabled: false }),
      row({ name: 'off_one', configured: true, enabled: false }),
    ])
    await act(async () => {
      fireEvent.click(control('preset_a'))
    })
    await act(async () => {
      fireEvent.click(control('off_one'))
    })
    expect(acts).toEqual([
      ['connect', 'preset_a', {}],
      ['toggle', 'off_one', { enabled: true }],
    ])
    expect(sectionOf('preset_a')).toBe('gui.agent.g_on')
    expect(sectionOf('off_one')).toBe('gui.agent.g_on')
  })

  it('connects a stale row through act(migrate) only after the hub\'s confirm is answered', async () => {
    /* A flag would have left `upgrade_to` standing and the old command line
       in place; the remove plus add drops the handles of runs in flight,
       hence the question -- the same one the hub asks. */
    const { acts } = await mounted([row({ name: 'pi', configured: true, enabled: false, upgrade_to: 'acp' })])
    expect(rowNamed('pi').querySelector('.extAgents-one')!.textContent).toContain('gui.agent.tag_stale')

    await act(async () => {
      fireEvent.click(control('pi'))
    })
    expect(confirmStore.get().open).toBe(true)
    expect(confirmStore.get().title).toBe('gui.agent.migrate_do')
    expect(acts).toEqual([])

    await act(async () => {
      confirmStore.answer(true)
    })
    expect(acts).toEqual([['migrate', 'pi', {}]])
  })

  it('offers a shipped agent switched off, and switches it back on', async () => {
    await mounted([row({ name: 'raven_coder', vendored: true, configured: false, enabled: false })])
    expect(sectionOf('raven_coder')).toBe('gui.agent.g_avail')
    expect(store.stepDone()).toBe(false)
    await act(async () => {
      fireEvent.click(control('raven_coder'))
    })
    expect(sectionOf('raven_coder')).toBe('gui.agent.g_on')
    /* Drawn as connected, still not counted: the step is about an external agent. */
    expect(store.stepDone()).toBe(false)
  })

  it('says connecting on the row while the write is in flight', async () => {
    let release: (rows: ExtAgentRow[]) => void = () => {}
    const rows = [row({ name: 'preset_a' })]
    install(rows)
    setSources({
      extAgents: {
        load: async () => rows,
        act: () => new Promise<ExtAgentRow[]>((res) => (release = res)),
      },
    })
    render(<AgentsStepBody />)
    await act(async () => {
      await store.load(true)
    })
    await act(async () => {
      fireEvent.click(control('preset_a'))
    })
    expect(control('preset_a').tagName).toBe('SPAN')
    expect(control('preset_a').textContent).toBe('gui.agent.testing')
    expect(rowNamed('preset_a').querySelector('.extAgents-one-work')).not.toBeNull()

    await act(async () => {
      release([row({ name: 'preset_a', configured: true, enabled: true })])
    })
    expect(control('preset_a').textContent).toBe('gui.agent.disconnect')
  })

  it('keeps a refused connect on the row in red with Retry, and Retry sends it again', async () => {
    /* The hub's rule, not the toast the old step used: a refusal is a state
       the row is in, so the reason is still there when the reader looks. */
    const refuse: Record<string, string> = { preset_a: 'did not answer a test message' }
    const { acts } = await mounted([row({ name: 'preset_a' })], refuse)
    await act(async () => {
      fireEvent.click(control('preset_a'))
    })
    expect(rowNamed('preset_a').querySelector('.extAgents-one-bad')!.textContent).toBe('did not answer a test message')
    expect(control('preset_a').textContent).toBe('gui.retry')
    expect(toastWriter.items).toEqual([])

    delete refuse.preset_a
    await act(async () => {
      fireEvent.click(control('preset_a'))
    })
    expect(acts).toEqual([
      ['connect', 'preset_a', {}],
      ['connect', 'preset_a', {}],
    ])
    expect(rowNamed('preset_a').querySelector('.extAgents-one-bad')).toBeNull()
    expect(control('preset_a').textContent).toBe('gui.agent.disconnect')
  })

  it('disconnects a connected row through act(toggle, {enabled: false})', async () => {
    const { acts } = await mounted([row({ name: 'switched_on', configured: true, enabled: true })])
    await act(async () => {
      fireEvent.click(control('switched_on'))
    })
    expect(acts).toEqual([['toggle', 'switched_on', { enabled: false }]])
    expect(sectionOf('switched_on')).toBe('gui.agent.g_avail')
  })

  it('is done once an external agent is connected, whatever the shipped ones do', async () => {
    await mounted([
      row({ name: 'shipped', vendored: true, configured: false, enabled: true }),
      row({ name: 'preset_a', configured: false, enabled: false }),
    ])
    expect(store.stepDone()).toBe(false)
    await act(async () => {
      fireEvent.click(control('preset_a'))
    })
    expect(store.stepDone()).toBe(true)
    expect(store.found().map((r) => r.name)).toEqual(['preset_a'])
  })
})
