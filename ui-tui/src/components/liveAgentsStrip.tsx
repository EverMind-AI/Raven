// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Live Agents Strip: what the session has delegated, under the status rule,
// top to bottom: the `Raven` way-back row, the hand-made instances, the loose
// spawns, then the graphs -- whole and last. The instance rows -- one per
// addressable instance while resumable, running or idle -- are the job the
// chips row above the composer used to do: clicking one switches Direct Chat
// to it, the `Raven` row leading the strip is the way back to the main
// conversation, and the composer's own top border, not the strip, says which
// one is active. A graph line per `run_subagent_dag` run carries that run's
// tally and stays after the run is over, so a finished fan-out leaves
// something behind; under each, an agent line per running or queued node, then
// the instances the graph fanned out -- kept indented there after the run, the
// header rebuilt from the rows themselves after a resume. The loose spawns
// leave as each one settles.
//
// Clicking a non-instance agent line opens the Agents Overlay straight into
// that run's detail, where its transcript streams as it works; clicking a graph
// line opens the overlay itself, since a graph is not one transcript.
// `RAVEN_TUI_AGENT_STRIP_LINGER_MS` keeps settled agent lines on the strip for
// that long, which is a testing knob rather than a preference: the line is the
// only way into a run's detail, so a run that ends is one that can no longer be
// opened, and watching that boundary needs the line to stay put.

import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import type { DirectTargetRef } from '../app/directChatStore.js'
import type { LiveAgentRow, LiveDagRun } from '../app/liveAgentsStore.js'
import type { InstanceRow } from '../rpc/generated.js'
import type { Theme } from '../theme.js'

import { $directChat, directKey, enterDirect, isDirectTarget, leaveDirect } from '../app/directChatStore.js'
import { $dagRuns, $liveAgents, dagRunCounts } from '../app/liveAgentsStore.js'
import { fmtDuration } from '../domain/messages.js'
import { compactPreview } from '../lib/text.js'
import { openAgentsOverlay, openAgentsOverlayAt } from './agentsOverlay.js'

const MAX_STRIP_ROWS = 4
/** Graph lines shown at once. Lower than the agent budget on purpose: these do
 *  not leave on their own, so the strip's resting height is this number. */
const MAX_DAG_LINES = 3
/** Idle instance rows shown at once outside any graph. These do not leave on
 *  their own either -- they stand while the instance is resumable -- so they
 *  get their own small budget beside the live one rather than eating it. */
const MAX_IDLE_INSTANCE_ROWS = 2
/** Idle instance rows shown under one graph line. Per graph rather than shared
 *  with the flat budget, so one large fan-out neither hides every other
 *  instance nor stacks its whole roster under its own line. */
const MAX_IDLE_RUN_INSTANCE_ROWS = 2

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

/** One printed line: a graph, or one agent under it (`indent`) / beside it.
 *  A line carrying `target` is instance-backed: clicking it switches Direct
 *  Chat there instead of opening the Agents Overlay; `target: null` is the
 *  main conversation itself. */
export type StripLine =
  | { indent: boolean; kind: 'row'; row: LiveAgentRow; target?: DirectTargetRef | null }
  | { kind: 'dag'; run: LiveDagRun }

/** The way back to the main conversation, heading the instance layer whenever
 *  that layer is non-empty. What the chips row's first chip always was: a strip
 *  that offers ways in but no way out strands the user in a direct chat. */
const MAIN_STRIP_ROW: LiveAgentRow = {
  agent: 'Raven',
  id: 'inst:main',
  kind: 'spawn',
  label: 'Raven',
  seq: -1,
  status: 'completed'
}

/** An instance rendered through the row shape the strip already draws. `status`
 *  falls back to the registry row's own field so the bullet stays honest on the
 *  legacy gateway bus, where no `subagent.status` event ever arrives;
 *  `interrupted` deliberately does not read as running, since the row says so
 *  only because a gateway died mid-turn and no reply is coming. */
const instanceStripRow = (r: InstanceRow): LiveAgentRow => ({
  agent: r.agent,
  id: `inst:${directKey(r.agent, r.handle)}`,
  instance: r.handle,
  kind: 'spawn',
  label: r.title ?? r.handle,
  seq: r.createdAtMs ?? 0,
  status: r.status === 'running' || r.status === 'pending' ? 'running' : 'completed'
})

