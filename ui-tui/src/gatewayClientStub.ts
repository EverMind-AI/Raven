// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// GatewayClientStub — drop-in replacement for GatewayClient when there is no
// Python backend. Public interface matches GatewayClient (see
// gatewayClient.original.ts.removed in this same directory for the original):
// `start() / drain() / request<T>(method, params) / kill() / getLogTail(limit?)`
// plus EventEmitter `event` and `exit` channels. Internally there are no
// subprocesses, sockets, or filesystem reads — every RPC dispatches to a
// constant fixture from `./lib/stubGatewayFixtures.js`. Unknown methods log
// once and resolve to `{}` so the UI renders empty data instead of crashing.
// Deleted in a single commit when `tui-ipc-bridge` L2 lands real IPC.

import { EventEmitter } from 'node:events'

import type { GatewayEvent } from './gatewayTypes.js'
import {
  STUB_COMMANDS_CATALOG,
  STUB_CONFIG_FULL,
  STUB_CONFIG_GET_SKIN,
  STUB_CONFIG_MTIME,
  STUB_MODEL_OPTIONS,
  STUB_SESSION_CREATE,
  STUB_SESSION_LIST,
  STUB_SESSION_RESUME,
  STUB_SETUP_STATUS,
  STUB_SKIN,
} from './lib/stubGatewayFixtures.js'

const DELAY_MS = 50
const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

export class GatewayClientStub extends EventEmitter {
  private bufferedEvents: GatewayEvent[] = []
  private subscribed = false
  private warnedMethods = new Set<string>()

  constructor() {
    super()
    this.setMaxListeners(0)
  }

  start(): void {
    // Reset state so a fresh start() after kill() works.
    this.bufferedEvents = []
    this.subscribed = false

    // Defer the ready event so callers have a chance to .on('event', ...)
    // and then .drain() before the event fires — same shape as the original
    // gateway, which buffers events until drain().
    setTimeout(() => {
      const ready: GatewayEvent = { payload: { skin: STUB_SKIN }, type: 'gateway.ready' }
      this.publish(ready)
    }, 0)
  }

  drain(): void {
    this.subscribed = true
    const queued = this.bufferedEvents
    this.bufferedEvents = []

    for (const ev of queued) {
      this.emit('event', ev)
    }
  }

  async request<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    await delay(DELAY_MS)

    switch (method) {
      case 'session.list':
        return STUB_SESSION_LIST as unknown as T

      case 'session.resume':
        return STUB_SESSION_RESUME as unknown as T

      case 'session.create':
        return STUB_SESSION_CREATE as unknown as T

      case 'session.most_recent':
        return { session_id: 'mock-session-1', started_at: 1735689600, title: 'Mock Session' } as unknown as T

      case 'commands.catalog':
        return STUB_COMMANDS_CATALOG as unknown as T

      case 'model.options':
        return STUB_MODEL_OPTIONS as unknown as T
      case 'config.get':
        switch (params?.key) {
          case 'full':
            return STUB_CONFIG_FULL as unknown as T

          case 'mtime':
            return STUB_CONFIG_MTIME as unknown as T

          case 'skin':
            return STUB_CONFIG_GET_SKIN as unknown as T

          default:
            return {} as T
        }

      case 'setup.status':
        return STUB_SETUP_STATUS as unknown as T

      case 'delegation.status':
        return { paused: false, subagents: [] } as unknown as T

      case 'skills.manage':
        return { skills: {} } as unknown as T

      default:
        if (!this.warnedMethods.has(method)) {
          this.warnedMethods.add(method)
          console.warn(`[GatewayClientStub] unhandled RPC method: ${method} — returning {}`)
        }

        return {} as T
    }
  }

  kill(): void {
    this.bufferedEvents = []
    this.subscribed = false
  }


  getLogTail(_limit = 20): string {
    return '(stub gateway has no log)'
  }

  private publish(ev: GatewayEvent): void {
    if (this.subscribed) {
      this.emit('event', ev)
    } else {
      this.bufferedEvents.push(ev)
    }
  }
}

// Backwards-compat type alias: hermes consumers (app.tsx, useMainApp.ts, ...)
// import `type { GatewayClient } from './gatewayClientStub.js'` — after the sed
// rewrite (Phase 1 T2.4 / Phase 3 T3.3) they import from this file, but the
// symbol name they use is still `GatewayClient`. Avoids a churn-y rename
// across ~30 consumer sites. Deleted with the rest when tui-ipc-bridge L2
// brings the real GatewayClient back.
export type GatewayClient = GatewayClientStub
