import { describe, expect, it } from 'vitest'

import { isExtendedKeysCapableByXtversion, needsAltScreenResizeScrollbackClear, supportsExtendedKeys } from './terminal.js'

describe('terminal resize quirks', () => {
  it('uses a deeper alt-screen resize clear for Apple Terminal', () => {
    expect(needsAltScreenResizeScrollbackClear({ TERM_PROGRAM: 'Apple_Terminal' })).toBe(true)
    expect(needsAltScreenResizeScrollbackClear({ TERM_PROGRAM: ' Apple_Terminal ' })).toBe(true)
  })

  it('keeps the normal resize repaint path for modern terminals', () => {
    expect(needsAltScreenResizeScrollbackClear({ TERM_PROGRAM: 'vscode' })).toBe(false)
    expect(needsAltScreenResizeScrollbackClear({ TERM_PROGRAM: 'iTerm.app' })).toBe(false)
  })
})

// XTVERSION-driven extended-keys re-push: allowlist of terminal name prefixes
// known to honor Kitty keyboard / xterm modifyOtherKeys protocols, used when
// env-sniffing misses (typically over SSH).
describe('isExtendedKeysCapableByXtversion', () => {
  const capable = (name: string) => isExtendedKeysCapableByXtversion(name, 'linux')

  it('recognizes Zellij(N) reply', () => {
    expect(capable('Zellij(4401)')).toBe(true)
    expect(capable('zellij(0)')).toBe(true)
  })

  it('recognizes xterm.js (VSCode / Cursor / Codespaces integrated terminal)', () => {
    expect(capable('xterm.js(5.5.0)')).toBe(true)
    expect(capable('xterm.js')).toBe(true)
  })

  it('recognizes tmux N.N', () => {
    expect(capable('tmux(3.4)')).toBe(true)
  })

  it('recognizes Ghostty / kitty / WezTerm / iTerm2', () => {
    expect(capable('Ghostty 1.2.0')).toBe(true)
    expect(capable('kitty(0.32.0)')).toBe(true)
    expect(capable('WezTerm 20240203')).toBe(true)
    expect(capable('iTerm2 3.6')).toBe(true)
  })

  it('recognizes Windows Terminal and mintty variants', () => {
    expect(capable('WindowsTerminal 1.18')).toBe(true)
    expect(capable('Windows Terminal')).toBe(true)
    expect(capable('mintty 3.6.1')).toBe(true)
  })

  it('rejects unknown / legacy terminal names', () => {
    expect(capable('Apple_Terminal')).toBe(false)
    expect(capable('xterm')).toBe(false)
    expect(capable('rxvt')).toBe(false)
    expect(capable('unknown')).toBe(false)
  })

  it('rejects empty / whitespace-only input safely', () => {
    expect(capable('')).toBe(false)
    expect(capable('   ')).toBe(false)
  })

  it('trims and matches case-insensitively', () => {
    expect(capable('  ZELLIJ(99)  ')).toBe(true)
    expect(capable('XTERM.JS(1)')).toBe(true)
  })
})

describe('extended-key reporting on Windows', () => {
  it('keeps native Windows input on raw bytes so an IME commit is not re-encoded', () => {
    expect(supportsExtendedKeys('win32', 'windows-terminal')).toBe(false)
    expect(isExtendedKeysCapableByXtversion('Windows Terminal', 'win32')).toBe(false)
  })

  it('keeps the protocol enabled for the same terminals on other platforms', () => {
    expect(supportsExtendedKeys('linux', 'windows-terminal')).toBe(true)
    expect(isExtendedKeysCapableByXtversion('Windows Terminal', 'linux')).toBe(true)
  })
})