/** When a run's instances last grew: the freshest fan-outs win header slots. */
const lastCreated = (insts: readonly InstanceRow[]): number =>
  insts.reduce((at, r) => Math.max(at, r.createdAtMs ?? 0), 0)

/** A graph line rebuilt from its instances' own rows, for a run the live store
 *  no longer holds (a resume seeds only graphs still working). No nodes and no
 *  clock -- `dagLineStats` renders those as nothing -- but the grouping and the
 *  goal survive, because the rows carry `runId` and `runTitle` themselves. */
const syntheticRun = (runId: string, insts: readonly InstanceRow[]): LiveDagRun => ({
  nodes: {},
  runId,
  seq: -1,
  summary: insts.find(r => typeof r.runTitle === 'string' && r.runTitle !== '')?.runTitle ?? runId
})

/**
 * The strip, laid out: each shown graph followed by its own active nodes and
 * then its own settled instances, then the flat instance rows, then the runs
 * that belong to no graph.
 *
 * Pure so the layering is testable without a renderer. Graph lines are taken
 * from the end of `runs`, which is insertion-ordered, so the newest graphs are
 * the ones kept; a node whose graph was not kept falls through to the loose
 * lines rather than vanishing.
 *
 * The instance layer is what the chips row above the composer used to hold: a
 * resumable instance keeps its row running or idle, ordered by when it first
 * appeared and never resorted, and a live spawn row that answers to an
 * instance is merged into that one line rather than shown twice. An instance
 * born of a graph stays under that graph's line -- who fanned it out is what a
 * reader wants after the run as much as during it -- and when the live store
 * has no line for its run (after a resume), one is rebuilt from the rows
 * themselves, newest runs filling whatever graph-line budget is left. The
 * active target rides along whatever its `resumable` says -- the conversation
 * on screen must never fall off the strip.
 */
