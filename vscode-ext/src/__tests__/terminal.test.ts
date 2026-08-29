/**
 * Unit tests for TuiTerminalManager: editor-area terminal creation with
 * argv-passing shellPath/shellArgs, reuse while alive, recreation after
 * close, and disposal.
 */

import { describe, expect, it, vi } from 'vitest'

import type { VscodeApi, VscodeDisposable, VscodeTerminal } from '../api.js'

import { TuiTerminalManager } from '../terminal.js'

interface FakeTerminal extends VscodeTerminal {
  shown: number
  disposed: boolean
}

function createFakeApi() {
  const terminals: FakeTerminal[] = []
  const closeListeners: Array<(terminal: VscodeTerminal) => unknown> = []

  const makeTerminal = (): FakeTerminal => ({
    shown: 0,
    disposed: false,
    show: vi.fn().mockImplementation(function (this: FakeTerminal) {
      this.shown += 1
    }),
    dispose: vi.fn().mockImplementation(function (this: FakeTerminal) {
      this.disposed = true
    })
  })

  const api: VscodeApi = {
    window: {
      createTerminal: vi.fn(() => {
        const terminal = makeTerminal()
        terminals.push(terminal)
        return terminal
      }),
      onDidCloseTerminal: vi.fn(listener => {
        closeListeners.push(listener)
        const disposable: VscodeDisposable = { dispose: vi.fn() }
        return disposable
      }),
      showErrorMessage: vi.fn(async () => undefined)
    },
    workspace: {
      getConfiguration: vi.fn(() => ({ get: vi.fn() })),
      workspaceFolders: [{ uri: { fsPath: '/workspace/raven' } }]
    },
    commands: {
      registerCommand: vi.fn(() => ({ dispose: vi.fn() }))
    }
  }

  return { api, terminals, closeListeners }
}

const command = { command: '/usr/local/bin/raven', args: ['tui', '--dev'], label: 'raven on PATH' }

describe('TuiTerminalManager', () => {
  it('creates a terminal that runs raven directly, without a host shell', () => {
    const { api, terminals } = createFakeApi()
    const manager = new TuiTerminalManager(api)

    manager.open(command, '/workspace/raven')

    expect(api.window.createTerminal).toHaveBeenCalledWith({
      name: 'Raven',
      cwd: '/workspace/raven',
      shellPath: '/usr/local/bin/raven',
      shellArgs: ['tui', '--dev'],
      location: 'editor',
      env: undefined
    })
    expect(terminals).toHaveLength(1)
    expect(terminals[0].shown).toBe(1)
    expect(manager.isOpen).toBe(true)
  })

  it('passes uv run raven argv through shellArgs', () => {
    const { api } = createFakeApi()
    const manager = new TuiTerminalManager(api)
    const uvCommand = { command: '/usr/bin/uv', args: ['run', 'raven', 'tui'], label: 'uv run raven' }

    manager.open(uvCommand, undefined)

    expect(api.window.createTerminal).toHaveBeenCalledWith({
      name: 'Raven',
      cwd: undefined,
      shellPath: '/usr/bin/uv',
      shellArgs: ['run', 'raven', 'tui'],
      location: 'editor',
      env: undefined
    })
  })

  it('passes the login shell environment to the terminal', () => {
    const { api } = createFakeApi()
    const manager = new TuiTerminalManager(api)

    manager.open(command, undefined, { HTTP_PROXY: 'http://proxy.example:6760' })

    expect(api.window.createTerminal).toHaveBeenCalledWith({
      name: 'Raven',
      cwd: undefined,
      shellPath: '/usr/local/bin/raven',
      shellArgs: ['tui', '--dev'],
      location: 'editor',
      env: { HTTP_PROXY: 'http://proxy.example:6760' }
    })
  })

  it('reuses the existing terminal on subsequent opens', () => {
    const { api, terminals } = createFakeApi()
    const manager = new TuiTerminalManager(api)

    manager.open(command, undefined)
    manager.open(command, undefined)

    expect(api.window.createTerminal).toHaveBeenCalledTimes(1)
    expect(terminals[0].shown).toBe(2)
  })

  it('recreates the terminal after it closes', () => {
    const { api, terminals, closeListeners } = createFakeApi()
    const manager = new TuiTerminalManager(api)

    manager.open(command, undefined)
    const closed = terminals[0]
    closeListeners[0](closed)

    expect(manager.isOpen).toBe(false)

    manager.open(command, undefined)

    expect(api.window.createTerminal).toHaveBeenCalledTimes(2)
    expect(terminals).toHaveLength(2)
  })

  it('disposes the terminal and close subscription', () => {
    const { api, terminals } = createFakeApi()
    const manager = new TuiTerminalManager(api)

    manager.open(command, undefined)
    manager.dispose()

    expect(terminals[0].disposed).toBe(true)
    expect(manager.isOpen).toBe(false)
  })
})
