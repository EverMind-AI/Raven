/**
 * Unit tests for findOnPath and probeLoginShellEnv: which/where probing, the
 * bash/zsh login-shell fallback, env parsing, non-file output filtering, and
 * win32 behavior.
 */

import { describe, expect, it, vi } from 'vitest'

import { findOnPath, probeLoginShellEnv, type ProbeResult } from '../pathProbe.js'

function scriptedRunner(...results: ProbeResult[]) {
  let index = 0
  return vi.fn(() => results[index++] ?? { status: null, stdout: null })
}

describe('findOnPath', () => {
  it('returns the which result on posix without probing shells', () => {
    const runner = scriptedRunner({ status: 0, stdout: '/usr/local/bin/raven\n' })
    expect(findOnPath('linux', 'raven', runner, () => true)).toBe('/usr/local/bin/raven')
    expect(runner).toHaveBeenCalledTimes(1)
    expect(runner).toHaveBeenCalledWith('which', ['raven'], { encoding: 'utf8' })
  })

  it('falls back to bash and zsh login shells when which fails', () => {
    const runner = scriptedRunner(
      { status: 1, stdout: null },
      { status: 0, stdout: 'some banner\n/home/dev/.local/bin/raven\n' }
    )
    const exists = (filePath: string) => filePath.startsWith('/home/dev')
    expect(findOnPath('linux', 'raven', runner, exists)).toBe('/home/dev/.local/bin/raven')
    expect(runner).toHaveBeenCalledTimes(2)
    expect(runner).toHaveBeenLastCalledWith('bash', ['-lic', 'command -v raven'], { encoding: 'utf8', timeout: 5000 })
  })

  it('tries zsh after bash fails', () => {
    const runner = scriptedRunner(
      { status: 1, stdout: null },
      { status: null, stdout: null },
      { status: 0, stdout: '/opt/zsh/raven\n' }
    )
    expect(findOnPath('linux', 'raven', runner, () => true)).toBe('/opt/zsh/raven')
    expect(runner).toHaveBeenCalledTimes(3)
    expect(runner).toHaveBeenLastCalledWith('zsh', ['-lic', 'command -v raven'], { encoding: 'utf8', timeout: 5000 })
  })

  it('skips non-file lines such as shell aliases in probe output', () => {
    const runner = scriptedRunner(
      { status: 1, stdout: null },
      { status: 0, stdout: 'raven: aliased to /custom/raven\n/usr/bin/raven\n' }
    )
    const exists = (filePath: string) => !filePath.includes('aliased')
    expect(findOnPath('linux', 'raven', runner, exists)).toBe('/usr/bin/raven')
  })

  it('returns null when every probe fails', () => {
    const runner = scriptedRunner({ status: 1, stdout: null }, { status: 1, stdout: null }, { status: 1, stdout: null })
    expect(findOnPath('linux', 'raven', runner, () => true)).toBeNull()
  })

  it('returns null on posix when a probe cannot even spawn', () => {
    const runner = scriptedRunner()
    expect(findOnPath('linux', 'raven', runner, () => true)).toBeNull()
  })

  it('uses where on win32 and never probes shells', () => {
    const runner = scriptedRunner({ status: 0, stdout: 'C:\\bin\\raven.exe\n' })
    expect(findOnPath('win32', 'raven', runner, () => true)).toBe('C:\\bin\\raven.exe')
    expect(runner).toHaveBeenCalledWith('where', ['raven'], { encoding: 'utf8' })
    expect(runner).toHaveBeenCalledTimes(1)
  })

  it('returns null on win32 when where fails', () => {
    const runner = scriptedRunner({ status: 1, stdout: null })
    expect(findOnPath('win32', 'raven', runner, () => true)).toBeNull()
    expect(runner).toHaveBeenCalledTimes(1)
  })
})

