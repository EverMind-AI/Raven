/* The status bar's words: each starts with a capital, the bar's colour is
   picked through the same table the writers use, and no writer in src/app
   still spells a word the old lowercase way. */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import { errorStatus, STATUS, statusColorOf } from '../app/statusText.js'

const APP = join(dirname(fileURLToPath(import.meta.url)), '..', 'app')

/* Every spelling the status bar used before the words were capitalised. A
   literal of one of these left in src/app is a writer or a comparison the
   table missed, and the bar would show it lowercase or lose its colour. A
   failed turn's line is built, so its old `error: ` template is checked on
   the lines that write a status; transcript notes keep their own wording. */
const RETIRED = [
  'summoning raven…', 'ready', 'running…', 'forging session…', 'resuming…', 'resuming most recent…',
  'starting agent…', 'waiting for input…', 'setup running…', 'setup required', 'queued for next turn',
  'interpolating…', 'approval needed', 'confirm needed', 'sudo password needed', 'secret input needed',
  'gateway startup timeout', 'protocol warning', 'gateway exited',
]

const sources = (dir: string): string[] =>
  readdirSync(dir).flatMap(name => {
    const path = join(dir, name)

    if (statSync(path).isDirectory()) {
      return sources(path)
    }

    return /\.tsx?$/.test(name) ? [path] : []
  })

const PALETTE = { error: 'red', muted: 'grey', ok: 'green', warn: 'yellow' }

describe('the status bar words', () => {
  it('starts every status word with a capital', () => {
    expect(Object.values(STATUS).filter(v => !/^[A-Z]/.test(v))).toEqual([])
  })

  it('paints Ready in the ok colour', () => {
    expect(statusColorOf(STATUS.ready, PALETTE)).toBe('green')
    expect(statusColorOf(STATUS.running, PALETTE)).toBe('grey')
  })

  it('paints a failed turn red, under a capitalised word', () => {
    expect(errorStatus('model not available')).toBe('Error: model not available')
    expect(statusColorOf(errorStatus('model not available'), PALETTE)).toBe('red')
  })

  it('leaves no retired lowercase status literal in src/app', () => {
    const hits: string[] = []

    for (const file of sources(APP)) {
      readFileSync(file, 'utf8')
        .split('\n')
        .forEach((line, i) => {
          const code = line.trimStart()

          if (code.startsWith('//') || code.startsWith('*')) {
            return
          }

          const where = `${relative(APP, file)}:${i + 1}`

          for (const word of RETIRED) {
            if (line.includes(`'${word}'`)) {
              hits.push(`${where} ${word}`)
            }
          }

          if (line.includes('`error: ') && /status:|setStatus\(/.test(line)) {
            hits.push(`${where} error: template`)
          }
        })
    }

    expect(hits).toEqual([])
  })
})
