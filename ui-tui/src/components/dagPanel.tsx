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

import { Box, Text } from '@hermes/ink'
import { memo } from 'react'

import type { DagRunNode, DagRunState } from '../domain/dagRun.js'
import type { Theme } from '../theme.js'

import { layoutDag } from '../lib/dagLayout.js'
import { DAG_STATUS_GLYPH, dagRunHeadline } from '../lib/dagStatus.js'

const NodeRow = ({ node, t }: { node: DagRunNode; t: Theme }) => {
  const style = DAG_STATUS_GLYPH[node.status]

  return (
    <Text color={t.color.muted}>
      <Text color={style.color(t)}>{style.glyph} </Text>
      <Text color={node.status === 'pending' ? t.color.muted : t.color.text}>{node.id}</Text>
      <Text color={t.color.muted} dim>
        {' '}
        ({node.subagent}
        {node.instance ? `@${node.instance}` : ''})
      </Text>
      {node.dependsOn.length > 0 && (
        <Text color={t.color.muted} dim>
          {' '}
          ← {node.dependsOn.join(', ')}
        </Text>
      )}
      {node.error && (
        <Text color={t.color.error}>
          {' — '}
          {node.error}
        </Text>
      )}
    </Text>
  )
}

export const DagPanel = memo(function DagPanel({ run, t }: { run: DagRunState; t: Theme }) {
  const levels = layoutDag(run.nodes)

  if (levels.length === 0) {
    return null
  }

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
          <Box flexDirection="column" width={4}>
            <Text color={t.color.border} dim>
              {` L${index + 1} `}
            </Text>
          </Box>
          <Box flexDirection="column">
            {level.map(node => (
              <NodeRow key={node.id} node={node} t={t} />
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
