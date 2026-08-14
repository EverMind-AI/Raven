// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { render } from 'ink-testing-library'
import React from 'react'
import { describe, expect, it } from 'vitest'

import type { Episode, EpisodeTool } from '../types.js'

import { EpisodeView } from '../components/episodeView.js'
import { TOOL_PREVIEW_ROWS } from '../domain/episodeSummary.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const frame = (node: React.ReactElement) => stripAnsi(render(node).lastFrame() ?? '')
const view = (episodes: Episode[], extra: Record<string, unknown> = {}) =>
  frame(<EpisodeView cols={92} episodes={episodes} t={DEFAULT_THEME} {...extra} />)

const call = (id: string, name: string, summary: string, extra: Partial<EpisodeTool> = {}): EpisodeTool => ({
  id,
  name,
  summary,
  ok: true,
  done: true,
  durationMs: 400,
  ...extra
})

const step = (index: number, narration: string, tools: EpisodeTool[]): Episode => ({
  index,
  narration,
  reasoning: '',
  tools
})

// The glyphs the redesign removed. A row of triangles down the left margin is
// what made the transcript read as a control panel instead of a document, so
// their absence is a contract, not an accident.
const RETIRED = ['▸', '▾', '├', '└', '│', '·', '✗']

describe('EpisodeView', () => {
  it('renders a turn with no fold glyphs at all', () => {
    const f = view([
      step(0, 'checking the local install', [call('a', 'list_dir', '/Users/admin/.raven')]),
      step(1, '', [call('b', 'exec', 'which raven 2>/dev/null || which hermes')]),
      step(2, 'confirmed, it is standalone', [])
    ])

    for (const glyph of RETIRED) {
      expect(f).not.toContain(glyph)
    }

    expect(f).toContain('checking the local install')
    expect(f).toContain('confirmed, it is standalone')
  })

  it('folds every call between two things the model said into one row', () => {
    const f = view([
      step(0, 'looking around', [call('a', 'list_dir', '/Users/admin/.raven')]),
      step(1, '', [call('b', 'read_file', 'TOOLS.md')]),
      step(2, '', [call('c', 'exec', 'which raven'), call('d', 'exec', 'pip show raven-agent')])
    ])

    // Four calls over three steps, one row.
    expect(f).toContain('listed .raven, read TOOLS.md, ran 2 commands')
    expect(f).not.toContain('which raven')
    expect(f.split('\n').filter(l => l.trim()).length).toBe(2)
  })

  it('names a shell command by its programs, keeping the command for the detail', () => {
    const cmd = 'curl -s "https://api.example.com/x?a=1" | python3 -c "import sys; print(1)"'
    const episodes = [step(0, 'fetching', [call('g', 'exec', cmd, { resultPreview: 'ok' })])]

    const folded = view(episodes)
    expect(folded).toContain('ran curl -> python3')
    expect(folded).not.toContain('api.example.com')

    // One call, so opening skips straight to the detail -- no identical row in
    // between, which is what printed the same sentence twice before.
    const open = view(episodes, { openKeys: ['seg:g'] })
    expect(open).toContain('api.example.com')
    expect(open.match(/ran curl -> python3/g)).toHaveLength(1)
  })

  it('opens a stretch into one row per call, then a call into its detail', () => {
    const episodes = [
      step(0, 'looking', [
        call('a', 'exec', 'ruff check raven/', { resultPreview: 'All checks passed!' }),
        call('b', 'exec', 'git status --short')
      ])
    ]

    const level2 = view(episodes, { openKeys: ['seg:a'] })
    expect(level2).toContain('ran ruff check')
    expect(level2).toContain('ran git status')
    expect(level2).not.toContain('All checks passed!')

    const level3 = view(episodes, { openKeys: ['seg:a', 'call:a'] })
    expect(level3).toContain('ruff check raven/')
    expect(level3).toContain('All checks passed!')
  })

  it('names the failing call in the folded row, without expanding anything', () => {
    const f = view([
      step(0, 'linting', [
        call('a', 'exec', 'pytest tests/'),
        call('b', 'exec', 'ruff check raven/', { ok: false, resultPreview: 'Found 1 error.' }),
        call('c', 'exec', 'git status')
      ])
    ])

    expect(f).toContain('ruff check failed')
    // Still folded: neither the sibling calls nor the error body are showing.
    expect(f).not.toContain('ran pytest')
    expect(f).not.toContain('Found 1 error.')
  })

  it('treats an "Error:" result as a failure, and omits an argument the row already shows', () => {
    const needle = 'Hermes Agent latest release'
    const err = 'Error: Serper API key not configured. Set it in ~/.raven/config.json.'
    const episodes = [
      step(0, 'searching', [
        call('q1', 'web_search', needle, { resultPreview: err }),
        call('q2', 'web_search', 'another query', { resultPreview: err })
      ])
    ]

    // The backend reports this kind of failure as text, not as a failed call.
    expect(view(episodes)).toContain('2 failed')

    const open = view(episodes, { openKeys: ['seg:q1', 'call:q1'] })
    // The needle fits on the row whole, so the block carries only the error --
    // repeating the query one line below it was pure duplication.
    expect(open).toContain(`searched "${needle}"`)
    expect(open).toContain('Serper API key')
    expect(open.match(new RegExp(needle, 'g'))).toHaveLength(1)
  })

  it('wraps the whole result inside the detail block instead of truncating it', () => {
    const url = 'https://github.com/NousResearch/hermes-agent'
    const payload = `{"url": "${url}", "finalUrl": "${url}", "status": 200, "bytes": 48213}`
    const f = view([step(0, 'fetching', [call('f', 'web_fetch', url, { resultPreview: payload })])], {
      openKeys: ['seg:f']
    })

    // Every character survives -- the backend already capped this once, and a
    // second truncation at the column left nothing readable.
    expect(f.replace(/\s+/g, ' ')).toContain('"bytes": 48213}')
    // ...across several rows, none of which overflows the block.
    const body = f.split('\n').filter(l => l.includes('"'))
    expect(body.length).toBeGreaterThan(1)
    expect(Math.max(...body.map(l => l.length))).toBeLessThanOrEqual(92)
  })

  it('omits a duration that would floor to "0s"', () => {
    const fast = view([step(0, 'quick', [call('a', 'exec', 'git status', { durationMs: 120 })])])
    const slow = view([step(0, 'slow', [call('b', 'exec', 'pytest tests/', { durationMs: 3100 })])])

    expect(fast).not.toContain('(0s)')
    expect(slow).toContain('(3s)')
  })

  it('while running a lone call, prints it once -- not as its own summary too', () => {
    const query = 'Hermes Agent open source AI framework latest updates August 2026'
    const f = view(
      [
        step(0, 'checking upstream', [
          call('r', 'deep_research', query, { done: false, durationMs: undefined, startedAt: Date.now() - 15000 })
        ])
      ],
      { live: true }
    )

    // One call means the summary IS the call: two rows here printed the same
    // sentence twice, under two spinners.
    const rows = f.split('\n').filter(l => l.includes('researched'))
    expect(rows).toHaveLength(1)
  })

  it('streams the answer of a running episode that has not said anything yet', () => {
    // The closing answer arrives through `text` while `narration` is still
    // empty -- nothing flushes it into narration until the message ends -- so a
    // gate that waits for a talk segment leaves the whole stream invisible.
    const answering = view([step(0, '', [])], { live: true, text: 'LIVE OUTPUT' })
    expect(answering).toContain('LIVE OUTPUT')

    const afterWork = view([step(0, '', [call('a', 'read_file', 'a.go')])], { live: true, text: 'LIVE OUTPUT' })
    expect(afterWork).toContain('LIVE OUTPUT')
    expect(afterWork).toContain('read a.go')

    // Reasoning gives the episode a talk segment of its own, which is where the
    // stream belongs -- printing it twice is the failure on the other side.
    const thinking = frame(
      <EpisodeView
        cols={92}
        episodes={[{ index: 0, narration: '', reasoning: 'thinking hard', tools: [] }]}
        live
        t={DEFAULT_THEME}
        text="LIVE OUTPUT"
      />
    )
    expect(thinking.split('LIVE OUTPUT')).toHaveLength(2)
  })

  it('while running, shows the summary so far plus only the call in hand', () => {
    const f = view(
      [
        step(0, 'fetching', [
          call('a', 'web_fetch', 'https://example.com/one'),
          call('b', 'web_fetch', 'https://example.com/two'),
          call('c', 'web_fetch', 'https://example.com/three', { done: false, durationMs: undefined })
        ])
      ],
      { live: true }
    )

    expect(f).toContain('fetched 3 urls')
    expect(f).toContain('example.com/three')
    expect(f).not.toContain('example.com/one')
  })

  it('caps a long result inside the detail block', () => {
    const long = Array.from({ length: 13 }, (_, i) => `entry-${i}`).join('\n')
    const f = view([step(0, 'listing', [call('l', 'list_dir', '~/.raven', { resultPreview: long })])], {
      openKeys: ['seg:l']
    })

    expect(f).toContain('entry-0')
    expect(f).not.toContain(`entry-${TOOL_PREVIEW_ROWS}`)
    expect(f).toContain(`+${13 - TOOL_PREVIEW_ROWS}`)
  })

  it('renders only the final answer when there are no steps', () => {
    expect(view([], { text: 'just an answer' })).toContain('just an answer')
  })

  it('keeps reasoning behind its own row and never leaks the text unopened', () => {
    const episodes: Episode[] = [
      { index: 0, narration: 'on it', reasoning: 'SECRET internal plan', reasoningMs: 8000, tools: [] }
    ]

    const folded = view(episodes)
    expect(folded).toContain('reasoning (8s)')
    expect(folded).not.toContain('SECRET internal plan')

    expect(view(episodes, { openKeys: ['rsn:0'] })).toContain('SECRET internal plan')
  })

  it('draws a run_subagent_dag call graph under its tool row', () => {
    // The DAG tool's result text is clamped to 200 chars, so a fan-out of any
    // size leaves the transcript with no record of which node did what unless
    // the pinned graph renders here.
    const dagStep: Episode = {
      index: 0,
      narration: 'fanning out',
      tools: [
        {
          id: 'call-a',
          name: 'run_subagent_dag',
          summary: '3 nodes: fetch, parse, report',
          ok: true,
          done: true,
          resultPreview: 'DAG dag-1: 2/3 completed, 1 failed (parse)',
          dag: {
            runId: 'dag-1',
            done: true,
            dir: '/w/mas_dag/dag-1',
            summary: { total: 3, completed: 2, failed: 1, skipped: 0 },
            nodes: [
              { id: 'fetch', subagent: 'echo', dependsOn: [], status: 'completed' },
              { id: 'parse', subagent: 'echo', dependsOn: ['fetch'], status: 'failed', error: 'exited 1' },
              { id: 'report', subagent: 'echo', dependsOn: ['fetch'], status: 'completed' }
            ]
          }
        }
      ]
    }

    const f = frame(<EpisodeView episodes={[dagStep]} t={DEFAULT_THEME} text="done" />)

    expect(f).toContain('fetch')
    expect(f).toContain('parse')
    expect(f).toContain('report')
    expect(f).toContain('exited 1')
  })

  it('leaves a tool with no graph unchanged', () => {
    const f = view([step(0, 'reading the entry point', [call('a', 'read_file', 'ctrl.go')])])

    expect(f).toContain('ctrl.go')
    // A node id from the graph above: no call without a `dag` may draw one.
    expect(f).not.toContain('fetch')
  })
})
