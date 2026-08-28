import { describe, expect, it, vi } from 'vitest'

import { resolveRavenCommand, type ResolutionContext } from '../ravenBin.js'

const baseCtx = (overrides: Partial<ResolutionContext> = {}): ResolutionContext => ({
  platform: 'linux',
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

  it('appends extraArgs after the tui subcommand', () => {
    const command = resolveRavenCommand(
      baseCtx({ extraArgs: ['--dev', '--color', '256'], which: () => '/usr/bin/raven' })
    )
    expect(command?.args).toEqual(['tui', '--dev', '--color', '256'])
  })
})
