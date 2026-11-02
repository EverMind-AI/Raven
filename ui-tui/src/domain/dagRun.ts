// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// One `run_subagent_dag` call's graph, folded from its progress events.
//
// Kept pure and free of any store/Ink import so the state machine is testable
// on its own: the events arrive out of a socket in real time and a wrong fold
// shows up as a graph that quietly stops updating, which a render test cannot
// distinguish from a graph that is simply idle.

import type {
  DagNodeStatus,
  DagNodeUpdatedEvent,
  DagNotExecutedReason,
  DagRunCompletedEvent,
  DagRunReplannedEvent,
  DagRunSnapshot,
  DagRunStartedEvent
} from '../rpc/index.js'
import type { Msg } from '../types.js'

/** Any of the four progress events a DAG run emits. */
export type DagEvent = DagNodeUpdatedEvent | DagRunCompletedEvent | DagRunReplannedEvent | DagRunStartedEvent

/** The wire vocabulary plus `interrupted`, which the client infers rather than
 * receives: the runner only ever reports the five terminal states, so a node
 * still called `running` when the run closed lost its terminal frame. */
export type DagRunNodeStatus = DagNodeStatus | 'interrupted'

export interface DagRunNode {
  id: string
  subagent: string
  dependsOn: string[]
  /** Wire clock, in ms, as the runner stamped it. Absent until the node starts,
   * and on a run whose dir predates the fields. */
  startedAt?: number
  endedAt?: number
  /** Normalised, validated attempt history: the agent of each attempt, in order.
   * Absent -- not empty -- on a node no reassignment touched, so presence is
   * what says the history exists. */
  attemptAgents?: string[]
  /** Shared stateful handle; nodes naming the same one ran sequentially. */
  instance?: string
  status: DagRunNodeStatus
  /** The prompt as submitted, which names what the node was asked. Absent when
   * neither the call args nor the run dir supplied one. */
  promptTemplate?: string
  /** One line, for the user, on what this node was asked to do. Absent on a run
   * that predates the field. */
  nodeSummary?: string
  outputFile?: string
  error?: string
  /** Why a branch left this node out. Only on a `not_executed` node, and absent
   * when the frame or the run dir that said so did not say why. */
  reason?: DagNotExecutedReason
  /** What a decision node selected. `[]` is a decision that chose nothing, which
   * is not the same as `undefined`, a decision not made (or not made yet). */
  selectedRoutes?: string[]
  /** The judge's quote supporting the selection. */
  routeEvidence?: string
  /** The route names a decision node chooses between, in declaration order. */
  routes?: string[]
  /** How many routes the decision selects, `one` where the graph left it unset. */
  select?: 'many' | 'one'
  /** The guard: this node runs only if it holds once its dependencies settle. */
  when?: string
}

export interface DagRunSummary {
  total?: number
  completed?: number
  failed?: number
  skipped?: number
  cancelled?: number
  not_executed?: number
}

export interface DagRunState {
  runId: string
  /** The call this run belongs to, when the host correlates the two. */
  toolCallId?: string
  /** The multi-round run this graph is one round of. Absent on an ordinary
   * graph, which is every graph a tool call dispatched. A stint submits one
   * graph a round into the same conversation, so without this the delegation
   * view lists thirty unrelated runs and the reader picks the live one out of
   * them by reading summaries. */
  stintId?: string
  /** Which round of that run this graph is, counting from one. */
  roundIndex?: number
  nodes: DagRunNode[]
  done: boolean
  /** Where the run wrote its per-node outputs; known once it completes. */
  dir?: string
  summary?: DagRunSummary
  /** The run that replaced this one, when the agent replanned it. Arrives the
   * moment the desk accepts the decision, before this run's own `done` --
   * the successor is named a moment before it exists. */
  replannedInto?: string
}

