import { describe, expect, it } from 'vitest'

import { isExtendedKeysCapableByXtversion, needsAltScreenResizeScrollbackClear } from './terminal.js'

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
  it('recognizes Zellij(N) reply', () => {
    expect(isExtendedKeysCapableByXtversion('Zellij(4401)')).toBe(true)
    expect(isExtendedKeysCapableByXtversion('zellij(0)')).toBe(true)
  })

  it('recognizes xterm.js (VSCode / Cursor / Codespaces integrated terminal)', () => {
    expect(isExtendedKeysCapableByXtversion('xterm.js(5.5.0)')).toBe(true)
    expect(isExtendedKeysCapableByXtversion('xterm.js')).toBe(true)
  })

  it('recognizes tmux N.N', () => {
    expect(isExtendedKeysCapableByXtversion('tmux(3.4)')).toBe(true)
  })

  it('recognizes Ghostty / kitty / WezTerm / iTerm2', () => {
    expect(isExtendedKeysCapableByXtversion('Ghostty 1.2.0')).toBe(true)
    expect(isExtendedKeysCapableByXtversion('kitty(0.32.0)')).toBe(true)
    expect(isExtendedKeysCapableByXtversion('WezTerm 20240203')).toBe(true)
    expect(isExtendedKeysCapableByXtversion('iTerm2 3.6')).toBe(true)
  })

  it('recognizes Windows Terminal and mintty variants', () => {
    expect(isExtendedKeysCapableByXtversion('WindowsTerminal 1.18')).toBe(true)
    expect(isExtendedKeysCapableByXtversion('Windows Terminal')).toBe(true)
    expect(isExtendedKeysCapableByXtversion('mintty 3.6.1')).toBe(true)
  })

  it('rejects unknown / legacy terminal names', () => {
    expect(isExtendedKeysCapableByXtversion('Apple_Terminal')).toBe(false)
    expect(isExtendedKeysCapableByXtversion('xterm')).toBe(false)
    expect(isExtendedKeysCapableByXtversion('rxvt')).toBe(false)
    expect(isExtendedKeysCapableByXtversion('unknown')).toBe(false)
  })

  it('rejects empty / whitespace-only input safely', () => {
    expect(isExtendedKeysCapableByXtversion('')).toBe(false)
    expect(isExtendedKeysCapableByXtversion('   ')).toBe(false)
  })

  it('trims and matches case-insensitively', () => {
    expect(isExtendedKeysCapableByXtversion('  ZELLIJ(99)  ')).toBe(true)
    expect(isExtendedKeysCapableByXtversion('XTERM.JS(1)')).toBe(true)
  })
})
