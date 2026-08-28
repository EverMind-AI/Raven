/**
 * VS Code extension entry point.
 *
 * Registers the raven.openTui command plus a status bar shortcut. The command
 * resolves how to launch raven (setting, RAVEN_BIN, PATH, uv) and opens the
 * TUI inside a dedicated integrated terminal that is reused across
 * invocations.
 */

import { spawnSync } from 'node:child_process'
import { existsSync, statSync } from 'node:fs'
import { homedir } from 'node:os'

import { loadVscode, type VscodeApi, type VscodeDisposable } from './api.js'
import { findOnPath, type ProbeRunner } from './pathProbe.js'
import { resolveRavenCommand } from './ravenBin.js'
import { TuiTerminalManager } from './terminal.js'

const INSTALL_DOCS_URL = 'https://github.com/EverMind-AI/Raven#quick-start'

const runProbe: ProbeRunner = (command, args, { encoding, timeout }) => {
  const result = spawnSync(command, args, { encoding, timeout })
  return { status: result.status, stdout: result.stdout ?? null }
}

function isExistingFile(filePath: string): boolean {
  try {
    return statSync(filePath).isFile()
  } catch {
    return false
  }
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
      env: process.env,
      configExecutablePath: config.get('executablePath') ?? '',
      extraArgs: config.get('extraArgs') ?? [],
      homeDir: homedir(),
      exists: isExistingFile,
      which: name => findOnPath(process.platform, name, runProbe, existsSync)
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
