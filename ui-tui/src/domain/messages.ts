// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import type { DagGetResult, TranscriptDelegated } from '../rpc/index.js'
import type { Msg, SessionInfo } from '../types.js'
import type { DagRunState } from './dagRun.js'
import type { FoldRow } from './episodeFold.js'

import { LONG_MSG } from '../config/limits.js'
import { t } from '../i18n/index.js'
import { fmtK } from '../lib/text.js'
import { foldDagSnapshot } from './dagRun.js'
import { clampToolPreview, foldRowsIntoEpisodes } from './episodeFold.js'
import { addUnique, artifactMessage, changedFile, deliveryFiles } from './turnArtifacts.js'

/** Structurally `GatewayRpc`, restated so a test can pass a plain function. */
type Rpc = <T extends object>(
  method: string,
  params?: Record<string, unknown>,
  opts?: { quiet?: boolean }
) => Promise<null | T>

export const introMsg = (info: SessionInfo): Msg => ({ info, kind: 'intro', role: 'system', text: '' })

// The intro row is the opening cover -- wordmark plus session panel, 35 rows of
// it -- and it stays in `historyItems` because the late `session.info` event
// patches itself onto that row. Only the chat view drops it, and only once a
// turn has happened: startup notices and slash output are not a conversation,
// so a box that warns about its credentials on boot still gets its cover.
export const hideIntroAfterFirstTurn = (items: Msg[]): Msg[] =>
  items[0]?.kind === 'intro' && items.some(msg => msg.role === 'assistant' || msg.role === 'user')
    ? items.slice(1)
    : items

export const imageTokenMeta = (info?: ImageMeta | null) => {
  const { width, height, token_estimate: t } = info ?? {}

  return [width && height ? `${width}x${height}` : '', (t ?? 0) > 0 ? `~${fmtK(t!)} tok` : '']
    .filter(Boolean)
    .join(' · ')
}

export const attachedImageNotice = (info?: ({ name?: string } & ImageMeta) | null) => {
  const meta = imageTokenMeta(info)
  const label = info?.name ? `📎 Attached image: ${info.name}` : '📎 Attached image'

  return `${label}${meta ? ` · ${meta}` : ''}`
}

export const userDisplay = (text: string) => {
  if (text.length <= LONG_MSG) {
    return text
  }

  const first = text.split('\n')[0]?.trim() ?? ''
  const words = first.split(/\s+/).filter(Boolean)
  const prefix = (words.length > 1 ? words.slice(0, 4).join(' ') : first).slice(0, 80)

  return `${prefix || '(message)'} [long message]`
}

export const toTranscriptMessages = (rows: unknown): Msg[] => {
  if (!Array.isArray(rows)) {
    return []
  }

  const folded: FoldRow[] = []
  let artifacts = { changes: [], deliveries: [] } as NonNullable<Msg['artifacts']>
  const flushArtifacts = () => {
    const message = artifactMessage(artifacts)

    if (message) {
      folded.push({ passthrough: message, role: 'system', text: '' })
    }

    artifacts = { changes: [], deliveries: [] }
  }

  for (const row of rows) {
    if (!row || typeof row !== 'object') {
      continue
    }

    const {
      context,
      delegated,
      duration_ms: durationMs,
      metadata,
      name,
      origin,
      reasoning_content: reasoning,
      reasoning_ms: reasoningMs,
      role,
      text,
      tool_call_id: toolCallId,
      tool_calls: toolCalls
    } = row as TranscriptRow

    if (role === 'user' && origin) {
      flushArtifacts()
      /* A turn the runtime opened, not a person typing. Its text is internal
         prose, so it is replaced rather than shown: the same line the live
         trail prints when a delegated result rejoins the conversation, which is
         also the row this replay was missing -- it arrives on an event, and an
         event is not in the transcript. Only a subagent delivery carries
         `delegated`; a cron/sentinel/heartbeat-opened turn falls back to the
         older, label-less line rather than fabricating one. */
      if (delegated) {
        const key = delegated.status === 'error' ? 'gui.deleg.delivered_err' : 'gui.deleg.delivered'

        folded.push({ role: 'system', text: `↩ ${delegated.label} — ${t(key, key)}` })
      } else {
        folded.push({ role: 'system', text: `${origin} ${t('gui.deleg.delivered', 'delivered')}` })
      }

      continue
    }

    if (role === 'tool') {
      deliveryFiles(metadata).forEach(file => addUnique(artifacts.deliveries, file))
      folded.push({
        role: 'tool',
        text: clampToolPreview(typeof text === 'string' ? text : ''),
        ...(durationMs != null ? { durationMs } : {}),
        ...(toolCallId ? { toolCallId } : {}),
        // Carried along so the core can stand up an episode on its own when no
        // announcing call claims this row -- see `foldRowsIntoEpisodes`.
        ...(name ? { name, summary: (context ?? '').trim() } : {})
      })

      continue
    }

    if (role !== 'assistant' && role !== 'user' && role !== 'system') {
      continue
    }

    const calls = (toolCalls ?? [])
      .filter((call): call is Required<TranscriptToolCallRow> => Boolean(call?.id && call.name))
      .map(call => ({ arguments: call.arguments ?? '', id: call.id, name: call.name }))

    // Ahead of the skip below: a row that carries only a file-changing call
    // still contributes that file to the shelf, which is what main's own
    // empty-text branch did before the skip existed.
    if (role === 'assistant') {
      for (const call of toolCalls ?? []) {
        let args: unknown = {}

        try {
          args = JSON.parse(call.arguments || '{}')
        } catch {
          args = {}
        }

        const change = changedFile(call.name ?? '', args)

        if (change) {
          addUnique(artifacts.changes, change)
        }
      }
    }

    // The backend joins only `type=="text"` blocks, so an image-only message
    // arrives with empty text -- skip it, but only when nothing else on the row
    // is worth a line; tool_calls or reasoning make it a real row regardless.
    // Skipped without flushing, so an empty row cannot split a turn's shelf.
    if (!(typeof text === 'string' && text.trim()) && !calls.length && !reasoning) {
      continue
    }

    if (role !== 'assistant') {
      flushArtifacts()
    }

    folded.push({
      role,
      text: typeof text === 'string' ? text : '',
      ...(calls.length ? { calls, foldSeed: calls[0]!.id } : {}),
      ...(reasoning ? { reasoning } : {}),
      ...(reasoningMs != null ? { reasoningMs } : {})
    })

    if (role === 'assistant' && !calls.length) {
      flushArtifacts()
    }
  }

  flushArtifacts()

  return foldRowsIntoEpisodes(folded)
}

