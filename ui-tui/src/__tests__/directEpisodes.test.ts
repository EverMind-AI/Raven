// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { DirectTurn } from '../rpc/generated.js'

import { callSubject, foldDirectTurns } from '../domain/directEpisodes.js'
import { segmentTurn, toolParts, toolsSummary } from '../domain/episodeSummary.js'

const turn = (over: Partial<DirectTurn> & Pick<DirectTurn, 'role'>): DirectTurn => ({
  call_id: 'log-0',
  content: '',
  at_ms: 0,
  ...over
})

const call = (id: string, name: string, args: Record<string, unknown>) => ({
  id,
  name,
  arguments: JSON.stringify(args)
})

describe('callSubject', () => {
  it('reads the subject the runtime put under the tool own key', () => {
    expect(callSubject('{"command":"ls -la","cwd":"/repo"}')).toBe('ls -la')
    expect(callSubject('{"path":"/repo/a.ts","limit":20}')).toBe('/repo/a.ts')
  })

  it('falls back to insertion order for a tool it does not know', () => {
    expect(callSubject('{"filePath":"/w/note.txt"}')).toBe('/w/note.txt')
  })

  it('survives a payload that is not an arguments object', () => {
    expect(callSubject('ls -la')).toBe('ls -la')
    expect(callSubject('"ls -la"')).toBe('ls -la')
    expect(callSubject('{}')).toBe('')
    expect(callSubject('{"limit":20}')).toBe('')
  })
})

