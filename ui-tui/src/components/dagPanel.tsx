// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// One `run_subagent_dag` run: its dependency graph, then a row per node.
//
// The graph is the point. A boxed node per column of dependency depth, wires
// routed between them, and the status glyph inside each box -- so the run reads
// as a shape, and a completed wavefront can be watched moving left to right.
// What a node was *asked* does not fit in a box, so that stays on the rows
// below, which the graph indexes by a short ordinal printed in both places.
//
// A row reads as what the node was asked, not as its id: the id is generated,
// is the widest thing on the row, and answers a question nobody scanning a
// running graph is asking. Clicking the row -- or the node's box -- opens its
// trace box, with the id and the instance handle above it, which is where a
// reader who wants `/dag <node>` will look.
//
// A running node's live line hangs off its row. The rows were dropped once, when
// a labelled box said everything a row did; a node now carries the summary it was
// dispatched with, which no box has room for, so they are drawn under every
// picture again.
//
// The rows are columns, not sentences: the status glyph takes the margin the
// transcript reserves for markers everywhere else, then a fixed ordinal column,
// then an agent column padded to the widest name in the run. A reader scans one
// of those columns down; `Coder: what it was asked` per row started every
// summary at a different offset, so there was nothing to scan.
//
// The run id is the one thing here that names nothing a reader is looking for,
// so it is dim and elided in the middle -- both ends of it, which is what tells
// two runs of one session apart -- and the tally beside it carries the weight.

