// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { describe, expect, it } from 'vitest'

import { findSlashCommand } from '../app/slash/registry.js'

// A command that changes session state has to run in this process.
// createSlashHandler.ts:45-51 routes a slash locally when findSlashCommand
// resolves it and otherwise hands it to slash.exec, which runs the CLI in a
// subprocess -- where a mutation cannot reach the live session at all.
const MUTATING_COMMANDS = [
  'background',
  'branch',
  'browser',
  'busy',
  'clear',
  'compress',
  'fast',
  'model',
  'new',
  'personality',
  'queue',
  'reasoning',
  'reload-mcp',
  'retry',
  'rollback',
  'steer',
  'stop',
  'title',
  'tools',
  'undo',
  'verbose',
  'voice',
  'yolo'
] as const

describe('slash routing', () => {
  it('resolves every mutating command locally instead of the CLI slash worker', () => {
    expect(MUTATING_COMMANDS.filter(name => !findSlashCommand(name))).toEqual([])
  })

  it('leaves an unknown command unresolved so it reaches the CLI', () => {
    expect(findSlashCommand('channels-status')).toBeUndefined()
  })

  it('resolves aliases and is case-insensitive', () => {
    expect(findSlashCommand('MODEL')).toBe(findSlashCommand('model'))
    expect(findSlashCommand('bg')).toBe(findSlashCommand('background'))
  })
})