const fromStart = (payload: DagRunStartedEvent['payload'], promptTemplates?: Record<string, string>): DagRunState => ({
  runId: payload.run_id,
  ...(payload.tool_call_id ? { toolCallId: payload.tool_call_id } : {}),
  ...(payload.stint_id ? { stintId: payload.stint_id } : {}),
  ...(payload.round_index ? { roundIndex: payload.round_index } : {}),
  done: false,
  nodes: payload.nodes.map(node => ({
    id: node.id,
    subagent: node.subagent,
    dependsOn: [...node.depends_on],
    ...(node.instance ? { instance: node.instance } : {}),
    ...(promptTemplates?.[node.id] ? { promptTemplate: promptTemplates[node.id] } : {}),
    ...(node.node_summary ? { nodeSummary: node.node_summary } : {}),
    ...(node.routes !== undefined ? { routes: [...node.routes] } : {}),
    ...(node.select !== undefined ? { select: node.select } : {}),
    ...(node.when !== undefined ? { when: node.when } : {}),
    status: 'pending' as const
  }))
})

// What a frame or a manifest entry says a branch did to a node, copied by
// presence and never by truthiness: `selected_routes: []` is a decision that
// chose nothing, and dropping it would read as a decision never made. A frame
// that says none of it adds none of it, so an earlier frame's word stands.
const outcomeOf = (source: {
  reason?: DagNotExecutedReason
  route_evidence?: string
  selected_routes?: string[]
}): Pick<DagRunNode, 'reason' | 'routeEvidence' | 'selectedRoutes'> => ({
  ...(source.reason !== undefined ? { reason: source.reason } : {}),
  ...(source.selected_routes !== undefined ? { selectedRoutes: [...source.selected_routes] } : {}),
  ...(source.route_evidence !== undefined ? { routeEvidence: source.route_evidence } : {})
})

/** A frame's or a manifest row's attempt fields, normalised. */
interface AttemptReport {
  history?: string[]
  instance?: string
  instanceGiven: boolean
  subagent?: string
}

/** The attempt history a frame or a snapshot row carries, validated.
 *
 * Absent and malformed are the same answer: `undefined`. A half-valid list
 * would silently drop or misplace an attempt, which is the one thing a history
 * exists to prevent -- so a node keeps whatever history it already had. */
const attemptAgentsOf = (value: unknown): string[] | undefined =>
  Array.isArray(value) && value.every(agent => typeof agent === 'string' && agent) ? [...value] : undefined

const attemptReportOf = (source: {
  attempt_agents?: string[] | null
  instance?: string | null
  subagent?: string | null
}): AttemptReport => ({
  history: attemptAgentsOf(source.attempt_agents),
  instance: source.instance ?? undefined,
  // Presence, not value: a present null clears the handle a stateless retry
  // left behind, while an absent key says this frame does not speak for it.
  instanceGiven: Object.hasOwn(source, 'instance'),
  subagent: source.subagent ?? undefined
})

/**
 * Fold one report of a reassigned node's attempt onto the node it is about.
 *
 * Identity, status and timing belong to one attempt, so they move together. A
 * report that cannot claim a newer attempt than the node holds -- it names none,
 * or fewer -- is from an attempt the node has moved past: the frames of the
 * attempt that gave up can still be in flight when the next one starts. `null`
 * answers that case, and the caller keeps its node.
 *
 * A newer attempt's start and end are its own, so a report carrying neither
 * clears what the previous attempt left rather than dating the new one with it.
 * Within one attempt the established policy stands: a frame without a timestamp
 * does not erase the one an earlier frame reported.
 */
const foldAttemptReport = (
  node: DagRunNode,
  report: AttemptReport,
  status: DagRunNodeStatus,
  times: { endedAt?: number; startedAt?: number }
): DagRunNode | null => {
  const held = node.attemptAgents

  if (held !== undefined && (report.history === undefined || report.history.length < held.length)) {
    return null
  }

  const newer = report.history !== undefined && (held === undefined || report.history.length > held.length)
  const next: DagRunNode = { ...node, status }

  if (newer) {
    delete next.startedAt
    delete next.endedAt
  }

  if (times.startedAt !== undefined) {
    next.startedAt = times.startedAt
  }

  if (times.endedAt !== undefined) {
    next.endedAt = times.endedAt
  }

  if (report.subagent !== undefined) {
    next.subagent = report.subagent
  }

  if (report.instanceGiven) {
    if (report.instance !== undefined) {
      next.instance = report.instance
    } else {
      delete next.instance
    }
  }

  if (report.history !== undefined) {
    next.attemptAgents = report.history
  }

  return next
}

