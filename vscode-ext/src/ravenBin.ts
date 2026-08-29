/**
 * Resolve how to invoke `raven tui` on this machine.
 *
 * Resolution order:
 * 1. raven.executablePath (VS Code setting)
 * 2. RAVEN_BIN environment variable
 * 3. `raven` found on PATH via which/where
 * 4. `uv run raven` as a dev fallback (works inside a repo that declares raven)
 *
 * Every side effect (file existence, PATH lookup) is injected, so the function
 * is pure and unit-testable without touching a real machine.
 */

import { join, win32 } from 'node:path'

export interface RavenCommand {
  command: string
  args: string[]
  label: string
}

export interface ResolutionContext {
  env: NodeJS.ProcessEnv
  configExecutablePath: string
  extraArgs: string[]
  homeDir: string
  exists: (filePath: string) => boolean
  which: (command: string) => string | null
}

function tuiArgs(extraArgs: string[]): string[] {
  return ['tui', ...extraArgs]
}

export function sanitizeExtraArgs(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return []
  }
  return value.filter((arg): arg is string => typeof arg === 'string')
}

function expandHome(filePath: string, homeDir: string): string {
  if (filePath === '~') {
    return homeDir
  }
  if (filePath.startsWith('~/')) {
    return join(homeDir, filePath.slice(2))
  }
  if (filePath.startsWith('~\\')) {
    return win32.join(homeDir, filePath.slice(2))
  }
  return filePath
}

export function resolveRavenCommand(ctx: ResolutionContext): RavenCommand | null {
  const configuredPath = expandHome(ctx.configExecutablePath || ctx.env.RAVEN_BIN || '', ctx.homeDir)
  if (configuredPath) {
    if (!ctx.exists(configuredPath)) {
      return null
    }
    return { command: configuredPath, args: tuiArgs(ctx.extraArgs), label: 'configured path' }
  }

  const onPath = ctx.which('raven')
  if (onPath) {
    return { command: onPath, args: tuiArgs(ctx.extraArgs), label: 'raven on PATH' }
  }

  const uv = ctx.which('uv')
  if (uv) {
    return { command: uv, args: ['run', 'raven', ...tuiArgs(ctx.extraArgs)], label: 'uv run raven' }
  }

  return null
}
