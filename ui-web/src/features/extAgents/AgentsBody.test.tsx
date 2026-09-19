// @vitest-environment happy-dom
/* The onboarding wizard's agents pane: the two buckets it draws over the
 * extAgents roster, and the two writes a row's own button can make.
 */

import { act, cleanup, render, screen } from '@testing-library/react'
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
    kind: 'cli',
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
   fixture source on sources.extAgents -- see ExtAgentsPage.test.tsx's own
   `install`. */
function install(rows: ExtAgentRow[], over: Partial<ExtAgentsSource> = {}) {
  const acts: Array<[string, string, ExtAgentActArgs]> = []
  const loads: boolean[] = []
  const source: ExtAgentsSource = {
    load: async (probe) => {
      loads.push(!!probe)
      return rows
    },
    act: async (op, r, args) => {
      acts.push([op, r.name, args || {}])
      if (op === 'toggle') r.enabled = !!(args as { enabled?: boolean } | undefined)?.enabled
      return rows
    },
    ...over,
  }
  toastWriter.items = []
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ extAgents: source })
  return { source, acts, loads }
}

const rowsOf = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.surow')]
const rowNamed = (name: string): HTMLElement => rowsOf().find((r) => r.querySelector('.nm b')!.textContent === name)!
const actBtn = (name: string): HTMLButtonElement => rowNamed(name).querySelector('.suact button')!
const groupNamed = (label: string): HTMLElement | null =>
  [...document.querySelectorAll<HTMLElement>('.sugrp')].find((g) => g.querySelector('.hd b')!.textContent === label) ??
  null
const groupCount = (label: string): string | null => groupNamed(label)?.querySelector('.hd .n')?.textContent ?? null

afterEach(() => {
  cleanup()
  resetSources()
  store._resetForTests()
  vi.restoreAllMocks()
})

