/** The per-instance effort chip: what it draws for an instance following the
 * conversation and for one set apart from it, that the menu is the agent's own
 * rungs, that Auto clears rather than sets, and that neither a refusal nor a
 * moving session tier can leave it naming a rung the next turn will not use.
 */

// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as tier from '../../shell/tier'
import { InstanceMode } from './InstanceMode'

import type { Shell } from '../../shell/bridge'
import type { AgentsSource, InstanceModeReply, InstanceRow } from './types'

const ROW: InstanceRow = { sessionKey: 's1', agent: 'raven-research', handle: 'h1', kind: 'cli' }

/* A vocabulary outside the tier ladder, and deliberately one no shipped agent
   declares: raven-research names its three after the ladder and every other
   product inherits the built-in three, so nothing on the current tree exercises
   the case this control exists for. Inventing it here is the point -- the menu
   is read off the reply precisely so that nothing on this side may assume
   `medium`/`high`/`max`. */
const MENU = [
  { id: 'fast', name: 'Fast' },
  { id: 'deep', name: 'Deep' },
  { id: 'ultra', name: 'Ultra' },
]

let asked: unknown[]
let answer: (mode: string | null | undefined) => Promise<InstanceModeReply>

function wire(over: Partial<AgentsSource> = {}): void {
  asked = []
  window.RavenShell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: () => {},
    showPage: () => {},
  } as Shell
  const source: AgentsSource = {
    list: async () => [],
    instanceMode: async (agent, handle) => { asked.push(['read', agent, handle]); return answer(undefined) },
    instanceSetMode: async (agent, handle, mode) => { asked.push(['set', agent, handle, mode]); return answer(mode) },
    ...over,
  }
  window.DS = { agents: source } as unknown as typeof window.DS
  document.body.innerHTML = '<div id="menu" data-open="false"></div><div id="toasts"></div>'
}

beforeEach(() => {
  answer = async (mode) => ({
    mode: mode === undefined ? null : mode,
    inherited: 'deep',
    availableModes: MENU,
  })
  wire()
})

afterEach(() => {
  cleanup()
  delete window.RavenShell
  document.body.innerHTML = ''
})

const chip = (): HTMLElement | null => document.querySelector('.pane-imode')
const rows = (): string[] =>
  [...document.querySelectorAll('#menu button')].map((b) => b.textContent || '')

async function draw(): Promise<void> {
  await act(async () => { render(<InstanceMode row={ROW} />) })
}

