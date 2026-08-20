// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// GatewayClientStub — in-memory test double for GatewayClient, and the
// nominal home of the `GatewayClient` interface type that `app.tsx` /
// `useMainApp.ts` / the component tree import as `import type`. Nothing in
// production constructs this class; `entry.tsx` always builds
// `GatewayClientCompat` (the real RPC path). Tests construct the stub to
// drive the UI without a Python backend: `start() / drain() /
// request<T>(method, params) / kill() / getLogTail(limit?)` plus
// EventEmitter `event` and `exit` channels, every RPC answered from a
// constant fixture in `./lib/stubGatewayFixtures.js`. Unknown methods log
// once and resolve to `{}` so the UI renders empty data instead of
// crashing. (An earlier header promised deletion "when real IPC lands";
// real IPC landed and the type + test-double roles keep this file alive.)

import { EventEmitter } from 'node:events'

import type { GatewayEvent, ModelOptionProvider } from './gatewayTypes.js'

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
  STUB_SKIN
} from './lib/stubGatewayFixtures.js'

const DELAY_MS = 50
const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

const stubProvider = (slug: unknown): ModelOptionProvider | undefined =>
  STUB_MODEL_OPTIONS.providers?.find(p => p.slug === slug)

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

      case 'model.save_key': {
        const base = stubProvider(params.slug) ?? { name: String(params.slug ?? ''), slug: String(params.slug ?? '') }

        return { provider: { ...base, authenticated: true } } as unknown as T
      }

      case 'model.disconnect':
        return { disconnected: true } as unknown as T

      case 'model.add_model': {
        const base = stubProvider(params.slug) ?? { name: String(params.slug ?? ''), slug: String(params.slug ?? '') }
        const model = String(params.model ?? '')
        const models = base.models?.includes(model) ? base.models : [...(base.models ?? []), model]

        return { provider: { ...base, models, total_models: models.length } } as unknown as T
      }

      case 'model.remove_model': {
        const base = stubProvider(params.slug) ?? { name: String(params.slug ?? ''), slug: String(params.slug ?? '') }
        const model = String(params.model ?? '')
        const models = (base.models ?? []).filter(m => m !== model)

        return { provider: { ...base, models, total_models: models.length } } as unknown as T
      }

      case 'config.set':
        if (params.key === 'model') {
          const value = String(params.value ?? '')

          return { applied: true, previous: STUB_MODEL_OPTIONS.model ?? null, value } as unknown as T
        }

        return {} as T

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

// The `GatewayClient` interface type, under the name every consumer uses:
// app.tsx, useMainApp.ts and the rest import `type { GatewayClient }` from
// this file and typecheck structurally against it — `GatewayClientCompat`
// (the production wiring) satisfies it without importing it. The alias
// stays because renaming it is churn across ~30 consumer sites with no
// payoff; like the class above, it has no pending deletion.
export type GatewayClient = GatewayClientStub
