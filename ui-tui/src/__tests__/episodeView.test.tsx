// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { render } from 'ink-testing-library'
import React from 'react'
import { describe, expect, it } from 'vitest'

import type { DirectTurn } from '../rpc/generated.js'
import type { Episode, EpisodeTool, Msg } from '../types.js'

import { $directChat, viewKeyOf } from '../app/directChatStore.js'
import { resetFolds, toggleFold } from '../app/foldStore.js'
import { EpisodeMessage, EpisodeView, turnFoldScope } from '../components/episodeView.js'
import { foldDirectTurns } from '../domain/directEpisodes.js'
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

describe('SteerRow', () => {
  it('draws a steer as one marked line inside the turn', () => {
    const out = frame(
      <EpisodeMessage
        cols={92}
        msg={{
          episodes: [
            { index: 0, narration: 'Looking around.', tools: [] },
            { index: 1, steer: 'the docs first', steerAtMs: Date.UTC(2026, 7, 26, 11, 28), tools: [] }
          ],
          foldId: 't1',
          kind: 'episodes',
          role: 'assistant',
          text: 'On it.'
        }}
        t={DEFAULT_THEME}
      />
    )

    expect(out).toContain('Looking around.')
    expect(out).toContain('\u21b3 steer')
    expect(out).toContain('the docs first')
    expect(out.indexOf('Looking around.')).toBeLessThan(out.indexOf('the docs first'))
    expect(out.indexOf('the docs first')).toBeLessThan(out.indexOf('On it.'))
  })
})

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

  it('keeps a fold opened while the turn ran open once the turn lands', () => {
    const cmd = 'ruff check raven/ ui-tui/ --output-format concise'
    const running = [step(0, 'linting', [call('a', 'exec', cmd, { done: false, durationMs: undefined })])]
    const landed = [step(0, 'linting', [call('a', 'exec', cmd, { resultPreview: 'All checks passed' })])]
    // What `streamingAssistant` names for the running turn, and what
    // `EpisodeMessage` recomputes for the row it settles into.
    const scope = turnFoldScope(viewKeyOf($directChat.get().active), 't7')

    // The reader's click, mid-run.
    toggleFold(scope, 'seg:a')

    const live = frame(<EpisodeView cols={92} episodes={running} live scope={scope} t={DEFAULT_THEME} />)
    const settled = frame(
      <EpisodeMessage
        cols={92}
        msg={{ episodes: landed, foldId: 't7', kind: 'episodes', role: 'assistant', text: '' }}
        t={DEFAULT_THEME}
      />
    )

    expect(live).toContain('--output-format')
    expect(settled).toContain('--output-format')

    resetFolds()
  })

  // The same invariant for the instance conversation view, whose rows come from
  // the other producer. Its `call_id` is a row ordinal the backend mints per
  // read -- `live-<n>` for a turn in flight, `log-<n>` once the record holds it
  // -- so a fold keyed on that string is dropped at exactly the settle it has to
  // survive. The turn's own call id is the same in both reads.
  it('keeps that fold open in the instance view, whose row ordinals change at the settle', () => {
    const cmd = 'ruff check raven/ ui-tui/ --output-format concise'
    const rows = (seed: string): DirectTurn[] => [
      { at_ms: 1, call_id: `${seed}-0`, content: 'lint it', role: 'user' },
      {
        at_ms: 2,
        call_id: `${seed}-1`,
        content: '',
        role: 'assistant',
        tool_calls: [{ arguments: JSON.stringify({ command: cmd }), id: 'call_abc123', name: 'exec' }]
      },
      {
        at_ms: 3,
        call_id: `${seed}-2`,
        content: 'All checks passed',
        role: 'tool',
        tool_call_id: 'call_abc123'
      }
    ]
    const turnOf = (seed: string) => foldDirectTurns(rows(seed)).find(m => m.kind === 'episodes')!
    const live = turnOf('live')
    const settled = turnOf('log')

    expect(live.foldId).toBe('call_abc123')
    expect(settled.foldId).toBe('call_abc123')

    const scope = turnFoldScope(viewKeyOf($directChat.get().active), 'call_abc123')

    toggleFold(scope, 'seg:call_abc123')

    const render = (msg: Msg) => frame(<EpisodeMessage cols={92} msg={msg} t={DEFAULT_THEME} />)

    expect(render(live)).toContain('--output-format')
    expect(render(settled)).toContain('--output-format')

    resetFolds()
  })

  it('keeps a running row on the same column as the reasoning row', () => {
    const episodes: Episode[] = [
      {
        index: 0,
        narration: '',
        reasoning: 'weighing whether to sleep first or echo first, and for how long',
        reasoningMs: 1000,
        tools: [
          call('s', 'exec', 'sleep 5 && echo done', {
            done: false,
            durationMs: undefined,
            startedAt: Date.now() - 5000
          })
        ]
      }
    ]

    const columnOf = (frame: string, needle: string) =>
      frame
        .split('\n')
        .find(l => l.includes(needle))!
        .indexOf(needle)

    const running = view(episodes, { live: true })
    const settled = view([step(0, '', [call('s', 'exec', 'sleep 5 && echo done', { durationMs: 5000 })])])

    // The spinner belongs in the marker margin, so the label starts where every
    // other row's text starts -- and does not jump left when the call lands.
    expect(columnOf(running, 'ran ')).toBe(columnOf(running, 'reasoning'))
    expect(columnOf(running, 'ran ')).toBe(columnOf(settled, 'ran '))
  })

  it('opens the call in hand while it is still running', () => {
    const cmd = 'pytest tests/integration/test_dag_smoke.py -x -q --maxfail=1 --timeout=600'
    const episodes = [
      step(0, 'checking', [
        call('a', 'read_file', 'conftest.py'),
        call('b', 'exec', cmd, { done: false, durationMs: undefined })
      ])
    ]

    // Closed, the row names the programs and keeps the command for the detail --
    // which is the whole reason a running call has to be openable.
    const closed = view(episodes, { live: true })
    expect(closed).toContain('ran pytest')
    expect(closed).not.toContain('--maxfail=1')

    const open = view(episodes, { live: true, openKeys: ['call:b'] })
    expect(open).toContain('--maxfail=1')
  })

  it('lists every call of a running stretch once the reader opens it', () => {
    const episodes = [
      step(0, 'fetching', [
        call('a', 'web_fetch', 'https://example.com/one'),
        call('b', 'web_fetch', 'https://example.com/two'),
        call('c', 'web_fetch', 'https://example.com/three', { done: false, durationMs: undefined })
      ])
    ]

    const open = view(episodes, { live: true, openKeys: ['seg:a'] })

    expect(open).toContain('example.com/one')
    expect(open).toContain('example.com/three')
  })

  it('opens a lone running call straight into its detail', () => {
    const cmd = 'ruff check raven/ ui-tui/ --output-format concise'
    const episodes = [step(0, 'linting', [call('r', 'exec', cmd, { done: false, durationMs: undefined })])]

    expect(view(episodes, { live: true })).not.toContain('--output-format')
    expect(view(episodes, { live: true, openKeys: ['seg:r'] })).toContain('--output-format')
  })

  it('keeps the argument in the block of a call that has no output yet', () => {
    // The row and the argument are the same text here, which is normally the
    // one case the block drops it -- but with no result to show in its place
    // that left the reader an empty slab.
    const episodes = [
      step(0, 'looking', [call('l', 'list_dir', '~/.raven/sessions', { done: false, durationMs: undefined })])
    ]
    const open = view(episodes, { live: true, openKeys: ['seg:l'] })

    expect(open.split('~/.raven/sessions')).toHaveLength(3)
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

const DAG_RUN = {
  done: true,
  nodes: [
    { dependsOn: [], id: 'a', promptTemplate: 'do a', status: 'completed' as const, subagent: 'echo' },
    { dependsOn: ['a'], id: 'b', promptTemplate: 'do b', status: 'completed' as const, subagent: 'echo' }
  ],
  runId: 'dag-1'
}

describe('a stretch holding a dag call', () => {
  it('opens itself, so the graph is drawn without a click', () => {
    const f = view([
      step(0, 'planning', [
        call('s1', 'read_skill', 'local/subagent-dag-orchestration'),
        call('s2', 'run_subagent_dag', '2 nodes: a, b', { dag: DAG_RUN })
      ])
    ])

    // The box corner: the graph rendered. Without the default this stretch
    // folds to one summary row and draws nothing.
    expect(f).toContain('\u256d')
    expect(f).toContain('run subagent dag')
  })

  it('draws the call once, in the panel that is its result', () => {
    // The row above the panel said `run subagent dag 3 nodes: a, b, c` and the
    // panel's header said the same thing again underneath it. The header won:
    // it is inside the frame, beside the tally the row could not carry.
    const f = view([step(0, 'planning', [call('s2', 'run_subagent_dag', '2 nodes: a, b', { dag: DAG_RUN })])])

    expect(f.split('run subagent dag').length - 1).toBe(1)
    expect(f).not.toContain('2 nodes: a, b')
  })

  it('keeps the graph when the reader folds it by hand', () => {
    // `openKeys` seeds the reader's decisions; a stretch that defaults open is
    // closed by naming it here, which is what the tri-state store records.
    const f = view(
      [
        step(0, 'planning', [
          call('s1', 'read_skill', 'local/subagent-dag-orchestration'),
          call('s2', 'run_subagent_dag', '2 nodes: a, b', { dag: DAG_RUN })
        ])
      ],
      { closedKeys: ['seg:s1'] }
    )

    expect(f).toContain('\u256d')
  })

  it('leaves a stretch without a dag call folded', () => {
    const f = view([step(0, 'looking', [call('t1', 'read_file', 'a.ts'), call('t2', 'read_file', 'b.ts')])])

    expect(f).not.toContain('\u256d')
  })
})

describe('a solo dag call', () => {
  const solo = (extra: Partial<EpisodeTool> = {}) =>
    call('s1', 'run_subagent_dag', '2 nodes: a, b', { dag: DAG_RUN, resultPreview: 'ran a then b', ...extra })

  it('draws the graph without auto-expanding the detail block', () => {
    const f = view([step(0, 'planning', [solo()])])

    // The folded row already names the call, so this stretch skips straight to
    // its Detail Block on open -- the graph must not drag that block open too,
    // or the result prints twice: once as a picture, once as raw text.
    expect(f).toContain('\u256d')
    expect(f).not.toContain('ran a then b')
  })

  it('still expands to the detail block when the reader opens it', () => {
    const f = view([step(0, 'planning', [solo()])], { openKeys: ['seg:s1'] })

    expect(f).toContain('ran a then b')
  })
})

describe('an open reasoning block', () => {
  const REASONING =
    'The user asks me to deeply think about a question: how to explain the Riemann ' +
    'Hypothesis to a person. This is a conceptual question, not a research task ' +
    'requiring web search. I should give a well-structured answer.'

  const thinkingTurn = () =>
    render(
      <EpisodeView
        cols={80}
        episodes={[{ index: 0, narration: '', reasoning: REASONING, tools: [], startedAt: 1 }]}
        live
        scope="s"
        t={DEFAULT_THEME}
        text=""
      />
    ).lastFrame() ?? ''

  it('carries its rule down every wrapped row', () => {
    const ruled = stripAnsi(thinkingTurn())
      .split('\n')
      .filter(line => line.includes('\u258f'))

    expect(ruled.length).toBeGreaterThan(2)
    expect(ruled.at(-1)).toContain('answer.')
  })

  it('puts its body on the prose column, with the rule out in the margin', () => {
    // The reasoning and the answer under it are the same column of text; only
    // the margin glyph differs. Indenting the block past the answer left two
    // ragged left edges, which is what made the aside look bolted on.
    const rows = stripAnsi(
      frame(
        <EpisodeView
          cols={80}
          episodes={[{ index: 0, narration: 'so here is the shape of it', reasoning: REASONING, tools: [] }]}
          openKeys={['rsn:0']}
          scope="s"
          t={DEFAULT_THEME}
        />
      )
    ).split('\n')

    const bodyColumn = (line: string) => line.length - line.trimStart().length

    for (const line of rows.filter(l => l.includes('\u258f'))) {
      expect(line.indexOf('\u258f')).toBe(0)
      expect(bodyColumn(line.slice(1))).toBe(1)
    }

    const answer = rows.find(l => l.includes('so here is the shape of it'))!

    expect(answer.indexOf('so here')).toBe(2)
  })

  it('is an aside, not a tool payload, so it has no filled ground', () => {
    // `detailBg` means "tool output" everywhere else in the transcript. Prose on
    // that ground outweighs the answer it was only leading up to, so this block
    // earns its separation from the rule alone.
    expect(thinkingTurn()).not.toContain('\u001b[48;')
  })
})

describe('the reasoning row while the model is still reasoning', () => {
  // The margin column: the spinner here, the rule under it, the reply marker
  // on the answer. Text starts after it, on one shared column.
  const MARGIN = 2

  const REASONING =
    'The user asks how to explain the Riemann Hypothesis to a person. This is a ' +
    'conceptual question, not a research task. Let me structure a layered answer.'

  const turn = (extra: Record<string, unknown>) =>
    stripAnsi(
      frame(
        <EpisodeView
          cols={80}
          episodes={[{ index: 0, narration: '', reasoning: REASONING, reasoningMs: 9000, tools: [], startedAt: 1 }]}
          live
          t={DEFAULT_THEME}
          text=""
          {...extra}
        />
      )
    ).split('\n')

  it('spends the margin column on the spinner, so the label keeps the prose column', () => {
    const row = turn({ scope: 'spin' }).find(line => line.includes('reasoning ('))!

    expect(row.indexOf('reasoning')).toBe(MARGIN)
    expect(row[0]).not.toBe(' ')
  })

  it('scrolls a live tail of the reasoning when the block is closed', () => {
    const row = turn({ closedKeys: ['rsn:0'], scope: 'closed' }).find(line => line.includes('reasoning ('))!

    // Closed, the row is the only view of the reasoning there is: a label and a
    // ticking clock say nothing about whether the model is getting anywhere.
    expect(row).toContain('structure a layered answer.')
    expect(row).toContain('\u2026')
  })

  it('drops the tail once the block is open, which already shows all of it', () => {
    const rows = turn({ scope: 'open' })
    const row = rows.find(line => line.includes('reasoning ('))!

    // The spinner still turns in the margin; what the open row drops is the
    // tail, because the block under it is already showing the whole thing.
    expect(row.slice(MARGIN).trimEnd()).toMatch(/^reasoning \([^)]+\)$/)
    expect(rows.some(line => line.includes('\u258f'))).toBe(true)
  })
})
