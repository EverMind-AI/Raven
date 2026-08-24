/** Tests for floating workspace pane identity and session reset behavior. */

// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import * as desk from './deskStore'

import type { Shell } from '../../shell/bridge'

function wire(): void {
  const fakeShell: Shell = {
    T: (key) => key,
    confirmAsk: (_title, _body, _label, fn) => fn(),
    showPage: () => {},
    workspaceSetOpen: () => {},
  }
  window.RavenShell = fakeShell
  localStorage.clear()
  desk._resetForTests()
}

beforeEach(wire)

afterEach(() => {
  desk._resetForTests()
  window.RavenShell = undefined
  localStorage.clear()
})

describe('desk store', () => {
  it('updates pane identity and selection when navigating to another file', () => {
    desk.openDeskFile('/workspace/a.ts')
    desk.toggleSolo('file:/workspace/a.ts')

    desk.replacePaneFile('file:/workspace/a.ts', '/workspace/b.ts')

    expect(desk.getState().panes.map((pane) => pane.id)).toEqual(['file:/workspace/b.ts'])
    expect(desk.getState().active).toBe('file:/workspace/b.ts')
    expect(desk.getState().solo).toBe('file:/workspace/b.ts')
    desk.openDeskFile('/workspace/b.ts')
    expect(desk.getState().panes).toHaveLength(1)
  })

  it('clears session panes without dropping subscribers', () => {
    desk.openDeskFile('/workspace/a.ts')
    let updates = 0
    const unsubscribe = desk.subscribe(() => { updates += 1 })

    desk.reset()

    expect(updates).toBe(1)
    expect(desk.getState().panes).toEqual([])
    desk.openDeskFile('/workspace/b.ts')
    expect(updates).toBe(2)
    unsubscribe()
  })
})
