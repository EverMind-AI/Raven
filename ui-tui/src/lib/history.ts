// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { ravenHomeDir } from './ravenHome.js'

const MAX = 1000
const dir = ravenHomeDir()
const file = join(dir, '.raven_history')

let cache: string[] | null = null

export function load() {
  if (cache) {
    return cache
  }

  try {
    if (!existsSync(file)) {
      cache = []

      return cache
    }

    const entries: string[] = []
    let current: string[] = []

    // Split the way the Python reader reads: `Path.read_text` applies
    // universal-newline translation, which folds a lone CR as well as CRLF.
    // A CR kept at the end of a line would ride into the entry and defeat the
    // adjacent-duplicate check.
    for (const line of readFileSync(file, 'utf8').split(/\r\n|\n|\r/)) {
      if (line.startsWith('+')) {
        current.push(line.slice(1))
      } else if (current.length) {
        entries.push(current.join('\n'))
        current = []
      }
    }

    if (current.length) {
      entries.push(current.join('\n'))
    }

    cache = entries.slice(-MAX)
  } catch {
    cache = []
  }

  return cache
}

export function append(line: string) {
  const trimmed = line.trim()

  if (!trimmed) {
    return
  }

  const items = load()

  if (items.at(-1) === trimmed) {
    return
  }

  items.push(trimmed)

  if (items.length > MAX) {
    items.splice(0, items.length - MAX)
  }

  try {
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true })
    }

    const ts = new Date().toISOString().replace('T', ' ').replace('Z', '')

    const encoded = trimmed
      .split('\n')
      .map(l => `+${l}`)
      .join('\n')

    appendFileSync(file, `\n# ${ts}\n${encoded}\n`)
  } catch {
    void 0
  }
}
