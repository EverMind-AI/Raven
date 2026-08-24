// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { FoldRow } from '../domain/episodeFold.js'

import { callIntent, foldRowsIntoEpisodes } from '../domain/episodeFold.js'

const row = (over: Partial<FoldRow> & Pick<FoldRow, 'role'>): FoldRow => ({ text: '', ...over })

describe('foldRowsIntoEpisodes', () => {
  it('builds one episodes message per turn, with a tool per call', () => {
    const msgs = foldRowsIntoEpisodes([
      row({ role: 'user', text: 'do it' }),
      row({
        role: 'assistant',
        calls: [{ arguments: '{"path":"a.ts"}', id: 'c1', name: 'read_file' }],
        foldSeed: 'c1'
      }),
      row({ durationMs: 1200, role: 'tool', text: 'contents', toolCallId: 'c1' }),
      row({ role: 'assistant', text: 'done' })
    ])

    expect(msgs[0]).toEqual({ role: 'user', text: 'do it' })

    const turn = msgs[1]!

    expect(turn.kind).toBe('episodes')
    expect(turn.text).toBe('done')

    const tool = turn.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({
      durationMs: 1200,
      id: 'c1',
      name: 'read_file',
      resultPreview: 'contents',
      summary: 'a.ts'
    })
  })

  it('leaves a call unanswered when no tool row matches it', () => {
    // An interrupted turn stores the call and never its result. The row still
    // has to appear, or the transcript loses the fact that it was made.
    const msgs = foldRowsIntoEpisodes([
      row({ role: 'assistant', calls: [{ arguments: '{}', id: 'c9', name: 'exec' }], foldSeed: 'c9' })
    ])
    const tool = msgs[0]!.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({ id: 'c9' })
    expect(tool.resultPreview).toBeUndefined()
  })

  it('keeps a thought that produced no call', () => {
    const msgs = foldRowsIntoEpisodes([row({ reasoning: 'thinking', role: 'assistant' })])

    expect(msgs[0]!.episodes![0]).toMatchObject({ reasoning: 'thinking', tools: [] })
  })

  it('carries a thinking time onto an episode that also made a call', () => {
    const msgs = foldRowsIntoEpisodes([
      row({
        calls: [{ arguments: '{}', id: 'c1', name: 'exec' }],
        foldSeed: 'c1',
        reasoning: 'planning the call',
        reasoningMs: 900,
        role: 'assistant'
      })
    ])

    expect(msgs[0]!.episodes![0]).toMatchObject({ reasoning: 'planning the call', reasoningMs: 900 })
  })

  it('derives a duration from wall clocks when none was supplied', () => {
    // The direct-chat records carry two timestamps and no duration; the resume
    // payload carries a duration and no start. One core has to serve both.
    const msgs = foldRowsIntoEpisodes([
      row({ atMs: 1000, role: 'assistant', calls: [{ arguments: '{}', id: 'c1', name: 'exec' }], foldSeed: 'c1' }),
      row({ atMs: 1750, role: 'tool', text: 'ok', toolCallId: 'c1' })
    ])

    expect(msgs[0]!.episodes![0]!.tools[0]!.durationMs).toBe(750)
  })

  it('passes a system row through and closes the turn before it', () => {
    // The resume path turns a runtime-opened row into a system note; it belongs
    // between turns, not inside one.
    const msgs = foldRowsIntoEpisodes([
      row({ role: 'assistant', calls: [{ arguments: '{}', id: 'c1', name: 'exec' }], foldSeed: 'c1' }),
      row({ role: 'system', text: 'raven-code delivered' })
    ])

    expect(msgs.map(m => m.role)).toEqual(['assistant', 'system'])
    expect(msgs[1]).toEqual({ role: 'system', text: 'raven-code delivered' })
  })

  it('marks a resumed call failed when the runtime wrote the failed marker', () => {
    // A resumed row never has a real `ok` field of its own -- only the marker
    // the runtime prefixed onto the result says the call failed.
    const msgs = foldRowsIntoEpisodes([
      row({ role: 'assistant', calls: [{ arguments: '{}', id: 'c1', name: 'exec' }], foldSeed: 'c1' }),
      row({ role: 'tool', text: '[failed] boom', toolCallId: 'c1' })
    ])
    const tool = msgs[0]!.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({ ok: false, resultPreview: 'boom' })
  })

  it('marks a resumed call failed when it was interrupted mid-call', () => {
    const msgs = foldRowsIntoEpisodes([
      row({ role: 'assistant', calls: [{ arguments: '{}', id: 'c1', name: 'exec' }], foldSeed: 'c1' }),
      row({ role: 'tool', text: '[interrupted] this call never returned', toolCallId: 'c1' })
    ])
    const tool = msgs[0]!.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({ ok: false, resultPreview: 'this call never returned' })
  })

  it('marks an orphaned call failed from its own marked result', () => {
    const msgs = foldRowsIntoEpisodes([row({ name: 'exec', role: 'tool', text: '[failed] boom', toolCallId: 'c9' })])
    const tool = msgs[0]!.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({ ok: false, resultPreview: 'boom' })
  })

  it('leaves an explicit ok alone even when the text looks like a marker', () => {
    // Direct-chat rows always set `ok` themselves; only the absent case (a
    // resumed row) should fall through to the marker check.
    const msgs = foldRowsIntoEpisodes([
      row({ role: 'assistant', calls: [{ arguments: '{}', id: 'c1', name: 'exec' }], foldSeed: 'c1' }),
      row({ ok: true, role: 'tool', text: '[failed] not actually stripped here', toolCallId: 'c1' })
    ])
    const tool = msgs[0]!.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({ ok: true, resultPreview: '[failed] not actually stripped here' })
  })
})

describe('callIntent', () => {
  it('reads the description claude code sends beside a command', () => {
    expect(callIntent('{"command":"ls","description":"List the repo"}')).toBe('List the repo')
  })

  it('is empty for a call that carries no description', () => {
    expect(callIntent('{"command":"ls"}')).toBe('')
  })

  it('is empty for arguments that are not an object', () => {
    expect(callIntent('not json')).toBe('')
    expect(callIntent('"a bare string"')).toBe('')
  })
})
