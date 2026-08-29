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
  dispose(): void
}

export interface VscodeApi {
  window: {
    createTerminal(options: { name: string; cwd?: string; shellPath?: string; shellArgs?: string[] }): VscodeTerminal
    onDidCloseTerminal(listener: (terminal: VscodeTerminal) => unknown): VscodeDisposable
    showErrorMessage(message: string, ...actions: string[]): Promise<string | undefined>
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
      showErrorMessage: async (message, ...actions) => vscode.window.showErrorMessage(message, ...actions)
    },
    workspace: {
      getConfiguration: section => vscode.workspace.getConfiguration(section),
      workspaceFolders: vscode.workspace.workspaceFolders?.map(folder => ({ uri: { fsPath: folder.uri.fsPath } }))
    },
    commands: {
      registerCommand: (id, handler) => vscode.commands.registerCommand(id, handler)
    }
  }
}
