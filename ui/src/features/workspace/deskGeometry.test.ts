/** Tests for floating workspace geometry and column transitions. */

import { describe, expect, it } from 'vitest'

import {
  workspaceColumnCount,
  workspaceTransitionWidth,
} from './deskGeometry'

describe('workspace geometry', () => {
  it('uses one column for two panes and two columns from the third pane', () => {
    expect(workspaceColumnCount(0)).toBe(0)
    expect(workspaceColumnCount(2)).toBe(1)
    expect(workspaceColumnCount(3)).toBe(2)
  })

  it('expands into a second column without shrinking the existing column', () => {
    expect(workspaceTransitionWidth({
      previousWidth: 520,
      previousColumns: 1,
      nextColumns: 2,
      availableWidth: 1200,
    })).toBe(1054)
  })

  it('clamps a second column to the available viewport width', () => {
    expect(workspaceTransitionWidth({
      previousWidth: 720,
      previousColumns: 1,
      nextColumns: 2,
      availableWidth: 970,
    })).toBe(970)
  })
})