describe('an instance own effort', () => {
  it('shows what the instance will run at, which under Auto is the inherited rung', async () => {
    /* Naming "Auto" alone would leave the reader to open the menu for the one
       thing the chip is for. */
    await draw()

    expect(chip()).not.toBeNull()
    expect(chip()!.textContent).toBe('Deep')
    expect(chip()!.className).toContain('auto')
    expect(asked).toEqual([['read', 'raven-research', 'h1']])
  })

  it('marks an instance that was set apart from the conversation', async () => {
    answer = async (mode) => ({ mode: mode === undefined ? 'ultra' : mode, inherited: 'deep', availableModes: MENU })
    await draw()

    expect(chip()!.textContent).toBe('Ultra')
    expect(chip()!.className).not.toContain('auto')
  })

  it('offers this agent own rungs, not the tier ladder', async () => {
    /* Each agent advertises its own vocabulary from its own handshake. Drawing
       `medium`/`high`/`max` here would offer rungs it does not have. */
    await draw()
    await act(async () => { chip()!.click() })

    expect(rows()).toEqual(['gui.imode.auto · Deep ✓', 'Fast', 'Deep', 'Ultra'])
  })

  it('says where Auto currently lands, and marks the override instead when there is one', async () => {
    answer = async (mode) => ({ mode: mode === undefined ? 'fast' : mode, inherited: 'deep', availableModes: MENU })
    await draw()
    await act(async () => { chip()!.click() })

    expect(rows()).toEqual(['gui.imode.auto · Deep', 'Fast ✓', 'Deep', 'Ultra'])
  })

  it('says the agent own default when there is no tier to inherit', async () => {
    /* `inherited: null` is not "no answer": it is the session tier failing to
       clamp onto this agent, so the agent runs on whatever it defaults to. */
    answer = async (mode) => ({ mode: mode === undefined ? null : mode, inherited: null, availableModes: MENU })
    await draw()

    expect(chip()!.textContent).toBe('gui.imode.auto')
    await act(async () => { chip()!.click() })
    expect(rows()[0]).toBe('gui.imode.auto · gui.imode.agent_default ✓')
  })

  it('clears the override rather than setting a mode, when Auto is picked', async () => {
    /* There is no sentinel id for "no override" -- an agent is free to call one
       of its own modes "default" -- so the absence travels as `clear`. */
    answer = async (mode) => ({ mode: mode === undefined ? 'ultra' : mode, inherited: 'deep', availableModes: MENU })
    await draw()
    await act(async () => { chip()!.click() })

    answer = async () => ({ mode: null, inherited: 'deep', availableModes: MENU })
    await act(async () => { (document.querySelectorAll('#menu button')[0] as HTMLElement).click() })

    expect(asked.at(-1)).toEqual(['set', 'raven-research', 'h1', null])
    expect(chip()!.textContent).toBe('Deep')
    expect(chip()!.className).toContain('auto')
  })

  it('takes the mode from the reply, not from the row that was clicked', async () => {
    await draw()
    await act(async () => { chip()!.click() })

    /* The manager refuses a rung the agent does not advertise; painting the click
       would leave the chip naming one the next turn will not run at. */
    answer = async () => ({ mode: null, inherited: 'deep', availableModes: MENU })
    await act(async () => { (document.querySelectorAll('#menu button')[3] as HTMLElement).click() })

    expect(asked.at(-1)).toEqual(['set', 'raven-research', 'h1', 'ultra'])
    expect(chip()!.textContent).toBe('Deep')
    expect(chip()!.className).toContain('auto')
  })

  it('keeps the chip where it was when the switch is refused', async () => {
    await draw()
    await act(async () => { chip()!.click() })

    answer = async () => { throw { data: { detail: 'no mode ultra; this agent offers fast, deep' } } }
    await act(async () => { (document.querySelectorAll('#menu button')[3] as HTMLElement).click() })

    expect(chip()!.textContent).toBe('Deep')
    expect(screen.queryByText(/no mode ultra/)).not.toBeNull()
  })

  it('re-reads under Auto when the conversation tier moves', async () => {
    /* Under Auto the chip is showing `inherited`, which is the session's tier
       clamped to this agent -- so a switch changes what this pane says with
       nothing on this pane touched. The server uses the new rung on the
       instance's next turn; without this the chip named the old one. */
    let inherited = 'deep'
    answer = async (mode) => ({
      mode: mode === undefined ? null : mode, inherited, availableModes: MENU,
    })
    await draw()
    expect(chip()!.textContent).toBe('Deep')

    inherited = 'ultra'
    await act(async () => { tier._notifyForTests('max') })

    expect(chip()!.textContent).toBe('Ultra')
    expect(asked.filter((a) => Array.isArray(a) && a[0] === 'read')).toHaveLength(2)
  })

  it('does not re-read for a tier move when the instance has its own mode', async () => {
    /* An override outranks the tier, so `inherited` is not what this chip is
       showing and the switch is none of its business. */
    answer = async (mode) => ({
      mode: mode === undefined ? 'fast' : mode, inherited: 'deep', availableModes: MENU,
    })
    await draw()
    expect(chip()!.textContent).toBe('Fast')

    await act(async () => { tier._notifyForTests('max') })

    expect(asked.filter((a) => Array.isArray(a) && a[0] === 'read')).toHaveLength(1)
    expect(chip()!.textContent).toBe('Fast')
  })

  it('draws nothing when the read fails', async () => {
    answer = async () => { throw new Error('socket') }
    await draw()

    expect(chip()).toBeNull()
  })

  it('drops the chip when the pane moves to an instance it cannot read', async () => {
    /* Not the same as the first read failing, which leaves nothing to keep. Here
       a rung is already on screen, and holding it would put one instance's
       effort on another instance's pane. */
    const { rerender } = render(<InstanceMode row={ROW} />)
    await act(async () => {})
    expect(chip()!.textContent).toBe('Deep')

    answer = async () => { throw new Error('socket') }
    await act(async () => {
      rerender(<InstanceMode row={{ ...ROW, handle: 'h2' }} />)
    })

    expect(asked.at(-1)).toEqual(['read', 'raven-research', 'h2'])
    expect(chip()).toBeNull()
  })

  it('draws nothing for an agent that advertises no rungs', async () => {
    /* A menu of none is an agent with nothing to choose between, not an agent
       whose choice was not read. */
    answer = async () => ({ mode: null, inherited: null, availableModes: [] })
    await draw()

    expect(chip()).toBeNull()
  })

  it('draws nothing on a server without the surface', async () => {
    wire({ instanceMode: undefined, instanceSetMode: undefined })
    await draw()

    expect(chip()).toBeNull()
    expect(asked).toEqual([])
  })

  it('does not start a drag or reach the pane when the chip is used', async () => {
    /* The pane header is the drag handle once there is more than one pane, and
       a pointerdown on it also raises the pane. Neither should happen because a
       reader reached for this control. */
    const grab = vi.fn()
    await act(async () => {
      render(<div onPointerDown={grab}><InstanceMode row={ROW} /></div>)
    })
    const target = document.querySelector('.pane-imode') as HTMLElement

    await act(async () => {
      target.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    })

    expect(grab).not.toHaveBeenCalled()
  })
})
