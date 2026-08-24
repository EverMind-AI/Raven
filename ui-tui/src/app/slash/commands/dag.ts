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
//     id, and a graph row carries its id only in the block a click opens. The
//     graph prints a short ordinal in every box and at the head of every row and
//     this command takes one, so the ids are reachable without a mouse; the
//     refresh listing prints the ordinal-to-id mapping for anyone who wants it.
//
// Both are user-initiated for a reason: the terminal event handlers are
// synchronous (the turn commits its transcript row inside them), so an RPC
// round-trip cannot be awaited there without reordering the event stream.

import type { DagRunState } from '../../../domain/dagRun.js'
import type { DagGetResult, DagNodeResult, DagRunSnapshot } from '../../../rpc/index.js'
import type { Msg } from '../../../types.js'
import type { SlashCommand } from '../types.js'

import { dagRunsFromHistory, foldDagSnapshot } from '../../../domain/dagRun.js'
import { dagRunHeadline, formatDagNodeDetail } from '../../../lib/dagStatus.js'
import { turnController } from '../../turnController.js'
import { getTurnState } from '../../turnStore.js'

// The file itself is uncapped and can be megabytes. This is a transcript, not a
// pager, so ask for a slice that stays readable and say when it was cut.
const NODE_OUTPUT_CHARS = 4000

/**
 * Every run this `/dag` call should offer to refresh.
 *
 * A run only reaches `history` once its turn commits, so a still-running
 * turn's graph exists solely in `live` until then; keeping it out here would
 * make a mid-turn `/dag` claim there is nothing to refresh. Once a run does
 * reach history, `live` may repeat it -- genuinely mid-turn, or appended
 * there by this same command's own past call on a resumed session (see
 * `applyDagSnapshot`) -- so history's copy wins on a duplicate: it is what
 * the visible transcript actually renders.
 */
const dagRunsToRefresh = (history: DagRunState[], live: DagRunState[]): DagRunState[] => {
  const seen = new Set(history.map(run => run.runId))

  return [...history, ...live.filter(run => !seen.has(run.runId))]
}

/**
 * Immutably fold a refreshed snapshot into the history row that carries its
 * run, replacing only what changed.
 *
 * `applyDagSnapshot` only reaches `turnController`'s live episodes, which a
 * resumed session has none of (see `dagRunsFromHistory`) -- the graph a
 * resumed transcript renders lives on `tool.dag` in history instead, and a
 * mutation there would not re-render: `MessageLine`/`EpisodeMessage` are
 * memoized on the `msg` prop's identity, so a fresh reference has to reach all
 * the way from the top-level array down to the one tool that changed. Every
 * message, episode and tool the refresh does not touch keeps its existing
 * reference, which is what lets the rest of the transcript skip re-rendering.
 */
const withRefreshedDagRun = (history: Msg[], snapshot: DagRunSnapshot): Msg[] => {
  let historyChanged = false

  const next = history.map(msg => {
    if (!msg.episodes) {
      return msg
    }

    let episodesChanged = false

    const episodes = msg.episodes.map(episode => {
      let toolsChanged = false

      const tools = episode.tools.map(tool => {
        if (tool.dag?.runId !== snapshot.run_id) {
          return tool
        }

        toolsChanged = true

        return { ...tool, dag: foldDagSnapshot(tool.dag, snapshot) }
      })

      if (!toolsChanged) {
        return episode
      }

      episodesChanged = true

      return { ...episode, tools }
    })

    if (!episodesChanged) {
      return msg
    }

    historyChanged = true

    return { ...msg, episodes }
  })

  return historyChanged ? next : history
}

export const dagCommands: SlashCommand[] = [
  {
    help: "refresh this session's sub-agent DAG graphs, or show one node's prompt/output",
    name: 'dag',
    run: (arg, ctx) => {
      const { gateway, local, transcript, ui } = ctx
      const runs = dagRunsToRefresh(dagRunsFromHistory(local.getHistoryItems()), getTurnState().dagRuns)
      const node = arg.trim()

      if (runs.length === 0) {
        transcript.sys('no DAG run in this session')

        return
      }

      if (!node) {
        runs.forEach(run => {
          gateway
            .rpc<DagGetResult>('dag.get', { run_id: run.runId, session_key: ui.sid })
            .then(
              ctx.guarded<DagGetResult>(result => {
                turnController.applyDagSnapshot(result.run)

                // applyDagSnapshot only ever reaches a live episode, which a
                // resumed session (or a turn that has since ended) has none
                // of -- the graph such a transcript renders lives on the
                // history row's tool instead, and only this reaches that
                // one. Unconditional because a mid-turn run has no row in
                // history yet: withRefreshedDagRun returns the original
                // reference when no row carries the run, so this is free
                // when it does not apply, and is what keeps a resumed
                // session's next /dag refreshing instead of going silent.
                transcript.setHistoryItems(prev => withRefreshedDagRun(prev, result.run))

                // Read the tally back off the store rather than the response, so
                // the line cannot disagree with the graph that just re-rendered.
                const refreshed = getTurnState().dagRuns.find(item => item.runId === run.runId)
                transcript.sys(`${run.runId}: ${refreshed ? dagRunHeadline(refreshed) : 'refreshed'}`)

                // Numbered, because the ordinal is what the graph shows and the
                // id is what `dag.node` takes: this line is the only place the
                // two are printed side by side.
                if (refreshed && refreshed.nodes.length > 0) {
                  const listed = refreshed.nodes.map((item, index) => `${index + 1} ${item.id}`)

                  transcript.sys(`  nodes: ${listed.join(', ')}`)
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

      // An id has to be found in a graph. The most recent run wins when several
      // are open -- the same one whose live panel is on screen.
      const byId = [...runs].reverse().find(item => item.nodes.some(entry => entry.id === node))
      // Failing that, a bare number is the ordinal the graph prints in each box
      // and at the head of each row. Tried second, so a node whose id happens to
      // be a number still resolves as an id; and only against the latest run,
      // because that is the graph whose ordinals the user is reading.
      const latest = runs[runs.length - 1]
      const byOrdinal = byId || !/^\d+$/.test(node) ? undefined : latest?.nodes[Number(node) - 1]
      const owner = byId ?? (byOrdinal ? latest : undefined)
      const target = byId ? node : byOrdinal?.id

      if (!owner || !target) {
        transcript.sys(`no node '${node}' in this turn's DAG runs`)

        return
      }

      gateway
        .rpc<DagNodeResult>('dag.node', {
          max_output_chars: NODE_OUTPUT_CHARS,
          node: target,
          run_id: owner.runId,
          session_key: ui.sid
        })
        .then(
          ctx.guarded<DagNodeResult>(result => {
            transcript.page(formatDagNodeDetail(result.node), `${target} @ ${owner.runId}`)
          })
        )
        .catch((err: unknown) => {
          if (!ctx.stale()) {
            transcript.sys(`dag node read failed for ${target}: ${String(err)}`)
          }
        })
    },
    usage: '/dag [node|ordinal]'
  }
]
