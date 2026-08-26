import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it } from 'vitest'

import type { DagRunNode, DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { EpisodeTool, Msg } from '../types.js'

import { MessageLine } from '../components/messageLine.js'
import { DAG_TRACE_BOX_ROWS } from '../config/limits.js'
import { layoutDagGraph } from '../lib/dagGraphLayout.js'
import { dagNodeKey } from '../lib/dagOpenNodes.js'
import { estimatedMsgHeight, messageHeightKey, wrappedLines } from '../lib/virtualHeights.js'
import { DEFAULT_THEME } from '../theme.js'
import { TerminalScreen } from './support/terminalScreen.js'

const node = (id: string, status: DagRunNodeStatus = 'pending') => ({ dependsOn: [], id, status, subagent: 'echo' })

const dagRun = (nodeCount: number, extra: Partial<DagRunState> = {}): DagRunState => ({
  done: false,
  nodes: Array.from({ length: nodeCount }, (_, i) => node(`n${i}`)),
  runId: 'dag-1',
  ...extra
})

const withDagTool = (dag: DagRunState | undefined, ...extraTools: EpisodeTool[]): Msg => ({
  episodes: [
    { index: 0, tools: [{ dag, id: 't0', name: 'run_subagent_dag', ok: true, summary: 'dag' }, ...extraTools] }
  ],
  kind: 'episodes',
  role: 'assistant',
  text: ''
})

const runWith = (nodes: DagRunNode[], extra: Partial<DagRunState> = {}): DagRunState => ({
  done: false,
  nodes,
  runId: 'r1',
  ...extra
})

const msgWithDag = withDagTool

const BASE = { compact: false, details: false }

describe('virtual height estimates', () => {
  it('uses stable content keys across resumed message objects', () => {
    const msg: Msg = { role: 'assistant', text: 'same text', tools: ['Search Files [long message]'] }

    expect(messageHeightKey(msg)).toBe(messageHeightKey({ ...msg }))
  })

  it('accounts for wrapping and preserved blank-block rhythm', () => {
    const msg: Msg = { role: 'assistant', text: `one\n\n${'x'.repeat(90)}` }

    expect(wrappedLines(msg.text, 30)).toBe(5)
    expect(estimatedMsgHeight(msg, 35, { compact: false, details: false })).toBeGreaterThan(5)
  })

  it('wraps by terminal cell width instead of UTF-16 code units', () => {
    expect(wrappedLines('你好世界', 4)).toBe(2)
    expect(wrappedLines('e\u0301e\u0301', 1)).toBe(2)
    expect(wrappedLines('🙂🙂', 2)).toBe(2)
  })

  it('uses compound user prompt width when estimating user message wrapping', () => {
    const msg: Msg = { role: 'user', text: 'x'.repeat(21) }

    expect(estimatedMsgHeight(msg, 26, { compact: false, details: false, userPrompt: '❯' })).toBe(5)
    expect(estimatedMsgHeight(msg, 26, { compact: false, details: false, userPrompt: 'Ψ >' })).toBe(6)
  })

  it('reserves the prompt block padding a user message renders with', () => {
    const user: Msg = { role: 'user', text: 'one line' }
    const assistant: Msg = { role: 'assistant', text: 'one line' }
    const opts = { compact: true, details: false }

    // One row of text, a blank margin either side, and the block's own two
    // padding rows -- messageLine.tsx draws the padding whatever the tier.
    expect(estimatedMsgHeight(user, 80, opts)).toBe(estimatedMsgHeight(assistant, 80, opts) + 4)
  })

  it('includes detail sections when visible', () => {
    const msg: Msg = { role: 'assistant', text: 'ok', thinking: 'line 1\nline 2', tools: ['Tool A', 'Tool B'] }

    expect(estimatedMsgHeight(msg, 80, { compact: false, details: true })).toBeGreaterThan(
      estimatedMsgHeight(msg, 80, { compact: false, details: false })
    )
  })

  it('reserves two extra rows for the inter-turn separator on non-first user messages', () => {
    const msg: Msg = { role: 'user', text: 'follow-up question' }
    const base = estimatedMsgHeight(msg, 80, { compact: false, details: false })
    const withSep = estimatedMsgHeight(msg, 80, { compact: false, details: false, withSeparator: true })

    expect(withSep).toBe(base + 2)
  })

  it('keeps the one-row estimate for a work segment with no DAG call', () => {
    const msg = withDagTool(undefined, { id: 't1', name: 'ls', ok: true, summary: 'ls' })

    expect(estimatedMsgHeight(msg, 80, { compact: false, details: false })).toBe(1)
  })

  it('accounts for a DAG panel drawn under a solo-call work segment', () => {
    const dag = dagRun(2, { dir: '/tmp/out', done: true })
    const msg = withDagTool(dag)

    const dagWidth = Math.max(20, 80 - 4)
    const picture = layoutDagGraph(dag.nodes, { width: Math.max(24, Math.max(28, dagWidth - 2) - 4) })
    // No call row: a dag call's panel is the box that carries the call. Then
    // DagPanel's own chrome -- two border rows, the header, two rules and the
    // hint -- around the picture, a row per node, and the outputs line.
    const rows = dag.nodes.length
    const expected = 6 + (picture?.height ?? 0) + rows + 1

    expect(estimatedMsgHeight(msg, 80, { compact: false, details: false })).toBe(expected)
  })

  it('accounts for a DAG panel drawn under a multi-call work segment', () => {
    const dag = dagRun(4)
    const msg = withDagTool(dag, { id: 't1', name: 'ls', ok: true, summary: 'ls' })

    const dagWidth = Math.max(20, 80 - 4)
    const picture = layoutDagGraph(dag.nodes, { width: Math.max(24, Math.max(28, dagWidth - 4) - 4) })
    // Summary row + a row for the one call that draws one (the dag call does
    // not), plus the panel.
    const rows = dag.nodes.length
    const expected = 1 + 1 + (6 + (picture?.height ?? 0) + rows)

    expect(estimatedMsgHeight(msg, 80, { compact: false, details: false })).toBe(expected)
  })

  it('invalidates the cache key when a DAG panel grows or completes', () => {
    const noDag = messageHeightKey(withDagTool(undefined))
    const twoNodes = messageHeightKey(withDagTool(dagRun(2)))
    const fourNodes = messageHeightKey(withDagTool(dagRun(4)))
    const finished = messageHeightKey(withDagTool(dagRun(4, { dir: '/tmp/out', done: true })))

    expect(twoNodes).not.toBe(noDag)
    expect(fourNodes).not.toBe(twoNodes)
    expect(finished).not.toBe(fourNodes)
  })
})

describe('dag panel height', () => {
  // The slots live under the node rows, so they exist only in the layout that
  // draws rows. At 32 columns this two-node chain lays out compact -- boxes of
  // bare ordinals, naming nothing -- which is the case where the panel still
  // prints the rows, and the slots with them.
  const NARROW_COLS = 32
  const chain = (head: DagRunNodeStatus) =>
    runWith([
      { dependsOn: [], id: 'a', status: head, subagent: 'echo' },
      { dependsOn: ['a'], id: 'b', status: 'pending', subagent: 'echo' }
    ])

  it('costs a running node no more than a finished one, having no slot of its own', () => {
    // The live line under a running row is gone; only an opened node has a slot.
    const idle = msgWithDag(chain('completed'))
    const busy = msgWithDag(chain('running'))

    expect(estimatedMsgHeight(busy, NARROW_COLS, BASE)).toBe(estimatedMsgHeight(idle, NARROW_COLS, BASE))
  })

  it('counts the box an expanded node opens', () => {
    const msg = msgWithDag(chain('running'))
    const open = new Set([dagNodeKey('r1', 'a')])

    expect(estimatedMsgHeight(msg, NARROW_COLS, { ...BASE, dagOpen: open })).toBe(
      estimatedMsgHeight(msg, NARROW_COLS, BASE) + DAG_TRACE_BOX_ROWS
    )
  })

  it('counts the slots under a labelled picture too, because the rows are drawn there', () => {
    // The rows were dropped under a labelled picture for a while, and the slots
    // went with them. They are back -- a box holds neither the node's name nor
    // what it cost -- so an open node costs its box exactly as under a compact
    // picture.
    const busy = msgWithDag(chain('running'))
    const open = new Set([dagNodeKey('r1', 'a')])

    expect(estimatedMsgHeight(busy, 100, { ...BASE, dagOpen: open })).toBe(
      estimatedMsgHeight(busy, 100, BASE) + DAG_TRACE_BOX_ROWS
    )
  })

  it('is unchanged for a run with nothing running and nothing open', () => {
    const msg = msgWithDag(runWith([node('a', 'completed')]))

    expect(estimatedMsgHeight(msg, 100, { ...BASE, dagOpen: new Set() })).toBe(
      estimatedMsgHeight(msg, 100, BASE)
    )
  })

  it('re-keys when a node starts running', () => {
    const idle = msgWithDag(runWith([node('a', 'pending')]))
    const busy = msgWithDag(runWith([node('a', 'running')]))

    expect(messageHeightKey(busy)).not.toBe(messageHeightKey(idle))
  })

  it('counts a box for a pending node with no template, which is now openable', () => {
    // `dagNodeToggleKey` used to answer `null` without a template, which left a
    // node's own id unreachable. It answers a key for every node now, so the
    // estimate has to reserve the box a click will open.
    const msg = msgWithDag(runWith([node('a', 'pending')]))
    const open = new Set([dagNodeKey('r1', 'a')])

    expect(estimatedMsgHeight(msg, 100, { ...BASE, dagOpen: open })).toBe(
      estimatedMsgHeight(msg, 100, BASE) + DAG_TRACE_BOX_ROWS
    )
  })
})

// The estimate feeds the virtual window's row reservation, so it may sit above
// the real height but never below it: too few rows reserved is what leaves the
// previous message's cells on screen under the next one.
//
// Counted off a screen model rather than the raw stream: the renderer's cursor
// moves make a stream line and a screen row different things, and only the row
// count is what the reservation has to cover.
const renderedRows = async (msg: Msg, cols: number) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  const screen = new TerminalScreen(cols, 120)

  Object.assign(stdout, { columns: cols, isTTY: true, rows: 120 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', chunk => {
    screen.write(chunk.toString())
  })

  const instance = renderSync(React.createElement(MessageLine, { cols, msg, t: DEFAULT_THEME }), {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  await new Promise(resolve => setTimeout(resolve, 40))
  const rows = screen.text().split('\n')
  let last = -1

  rows.forEach((row, index) => {
    if (row.trim()) {
      last = index
    }
  })

  instance.unmount()
  instance.cleanup()

  return last + 1
}

describe('estimatedMsgHeight covers quoted prose', () => {
  const estimate = (msg: Msg, cols: number) =>
    estimatedMsgHeight(msg, cols, { compact: false, details: false, userPrompt: '>' })

  it('reserves enough rows for a quote that wraps', async () => {
    const msg: Msg = { role: 'assistant', text: `> ${'word '.repeat(60)}` }

    expect(estimate(msg, 80)).toBeGreaterThanOrEqual(await renderedRows(msg, 80))
  })

  it('reserves enough rows for a nested quote, whose rule indents twice', async () => {
    const msg: Msg = { role: 'assistant', text: `>>> ${'word '.repeat(60)}` }

    expect(estimate(msg, 80)).toBeGreaterThanOrEqual(await renderedRows(msg, 80))
  })

  it('counts the rule, not the markers: a quote costs more rows than the same prose', () => {
    const prose = 'word '.repeat(60)

    expect(estimate({ role: 'assistant', text: `> ${prose}` }, 80)).toBeGreaterThan(
      estimate({ role: 'assistant', text: prose }, 80)
    )
  })
})
