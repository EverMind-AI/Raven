// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { stringWidth } from '@hermes/ink'
import { render } from 'ink-testing-library'
import React from 'react'
import { describe, expect, it } from 'vitest'

import type { Usage } from '../types.js'

import { StatusRule } from '../components/appChrome.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const USAGE: Usage = { calls: 0, input: 0, output: 0, total: 0 }
const FULL_USAGE: Usage = { calls: 0, context_max: 200000, context_used: 0, input: 0, output: 0, total: 0 }

// A branch name long enough that the label cannot share a 57-column terminal
// with the left slot -- the case that used to render 66 columns wide and wrap
// its tail onto the live-agents strip below.
const LONG_CWD = '~/workspace/raven-rag (feat/tui_subagent_live_view)'
// Display width is twice the character count here, which is what `String.length`
// arithmetic could not see.
const CJK_CWD = '~/工作区/乌鸦项目/前端界面 (feat/主分支)'

const base = {
  bgCount: 0,
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

describe('StatusRule fits the terminal', () => {
  const rowsOf = (cols: number, cwdLabel: string, extra: Record<string, unknown> = {}) =>
    frameOf({ cols, cwdLabel, usage: FULL_USAGE, ...extra })
      .split('\n')
      .filter(line => line.length > 0)

  for (const cols of [24, 40, 57, 80, 120]) {
    it(`draws one row no wider than ${cols} columns`, () => {
      for (const label of [LONG_CWD, CJK_CWD]) {
        const rows = rowsOf(cols, label)

        expect(rows).toHaveLength(1)
        expect(stringWidth(rows[0]!)).toBeLessThanOrEqual(cols)
      }
    })
  }

  it('keeps the tail of the cwd label when it has to cut, because that is where the branch is', () => {
    expect(rowsOf(57, LONG_CWD)[0]).toContain('(feat/tui_subagent_live_view)')
  })

  it('keeps the head of the update nudge instead, since a sentence cut from the front is unreadable', () => {
    const row = rowsOf(50, LONG_CWD, { updateAvailable: true, updateCommand: 'raven upgrade' })[0]!

    expect(row).toContain('Update available')
    expect(stringWidth(row)).toBeLessThanOrEqual(50)
  })

  it('never spends the whole row on the label -- the status word survives', () => {
    expect(rowsOf(40, LONG_CWD)[0]).toContain('ready')
  })
})

describe('reported usage cost', () => {
  it('distinguishes unknown, zero and partial costs', () => {
    expect(frameOf({ cols: 200, showCost: true, usage: { ...USAGE, cost_usd: null } })).toContain('cost unknown')
    expect(frameOf({ cols: 200, showCost: true, usage: { ...USAGE, cost_usd: 0 } })).toContain('$0')
    const partial = frameOf({ cols: 200, showCost: true, usage: { ...USAGE, cost_usd: 0.84, cost_missing_calls: 2 } })
    expect(partial).toContain('$0.8400')
    expect(partial).toContain('2 calls with unknown cost')
  })
})
