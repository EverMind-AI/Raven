// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// GatewayClientCompat — EventEmitter-shaped adapter wrapping the typed
// production RpcClient. Provides the legacy GatewayClient API surface
// (`request<T>(method, params)`, `start()`, `kill()`, `getLogTail()`,
// `drain()`, `.on('event'/'exit', handler)`) so the hermes-fork-imported
// ui-tui can use the typed RpcClient backend without rewriting all
// 169 components.
//
// Lifetime: temporary glue. Retire when Phase 4 turn-streaming lands and
// useMainApp.ts is rewired to typed `rpcClient.subscribe(...)` calls
// (per proposal §4.2 follow-up `tui-chat-streaming`).
//
// Adapter responsibilities:
//   1. EventEmitter for 'event' (GatewayEvent) and 'exit' (number) channels.
//   2. Synthesize `gateway.ready` event with `STUB_SKIN` (re-exported from
//      `lib/stubGatewayFixtures.ts`) on next-tick after `start()` resolves
//      the RpcClient's `system.hello` handshake — same shape as
//      `GatewayClientStub.start()` so 169 .tsx consumers boot identically.
//   3. `request<T>(m, p)` delegates to `rpc<T>(m, p)`.
//   4. `kill()` → `RpcClient.close()` + emit 'exit'.
//   5. `getLogTail()` → placeholder string (no buffered stdout in real path).
//   6. `drain()` → flush pre-subscriber buffered events from `bufferedEvents`,
//      matching `GatewayClientStub.drain()` semantics so the boot-time
//      `gateway.ready` signal is not lost when `useMainApp.ts` attaches its
//      `.on('event', ...)` handler after `start()`.
//
// API surface MUST stay structurally compatible with `GatewayClientStub` —
// `app.tsx` / `useMainApp.ts` / `useConfigSync.ts` / etc. import
// `type { GatewayClient } from './gatewayClientStub.js'` and TypeScript
// structurally typechecks the instance passed via `<App gw={...} />`.

import { EventEmitter } from 'node:events'

import type { GatewayEvent } from './gatewayTypes.js'

import { STUB_SKIN } from './lib/stubGatewayFixtures.js'
import { RpcClient } from './rpc/index.js'

const CLIENT_VERSION = '0.0.2'
// Mirrors the negotiated capability list sent during `system.hello`.
// Server-side does not yet enforce these, but we send the canonical set
// so future capability negotiation has a stable baseline.
const CLIENT_CAPABILITIES: string[] = ['cli-dispatch', 'cli', 'config', 'stubs']

export interface GatewayClientCompatOptions {
  /** Unix socket path. Defaults to env `RAVEN_RPC_SOCKET`. */
  socketPath?: string
  /** Injected RpcClient (test seam). Owns its own lifecycle when supplied. */
  rpcClient?: RpcClient
}

export class GatewayClientCompat extends EventEmitter {
  // `rpcClient` is exposed so Phase 6's typed chat path
  // (`createChatStream` / `useChatStream`) can subscribe to `turn.*` events
  // directly without going through the EventEmitter adapter surface, while
  // the legacy 169-component bus continues to consume `gw.on('event', ...)`.
  // Per `cross-language-rpc-adapter-pattern.md` §三, the adapter retires
  // file-by-file as consumers migrate; sharing this single RpcClient between
  // both paths keeps the socket count at one and avoids handshake races.
  public readonly rpcClient: RpcClient
  private bufferedEvents: GatewayEvent[] = []
  private subscribed = false
  private startPromise: Promise<void> | null = null
  private killed = false

  constructor(opts: GatewayClientCompatOptions = {}) {
    super()
    this.setMaxListeners(0)
    this.rpcClient =
      opts.rpcClient ??
      new RpcClient({
        // Bridge first-class top-level notifications (confirm.request, and any
        // future sudo/secret/clarify the backend emits) onto the legacy
        // `'event'` bus that `createGatewayEventHandler` consumes. Subscription
        // -stream `event` notifications keep flowing through the typed registry.
        onNotification: (method, params) => this.publishServerNotification(method, params),
        socketPath: opts.socketPath
      })
  }

  /**
   * Map a top-level server notification (`{method, params}`) to a
   * `GatewayEvent` (`{type, payload}`) and publish it. `createGatewayEventHandler`
   * switches on `type` and ignores unknown ones, so this is forward-compatible.
   */
  private publishServerNotification(method: string, params: unknown): void {
    this.publish({ payload: params, type: method } as GatewayEvent)
  }

  /**
   * Boot sequence:
   *   1. await `system.hello` (5s server-side timeout per Phase 2 RpcServer).
   *   2. synthesize `gateway.ready` event with `STUB_SKIN`.
   *   3. buffer (or emit, if `drain()` already called) the event.
   *
   * Idempotent — repeated `start()` returns the same promise, matching
   * `GatewayClientStub.start()` (which is a no-op on second call beyond
   * resetting state). The first call wins.
   */
  start(): Promise<void> {
    if (this.startPromise) {
      return this.startPromise
    }

    this.startPromise = (async () => {
      await this.rpcClient.rpc('system.hello', {
        client_version: CLIENT_VERSION,
        client_capabilities: CLIENT_CAPABILITIES
      })
      // Defer event publish so `useMainApp.ts`'s useEffect (which attaches
      // `.on('event', ...)` then calls `drain()`) can mount before we
      // publish. Without this `setTimeout(0)` the React effect could miss
      // the event — same trick `GatewayClientStub.start()` uses.
      setTimeout(() => {
        if (this.killed) {
          return
        }

        const ready: GatewayEvent = { payload: { skin: STUB_SKIN }, type: 'gateway.ready' }
        this.publish(ready)
      }, 0)
    })()

    return this.startPromise
  }

  /**
   * Flush buffered events to subscribers. Mirrors `GatewayClientStub.drain()`:
   * the first call switches the publish path from "buffer" to "emit
   * directly", then replays everything that was buffered while not
   * subscribed.
   */
  drain(): void {
    this.subscribed = true
    const queued = this.bufferedEvents
    this.bufferedEvents = []

    for (const ev of queued) {
      this.emit('event', ev)
    }
  }

  /** Invoke an RPC method. Pure delegate to `RpcClient.rpc()`. */
  async request<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    return this.rpcClient.rpc<T>(method, params)
  }

  /**
   * Tear down: close the RPC socket, emit `exit` so `useMainApp.ts` clears
   * UI state. Safe to call multiple times.
   */
  kill(): void {
    if (this.killed) {
      return
    }

    this.killed = true
    this.bufferedEvents = []
    this.subscribed = false
    try {
      this.rpcClient.close()
    } finally {
      this.emit('exit', 0)
    }
  }

  /**
   * Placeholder — the real gateway path has no buffered child-process stdout
   * to tail (server logs are written by Python `loguru` to its own files).
   * Hermes UI's `/logs` slash command treats this as a no-data signal.
   */
  getLogTail(_limit = 20): string {
    return '(real gateway via RPC — see Python loguru sink for server logs)'
  }

  private publish(ev: GatewayEvent): void {
    if (this.subscribed) {
      this.emit('event', ev)
    } else {
      this.bufferedEvents.push(ev)
    }
  }
}

// Backwards-compat type alias mirroring `gatewayClientStub.ts`'s alias.
// Most consumers import `type { GatewayClient } from './gatewayClientStub.js'`,
// not from this file — TypeScript structurally typechecks the runtime
// instance against `GatewayClientStub`, and `GatewayClientCompat` has the
// same public surface. The alias here is exported for any future consumer
// that wants to switch its type import to the adapter directly.
export type GatewayClient = GatewayClientCompat
