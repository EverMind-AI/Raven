// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { Box, Text, useInput, useStdout } from '@hermes/ink'
import { useEffect, useMemo, useState } from 'react'

import type { GatewayClient } from '../gatewayClientStub.js'
import type { ModelOptionProvider, ModelOptionsResponse } from '../gatewayTypes.js'
import type { Theme } from '../theme.js'

import { providerDisplayNames } from '../domain/providers.js'
import { asRpcResult, rpcErrorMessage } from '../lib/rpc.js'
import { OverlayHint, useOverlayKeys, windowItems } from './overlayControls.js'

const VISIBLE = 12
const MIN_WIDTH = 40
const MAX_WIDTH = 90

type Stage = 'provider' | 'addProvider' | 'key' | 'model' | 'addModel' | 'disconnect'

/** The row that opens the second level; not a provider. */
const ADD_ROW = '__add_provider__'

/** What a provider still needs, in the four shapes the backend reports. */
function unconfiguredHint(authType: string | undefined): string {
  if (authType === 'oauth') {
    return '(sign in)'
  }
  if (authType === 'local') {
    return '(no address)'
  }
  if (authType === 'endpoint') {
    return '(no endpoint)'
  }
  return '(no key)'
}

/** A local deployment has no key to paste, and OAuth is a terminal command. */
function unconfiguredWarning(p: ModelOptionProvider): string {
  if (p.auth_type === 'oauth') {
    return `run \`raven provider login ${p.slug.replace(/_/g, '-')}\``
  }
  if (p.auth_type === 'local') {
    return 'enter the server address to activate'
  }
  return p.key_env ? `paste ${p.key_env} to activate` : 'enter a key to activate'
}
type KeyField = 'api_key' | 'api_base'

