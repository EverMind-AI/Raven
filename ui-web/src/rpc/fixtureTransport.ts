import type { ParamsOf, ResultOf, RpcMethod } from './generated'
import type { PushMethod } from './notifications'
import type {
  BinaryHandler,
  ConnectionState,
  NotificationHandler,
  RpcTransport,
  StateInfo,
  StateListener,
} from './transport'

import { RpcError } from './transport'

type Responder<M extends RpcMethod> = ResultOf<M> | ((params: ParamsOf<M>) => ResultOf<M> | Promise<ResultOf<M>>)

export type Fixtures = { [M in RpcMethod]?: Responder<M> }

/* What a responder is given instead of the ambient clock and the ambient
   timer. Every time field a fixture answers comes from `now()`, so two passes
   over the same fixtures with the same `now` are byte-identical
   (scripts/gates/fixture-now.test.mjs), and a scripted conversation pushes its
   frames through `emit` on `schedule`'s clock -- which is how the offline page
   plays a turn out over time through the same pipeline the live page uses. */
export interface FixtureEnv {
  now(): number
  emit(method: PushMethod, params: unknown): void
  schedule(ms: number, fn: () => void): void
}

/** A fixture library that needs the env -- a clock, or frames to push later. */
export type FixtureBuilder = (env: FixtureEnv) => Fixtures

export interface FixtureOptions {
  /** The clock every fixture time field is derived from. */
  now?: () => number
  /** Where a scheduled emission goes; a test hands in its own queue. */
  timer?: (ms: number, fn: () => void) => void
}

/**
 * A transport fed from recorded responses -- the whole UI runs with no raven
 * behind it. This is what base.html's demo shell was for (design iteration,
 * screenshots, tests); here it is also the test harness: Vitest drives the
 * real components through the same interface production uses.
 */
export class FixtureTransport implements RpcTransport {
  private readonly handlers = new Map<string, Set<NotificationHandler>>()
  private readonly binaryHandlers = new Set<BinaryHandler>()
  private readonly stateListeners = new Set<StateListener>()
  private state: ConnectionState = 'closed'
  /** Every call made, in order -- lets a test assert on traffic. */
  readonly calls: Array<{ method: string; params: unknown }> = []

  private readonly fixtures: Fixtures
  private readonly clock: () => number
  private readonly timer: (ms: number, fn: () => void) => void

  /* Read by the fixtures themselves, so the library can answer a time and push
     a frame without reaching for a global. */
  readonly env: FixtureEnv = {
    now: () => this.clock(),
    emit: (method, params) => this.emit(method, params),
    schedule: (ms, fn) => this.timer(ms, fn),
  }

  constructor(fixtures: Fixtures | FixtureBuilder, options: FixtureOptions = {}) {
    this.clock = options.now ?? (() => Date.now())
    this.timer = options.timer ?? ((ms, fn) => { setTimeout(fn, ms) })
    this.fixtures = typeof fixtures === 'function' ? fixtures(this.env) : fixtures
  }

  async call<M extends RpcMethod>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>> {
    this.calls.push({ method, params })
    // Widened to unknown before the typeof check: narrowing a function/value
    // union distributed over 128 method entries blows TS's complexity budget.
    const responder: unknown = this.fixtures[method]
    if (responder === undefined) {
      throw new RpcError(-32601, `fixture: no response recorded for ${method}`)
    }
    if (typeof responder === 'function') {
      return await (responder as (p: ParamsOf<M>) => ResultOf<M> | Promise<ResultOf<M>>)(params)
    }
    return responder as ResultOf<M>
  }

  async callUnchecked(method: string, params: Record<string, unknown>): Promise<unknown> {
    this.calls.push({ method, params })
    const responder: unknown = (this.fixtures as Record<string, unknown>)[method]
    if (responder === undefined) {
      throw new RpcError(-32601, `fixture: no response recorded for ${method}`)
    }
    if (typeof responder === 'function') {
      return await (responder as (p: Record<string, unknown>) => unknown)(params)
    }
    return responder
  }

  on(method: PushMethod, handler: NotificationHandler): () => void {
    const set = this.handlers.get(method) ?? new Set()
    set.add(handler)
    this.handlers.set(method, set)
    return () => set.delete(handler)
  }

  binary(handler: BinaryHandler): () => void {
    this.binaryHandlers.add(handler)
    return () => this.binaryHandlers.delete(handler)
  }

  /** Test hook: push a binary frame into the app. */
  emitBinary(buf: ArrayBuffer): void {
    for (const h of this.binaryHandlers) {
      h(buf)
    }
  }

  /** Push a server-style notification into the app, now. */
  emit(method: string, params: unknown): void {
    for (const h of this.handlers.get(method) ?? []) {
      h(params)
    }
  }

  onState(listener: StateListener): () => void {
    this.stateListeners.add(listener)
    listener(this.state)
    return () => this.stateListeners.delete(listener)
  }

  /** Test hook: simulate a connection-state transition. */
  setState(next: ConnectionState, info?: StateInfo): void {
    this.state = next
    for (const l of this.stateListeners) {
      l(next, info)
    }
  }

  async connect(): Promise<boolean> {
    this.setState('open')
    return true
  }

  close(): void {
    this.setState('closed')
  }
}
