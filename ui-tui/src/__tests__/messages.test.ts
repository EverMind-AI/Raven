// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it } from 'vitest'

import { MessageLine } from '../components/messageLine.js'
import { toTranscriptMessages } from '../domain/messages.js'
import { upsert } from '../lib/messages.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

describe('toTranscriptMessages', () => {
  it('preserves assistant tool-call rows so resume does not drop prior turns', () => {
    const rows = [
      { role: 'user', text: 'first prompt' },
      { role: 'tool', context: 'repo', name: 'search_files', text: 'ignored raw result' },
      { role: 'assistant', text: 'first answer' },
      { role: 'user', text: 'second prompt' }
    ]

    expect(toTranscriptMessages(rows).map(msg => [msg.role, msg.text])).toEqual([
      ['user', 'first prompt'],
      ['assistant', 'first answer'],
      ['user', 'second prompt']
    ])
    expect(toTranscriptMessages(rows)[1]?.tools?.[0]).toContain('Search Files')
  })

  /* A turn the runtime opened, replayed. Its text is internal prose -- a
     sub-agent announce carries an untrusted fence, an instance handle and an
     instruction not to repeat either to the user -- and every `role=user` row
     was pushed as typed words, so reopening the session drew all of it as the
     user's own. */
  it('does not replay a runtime-opened turn as the user talking', () => {
    const rows = [
      { role: 'user', text: 'find the bug' },
      {
        origin: 'subagent',
        role: 'user',
        text: '[BEGIN UNTRUSTED subagent #ab] handle raven-1 -- do not repeat this'
      },
      { role: 'assistant', text: 'done' }
    ]

    const out = toTranscriptMessages(rows)

    expect(out.map(msg => msg.role)).toEqual(['user', 'system', 'assistant'])
    expect(out[1]?.text).toContain('subagent')
    expect(out[1]?.text).not.toContain('UNTRUSTED')
    expect(out[1]?.text).not.toContain('raven-1')
  })

  it('still replays a person as the user', () => {
    const out = toTranscriptMessages([{ role: 'user', text: 'typed by hand' }])

    expect(out.map(msg => [msg.role, msg.text])).toEqual([['user', 'typed by hand']])
  })

  it('carries a stored call duration onto the resumed trail line', () => {
    const rows = [
      { role: 'user', text: 'prompt' },
      { duration_ms: 1240, name: 'search_files', role: 'tool', text: 'result' },
      { role: 'assistant', text: 'answer' }
    ]

    expect(toTranscriptMessages(rows)[1]?.tools?.[0]).toBe('Search Files (1.2s) ✓')
  })

  it('draws no clock for a call written before the duration was recorded', () => {
    const rows = [
      { role: 'user', text: 'prompt' },
      { name: 'search_files', role: 'tool', text: 'result' },
      { role: 'assistant', text: 'answer' }
    ]

    expect(toTranscriptMessages(rows)[1]?.tools?.[0]).toBe('Search Files ✓')
  })

  it('restores delivered and changed files after a restart', () => {
    const rows = [
      { role: 'user', text: 'build it' },
      {
        role: 'assistant',
        text: '',
        tool_calls: [
          { id: 'w1', name: 'write_file', arguments: JSON.stringify({ path: '/tmp/report.md' }) },
          { id: 'e1', name: 'edit_file', arguments: JSON.stringify({ file_path: '/tmp/chart.csv' }) }
        ]
      },
      {
        role: 'tool',
        name: 'deliver_files',
        text: 'Delivered report.md',
        metadata: {
          raven_delivery: {
            files: [{ name: 'report.md', path: '/tmp/report.md', size: 1200, missing: true }]
          }
        }
      },
      { role: 'assistant', text: 'Done.' }
    ]

    const artifacts = toTranscriptMessages(rows).find(msg => msg.kind === 'artifacts')?.artifacts

    expect(artifacts?.deliveries).toEqual([
      { ext: 'MD', missing: true, name: 'report.md', size: 1200, title: 'report.md' }
    ])
    expect(artifacts?.changes).toEqual([
      { change: 'new', ext: 'MD', name: 'report.md' },
      { change: 'edit', ext: 'CSV', name: 'chart.csv' }
    ])
  })
})

describe('MessageLine', () => {
  it('preserves a separator after compound user prompt glyphs in transcript rows', () => {
    const stdout = new PassThrough()
    const stdin = new PassThrough()
    const stderr = new PassThrough()
    let output = ''

    Object.assign(stdout, { columns: 80, isTTY: false, rows: 24 })
    Object.assign(stdin, { isTTY: false })
    Object.assign(stderr, { isTTY: false })
    stdout.on('data', chunk => {
      output += chunk.toString()
    })

    const t = {
      ...DEFAULT_THEME,
      brand: { ...DEFAULT_THEME.brand, prompt: 'Ψ >' }
    }

    const instance = renderSync(
      React.createElement(MessageLine, {
        cols: 80,
        msg: { role: 'user', text: 'Okay' },
        t
      }),
      {
        patchConsole: false,
        stderr: stderr as NodeJS.WriteStream,
        stdin: stdin as NodeJS.ReadStream,
        stdout: stdout as NodeJS.WriteStream
      }
    )

    instance.unmount()
    instance.cleanup()

    const renderedLine = stripAnsi(output)
      .split('\n')
      .find(line => line.includes('Okay'))

    expect(renderedLine).toContain('Ψ > Okay')
  })

  it('renders compact artifact sections with a missing marker', () => {
    const stdout = new PassThrough()
    const stdin = new PassThrough()
    const stderr = new PassThrough()
    let output = ''

    Object.assign(stdout, { columns: 80, isTTY: false, rows: 24 })
    Object.assign(stdin, { isTTY: false })
    Object.assign(stderr, { isTTY: false })
    stdout.on('data', chunk => { output += chunk.toString() })

    const instance = renderSync(
      React.createElement(MessageLine, {
        cols: 80,
        msg: {
          artifacts: {
            changes: [{ change: 'edit', ext: 'CSV', name: 'pricing.csv' }],
            deliveries: [{ ext: 'PDF', missing: true, name: 'report.pdf', title: 'Final report' }]
          },
          kind: 'artifacts',
          role: 'system',
          text: ''
        },
        t: DEFAULT_THEME
      }),
      {
        patchConsole: false,
        stderr: stderr as NodeJS.WriteStream,
        stdin: stdin as NodeJS.ReadStream,
        stdout: stdout as NodeJS.WriteStream
      }
    )

    instance.unmount()
    instance.cleanup()

    const rendered = stripAnsi(output)
    expect(rendered).toContain('Final report')
    expect(rendered).toContain('[missing]')
    expect(rendered).toContain('pricing.csv')
  })
})

describe('upsert', () => {
  it('appends when last role differs', () => {
    expect(upsert([{ role: 'user', text: 'hi' }], 'assistant', 'hello')).toHaveLength(2)
  })

  it('replaces when last role matches', () => {
    expect(upsert([{ role: 'assistant', text: 'partial' }], 'assistant', 'full')[0]!.text).toBe('full')
  })

  it('appends to empty', () => {
    expect(upsert([], 'user', 'first')).toEqual([{ role: 'user', text: 'first' }])
  })

  it('does not mutate', () => {
    const prev = [{ role: 'user' as const, text: 'hi' }]
    upsert(prev, 'assistant', 'yo')
    expect(prev).toHaveLength(1)
  })
})
