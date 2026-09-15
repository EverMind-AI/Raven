// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Picking a sub-agent to start a fresh direct-chat instance of.
//
// The only creation path the user drives: every other one runs for the main
// agent, so without this an agent nothing has been delegated to cannot be
// direct-chatted at all. Distinct from `/subagents`, which configures *which*
// sub-agents exist -- this one instantiates one that already does.

import { Box, Text, useInput, useStdout } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import type { GatewayClient } from '../gatewayClientStub.js'
import type { InstanceRow, SubagentRow, SubagentsInstanceCreateResult, SubagentsListResult } from '../rpc/generated.js'
import type { Theme } from '../theme.js'

import { $directChat } from '../app/directChatStore.js'
import { t as uiText } from '../i18n/index.js'
import { rpcErrorMessage } from '../lib/rpc.js'
import { OverlayHint, useOverlayKeys, windowOffset } from './overlayControls.js'

const VISIBLE = 12
const MIN_WIDTH = 60
const MAX_WIDTH = 110

/**
 * The rows a direct chat can actually address.
 *
 * `stateful` is what makes an agent chattable: `SubagentManager.chat` refuses a
 * stateless one, because against it every turn starts over and the conversation
 * on screen reads as the instance forgetting. An absent flag is a server that
 * predates the field, and is read as "not offered" rather than as an error --
 * offering one on a guess is what would produce the refusal this filter exists
 * to avoid.
 */
export const addressableAgents = (rows: SubagentRow[]) => rows.filter(r => r.enabled && r.stateful === true)

export function NewInstancePicker({ gw, onCancel, onCreated, sessionKey, t }: NewInstancePickerProps) {
  const [rows, setRows] = useState<SubagentRow[]>([])
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [sel, setSel] = useState(0)
  const [creating, setCreating] = useState(false)

  const { instances } = useStore($directChat)
  const { stdout } = useStdout()
  const width = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, (stdout?.columns ?? 80) - 6))

  useOverlayKeys({ disabled: creating, onClose: onCancel })

  useEffect(() => {
    // `probe: false` -- this overlay opens to pick a name, and the up-to-10s
    // per-entry availability round trip buys nothing here: diagnosing a broken
    // agent is `/subagents`' job, and a broken one fails loudly on its own
    // first turn.
    gw.request<SubagentsListResult>('subagents.list', { probe: false })
      .then(r => {
        setRows(addressableAgents(r?.rows ?? []))
        setErr('')
      })
      .catch((e: unknown) => setErr(rpcErrorMessage(e)))
      .finally(() => setLoading(false))
  }, [gw])

  const create = (index: number) => {
    const target = rows[index]

    if (!target || creating) {
      return
    }

    if (!sessionKey) {
      setErr(uiText('gui.panel.no_active_session'))

      return
    }

    setCreating(true)
    gw.request<SubagentsInstanceCreateResult>('subagents.instance.create', {
      agent: target.name,
      session_key: sessionKey
    })
      .then(r => {
        if (!r?.instance) {
          setErr('invalid response: subagents.instance.create')
          setCreating(false)

          return
        }

        // The parent switches into it and closes this overlay; unmounting is
        // why `creating` is never cleared on the success path.
        onCreated(r.instance)
      })
      .catch((e: unknown) => {
        setErr(rpcErrorMessage(e))
        setCreating(false)
      })
  }

  useInput((ch, key) => {
    if (creating) {
      return
    }

    if (key.upArrow && sel > 0) {
      setSel(s => s - 1)
    }

    if (key.downArrow && sel < rows.length - 1) {
      setSel(s => s + 1)
    }

    if (key.return) {
      return create(sel)
    }

    const n = parseInt(ch)

    if (n >= 1 && n <= Math.min(9, rows.length)) {
      create(n - 1)
    }
  })

  if (loading) {
    return <Text color={t.color.muted}>{uiText('gui.panel.loading_subagents')}</Text>
  }

  if (!rows.length) {
    return (
      <Box flexDirection="column">
        <Text color={t.color.muted}>
          {err ? uiText('gui.panel.error_x', '', { detail: err }) : uiText('gui.panel.no_direct_chat')}
        </Text>
        <OverlayHint t={t}>{uiText('gui.panel.esc_cancel')}</OverlayHint>
      </Box>
    )
  }

  const offset = windowOffset(rows.length, sel, VISIBLE)

  return (
    <Box flexDirection="column" width={width}>
      <Text bold color={t.color.accent}>
        New Instance
      </Text>

      {offset > 0 && <Text color={t.color.muted}> {uiText('gui.panel.more_up', '', { n: offset })}</Text>}

      {rows.slice(offset, offset + VISIBLE).map((row, vi) => {
        const i = offset + vi
        const selected = sel === i
        // What you want to know before opening a fifth one. Read off the strip
        // rather than asked for, so the overlay still costs one call.
        const open = instances.filter(r => r.agent === row.name && r.kind !== 'dag-node').length

        return (
          <Box key={row.name}>
            <Text bold={selected} color={selected ? t.color.accent : t.color.muted} inverse={selected}>
              {selected ? '▸ ' : '  '}
            </Text>

            <Box width={26}>
              <Text bold={selected} color={selected ? t.color.accent : t.color.muted} inverse={selected}>
                {String(i + 1).padStart(2)}. {row.name}
              </Text>
            </Box>

            <Box width={20}>
              <Text bold={selected} color={selected ? t.color.accent : t.color.muted} inverse={selected}>
                {row.kind}
                {open ? ` · ${open} open` : ''}
              </Text>
            </Box>

            <Text
              bold={selected}
              color={selected ? t.color.accent : t.color.muted}
              inverse={selected}
              wrap="truncate-end"
            >
              {row.description}
            </Text>
          </Box>
        )
      })}

      {offset + VISIBLE < rows.length && (
        <Text color={t.color.muted}> {uiText('gui.panel.more_down', '', { n: rows.length - offset - VISIBLE })}</Text>
      )}
      {err && <Text color={t.color.label}>{uiText('gui.panel.error_x', '', { detail: err })}</Text>}
      {creating ? (
        <OverlayHint t={t}>{uiText('gui.panel.creating')}</OverlayHint>
      ) : (
        <OverlayHint t={t}>{uiText('gui.panel.k_instances')}</OverlayHint>
      )}
    </Box>
  )
}

interface NewInstancePickerProps {
  gw: GatewayClient
  onCancel: () => void
  onCreated: (row: InstanceRow) => void
  sessionKey: null | string
  t: Theme
}
