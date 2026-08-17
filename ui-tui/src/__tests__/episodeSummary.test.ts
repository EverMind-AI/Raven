// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { Episode, EpisodeTool } from '../types.js'

import {
  episodeFailed,
  execLabel,
  failureNote,
  foldedPreviewRows,
  previewLines,
  segmentTurn,
  TOOL_PREVIEW_ROWS,
  toolArgument,
  toolParts,
  toolsPhrase,
  toolsSummary,
  totalDurationMs
} from '../domain/episodeSummary.js'

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
    expect(toolsSummary([tool('exec', 'ls internal/biz/')])).toBe('ran ls')
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

  it('keeps a row label short: the full argument belongs to the detail block', () => {
    const cmd = 'curl -s "https://api.example.com/a?b=1" | python3 -c "import sys, json; print(1)"'
    const call = { ...tool('exec', cmd), durationMs: 1200 }

    // The row names the programs...
    expect(toolParts(call)).toEqual({ verb: 'ran', detail: 'curl -> python3' })
    // ...and the whole command survives, untouched, for the expanded block.
    expect(toolArgument(call)).toBe(cmd)
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

describe('result preview rows', () => {
  const withPreview = (resultPreview: string, name = 'read_file', summary = 'a.go'): EpisodeTool => ({
    ...tool(name, summary),
    resultPreview
  })

  it('drops blank rows, including a read_file line that is only its line number', () => {
    expect(previewLines(withPreview('1| # Title\n2|\n3| body\n\n4|   '))).toEqual(['1| # Title', '3| body'])
  })

  it('keeps a machine payload: the detail block is the one place raw output appears', () => {
    const url = 'https://example.com/a'
    const payload = `{"url":"${url}","finalUrl":"${url}","status":200}`

    // Filtering this as "redundant with the row" left an empty detail block --
    // the row carries a short label, so the payload is all there is to show.
    expect(previewLines(withPreview(payload, 'web_fetch', url))).toEqual([payload])
    expect(previewLines(withPreview('{\n  "a": 1\n}', 'read_file', 'x.json'))).toEqual(['{', '"a": 1', '}'])
  })

  it('counts every row when short, and cap + the "+N" row when long', () => {
    expect(foldedPreviewRows(withPreview('a\nb\nc'))).toBe(3)
    expect(foldedPreviewRows(tool('read_file', 'a.go'))).toBe(0)

    const long = Array.from({ length: TOOL_PREVIEW_ROWS + 8 }, (_, i) => `line ${i}`).join('\n')
    expect(foldedPreviewRows(withPreview(long))).toBe(TOOL_PREVIEW_ROWS + 1)
    // Dense (inside an expanded run) squeezes any result onto one row.
    expect(foldedPreviewRows(withPreview(long), true)).toBe(1)
  })
})

describe('folded group label', () => {
  it('phrases a finished group exactly like a folded step describes the same work', () => {
    const fetches = [tool('web_fetch', 'https://a.example'), tool('web_fetch', 'https://b.example')]

    expect(toolsPhrase(fetches)).toBe('fetched 2 urls')
    expect(toolsSummary(fetches)).toBe('fetched 2 urls')
  })

  it('sums durations, and reports none when nothing was timed', () => {
    expect(
      totalDurationMs([
        { ...tool('exec', 'a'), durationMs: 400 },
        { ...tool('exec', 'b'), durationMs: 600 }
      ])
    ).toBe(1000)
    expect(totalDurationMs([tool('exec', 'a')])).toBeUndefined()
  })
})

describe('execLabel', () => {
  it('names the programs, not the command', () => {
    expect(execLabel('curl -s "https://api.example.com/x" | python3 -c "print(1)"')).toBe('curl -> python3')
    expect(execLabel('pytest tests/test_cli_theme.py -x')).toBe('pytest')
  })

  it('keeps a subcommand, because it carries half the meaning', () => {
    expect(execLabel('ruff check raven/ ui-tui/')).toBe('ruff check')
    expect(execLabel('git status --short')).toBe('git status')
    // A flag is an argument, not a subcommand.
    expect(execLabel('curl -s https://example.com')).toBe('curl')
  })

  it('does not split on an operator inside quotes', () => {
    // The naive split tore this in half and put `[print(...)` on the row.
    expect(execLabel(`python3 -c "import sys; print(1)"`)).toBe('python3')
    // `-c` is a flag, so it stays an argument: the label is just the program.
    expect(execLabel(`sh -c 'a && b'`)).toBe('sh')
  })

  it('drops plumbing and repeated programs', () => {
    expect(execLabel('ls ~/.raven/ | head -20; find ~/.raven -maxdepth 1 -type d')).toBe('ls -> find')
    // `|| which hermes` is the same intent, not a second stage.
    expect(execLabel('which raven 2>/dev/null || which hermes 2>/dev/null')).toBe('which raven')
    // A bare `head file` is the work itself, so it survives as the first stage.
    expect(execLabel('head -20 notes.md')).toBe('head')
  })

  it('steps over env assignments and wrappers', () => {
    expect(execLabel('FOO=bar sudo /usr/local/bin/ruff check .')).toBe('ruff check')
  })

  it('elides the middle of a long chain rather than sprawling', () => {
    expect(execLabel('a | b | c | d')).toBe('a -> b -> …')
  })

  it('keeps a redirection whole, because its `&` is not a stage break', () => {
    // Split there, `1` stood where a program name goes: `pytest -> 1`.
    expect(execLabel('pytest -q 2>&1 | tail -20')).toBe('pytest')
    expect(execLabel('uv run pytest tests/ 2>&1')).toBe('uv run')
    expect(execLabel('npm run type-check > /tmp/tc.log 2>&1')).toBe('npm run')
    // And the phantom stage no longer eats the slot the second program needs.
    expect(execLabel('uv run pytest 2>&1 | grep FAIL')).toBe('uv run -> grep FAIL')
    expect(execLabel('make check &> /tmp/out.log')).toBe('make check')
  })

  it('never puts the command back on the row when no stage names a program', () => {
    // A flag where the program was expected dropped every stage, and the
    // fallback then printed the whole command -- a 44-character payload on a
    // row whose entire job is to be a title.
    expect(execLabel('sudo -u postgres psql -c "select 1"')).toBe('sudo')
    expect(execLabel('command -v raven')).toBe('command')
    expect(execLabel('env -u PYTHONPATH uv run pytest')).toBe('env')
    expect(execLabel('time -p make build')).toBe('time')
  })

  it('does not name a redirection target as the program', () => {
    // `\;` ends the stage, so the next one starts at the redirection, whose
    // basename strip read `2>/dev/null` as a program called `null`.
    expect(execLabel('find . -name "*.pyc" -exec rm {} \\; 2>/dev/null')).toBe('find')
  })

  it('spends its slots on the work, not on where the work happened', () => {
    expect(execLabel('cd /repo && git status')).toBe('git status')
    // A lone chdir is the whole command, so it still names itself.
    expect(execLabel('cd /repo')).toBe('cd')
  })
})

describe('failureNote', () => {
  it('names the one call that failed, so a folded row needs no click', () => {
    expect(failureNote([tool('exec', 'pytest x'), { ...tool('exec', 'ruff check raven/'), ok: false }])).toBe(
      'ruff check failed'
    )
  })

  it('counts instead of naming once more than one failed', () => {
    expect(
      failureNote([
        { ...tool('exec', 'a'), ok: false },
        { ...tool('exec', 'b'), ok: false }
      ])
    ).toBe('2 failed')
  })

  it('says nothing when everything worked', () => {
    expect(failureNote([tool('exec', 'a')])).toBe('')
  })
})

describe('segmentTurn', () => {
  const ep2 = (index: number, narration: string, tools: EpisodeTool[]): Episode => ({
    index,
    narration,
    reasoning: '',
    tools
  })
  const shape = (episodes: Episode[]) =>
    segmentTurn(episodes).map(seg => (seg.kind === 'talk' ? 'talk' : `work:${seg.tools.length}`))

  it('reads a turn as talk, work, talk, work', () => {
    expect(
      shape([
        ep2(0, 'here we go', [tool('exec', 'a')]),
        ep2(1, '', [tool('exec', 'b')]),
        ep2(2, '', [tool('exec', 'c')]),
        ep2(3, 'done', [tool('exec', 'd')])
      ])
    ).toEqual(['talk', 'work:3', 'talk', 'work:1'])
  })

  it('merges every call between two things the model said, across step boundaries', () => {
    const [, work] = segmentTurn([
      ep2(0, 'looking', [tool('read_file', 'a.go')]),
      ep2(1, '', [tool('read_file', 'b.go')]),
      ep2(2, '', [tool('exec', 'ls')])
    ])

    // Three steps, one stretch of work -- the reader never asked for steps.
    expect(work!.kind).toBe('work')
    expect(work!.kind === 'work' && work.tools.map(t => t.name)).toEqual(['read_file', 'read_file', 'exec'])
  })

  it('treats reasoning as the model speaking, so it closes the stretch before it', () => {
    const withCot: Episode = { ...ep2(1, '', [tool('exec', 'b')]), reasoning: 'a real chain of thought' }

    expect(shape([ep2(0, 'go', [tool('exec', 'a')]), withCot, ep2(2, '', [tool('exec', 'c')])])).toEqual([
      'talk',
      'work:1',
      'talk',
      'work:2'
    ])
  })

  it('emits nothing for a turn with no calls and no narration', () => {
    expect(shape([ep2(0, '', [])])).toEqual([])
  })
})

describe('episodeFailed', () => {
  it('aggregates across episodes and flags failures', () => {
    const episodes = [ep([tool('read_file', 'a.go'), tool('read_file', 'b.go')]), ep([tool('exec', 'ls', false)])]
    expect(episodeFailed(episodes[1]!)).toBe(true)
    expect(episodeFailed(episodes[0]!)).toBe(false)
  })
})
