/**
 * Single-Raven-terminal lifecycle: reuse the terminal while it is alive,
 * recreate it once it closes, and quote the launch line for the host shell.
 */

import type { VscodeApi, VscodeDisposable, VscodeTerminal } from './api.js'
import type { RavenCommand } from './ravenBin.js'

const POSIX_SAFE_ARG = /^[A-Za-z0-9_@%+=:,./-]+$/
const WIN32_SAFE_ARG = /^[A-Za-z0-9_@+=:,./\\-]+$/

export function buildSendText(command: RavenCommand, platform: NodeJS.Platform): string {
  const argv = [command.command, ...command.args]
  if (platform === 'win32') {
    return argv.map(arg => (WIN32_SAFE_ARG.test(arg) ? arg : `"${arg.replaceAll('"', '""')}"`)).join(' ')
  }
  const quote = (arg: string): string => (POSIX_SAFE_ARG.test(arg) ? arg : `'${arg.replaceAll("'", `'\\''`)}'`)
  return argv.map(quote).join(' ')
}

export class TuiTerminalManager {
  private terminal: VscodeTerminal | null = null
  private closeSubscription: VscodeDisposable | null = null

  constructor(private readonly api: VscodeApi) {}

  get isOpen(): boolean {
    return this.terminal !== null
  }

  open(command: RavenCommand, workspacePath: string | undefined, platform: NodeJS.Platform): void {
    if (this.terminal) {
      this.terminal.show()
      return
    }
    this.terminal = this.api.window.createTerminal({ name: 'Raven', cwd: workspacePath })
    this.closeSubscription = this.api.window.onDidCloseTerminal(closed => {
      if (closed !== this.terminal) {
        return
      }
      this.terminal = null
      this.closeSubscription?.dispose()
      this.closeSubscription = null
    })
    this.terminal.show()
    this.terminal.sendText(buildSendText(command, platform), true)
  }

  dispose(): void {
    this.closeSubscription?.dispose()
    this.closeSubscription = null
    this.terminal?.dispose()
    this.terminal = null
  }
}
