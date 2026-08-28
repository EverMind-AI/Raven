/**
 * Single-Raven-terminal lifecycle: reuse the terminal while it is alive and
 * recreate it once it closes.
 *
 * The terminal process is raven itself (shellPath/shellArgs), so arguments
 * reach it through argv without any host-shell expansion, and a normal TUI
 * exit closes the terminal, which resets the manager for the next launch.
 */

import type { VscodeApi, VscodeDisposable, VscodeTerminal } from './api.js'
import type { RavenCommand } from './ravenBin.js'

export class TuiTerminalManager {
  private terminal: VscodeTerminal | null = null
  private closeSubscription: VscodeDisposable | null = null

  constructor(private readonly api: VscodeApi) {}

  get isOpen(): boolean {
    return this.terminal !== null
  }

  open(command: RavenCommand, workspacePath: string | undefined): void {
    if (this.terminal) {
      this.terminal.show()
      return
    }
    this.terminal = this.api.window.createTerminal({
      name: 'Raven',
      cwd: workspacePath,
      shellPath: command.command,
      shellArgs: command.args
    })
    this.closeSubscription = this.api.window.onDidCloseTerminal(closed => {
      if (closed !== this.terminal) {
        return
      }
      this.terminal = null
      this.closeSubscription?.dispose()
      this.closeSubscription = null
    })
    this.terminal.show()
  }

  dispose(): void {
    this.closeSubscription?.dispose()
    this.closeSubscription = null
    this.terminal?.dispose()
    this.terminal = null
  }
}
