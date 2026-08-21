// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// A fold is the reader's decision; the row under it is a value the runtime
// replaces. These pin the two apart: a row that changes must not close what the
// reader opened.

import { render } from 'ink-testing-library'
import React from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import type { Episode, EpisodeTool, Msg } from '../types.js'

import { isFoldOpen, openFolds, resetFolds, toggleFold } from '../app/foldStore.js'
import { EpisodeMessage, EpisodeView } from '../components/episodeView.js'
import { foldDirectTurns } from '../domain/directEpisodes.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const tool = (id: string, summary: string, result: string): EpisodeTool => ({
  id,
  name: 'exec',
  summary,
  resultPreview: result,
  ok: true,
  done: true
})

const episodes = (tools: EpisodeTool[]): Episode[] => [{ index: 0, reasoning: '', tools }]

const frame = (eps: Episode[], scope: string) =>
  stripAnsi(render(<EpisodeView cols={92} episodes={eps} scope={scope} t={DEFAULT_THEME} />).lastFrame() ?? '')

beforeEach(() => {
  resetFolds()
})

describe('foldStore', () => {
  it('toggles one fold on and off', () => {
    expect(isFoldOpen('v', 'call:c1')).toBe(false)
    toggleFold('v', 'call:c1')
    expect(isFoldOpen('v', 'call:c1')).toBe(true)
    toggleFold('v', 'call:c1')
    expect(isFoldOpen('v', 'call:c1')).toBe(false)
  })

  it('keeps two views apart', () => {
    toggleFold('main', 'call:c1')

    expect(isFoldOpen('main', 'call:c1')).toBe(true)
    expect(isFoldOpen('4:Coder/h1', 'call:c1')).toBe(false)
    expect(openFolds('4:Coder/h1')).toEqual([])
  })
})

describe('EpisodeView folds', () => {
  it('renders a call detail only once its fold is open', () => {
    const eps = episodes([tool('c1', 'ls -la', 'total 42')])

    expect(frame(eps, 'v1')).not.toContain('total 42')
    toggleFold('v1', 'seg:c1')
    expect(frame(eps, 'v1')).toContain('total 42')
  })

  it('survives the row being replaced by a poll', () => {
    // The whole point. A step lands, so the runtime hands over a new episodes
    // value and the renderer remounts the row -- what the reader opened has to
    // still be open on the other side of that. A second call turns the stretch
    // into a list, so the same fold now shows the calls rather than one detail;
    // what must not happen is it snapping back to a single summary row.
    toggleFold('v1', 'seg:c1')
    const grown = episodes([tool('c1', 'ls -la', 'total 42'), tool('c2', 'pwd', '/repo')])
    const out = frame(grown, 'v1')

    expect(out).toContain('ran ls')
    expect(out).toContain('ran pwd')
  })

  it('keeps one call detail open while the turn grows around it', () => {
    const grown = episodes([tool('c1', 'ls -la', 'total 42'), tool('c2', 'pwd', '/repo')])
    toggleFold('v1', 'seg:c1')
    toggleFold('v1', 'call:c1')

    expect(frame(grown, 'v1')).toContain('total 42')

    // A third call lands. The detail the reader opened is still open.
    const grownMore = episodes([...grown[0]!.tools, tool('c3', 'find .', './a')]).map(e => e)

    expect(frame(grownMore, 'v1')).toContain('total 42')
  })

  it('does not leak a fold from one turn into another in the same view', () => {
    // `rsn:<n>` is an index that restarts at 0 in every message, so one scope per
    // *view* put every turn's first thought under one key: opening one turn's
    // reasoning opened all of them, in the main transcript as much as here.
    const withThought = (id: string, thought: string): Episode[] => [
      { index: 0, reasoning: thought, tools: [tool(id, 'ls', 'out')] }
    ]
    const turnA = withThought('c1', 'AAA-thought')
    const turnB = withThought('c2', 'BBB-thought')

    toggleFold('main:c1', 'rsn:0')

    expect(frame(turnA, 'main:c1')).toContain('AAA-thought')
    expect(frame(turnB, 'main:c2')).not.toContain('BBB-thought')
  })

  it('does not leak a fold into another instance view', () => {
    toggleFold('v1', 'seg:c1')
    const eps = episodes([tool('c1', 'ls -la', 'total 42')])

    expect(frame(eps, 'v1')).toContain('total 42')
    expect(frame(eps, 'v2')).not.toContain('total 42')
  })
})

describe('EpisodeMessage fold scope', () => {
  const msgOf = (callId: string, thought: string): Msg => ({
    kind: 'episodes',
    role: 'assistant',
    text: 'answer',
    episodes: [{ index: 0, reasoning: thought, tools: [tool(callId, 'ls -la', 'out')] }]
  })

  const messageFrame = (msg: Msg) =>
    stripAnsi(render(<EpisodeMessage cols={92} msg={msg} t={DEFAULT_THEME} />).lastFrame() ?? '')

  it('derives a different scope per message, so a turn fold is its own', () => {
    // The regression this pins: `rsn:<n>` restarts at 0 in every message, so a
    // scope per view made every turn's first thought one key. Opening one turn's
    // reasoning opened every other turn's -- in the main transcript too, since
    // `messageLine` renders this for every episodes row.
    const a = msgOf('c1', 'AAA-thought')
    const b = msgOf('c2', 'BBB-thought')

    expect(messageFrame(a)).not.toContain('AAA-thought')

    // The scope this component derives for `a`: the view, then a's first call.
    toggleFold('main:c1', 'rsn:0')

    expect(messageFrame(a)).toContain('AAA-thought')
    expect(messageFrame(b)).not.toContain('BBB-thought')
  })

  it('keeps a fold on a turn that called nothing, while its answer streams', () => {
    // The turn only thought and answered, so there is no tool call to key on and
    // its `text` grows on every poll -- which means `replaceRows` hands down a
    // fresh object each time. Keyed on the object, the reader's fold closed once
    // per poll; keyed on the row the message was folded from, it holds.
    const streaming = (text: string): Msg[] =>
      foldDirectTurns([
        { call_id: 'live-0', role: 'user', content: 'ask', at_ms: 0 },
        // A thought with no prose of its own is what makes a tool-less episode;
        // the answer arrives as its own row and grows as it streams.
        { call_id: 'live-1', role: 'assistant', content: '', reasoning_content: 'THE-THOUGHT', at_ms: 0 },
        { call_id: 'live-2', role: 'assistant', content: text, at_ms: 0 }
      ] as never)

    const first = streaming('partial')[1]!

    expect(messageFrame(first)).not.toContain('THE-THOUGHT')
    toggleFold(`main:${first.foldId}`, 'rsn:0')
    expect(messageFrame(first)).toContain('THE-THOUGHT')

    // The same turn, one poll later, with more of the answer in it.
    const later = streaming('partial answer, more of it')[1]!

    expect(later).not.toBe(first)
    expect(messageFrame(later)).toContain('THE-THOUGHT')
  })

  it('keeps a message scope while the runtime replaces the row under it', () => {
    // A step landing hands over a new object. The first call id does not move,
    // so what the reader opened is still open.
    toggleFold('main:c1', 'rsn:0')
    const grown: Msg = {
      kind: 'episodes',
      role: 'assistant',
      text: '',
      episodes: [
        { index: 0, reasoning: 'AAA-thought', tools: [tool('c1', 'ls -la', 'out'), tool('c9', 'pwd', '/repo')] }
      ]
    }

    expect(messageFrame(grown)).toContain('AAA-thought')
  })
})
