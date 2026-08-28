import { describe, expect, it, vi } from 'vitest'

import type { VscodeApi, VscodeDisposable, VscodeTerminal } from '../api.js'

import { buildSendText, TuiTerminalManager } from '../terminal.js'

interface FakeTerminal extends VscodeTerminal {
  sent: string[]
  shown: number
  disposed: boolean
}

function createFakeApi() {
  const terminals: FakeTerminal[] = []
  const closeListeners: Array<(terminal: VscodeTerminal) => unknown> = []

  const makeTerminal = (): FakeTerminal => ({
    sent: [],
    shown: 0,
    disposed: false,
    show: vi.fn().mockImplementation(function (this: FakeTerminal) {
      this.shown += 1
    }),
    sendText: vi.fn().mockImplementation(function (this: FakeTerminal, text: string) {
      this.sent.push(text)
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
      showErrorMessage: vi.fn(async () => undefined),
      createStatusBarItem: vi.fn(() => ({
        text: '',
        tooltip: undefined,
        command: undefined,
        show: vi.fn(),
        hide: vi.fn(),
        dispose: vi.fn()
      }))
    },
    workspace: {
      getConfiguration: vi.fn(() => ({ get: vi.fn() })),
      workspaceFolders: [{ uri: { fsPath: '/workspace/raven' } }]
    },
    commands: {
      registerCommand: vi.fn(() => ({ dispose: vi.fn() }))
    },
    StatusBarAlignment: { Left: 1 }
  }

  return { api, terminals, closeListeners }
}

const command = { command: '/usr/local/bin/raven', args: ['tui'], label: 'raven on PATH' }

describe('buildSendText', () => {
  it('leaves simple posix commands unquoted', () => {
    expect(buildSendText(command, 'linux')).toBe('/usr/local/bin/raven tui')
  })

  it('single-quotes posix args containing spaces or shell metacharacters', () => {
    expect(
      buildSendText({ command: '/opt/my raven/raven', args: ['tui', '--title', "dev's box"], label: 'x' }, 'linux')
    ).toBe("'/opt/my raven/raven' tui --title 'dev'\\''s box'")
  })

  it('double-quotes win32 args containing spaces', () => {
    expect(buildSendText({ command: 'C:\\Program Files\\Raven\\raven.exe', args: ['tui'], label: 'x' }, 'win32')).toBe(
      '"C:\\Program Files\\Raven\\raven.exe" tui'
    )
  })
})

describe('TuiTerminalManager', () => {
  it('creates a terminal named Raven and launches raven tui', () => {
    const { api, terminals } = createFakeApi()
    const manager = new TuiTerminalManager(api)

    manager.open(command, '/workspace/raven', 'linux')

    expect(api.window.createTerminal).toHaveBeenCalledWith({ name: 'Raven', cwd: '/workspace/raven' })
    expect(terminals).toHaveLength(1)
    expect(terminals[0].shown).toBe(1)
    expect(terminals[0].sent).toEqual(['/usr/local/bin/raven tui'])
    expect(manager.isOpen).toBe(true)
  })

  it('reuses the existing terminal on subsequent opens', () => {
    const { api, terminals } = createFakeApi()
    const manager = new TuiTerminalManager(api)

    manager.open(command, undefined, 'linux')
    manager.open(command, undefined, 'linux')

    expect(api.window.createTerminal).toHaveBeenCalledTimes(1)
    expect(terminals[0].shown).toBe(2)
  })

  it('recreates the terminal after it closes', () => {
    const { api, terminals, closeListeners } = createFakeApi()
    const manager = new TuiTerminalManager(api)

    manager.open(command, undefined, 'linux')
    const closed = terminals[0]
    closeListeners[0](closed)

    expect(manager.isOpen).toBe(false)

    manager.open(command, undefined, 'linux')

    expect(api.window.createTerminal).toHaveBeenCalledTimes(2)
    expect(terminals).toHaveLength(2)
  })

  it('disposes the terminal and close subscription', () => {
    const { api, terminals } = createFakeApi()
    const manager = new TuiTerminalManager(api)

    manager.open(command, undefined, 'linux')
    manager.dispose()

    expect(terminals[0].disposed).toBe(true)
    expect(manager.isOpen).toBe(false)
  })
})