const withNodeStatus = (
  run: DagRunState,
  id: string,
  status: DagRunNodeStatus,
  times: { endedAt?: number; startedAt?: number } = {},
  outcome: Pick<DagRunNode, 'reason' | 'routeEvidence' | 'selectedRoutes'> = {},
  report: AttemptReport = attemptReportOf({})
): DagRunState => {
  let changed = false

  const nodes = run.nodes.map(node => {
    if (node.id !== id) {
      return node
    }

    const folded = foldAttemptReport(node, report, status, times)

    if (!folded) {
      return node
    }

    changed = true

    return { ...folded, ...outcome }
  })

  return changed ? { ...run, nodes } : run
}

const fromCompletion = (run: DagRunState, payload: DagRunCompletedEvent['payload']): DagRunState => {
  const byNode = new Map(payload.files.map(file => [file.node, file]))

  return {
    ...run,
    done: true,
    dir: payload.dir,
    summary: { ...payload.summary },
    nodes: run.nodes.map(node => {
      const file = byNode.get(node.id)

      // The manifest is authoritative for a node it names; one it does not name
      // keeps whatever the last update said, except that `running` can no longer
      // be true of a run that has closed. A row of an attempt the node has
      // already moved past is a third case -- one this client missed the
      // reassignment of -- and leaving it out whole is what keeps its output and
      // failure text from being attributed to an attempt it never described.
      const folded = file ? foldAttemptReport(node, attemptReportOf(file), file.status, {}) : null

      if (file && !folded) {
        return node
      }

      return {
        ...(folded ?? { ...node, status: node.status === 'running' ? 'interrupted' : node.status }),
        ...(file?.output_file ? { outputFile: file.output_file } : {}),
        ...(file?.error ? { error: file.error } : {}),
        ...(file ? outcomeOf(file) : {})
      }
    })
  }
}

/**
 * Attach prompts to a run whose nodes were built without them.
 *
 * `dag.run_started` travels on the DAG tool's own progress sink and the
 * `tool.start` carrying the prompts goes through the delivery hub, so either
 * can reach the client first -- the margin measured on a real run was 2ms.
 * `fromStart` covers the order where the prompts are already known; this covers
 * the other one. Without it that margin silently decides whether any row of the
 * graph can be expanded, and nothing later puts the prompts back.
 *
 * Returns `run` itself when it adds nothing: the store publishes by identity, so
 * a fresh object here would re-render every graph on every call. A node that
 * already has a template keeps it -- a `dag.get` snapshot read the run dir,
 * which is a truer source than the arguments the model sent.
 */
export const withPromptTemplates = (run: DagRunState, templates: Record<string, string>): DagRunState => {
  let changed = false

  const nodes = run.nodes.map(node => {
    const template = templates[node.id]

    if (!template || node.promptTemplate) {
      return node
    }

    changed = true

    return { ...node, promptTemplate: template }
  })

  return changed ? { ...run, nodes } : run
}

/**
 * Fold one progress event into the run it belongs to.
 *
 * Returns a new state, never a mutation of `prev` — the store publishes by
 * identity, so folding in place would not re-render.
 *
 * `null` in means no run is open yet: only `dag.run_started` can open one, so an
 * update that arrives first (a subscription attached mid-run) is dropped rather
 * than used to invent a node, which would draw a graph that never gains its
 * edges. An event naming a different run leaves `prev` untouched.
 *
 * `promptTemplates` (node id -> prompt as submitted) comes from the tool call's
 * own arguments, since no event carries it. Only `dag.run_started` builds nodes,
 * so it is read there and ignored on the rest.
 */
export const foldDagEvent = (
  prev: DagRunState | null,
  event: DagEvent,
  promptTemplates?: Record<string, string>
): DagRunState | null => {
  if (event.type === 'dag.run_started') {
    return fromStart(event.payload, promptTemplates)
  }

  if (prev === null || prev.runId !== event.payload.run_id) {
    return prev
  }

  // Arrives the moment the desk accepts the decision -- before this run's
  // own `dag.run_completed`, and before the successor even starts -- so this
  // must leave `done` and the nodes alone rather than treat it as the run ending.
  if (event.type === 'dag.run_replanned') {
    return { ...prev, replannedInto: event.payload.replan_run_id }
  }

  if (event.type === 'dag.run_completed') {
    return fromCompletion(prev, event.payload)
  }

  return prev.nodes.some(node => node.id === event.payload.node)
    ? withNodeStatus(
        prev,
        event.payload.node,
        event.payload.status,
        { endedAt: event.payload.ended_at, startedAt: event.payload.started_at },
        outcomeOf(event.payload),
        attemptReportOf(event.payload)
      )
    : prev
}

