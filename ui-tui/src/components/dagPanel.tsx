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
// trace box, with the id above it, which is where a reader who wants
// `/dag <node>` will look.

import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { memo } from 'react'

import type { DagRunNode, DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { DagGraphGeometry } from '../lib/dagGraphLayout.js'
import type { DagPictureSpan } from '../lib/dagGraphRender.js'
import type { Theme } from '../theme.js'

import { layoutDagGraph } from '../lib/dagGraphLayout.js'
import { renderDagGraph } from '../lib/dagGraphRender.js'
import { $dagOpenNodes, dagNodeKey, dagNodeToggleKey, dagSpanToggleKey, toggleDagNode } from '../lib/dagOpenNodes.js'
import { DAG_STATUS_GLYPH, dagNodeNames, dagNodeSummary, dagRunHeadline } from '../lib/dagStatus.js'
import { DagNodeSlot } from './dagNodeTrace.js'

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
            const text = (
              <Text color={spanColor(span, statusOf(span.nodeId), t)} dim={span.kind === 'wire'}>
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

const NodeRow = ({
  drawn,
  ordinal,
  node,
  open,
  runId,
  t,
  width
}: {
  drawn: ReadonlySet<string> | null
  ordinal: number
  node: DagRunNode
  open: boolean
  runId: string
  t: Theme
  width: number
}) => {
  const style = DAG_STATUS_GLYPH[node.status]
  const { deps, parens } = dagNodeNames(node, drawn)
  const head = `${ordinal} `
  // What the parts that must share the summary's line leave it. The dependency
  // list is not among them: it is the one part that wraps without losing
  // anything, so billing the summary for it would clip the words a reader is
  // here for and still wrap. Floored so an outsized ordinal cannot squeeze the
  // summary to nothing.
  const room = Math.max(
    24,
    width - head.length - style.glyph.length - 1 - node.subagent.length - 2 - (parens ? parens.length + 1 : 0)
  )
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
          <Text color={t.color.border} dim>
            {head}
          </Text>
          <Text color={style.color(t)}>{style.glyph} </Text>
          {summary ? (
            <Text color={node.status === 'pending' ? t.color.muted : t.color.text}>
              {node.subagent}: {summary}
            </Text>
          ) : (
            // Neither a summary nor a template reached the client, so the id is all this row has.
            <Text color={node.status === 'pending' ? t.color.muted : t.color.text}>{node.id}</Text>
          )}
          {parens && (
            <Text color={t.color.muted} dim>
              {' '}
              {parens}
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
  // then carried by the rows alone, which `dagNodeNames` handles by naming every
  // dependency again.
  const picture = layoutDagGraph(run.nodes, { width })

  return (
    <Box flexDirection="column" marginBottom={1}>
      <Text color={t.color.muted}>
        <Text bold color={t.color.text}>
          {run.runId}
        </Text>{' '}
        <Text color={t.color.statusFg} dim>
          {dagRunHeadline(run)}
        </Text>
      </Text>

      {picture && <DagPicture nodes={run.nodes} picture={picture} runId={run.runId} t={t} />}

      {run.nodes.map((node, index) => (
        <NodeRow
          drawn={picture?.drawn ?? null}
          ordinal={index + 1}
          key={node.id}
          node={node}
          open={openNodes.has(dagNodeKey(run.runId, node.id))}
          runId={run.runId}
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
