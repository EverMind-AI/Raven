import type { ParamsOf, ResultOf, RpcMethod } from './generated'

export class RpcError extends Error {
  constructor(
    readonly code: number,
    message: string,
    readonly data?: unknown
  ) {
    super(message)
    this.name = 'RpcError'
  }
}

export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'auth-failed' | 'closed'

export type NotificationHandler = (params: unknown) => void

/**
 * The typed end state of the DataSource seam. Two implementations planned:
 *
 * - a WebSocket transport speaking JSON-RPC over /rpc to a live raven
 *   (lands with the first migrated feature, where it gets a consumer);
 * - `FixtureTransport` answering from recorded responses, no backend at all.
 *
 * The page's best idea -- a UI that runs without its engine (the demo layer
 * in ui-web/src/demo/) -- kept, with the checks turned on: today ui-web/src/live/
 * assigns over demo globals by name at runtime and DS entries are untyped,
 * so a rename in one layer breaks the other silently. An interface both
 * sides compile against is the same capability made checkable, and it is
 * what each feature's `source.ts` will be written against as it leaves the
 * concatenated script.
 */
export interface RpcTransport {
  /** Call a contract method. Name, params and result all come from the contract. */
  call<M extends RpcMethod>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>>
  /**
   * Attach a handler for a server-pushed notification method. Returns the
   * detach function. Names stay plain strings: the contract declares calls,
   * not pushes.
   */
  on(method: string, handler: NotificationHandler): () => void
  /** Observe connection-state changes (fires immediately with the current state). */
  onState(listener: (state: ConnectionState) => void): () => void
  connect(): Promise<boolean>
  close(): void
}