export function ModelPicker({ gw, onCancel, onSelect, sessionId, t }: ModelPickerProps) {
  const [providers, setProviders] = useState<ModelOptionProvider[]>([])
  const [currentModel, setCurrentModel] = useState('')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [providerIdx, setProviderIdx] = useState(0)
  const [modelIdx, setModelIdx] = useState(0)
  const [stage, setStage] = useState<Stage>('provider')
  const [fromAddList, setFromAddList] = useState(false)
  const [keyInput, setKeyInput] = useState('')
  const [baseInput, setBaseInput] = useState('')
  const [keyField, setKeyField] = useState<KeyField>('api_key')
  const [keySaving, setKeySaving] = useState(false)
  const [keyError, setKeyError] = useState('')
  const [modelNameInput, setModelNameInput] = useState('')

  const { stdout } = useStdout()
  // Pin the picker to a stable width so the FloatBox parent (which shrinks-
  // to-fit with alignSelf="flex-start") doesn't resize as long provider /
  // model names scroll into view, and so `wrap="truncate-end"` on each row
  // has an actual constraint to truncate against.
  const width = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, (stdout?.columns ?? 80) - 6))

  useEffect(() => {
    gw.request<ModelOptionsResponse>('model.options', sessionId ? { session_id: sessionId } : {})
      .then(raw => {
        const r = asRpcResult<ModelOptionsResponse>(raw)

        if (!r) {
          setErr('invalid response: model.options')
          setLoading(false)

          return
        }

        const next = r.providers ?? []
        setProviders(next)
        setCurrentModel(String(r.model ?? ''))
        // Located in the list the first screen shows, not in the whole response:
        // the current provider's index among all of them lands past the end of
        // the configured ones, and a selection past the end highlights no row at
        // all -- the picker opened with no cursor on it.
        setProviderIdx(
          Math.max(
            0,
            next.filter(p => p.authenticated !== false).findIndex(p => p.is_current)
          )
        )
        setModelIdx(0)
        setStage('provider')
        setErr('')
        setLoading(false)
      })
      .catch((e: unknown) => {
        setErr(rpcErrorMessage(e))
        setLoading(false)
      })
  }, [gw, sessionId])

  // Everything the picker needs is already in one response; the split is a view.
  // A provider the user has not set up is not a model they can switch to, and
  // listing all twenty-one of them buried the two or three that were.
  const configured = useMemo(() => providers.filter(p => p.authenticated !== false), [providers])
  const unconfigured = useMemo(() => providers.filter(p => p.authenticated === false), [providers])
  // Which list the selection belongs to, kept apart from which screen is showing:
  // deriving it from the stage meant that leaving the add list for the key screen
  // silently re-pointed the selection at the configured list, so choosing an
  // unconfigured provider opened the first configured one instead.
  const rowsForStage = fromAddList ? unconfigured : configured
  const onAddRow = stage === 'provider' && providerIdx === configured.length
  const provider = onAddRow ? undefined : rowsForStage[providerIdx]
  const models = provider?.models ?? []
  const names = useMemo(() => providerDisplayNames(rowsForStage), [rowsForStage])

  const back = () => {
    if (stage === 'addProvider') {
      setStage('provider')
      setFromAddList(false)
      setProviderIdx(0)

      return
    }

    if (stage === 'addModel') {
      setStage('model')
      setModelNameInput('')
      setKeyError('')

      return
    }

    if (stage === 'model' || stage === 'key' || stage === 'disconnect') {
      // Backing out of a set-up that did not happen returns to the list it was
      // opened from, rather than one level further out than the user asked for.
      const toAddList = stage === 'key' && fromAddList && provider?.authenticated === false
      setStage(toAddList ? 'addProvider' : 'provider')
      setFromAddList(toAddList)
      setModelIdx(0)
      setKeyInput('')
      setBaseInput('')
      setKeyField('api_key')
      setKeyError('')
      setKeySaving(false)

      return
    }

    onCancel()
  }

  useOverlayKeys({ onBack: back, onClose: onCancel })

  useInput((ch, key) => {
    // Key entry stage handles its own input (api_key + optional api_base)
    if (stage === 'key') {
      if (keySaving) {
        return
      }

      const showBase = provider?.auth_type === 'endpoint' || provider?.auth_type === 'local'
      const focusBase = showBase && keyField === 'api_base'

      // Tab moves between the two fields when api_base is shown.
      if (key.tab && showBase && provider?.auth_type !== 'local') {
        setKeyField(f => (f === 'api_key' ? 'api_base' : 'api_key'))

        return
      }

      if (key.return) {
        // Enter on api_key advances to api_base instead of submitting, so the
        // user can fill both fields with single-key navigation.
        if (showBase && keyField === 'api_key' && provider?.auth_type !== 'local') {
          setKeyField('api_base')

          return
        }

        const apiKey = keyInput.trim()
        const apiBase = baseInput.trim()

        // A local deployment is reached by address and has no key; demanding one
        // here blocked the only providers whose key field means nothing, even
        // after the backend stopped requiring it.
        if (!apiKey && provider?.auth_type !== 'local') {
          setKeyError('API key is required')

          return
        }

        if (provider?.needs_api_base && !apiBase) {
          setKeyError('API base URL is required for this provider')

          return
        }

        setKeySaving(true)
        setKeyError('')
        gw.request<{ provider?: ModelOptionProvider }>('model.save_key', {
          slug: provider?.slug,
          api_key: apiKey,
          ...(apiBase ? { api_base: apiBase } : {}),
          ...(sessionId ? { session_id: sessionId } : {})
        })
          .then(raw => {
            const r = asRpcResult<{ provider?: ModelOptionProvider }>(raw)

            if (!r?.provider) {
              setKeyError('failed to save key')
              setKeySaving(false)

              return
            }

            // Update the provider in our list with fresh data
            const saved = r.provider!
            setProviders(prev => prev.map(p => (p.slug === saved.slug ? saved : p)))
            // It has just joined the configured list, so the selection moves with
            // it rather than pointing into the list it left.
            setFromAddList(false)
            setProviderIdx(
              Math.max(
                0,
                providers
                  .filter(pr => pr.authenticated !== false || pr.slug === saved.slug)
                  .findIndex(pr => pr.slug === saved.slug)
              )
            )
            setKeyInput('')
            setBaseInput('')
            setKeyField('api_key')
            setKeySaving(false)
            setStage('model')
            setModelIdx(0)
          })
          .catch((e: unknown) => {
            setKeyError(rpcErrorMessage(e))
            setKeySaving(false)
          })

        return
      }

      if (key.backspace || key.delete) {
        if (focusBase) {
          setBaseInput(v => v.slice(0, -1))
        } else {
          setKeyInput(v => v.slice(0, -1))
        }

        return
      }

      // ctrl+u clears the focused field
      if (ch === '\u0015') {
        if (focusBase) {
          setBaseInput('')
        } else {
          setKeyInput('')
        }

        return
      }

      if (ch && !key.ctrl && !key.meta) {
        if (focusBase) {
          setBaseInput(v => v + ch)
        } else {
          setKeyInput(v => v + ch)
        }
      }

      return
    }

    // Add-model-name sub-input
    if (stage === 'addModel') {
      if (keySaving) {
        return
      }

      if (key.return) {
        const model = modelNameInput.trim()

        if (!model || !provider) {
          return
        }

        setKeySaving(true)
        setKeyError('')
        gw.request<{ provider?: ModelOptionProvider }>('model.add_model', {
          slug: provider.slug,
          model,
          ...(sessionId ? { session_id: sessionId } : {})
        })
          .then(raw => {
            const r = asRpcResult<{ provider?: ModelOptionProvider }>(raw)

            if (!r?.provider) {
              setKeyError('failed to add model')
              setKeySaving(false)

              return
            }

            setProviders(prev => prev.map(p => (p.slug === r.provider!.slug ? r.provider! : p)))
            const idx = (r.provider.models ?? []).indexOf(model)
            setModelNameInput('')
            setKeySaving(false)
            setStage('model')
            setModelIdx(idx >= 0 ? idx : 0)
          })
          .catch((e: unknown) => {
            setKeyError(rpcErrorMessage(e))
            setKeySaving(false)
          })

        return
      }

      if (key.backspace || key.delete) {
        setModelNameInput(v => v.slice(0, -1))

        return
      }

      if (ch && !key.ctrl && !key.meta) {
        setModelNameInput(v => v + ch)
      }

      return
    }

    // Disconnect confirmation stage
    if (stage === 'disconnect') {
      if (ch.toLowerCase() === 'y' || key.return) {
        if (!provider) {
          setStage('provider')

          return
        }

        setKeySaving(true)
        gw.request<{ disconnected?: boolean }>('model.disconnect', {
          slug: provider.slug,
          ...(sessionId ? { session_id: sessionId } : {})
        })
          .then(raw => {
            const r = asRpcResult<{ disconnected?: boolean }>(raw)

            if (r?.disconnected) {
              // Mark provider as unauthenticated in local state
              setProviders(prev =>
                prev.map(p =>
                  p.slug === provider.slug
                    ? {
                        ...p,
                        authenticated: false,
                        models: [],
                        total_models: 0,
                        warning: unconfiguredWarning(p)
                      }
                    : p
                )
              )
            }

            setKeySaving(false)
            setStage('provider')
          })
          .catch(() => {
            setKeySaving(false)
            setStage('provider')
          })

        return
      }

      if (ch.toLowerCase() === 'n' || key.escape) {
        setStage('provider')

        return
      }

      return
    }

    const onProviderList = stage === 'provider' || stage === 'addProvider'
    const addRowCount = stage === 'provider' && unconfigured.length > 0 ? 1 : 0
    const count = onProviderList ? rowsForStage.length + addRowCount : models.length
    const sel = onProviderList ? providerIdx : modelIdx
    const setSel = onProviderList ? setProviderIdx : setModelIdx

    if (key.upArrow && sel > 0) {
      setSel(v => v - 1)

      return
    }

    if (key.downArrow && sel < count - 1) {
      setSel(v => v + 1)

      return
    }

    if (key.return) {
      if (stage === 'provider' && onAddRow) {
        setStage('addProvider')
        setFromAddList(true)
        setProviderIdx(0)

        return
      }

      if (onProviderList) {
        if (!provider) {
          return
        }

        if (provider.authenticated === false) {
          // Every shape but OAuth is completable here: the key stage asks for a
          // key, an address, or both, from what the provider reports needing.
          // OAuth is a browser flow plus a token file, so its row states the
          // command that does it rather than pretending to.
          if (provider.auth_type !== 'oauth') {
            setStage('key')
            setKeyInput('')
            setBaseInput('')
            setKeyField(provider.auth_type === 'local' ? 'api_base' : 'api_key')
            setKeyError('')
          }

          return
        }

        setStage('model')
        setModelIdx(0)

        return
      }

      if (stage === 'model' && keySaving) {
        return
      }

      const model = models[modelIdx]

      if (provider && model) {
        onSelect(model, provider.slug)
      } else {
        setStage('provider')
      }

      return
    }

    // Model stage: add a model name to the provider's list.
    if (ch.toLowerCase() === 'a' && stage === 'model' && provider && !keySaving) {
      setStage('addModel')
      setModelNameInput('')
      setKeyError('')

      return
    }

    // Model stage: delete the highlighted model name from the provider's list.
    if ((ch.toLowerCase() === 'd' || ch.toLowerCase() === 'x') && stage === 'model' && !keySaving) {
      const model = models[modelIdx]

      if (!provider || !model) {
        return
      }

      setKeySaving(true)
      setKeyError('')
      gw.request<{ provider?: ModelOptionProvider }>('model.remove_model', {
        slug: provider.slug,
        model,
        ...(sessionId ? { session_id: sessionId } : {})
      })
        .then(raw => {
          const r = asRpcResult<{ provider?: ModelOptionProvider }>(raw)

          if (r?.provider) {
            setProviders(prev => prev.map(p => (p.slug === r.provider!.slug ? r.provider! : p)))
            setModelIdx(idx => Math.max(0, Math.min(idx, (r.provider!.models?.length ?? 1) - 1)))
          }

          setKeySaving(false)
        })
        .catch((e: unknown) => {
          setKeyError(rpcErrorMessage(e))
          setKeySaving(false)
        })

      return
    }

    // Disconnect: only in provider stage, only for authenticated providers
    if (ch.toLowerCase() === 'd' && stage === 'provider' && provider && provider.authenticated !== false) {
      setStage('disconnect')

      return
    }
  })

  if (loading) {
    return <Text color={t.color.muted}>loading models…</Text>
  }

  if (err) {
    return (
      <Box flexDirection="column">
        <Text color={t.color.label}>error: {err}</Text>
        <OverlayHint t={t}>Esc/q cancel</OverlayHint>
      </Box>
    )
  }

  if (!providers.length) {
    return (
      <Box flexDirection="column">
        <Text color={t.color.muted}>no providers available</Text>
        <OverlayHint t={t}>Esc/q cancel</OverlayHint>
      </Box>
    )
  }

  // ── Key entry stage ──────────────────────────────────────────────────
  if (stage === 'key' && provider) {
    const showBase = provider.auth_type === 'endpoint' || provider.auth_type === 'local'
    // A local deployment has no key, so it gets no key field: an input the server
    // ignores reads as something the user has to find before they can continue.
    const showKey = provider.auth_type !== 'local'
    const focusBase = showBase && (keyField === 'api_base' || !showKey)
    const masked = keyInput ? '•'.repeat(Math.min(keyInput.length, 40)) : ''
    const keyLabel = provider.key_env ?? 'API key'
    const baseLabel =
      provider.auth_type === 'local'
        ? 'Server address'
        : `API base${provider.needs_api_base ? ' (required)' : ' (optional)'}`
    const caret = keySaving ? '' : '▎'

    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          Configure {provider.name}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          Saved to ~/.raven/.env{showBase ? ' · Tab switches field' : ''}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        {showKey ? (
          <>
            <Text color={focusBase ? t.color.muted : t.color.accent} wrap="truncate-end">
              {focusBase ? '  ' : '▸ '}
              {keyLabel}:
            </Text>

            <Text color={t.color.accent} wrap="truncate-end">
              {'  '}
              {masked || '(empty)'}
              {focusBase ? '' : caret}
            </Text>
          </>
        ) : null}

        {showBase ? (
          <>
            <Text color={focusBase ? t.color.accent : t.color.muted} wrap="truncate-end">
              {focusBase ? '▸ ' : '  '}
              {baseLabel}:
            </Text>

            <Text color={t.color.accent} wrap="truncate-end">
              {'  '}
              {baseInput || '(empty)'}
              {focusBase ? caret : ''}
            </Text>
          </>
        ) : null}

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        {keyError ? (
          <Text color={t.color.label} wrap="truncate-end">
            error: {keyError}
          </Text>
        ) : keySaving ? (
          <Text color={t.color.muted} wrap="truncate-end">
            saving…
          </Text>
        ) : (
          <Text color={t.color.muted} wrap="truncate-end">
            {' '}
          </Text>
        )}

        <OverlayHint t={t}>
          {showBase ? 'Enter next/save · Tab field · Ctrl+U clear · Esc back' : 'Enter save · Ctrl+U clear · Esc back'}
        </OverlayHint>
      </Box>
    )
  }

  // ── Add model name stage ─────────────────────────────────────────────
  if (stage === 'addModel' && provider) {
    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          Add model to {provider.name}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          Type the full model id
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        <Text color={t.color.accent} wrap="truncate-end">
          {'  '}
          {modelNameInput || '(empty)'}
          {keySaving ? '' : '▎'}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        {keyError ? (
          <Text color={t.color.label} wrap="truncate-end">
            error: {keyError}
          </Text>
        ) : keySaving ? (
          <Text color={t.color.muted} wrap="truncate-end">
            saving…
          </Text>
        ) : (
          <Text color={t.color.muted} wrap="truncate-end">
            {' '}
          </Text>
        )}

        <OverlayHint t={t}>Enter add · Ctrl+U clear · Esc back</OverlayHint>
      </Box>
    )
  }

  // ── Disconnect confirmation stage ─────────────────────────────────────
  if (stage === 'disconnect' && provider) {
    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          Disconnect {provider.name}?
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          This removes saved credentials for {provider.name}.
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          You can re-authenticate later by selecting it again.
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        {keySaving ? (
          <Text color={t.color.muted} wrap="truncate-end">
            disconnecting…
          </Text>
        ) : (
          <OverlayHint t={t}>y/Enter confirm · n/Esc cancel</OverlayHint>
        )}
      </Box>
    )
  }

  // ── Provider selection stages ────────────────────────────────────────
  if (stage === 'provider' || stage === 'addProvider') {
    const adding = stage === 'addProvider'
    const rows = rowsForStage.map((p, i) => {
      const authMark = p.authenticated === false ? '○' : p.is_current ? '*' : '●'
      const modelCount = p.total_models ?? p.models?.length ?? 0

      const suffix = p.authenticated === false ? unconfiguredHint(p.auth_type) : `${modelCount} models`

      return `${authMark} ${names[i]} · ${suffix}`
    })

    if (!adding && unconfigured.length > 0) {
      rows.push(`+ add a provider · ${unconfigured.length} not set up`)
    }

    const { items, offset } = windowItems(rows, providerIdx, VISIBLE)

    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          {adding ? 'Add a provider' : 'Select provider (step 1/2)'}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {adding ? 'Enter to set one up · Esc to go back' : 'Full model IDs on the next step · Enter to continue'}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          Current: {currentModel || '(unknown)'}
        </Text>
        <Text color={t.color.label} wrap="truncate-end">
          {provider?.warning ? `warning: ${provider.warning}` : ' '}
        </Text>
        <Text color={t.color.muted} wrap="truncate-end">
          {offset > 0 ? ` ↑ ${offset} more` : ' '}
        </Text>

        {Array.from({ length: VISIBLE }, (_, i) => {
          const row = items[i]
          const idx = offset + i
          const p = rowsForStage[idx]
          const dimmed = p ? p.authenticated === false : false

          return row ? (
            <Text
              bold={providerIdx === idx}
              color={providerIdx === idx ? t.color.accent : dimmed ? t.color.label : t.color.muted}
              inverse={providerIdx === idx}
              key={providers[idx]?.slug ?? `row-${idx}`}
              wrap="truncate-end"
            >
              {providerIdx === idx ? '▸ ' : '  '}
              {idx + 1}. {row}
            </Text>
          ) : (
            <Text color={t.color.muted} key={`pad-${i}`} wrap="truncate-end">
              {' '}
            </Text>
          )
        })}

        <Text color={t.color.muted} wrap="truncate-end">
          {offset + VISIBLE < rows.length ? ` ↓ ${rows.length - offset - VISIBLE} more` : ' '}
        </Text>

        <OverlayHint t={t}>↑/↓ select · Enter choose · d disconnect · Esc/q cancel</OverlayHint>
      </Box>
    )
  }

  // ── Model selection stage ────────────────────────────────────────────
  const { items, offset } = windowItems(models, modelIdx, VISIBLE)

  return (
    <Box flexDirection="column" width={width}>
      <Text bold color={t.color.accent} wrap="truncate-end">
        Select model (step 2/2)
      </Text>

      <Text color={t.color.muted} wrap="truncate-end">
        {names[providerIdx] || '(unknown provider)'} · Esc back
      </Text>
      <Text color={t.color.label} wrap="truncate-end">
        {provider?.warning ? `warning: ${provider.warning}` : ' '}
      </Text>
      <Text color={t.color.muted} wrap="truncate-end">
        {offset > 0 ? ` ↑ ${offset} more` : ' '}
      </Text>

      {Array.from({ length: VISIBLE }, (_, i) => {
        const row = items[i]
        const idx = offset + i

        if (!row) {
          return !models.length && i === 0 ? (
            <Text color={t.color.muted} key="empty" wrap="truncate-end">
              no models listed for this provider
            </Text>
          ) : (
            <Text color={t.color.muted} key={`pad-${i}`} wrap="truncate-end">
              {' '}
            </Text>
          )
        }

        const prefix = modelIdx === idx ? '▸ ' : row === currentModel ? '* ' : '  '

        return (
          <Text
            bold={modelIdx === idx}
            color={modelIdx === idx ? t.color.accent : t.color.muted}
            inverse={modelIdx === idx}
            key={`${provider?.slug ?? 'prov'}:${idx}:${row}`}
            wrap="truncate-end"
          >
            {prefix}
            {idx + 1}. {row}
          </Text>
        )
      })}

      <Text color={t.color.muted} wrap="truncate-end">
        {offset + VISIBLE < models.length ? ` ↓ ${models.length - offset - VISIBLE} more` : ' '}
      </Text>

      <Text color={t.color.muted} wrap="truncate-end">
        scope: global
      </Text>
      <OverlayHint t={t}>
        {models.length
          ? '↑/↓ select · Enter switch · a add · d/x delete · Esc back · q close'
          : 'a add model · Enter/Esc back · q close'}
      </OverlayHint>
    </Box>
  )
}

interface ModelPickerProps {
  gw: GatewayClient
  onCancel: () => void
  onSelect: (model: string, providerSlug: string) => void
  sessionId: string | null
  t: Theme
}
