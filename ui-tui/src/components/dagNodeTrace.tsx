// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The slot under one DAG node's row: nothing, a live line, or a box.
//
// Three states of one place rather than three places, because the second click
// has to read as "go back" -- a box that appeared *beside* the line it replaced
// would leave the reader looking for what changed.
//
// The box is a constant height, and both halves of that are deliberate: its
// footer is drawn whether or not anything was cut, and a short trace is
// blank-padded. A box that grew as the run produced messages would shove every
// row beneath it several times a second, and the height model could then not
// state a panel's height without folding the trace first.
//
// StreamLine and TraceBox are separate components rather than two branches of
// one, because a hook cannot be conditional: folding and measuring the trace
// has to live on a component that only mounts once the box is actually shown,
// or a collapsed running node would pay for a measurement it never draws.

import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { memo, useMemo } from 'react'

import type { DagRunNode } from '../domain/dagRun.js'
import type { TranscriptMessage } from '../rpc/index.js'
import type { Theme } from '../theme.js'

import { $dagNodeTraces } from '../app/dagNodeStore.js'
import { DAG_TRACE_BOX_ROWS, DAG_TRACE_ROWS } from '../config/limits.js'
import { dagNodeKey } from '../lib/dagOpenNodes.js'
import { dagNodeHandle } from '../lib/dagStatus.js'
import { dagStreamTail, fitTraceTail } from '../lib/dagStream.js'
import { MessageLine } from './messageLine.js'
import { Spinner } from './thinking.js'

const INDENT = 2

// The spinner and the space after it.
const SPINNER_CELLS = 2

// Bounds the fallback block. The template is shown as authored, where a
// `{{ ref:<path> }}` is thirty-odd literal characters rather than the file, so
// reaching this takes a genuinely long instruction block.
const PROMPT_CHARS = 4000

/** What the node was asked, for a node whose trace could not be read. */
const NodePrompt = ({ node, t, width }: { node: DagRunNode; t: Theme; width: number }) => {
  const prompt = node.promptTemplate ?? ''

  return (
    <Box flexDirection="column" width={Math.max(8, width)}>
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

// No `wrap="truncate-end"` here, deliberately: the Spinner is a nested <Text>,
// and a nested Text makes ink's own truncation a no-op (see the note on
// `clipToWidth`, text.ts:63). `dagStreamTail` has already cut to `room`, which
// is the actual guarantee that this stays one row.
const StreamLine = ({
  messages,
  t,
  width
}: {
  messages?: readonly TranscriptMessage[]
  t: Theme
  width: number
}) => {
  const room = Math.max(8, width - INDENT - SPINNER_CELLS)
  const tail = useMemo(() => (messages ? dagStreamTail(messages, room) : ''), [messages, room])

  return (
    <Box paddingLeft={INDENT} width={Math.max(8, width)}>
      <Text color={t.color.muted}>
        <Spinner color={t.color.accent} variant="tool" /> {tail || 'working…'}
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
  const fit = useMemo(() => fitTraceTail(messages ?? [], DAG_TRACE_ROWS, inner), [inner, messages])

  return (
    <Box
      borderColor={t.color.border}
      borderStyle="round"
      flexDirection="column"
      height={DAG_TRACE_BOX_ROWS}
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
        <Box flexDirection="column" flexGrow={1}>
          {fit.shown.map((msg, index) => (
            <MessageLine cols={inner} key={index} msg={msg} t={t} />
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
    return node.status === 'running' ? <StreamLine messages={messages} t={t} width={width} /> : null
  }

  return <TraceBox messages={messages} node={node} ordinal={ordinal} t={t} width={width} />
})
