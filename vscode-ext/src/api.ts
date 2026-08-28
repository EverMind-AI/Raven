/**
 * Narrow facade over the `vscode` API.
 *
 * The launcher logic (executable resolution, terminal lifecycle) only depends
 * on this interface, so unit tests can drive it with fakes and never load the
 * real `vscode` module, which only exists inside the extension host.
 */

import type * as vscodeNs from 'vscode'

export interface VscodeDisposable {
  dispose(): void
}

export interface VscodeTerminal {
  show(preserveFocus?: boolean): void
  sendText(text: string, addNewLine?: boolean): void
  dispose(): void
}

export interface VscodeStatusBarItem {
  text: string
  tooltip: string | undefined
  command: string | undefined
  show(): void
  hide(): void
  dispose(): void
}

export interface VscodeApi {
  window: {
    createTerminal(options: { name: string; cwd?: string }): VscodeTerminal
    onDidCloseTerminal(listener: (terminal: VscodeTerminal) => unknown): VscodeDisposable
    showErrorMessage(message: string, ...actions: string[]): Promise<string | undefined>
    createStatusBarItem(alignment: number, priority?: number): VscodeStatusBarItem
  }
  workspace: {
    getConfiguration(section: string): {
      get<T>(key: string): T | undefined
    }
    workspaceFolders?: Array<{ uri: { fsPath: string } }>
  }
  commands: {
    registerCommand(id: string, handler: (...args: unknown[]) => unknown): VscodeDisposable
  }
  StatusBarAlignment: { Left: number }
}

export async function loadVscode(): Promise<VscodeApi | null> {
  try {
    const vscode: typeof vscodeNs = await import('vscode')
    return adaptVscode(vscode)
  } catch {
    return null
  }
}

function adaptVscode(vscode: typeof vscodeNs): VscodeApi {
  return {
    window: {
      createTerminal: options => vscode.window.createTerminal(options),
      onDidCloseTerminal: listener => vscode.window.onDidCloseTerminal(terminal => listener(terminal)),
      showErrorMessage: async (message, ...actions) => vscode.window.showErrorMessage(message, ...actions),
      createStatusBarItem: (alignment, priority) => {
        const item = vscode.window.createStatusBarItem(alignment, priority)
        return {
          get text(): string {
            return item.text
          },
          set text(value: string) {
            item.text = value
          },
          get tooltip(): string | undefined {
            return typeof item.tooltip === 'string' ? item.tooltip : undefined
          },
          set tooltip(value: string | undefined) {
            item.tooltip = value
          },
          get command(): string | undefined {
            return typeof item.command === 'string' ? item.command : undefined
          },
          set command(value: string | undefined) {
            item.command = value
          },
          show: () => item.show(),
          hide: () => item.hide(),
          dispose: () => item.dispose()
        }
      }
    },
    workspace: {
      getConfiguration: section => vscode.workspace.getConfiguration(section),
      workspaceFolders: vscode.workspace.workspaceFolders?.map(folder => ({ uri: { fsPath: folder.uri.fsPath } }))
    },
    commands: {
      registerCommand: (id, handler) => vscode.commands.registerCommand(id, handler)
    },
    StatusBarAlignment: vscode.StatusBarAlignment
  }
}
