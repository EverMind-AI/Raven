// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Stub fixtures for GatewayClientStub. Used by `raven tui` to render the
// hermes UI shell without any Python backend or IPC. When `tui-ipc-bridge` L2
// lands real RPC dispatch, this entire module is deleted in a single commit.

import type {
  CommandsCatalogResponse,
  ConfigFullResponse,
  ConfigGetValueResponse,
  ConfigMtimeResponse,
  GatewaySkin,
  GatewayTranscriptMessage,
  ModelOptionsResponse,
  SessionListItem,
  SessionListResponse,
  SessionResumeResponse,
  SetupStatusResponse,
} from '../gatewayTypes.js'
import type { SessionInfo } from '../types.js'

const MOCK_SESSION_ID = 'mock-session-1'
const MOCK_STARTED_AT = 1735689600 // 2025-01-01T00:00:00Z (stable for tests)

export const STUB_SESSION_INFO: SessionInfo = {
  model: 'claude-sonnet-4-6',
  skills: {},
  tools: {},
  // Raven Agent fork: independent version line, "X commits behind" semantic n/a.
  update_behind: null,
}

export const STUB_SESSION_LIST_ITEM: SessionListItem = {
  id: MOCK_SESSION_ID,
  message_count: 3,
  preview: 'hello / I am a stub.',
  started_at: MOCK_STARTED_AT,
  title: 'Mock Session',
}

export const STUB_SESSION_LIST: SessionListResponse = {
  sessions: [STUB_SESSION_LIST_ITEM],
}

export const STUB_MESSAGES: GatewayTranscriptMessage[] = [
  { role: 'system', text: 'Raven Agent TUI stub mode — no real backend attached.' },
  { role: 'user', text: 'hello' },
  { role: 'assistant', text: 'I am a stub.' },
]

export const STUB_SESSION_RESUME: SessionResumeResponse = {
  info: STUB_SESSION_INFO,
  message_count: STUB_MESSAGES.length,
  messages: STUB_MESSAGES,
  session_id: MOCK_SESSION_ID,
}

const STUB_SLASH_PAIRS: [string, string][] = [
  ['/help', 'show stub help'],
  ['/exit', 'exit TUI'],
  ['/clear', 'clear screen'],
]

export const STUB_COMMANDS_CATALOG: CommandsCatalogResponse = {
  canon: {},
  categories: [{ name: 'Core', pairs: STUB_SLASH_PAIRS }],
  pairs: STUB_SLASH_PAIRS,
  skill_count: 0,
  sub: {},
}

export const STUB_MODEL_OPTIONS: ModelOptionsResponse = {
  model: 'claude-sonnet-4-6',
  provider: 'anthropic',
  providers: [
    {
      authenticated: true,
      is_current: true,
      models: ['claude-sonnet-4-6'],
      name: 'Anthropic',
      slug: 'anthropic',
    },
  ],
}

export const STUB_SKIN: GatewaySkin = {}

// `tui_auto_resume_recent: true` so on `gateway.ready` the startup flow goes
// session.most_recent → resumeById(mock-session-1) → session.resume (which
// returns STUB_MESSAGES, 3 transcript messages). Without this the flow falls
// to newSession() → session.create which returns an empty transcript and
// Banner / mock messages never render. REQ-9 acceptance requires the 3-message
// mock chat to be visible.
export const STUB_CONFIG_FULL: ConfigFullResponse = {
  config: {
    display: { tui_auto_resume_recent: true },
    voice: {},
  },
}

export const STUB_SESSION_CREATE = {
  info: STUB_SESSION_INFO,
  session_id: MOCK_SESSION_ID,
}

export const STUB_CONFIG_MTIME: ConfigMtimeResponse = { mtime: 0 }

export const STUB_CONFIG_GET_SKIN: ConfigGetValueResponse = { value: '' }

export const STUB_SETUP_STATUS: SetupStatusResponse = { provider_configured: true }