describe('probeLoginShellEnv', () => {
  it('parses KEY=VALUE lines and ignores shell noise', () => {
    const runner = scriptedRunner({
      status: 0,
      stdout: 'no job control in this shell\nHTTP_PROXY=http://proxy:6760\nPATH=/usr/bin:/bin\n'
    })
    expect(probeLoginShellEnv(runner, '/bin/zsh', 'linux')).toEqual({
      HTTP_PROXY: 'http://proxy:6760',
      PATH: '/usr/bin:/bin'
    })
    expect(runner).toHaveBeenCalledWith('/bin/zsh', ['-lic', 'env'], { encoding: 'utf8', timeout: 5000 })
  })

  it('prefers the hinted shell and falls back to bash then zsh', () => {
    const runner = scriptedRunner({ status: 1, stdout: null }, { status: 0, stdout: 'A=1\n' })
    expect(probeLoginShellEnv(runner, '/usr/bin/zsh', 'darwin')).toEqual({ A: '1' })
    expect(runner).toHaveBeenCalledTimes(2)
  })

  it('ignores an unsupported shell hint', () => {
    const runner = scriptedRunner({ status: 0, stdout: 'A=1\n' })
    expect(probeLoginShellEnv(runner, '/usr/bin/fish', 'linux')).toEqual({ A: '1' })
    expect(runner).toHaveBeenCalledWith('bash', ['-lic', 'env'], { encoding: 'utf8', timeout: 5000 })
  })

  it('returns null when every shell probe fails', () => {
    const runner = scriptedRunner({ status: 1, stdout: null }, { status: 1, stdout: null }, { status: 1, stdout: null })
    expect(probeLoginShellEnv(runner, '/bin/bash', 'linux')).toBeNull()
  })
})

describe('probeLoginShellEnv on win32', () => {
  const envScript =
    '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-ChildItem Env: | ForEach-Object { "$($_.Name)=$($_.Value)" }'

  it('reads the environment from powershell.exe and merges pwsh when present', () => {
    const runner = scriptedRunner(
      { status: 0, stdout: 'HTTP_PROXY=http://proxy:6760\nPath=C:\\Windows\\System32\n' },
      { status: 0, stdout: 'PWSH_ONLY=1\n' }
    )
    expect(probeLoginShellEnv(runner, undefined, 'win32')).toEqual({
      HTTP_PROXY: 'http://proxy:6760',
      Path: 'C:\\Windows\\System32',
      PWSH_ONLY: '1'
    })
    expect(runner).toHaveBeenCalledTimes(2)
    expect(runner).toHaveBeenNthCalledWith(1, 'powershell.exe', ['-NonInteractive', '-Command', envScript], {
      encoding: 'utf8',
      timeout: 15000
    })
  })

  it('prefers the VS Code-configured PowerShell 7 profile and lets it win conflicts', () => {
    const hint = 'C:\\Program Files\\PowerShell\\7\\pwsh.exe'
    const runner = scriptedRunner(
      { status: 0, stdout: 'HTTP_PROXY=http://proxy-from-pwsh:6760\nB=2\n' },
      { status: 0, stdout: 'HTTP_PROXY=http://proxy-from-ps:6760\nA=1\n' }
    )
    expect(probeLoginShellEnv(runner, hint, 'win32')).toEqual({
      HTTP_PROXY: 'http://proxy-from-pwsh:6760',
      B: '2',
      A: '1'
    })
    expect(runner).toHaveBeenCalledTimes(2)
    expect(runner).toHaveBeenNthCalledWith(1, hint, ['-NonInteractive', '-Command', envScript], {
      encoding: 'utf8',
      timeout: 15000
    })
    expect(runner).toHaveBeenNthCalledWith(2, 'powershell.exe', ['-NonInteractive', '-Command', envScript], {
      encoding: 'utf8',
      timeout: 15000
    })
  })

  it('falls back to pwsh when powershell.exe fails', () => {
    const runner = scriptedRunner({ status: 1, stdout: null }, { status: 0, stdout: 'A=1\n' })
    expect(probeLoginShellEnv(runner, undefined, 'win32')).toEqual({ A: '1' })
    expect(runner).toHaveBeenCalledTimes(2)
  })

  it('returns null when both windows shells fail', () => {
    const runner = scriptedRunner({ status: 1, stdout: null }, { status: 1, stdout: null })
    expect(probeLoginShellEnv(runner, undefined, 'win32')).toBeNull()
  })
})
