// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import type { Msg, SessionInfo } from '../types.js'

import { LONG_MSG } from '../config/limits.js'
import { t } from '../i18n/index.js'
import { buildToolTrailLine, fmtK } from '../lib/text.js'
import { addUnique, artifactMessage, changedFile, deliveryFiles } from './turnArtifacts.js'

export const introMsg = (info: SessionInfo): Msg => ({ info, kind: 'intro', role: 'system', text: '' })

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

  const out: Msg[] = []
  let pending: string[] = []
  let artifacts = { changes: [], deliveries: [] } as NonNullable<Msg['artifacts']>
  const flushArtifacts = () => {
    const message = artifactMessage(artifacts)
    if (message) {out.push(message)}
    artifacts = { changes: [], deliveries: [] }
  }

  for (const row of rows) {
    if (!row || typeof row !== 'object') {
      continue
    }

    const { context, duration_ms: durationMs, metadata, name, origin, role, text, tool_calls: calls } = row as TranscriptRow

    if (role === 'user' && origin) {
      flushArtifacts()
      /* A turn the runtime opened, not a person typing. Its text is internal
         prose, so it is replaced rather than shown: the same sentence the live
         trail prints when a delegated result rejoins the conversation, which is
         also the row this replay was missing -- it arrives on an event, and an
         event is not in the transcript. */
      out.push({ role: 'system', text: `${origin} ${t('gui.deleg.delivered', 'delivered')}` })
      pending = []

      continue
    }

    if (role === 'tool') {
      deliveryFiles(metadata).forEach(file => addUnique(artifacts.deliveries, file))
      // The stored span, so a resumed trail line carries the same "(1.2s)" the
      // live one did. Absent on an entry written before it was recorded --
      // undefined, which prints no clock rather than a "(0.0s)" nothing ran in.
      pending.push(
        buildToolTrailLine(
          name ?? 'tool',
          context ?? '',
          undefined,
          undefined,
          durationMs != null ? durationMs / 1000 : undefined
        )
      )

      continue
    }

    if (typeof text !== 'string' || !text.trim()) {
      if (role === 'assistant') {
        for (const call of calls ?? []) {
          let args: unknown = {}
          try { args = JSON.parse(call.arguments || '{}') } catch { args = {} }
          const change = changedFile(call.name || '', args)
          if (change) {addUnique(artifacts.changes, change)}
        }
      }
      continue
    }

    if (role === 'assistant') {
      for (const call of calls ?? []) {
        let args: unknown = {}
        try { args = JSON.parse(call.arguments || '{}') } catch { args = {} }
        const change = changedFile(call.name || '', args)
        if (change) {addUnique(artifacts.changes, change)}
      }
      out.push({ role, text, ...(pending.length && { tools: pending }) })
      pending = []
      if (!calls?.length) {flushArtifacts()}
    } else if (role === 'user' || role === 'system') {
      flushArtifacts()
      out.push({ role, text })
      pending = []
    }
  }

  flushArtifacts()

  return out
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

interface TranscriptRow {
  context?: string
  duration_ms?: number
  name?: string
  metadata?: Record<string, unknown>
  /** See `GatewayTranscriptMessage.origin`: set when the runtime opened the turn. */
  origin?: string
  role?: string
  text?: string
  tool_calls?: Array<{ arguments?: string; id?: string; name?: string }>
}
