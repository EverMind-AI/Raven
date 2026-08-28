/**
 * VS Code extension entry point.
 *
 * Registers the raven.openTui command plus a status bar shortcut. The command
 * resolves how to launch raven (setting, RAVEN_BIN, PATH, uv) and opens the
 * TUI inside a dedicated integrated terminal that is reused across
 * invocations.
 */

import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'

import { loadVscode, type VscodeApi, type VscodeDisposable } from './api.js'
import { resolveRavenCommand } from './ravenBin.js'
import { TuiTerminalManager } from './terminal.js'

const INSTALL_DOCS_URL = 'https://github.com/EverMind-AI/Raven#quick-start'

function firstExistingPath(stdout: string): string | null {
  return (
    stdout
      .split(/\r?\n/)
      .map(line => line.trim())
      .find(line => line.length > 0 && existsSync(line)) ?? null
  )
}

function findOnPath(platform: NodeJS.Platform, command: string): string | null {
  const probe = platform === 'win32' ? 'where' : 'command'
  const probeArgs = platform === 'win32' ? [command] : ['-v', command]
  const result = spawnSync(probe, probeArgs, { encoding: 'utf8' })
  const direct = result.status === 0 ? firstExistingPath(result.stdout) : null
  if (direct || platform === 'win32') {
    return direct
  }

  // GUI-launched VS Code does not inherit the shell rc PATH, so which/where
  // can miss binaries the user's terminal sees. Re-probe inside a login shell.
  for (const shell of ['bash', 'zsh']) {
    const shellResult = spawnSync(shell, ['-lic', `command -v ${command}`], { encoding: 'utf8', timeout: 5000 })
    const fromShell = shellResult.status === 0 ? firstExistingPath(shellResult.stdout) : null
    if (fromShell) {
      return fromShell
    }
  }
  return null
}

function firstWorkspacePath(api: VscodeApi): string | undefined {
  return api.workspace.workspaceFolders?.[0]?.uri.fsPath
}

export async function activate(context: { subscriptions: VscodeDisposable[] }): Promise<void> {
  const api = await loadVscode()
  if (!api) {
    return
  }

  const manager = new TuiTerminalManager(api)

  const openTui = async (): Promise<void> => {
    const config = api.workspace.getConfiguration('raven')
    const command = resolveRavenCommand({
      platform: process.platform,
      env: process.env,
      configExecutablePath: config.get('executablePath') ?? '',
      extraArgs: config.get('extraArgs') ?? [],
      homeDir: homedir(),
      exists: existsSync,
      which: name => findOnPath(process.platform, name)
    })
    if (!command) {
      await api.window.showErrorMessage(
        `Raven executable not found. Install it from [GitHub](${INSTALL_DOCS_URL}) or set **raven.executablePath**.`
      )
      return
    }
    manager.open(command, firstWorkspacePath(api), process.platform)
  }

  context.subscriptions.push(api.commands.registerCommand('raven.openTui', openTui))

  const statusBar = api.window.createStatusBarItem(api.StatusBarAlignment.Left, 1)
  statusBar.text = '$(terminal) Raven'
  statusBar.tooltip = 'Open Raven TUI'
  statusBar.command = 'raven.openTui'
  statusBar.show()
  context.subscriptions.push(statusBar)
}
