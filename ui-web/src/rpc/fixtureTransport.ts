import type { ParamsOf, ResultOf, RpcMethod } from './generated'
import type { ConnectionState, NotificationHandler, RpcTransport } from './transport'

import { RpcError } from './transport'

type Responder<M extends RpcMethod> = ResultOf<M> | ((params: ParamsOf<M>) => ResultOf<M> | Promise<ResultOf<M>>)

export type Fixtures = { [M in RpcMethod]?: Responder<M> }

/**
 * A transport fed from recorded responses -- the whole UI runs with no raven
 * behind it. This is what base.html's demo shell was for (design iteration,
 * screenshots, tests); here it is also the test harness: Vitest drives the
 * real components through the same interface production uses.
 */
export class FixtureTransport implements RpcTransport {
  private readonly handlers = new Map<string, Set<NotificationHandler>>()
  private readonly stateListeners = new Set<(s: ConnectionState) => void>()
  private state: ConnectionState = 'closed'
  /** Every call made, in order -- lets a test assert on traffic. */
  readonly calls: Array<{ method: RpcMethod; params: unknown }> = []

  constructor(private readonly fixtures: Fixtures) {}

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

  on(method: string, handler: NotificationHandler): () => void {
    const set = this.handlers.get(method) ?? new Set()
    set.add(handler)
    this.handlers.set(method, set)
    return () => set.delete(handler)
  }

  /** Test hook: push a server-style notification into the app. */
  emit(method: string, params: unknown): void {
    for (const h of this.handlers.get(method) ?? []) {
      h(params)
    }
  }

  onState(listener: (state: ConnectionState) => void): () => void {
    this.stateListeners.add(listener)
    listener(this.state)
    return () => this.stateListeners.delete(listener)
  }

  /** Test hook: simulate a connection-state transition. */
  setState(next: ConnectionState): void {
    this.state = next
    for (const l of this.stateListeners) {
      l(next)
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
