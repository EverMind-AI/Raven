// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// `--print-colors` is the diagnostic that shows what each palette role
// resolves to. An empty role has no color to paint, so it is reported as
// `(unset)` instead of being sent through the colorizer; without that branch
// the row advertises a swatch it cannot actually paint.

import { describe, expect, it } from 'vitest'

import type { Theme } from '../theme.js'

import { renderColorSwatches } from '../lib/printColors.js'
import { DEFAULT_THEME } from '../theme.js'

const roleLine = (theme: Theme, role: string) =>
  renderColorSwatches(theme, 3)
    .split('\n')
    .find(line => new RegExp(`^\\s+${role}\\s`).test(line)) ?? ''

describe('renderColorSwatches', () => {
  it('reports an empty role as (unset) instead of painting a swatch', () => {
    const theme: Theme = {
      ...DEFAULT_THEME,
      color: { ...DEFAULT_THEME.color, selectionFg: '' }
    }

    const line = roleLine(theme, 'selectionFg')

    expect(line).toMatch(/^\s+selectionFg\s+\(unset\)$/)
    expect(line).not.toContain('████████')
  })

  it('still paints a swatch for a role that carries a color', () => {
    const theme: Theme = {
      ...DEFAULT_THEME,
      color: { ...DEFAULT_THEME.color, selectionBg: 'ansi:cyan' }
    }

    const line = roleLine(theme, 'selectionBg')

    expect(line).toContain('████████')
    expect(line).not.toContain('(unset)')
  })
})
