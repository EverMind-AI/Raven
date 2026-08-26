// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Live Agents Strip: what the session has delegated, under the status rule, in
// two layers. A graph line per `run_subagent_dag` run carries that run's tally
// and stays after the run is over, so a finished fan-out leaves something
// behind; the agent lines under it -- one per running or queued node, plus the
// loose spawns -- are the live layer and leave as each one settles.
//
// Clicking an agent line opens the Agents Overlay straight into that run's
// detail, where its transcript streams as it works; clicking a graph line opens
// the overlay itself, since a graph is not one transcript.
// `RAVEN_TUI_AGENT_STRIP_LINGER_MS` keeps settled agent lines on the strip for
// that long, which is a testing knob rather than a preference: the line is the
// only way into a run's detail, so a run that ends is one that can no longer be
// opened, and watching that boundary needs the line to stay put.

import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import type { LiveAgentRow, LiveDagRun } from '../app/liveAgentsStore.js'
import type { Theme } from '../theme.js'

import { $dagRuns, $liveAgents, dagRunCounts } from '../app/liveAgentsStore.js'
import { fmtDuration } from '../domain/messages.js'
import { compactPreview } from '../lib/text.js'
import { openAgentsOverlay, openAgentsOverlayAt } from './agentsOverlay.js'

const MAX_STRIP_ROWS = 4
/** Graph lines shown at once. Lower than the agent budget on purpose: these do
 *  not leave on their own, so the strip's resting height is this number. */
const MAX_DAG_LINES = 3

/** How long a settled agent line stays on the strip. 0 -- the default -- is the
 *  shipped behaviour: the agent layer shows what is in flight and nothing else.
 *
 *  Read once at module load rather than per render: it is a launch-time knob,
 *  and re-reading it on every frame would cost a `process.env` lookup per row
 *  for a value that cannot change. A malformed value reads as 0, because a
 *  strip that silently kept every row forever is worse than one that ignored a
 *  typo. */
const LINGER_MS = (() => {
  const raw = Number.parseInt(process.env.RAVEN_TUI_AGENT_STRIP_LINGER_MS ?? '', 10)

  return Number.isFinite(raw) && raw > 0 ? raw : 0
})()

/** The agent lines the strip shows: active runs, capped, overflow counted.
 *
 *  `now` is a parameter so a caller that already has a clock (the strip
 *  re-renders on one while anything runs) does not take a second reading, and so
 *  a test can place a row either side of the window without waiting for it. */
export const stripRows = (
  rows: LiveAgentRow[],
  max = MAX_STRIP_ROWS,
  now = Date.now(),
  lingerMs = LINGER_MS
): { overflow: number; visible: LiveAgentRow[] } => {
  const active = rows.filter(r => {
    if (r.status === 'running' || r.status === 'pending') {
      return true
    }

    // `settledAtMs` is stamped by this client when it saw the row turn terminal
    // (see liveAgentsStore); a row without one has been settled since before
    // this session and has no window left to be inside.
    return lingerMs > 0 && r.settledAtMs !== undefined && now - r.settledAtMs < lingerMs
  })

  return { overflow: Math.max(0, active.length - max), visible: active.slice(0, max) }
}

/** One printed line: a graph, or one agent under it (`indent`) / beside it. */
export type StripLine = { indent: boolean; kind: 'row'; row: LiveAgentRow } | { kind: 'dag'; run: LiveDagRun }

/**
 * The strip, laid out: each shown graph followed by its own active nodes, then
 * the runs that belong to no graph.
 *
 * Pure so the layering is testable without a renderer. Graph lines are taken
 * from the end of `runs`, which is insertion-ordered, so the newest graphs are
 * the ones kept; a node whose graph was not kept falls through to the loose
 * lines rather than vanishing.
 */
export const stripLines = (
  rows: LiveAgentRow[],
  runs: LiveDagRun[],
  max = MAX_STRIP_ROWS,
  maxDagLines = MAX_DAG_LINES,
  now = Date.now(),
  lingerMs = LINGER_MS
): { lines: StripLine[]; overflow: number } => {
  const { overflow, visible } = stripRows(rows, max, now, lingerMs)
  const shownRuns = runs.slice(Math.max(0, runs.length - maxDagLines))

  const lines: StripLine[] = []
  const grouped = new Set<string>()

  for (const run of shownRuns) {
    lines.push({ kind: 'dag', run })

    for (const row of visible) {
      if (row.kind === 'dag-node' && row.runId === run.runId) {
        lines.push({ indent: true, kind: 'row', row })
        grouped.add(row.id)
      }
    }
  }

  for (const row of visible) {
    if (!grouped.has(row.id)) {
      lines.push({ indent: false, kind: 'row', row })
    }
  }

  return { lines, overflow: overflow + (runs.length - shownRuns.length) }
}

