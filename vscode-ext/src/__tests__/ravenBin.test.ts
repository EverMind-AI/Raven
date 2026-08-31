/**
 * Unit tests for resolveRavenCommand: resolution order (setting, RAVEN_BIN,
 * PATH, uv), ~ expansion, existence validation, and extraArgs handling.
 */

import { describe, expect, it, vi } from 'vitest'

import { resolveRavenCommand, sanitizeExtraArgs, type ResolutionContext } from '../ravenBin.js'

const baseCtx = (overrides: Partial<ResolutionContext> = {}): ResolutionContext => ({
  env: {},
  configExecutablePath: '',
  extraArgs: [],
  homeDir: '/home/dev',
  exists: () => true,
  which: () => null,
  ...overrides
})

describe('resolveRavenCommand', () => {
  it('prefers raven.executablePath when it exists', () => {
    const command = resolveRavenCommand(baseCtx({ configExecutablePath: '/opt/raven/raven', exists: () => true }))
    expect(command).toEqual({
      command: '/opt/raven/raven',
      args: ['tui'],
      label: 'configured path'
    })
  })

  it('returns null when the configured path does not exist', () => {
    const command = resolveRavenCommand(baseCtx({ configExecutablePath: '/opt/raven/raven', exists: () => false }))
    expect(command).toBeNull()
  })

  it('falls back to RAVEN_BIN when the setting is empty', () => {
    const command = resolveRavenCommand(baseCtx({ env: { RAVEN_BIN: '/home/dev/.local/bin/raven' } }))
    expect(command).toEqual({
      command: '/home/dev/.local/bin/raven',
      args: ['tui'],
      label: 'configured path'
    })
  })

  it('uses raven found on PATH when nothing is configured', () => {
    const command = resolveRavenCommand(baseCtx({ which: name => (name === 'raven' ? '/usr/local/bin/raven' : null) }))
    expect(command).toEqual({
      command: '/usr/local/bin/raven',
      args: ['tui'],
      label: 'raven on PATH'
    })
  })

  it('falls back to uv run raven when raven is not on PATH', () => {
    const command = resolveRavenCommand(baseCtx({ which: name => (name === 'uv' ? '/usr/bin/uv' : null) }))
    expect(command).toEqual({
      command: '/usr/bin/uv',
      args: ['run', 'raven', 'tui'],
      label: 'uv run raven'
    })
  })

  it('returns null when neither raven nor uv is available', () => {
    expect(resolveRavenCommand(baseCtx())).toBeNull()
  })

  it('expands ~ in the configured path and checks the expanded path', () => {
    const exists = vi.fn(() => true)
    const command = resolveRavenCommand(baseCtx({ configExecutablePath: '~/.local/bin/raven', exists }))
    expect(command?.command).toBe('/home/dev/.local/bin/raven')
    expect(exists).toHaveBeenCalledWith('/home/dev/.local/bin/raven')
  })

  it('expands a bare ~ to the home directory', () => {
    const command = resolveRavenCommand(baseCtx({ env: { RAVEN_BIN: '~' }, exists: () => false }))
    expect(command).toBeNull()
    expect(
      resolveRavenCommand(baseCtx({ env: { RAVEN_BIN: '~' }, exists: path => path === '/home/dev' }))?.command
    ).toBe('/home/dev')
  })

  it('expands a Windows-style ~\\ prefix against the home directory', () => {
    const command = resolveRavenCommand(
      baseCtx({ homeDir: 'C:\\Users\\dev', configExecutablePath: '~\\bin\\raven.exe', exists: () => true })
    )
    expect(command?.command).toBe('C:\\Users\\dev\\bin\\raven.exe')
  })

  it('appends extraArgs after the tui subcommand', () => {
    const command = resolveRavenCommand(
      baseCtx({ extraArgs: ['--dev', '--color', '256'], which: () => '/usr/bin/raven' })
    )
    expect(command?.args).toEqual(['tui', '--dev', '--color', '256'])
  })
})

describe('sanitizeExtraArgs', () => {
  it('returns an empty array for a string value', () => {
    expect(sanitizeExtraArgs('--dev')).toEqual([])
  })

  it('returns an empty array for undefined or non-array values', () => {
    expect(sanitizeExtraArgs(undefined)).toEqual([])
    expect(sanitizeExtraArgs({ dev: true })).toEqual([])
  })

  it('keeps only string entries of a mixed array', () => {
    expect(sanitizeExtraArgs(['--dev', 42, '--color', null])).toEqual(['--dev', '--color'])
  })

  it('passes a plain string array through', () => {
    expect(sanitizeExtraArgs(['--dev', '--color', '256'])).toEqual(['--dev', '--color', '256'])
  })
})
