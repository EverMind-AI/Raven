// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import type { RunExternalProcess } from '@hermes/ink'

import { Box, Text, useInput, useStdout } from '@hermes/ink'
import { useCallback, useEffect, useMemo, useState } from 'react'

import type { GatewayClient } from '../gatewayClientStub.js'
import type {
  ModelEndpointsResponse,
  ModelOptionProvider,
  ModelOptionsResponse,
  ProviderEndpointInfo
} from '../gatewayTypes.js'
import type { LaunchResult } from '../lib/externalCli.js'
import type { Theme } from '../theme.js'

import { providerDisplayNames } from '../domain/providers.js'
import { asRpcResult, rpcErrorMessage } from '../lib/rpc.js'
import { OverlayHint, useOverlayKeys, windowItems } from './overlayControls.js'

const VISIBLE = 12
const MIN_WIDTH = 40
const MAX_WIDTH = 90

type Stage =
  | 'provider'
  | 'addProvider'
  | 'key'
  | 'model'
  | 'addModel'
  | 'disconnect'
  | 'oauthLogin'
  | 'endpoints'
  | 'addEndpoint'

/** Where the sign-in handoff is: waiting to start, running, or back from it. */
type LoginPhase = 'idle' | 'running' | 'done'

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

/** The three fields the add-endpoint screen collects, in the order it asks. */
type EndpointField = 'label' | 'api_key' | 'api_base'

const ENDPOINT_FIELD_ORDER: EndpointField[] = ['label', 'api_key', 'api_base']

