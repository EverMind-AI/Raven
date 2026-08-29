/**
 * Single-Raven-terminal lifecycle: reuse the terminal while it is alive and
 * recreate it once it closes.
 *
 * The terminal opens as an editor-area tab (location: editor) so the TUI
 * sits in the file tab row like the OpenCode extension. The terminal process
 * is raven itself (shellPath/shellArgs), so arguments reach it through argv
 * without any host-shell expansion, and a normal TUI exit closes the tab,
 * which resets the manager for the next launch. The login shell environment
 * (proxy settings, API keys) is passed through so raven behaves the same as
 * in a directly opened terminal.
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

  open(command: RavenCommand, workspacePath: string | undefined, env?: Record<string, string>): void {
    if (this.terminal) {
      this.terminal.show()
      return
    }
    this.terminal = this.api.window.createTerminal({
      name: 'Raven',
      cwd: workspacePath,
      shellPath: command.command,
      shellArgs: command.args,
      location: 'editor',
      env
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
