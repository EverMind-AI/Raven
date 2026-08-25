import { describe, expect, it } from 'vitest'

import type { DagRunNode, DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { EpisodeTool, Msg } from '../types.js'

import { DAG_TRACE_BOX_ROWS } from '../config/limits.js'
import { layoutDagGraph } from '../lib/dagGraphLayout.js'
import { dagNodeKey } from '../lib/dagOpenNodes.js'
import { estimatedMsgHeight, messageHeightKey, wrappedLines } from '../lib/virtualHeights.js'

const node = (id: string, status: DagRunNodeStatus = 'pending') => ({ dependsOn: [], id, status, subagent: 'echo' })

const dagRun = (nodeCount: number, extra: Partial<DagRunState> = {}): DagRunState => ({
  done: false,
  nodes: Array.from({ length: nodeCount }, (_, i) => node(`n${i}`)),
  runId: 'dag-1',
  ...extra
})

const withDagTool = (dag: DagRunState | undefined, ...extraTools: EpisodeTool[]): Msg => ({
  episodes: [{ index: 0, tools: [{ dag, id: 't0', name: 'run_subagent_dag', ok: true, summary: 'dag' }, ...extraTools] }],
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
    const picture = layoutDagGraph(dag.nodes, { width: Math.max(24, dagWidth - 4) })
    // 1 call row, plus DagPanel's own: header + picture + one row per node + outputs line.
    const expected = 1 + (1 + (picture?.height ?? 0) + dag.nodes.length + 1)

    expect(estimatedMsgHeight(msg, 80, { compact: false, details: false })).toBe(expected)
  })

  it('accounts for a DAG panel drawn under a multi-call work segment', () => {
    const dag = dagRun(4)
    const msg = withDagTool(dag, { id: 't1', name: 'ls', ok: true, summary: 'ls' })

    const dagWidth = Math.max(20, 80 - 4)
    const picture = layoutDagGraph(dag.nodes, { width: Math.max(24, dagWidth - 6) })
    // Summary row + one row per call (both calls, open by default), plus one DagPanel.
    const expected = 1 + 2 + (1 + (picture?.height ?? 0) + dag.nodes.length)

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
  it('counts a stream line for each running node', () => {
    const idle = msgWithDag(runWith([node('a', 'completed')]))
    const busy = msgWithDag(runWith([node('a', 'running')]))

    expect(estimatedMsgHeight(busy, 100, BASE)).toBe(estimatedMsgHeight(idle, 100, BASE) + 1)
  })

  it('counts the box for an expanded node instead of the line', () => {
    const msg = msgWithDag(runWith([node('a', 'running')]))
    const open = new Set([dagNodeKey('r1', 'a')])

    expect(estimatedMsgHeight(msg, 100, { ...BASE, dagOpen: open })).toBe(
      estimatedMsgHeight(msg, 100, BASE) - 1 + DAG_TRACE_BOX_ROWS
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

  it('does not count a box for a pending node with no template, even with its bare key open', () => {
    const msg = msgWithDag(runWith([node('a', 'pending')]))
    const open = new Set([dagNodeKey('r1', 'a')])

    expect(estimatedMsgHeight(msg, 100, { ...BASE, dagOpen: open })).toBe(estimatedMsgHeight(msg, 100, BASE))
  })
})