export function ModelPicker({ gw, launcher, onCancel, onSelect, sessionId, suspend, t }: ModelPickerProps) {
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
  const [endpoints, setEndpoints] = useState<ProviderEndpointInfo[]>([])
  const [endpointIdx, setEndpointIdx] = useState(0)
  const [endpointField, setEndpointField] = useState<EndpointField>('label')
  const [endpointInputs, setEndpointInputs] = useState<Record<EndpointField, string>>({
    api_base: '',
    api_key: '',
    label: ''
  })
  // The sign-in screen names its provider from here rather than from the
  // selection: a successful login moves that provider out of the unconfigured
  // list the cursor is pointing into, which would rename the screen mid-flow.
  const [loginTarget, setLoginTarget] = useState<null | { name: string; slug: string }>(null)
  const [loginPhase, setLoginPhase] = useState<LoginPhase>('idle')
  const [loginError, setLoginError] = useState('')

  const { stdout } = useStdout()
  // Pin the picker to a stable width so the FloatBox parent (which shrinks-
  // to-fit with alignSelf="flex-start") doesn't resize as long provider /
  // model names scroll into view, and so `wrap="truncate-end"` on each row
  // has an actual constraint to truncate against.
  const width = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, (stdout?.columns ?? 80) - 6))

  // Refetch only: no cursor, no stage. A sign-in refetches to pick up the new
  // credential, and moving the cursor or the stage from here would drop the
  // user on whichever provider the fresh list happens to put under the old
  // index instead of the one they just signed in to.
  const loadOptions = useCallback(async (): Promise<ModelOptionProvider[] | null> => {
    try {
      const raw = await gw.request<ModelOptionsResponse>('model.options', sessionId ? { session_id: sessionId } : {})
      const r = asRpcResult<ModelOptionsResponse>(raw)

      if (!r) {
        setErr('invalid response: model.options')
        setLoading(false)

        return null
      }

      const next = r.providers ?? []
      setProviders(next)
      setCurrentModel(String(r.model ?? ''))
      setErr('')
      setLoading(false)

      return next
    } catch (e: unknown) {
      setErr(rpcErrorMessage(e))
      setLoading(false)

      return null
    }
  }, [gw, sessionId])

  useEffect(() => {
    void loadOptions().then(next => {
      if (!next) {
        return
      }

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
    })
  }, [loadOptions])

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
  // The sign-in screen and the way back out both ask this, and both have to ask
  // it of the target rather than of the row under the cursor: a login that landed
  // moves its provider off the unconfigured list the cursor points into.
  const targetStillUnauthenticated = loginTarget
    ? providers.find(p => p.slug === loginTarget.slug)?.authenticated === false
    : provider?.authenticated === false
  const names = useMemo(() => providerDisplayNames(rowsForStage), [rowsForStage])

  // Refetch only, same as ``loadOptions``: the endpoint list is not carried by
  // ``model.options``, so every screen that shows it asks for it.
  const loadEndpoints = useCallback(
    async (slug: string) => {
      try {
        const raw = await gw.request<ModelEndpointsResponse>('model.endpoints', {
          slug,
          ...(sessionId ? { session_id: sessionId } : {})
        })
        setEndpoints(asRpcResult<ModelEndpointsResponse>(raw)?.endpoints ?? [])
      } catch (e: unknown) {
        setKeyError(rpcErrorMessage(e))
      }
    },
    [gw, sessionId]
  )

  const clearEndpointInputs = () => {
    setEndpointInputs({ api_base: '', api_key: '', label: '' })
    setEndpointField('label')
  }

  const back = () => {
    if (stage === 'addEndpoint') {
      setStage('endpoints')
      clearEndpointInputs()
      setKeyError('')

      return
    }

    if (stage === 'endpoints') {
      setStage('model')
      setKeyError('')

      return
    }

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

    if (stage === 'model' || stage === 'key' || stage === 'disconnect' || stage === 'oauthLogin') {
      // Backing out of a set-up that did not happen returns to the list it was
      // opened from, rather than one level further out than the user asked for.
      const toAddList = (stage === 'key' || stage === 'oauthLogin') && fromAddList && targetStillUnauthenticated
      setStage(toAddList ? 'addProvider' : 'provider')
      setFromAddList(toAddList)

      // A set-up that did happen leaves the add list, and the index that pointed
      // into it can be past the end of the shorter configured list -- a
      // selection past the end highlights no row at all.
      if (fromAddList && !toAddList) {
        setProviderIdx(0)
      }

      setModelIdx(0)
      setKeyInput('')
      setBaseInput('')
      setKeyField('api_key')
      setKeyError('')
      setKeySaving(false)
      setLoginTarget(null)
      setLoginPhase('idle')
      setLoginError('')

      return
    }

    onCancel()
  }

  const signIn = async (target: { name: string; slug: string }) => {
    const args = ['provider', 'login', target.slug.replace(/_/g, '-')]

    setLoginPhase('running')
    setLoginError('')

    let result: LaunchResult = { code: null }

    // The child owns the real terminal for the whole browser / device-code flow,
    // so Ink has to be suspended around it rather than redrawing over it.
    await suspend(async () => {
      result = await launcher(args)
    })

    if (result.error) {
      setLoginError(result.error)
      setLoginPhase('idle')

      return
    }

    // A child killed by a signal reports no exit code, which is what Ctrl+C looks
    // like from here -- and this screen promises that Ctrl+C cancels. Rendering it
    // as `exited with code null` told the user their own key press was a fault.
    if (result.code === null) {
      setLoginPhase('idle')

      return
    }

    if (result.code !== 0) {
      setLoginError(`\`raven ${args.join(' ')}\` exited with code ${result.code}`)
      setLoginPhase('idle')

      return
    }

    const fresh = await loadOptions()
    const signedIn = fresh?.find(p => p.slug === target.slug)

    // A sign-in that took moves its provider onto the configured list, so this
    // goes where the provider now is -- the same landing the key screen gives a
    // provider it just set up. Staying put instead left the user to press Esc
    // into the list the provider had just left.
    if (signedIn?.authenticated) {
      setFromAddList(false)
      setProviderIdx(Math.max(0, fresh!.filter(p => p.authenticated !== false).indexOf(signedIn)))
      setLoginTarget(null)
      setLoginPhase('idle')
      setStage('model')
      setModelIdx(0)

      return
    }

    // Signed in and still no credentials, or the list could not be refetched:
    // both are reported on this screen rather than navigated away from.
    setLoginPhase('done')
  }

  useOverlayKeys({
    // Both stages type into a field, and the sign-in has handed the terminal to
    // a child process -- leaving the screen from under either one loses input
    // the user has already given.
    closeOnQ: stage !== 'key' && stage !== 'addModel' && stage !== 'addEndpoint',
    disabled: loginPhase === 'running',
    onBack: back,
    onClose: onCancel
  })

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

    // Add-endpoint sub-input: label, then key, then base.
    if (stage === 'addEndpoint') {
      if (keySaving) {
        return
      }

      const fieldIdx = ENDPOINT_FIELD_ORDER.indexOf(endpointField)

      if (key.tab) {
        setEndpointField(ENDPOINT_FIELD_ORDER[(fieldIdx + 1) % ENDPOINT_FIELD_ORDER.length])

        return
      }

      if (key.return) {
        // Enter advances through the fields and only submits on the last one,
        // so the whole endpoint can be entered without reaching for Tab.
        if (fieldIdx < ENDPOINT_FIELD_ORDER.length - 1) {
          setEndpointField(ENDPOINT_FIELD_ORDER[fieldIdx + 1])

          return
        }

        const label = endpointInputs.label.trim()

        if (!provider) {
          return
        }

        // The label is what every later edit addresses this entry by, so an
        // unnamed one is not something the list could offer back.
        if (!label) {
          setKeyError('a label is required')

          return
        }

        const apiKey = endpointInputs.api_key.trim()
        const apiBase = endpointInputs.api_base.trim()

        setKeySaving(true)
        setKeyError('')
        gw.request<ModelEndpointsResponse>('model.add_endpoint', {
          slug: provider.slug,
          label,
          ...(apiKey ? { api_key: apiKey } : {}),
          ...(apiBase ? { api_base: apiBase } : {}),
          ...(sessionId ? { session_id: sessionId } : {})
        })
          .then(raw => {
            const r = asRpcResult<ModelEndpointsResponse>(raw)

            if (!r?.endpoints) {
              setKeyError('failed to add endpoint')
              setKeySaving(false)

              return
            }

            setEndpoints(r.endpoints)
            setEndpointIdx(
              Math.max(
                0,
                r.endpoints.findIndex(ep => ep.label === label)
              )
            )
            clearEndpointInputs()
            setKeySaving(false)
            setStage('endpoints')
          })
          .catch((e: unknown) => {
            setKeyError(rpcErrorMessage(e))
            setKeySaving(false)
          })

        return
      }

      if (key.backspace || key.delete) {
        setEndpointInputs(v => ({ ...v, [endpointField]: v[endpointField].slice(0, -1) }))

        return
      }

      if (ch === '\u0015') {
        setEndpointInputs(v => ({ ...v, [endpointField]: '' }))

        return
      }

      if (ch && !key.ctrl && !key.meta) {
        setEndpointInputs(v => ({ ...v, [endpointField]: v[endpointField] + ch }))
      }

      return
    }

    // Endpoint list stage: same add/delete vocabulary as the model list.
    if (stage === 'endpoints') {
      if (keySaving) {
        return
      }

      if (key.upArrow && endpointIdx > 0) {
        setEndpointIdx(v => v - 1)

        return
      }

      if (key.downArrow && endpointIdx < endpoints.length - 1) {
        setEndpointIdx(v => v + 1)

        return
      }

      if (ch.toLowerCase() === 'a') {
        clearEndpointInputs()
        setKeyError('')
        setStage('addEndpoint')

        return
      }

      if (ch.toLowerCase() === 'd' || ch.toLowerCase() === 'x') {
        const target = endpoints[endpointIdx]

        if (!provider || !target) {
          return
        }

        setKeySaving(true)
        setKeyError('')
        gw.request<ModelEndpointsResponse>('model.remove_endpoint', {
          slug: provider.slug,
          label: target.label,
          ...(sessionId ? { session_id: sessionId } : {})
        })
          .then(raw => {
            const r = asRpcResult<ModelEndpointsResponse>(raw)

            if (r?.endpoints) {
              setEndpoints(r.endpoints)
              setEndpointIdx(idx => Math.max(0, Math.min(idx, r.endpoints!.length - 1)))
            }

            setKeySaving(false)
          })
          .catch((e: unknown) => {
            setKeyError(rpcErrorMessage(e))
            setKeySaving(false)
          })

        return
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

    // Sign-in confirmation stage. Without its own branch the keys fall through
    // to the list handlers below, where Enter on a provider with no models sends
    // the user back to the list without ever starting the login.
    if (stage === 'oauthLogin') {
      if (key.return && loginPhase !== 'running' && loginTarget) {
        void signIn(loginTarget)
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
          // Every shape but OAuth is completable in place: the key stage asks for
          // a key, an address, or both, from what the provider reports needing.
          // OAuth is a browser flow, so it gets a confirmation screen and then
          // the terminal, which is the one thing this screen cannot host.
          if (provider.auth_type === 'oauth') {
            setStage('oauthLogin')
            setLoginTarget({ name: provider.name, slug: provider.slug })
            setLoginPhase('idle')
            setLoginError('')

            return
          }

          setStage('key')
          setKeyInput('')
          setBaseInput('')
          setKeyField(provider.auth_type === 'local' ? 'api_base' : 'api_key')
          setKeyError('')

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

    // Model stage: manage the several accounts/regions behind this provider.
    if (ch.toLowerCase() === 'e' && stage === 'model' && provider && !keySaving) {
      setEndpoints([])
      setEndpointIdx(0)
      setKeyError('')
      setStage('endpoints')
      void loadEndpoints(provider.slug)

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

  // ── Endpoint list stage ──────────────────────────────────────────────
  if (stage === 'endpoints' && provider) {
    const rows = endpoints.map(
      ep => `${ep.label} · ${ep.api_key || '(empty)'}${ep.api_base ? ` · ${ep.api_base}` : ''}`
    )
    const { items, offset } = windowItems(rows, endpointIdx, VISIBLE)

    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          Endpoints for {provider.name}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          Several accounts or regions under one provider · keys are never shown
        </Text>

        <Text color={t.color.label} wrap="truncate-end">
          {keyError ? `error: ${keyError}` : ' '}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {offset > 0 ? ` ↑ ${offset} more` : ' '}
        </Text>

        {Array.from({ length: VISIBLE }, (_, i) => {
          const row = items[i]
          const idx = offset + i

          if (!row) {
            return !rows.length && i === 0 ? (
              <Text color={t.color.muted} key="empty" wrap="truncate-end">
                no endpoints configured · a adds one
              </Text>
            ) : (
              <Text color={t.color.muted} key={`pad-${i}`} wrap="truncate-end">
                {' '}
              </Text>
            )
          }

          return (
            <Text
              bold={endpointIdx === idx}
              color={endpointIdx === idx ? t.color.accent : t.color.muted}
              inverse={endpointIdx === idx}
              key={`${provider.slug}:${endpoints[idx]?.label ?? idx}`}
              wrap="truncate-end"
            >
              {endpointIdx === idx ? '▸ ' : '  '}
              {idx + 1}. {row}
            </Text>
          )
        })}

        <Text color={t.color.muted} wrap="truncate-end">
          {offset + VISIBLE < rows.length ? ` ↓ ${rows.length - offset - VISIBLE} more` : ' '}
        </Text>

        {keySaving ? (
          <Text color={t.color.muted} wrap="truncate-end">
            saving…
          </Text>
        ) : (
          <OverlayHint t={t}>↑/↓ select · a add · d/x delete · Esc back · q close</OverlayHint>
        )}
      </Box>
    )
  }

  // ── Add endpoint stage ───────────────────────────────────────────────
  if (stage === 'addEndpoint' && provider) {
    const fields: { label: string; name: EndpointField; value: string }[] = [
      { label: 'Label', name: 'label', value: endpointInputs.label },
      { label: 'API key', name: 'api_key', value: '•'.repeat(Math.min(endpointInputs.api_key.length, 40)) },
      { label: 'API base (optional)', name: 'api_base', value: endpointInputs.api_base }
    ]
    const caret = keySaving ? '' : '▎'

    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          Add endpoint to {provider.name}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          A label already in the list replaces that entry · Tab switches field
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        {fields.map(field => {
          const focused = endpointField === field.name

          return (
            <Box flexDirection="column" key={field.name}>
              <Text color={focused ? t.color.accent : t.color.muted} wrap="truncate-end">
                {focused ? '▸ ' : '  '}
                {field.label}:
              </Text>

              <Text color={t.color.accent} wrap="truncate-end">
                {'  '}
                {field.value || '(empty)'}
                {focused ? caret : ''}
              </Text>
            </Box>
          )
        })}

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

        <OverlayHint t={t}>Enter next/add · Tab field · Ctrl+U clear · Esc back</OverlayHint>
      </Box>
    )
  }

  // ── OAuth sign-in stage ──────────────────────────────────────────────
  if (stage === 'oauthLogin' && loginTarget) {
    const command = `raven provider login ${loginTarget.slug.replace(/_/g, '-')}`
    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          Sign in to {loginTarget.name}?
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          This is a browser sign-in. Raven hands the terminal over until it ends:
        </Text>

        <Text color={t.color.accent} wrap="truncate-end">
          {'  '}
          {command}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          Ctrl+C while it runs cancels the sign-in and comes back here.
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {' '}
        </Text>

        {loginError ? (
          <Text color={t.color.label} wrap="truncate-end">
            error: {loginError}
          </Text>
        ) : loginPhase === 'running' ? (
          <Text color={t.color.muted} wrap="truncate-end">
            signing in…
          </Text>
        ) : loginPhase === 'done' && targetStillUnauthenticated ? (
          <Text color={t.color.label} wrap="truncate-end">
            sign-in ended, but Raven still finds no credentials for it.
          </Text>
        ) : loginPhase === 'done' ? (
          <Text color={t.color.muted} wrap="truncate-end">
            signed in.
          </Text>
        ) : (
          <Text color={t.color.muted} wrap="truncate-end">
            {' '}
          </Text>
        )}

        {loginPhase === 'running' ? (
          <Text color={t.color.muted} wrap="truncate-end">
            {' '}
          </Text>
        ) : (
          <OverlayHint t={t}>
            {loginPhase === 'done' ? 'Esc back · Enter run it again' : 'Enter sign in · Esc back'}
          </OverlayHint>
        )}
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

        {/* The model id survives the disconnect and still names this provider, so
            the session is left pointing at one that cannot answer. */}
        {provider.is_current ? (
          <Text color={t.color.label} wrap="truncate-end">
            It serves your current model - pick another one after disconnecting.
          </Text>
        ) : null}

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
        // The id stays: it is what gets stored, and a user comparing it against
        // a vendor's docs needs to see it. The name goes first because that is
        // what someone choosing a model is reading for.
        const label = provider?.model_labels?.[row]?.label
        const text = label && label !== row ? `${label} · ${row}` : row

        return (
          <Text
            bold={modelIdx === idx}
            color={modelIdx === idx ? t.color.accent : t.color.muted}
            inverse={modelIdx === idx}
            key={`${provider?.slug ?? 'prov'}:${idx}:${row}`}
            wrap="truncate-end"
          >
            {prefix}
            {idx + 1}. {text}
          </Text>
        )
      })}

      <Text color={t.color.muted} wrap="truncate-end">
        {offset + VISIBLE < models.length ? ` ↓ ${models.length - offset - VISIBLE} more` : ' '}
      </Text>

      {/* One line about the highlighted model. Blank rather than absent, so the
          list below does not jump as the cursor moves between a model the
          catalogue describes and one it does not. */}
      <Text color={t.color.label} wrap="truncate-end">
        {provider?.model_labels?.[models[modelIdx] ?? '']?.description ?? ' '}
      </Text>

      <Text color={t.color.muted} wrap="truncate-end">
        scope: global
      </Text>
      <OverlayHint t={t}>
        {models.length
          ? '↑/↓ select · Enter switch · a add · d/x delete · e endpoints · Esc back'
          : 'a add model · e endpoints · Enter/Esc back · q close'}
      </OverlayHint>
    </Box>
  )
}

interface ModelPickerProps {
  gw: GatewayClient
  launcher: (args: string[]) => Promise<LaunchResult>
  onCancel: () => void
  onSelect: (model: string, providerSlug: string) => void
  sessionId: string | null
  suspend: (run: RunExternalProcess) => Promise<void>
  t: Theme
}
