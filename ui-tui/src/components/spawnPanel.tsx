// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// One `spawn` call: a bordered panel carrying the call, the run's status, and
// the tail of its live trace.
//
// The dag panel minus the graph: a spawn is a single run, so there is no
// topology to draw and no rows to scan -- the panel is a header naming the call
// and a trace box under it. The box is open by default while the run works,
// because watching the sub-agent move is the panel's whole point, and folds
// once the run settles, when the transcript wants its rows back; a reader's own
// toggle wins over both (see `spawnOpen`).
//
// The trace box is a constant height for the same reasons the dag node's is
// (see `dagNodeTrace.tsx`): it is re-read twice a second while the run works,
// and a box that grew with the trace would shove every row beneath it that
// often. `fitTraceTail` folds the wire messages through the transcript's own
// renderer -- dense, since the breathing rows the main transcript spends
// between segments are blank lines an eight-row box cannot afford -- so a
// delegated run reads like the main agent, not like a second renderer that
// drifts. The tail is bottom-aligned: slack sits above the newest step, where
// it reads as "history above" rather than as a hole before the footer.
//
// One footer line carries everything the box owes the reader -- what was cut,
// the fold toggle, and /agents -- because a second rule and a second dim hint
// row were more furniture than a single-run panel can justify.
//
// Folded, that same row shows the run's newest line instead of a hint. A hint
// spends the panel's one remaining row saying what the disclosure mark already
// says, while the tail says what the run is doing -- and, because it is re-read
// with the trace, a folded panel still reads as moving rather than as a closed
// drawer.

