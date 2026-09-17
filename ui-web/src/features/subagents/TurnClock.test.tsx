// @vitest-environment happy-dom
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TurnClock } from './TurnClock'

import type { Shell } from '../../shell/bridge'
import type { InstanceRow } from './types'

function wire(): void {
  const shell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: () => {},
    showPage: () => {},
  }
  window.RavenShell = shell
}

const row = (over: Partial<InstanceRow> = {}): InstanceRow =>
  ({ sessionKey: 's1', agent: 'hermes', handle: 'one', kind: 'cli', ...over }) as InstanceRow

const shown = (): string | null => document.querySelector('.pane-turnms')?.textContent ?? null

afterEach(() => {
  cleanup()
  delete window.RavenShell
  vi.useRealTimers()
})

describe('the turn clock on an instance pane', () => {
  it('draws nothing for an instance answering nothing', () => {
    /* Absent is the signal, so an instance that finished between two polls stops
       counting rather than freezing on its last number. */
    wire()
    render(<TurnClock row={row()} />)
    expect(shown()).toBeNull()
  })

  it('counts from the turn start the server published', () => {
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_090_000)
    render(<TurnClock row={row({ turnStartedAtMs: 1_700_000_000_000 })} />)
    /* 90s, in the spelling `shell/duration.ts` gives every other elapsed number
       on the page. */
    expect(shown()).toBe('1m30s')
  })

  it('will not fall back to the row stamp when no turn is running', () => {
    /* `updatedAtMs` sits on the same row and is the tempting field: it is always
       there and it looks like a time. Every registry write stamps it -- a
       binding commit, a graph-origin write -- so a clock counting from it dates
       the row rather than the turn, and would run on an instance that is
       answering nothing at all. The row here carries only that stamp. */
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_090_000)
    render(<TurnClock row={row({ updatedAtMs: 1_700_000_000_000 })} />)
    expect(shown()).toBeNull()
  })

  it('holds back the first second rather than opening on zero', () => {
    /* A clock that opens on "0.0s" reads as broken rather than as new, which is
       the rule the transcript's own card already follows. */
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_000_400)
    render(<TurnClock row={row({ turnStartedAtMs: 1_700_000_000_000 })} />)
    expect(shown()).toBeNull()
  })

  it('names what the number measures, for a reader who cannot see the pane', () => {
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_090_000)
    render(<TurnClock row={row({ turnStartedAtMs: 1_700_000_000_000 })} />)
    expect(document.querySelector('.pane-turnms')?.getAttribute('title'))
      .toBe('gui.ws.instance_turn_elapsed {"d":"1m30s"}')
  })
})
