// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Live Agents Strip: one row per active delegated run (spawn or dag node),
// rendered under the status rule. Clicking a row opens the Agents Overlay
// straight into that run's detail, where its transcript streams as it works.
// Hidden entirely while nothing is running — the strip is a live monitor,
// not a history. `RAVEN_TUI_AGENT_STRIP_LINGER_MS` keeps settled rows on it for
// that long, which is a testing knob rather than a preference: the strip is the
// only way into a run's detail, so a run that ends is one that can no longer be
// opened, and watching that boundary needs the row to stay put.

import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import type { LiveAgentRow } from '../app/liveAgentsStore.js'
import type { Theme } from '../theme.js'

import { $liveAgents } from '../app/liveAgentsStore.js'
import { fmtDuration } from '../domain/messages.js'
import { compactPreview } from '../lib/text.js'
import { openAgentsOverlay, openAgentsOverlayAt } from './agentsOverlay.js'

const MAX_STRIP_ROWS = 4

/** How long a settled row stays on the strip. 0 -- the default -- is the shipped
 *  behaviour: the strip shows what is in flight and nothing else.
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

/** The rows the strip shows: active runs, capped, overflow counted.
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

export const stripRowLabel = (row: LiveAgentRow, maxChars: number): string => {
  const name = row.agent ?? 'raven'
  const detail = row.kind === 'dag-node' ? row.label : (row.instance ?? row.label)

  return compactPreview(detail === name ? name : `${name} · ${detail}`, Math.max(8, maxChars))
}

export function LiveAgentsStrip({ cols, t }: { cols: number; t: Theme }) {
  const rows = useStore($liveAgents)
  const [now, setNow] = useState(() => Date.now())
  const { overflow, visible } = stripRows(rows, MAX_STRIP_ROWS, now)
  const anyRunning = visible.some(r => r.status === 'running')
  // A lingering row leaves on a clock, not on an event, so the tick has to
  // outlast the last running row -- without this the strip would freeze holding
  // a settled row until something else re-rendered it.
  const ticking = anyRunning || (LINGER_MS > 0 && visible.length > 0)

  useEffect(() => {
    if (!ticking) {
      return
    }

    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)

    return () => clearInterval(id)
  }, [ticking])

  if (visible.length === 0) {
    return null
  }

  return (
    <Box flexDirection="column">
      {visible.map(row => {
        const running = row.status === 'running'
        const elapsed = running && row.startedAtMs !== undefined ? fmtDuration(now - row.startedAtMs) : ''

        return (
          <Box
            key={row.id}
            // On the Box, not the Text: only Box carries mouse props in this
            // renderer, so a handler on the Text is silently never called.
            onClick={() => openAgentsOverlayAt(row.id)}
          >
            <Text color={t.color.muted} wrap="truncate-end">
              {'  '}
              <Text color={running ? t.color.accent : t.color.muted}>{running ? '●' : '○'}</Text>{' '}
              <Text color={t.color.text}>{stripRowLabel(row, cols - 24)}</Text>
              {row.kind === 'dag-node' ? <Text color={t.color.label}> dag</Text> : null}
              {elapsed ? ` ${elapsed}` : ''}
              {!running ? ' queued' : ''}
            </Text>
          </Box>
        )
      })}

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
