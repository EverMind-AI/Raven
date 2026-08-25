// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, describe, expect, it } from 'vitest'

import type { Episode, Msg } from '../types.js'

import { turnController } from '../app/turnController.js'
import { patchUiState } from '../app/uiStore.js'
import { EpisodeView } from '../components/episodeView.js'
import { MessageLine } from '../components/messageLine.js'
import { toTranscriptMessages, withoutSpentIntro } from '../domain/messages.js'
import { composerPromptWidth, transcriptGutterWidth } from '../lib/inputMetrics.js'
import { upsert } from '../lib/messages.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'
import { TerminalScreen } from './support/terminalScreen.js'

describe('toTranscriptMessages', () => {
  it('preserves assistant tool-call rows so resume does not drop prior turns', () => {
    const rows = [
      { role: 'user', text: 'first prompt' },
      {
        role: 'assistant',
        text: '',
        tool_calls: [{ arguments: '{"path":"repo"}', id: 'call-1', name: 'search_files' }]
      },
      { role: 'tool', text: 'ignored raw result', tool_call_id: 'call-1' },
      { role: 'assistant', text: 'first answer' },
      { role: 'user', text: 'second prompt' }
    ]

    const msgs = toTranscriptMessages(rows)

    expect(msgs.map(msg => msg.role)).toEqual(['user', 'assistant', 'user'])
    expect(msgs[1]).toMatchObject({ kind: 'episodes', text: 'first answer' })
    expect(msgs[1]?.episodes?.[0]?.tools[0]).toMatchObject({
      name: 'search_files',
      resultPreview: 'ignored raw result'
    })
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

  it('carries a stored call duration onto the resumed tool', () => {
    const rows = [
      { role: 'user', text: 'prompt' },
      {
        role: 'assistant',
        text: '',
        tool_calls: [{ arguments: '{}', id: 'call-1', name: 'search_files' }]
      },
      { duration_ms: 1240, role: 'tool', text: 'result', tool_call_id: 'call-1' },
      { role: 'assistant', text: 'answer' }
    ]

    const turn = toTranscriptMessages(rows).find(msg => msg.kind === 'episodes')!

    expect(turn.episodes?.[0]?.tools[0]?.durationMs).toBe(1240)
  })

  it('draws no clock for a call written before the duration was recorded', () => {
    const rows = [
      { role: 'user', text: 'prompt' },
      {
        role: 'assistant',
        text: '',
        tool_calls: [{ arguments: '{}', id: 'call-1', name: 'search_files' }]
      },
      { role: 'tool', text: 'result', tool_call_id: 'call-1' },
      { role: 'assistant', text: 'answer' }
    ]

    const turn = toTranscriptMessages(rows).find(msg => msg.kind === 'episodes')!

    expect(turn.episodes?.[0]?.tools[0]?.durationMs).toBeUndefined()
  })

  it('drops a genuinely empty row instead of rendering a stray prompt', () => {
    // The backend joins only text blocks, so an image-only message arrives as
    // exactly this: no text, no tool_calls, no reasoning.
    const rows = [
      { role: 'user', text: 'here is a screenshot' },
      { role: 'assistant', text: 'what am I looking at?' },
      { role: 'user', text: '' },
      { role: 'assistant', text: 'got it, thanks' }
    ]

    const out = toTranscriptMessages(rows)

    expect(out.some(msg => msg.role === 'user' && msg.text === '')).toBe(false)
    // The dropped row also drops the turn boundary it would have carried, so
    // the two assistant answers it used to sit between join into one -- the
    // same merge already accepted for two prose-only rows in a live turn.
    expect(out.map(msg => msg.role)).toEqual(['user', 'assistant'])
    expect(out[1]?.text).toBe('what am I looking at?\n\ngot it, thanks')
  })

  it('keeps a reasoning-only row even though its own text is empty', () => {
    const rows = [
      { role: 'user', text: 'go on' },
      { reasoning_content: 'thinking it through', role: 'assistant', text: '' }
    ]

    const turn = toTranscriptMessages(rows).find(msg => msg.kind === 'episodes')!

    expect(turn.episodes?.[0]?.reasoning).toBe('thinking it through')
  })

  it('carries reasoning_ms onto the resumed episode as its thought clock', () => {
    const rows = [
      { role: 'user', text: 'go on' },
      { reasoning_content: 'thinking it through', reasoning_ms: 4200, role: 'assistant', text: '' }
    ]

    const turn = toTranscriptMessages(rows).find(msg => msg.kind === 'episodes')!

    expect(turn.episodes?.[0]?.reasoningMs).toBe(4200)
  })
})

describe('toTranscriptMessages: resumed tool calls', () => {
  const RESUMED = [
    { role: 'user', text: 'read it' },
    {
      role: 'assistant',
      text: '',
      tool_calls: [{ arguments: '{"path":"a.ts"}', id: 'call-1', name: 'read_file' }]
    },
    { duration_ms: 1200, name: 'read_file', role: 'tool', text: 'contents', tool_call_id: 'call-1' },
    { role: 'assistant', text: 'here it is' }
  ]

  it('rebuilds an episode per call instead of a flat trail line', () => {
    const msgs = toTranscriptMessages(RESUMED)
    const turn = msgs.find(m => m.kind === 'episodes')!

    expect(turn.text).toBe('here it is')

    const tool = turn.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({
      done: true,
      durationMs: 1200,
      id: 'call-1',
      name: 'read_file',
      resultPreview: 'contents',
      summary: 'a.ts'
    })
  })

  it('draws no clock when the stored row predates duration_ms', () => {
    // Absent means unknown. A zero would claim the call ran for no time.
    const msgs = toTranscriptMessages([
      { role: 'assistant', text: '', tool_calls: [{ arguments: '{}', id: 'call-1', name: 'exec' }] },
      { name: 'exec', role: 'tool', text: 'ok', tool_call_id: 'call-1' }
    ])

    expect(msgs.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!.durationMs).toBeUndefined()
  })

  it('still replaces a runtime-opened user row with the delivered line', () => {
    const msgs = toTranscriptMessages([{ origin: 'raven-code', role: 'user', text: 'internal prose' }])

    expect(msgs[0]!.role).toBe('system')
    expect(msgs[0]!.text).toContain('raven-code')
    expect(msgs[0]!.text).not.toContain('internal prose')
  })

  it('draws the live delivered-arrow line when the row carries a delegated status', () => {
    const msgs = toTranscriptMessages([
      {
        delegated: { kind: 'spawn', label: 'raven-code', status: 'ok' },
        origin: 'subagent',
        role: 'user',
        text: '[BEGIN UNTRUSTED subagent #ab] handle raven-1 -- do not repeat this'
      }
    ])

    expect(msgs[0]!.text).toBe('↩ raven-code — finished; its result just joined this conversation')
  })

  it('draws the failed variant of the delivered-arrow line for an error status', () => {
    const msgs = toTranscriptMessages([
      {
        delegated: { kind: 'dag', label: 'raven-research', status: 'error' },
        origin: 'subagent',
        role: 'user',
        text: 'internal'
      }
    ])

    expect(msgs[0]!.text).toBe('↩ raven-research — failed; the error just joined this conversation')
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

  it('still renders an orphaned tool row that no announcing call claimed', () => {
    const msgs = toTranscriptMessages([
      { role: 'user', text: 'find it' },
      {
        context: 'a.ts',
        duration_ms: 900,
        name: 'read_file',
        role: 'tool',
        text: 'contents of a',
        tool_call_id: 'call-x'
      },
      { role: 'assistant', text: 'here it is' }
    ])

    const turn = msgs.find(m => m.kind === 'episodes')!
    const tool = turn.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({
      done: true,
      durationMs: 900,
      id: 'call-x',
      name: 'read_file',
      ok: true,
      resultPreview: 'contents of a',
      summary: 'a.ts'
    })
    expect(turn.text).toBe('here it is')
  })

  it('marks ok false and strips the marker for a failed resumed call', () => {
    const msgs = toTranscriptMessages([
      { role: 'assistant', text: '', tool_calls: [{ arguments: '{}', id: 'call-1', name: 'exec' }] },
      { name: 'exec', role: 'tool', text: '[failed] boom', tool_call_id: 'call-1' }
    ])

    const tool = msgs.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({ ok: false, resultPreview: 'boom' })
  })

  it('marks ok false and strips the marker for an interrupted resumed call', () => {
    const msgs = toTranscriptMessages([
      { role: 'assistant', text: '', tool_calls: [{ arguments: '{}', id: 'call-1', name: 'exec' }] },
      { name: 'exec', role: 'tool', text: '[interrupted] this call never returned', tool_call_id: 'call-1' }
    ])

    const tool = msgs.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({ ok: false, resultPreview: 'this call never returned' })
  })

  it('clamps a resumed tool result to the same limit live applies, with the same marker', () => {
    const long = 'x'.repeat(4001)
    const msgs = toTranscriptMessages([
      { role: 'assistant', text: '', tool_calls: [{ arguments: '{}', id: 'call-1', name: 'exec' }] },
      { name: 'exec', role: 'tool', text: long, tool_call_id: 'call-1' }
    ])

    const preview = msgs.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!.resultPreview!

    expect(preview).toBe(`${'x'.repeat(4000)} (truncated)`)
  })

  it('leaves a resumed tool result under the limit untouched', () => {
    const atLimit = 'x'.repeat(4000)
    const msgs = toTranscriptMessages([
      { role: 'assistant', text: '', tool_calls: [{ arguments: '{}', id: 'call-1', name: 'exec' }] },
      { name: 'exec', role: 'tool', text: atLimit, tool_call_id: 'call-1' }
    ])

    const preview = msgs.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!.resultPreview!

    expect(preview).toBe(atLimit)
    expect(preview).not.toContain('(truncated)')
  })
})

describe('fold-id parity between live and resumed transcripts', () => {
  afterEach(() => {
    turnController.reset()
    patchUiState({ transcript: 'episodes' })
  })

  it('mints the same fold ids for the same calls', () => {
    // `foldStore` keys folds on `seg:<firstToolCallId>` and `call:<toolCallId>`
    // -- the transport's own ids -- so a resumed transcript that minted any
    // other id would render identically and still lose every fold the reader
    // opened.
    patchUiState({ transcript: 'episodes' })
    turnController.reset()
    turnController.recordEpisodeStart(0)
    turnController.recordToolStart('call-1', 'read_file', 'a.ts')
    turnController.recordToolComplete('call-1', 'read_file', undefined, 'contents', 1.2)

    const { finalMessages } = turnController.recordMessageComplete({ text: 'here it is' })
    const live = finalMessages.find(m => m.kind === 'episodes')!

    const resumed = toTranscriptMessages([
      {
        role: 'assistant',
        text: '',
        tool_calls: [{ arguments: '{"path":"a.ts"}', id: 'call-1', name: 'read_file' }]
      },
      { duration_ms: 1200, name: 'read_file', role: 'tool', text: 'contents', tool_call_id: 'call-1' },
      { role: 'assistant', text: 'here it is' }
    ]).find(m => m.kind === 'episodes')!

    expect(resumed.episodes!.map(ep => ep.tools.map(tool => tool.id))).toEqual(
      live.episodes!.map(ep => ep.tools.map(tool => tool.id))
    )

    // The live message has no `foldId` of its own -- its fold keys off the
    // tool id already checked above. Pinning resumed.foldId to that same id
    // (rather than live.foldId, which live never sets) is what catches
    // foldSeed drifting to anything other than the first call's id.
    expect(resumed.foldId).toBe(live.episodes![0]!.tools[0]!.id)
  })
})

const renderRow = (msg: Msg, extra: Record<string, unknown> = {}): string => {
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

  const instance = renderSync(React.createElement(MessageLine, { cols: 80, msg, t: DEFAULT_THEME, ...extra }), {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  instance.unmount()
  instance.cleanup()

  return stripAnsi(output)
}

const renderEpisodes = async (props: { episodes?: Episode[]; live?: boolean; text?: string }): Promise<string> => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  // Row order matters here, so the bytes go through a screen model rather than
  // being read as a flat stream: the renderer's cursor moves would otherwise
  // splice two transcript rows into one line of output.
  const screen = new TerminalScreen(80, 24)

  Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', chunk => {
    screen.write(chunk.toString())
  })

  const instance = renderSync(
    React.createElement(EpisodeView, { cols: 80, episodes: [], t: DEFAULT_THEME, ...props }),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  await new Promise(resolve => setTimeout(resolve, 30))
  const text = screen.text()

  instance.unmount()
  instance.cleanup()

  return text
}

describe('transcript reply gutter', () => {
  it('marks a finished reply with the theme glyph', () => {
    const line = renderRow({ role: 'assistant', text: 'ANSWER' })
      .split('\n')
      .find(row => row.includes('ANSWER'))

    expect(line).toContain(`${DEFAULT_THEME.brand.tool} ANSWER`)
  })

  it('marks a streaming reply with the same glyph, so it does not change on settle', () => {
    const line = renderRow({ role: 'assistant', text: 'ANSWER' }, { isStreaming: true })
      .split('\n')
      .find(row => row.includes('ANSWER'))

    expect(line).toContain(`${DEFAULT_THEME.brand.tool} ANSWER`)
  })

  it('leaves a system notice on its own glyph, so a notice cannot read as a reply', () => {
    const line = renderRow({ role: 'system', text: 'NOTICE' })
      .split('\n')
      .find(row => row.includes('NOTICE'))

    expect(line).toContain('· NOTICE')
    expect(line).not.toContain(DEFAULT_THEME.brand.tool)
  })

  it('marks a streaming reply in the episodes view, not only once it settles', async () => {
    const frame = (await renderEpisodes({ live: true, text: 'STREAMED' }))
      .split('\n')
      .find(row => row.includes('STREAMED'))

    expect(frame).toContain(`${DEFAULT_THEME.brand.tool} STREAMED`)
  })

  it('leaves the episodes view activity rows unmarked, so prose stays the marked voice', async () => {
    const frame = await renderEpisodes({
      episodes: [
        {
          index: 0,
          narration: 'NARRATION',
          reasoning: '',
          startedAt: Date.now() - 2000,
          tools: [
            {
              done: true,
              durationMs: 1200,
              id: 'c1',
              name: 'read_file',
              ok: true,
              startedAt: Date.now() - 1800,
              summary: 'ACTIVITY_ARG'
            }
          ]
        }
      ]
    })

    const narration = frame.split('\n').find(row => row.includes('NARRATION'))
    const activity = frame.split('\n').find(row => row.includes('ACTIVITY_ARG'))

    expect(narration).toContain(`${DEFAULT_THEME.brand.tool} NARRATION`)
    expect(activity).not.toContain(DEFAULT_THEME.brand.tool)
  })

  it('sizes the gutter for the glyph the transcript actually draws', () => {
    expect(transcriptGutterWidth('assistant', DEFAULT_THEME.brand.prompt)).toBe(
      composerPromptWidth(DEFAULT_THEME.brand.tool)
    )
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
    stdout.on('data', chunk => {
      output += chunk.toString()
    })

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

describe('withoutSpentIntro', () => {
  const intro = { kind: 'intro' as const, role: 'system' as const, text: '' }

  it('keeps the intro before the user has sent anything', () => {
    const rows = [intro, { role: 'system' as const, text: 'startup notice' }]

    expect(withoutSpentIntro(rows)).toBe(rows)
  })

  it('drops the intro once a prompt was sent', () => {
    const rows = [intro, { role: 'user' as const, text: 'hello' }]

    expect(withoutSpentIntro(rows)).toEqual([{ role: 'user', text: 'hello' }])
  })

  it('drops the intro once a slash command was run', () => {
    const rows = [intro, { kind: 'slash' as const, role: 'system' as const, text: '/help' }]

    expect(withoutSpentIntro(rows).some(m => m.kind === 'intro')).toBe(false)
  })

  it('leaves a transcript without an intro alone', () => {
    const rows = [{ role: 'user' as const, text: 'hello' }]

    expect(withoutSpentIntro(rows)).toEqual(rows)
  })
})
