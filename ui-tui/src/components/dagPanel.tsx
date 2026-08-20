// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// One `run_subagent_dag` run drawn as dependency levels.
//
// A terminal cannot route edges the way the web UI's canvas can, so the topology
// is carried by two things instead: nodes that may run together share a level,
// and every node names the dependencies it waits on. That is legible at any
// graph size and needs no box-drawing beyond the level gutter.
//
// A row reads as what the node was asked, not as its id: the id is generated,
// is the widest thing on the row, and answers a question nobody scanning a
// running graph is asking. Clicking the row expands the prompt in full, with the
// id above it -- which is where a reader who wants `/dag <node>` will look.

import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { memo } from 'react'

import type { DagRunNode, DagRunState } from '../domain/dagRun.js'
import type { Theme } from '../theme.js'

import { layoutDag } from '../lib/dagLayout.js'
import { $dagOpenNodes, dagNodeKey, toggleDagNode } from '../lib/dagOpenNodes.js'
import { DAG_STATUS_GLYPH, dagNodeNames, dagNodeSummary, dagRunHeadline } from '../lib/dagStatus.js'

// The level gutter, and the indent the expanded prompt sits at under its row.
const GUTTER = 4
const PROMPT_INDENT = 2

// Bounds what one expanded row can push into the transcript. This block shows
// the template as authored, where a `{{ ref:<path> }}` is thirty-odd literal
// characters -- it only becomes the file's contents when the runner renders it
// -- so reaching this cap takes a genuinely long instruction block rather than
// an injected file. `/dag <node>` pages the rendered text in full.
const PROMPT_CHARS = 4000

const NodePrompt = ({ node, t, width }: { node: DagRunNode; t: Theme; width: number }) => {
  const prompt = node.promptTemplate ?? ''

  return (
    <Box flexDirection="column" paddingLeft={PROMPT_INDENT} width={Math.max(8, width - PROMPT_INDENT)}>
      <Text color={t.color.muted} dim>
        {node.id}
        {node.outputFile ? ` → ${node.outputFile}` : ''}
      </Text>
      <Text color={t.color.text} wrap="wrap">
        {prompt.length > PROMPT_CHARS ? `${prompt.slice(0, PROMPT_CHARS)}\n…` : prompt}
      </Text>
    </Box>
  )
}

const NodeRow = ({
  node,
  open,
  runId,
  t,
  width
}: {
  node: DagRunNode
  open: boolean
  runId: string
  t: Theme
  width: number
}) => {
  const style = DAG_STATUS_GLYPH[node.status]
  const { deps, parens } = dagNodeNames(node)
  // What the parts that must share the summary's line leave it. The dependency
  // list is not among them: it is the one part that wraps without losing
  // anything, so billing the summary for it would clip the words a reader is
  // here for and still wrap. Floored so an outsized handle cannot squeeze the
  // summary to nothing.
  const room = Math.max(24, width - style.glyph.length - 1 - node.subagent.length - 2 - parens.length - 1)
  const summary = dagNodeSummary(node.promptTemplate, room)

  // A row with no template has nothing to expand to -- and it is already showing
  // its node id, so nothing is hidden either. Leaving it unclickable beats a
  // dead affordance that swallows the click.
  const expandable = Boolean(node.promptTemplate)

  return (
    <Box flexDirection="column">
      <Box
        // On the Box, not the Text: only Box carries mouse props in this fork.
        // The click has to stop here -- dispatchClick bubbles through every
        // ancestor handler, and the transcript rows above this one toggle on it.
        onClick={
          expandable
            ? (event: { stopImmediatePropagation?: () => void }) => {
                event.stopImmediatePropagation?.()
                toggleDagNode(dagNodeKey(runId, node.id))
              }
            : undefined
        }
      >
        <Text color={t.color.muted}>
          <Text color={style.color(t)}>{style.glyph} </Text>
          {summary ? (
            <Text color={node.status === 'pending' ? t.color.muted : t.color.text}>
              {node.subagent}: {summary}
            </Text>
          ) : (
            // No template reached the client, so the id is all this row has.
            <Text color={node.status === 'pending' ? t.color.muted : t.color.text}>{node.id}</Text>
          )}
          <Text color={t.color.muted} dim>
            {' '}
            {parens}
          </Text>
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

      {open && expandable && <NodePrompt node={node} t={t} width={width} />}
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
  const levels = layoutDag(run.nodes)

  if (levels.length === 0) {
    return null
  }

  const room = Math.max(24, width - GUTTER)

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

      {levels.map((level, index) => (
        <Box flexDirection="row" key={index}>
          <Box flexDirection="column" width={GUTTER}>
            <Text color={t.color.border} dim>
              {` L${index + 1} `}
            </Text>
          </Box>
          <Box flexDirection="column">
            {level.map(node => (
              <NodeRow
                key={node.id}
                node={node}
                open={openNodes.has(dagNodeKey(run.runId, node.id))}
                runId={run.runId}
                t={t}
                width={room}
              />
            ))}
          </Box>
        </Box>
      ))}

      {run.done && run.dir && (
        <Text color={t.color.muted} dim>
          {`  outputs in ${run.dir}`}
        </Text>
      )}
    </Box>
  )
})
