// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// `/dag` — the user-initiated half of the DAG surface.
//
// The live `dag.*` events cover a run that behaves. Two things they cannot do:
//
//   * repair a graph whose frames were lost. Nothing replays them, so a gateway
//     that restarted mid-run leaves nodes pinned to `running` forever. `dag.get`
//     re-reads the run dir, where the instance registry supplies the progress
//     the events did not deliver.
//   * show a node's rendered prompt or its output. No event carries either: the
//     manifest inlines only the leaf nodes' text, and the tool result kept in
//     the transcript is clamped to 200 chars.
//   * name the nodes. `run_subagent_dag`'s own call label elides past the third
//     id, and a graph row carries its id only in the block a click opens, so on
//     a graph of any size the refresh listing is the only way to learn the id
//     this command takes as its argument.
//
// Both are user-initiated for a reason: the terminal event handlers are
// synchronous (the turn commits its transcript row inside them), so an RPC
// round-trip cannot be awaited there without reordering the event stream.

import type { DagGetResult, DagNodeResult } from '../../../rpc/index.js'
import type { SlashCommand } from '../types.js'

import { dagRunHeadline, formatDagNodeDetail } from '../../../lib/dagStatus.js'
import { turnController } from '../../turnController.js'
import { getTurnState } from '../../turnStore.js'

// The file itself is uncapped and can be megabytes. This is a transcript, not a
// pager, so ask for a slice that stays readable and say when it was cut.
const NODE_OUTPUT_CHARS = 4000

export const dagCommands: SlashCommand[] = [
  {
    help: "refresh this turn's sub-agent DAG graphs, or show one node's prompt/output",
    name: 'dag',
    run: (arg, ctx) => {
      const { gateway, transcript, ui } = ctx
      const runs = getTurnState().dagRuns
      const node = arg.trim()

      if (runs.length === 0) {
        transcript.sys('no DAG run in this turn')

        return
      }

      if (!node) {
        runs.forEach(run => {
          gateway
            .rpc<DagGetResult>('dag.get', { run_id: run.runId, session_key: ui.sid })
            .then(
              ctx.guarded<DagGetResult>(result => {
                turnController.applyDagSnapshot(result.run)
                // Read the tally back off the store rather than the response, so
                // the line cannot disagree with the graph that just re-rendered.
                const refreshed = getTurnState().dagRuns.find(item => item.runId === run.runId)
                transcript.sys(`${run.runId}: ${refreshed ? dagRunHeadline(refreshed) : 'refreshed'}`)

                // The ids, because this command's own argument is one and there
                // is no other keyboard route to them: the tool row's label
                // elides past the third, and a graph row shows its id only once
                // expanded, which takes a mouse.
                if (refreshed && refreshed.nodes.length > 0) {
                  transcript.sys(`  nodes: ${refreshed.nodes.map(item => item.id).join(', ')}`)
                }
              })
            )
            .catch((err: unknown) => {
              // Leave the graph as it was: a run dir that was cleaned up is not
              // a reason to blank what the user can still see.
              if (!ctx.stale()) {
                transcript.sys(`dag refresh failed for ${run.runId}: ${String(err)}`)
              }
            })
        })

        return
      }

      // A node is addressed by id alone, so it has to be found in a graph. The
      // most recent run wins when several are open -- the same one whose live
      // panel is on screen.
      const owner = [...runs].reverse().find(run => run.nodes.some(item => item.id === node))

      if (!owner) {
        transcript.sys(`no node '${node}' in this turn's DAG runs`)

        return
      }

      gateway
        .rpc<DagNodeResult>('dag.node', {
          max_output_chars: NODE_OUTPUT_CHARS,
          node,
          run_id: owner.runId,
          session_key: ui.sid
        })
        .then(
          ctx.guarded<DagNodeResult>(result => {
            transcript.page(formatDagNodeDetail(result.node), `${node} @ ${owner.runId}`)
          })
        )
        .catch((err: unknown) => {
          if (!ctx.stale()) {
            transcript.sys(`dag node read failed for ${node}: ${String(err)}`)
          }
        })
    },
    usage: '/dag [node]'
  }
]
