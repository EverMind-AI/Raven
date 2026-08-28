/**
 * Probe for an executable on PATH.
 *
 * GUI-launched VS Code does not inherit the shell rc PATH, so a plain
 * which/where probe can miss binaries the user's terminal sees. This module
 * therefore falls back to probing inside login shells. Every side effect is
 * injected so the probing logic is unit-testable.
 */

export interface ProbeResult {
  status: number | null
  stdout: string | null
}

export interface ProbeRunner {
  (command: string, args: string[], options: { encoding: 'utf8'; timeout?: number }): ProbeResult
}

function firstExistingPath(stdout: string | null, exists: (filePath: string) => boolean): string | null {
  if (!stdout) {
    return null
  }
  return (
    stdout
      .split(/\r?\n/)
      .map(line => line.trim())
      .find(line => line.length > 0 && exists(line)) ?? null
  )
}

export function findOnPath(
  platform: NodeJS.Platform,
  command: string,
  runner: ProbeRunner,
  exists: (filePath: string) => boolean
): string | null {
  const isWin = platform === 'win32'
  const direct = runner(isWin ? 'where' : 'which', [command], { encoding: 'utf8' })
  const directPath = direct.status === 0 ? firstExistingPath(direct.stdout, exists) : null
  if (directPath || isWin) {
    return directPath
  }

  for (const shell of ['bash', 'zsh']) {
    const result = runner(shell, ['-lic', `command -v ${command}`], { encoding: 'utf8', timeout: 5000 })
    const fromShell = result.status === 0 ? firstExistingPath(result.stdout, exists) : null
    if (fromShell) {
      return fromShell
    }
  }
  return null
}
