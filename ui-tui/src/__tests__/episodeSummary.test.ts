// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { Episode, EpisodeTool } from '../types.js'

import { episodeFailed, toolParts, toolsSummary } from '../domain/episodeSummary.js'

const tool = (name: string, summary: string, ok = true): EpisodeTool => ({
  id: `${name}:${summary}`,
  name,
  summary,
  ok
})
const ep = (tools: EpisodeTool[], reasoning = '', narration = ''): Episode => ({
  index: 0,
  reasoning,
  narration,
  tools
})

describe('toolsSummary', () => {
  it('counts a run of the same info tool', () => {
    expect(toolsSummary([tool('read_file', 'a.go'), tool('read_file', 'b.go'), tool('read_file', 'c.go')])).toBe(
      'read 3 files'
    )
  })

  it('shows the target for a single call', () => {
    expect(toolsSummary([tool('read_file', 'src/device/approve.go')])).toBe('read approve.go')
    expect(toolsSummary([tool('exec', 'ls internal/biz/')])).toBe('ran ls internal/biz/')
    expect(toolsSummary([tool('grep', 'DeviceFlow')])).toBe('searched "DeviceFlow"')
  })

  it('joins distinct tools in order', () => {
    expect(toolsSummary([tool('read_file', 'a.go'), tool('read_file', 'b.go'), tool('exec', 'ls')])).toBe(
      'read 2 files, ran ls'
    )
  })

  it('uses target-style plural for a run of action tools', () => {
    expect(toolsSummary([tool('exec', 'ls'), tool('exec', 'cat x')])).toBe('ran 2 commands')
  })

  it('derives a humanized verb for tools with no override (never a wrong "ran")', () => {
    // A tool we never listed still gets its own name as the verb, not "ran".
    expect(toolsSummary([tool('quantum_leap', ''), tool('quantum_leap', '')])).toBe('quantum leap 2 calls')
    // Real backend tools that predate this table render sensibly with no edits:
    expect(toolsSummary([tool('image_generate', 'a red fox')])).toBe('image generate a red fox')
    expect(toolsSummary([tool('web_search', 'hermes agent')])).toBe('searched "hermes agent"')
  })

  it('shows +added -removed for an edited file', () => {
    const edited = { ...tool('edit_file', 'notes.md'), added: 12, removed: 3 }
    expect(toolsSummary([edited])).toBe('edited notes.md (+12 -3)')
    // A missing side reads as zero, and no stats at all means no suffix.
    expect(toolsSummary([{ ...tool('edit_file', 'notes.md'), added: 5 }])).toBe('edited notes.md (+5 -0)')
    expect(toolsSummary([tool('edit_file', 'notes.md')])).toBe('edited notes.md')
  })

  it('splits a tool row into verb + detail (full path, not just basename)', () => {
    expect(toolParts(tool('read_file', 'src/approve.go'))).toEqual({ verb: 'read', detail: 'src/approve.go' })
    expect(toolParts(tool('exec', 'ls biz/'))).toEqual({ verb: 'ran', detail: 'ls biz/' })
    expect(toolParts({ ...tool('edit_file', 'notes.md'), added: 12, removed: 3 })).toEqual({
      verb: 'edited',
      detail: 'notes.md +12 -3'
    })
  })

  it('left-clips a long path to keep the tail', () => {
    const long = `/tmp/everme/server/internal/controller/${'nested/'.repeat(12)}memory/agent_memory.go`
    const { detail } = toolParts(tool('read_file', long))
    expect(detail.startsWith('…')).toBe(true)
    expect(detail.endsWith('agent_memory.go')).toBe(true)
  })

  it('summarizes delegated subagents by count', () => {
    expect(toolsSummary([tool('spawn', 'a'), tool('spawn', 'b'), tool('spawn', 'c'), tool('spawn', 'd')])).toBe(
      'delegated 4 subagents'
    )
  })
})

describe('episodeFailed', () => {
  it('aggregates across episodes and flags failures', () => {
    const episodes = [ep([tool('read_file', 'a.go'), tool('read_file', 'b.go')]), ep([tool('exec', 'ls', false)])]
    expect(episodeFailed(episodes[1]!)).toBe(true)
    expect(episodeFailed(episodes[0]!)).toBe(false)
  })
})
