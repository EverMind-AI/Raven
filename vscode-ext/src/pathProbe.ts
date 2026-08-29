/**
 * Probe the user's login shell for PATH entries and environment variables.
 *
 * GUI-launched VS Code does not inherit the shell rc PATH or env (proxy
 * settings, API keys), so a plain which/where probe can miss binaries and a
 * launched process can miss configuration that a direct terminal run would
 * have. This module therefore falls back to probing inside login shells
 * (bash/zsh on posix, powershell.exe/pwsh on Windows). Every side effect is
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

function parseEnvLines(stdout: string): Record<string, string> {
  const env: Record<string, string> = {}
  for (const line of stdout.split(/\r?\n/)) {
    const match = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(line)
    if (match) {
      env[match[1]] = match[2]
    }
  }
  return env
}

export function probeLoginShellEnv(
  runner: ProbeRunner,
  shellHint: string | undefined,
  platform: NodeJS.Platform
): Record<string, string> | null {
  if (platform === 'win32') {
    return probeWindowsEnv(runner)
  }
  return probePosixEnv(runner, shellHint)
}

function probeWindowsEnv(runner: ProbeRunner): Record<string, string> | null {
  const script =
    '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-ChildItem Env: | ForEach-Object { "$($_.Name)=$($_.Value)" }'
  for (const shell of ['powershell.exe', 'pwsh']) {
    const result = runner(shell, ['-NoProfile', '-NonInteractive', '-Command', script], {
      encoding: 'utf8',
      timeout: 10000
    })
    if (result.status !== 0 || !result.stdout) {
      continue
    }
    const env = parseEnvLines(result.stdout)
    if (Object.keys(env).length > 0) {
      return env
    }
  }
  return null
}

function probePosixEnv(runner: ProbeRunner, shellHint: string | undefined): Record<string, string> | null {
  const candidates: string[] = []
  if (shellHint && (shellHint.endsWith('bash') || shellHint.endsWith('zsh'))) {
    candidates.push(shellHint)
  }
  for (const shell of ['bash', 'zsh']) {
    if (!candidates.includes(shell)) {
      candidates.push(shell)
    }
  }

  for (const shell of candidates) {
    const result = runner(shell, ['-lic', 'env'], { encoding: 'utf8', timeout: 5000 })
    if (result.status !== 0 || !result.stdout) {
      continue
    }
    const env = parseEnvLines(result.stdout)
    if (Object.keys(env).length > 0) {
      return env
    }
  }
  return null
}
