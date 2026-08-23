// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { type ClipboardPath } from '@hermes/ink'
import { execFile, spawn } from 'node:child_process'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)
const CLIPBOARD_MAX_BUFFER = 4 * 1024 * 1024
const POWERSHELL_ARGS = ['-NoProfile', '-NonInteractive', '-Command', 'Get-Clipboard -Raw'] as const

type ClipboardRun = typeof execFileAsync

export function isUsableClipboardText(text: null | string): text is string {
  if (!text || !/[^\s]/.test(text)) {
    return false
  }

  if (text.includes('\u0000')) {
    return false
  }

  let suspicious = 0

  for (const ch of text) {
    const code = ch.charCodeAt(0)
    const isControl = code < 0x20 && ch !== '\n' && ch !== '\r' && ch !== '\t'

    if (isControl || ch === '\ufffd') {
      suspicious += 1
    }
  }

  return suspicious <= Math.max(2, Math.floor(text.length * 0.02))
}

function readClipboardCommands(
  platform: NodeJS.Platform,
  env: NodeJS.ProcessEnv
): Array<{ args: readonly string[]; cmd: string }> {
  if (platform === 'darwin') {
    return [{ cmd: 'pbpaste', args: [] }]
  }

  if (platform === 'win32') {
    return [{ cmd: 'powershell', args: POWERSHELL_ARGS }]
  }

  const attempts: Array<{ args: readonly string[]; cmd: string }> = []

  if (env.WSL_INTEROP || env.WSL_DISTRO_NAME) {
    attempts.push({ cmd: 'powershell.exe', args: POWERSHELL_ARGS })
  }

  if (env.WAYLAND_DISPLAY) {
    attempts.push({ cmd: 'wl-paste', args: ['--type', 'text'] })
  }

  attempts.push({ cmd: 'xclip', args: ['-selection', 'clipboard', '-out'] })

  return attempts
}

/**
 * Read plain text from the system clipboard.
 *
 * Uses native platform tools in fallback order:
 * - macOS: pbpaste
 * - Windows: PowerShell Get-Clipboard -Raw
 * - WSL: powershell.exe Get-Clipboard -Raw
 * - Linux Wayland: wl-paste --type text
 * - Linux X11: xclip -selection clipboard -out
 */
export async function readClipboardText(
  platform: NodeJS.Platform = process.platform,
  run: ClipboardRun = execFileAsync,
  env: NodeJS.ProcessEnv = process.env
): Promise<string | null> {
  for (const attempt of readClipboardCommands(platform, env)) {
    try {
      const result = await run(attempt.cmd, [...attempt.args], {
        encoding: 'utf8',
        maxBuffer: CLIPBOARD_MAX_BUFFER,
        windowsHide: true
      })

      if (typeof result.stdout === 'string') {
        return result.stdout
      }
    } catch {
      // Fall through to the next clipboard backend.
    }
  }

  return null
}

function writeClipboardCommands(
  platform: NodeJS.Platform,
  env: NodeJS.ProcessEnv
): Array<{ args: readonly string[]; cmd: string }> {
  if (platform === 'darwin') {
    return [{ cmd: 'pbcopy', args: [] }]
  }

  if (platform === 'win32') {
    return [{ cmd: 'powershell', args: ['-NoProfile', '-NonInteractive', '-Command', 'Set-Clipboard -Value $input'] }]
  }

  const attempts: Array<{ args: readonly string[]; cmd: string }> = []

  if (env.WSL_INTEROP || env.WSL_DISTRO_NAME) {
    attempts.push({
      cmd: 'powershell.exe',
      args: ['-NoProfile', '-NonInteractive', '-Command', 'Set-Clipboard -Value $input']
    })
  }

  if (env.WAYLAND_DISPLAY) {
    attempts.push({ cmd: 'wl-copy', args: ['--type', 'text/plain'] })
  }

  attempts.push({ cmd: 'xclip', args: ['-selection', 'clipboard', '-in'] })
  attempts.push({ cmd: 'xsel', args: ['--clipboard', '--input'] })

  return attempts
}

/**
 * Write plain text to the system clipboard.
 *
 * Tries native platform tools in fallback order:
 * - macOS: pbcopy
 * - Windows: PowerShell Set-Clipboard
 * - WSL: powershell.exe Set-Clipboard
 * - Linux Wayland: wl-copy --type text/plain
 * - Linux X11: xclip -selection clipboard -in
 * - Linux X11 alt: xsel --clipboard --input
 *
 * Returns true if at least one backend succeeded, false otherwise
 * (callers should fall back to OSC52 on false).
 */
export async function writeClipboardText(
  text: string,
  platform: NodeJS.Platform = process.platform,
  start: typeof spawn = spawn,
  env: NodeJS.ProcessEnv = process.env
): Promise<boolean> {
  const candidates = writeClipboardCommands(platform, env)

  for (const { cmd, args } of candidates) {
    try {
      const ok = await new Promise<boolean>(resolve => {
        const child = start(cmd, [...args], { stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true })

        child.once('error', () => resolve(false))
        child.once('close', code => resolve(code === 0))
        child.stdin?.end(text)
      })

      if (ok) {
        return true
      }
    } catch {
      // Fall through to the next clipboard backend.
    }
  }

  return false
}

/**
 * Transcript line for a completed copy, naming the channel it actually took.
 *
 * Only the OSC 52 path can silently fail: the bytes reach the terminal and
 * the terminal decides whether to honour them, which is a setting the user
 * owns. Saying so there -- and not saying it where a native tool really did
 * write the clipboard -- is what keeps the wording worth reading.
 */
function copiedCount(charCount: number): string {
  return `copied ${charCount} character${charCount === 1 ? '' : 's'}`
}

export function copyResultNotice(charCount: number, path: ClipboardPath): string {
  const copied = copiedCount(charCount)

  switch (path) {
    case 'native':
      return copied

    case 'osc52':
      return `${copied} via OSC 52 — if the paste comes up empty, allow clipboard access in your terminal`

    case 'tmux-buffer':
      return `${copied} to the tmux buffer — reaching the system clipboard needs tmux set-clipboard`
  }
}

/**
 * Transcript line for an automatic copy-on-select write.
 *
 * This fires on every drag, so it stays terse -- except the first one of a
 * session, which carries the path caveat. OSC 52 is the one path the terminal
 * can still refuse, and the first copy is the only moment a user is looking
 * for the reason a paste came up empty.
 */
export function copyOnSelectNotice(charCount: number, path: ClipboardPath, firstOfSession: boolean): string {
  return firstOfSession ? copyResultNotice(charCount, path) : copiedCount(charCount)
}