import { Box, stringWidth, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { memo, useMemo } from 'react'

import type { SpawnRunState } from '../domain/spawnRun.js'
import type { Theme } from '../theme.js'

import { $dagNodeTraces } from '../app/dagNodeStore.js'
import { fmtDuration } from '../domain/messages.js'
import { DAG_STATUS_GLYPH, dagTraceRows } from '../lib/dagStatus.js'
import { fitTraceTail, traceTailLine } from '../lib/dagStream.js'
import { $spawnOpenOverrides, spawnTraceKey, spawnTraceOpen, toggleSpawnTrace } from '../lib/spawnOpen.js'
import { clipToWidth, elideMiddle } from '../lib/text.js'
import { MessageLine } from './messageLine.js'
import { Spinner } from './thinking.js'

// What the call is, printed once, at the top of the box that is its result --
// the same convention as the dag panel's `run subagent dag`.
const TITLE = 'spawn'

// The disclosure mark on the folded row: what the row costs the tail, and the
// only thing on a folded panel that says it opens.
const OPEN_MARK = '\u25be '

// The record id, elided like the dag header's run id: enough of each end to
// tell two runs apart, which is all anyone reads it for.
const CALL_ID_CELLS = 22

// Bounds the prompt fallback, as in `dagNodeTrace.tsx`.
const PROMPT_CHARS = 4000

/** The panel's own rule -- same furniture as the dag panel's. */
const Rule = ({ t, width }: { t: Theme; width: number }) => (
  <Text color={t.color.border} dim>
    {'─'.repeat(Math.max(0, width))}
  </Text>
)

export const SpawnPanel = memo(function SpawnPanel({
  now = Date.now(),
  prompt = '',
  run,
  t,
  width = 116
}: {
  /** Ticked by the transcript while a turn is live, so the elapsed time moves. */
  now?: number
  /** The task as submitted -- the tool call's own argument. The only thing the
   *  trace box can show before the run's first step reaches the collector. */
  prompt?: string
  run: SpawnRunState
  t: Theme
  width?: number
}) {
  const overrides = useStore($spawnOpenOverrides)
  const traces = useStore($dagNodeTraces)

  const open = spawnTraceOpen(run, overrides)
  const running = run.status === 'running'
  const style = DAG_STATUS_GLYPH[run.status]
  const elapsed = run.startedAt !== undefined ? fmtDuration(Math.max(0, (run.endedAt ?? now) - run.startedAt)) : ''

  // The border takes a column each side and the padding one more.
  const inner = Math.max(24, width - 4)

  // The header's right half: while the run works the spinner already says
  // "running", so the word would say it twice and the elapsed time carries the
  // slot; a settled run names its status, which nothing else on the panel does.
  // Measured in cells so the label can be cut to what is left -- nothing here
  // truncates on its own (nested Texts, see the dag header's note). The mark
  // and its space cost 2.
  const right = running ? elapsed || 'running' : `${run.status}${elapsed ? ` · ${elapsed}` : ''}`
  const labelRoom = Math.max(8, inner - stringWidth(TITLE) - 2 - stringWidth(right) - 2 - 2)
  const label = clipToWidth(run.label, labelRoom)

  const rows = dagTraceRows(run.status)
  const messages = run.callId ? traces.get(spawnTraceKey(run.callId))?.messages : undefined
  const fit = useMemo(() => fitTraceTail(messages ?? [], rows, inner), [inner, messages, rows])
  // The folded row's own budget: the panel's width less the disclosure mark.
  const tailRoom = Math.max(8, inner - stringWidth(OPEN_MARK))
  const tail = useMemo(
    () => (open ? '' : traceTailLine(messages ?? [], tailRoom, running)),
    [messages, open, running, tailRoom]
  )
  const handle = run.agent ? `${run.agent}${run.instance ? `@${run.instance}` : ''}` : ''
  // What identifies the run, most useful first: the handle a reader scans for,
  // the tally, and last the record id -- which names nothing a reader is
  // looking for here (/agents does not take it) but still tells two runs apart.
  const meta = [
    handle,
    fit.shown.length > 0 ? `${messages?.length ?? 0} msgs` : '',
    elideMiddle(run.callId ?? run.taskId, CALL_ID_CELLS)
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <Box
      borderColor={run.status === 'failed' || run.status === 'cancelled' ? t.color.error : t.color.border}
      borderStyle="round"
      flexDirection="column"
      marginBottom={1}
      // On the Box, not a Text: only Box carries mouse props in this fork. The
      // click has to stop here -- dispatchClick bubbles through every ancestor
      // handler, and the transcript rows above this panel toggle on it.
      onClick={(event: { stopImmediatePropagation?: () => void }) => {
        event.stopImmediatePropagation?.()
        toggleSpawnTrace(run)
      }}
      paddingX={1}
      width={Math.max(28, width)}
    >
      {/* The call this panel is the result of, said once. The transcript row it
          would sit under is suppressed for a spawn call (see `WorkSegment`),
          exactly as for a dag call: two headings for one run. */}
      <Box>
        <Box flexGrow={1} minWidth={0}>
          <Text bold color={t.color.primary}>
            {TITLE}
            <Text bold={false} color={t.color.muted}>
              {'  '}
              {label}
            </Text>
          </Text>
        </Box>

        <Box flexShrink={0}>
          {running ? (
            <Text color={style.color(t)}>
              <Spinner color={style.color(t)} variant="tool" /> {elapsed || 'running'}
            </Text>
          ) : (
            <Text color={style.color(t)}>
              {style.glyph} {run.status}
              {elapsed ? (
                <Text color={t.color.muted} dim>
                  {' · '}
                  {elapsed}
                </Text>
              ) : null}
            </Text>
          )}
        </Box>
      </Box>

      <Rule t={t} width={inner} />

      {open ? (
        <>
          <Text color={t.color.muted} dim wrap="truncate-end">
            {meta}
          </Text>

          {/* Bottom-aligned when it holds a tail: slack sits above the newest
              step, where it reads as "history above" rather than as a hole
              before the footer. The prompt fallback keeps the top -- its head
              is the part that says what was asked. */}
          <Box
            flexDirection="column"
            height={rows}
            justifyContent={fit.shown.length > 0 ? 'flex-end' : 'flex-start'}
            overflow="hidden"
          >
            {fit.shown.length > 0 ? (
              /* `hidden` offsets the key out of the window, so a row keeps its
                 instance as the tail slides (see the same note in
                 `dagNodeTrace`). */
              fit.shown.map((msg, index) => (
                <MessageLine cols={inner} dense key={fit.hidden + index} msg={msg} t={t} />
              ))
            ) : (
              <Text color={t.color.text} wrap="wrap">
                {prompt.length > PROMPT_CHARS ? `${prompt.slice(0, PROMPT_CHARS)}\n…` : prompt}
              </Text>
            )}
          </Box>

          <Text color={t.color.muted} dim wrap="truncate-end">
            {fit.hidden > 0 ? `↑ ${fit.hidden} earlier · ` : ''}
            {'click to fold · /agents for the full trace'}
          </Text>
        </>
      ) : (
        /* The newest line the run said, or the head of what it was asked
           while it has said nothing yet -- and the hint only when it has
           neither, so the row is never blank next to the mark. */
        <Box>
          <Text color={t.color.accent}>{OPEN_MARK}</Text>
          <Text color={t.color.muted} dim wrap="truncate-end">
            {tail ||
              clipToWidth(prompt.slice(0, PROMPT_CHARS), tailRoom) ||
              'click to open the trace · /agents for the full trace'}
          </Text>
        </Box>
      )}
    </Box>
  )
})