import { Box, stringWidth, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { memo } from 'react'

import type { DagRunNode, DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { DagGraphGeometry } from '../lib/dagGraphLayout.js'
import type { DagPictureSpan } from '../lib/dagGraphRender.js'
import type { Theme } from '../theme.js'

import { fmtDuration } from '../domain/messages.js'
import { layoutDagGraph } from '../lib/dagGraphLayout.js'
import { renderDagGraph } from '../lib/dagGraphRender.js'
import { $dagOpenNodes, dagNodeKey, dagNodeToggleKey, dagSpanToggleKey, toggleDagNode } from '../lib/dagOpenNodes.js'
import {
  DAG_STATUS_GLYPH,
  dagDetailRoom,
  dagNodeElapsed,
  dagRowDetail,
  dagRunElapsedMs,
  dagRunTally,
  dagSharedInstances
} from '../lib/dagStatus.js'
import { clipToWidth, elideMiddle, padToWidth } from '../lib/text.js'
import { DagNodeSlot } from './dagNodeTrace.js'
import { Spinner } from './thinking.js'

// The status glyph plus its space: the margin column, same as the transcript's
// reply marker and the reasoning rule.
const LEAD = 2

// Between two row columns. Two cells, not one: one reads as a word boundary
// inside a column rather than the edge of it.
const GAP = 2

// Ceilings on the two name columns, so one long name cannot spend the row.
const NAME_CAP = 18
const ID_CAP = 22

// What turns the summary in under its node's name. One cell, since it stands in
// the ordinal's column -- padded to it, so a two-digit run keeps its alignment.
const DETAIL_MARK = '\u2514'

// The node name keeps at least this much when the frame is too narrow for both
// name columns: a clipped id still identifies the row, a clipped agent does not.
const NAME_FLOOR = 8

// What the call is, printed once, at the top of the box that is its result.
const TITLE = 'run subagent dag'

const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? '' : 's'}`

// The run id, elided. Enough of each end to tell two runs of one session apart,
// which is all anyone reads it for; `/dag` takes a node, not a run.
const RUN_ID_CELLS = 22

// The status mark. A running node turns rather than sits: `\u25cf` and `\u25cb` differ
// by a fill a reader has to look for, and a graph whose only sign of life is a
// tally that changes every few minutes reads as stalled. The braille frames are
// one cell wide, same as the static glyphs, so the picture's geometry -- laid
// out from the glyph widths -- is unaffected.
const StatusGlyph = ({ status, t }: { status: DagRunNodeStatus; t: Theme }) => {
  const style = DAG_STATUS_GLYPH[status]

  return status === 'running' ? (
    <Spinner color={style.color(t)} variant="tool" />
  ) : (
    <Text color={style.color(t)}>{style.glyph}</Text>
  )
}

// Only a failed or cancelled node tints its frame. That one has to be findable
// in a glance across a wide graph; giving every status its own frame colour puts
// five of them in competition and leaves none of them loud.
const spanColor = (span: DagPictureSpan, status: DagRunNodeStatus | undefined, t: Theme) => {
  if (span.kind === 'wire') {
    return t.color.border
  }

  if (span.kind === 'border') {
    return status === 'cancelled' || status === 'failed' ? t.color.error : t.color.border
  }

  if (span.kind === 'glyph') {
    return status ? DAG_STATUS_GLYPH[status].color(t) : t.color.text
  }

  return status === 'pending' || status === 'skipped' ? t.color.muted : t.color.text
}

const DagPicture = ({
  nodes,
  picture,
  runId,
  t
}: {
  nodes: readonly DagRunNode[]
  picture: DagGraphGeometry
  runId: string
  t: Theme
}) => {
  const statusOf = (nodeId: string | undefined) => nodes.find(node => node.id === nodeId)?.status

  return (
    <Box flexDirection="column">
      {renderDagGraph(picture).map((row, index) => (
        <Box flexDirection="row" key={index}>
          {row.map((span, cell) => {
            const toggle = dagSpanToggleKey(runId, span, nodes)
            const status = statusOf(span.nodeId)
            const text =
              span.kind === 'glyph' && status ? (
                <StatusGlyph status={status} t={t} />
              ) : (
                <Text color={spanColor(span, status, t)} dim={span.kind === 'wire'}>
                  {span.text}
                </Text>
              )

            // A box opens the same prompt block its row does, so the thing a
            // reader is already looking at is the target. Wires are not clickable
            // -- they belong to no node.
            return toggle ? (
              <Box
                key={cell}
                // On the Box, not the Text: only Box carries mouse props in this
                // fork. The click has to stop here -- dispatchClick bubbles
                // through every ancestor handler, and the transcript rows above
                // toggle on it.
                onClick={(event: { stopImmediatePropagation?: () => void }) => {
                  event.stopImmediatePropagation?.()
                  toggleDagNode(toggle)
                }}
              >
                {text}
              </Box>
            ) : (
              <Box key={cell}>{text}</Box>
            )
          })}
        </Box>
      ))}
    </Box>
  )
}

// One row per node, under whatever picture fits: what it is called, who ran it
// and what it cost on the first line, then what it was asked on a second, turned
// in under the name. With no picture at all that second line is also the only
// place the topology exists, which is why `dagNodeDeps` names every dependency
// again.
//
// Two lines rather than one because the summary is a sentence and the rest are
// columns: sharing a line, the sentence took every cell the columns did not, and
// a CJK one -- two cells a character -- pushed the agent and the elapsed time so
// far right that neither column could be read down any more.
const NodeRow = ({
  drawn,
  elapsedWidth,
  idWidth,
  nameWidth,
  now,
  ordinal,
  ordinalWidth,
  node,
  open,
  runId,
  sharedInstance,
  t,
  width
}: {
  drawn: ReadonlySet<string> | null
  elapsedWidth: number
  idWidth: number
  nameWidth: number
  now: number
  ordinal: number
  ordinalWidth: number
  node: DagRunNode
  open: boolean
  runId: string
  sharedInstance: boolean
  t: Theme
  width: number
}) => {
  // Only a handle two nodes share is topology; the rest of it is in the block.
  const tag = sharedInstance && node.instance ? ` @${node.instance.slice(-6)}` : ''
  const elapsed = dagNodeElapsed(node, now)
  // The mark takes the ordinal's column and the text starts where the node's
  // name does, so the second line reads as belonging to the first rather than
  // as a row of its own.
  const mark = `${' '.repeat(LEAD)}${DETAIL_MARK.padStart(ordinalWidth)}${' '.repeat(GAP)}`
  const detail = dagRowDetail(node, drawn, dagDetailRoom(width, ordinalWidth))

  // A row still pending with no template has nothing to expand to; any node
  // that has started has a trace to show even without one. Asked of the same
  // helper the picture's boxes use, so a row and its box are never expandable
  // apart.
  const toggle = dagNodeToggleKey(runId, node)
  const onClick = toggle
    ? (event: { stopImmediatePropagation?: () => void }) => {
        // On the Box, not the Text: only Box carries mouse props in this fork.
        // The click has to stop here -- dispatchClick bubbles through every
        // ancestor handler, and the transcript rows above this one toggle on it.
        event.stopImmediatePropagation?.()
        toggleDagNode(toggle)
      }
    : undefined

  return (
    <Box flexDirection="column">
      <Box onClick={onClick}>
        <Box flexGrow={1} minWidth={0}>
          <Text color={t.color.muted}>
            <StatusGlyph status={node.status} t={t} />
            <Text color={t.color.border} dim>
              {` ${String(ordinal).padStart(ordinalWidth)}  `}
            </Text>
            <Text bold color={node.status === 'pending' ? t.color.muted : t.color.text}>
              {padToWidth(node.id, idWidth)}
            </Text>
            <Text color={t.color.muted}>{`  ${padToWidth(node.subagent, nameWidth)}`}</Text>
            {tag && (
              <Text color={t.color.muted} dim>
                {tag}
              </Text>
            )}
          </Text>
        </Box>

        <Box flexShrink={0}>
          <Text color={node.status === 'running' ? t.color.accent : t.color.muted} dim={node.status !== 'running'}>
            {`  ${elapsed.padStart(elapsedWidth)}`}
          </Text>
        </Box>
      </Box>

      {(detail.summary || detail.deps || detail.error) && (
        <Box onClick={onClick}>
          <Text color={t.color.muted}>
            <Text color={t.color.border} dim>
              {mark}
            </Text>
            {detail.summary}
            {detail.deps && (
              <Text dim>
                {'  '}
                {detail.deps}
              </Text>
            )}
            {detail.error && (
              <Text color={t.color.error}>
                {detail.summary || detail.deps ? '  ' : ''}
                {detail.error}
              </Text>
            )}
          </Text>
        </Box>
      )}

      <DagNodeSlot node={node} open={open && Boolean(toggle)} ordinal={ordinal} runId={runId} t={t} width={width} />
    </Box>
  )
}

/**
 * What the header keeps when it cannot have everything.
 *
 * Nothing here truncates on its own -- both halves are Texts holding nested
 * Texts, which ink's `truncate-end` does not cut -- so the fit is decided in
 * cells, and the parts are given up in the order they are worth least: the run
 * id, which names nothing a reader is looking for, then the elapsed time, which
 * the strip also carries. The tally and the call itself are what survive a
 * terminal narrow enough to force the choice.
 */
export const fitDagHeader = (
  inner: number,
  left: string,
  runId: string,
  tally: string,
  elapsed: string
): { elapsed: string; left: string; runId: string; tally: string } => {
  const right = (id: string, time: string) => [id, tally, time && `\u00b7 ${time}`].filter(Boolean).join(' ')
  const fits = (drawn: string) => stringWidth(left) + (drawn ? GAP + stringWidth(drawn) : 0) <= inner

  if (fits(right(runId, elapsed))) {
    return { elapsed, left, runId, tally }
  }

  if (fits(right('', elapsed))) {
    return { elapsed, left, runId: '', tally }
  }

  if (fits(right('', ''))) {
    return { elapsed: '', left, runId: '', tally }
  }

  // Even the tally alone does not fit: the call keeps the row, cut to it.
  return { elapsed: '', left: clipToWidth(left, inner), runId: '', tally: '' }
}

const HeaderTally = ({ run, t }: { run: DagRunState; t: Theme }) => (
  <Text>
    {dagRunTally(run).map(({ count, status }, index) => (
      <Text color={DAG_STATUS_GLYPH[status].color(t)} key={status}>
        {index > 0 ? ' ' : ''}
        {count}
        {DAG_STATUS_GLYPH[status].glyph}
      </Text>
    ))}
  </Text>
)

/** The tally as the plain string the fit is measured against. Same order and
 *  spacing `HeaderTally` draws, which is what makes the measurement true. */
const tallyText = (run: DagRunState): string =>
  dagRunTally(run)
    .map(({ count, status }) => `${count}${DAG_STATUS_GLYPH[status].glyph}`)
    .join(' ')

/** The panel's own rule, which is not the box's border: the header and the hint
 *  are furniture, and without a line under each the rows read as continuous
 *  with them. */
const Rule = ({ t, width }: { t: Theme; width: number }) => (
  <Text color={t.color.border} dim>
    {'\u2500'.repeat(Math.max(0, width))}
  </Text>
)

export const DagPanel = memo(function DagPanel({
  now = Date.now(),
  run,
  t,
  width = 116
}: {
  /** Ticked by the transcript while a turn is live, so the elapsed columns move.
   *  Defaulted for the callers that render a settled run once. */
  now?: number
  run: DagRunState
  t: Theme
  width?: number
}) {
  const openNodes = useStore($dagOpenNodes)

  if (run.nodes.length === 0) {
    return null
  }

  // The border takes a column each side and the padding one more.
  const inner = Math.max(24, width - 4)

  // `null` when even the compact label style overflows the row. The topology is
  // then carried by the rows alone, which `dagNodeDeps` handles by naming every
  // dependency again.
  const picture = layoutDagGraph(run.nodes, { width: inner })

  const ordinalWidth = String(run.nodes.length).length
  // One column for every node's time, so they align however long the run gets.
  const elapsedWidth = Math.max(...run.nodes.map(node => stringWidth(dagNodeElapsed(node, now))))
  // The two name columns, shrunk to whatever the frame leaves them: the agent
  // first, since the node's own name is the one a reader came for. Nothing on
  // this row truncates itself -- ink's `truncate-end` is a no-op on the nested
  // Texts it is drawn as -- so a column that does not fit here walks out through
  // the frame instead.
  const columns = Math.max(0, inner - LEAD - ordinalWidth - GAP - GAP - GAP - elapsedWidth)
  const wantedId = Math.min(ID_CAP, Math.max(...run.nodes.map(node => stringWidth(node.id))))
  const wantedName = Math.min(NAME_CAP, Math.max(...run.nodes.map(node => stringWidth(node.subagent))))
  const nameWidth = Math.max(0, Math.min(wantedName, columns - Math.min(wantedId, NAME_FLOOR)))
  const idWidth = Math.max(0, Math.min(wantedId, columns - nameWidth))
  const shared = dagSharedInstances(run.nodes)
  const elapsed = dagRunElapsedMs(run, now)
  const header = fitDagHeader(
    inner,
    `${TITLE}  ${plural(run.nodes.length, 'node')}`,
    elideMiddle(run.runId, RUN_ID_CELLS),
    tallyText(run),
    elapsed === undefined ? '' : fmtDuration(elapsed)
  )

  return (
    <Box
      borderColor={run.nodes.some(node => node.status === 'failed') ? t.color.error : t.color.border}
      borderStyle="round"
      flexDirection="column"
      marginBottom={1}
      paddingX={1}
      width={Math.max(28, width)}
    >
      {/* The call this panel is the result of, said once. The transcript row it
          used to sit under is suppressed for a dag call (see `WorkSegment`):
          two headings for one graph is what the box replaced. */}
      <Box>
        <Box flexGrow={1} minWidth={0}>
          <Text bold color={t.color.primary}>
            {header.left.slice(0, TITLE.length)}
            <Text color={t.color.muted}>{header.left.slice(TITLE.length)}</Text>
          </Text>
        </Box>

        <Box flexShrink={0}>
          <Text color={t.color.muted}>
            {header.runId && <Text dim>{header.runId} </Text>}
            {header.tally && <HeaderTally run={run} t={t} />}
            {header.elapsed ? ` \u00b7 ${header.elapsed}` : ''}
          </Text>
        </Box>
      </Box>

      {run.replannedInto && (
        <Text color={t.color.muted} dim wrap="truncate-end">
          {`replanned into ${elideMiddle(run.replannedInto, RUN_ID_CELLS)}`}
        </Text>
      )}

      <Rule t={t} width={inner} />

      {picture && <DagPicture nodes={run.nodes} picture={picture} runId={run.runId} t={t} />}

      {/* Under the picture, not instead of it. A box carries the agent and the
          status; the row carries the node's own name, what it is waiting on, and
          what it cost -- none of which fits in a box. */}
      {run.nodes.map((node, index) => (
        <NodeRow
          drawn={picture?.drawn ?? null}
          elapsedWidth={elapsedWidth}
          idWidth={idWidth}
          nameWidth={nameWidth}
          now={now}
          ordinal={index + 1}
          ordinalWidth={ordinalWidth}
          key={node.id}
          node={node}
          open={openNodes.has(dagNodeKey(run.runId, node.id))}
          runId={run.runId}
          sharedInstance={Boolean(node.instance && shared.has(node.instance))}
          t={t}
          width={inner}
        />
      ))}

      {run.done && run.dir && (
        <Text color={t.color.muted} dim wrap="truncate-end">
          {`outputs in ${run.dir}`}
        </Text>
      )}

      <Rule t={t} width={inner} />

      {/* Both ways in, on the one line: the click is invisible otherwise, and
          `/dag` is the only one that survives a trace too long for the box. */}
      <Text color={t.color.muted} dim wrap="truncate-end">
        {`click a node to open it \u00b7 /dag ${run.nodes.length > 1 ? `1-${run.nodes.length}` : '1'} for a full trace`}
      </Text>
    </Box>
  )
})
