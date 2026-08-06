// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { useEffect } from 'react'

import type { GatewayClient } from '../gatewayClientStub.js'
import type { ConfigFullResponse } from '../gatewayTypes.js'

import { resolveDetailsMode, resolveSections } from '../domain/details.js'
import { DEFAULT_VOICE_RECORD_KEY, type ParsedVoiceRecordKey, parseVoiceRecordKey } from '../lib/platform.js'
import { asRpcResult } from '../lib/rpc.js'
import {
  type BusyInputMode,
  DEFAULT_INDICATOR_STYLE,
  INDICATOR_STYLES,
  type IndicatorStyle,
  type StatusBarMode
} from './interfaces.js'
import { patchUiState } from './uiStore.js'

const STATUSBAR_ALIAS: Record<string, StatusBarMode> = {
  bottom: 'bottom',
  off: 'off',
  on: 'top',
  top: 'top'
}

export const normalizeStatusBar = (raw: unknown): StatusBarMode =>
  raw === false
    ? 'off'
    : raw === true
      ? 'top'
      : typeof raw === 'string'
        ? (STATUSBAR_ALIAS[raw.trim().toLowerCase()] ?? 'bottom')
        : 'bottom'

const BUSY_MODES = new Set<BusyInputMode>(['interrupt', 'queue', 'steer'])

// `queue` rather than `interrupt`: in a full-screen TUI you're
// typically authoring the next prompt while the agent is still
// streaming, and an unintended interrupt loses work.  Set
// `display.busy_input_mode: interrupt` (or `steer`) explicitly to opt
// out per-config.
const TUI_BUSY_DEFAULT: BusyInputMode = 'queue'

export const normalizeBusyInputMode = (raw: unknown): BusyInputMode => {
  if (typeof raw !== 'string') {
    return TUI_BUSY_DEFAULT
  }

  const v = raw.trim().toLowerCase() as BusyInputMode

  return BUSY_MODES.has(v) ? v : TUI_BUSY_DEFAULT
}

const INDICATOR_STYLE_SET: ReadonlySet<IndicatorStyle> = new Set(INDICATOR_STYLES)

export const normalizeIndicatorStyle = (raw: unknown): IndicatorStyle => {
  if (typeof raw !== 'string') {
    return DEFAULT_INDICATOR_STYLE
  }

  const v = raw.trim().toLowerCase() as IndicatorStyle

  return INDICATOR_STYLE_SET.has(v) ? v : DEFAULT_INDICATOR_STYLE
}

const FALSEY_MOUSE = new Set(['0', 'false', 'no', 'off'])
const hasOwn = (obj: object, key: PropertyKey) => Object.prototype.hasOwnProperty.call(obj, key)

export const normalizeMouseTracking = (display: { mouse_tracking?: unknown; tui_mouse?: unknown }): boolean => {
  const raw = hasOwn(display, 'mouse_tracking') ? display.mouse_tracking : display.tui_mouse

  if (raw === false || raw === 0) {
    return false
  }

  return typeof raw === 'string' ? !FALSEY_MOUSE.has(raw.trim().toLowerCase()) : true
}

const quietRpc = async <T extends object = Record<string, unknown>>(
  gw: GatewayClient,
  method: string,
  params: Record<string, unknown> = {}
): Promise<null | T> => {
  try {
    return asRpcResult<T>(await gw.request<T>(method, params))
  } catch {
    return null
  }
}

const _voiceRecordKeyFromConfig = (cfg: ConfigFullResponse | null): ParsedVoiceRecordKey => {
  const raw = cfg?.config?.voice?.record_key

  return raw ? parseVoiceRecordKey(raw) : DEFAULT_VOICE_RECORD_KEY
}

/** Fetch ``config.get full`` and fan the result through ``applyDisplay``.
 *
 * Extracted so the fetch/apply plumbing can be exercised by the test suite
 * without a React runtime, and a regression in it fails a test rather than only
 * showing up at runtime. */
export async function hydrateFullConfig(
  gw: GatewayClient,
  setBell: (v: boolean) => void,
  setVoiceRecordKey?: (v: ParsedVoiceRecordKey) => void
): Promise<ConfigFullResponse | null> {
  const cfg = await quietRpc<ConfigFullResponse>(gw, 'config.get', { key: 'full' })
  applyDisplay(cfg, setBell, setVoiceRecordKey)

  return cfg
}

export const applyDisplay = (
  cfg: ConfigFullResponse | null,
  setBell: (v: boolean) => void,
  setVoiceRecordKey?: (v: ParsedVoiceRecordKey) => void
) => {
  const d = cfg?.config?.display ?? {}

  setBell(!!d.bell_on_complete)

  // Only push the voice record key when the RPC actually returned a config
  // payload. ``quietRpc()`` collapses failures to ``null``; resetting the cached
  // shortcut on every null would clobber a custom binding after one transient
  // RPC error, so staying silent preserves the last-good state.
  if (setVoiceRecordKey && cfg) {
    setVoiceRecordKey(_voiceRecordKeyFromConfig(cfg))
  }

  patchUiState({
    busyInputMode: normalizeBusyInputMode(d.busy_input_mode),
    compact: !!d.tui_compact,
    detailsMode: resolveDetailsMode(d),
    detailsModeCommandOverride: false,
    indicatorStyle: normalizeIndicatorStyle(d.tui_status_indicator),
    inlineDiffs: d.inline_diffs !== false,
    // Only when the config says something. Every other field here would fall back
    // to the value the store already holds, but mouse tracking does not: its
    // default comes from RAVEN_TUI_DISABLE_MOUSE, and normalizing a silent config
    // to `true` turned the environment's opt-out back on at startup.
    //
    // Note the conditional is what makes that safe rather than the fetch: the
    // request below asks `config.get {key:'full'}` and the handler reads `keys`
    // (plural), so no display block is served at all today and this whole block
    // runs on `{}`. The mismatch predates this file's changes -- see the PR.
    ...(hasOwn(d, 'mouse_tracking') || hasOwn(d, 'tui_mouse') ? { mouseTracking: normalizeMouseTracking(d) } : {}),
    sections: resolveSections(d.sections),
    showCost: !!d.show_cost,
    showReasoning: d.show_reasoning !== false,
    statusBar: normalizeStatusBar(d.tui_statusbar),
    streaming: d.streaming !== false
    // NOTE: `transcript` is intentionally NOT synced here. It is a runtime-only
    // session flag toggled by `/transcript` (`display` is not a persisted config
    // block), so hydrating it would revert the user's choice.
  })
}

export function useConfigSync({
  gw,
  setBellOnComplete,
  setVoiceEnabled,
  setVoiceRecordKey,
  sid
}: UseConfigSyncOptions) {
  useEffect(() => {
    if (!sid) {
      return
    }

    // Keep startup cheap: voice.toggle status probes optional audio/STT deps and
    // can run long enough to delay prompt.submit on the single stdio RPC pipe.
    // Environment flags are enough to initialize the UI bit; the heavier status
    // check still runs when the user opens /voice.
    setVoiceEnabled(process.env.RAVEN_VOICE === '1')
    void hydrateFullConfig(gw, setBellOnComplete, setVoiceRecordKey)
  }, [gw, setBellOnComplete, setVoiceEnabled, setVoiceRecordKey, sid])
}

export interface UseConfigSyncOptions {
  gw: GatewayClient
  setBellOnComplete: (v: boolean) => void
  setVoiceEnabled: (v: boolean) => void
  setVoiceRecordKey?: (v: ParsedVoiceRecordKey) => void
  sid: null | string
}
