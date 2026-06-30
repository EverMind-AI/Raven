import { atom, computed } from 'nanostores'

import type { GatewaySkin } from '../gatewayTypes.js'

import { MOUSE_TRACKING } from '../config/env.js'
import { ZERO } from '../domain/usage.js'
import { applyDetectedBackground, DEFAULT_THEME, fromSkin, resolveCurrentDefaultTheme } from '../theme.js'
import { DEFAULT_INDICATOR_STYLE, type UiState } from './interfaces.js'

const buildUiState = (): UiState => ({
  bgTasks: new Set(),
  busy: false,
  busyInputMode: 'queue',
  compact: false,
  escapeArmed: false,
  detailsMode: 'collapsed',
  detailsModeCommandOverride: false,
  indicatorStyle: DEFAULT_INDICATOR_STYLE,
  info: null,
  inlineDiffs: true,
  mouseTracking: MOUSE_TRACKING,
  sections: {},
  showCost: false,
  // Default ON so reasoning models (deepseek-v4-pro / qwen /
  // o-series) show their thinking stream out of the box instead of leaving
  // the user staring at a silent screen for 1-4 min. Toggle via /thinking.
  showReasoning: true,
  sid: null,
  status: 'summoning raven…',
  statusBar: 'bottom',
  streaming: true,
  theme: DEFAULT_THEME,
  usage: ZERO
})

export const $uiState = atom<UiState>(buildUiState())

export const $uiTheme = computed($uiState, state => state.theme)
export const $uiSessionId = computed($uiState, state => state.sid)

export const getUiState = () => $uiState.get()

export const patchUiState = (next: Partial<UiState> | ((state: UiState) => UiState)) =>
  $uiState.set(typeof next === 'function' ? next($uiState.get()) : { ...$uiState.get(), ...next })

export const resetUiState = () => $uiState.set(buildUiState())

// Last skin pushed by the gateway, retained so a late terminal-background
// probe can rebuild the theme under the corrected light/dark scheme.
let lastSkin: GatewaySkin | null = null

const buildSkinTheme = (s: GatewaySkin) =>
  fromSkin(
    s.colors ?? {},
    s.branding ?? {},
    s.banner_logo ?? '',
    s.banner_hero ?? '',
    s.tool_prefix ?? '',
    s.help_header ?? ''
  )

export const applySkinTheme = (s: GatewaySkin) => {
  lastSkin = s
  patchUiState({ theme: buildSkinTheme(s) })
}

/**
 * Fold an OSC 11 background-color reply into the theme. When it flips the
 * detected light/dark scheme, rebuild the active theme — from the last skin
 * if one has arrived, else the curated per-scheme palette — so the whole UI
 * re-themes. No-ops when the scheme is unchanged or the reply is unparseable.
 */
export const applyTerminalBackground = (oscData: string) => {
  const res = applyDetectedBackground(oscData)

  if (res?.changed) {
    patchUiState({ theme: lastSkin ? buildSkinTheme(lastSkin) : resolveCurrentDefaultTheme() })
  }

  return res
}
