import type { ParamsOf, ResultOf, RpcMethod } from './generated'
import type { PushMethod } from './notifications'

export class RpcError extends Error {
  constructor(
    readonly code: number,
    message: string,
    readonly data?: unknown,
    /** The error frame as it came off the wire, for a caller that needs more. */
    readonly rpc?: unknown
  ) {
    super(message)
    this.name = 'RpcError'
  }
}

export type ConnectionState =
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'reconnected'
  | 'auth-failed'
  | 'closed'

export interface StateInfo {
  /** Rejoin attempts that have failed so far; 0 at the moment of the drop. */
  attempt?: number
}

export type StateListener = (state: ConnectionState, info?: StateInfo) => void

export type NotificationHandler = (params: unknown) => void

export type BinaryHandler = (buf: ArrayBuffer) => void

/**
 * The typed end state of the DataSource seam. Two implementations planned:
 *
 * - a WebSocket transport speaking JSON-RPC over /rpc to a live raven
 *   (lands with the first migrated feature, where it gets a consumer);
 * - `FixtureTransport` answering from recorded responses, no backend at all.
 *
 * The page's best idea -- a UI that runs without its engine -- kept, with the
 * checks turned on: the two layers it used to take assigned over each other's
 * globals by name at runtime and their seam entries were untyped, so a rename
 * in one broke the other silently. An interface both
 * sides compile against is the same capability made checkable, and it is
 * what each feature's `source.ts` will be written against as it leaves the
 * concatenated script.
 */
export interface RpcTransport {
  /** Call a contract method. Name, params and result all come from the contract. */
  call<M extends RpcMethod>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>>
  /**
   * Attach a handler for a server-pushed notification method. Returns the
   * detach function. The contract declares calls, not pushes, so the names
   * come from the hand-written table in ./notifications instead: a misspelt
   * one is a handler that is never called, silently, for the life of the tab.
   */
  on(method: PushMethod, handler: NotificationHandler): () => void
  /**
   * Attach a handler for binary frames (the screencast stream). Returns the
   * detach function.
   */
  binary(handler: BinaryHandler): () => void
  /**
   * The escape hatch for the two method names the page calls that the
   * contract does not declare: `raven.mcp.list` and `raven.mcp.set`. They
   * answer -32601 today and must go on doing so, so they keep their calls
   * here instead of being typed into existence or silently dropped.
   */
  callUnchecked(method: string, params: Record<string, unknown>): Promise<unknown>
  /** Observe connection-state changes (fires immediately with the current state). */
  onState(listener: StateListener): () => void
  connect(): Promise<boolean>
  close(): void
}