/**
 * Attach each resumed DAG call's graph, read back off disk.
 *
 * Done before the transcript is set rather than while rendering it, so drawing
 * stays a pure function of state: a fetch hung off the render would fire again
 * on every re-render and reorder against the reader's own clicks.
 *
 * A run whose directory is gone attaches nothing. The row then reads as it did
 * before graphs existed, which is the honest rendering of "the outputs were
 * deleted" -- an empty frame would claim the run had no nodes.
 */
export const hydrateDagRuns = async (rows: unknown, msgs: Msg[], rpc: Rpc, sessionId: string): Promise<Msg[]> => {
  if (!Array.isArray(rows)) {
    return msgs
  }

  const byCall = new Map<string, string>()

  for (const row of rows) {
    const { dag_run_id: runId, tool_call_id: callId } = (row ?? {}) as TranscriptRow

    if (runId && callId) {
      byCall.set(callId, runId)
    }
  }

  if (!byCall.size) {
    return msgs
  }

  const runs = new Map<string, DagRunState>()

  await Promise.all(
    [...new Set(byCall.values())].map(async runId => {
      try {
        // `quiet` is required, not cosmetic: without it a deleted run dir's
        // error would print into the transcript, which is exactly what a
        // resumed session must not surface for an ordinary missing run.
        const result = await rpc<DagGetResult>('dag.get', { run_id: runId, session_key: sessionId }, { quiet: true })

        if (result?.run) {
          runs.set(runId, foldDagSnapshot(null, result.run))
        }
      } catch {
        // Reported nowhere on purpose: a missing run dir is an ordinary state
        // for an old session, not an error the reader has to acknowledge.
      }
    })
  )

  for (const msg of msgs) {
    for (const episode of msg.episodes ?? []) {
      for (const tool of episode.tools) {
        const run = runs.get(byCall.get(tool.id) ?? '')

        if (run) {
          tool.dag = run
        }
      }
    }
  }

  return msgs
}

export const fmtDuration = (ms: number) => {
  const t = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const s = t % 60

  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`
}

interface ImageMeta {
  height?: number
  token_estimate?: number
  width?: number
}

interface TranscriptToolCallRow {
  arguments?: string
  id?: string
  name?: string
}

interface TranscriptRow {
  context?: string
  /** Set on a `run_subagent_dag` tool row; the run it started, for `hydrateDagRuns`. */
  dag_run_id?: string
  /** See `TranscriptMessage.delegated`: set when a delegated run's result opened the turn. */
  delegated?: TranscriptDelegated
  duration_ms?: number
  name?: string
  metadata?: Record<string, unknown>
  /** See `GatewayTranscriptMessage.origin`: set when the runtime opened the turn. */
  origin?: string
  reasoning_content?: string
  reasoning_ms?: number
  role?: string
  text?: string
  tool_call_id?: string
  tool_calls?: TranscriptToolCallRow[]
}
