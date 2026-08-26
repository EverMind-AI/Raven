// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The slot under one DAG node's row: nothing, or the box the row opens into.
//
// A running node used to keep a live tail here, one line of whatever it was
// producing. It cost the panel a row per running node and moved a few times a
// second, which is what a graph of three nodes reads as at a glance: churn. The
// trace it was a window on is a click away, in the box, and that click is what
// says the reader wants to watch one node rather than the shape.
//
// The box is a constant height, and both halves of that are deliberate: its
// footer is drawn whether or not anything was cut, and a short trace is
// blank-padded. A box that grew as the run produced messages would shove every
// row beneath it several times a second, and the height model could then not
// state a panel's height without folding the trace first.

import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { memo, useMemo } from 'react'

import type { DagRunNode } from '../domain/dagRun.js'
import type { TranscriptMessage } from '../rpc/index.js'
import type { Theme } from '../theme.js'

import { $dagNodeTraces } from '../app/dagNodeStore.js'
import { dagNodeKey } from '../lib/dagOpenNodes.js'
import { dagNodeHandle, dagNodeSummary, dagTraceBoxRows, dagTraceRows } from '../lib/dagStatus.js'
import { fitTraceTail } from '../lib/dagStream.js'
import { MessageLine } from './messageLine.js'

const INDENT = 2

// Bounds the fallback block. The template is shown as authored, where a
// `{{ ref:<path> }}` is thirty-odd literal characters rather than the file, so
// reaching this takes a genuinely long instruction block.
const PROMPT_CHARS = 4000

/** What the node was asked, for a node whose trace could not be read. */
const NodePrompt = ({ node, t, width }: { node: DagRunNode; t: Theme; width: number }) => {
  const prompt = node.promptTemplate ?? ''
  // The line the node was dispatched with. It used to lead the node's row; the
  // row now leads with the id, so this is where it lives -- and it is the only
  // thing a node dispatched without a template has to show at all.
  const summary = dagNodeSummary(node.nodeSummary, undefined, Math.max(8, width))

  return (
    <Box flexDirection="column" width={Math.max(8, width)}>
      <Text color={t.color.muted} dim>
        {node.id}
        {node.outputFile ? ` → ${node.outputFile}` : ''}
      </Text>
      {summary && (
        <Text color={t.color.muted} wrap="truncate-end">
          {summary}
        </Text>
      )}
      <Text color={t.color.text} wrap="wrap">
        {prompt.length > PROMPT_CHARS ? `${prompt.slice(0, PROMPT_CHARS)}\n…` : prompt}
      </Text>
    </Box>
  )
}

/** The bordered trace box for an expanded node -- the only thing that folds and measures the trace. */
const TraceBox = ({
  messages,
  node,
  ordinal,
  t,
  width
}: {
  messages?: readonly TranscriptMessage[]
  node: DagRunNode
  ordinal: number
  t: Theme
  width: number
}) => {
  // Borders take a column each side, and the box is indented from the row.
  const inner = Math.max(24, width - INDENT - 2)
  // A finished node's box is the taller one: it will never redraw again, and its
  // height is the only thing deciding how much of the trace is readable here.
  const rows = dagTraceRows(node.status)
  const fit = useMemo(() => fitTraceTail(messages ?? [], rows, inner), [inner, messages, rows])

  return (
    <Box
      borderColor={t.color.border}
      borderStyle="round"
      flexDirection="column"
      height={dagTraceBoxRows(node.status)}
      marginLeft={INDENT}
      overflow="hidden"
      width={Math.max(28, width - INDENT)}
    >
      {/* The handle rather than the bare agent when the node has one: this box is
          the expanded block `dagNodeHandle` is written for, and since the rows
          carry only a short tag of a *shared* handle, it is the only place the
          full `agent@instance` a reader needs for `/dag` exists. */}
      <Text color={t.color.muted} dim wrap="truncate-end">
        {node.id} · {dagNodeHandle(node) || node.subagent}
        {fit.shown.length > 0 ? ` · ${messages?.length ?? 0} msgs` : ''}
        {node.outputFile ? ` → ${node.outputFile}` : ''}
      </Text>

      {fit.shown.length > 0 ? (
        <Box flexDirection="column" height={rows} overflow="hidden">
          {/* `hidden` offsets the key out of the window, so a row keeps its
              instance as the tail slides instead of inheriting the previous
              row's state (see the same note in `agentsOverlay`). */}
          {fit.shown.map((msg, index) => (
            <MessageLine cols={inner} key={fit.hidden + index} msg={msg} t={t} />
          ))}
        </Box>
      ) : (
        <Box flexDirection="column" flexGrow={1}>
          <NodePrompt node={node} t={t} width={inner} />
        </Box>
      )}

      <Text color={t.color.muted} dim wrap="truncate-end">
        {fit.hidden > 0 ? `↑ ${fit.hidden} earlier messages — /dag ${ordinal}` : `/dag ${ordinal} for the full trace`}
      </Text>
    </Box>
  )
}

export const DagNodeSlot = memo(function DagNodeSlot({
  node,
  ordinal,
  open,
  runId,
  t,
  width
}: {
  node: DagRunNode
  ordinal: number
  open: boolean
  runId: string
  t: Theme
  width: number
}) {
  const traces = useStore($dagNodeTraces)
  const messages = traces.get(dagNodeKey(runId, node.id))?.messages

  if (!open) {
    return null
  }

  return <TraceBox messages={messages} node={node} ordinal={ordinal} t={t} width={width} />
})
