/**
 * Unit tests for findOnPath: which/where probing, the bash/zsh login-shell
 * fallback, non-file output filtering, and win32 behavior.
 */

import { describe, expect, it, vi } from 'vitest'

import { findOnPath, type ProbeResult } from '../pathProbe.js'

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