export const stripLines = (
  rows: LiveAgentRow[],
  runs: LiveDagRun[],
  instances: readonly InstanceRow[] = [],
  active: DirectTargetRef | null = null,
  max = MAX_STRIP_ROWS,
  maxDagLines = MAX_DAG_LINES,
  maxIdle = MAX_IDLE_INSTANCE_ROWS,
  now = Date.now(),
  lingerMs = LINGER_MS,
  maxIdlePerRun = MAX_IDLE_RUN_INSTANCE_ROWS
): { lines: StripLine[]; overflow: number } => {
  const { overflow: liveOverflow, visible } = stripRows(rows, max, now, lingerMs)
  const shownRuns = runs.slice(Math.max(0, runs.length - maxDagLines))
  const visibleIds = new Set(visible.map(r => r.id))

  const switchable = instances
    .filter(
      r =>
        r.kind !== 'dag-node' &&
        (r.resumable === true || isDirectTarget(active, { agent: r.agent, handle: r.handle }))
    )
    .sort((a, b) => (a.createdAtMs ?? 0) - (b.createdAtMs ?? 0))

  // Partition the instances: a row whose node line is on the strip is that
  // line for now (a stateful DAG node registers an instance row for the same
  // invocation, and two rows would say it twice); a row born of a graph
  // belongs under that graph's line; the rest are flat.
  const flat: InstanceRow[] = []
  const byRun = new Map<string, InstanceRow[]>()

  for (const inst of switchable) {
    if (inst.runId !== undefined && inst.nodeId !== undefined && visibleIds.has(`${inst.runId}/${inst.nodeId}`)) {
      continue
    }

    if (inst.runId !== undefined) {
      byRun.set(inst.runId, [...(byRun.get(inst.runId) ?? []), inst])
    } else {
      flat.push(inst)
    }
  }

  // Runs the live store no longer holds get a synthesized header, the newest
  // filling what is left of the graph-line budget (shown oldest-first, like
  // the live window); a run beyond the budget keeps its instances, flat.
  const shownIds = new Set(shownRuns.map(r => r.runId))
  const orphans = [...byRun.entries()]
    .filter(([runId]) => !shownIds.has(runId))
    .sort((a, b) => lastCreated(b[1]) - lastCreated(a[1]))
  const kept = orphans.slice(0, Math.max(0, maxDagLines - shownRuns.length)).reverse()

  for (const [, insts] of orphans.slice(Math.max(0, maxDagLines - shownRuns.length))) {
    flat.push(...insts)
  }

  flat.sort((a, b) => (a.createdAtMs ?? 0) - (b.createdAtMs ?? 0))

  const groups: { insts: InstanceRow[]; run: LiveDagRun }[] = [
    ...shownRuns.map(run => ({ insts: byRun.get(run.runId) ?? [], run })),
    ...kept.map(([runId, insts]) => ({ insts, run: syntheticRun(runId, insts) }))
  ]

  const consumed = new Set<string>()

  const toLine = (inst: InstanceRow): { row: LiveAgentRow; target: DirectTargetRef } => {
    const target = { agent: inst.agent, handle: inst.handle }
    const live = visible.find(r => r.kind === 'spawn' && r.agent === inst.agent && r.instance === inst.handle)

    if (live !== undefined) {
      consumed.add(live.id)

      return { row: live, target }
    }

    return { row: instanceStripRow(inst), target }
  }

  // Idle rows are capped so standing instances cannot eat the transcript.
  // Running rows never compete with them, and one slot is reserved for the
  // active target's idle row: hiding where the user is mid-switch is worse
  // than hiding a neighbour.
  const isIdle = (l: { row: LiveAgentRow }) => l.row.status !== 'running' && l.row.status !== 'pending'
  let droppedIdle = 0

  const capIdle = (all: { row: LiveAgentRow; target: DirectTargetRef }[], budget: number) => {
    const activeIdle = all.find(l => isIdle(l) && isDirectTarget(active, l.target))
    const out: typeof all = []
    let left = budget
    let seated = false

    for (const line of all) {
      if (!isIdle(line)) {
        out.push(line)
        continue
      }

      const reserve = activeIdle !== undefined && !seated && line !== activeIdle ? 1 : 0

      if (left - reserve > 0) {
        out.push(line)
        left--
        seated ||= line === activeIdle
      } else {
        droppedIdle++
      }
    }

    return out
  }

  const groupLines: StripLine[] = []
  const grouped = new Set<string>()
  let anyInstance = false

  for (const { insts, run } of groups) {
    groupLines.push({ kind: 'dag', run })

    for (const row of visible) {
      if (row.kind === 'dag-node' && row.runId === run.runId) {
        groupLines.push({ indent: true, kind: 'row', row })
        grouped.add(row.id)
      }
    }

    for (const line of capIdle(insts.map(toLine), maxIdlePerRun)) {
      anyInstance = true
      groupLines.push({ indent: true, kind: 'row', ...line })
    }
  }

  const flatLines = capIdle(flat.map(toLine), maxIdle)
  const lines: StripLine[] = []

  // Top to bottom: the way back, the hand-made instances, the loose spawns,
  // then the graphs, whole and last. The Raven row leads and sits outside
  // every budget: the one line that must never shed or move.
  if (anyInstance || flatLines.length > 0) {
    lines.push({ indent: false, kind: 'row', row: MAIN_STRIP_ROW, target: null })
  }

  for (const line of flatLines) {
    lines.push({ indent: false, kind: 'row', ...line })
  }

  for (const row of visible) {
    if (!grouped.has(row.id) && !consumed.has(row.id)) {
      lines.push({ indent: false, kind: 'row', row })
    }
  }

  lines.push(...groupLines)

  return { lines, overflow: liveOverflow + droppedIdle + (runs.length - shownRuns.length) }
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
  // A synthesized run (rebuilt from instance rows after a resume) declared no
  // nodes; "0/0 done" would read as a graph that did nothing.
  const parts = counts.total > 0 ? [`${counts.done}/${counts.total} done`] : []

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
  const direct = useStore($directChat)
  const [now, setNow] = useState(() => Date.now())
  const { lines, overflow } = stripLines(
    rows,
    runs,
    direct.instances,
    direct.active,
    MAX_STRIP_ROWS,
    MAX_DAG_LINES,
    MAX_IDLE_INSTANCE_ROWS,
    now
  )
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
              {dagLineStats(line.run, now) === '' ? '' : ` · ${dagLineStats(line.run, now)}`}
            </Text>
          </Box>
        ) : (
          <Box
            key={line.row.id}
            onClick={() => {
              if (line.target === undefined) {
                return openAgentsOverlayAt(line.row.id)
              }

              return line.target === null ? leaveDirect() : enterDirect(line.target.agent, line.target.handle)
            }}
          >
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
              {line.row.status === 'pending' ? ' queued' : ''}
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