export const stripRowLabel = (row: LiveAgentRow, maxChars: number): string => {
  const name = row.agent ?? 'raven'
  const detail = row.kind === 'dag-node' ? row.label : (row.instance ?? row.label)

  return compactPreview(detail === name ? name : `${name} · ${detail}`, Math.max(8, maxChars))
}

/** Whether a graph still has a node that can move. What paints its line as
 *  live, and what keeps the strip's clock ticking -- a graph with no node left
 *  is a frozen line, and one that reported no nodes at all is not "running"
 *  either, which a settled-means-all-terminal test would get backwards. */
export const isDagRunActive = (run: LiveDagRun): boolean => {
  const counts = dagRunCounts(run)

  return counts.running + counts.pending > 0
}

/** What a graph line calls itself: its goal when it reported one, else its id. */
export const dagLineName = (run: LiveDagRun, maxChars: number): string =>
  compactPreview(run.summary ?? run.runId, Math.max(8, maxChars))

/**
 * A graph's tally: how many nodes are done, working, and waiting, then how long
 * the graph has been at it -- frozen at its own end once it has one.
 *
 * Zero counts are dropped, so a finished graph reads as its result rather than
 * as a row of noughts. `done` keeps its denominator either way: it is the one
 * count whose meaning depends on the size of the graph.
 */
export const dagLineStats = (run: LiveDagRun, now = Date.now()): string => {
  const counts = dagRunCounts(run)
  const parts = [`${counts.done}/${counts.total} done`]

  if (counts.running > 0) {
    parts.push(`${counts.running} running`)
  }

  if (counts.pending > 0) {
    parts.push(`${counts.pending} queued`)
  }

  if (counts.failed > 0) {
    parts.push(`${counts.failed} failed`)
  }

  if (run.startedAtMs !== undefined) {
    parts.push(fmtDuration((run.endedAtMs ?? (isDagRunActive(run) ? now : run.startedAtMs)) - run.startedAtMs))
  }

  return parts.join(' · ')
}

export function LiveAgentsStrip({ cols, t }: { cols: number; t: Theme }) {
  const rows = useStore($liveAgents)
  const runs = useStore($dagRuns)
  const [now, setNow] = useState(() => Date.now())
  const { lines, overflow } = stripLines(rows, runs, MAX_STRIP_ROWS, MAX_DAG_LINES, now)
  const anyRunning = lines.some(l => (l.kind === 'dag' ? isDagRunActive(l.run) : l.row.status === 'running'))
  // A lingering line leaves on a clock, not on an event, so the tick has to
  // outlast the last running row -- without this the strip would freeze holding
  // a settled row until something else re-rendered it.
  const ticking = anyRunning || (LINGER_MS > 0 && lines.length > 0)

  useEffect(() => {
    if (!ticking) {
      return
    }

    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)

    return () => clearInterval(id)
  }, [ticking])

  if (lines.length === 0) {
    return null
  }

  return (
    <Box flexDirection="column">
      {lines.map(line =>
        line.kind === 'dag' ? (
          <Box
            key={`dag:${line.run.runId}`}
            // On the Box, not the Text: only Box carries mouse props in this
            // renderer, so a handler on the Text is silently never called.
            onClick={() => openAgentsOverlay()}
          >
            <Text color={t.color.muted} wrap="truncate-end">
              {'  '}
              <Text color={isDagRunActive(line.run) ? t.color.accent : t.color.muted}>
                {isDagRunActive(line.run) ? '◆' : '◇'}
              </Text>{' '}
              <Text color={t.color.label}>dag</Text>{' '}
              <Text color={t.color.text}>{dagLineName(line.run, cols - 48)}</Text>
              {` · ${dagLineStats(line.run, now)}`}
            </Text>
          </Box>
        ) : (
          <Box key={line.row.id} onClick={() => openAgentsOverlayAt(line.row.id)}>
            <Text color={t.color.muted} wrap="truncate-end">
              {line.indent ? '    ' : '  '}
              <Text color={line.row.status === 'running' ? t.color.accent : t.color.muted}>
                {line.row.status === 'running' ? '●' : '○'}
              </Text>{' '}
              <Text color={t.color.text}>{stripRowLabel(line.row, cols - 24)}</Text>
              {!line.indent && line.row.kind === 'dag-node' ? <Text color={t.color.label}> dag</Text> : null}
              {line.row.status === 'running' && line.row.startedAtMs !== undefined
                ? ` ${fmtDuration(now - line.row.startedAtMs)}`
                : ''}
              {line.row.status !== 'running' ? ' queued' : ''}
            </Text>
          </Box>
        )
      )}

      {overflow > 0 ? (
        <Box onClick={() => openAgentsOverlay()}>
          <Text color={t.color.muted}>
            {'  '}…+{overflow} more · ^T
          </Text>
        </Box>
      ) : null}
    </Box>
  )
}
