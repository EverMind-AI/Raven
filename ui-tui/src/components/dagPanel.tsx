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

import { layoutDagGraph } from '../lib/dagGraphLayout.js'
import { renderDagGraph } from '../lib/dagGraphRender.js'
import { $dagOpenNodes, dagNodeKey, dagNodeToggleKey, dagSpanToggleKey, toggleDagNode } from '../lib/dagOpenNodes.js'
import {
  DAG_STATUS_GLYPH,
  dagNodeDeps,
  dagNodeSummary,
  dagRunHeadline,
  dagSharedInstances
} from '../lib/dagStatus.js'
import { elideMiddle, padToWidth } from '../lib/text.js'
import { DagNodeSlot } from './dagNodeTrace.js'
import { Spinner } from './thinking.js'

// The status glyph plus its space: the margin column, same as the transcript's
// reply marker and the reasoning rule.
const LEAD = 2

// Between two row columns. Two cells, not one: one reads as a word boundary
// inside a column rather than the edge of it.
const GAP = 2

// Ceiling on the agent column, so one long agent name cannot spend the row that
// the summaries are the point of.
const NAME_CAP = 18

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

// One row per node, under whatever picture fits. The row carries what the box
// cannot: the summary the node was dispatched with, its live line while it runs,
// and its trace once opened. With no picture at all they are also the only place
// the topology exists, which is why `dagNodeDeps` names every dependency again.
const NodeRow = ({
  drawn,
  nameWidth,
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
  nameWidth: number
  ordinal: number
  ordinalWidth: number
  node: DagRunNode
  open: boolean
  runId: string
  sharedInstance: boolean
  t: Theme
  width: number
}) => {
  const deps = dagNodeDeps(node, drawn)
  // Only a handle two nodes share is topology; the rest of it is in the block.
  const tag = sharedInstance && node.instance ? ` @${node.instance.slice(-6)}` : ''
  // What the columns ahead of the summary leave it. The dependency list is not
  // billed here: it is the one part that wraps without losing anything, so
  // charging the summary for it would clip the words a reader is here for and
  // wrap anyway. Floored so a wide agent column cannot squeeze the summary to
  // nothing.
  const room = Math.max(24, width - LEAD - (ordinalWidth + GAP) - (nameWidth + GAP) - tag.length)
  const summary = dagNodeSummary(node.nodeSummary, node.promptTemplate, room)

  // A row still pending with no template has nothing to expand to; any node
  // that has started has a trace to show even without one. Asked of the same
  // helper the picture's boxes use, so a row and its box are never expandable
  // apart.
  const toggle = dagNodeToggleKey(runId, node)

  return (
    <Box flexDirection="column">
      <Box
        // On the Box, not the Text: only Box carries mouse props in this fork.
        // The click has to stop here -- dispatchClick bubbles through every
        // ancestor handler, and the transcript rows above this one toggle on it.
        onClick={
          toggle
            ? (event: { stopImmediatePropagation?: () => void }) => {
                event.stopImmediatePropagation?.()
                toggleDagNode(toggle)
              }
            : undefined
        }
      >
        <Text color={t.color.muted}>
          <StatusGlyph status={node.status} t={t} />
          <Text color={t.color.border} dim>
            {` ${String(ordinal).padStart(ordinalWidth)}  `}
          </Text>
          <Text color={node.status === 'pending' ? t.color.muted : t.color.text}>
            {/* Neither a summary nor a template reached the client, so the id is
                all this row has -- and then it stands in for the agent column
                too, because a row that names nothing is worse than a row named
                after its id. */}
            {summary ? `${padToWidth(node.subagent, nameWidth)}  ${summary}` : node.id}
          </Text>
          {tag && (
            <Text color={t.color.muted} dim>
              {tag}
            </Text>
          )}
          {deps && (
            <Text color={t.color.muted} dim>
              {deps}
            </Text>
          )}
          {node.error && (
            <Text color={t.color.error}>
              {' — '}
              {node.error}
            </Text>
          )}
        </Text>
      </Box>

      <DagNodeSlot
        node={node}
        open={open && Boolean(toggle)}
        ordinal={ordinal}
        runId={runId}
        t={t}
        width={width}
      />
    </Box>
  )
}

export const DagPanel = memo(function DagPanel({
  run,
  t,
  width = 116
}: {
  run: DagRunState
  t: Theme
  width?: number
}) {
  const openNodes = useStore($dagOpenNodes)

  if (run.nodes.length === 0) {
    return null
  }

  // `null` when even the compact label style overflows the row. The topology is
  // then carried by the rows alone, which `dagNodeDeps` handles by naming every
  // dependency again.
  const picture = layoutDagGraph(run.nodes, { width })

  const ordinalWidth = String(run.nodes.length).length
  const nameWidth = Math.min(NAME_CAP, Math.max(...run.nodes.map(node => stringWidth(node.subagent))))
  const shared = dagSharedInstances(run.nodes)

  return (
    <Box flexDirection="column" marginBottom={1}>
      <Text>
        <Text color={t.color.muted} dim>
          {elideMiddle(run.runId, RUN_ID_CELLS)}
        </Text>{' '}
        <Text color={t.color.muted}>{dagRunHeadline(run)}</Text>
      </Text>

      {picture && <DagPicture nodes={run.nodes} picture={picture} runId={run.runId} t={t} />}

      {/* Under the picture, not instead of it. The rows were dropped once, when a
          labelled box said everything a row did; a node now carries the summary
          it was dispatched with, which no box has room for, so the two no longer
          say the same thing. */}
      {run.nodes.map((node, index) => (
        <NodeRow
          drawn={picture?.drawn ?? null}
          nameWidth={nameWidth}
          ordinal={index + 1}
          ordinalWidth={ordinalWidth}
          key={node.id}
          node={node}
          open={openNodes.has(dagNodeKey(run.runId, node.id))}
          runId={run.runId}
          sharedInstance={Boolean(node.instance && shared.has(node.instance))}
          t={t}
          width={width}
        />
      ))}

      {run.done && run.dir && (
        <Text color={t.color.muted} dim>
          {`  outputs in ${run.dir}`}
        </Text>
      )}
    </Box>
  )
})
