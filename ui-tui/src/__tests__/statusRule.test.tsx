// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { render } from 'ink-testing-library'
import React from 'react'
import { describe, expect, it } from 'vitest'

import type { Usage } from '../types.js'

import { StatusRule } from '../components/appChrome.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const USAGE: Usage = { calls: 0, input: 0, output: 0, total: 0 }

const base = {
  bgCount: 0,
  busy: false,
  cols: 100,
  cwdLabel: '~/proj (main)',
  model: 'minimax/m3',
  showCost: false,
  status: 'ready',
  statusColor: DEFAULT_THEME.color.accent,
  t: DEFAULT_THEME,
  usage: USAGE
}

const frameOf = (extra: Record<string, unknown>) =>
  stripAnsi(render(<StatusRule {...base} {...extra} />).lastFrame() ?? '')

describe('StatusRule update nudge', () => {
  it('shows the cwd/branch label when no update is available', () => {
    const frame = frameOf({})
    expect(frame).toContain('~/proj (main)')
    expect(frame).not.toContain('Update available')
  })

  it('replaces the cwd/branch label with the upgrade nudge when an update is available', () => {
    const frame = frameOf({ updateAvailable: true, updateCommand: 'raven upgrade' })
    expect(frame).toContain('Update available')
    expect(frame).toContain('raven upgrade')
    expect(frame).not.toContain('~/proj (main)')
  })
})