describe('the onboarding wizard\'s agents pane', () => {
  it('shows a scanning placeholder before the first answer lands', async () => {
    let resolveLoad: (rows: ExtAgentRow[]) => void = () => {}
    install([], { load: () => new Promise((res) => (resolveLoad = res)) })
    render(<AgentsStepBody />)
    await act(async () => {
      void store.load(true)
    })
    expect(screen.getByText('gui.agent.setup_scanning')).toBeTruthy()
    expect(groupCount('gui.agent.setup_available')).toBe('0')

    await act(async () => {
      resolveLoad([])
    })
    expect(screen.queryByText('gui.agent.setup_scanning')).toBeNull()
  })

  it('splits found rows into available and connected, with their counts', async () => {
    install([
      row({ name: 'preset_a', configured: false, enabled: false }),
      row({ name: 'shipped', preset: undefined, vendored: true, configured: false, enabled: true }),
      row({ name: 'switched_on', configured: true, enabled: true }),
    ])
    render(<AgentsStepBody />)
    await act(async () => {
      await store.load(true)
    })

    expect(groupCount('gui.agent.setup_available')).toBe('1')
    expect(groupCount('gui.agent.setup_connected')).toBe('2')
    expect(rowNamed('preset_a').closest('.sugrp')!.querySelector('.hd b')!.textContent).toBe(
      'gui.agent.setup_available',
    )
    expect(rowNamed('shipped').closest('.sugrp')!.querySelector('.hd b')!.textContent).toBe('gui.agent.setup_connected')
    expect(rowNamed('switched_on').closest('.sugrp')!.querySelector('.hd b')!.textContent).toBe(
      'gui.agent.setup_connected',
    )
    expect(actBtn('preset_a').textContent).toBe('gui.agent.connect')
    expect(actBtn('shipped').textContent).toBe('gui.agent.disconnect')
  })

  it('hides the built-in agent, a missing row and an unfound row entirely', async () => {
    install([
      row({ name: 'raven', kind: 'builtin', builtin: true }),
      row({ name: 'not_here', probe_status: 'missing' }),
    ])
    render(<AgentsStepBody />)
    await act(async () => {
      await store.load(true)
    })

    expect(rowsOf()).toEqual([])
    expect(groupNamed('gui.agent.setup_available')).toBeNull()
    expect(groupNamed('gui.agent.setup_connected')).toBeNull()
  })

  it('leaves an openai row out of the available bucket', async () => {
    install([row({ name: 'miro', kind: 'openai', configured: false, enabled: false, has_api_key: false })])
    render(<AgentsStepBody />)
    await act(async () => {
      await store.load(true)
    })

    expect(rowsOf()).toEqual([])
  })

  it('connects a preset through act(connect)', async () => {
    const { acts } = install([row({ name: 'preset_a', configured: false, enabled: false })])
    render(<AgentsStepBody />)
    await act(async () => {
      await store.load(true)
    })

    await act(async () => {
      actBtn('preset_a').click()
    })

    expect(acts).toEqual([['connect', 'preset_a', {}]])
  })

  it('connects a disabled configured row through act(toggle, {enabled: true})', async () => {
    const { acts } = install([row({ name: 'off_one', configured: true, enabled: false })])
    render(<AgentsStepBody />)
    await act(async () => {
      await store.load(true)
    })

    await act(async () => {
      actBtn('off_one').click()
    })

    expect(acts).toEqual([['toggle', 'off_one', { enabled: true }]])
  })

  it('connects a stale row through act(migrate) after the same confirm the settings page asks', async () => {
    /* A flag would have left `upgrade_to` standing and the old command line
       in place -- the migration the card offered and never did. The remove
       plus add drops the handles of runs in flight, hence the confirm; the
       tag beside the kind says where the row moves to. */
    const confirms: string[] = []
    vi.spyOn(confirmStore, 'ask').mockImplementation((title, _body, _label, fn) => {
      confirms.push(title)
      fn()
    })
    const { acts } = install([row({ name: 'pi', configured: true, enabled: false, upgrade_to: 'acp' })])
    render(<AgentsStepBody />)
    await act(async () => {
      await store.load(true)
    })
    expect(rowNamed('pi').textContent).toContain('gui.agent.stale_to {"to":"gui.agent.kind_acp"}')

    await act(async () => {
      actBtn('pi').click()
    })

    expect(confirms).toEqual(['gui.agent.migrate_do'])
    expect(acts).toEqual([['migrate', 'pi', {}]])
  })

  it('offers a shipped agent switched off, and switches it back on', async () => {
    const { acts } = install([row({ name: 'raven_coder', vendored: true, configured: false, enabled: false })])
    render(<AgentsStepBody />)
    await act(async () => {
      await store.load(true)
    })
    expect(rowNamed('raven_coder').closest('.sugrp')!.querySelector('.hd b')!.textContent).toBe('gui.agent.setup_available')
    expect(store.stepDone()).toBe(false)

    await act(async () => {
      actBtn('raven_coder').click()
    })

    expect(acts).toEqual([['toggle', 'raven_coder', { enabled: true }]])
    expect(rowNamed('raven_coder').closest('.sugrp')!.querySelector('.hd b')!.textContent).toBe('gui.agent.setup_connected')
  })

  it('disables the row and shows the connecting state while the write is in flight', async () => {
    let settle = (): void => {}
    const { acts } = install([row({ name: 'preset_a', configured: false, enabled: false })], {
      act: async (op, r, args) => {
        acts.push([op, r.name, args || {}])
        await new Promise<void>((res) => (settle = res))
        return [r]
      },
    })
    render(<AgentsStepBody />)
    await act(async () => {
      await store.load(true)
    })

    await act(async () => {
      actBtn('preset_a').click()
    })

    expect(actBtn('preset_a').hasAttribute('disabled')).toBe(true)
    expect(actBtn('preset_a').textContent).toContain('gui.agent.setup_connecting')

    await act(async () => {
      settle()
    })

    expect(actBtn('preset_a').hasAttribute('disabled')).toBe(false)
    expect(actBtn('preset_a').textContent).toBe('gui.agent.connect')
  })

  it('disconnects a connected row through act(toggle, {enabled: false})', async () => {
    const { acts } = install([row({ name: 'switched_on', configured: true, enabled: true })])
    render(<AgentsStepBody />)
    await act(async () => {
      await store.load(true)
    })

    await act(async () => {
      actBtn('switched_on').click()
    })

    expect(acts).toEqual([['toggle', 'switched_on', { enabled: false }]])
  })
})
