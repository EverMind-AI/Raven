// @vitest-environment happy-dom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { InstanceModel } from './InstanceModel'
import * as store from './store'
import { resetSources, setSources } from '../../state/sources'

import type { Shell } from '../../shell/bridge'
import type { AgentsSource, InstanceRow } from './types'

function wire(over: Partial<AgentsSource> = {}): void {
  const shell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: () => {},
    showPage: () => {},
  }
  window.RavenShell = shell
  setSources({ agents: { list: async () => [], ...over } as AgentsSource })
  document.body.innerHTML = '<div id="menu" data-open="false"></div><div id="toast"></div>'
}

const row = (): InstanceRow =>
  ({ sessionKey: 's1', agent: 'hermes', handle: 'one', kind: 'acp' }) as InstanceRow

const chip = (): HTMLButtonElement | null => document.querySelector('.pane-imodel')
const menuRows = (): string[] =>
  [...document.querySelectorAll('#menu button')].map((b) => b.textContent || '')

async function mount() {
  const view = render(<InstanceModel row={row()} />)
  await act(async () => { await Promise.resolve() })
  return view
}

const OFFERED = [
  { value: 'hosted_vllm/hosted-vllm/qwen3.6-35B-A3B', name: 'qwen3.6-35B-A3B', group: 'Current' },
  { value: 'openrouter/openrouter/anthropic/claude-opus-5', name: 'claude-opus-5', group: 'OpenRouter' },
  { value: 'openrouter/openrouter/anthropic/claude-sonnet-5', name: 'claude-sonnet-5', group: 'OpenRouter' },
]

beforeEach(() => { store._resetForTests() })

afterEach(() => {
  cleanup()
  store._resetForTests()
  delete window.RavenShell
  resetSources()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('the model chip on an instance pane', () => {
  it('draws nothing for an agent that offers no menu', async () => {
    /* A cli agent has no such option, and neither does an acp agent that
       advertises none. For a reader those are the same fact: nothing to pick. */
    wire({ instanceModel: async () => ({ model: null, availableModels: [] }) })
    await mount()
    expect(chip()).toBeNull()
  })

  it('draws nothing when the source cannot answer', async () => {
    wire({ instanceModel: async () => { throw new Error('no') } })
    await mount()
    expect(chip()).toBeNull()
  })

  it('says the agent owns the choice rather than naming a model it did not pick', async () => {
    /* `model: null` is "whatever the agent chose", which this host cannot see --
       the probe's `currentValue` belongs to a throwaway session. Drawing that
       here would name a model this instance may never have run on. */
    wire({ instanceModel: async () => ({ model: null, availableModels: OFFERED }) })
    await mount()
    expect(chip()!.textContent).toBe('gui.imodel.agent')
    expect(chip()!.className).toContain('auto')
    expect(chip()!.title).toBe('gui.imodel.agent_tip')
  })

  it('shows the short name of an override, not the id that was sent', async () => {
    wire({
      instanceModel: async () => ({
        model: 'openrouter/openrouter/anthropic/claude-opus-5',
        availableModels: OFFERED,
      }),
    })
    await mount()
    expect(chip()!.textContent).toBe('claude-opus-5')
    expect(chip()!.className).not.toContain('auto')
    expect(chip()!.title).toBe('gui.imodel.set_tip')
  })

  it('falls back to the id when the agent named no short form', async () => {
    /* Honest last resort: long and provider-qualified, but it is what was
       offered. A prettier invented spelling would name a model under a name the
       agent never gave it. */
    wire({
      instanceModel: async () => ({
        model: 'some/opaque/id',
        availableModels: [{ value: 'some/opaque/id', name: '', group: '' }],
      }),
    })
    await mount()
    expect(chip()!.textContent).toBe('some/opaque/id')
  })

  it('sends the value and not the label when one is picked', async () => {
    const sent: Array<string | null> = []
    wire({
      instanceModel: async () => ({ model: null, availableModels: OFFERED }),
      instanceSetModel: async (_agent, _handle, model) => {
        sent.push(model)
        return { model, availableModels: OFFERED }
      },
    })
    await mount()
    act(() => { chip()!.click() })
    const opus = [...document.querySelectorAll<HTMLButtonElement>('#menu button')]
      .find((b) => (b.textContent || '').startsWith('claude-opus-5'))!
    await act(async () => { opus.click() })
    expect(sent).toEqual(['openrouter/openrouter/anthropic/claude-opus-5'])
    /* And the chip moved to what was actually applied, from the reply. */
    expect(chip()!.textContent).toBe('claude-opus-5')
  })

  it('offers the way back to the agent, always and first', async () => {
    const sent: Array<string | null> = []
    wire({
      instanceModel: async () => ({
        model: 'openrouter/openrouter/anthropic/claude-opus-5',
        availableModels: OFFERED,
      }),
      instanceSetModel: async (_agent, _handle, model) => {
        sent.push(model)
        return { model, availableModels: OFFERED }
      },
    })
    await mount()
    act(() => { chip()!.click() })
    expect(menuRows()[0]).toBe('gui.imodel.agent')
    await act(async () => { document.querySelector<HTMLButtonElement>('#menu button')!.click() })
    /* `null` clears, which is the only way back and must not be a sentinel id --
       an agent is free to offer a model called anything at all. */
    expect(sent).toEqual([null])
    expect(chip()!.textContent).toBe('gui.imodel.agent')
  })

  it('ticks the one in force, and only that one', async () => {
    wire({
      instanceModel: async () => ({
        model: 'openrouter/openrouter/anthropic/claude-opus-5',
        availableModels: OFFERED,
      }),
    })
    await mount()
    act(() => { chip()!.click() })
    const ticked = menuRows().filter((r) => r.includes('✓'))
    expect(ticked).toEqual(['claude-opus-5 ✓'])
  })

  it('breaks the list where the agent bucketed it', async () => {
    /* Forty ids in one unbroken run is not a menu anyone reads. The writer draws
       a flat list, so the agent's grouping survives as a separator. */
    wire({ instanceModel: async () => ({ model: null, availableModels: OFFERED }) })
    await mount()
    act(() => { chip()!.click() })
    const kinds = [...document.querySelectorAll('#menu > *')].map((n) => n.tagName)
    /* agent-own, rule, Current, rule, then the two OpenRouter rows together. */
    expect(kinds).toEqual(['BUTTON', 'HR', 'BUTTON', 'HR', 'BUTTON', 'BUTTON'])
  })

  it('keeps the chip where it was when the switch is refused', async () => {
    /* The manager refuses a value the agent does not advertise and the agent
       refuses one it will not write. Either way the chip must not move to a
       model the next turn will not run on. */
    wire({
      instanceModel: async () => ({
        model: 'openrouter/openrouter/anthropic/claude-opus-5',
        availableModels: OFFERED,
      }),
      instanceSetModel: async () => { throw { data: { detail: 'that one is not available' } } },
    })
    await mount()
    act(() => { chip()!.click() })
    const other = [...document.querySelectorAll<HTMLButtonElement>('#menu button')]
      .find((b) => (b.textContent || '').startsWith('claude-sonnet-5'))!
    await act(async () => { other.click() })
    expect(chip()!.textContent).toBe('claude-opus-5')
  })
})