/**
 * Replace a run's state with a snapshot read back off disk (`dag.get`).
 *
 * The live frames are not replayed anywhere, so a run whose gateway died
 * mid-flight leaves its nodes pinned to whatever the client last heard —
 * typically `running`, forever. The snapshot is the durable record and wins
 * outright; it is only ever fetched when the local state is known to be stale.
 *
 * `prev` contributes two things the snapshot may not have. The tool call the
 * graph was pinned to, which it never knows -- without carrying that over, a
 * repaired graph is orphaned from the transcript row that draws it. And what a
 * node's row was reading fine a moment earlier: its prompt template or summary,
 * which an older run dir did not record, and what a branch did to it, which the
 * snapshot of a run still going takes from the registry, where a write is
 * best-effort and can be dropped. Letting the snapshot win outright there would
 * blank the row.
 */
export const foldDagSnapshot = (prev: DagRunState | null, snapshot: DagRunSnapshot): DagRunState => ({
  runId: snapshot.run_id,
  ...(prev?.toolCallId ? { toolCallId: prev.toolCallId } : {}),
  done: snapshot.finalized,
  dir: snapshot.dir,
  summary: { ...snapshot.summary },
  nodes: snapshot.files.map(file => {
    const before = prev?.nodes.find(node => node.id === file.node)
    const template = file.prompt_template ?? before?.promptTemplate
    const summary = file.node_summary ?? before?.nodeSummary
    const report = attemptReportOf(file)
    // A read issued before a reassignment landed answers with the node as it
    // was: no history, or a shorter one than the live frames have already
    // moved. Everything in that row belongs to the attempt the node has moved
    // past, so the node keeps its own word entire -- not just its identity.
    if (
      before !== undefined &&
      before.attemptAgents !== undefined &&
      (report.history === undefined || report.history.length < before.attemptAgents.length)
    ) {
      return before
    }

    return {
      id: file.node,
      subagent: file.subagent ?? '',
      dependsOn: [...(file.depends_on ?? [])],
      ...(file.instance ? { instance: file.instance } : {}),
      status: file.status,
      ...(file.started_at !== undefined ? { startedAt: file.started_at } : {}),
      ...(file.ended_at !== undefined ? { endedAt: file.ended_at } : {}),
      ...(report.history !== undefined ? { attemptAgents: report.history } : {}),
      ...(template ? { promptTemplate: template } : {}),
      ...(summary ? { nodeSummary: summary } : {}),
      ...(file.output_file ? { outputFile: file.output_file } : {}),
      ...(file.error ? { error: file.error } : {}),
      ...(file.routes !== undefined ? { routes: [...file.routes] } : {}),
      ...(file.select !== undefined ? { select: file.select } : {}),
      ...(file.when !== undefined ? { when: file.when } : {}),
      ...outcomeOf({
        reason: file.reason ?? before?.reason,
        route_evidence: file.route_evidence ?? before?.routeEvidence,
        selected_routes: file.selected_routes ?? before?.selectedRoutes
      })
    }
  })
})

/**
 * A run pinned onto its tool row in the transcript, one entry per `runId`.
 *
 * Two different moments populate `tool.dag`: `recordDagEvent` pins a run as
 * its turn progresses, and `hydrateDagRuns` (`domain/messages.ts`) pins one
 * fetched fresh after a resume. Either way, this is what lets a run still be
 * found once `turnStore`'s live list has moved past it -- at turn end for the
 * first, always for the second.
 */
export const dagRunsFromHistory = (history: readonly Msg[]): DagRunState[] => {
  const byRunId = new Map<string, DagRunState>()

  for (const msg of history) {
    for (const episode of msg.episodes ?? []) {
      for (const tool of episode.tools) {
        if (tool.dag) {
          byRunId.set(tool.dag.runId, tool.dag)
        }
      }
    }
  }

  return [...byRunId.values()]
}
