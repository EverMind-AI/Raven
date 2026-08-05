// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { render } from 'ink-testing-library'
import React from 'react'
import { describe, expect, it } from 'vitest'

import type { Episode } from '../types.js'

import { EpisodeView } from '../components/episodeView.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const frame = (node: React.ReactElement) => stripAnsi(render(node).lastFrame() ?? '')

const noNarrationStep: Episode = {
  index: 0,
  reasoning: 'SECRET internal plan',
  narration: '',
  reasoningMs: 8000,
  tools: [
    { id: 'a', name: 'read_file', summary: 'a.go', ok: true, done: true, resultPreview: 'package a' },
    { id: 'b', name: 'read_file', summary: 'b.go', ok: true, done: true }
  ]
}

const narratedStep: Episode = {
  index: 1,
  reasoning: 'THE chain of thought',
  narration: 'I will read the controller files',
  reasoningMs: 3000,
  tools: [{ id: 'c', name: 'read_file', summary: 'ctrl.go', ok: true, done: true, resultPreview: 'package ctrl' }]
}

describe('EpisodeView (flat, one-level)', () => {
  it('collapses a no-narration step to one summary line', () => {
    const f = frame(<EpisodeView episodes={[noNarrationStep]} t={DEFAULT_THEME} text="FINAL ANSWER" />)
    // The collapsed summary line: "reasoning for 8s · read 2 files"
    expect(f).toContain('reasoning for 8s')
    expect(f).toContain('read 2 files')
    // Final answer prose renders below.
    expect(f).toContain('FINAL ANSWER')
    // Collapsed => the reasoning text and tool results stay hidden.
    expect(f).not.toContain('SECRET internal plan')
    expect(f).not.toContain('package a')
  })

  it('expands a narrated step: reasoning fold before narration, narration prose, tools with inline result', () => {
    const f = frame(<EpisodeView episodes={[narratedStep]} t={DEFAULT_THEME} />)
    expect(f).toContain('reasoning for 3s') // reasoning fold line (collapsed content)
    expect(f).toContain('I will read the controller files') // narration prose
    expect(f).toContain('read') // tool verb
    expect(f).toContain('ctrl.go') // tool arg
    expect(f).toContain('package ctrl') // result shown inline
    // CoT stays folded until the reasoning row is opened.
    expect(f).not.toContain('THE chain of thought')
    // reasoning line comes before the narration line
    expect(f.indexOf('reasoning for 3s')).toBeLessThan(f.indexOf('I will read the controller files'))
  })

  it('renders only the final answer when there are no steps', () => {
    const f = frame(<EpisodeView episodes={[]} t={DEFAULT_THEME} text="just an answer" />)
    expect(f).toContain('just an answer')
    expect(f).not.toContain('reasoning')
  })

  it('live: the running (last) step is expanded and streams its text', () => {
    const f = frame(<EpisodeView episodes={[noNarrationStep]} live t={DEFAULT_THEME} text="LIVE OUTPUT" />)
    // Running step is expanded (not the collapsed one-liner), so its tool shows...
    expect(f).toContain('a.go')
    // ...and the in-progress text streams as prose.
    expect(f).toContain('LIVE OUTPUT')
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
            dir: '/w/.ravenx_dag/dag-1',
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
    const f = frame(<EpisodeView episodes={[narratedStep]} t={DEFAULT_THEME} text="done" />)

    expect(f).toContain('ctrl.go')
    expect(f).not.toContain('L1')
  })
})