describe('foldDirectTurns', () => {
  it('says so when the trailing prompt was interrupted with its session', () => {
    const msgs = foldDirectTurns([turn({ content: 'dig in', interrupted: true, role: 'user' })])

    expect(msgs.map(m => m.role)).toEqual(['user', 'system'])
    expect(msgs[msgs.length - 1]!.text).toContain('interrupted')
  })

  it('adds no marker to a conversation whose turns all answered', () => {
    const msgs = foldDirectTurns([
      turn({ content: 'ask', role: 'user' }),
      turn({ content: 'answer', role: 'assistant' })
    ])

    expect(msgs.some(m => m.text.includes('interrupted'))).toBe(false)
  })

  it('carries a steer row into the turn instead of opening one', () => {
    const msgs = foldDirectTurns([
      turn({ call_id: 'log-0', content: 'do it', role: 'user' }),
      turn({ call_id: 'log-1', content: 'on it', role: 'assistant' }),
      turn({ at_ms: 7_000, call_id: 'log-2', content: 'the docs first', role: 'user', steer: true }),
      turn({ call_id: 'log-3', content: 'steered: the docs first', role: 'assistant' })
    ])

    expect(msgs.map(m => m.role)).toEqual(['user', 'assistant'])
    expect(msgs[1]!.episodes!.some(ep => ep.steer === 'the docs first' && ep.steerAtMs === 7_000)).toBe(true)
  })

  it('gives each turn one episodes message, so a run of calls can fold', () => {
    const msgs = foldDirectTurns([
      turn({ role: 'user', content: 'look around' }),
      turn({ role: 'assistant', tool_calls: [call('c1', 'read_file', { path: '/a.ts' })] }),
      turn({ role: 'tool', tool_call_id: 'c1', content: 'contents of a' }),
      turn({ role: 'assistant', tool_calls: [call('c2', 'read_file', { path: '/b.ts' })] }),
      turn({ role: 'tool', tool_call_id: 'c2', content: 'contents of b' }),
      turn({ role: 'assistant', content: 'two files, both fine.' })
    ])

    expect(msgs.map(m => m.role)).toEqual(['user', 'assistant'])
    expect(msgs[1]!.kind).toBe('episodes')
    expect(msgs[1]!.text).toBe('two files, both fine.')

    // One work segment across both episodes is what lets the summary collapse.
    const segments = segmentTurn(msgs[1]!.episodes ?? [])
    expect(segments).toHaveLength(1)
    expect(toolsSummary(segments[0]!.kind === 'work' ? segments[0]!.tools : [])).toBe('read 2 files')
  })

  it('names the call with the runtime verb and the argument beside it', () => {
    const msgs = foldDirectTurns([
      turn({ role: 'assistant', tool_calls: [call('c1', 'exec', { command: 'find . -name "*.ts"' })] }),
      turn({ role: 'tool', tool_call_id: 'c1', content: './a.ts\n./b.ts' })
    ])
    const tool = msgs[0]!.episodes![0]!.tools[0]!

    expect(toolParts(tool)).toEqual({ verb: 'ran', detail: 'find' })
    expect(tool.resultPreview).toBe('./a.ts\n./b.ts')
    expect(tool.ok).toBe(true)
  })

  it('reads a failed call from the prefix the runtime marks it with', () => {
    const msgs = foldDirectTurns([
      turn({ role: 'assistant', tool_calls: [call('c1', 'exec', { command: 'ls /nope' })] }),
      turn({ role: 'tool', tool_call_id: 'c1', content: '[failed] ls: /nope: No such file' })
    ])
    const tool = msgs[0]!.episodes![0]!.tools[0]!

    expect(tool.ok).toBe(false)
    expect(tool.resultPreview).toBe('ls: /nope: No such file')
  })

  it('times a call from the two clocks the record already carries', () => {
    const msgs = foldDirectTurns([
      turn({ role: 'assistant', at_ms: 1_000, tool_calls: [call('c1', 'exec', { command: 'sleep 3' })] }),
      turn({ role: 'tool', at_ms: 4_200, tool_call_id: 'c1', content: '' })
    ])

    expect(msgs[0]!.episodes![0]!.tools[0]!.durationMs).toBe(3_200)
  })

  it('puts narration on the step it preceded and the thought above it', () => {
    const msgs = foldDirectTurns([
      turn({
        role: 'assistant',
        content: 'let me check the config first.',
        reasoning_content: 'need to find the entry point',
        tool_calls: [call('c1', 'read_file', { path: '/tsconfig.json' })]
      }),
      turn({ role: 'tool', tool_call_id: 'c1', content: '{}' }),
      turn({ role: 'assistant', content: 'it is a plain ts project.' })
    ])
    const episode = msgs[0]!.episodes![0]!

    expect(episode.narration).toBe('let me check the config first.')
    expect(episode.reasoning).toBe('need to find the entry point')
    expect(msgs[0]!.text).toBe('it is a plain ts project.')

    // Narration closes the stretch of work before it, which is what makes the
    // transcript alternate talk and work instead of showing one wall of calls.
    expect(segmentTurn(msgs[0]!.episodes ?? []).map(s => s.kind)).toEqual(['talk', 'work'])
  })

  it('keeps a trailing thought that no call follows', () => {
    const msgs = foldDirectTurns([
      turn({ role: 'assistant', tool_calls: [call('c1', 'exec', { command: 'ls' })] }),
      turn({ role: 'tool', tool_call_id: 'c1', content: 'a b' }),
      turn({ role: 'assistant', reasoning_content: 'that is everything' }),
      turn({ role: 'assistant', content: 'done.' })
    ])

    expect(msgs[0]!.episodes!.map(e => e.reasoning)).toEqual(['', 'that is everything'])
    expect(msgs[0]!.text).toBe('done.')
  })

  it('starts a new message at each user turn', () => {
    const msgs = foldDirectTurns([
      turn({ role: 'user', content: 'first' }),
      turn({ role: 'assistant', content: 'one' }),
      turn({ role: 'user', content: 'second' }),
      turn({ role: 'assistant', content: 'two' })
    ])

    expect(msgs.map(m => [m.role, m.text])).toEqual([
      ['user', 'first'],
      ['assistant', 'one'],
      ['user', 'second'],
      ['assistant', 'two']
    ])
  })

  it('ignores a result whose call it never saw', () => {
    const msgs = foldDirectTurns([turn({ role: 'tool', tool_call_id: 'gone', content: 'orphan' })])

    expect(msgs).toEqual([])
  })

  it('joins several closing messages rather than keeping only the last', () => {
    const msgs = foldDirectTurns([
      turn({ role: 'assistant', content: 'part one' }),
      turn({ role: 'assistant', content: 'part two' })
    ])

    expect(msgs[0]!.text).toBe('part one\n\npart two')
  })

  it('folds a codex turn keeping codex names on every row', () => {
    const turns = [
      turn({ role: 'user', content: 'fix the bug', call_id: 'c1', at_ms: 1 }),
      turn({
        role: 'assistant',
        call_id: 'c1',
        at_ms: 2,
        tool_calls: [call('t1', 'commandExecution.read', { path: '/w/calc.py' })]
      }),
      turn({ role: 'tool', content: 'def add(a, b):', call_id: 'c1', tool_call_id: 't1', at_ms: 3 }),
      turn({
        role: 'assistant',
        call_id: 'c1',
        at_ms: 4,
        tool_calls: [call('t2', 'apply_patch', { path: 'calc.py' })]
      }),
      turn({
        role: 'tool',
        content: 'Success. Updated the following files:',
        call_id: 'c1',
        tool_call_id: 't2',
        at_ms: 5
      })
    ]

    const msgs = foldDirectTurns(turns)
    const episodes = msgs.find(m => m.kind === 'episodes')!.episodes!
    const names = episodes.flatMap(e => e.tools.map(t => t.name))

    expect(names).toEqual(['commandExecution.read', 'apply_patch'])
    expect(episodes[1]!.tools[0]!.summary).toBe('calc.py')
  })

  it('folds a stored apply_patch row to the filename, not the tool name', () => {
    // The shape the runtime now stores after `_backfill_subject` drops the
    // `command` that duplicated `apply_patch`'s own name: `path` first, `cwd`
    // trailing behind it.
    const msgs = foldDirectTurns([
      turn({ role: 'user', content: 'fix add()' }),
      turn({
        role: 'assistant',
        tool_calls: [call('t1', 'apply_patch', { path: 'calc.py', cwd: '/tmp/codexprobe/ws' })]
      }),
      turn({ role: 'tool', tool_call_id: 't1', content: 'Success. Updated the following files:' }),
      turn({ role: 'assistant', content: 'Fixed.' })
    ])

    const episodes = msgs.find(m => m.kind === 'episodes')!.episodes!
    expect(episodes[0]!.tools[0]!.summary).toBe('calc.py')
  })
})
