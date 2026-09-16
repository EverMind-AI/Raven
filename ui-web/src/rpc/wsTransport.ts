import type { ParamsOf, ResultOf, RpcMethod } from './generated'
import type {
  BinaryHandler,
  ConnectionState,
  NotificationHandler,
  RpcTransport,
  StateInfo,
  StateListener,
} from './transport'

import { RpcError } from './transport'

/* The live end of the DataSource seam: JSON-RPC 2.0 over one WebSocket to
   /rpc, with the rejoin policy the page runs today. Every behaviour here is
   ported from ui-web/src/live/020-rpc.js.

   What paints stays out of it: the reconnect status line, the upgrade shade,
   the auth banner and the desktop shell's reauth handshake all belong to the
   caller, which drives them off `onState`. */

/* How long a rejoin keeps trying, and how long it ever sleeps between tries.
   The ceiling matches the upgrade watcher's (20 minutes) on purpose: the
   reason the gateway is away this long is almost always an upgrade, and the
   two should not disagree about when to stop hoping. The 8s cap keeps a page
   left open overnight from hammering a machine that is simply off. */
const REJOIN_CEILING_MS = 1_200_000
const REJOIN_MAX_WAIT_MS = 8_000
const REJOIN_FIRST_WAIT_MS = 1_500
const REJOIN_GROWTH = 1.6

/* Not `WebSocket.CONNECTING`: the transport must be constructible where no
   global WebSocket exists, which is how its own tests drive it. */
const CONNECTING = 0

/** The slice of a WebSocket this transport uses, so a test can supply one. */
export interface WebSocketLike {
  readonly readyState: number
  binaryType: string
  onopen: (() => void) | null
  onclose: (() => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  send(data: string): void
  close(): void
  addEventListener(type: 'open' | 'close', fn: () => void, options: { once: true }): void
}

export interface Timers {
  setTimeout: (fn: () => void, ms: number) => number
  clearTimeout: (id: number) => void
}

export interface WsTransportOptions {
  url?: () => string
  makeSocket?: (url: string) => WebSocketLike
  probe?: () => Promise<boolean>
  now?: () => number
  timers?: Timers
}

interface Pending {
  resolve: (value: unknown) => void
  reject: (error: unknown) => void
}

interface WireError {
  code: number
  message?: string
  data?: unknown
}

interface WireFrame {
  id?: number | null
  method?: string
  params?: unknown
  result?: unknown
  error?: WireError
}

const defaultUrl = (): string =>
  `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/rpc`

/* The cast is the one place the browser's own type meets the slice above.
   A real WebSocket satisfies it at runtime but not structurally: its handler
   properties are typed against the full event objects, and a property's
   parameter types are checked contravariantly, so a narrower `{ data }` event
   is rejected however it is written. */
const defaultSocket = (url: string): WebSocketLike =>
  new globalThis.WebSocket(url) as unknown as WebSocketLike

/* What separates "the gateway is not there" from "the gateway refused this
   session": /health is unauthenticated precisely so it can answer a page whose
   cookie the gateway has already forgotten, so an answer means the process is
   back and a socket closing anyway is a real auth refusal. No answer means
   keep waiting. (020-rpc.js probed '/' with HEAD; the two are equivalent, and
   the dev server owns '/'.) */
const probeHealth = async (): Promise<boolean> => {
  try {
    const r = await fetch('/health', { cache: 'no-store' })
    return r.status !== 401 && r.status !== 403
  } catch {
    return false
  }
}

/* Read through globalThis on each call, so a test that installs fake timers
   after the transport was built still gets them. */
const defaultTimers: Timers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (id) => {
    globalThis.clearTimeout(id)
  },
}

const detailOf = (data: unknown): string => {
  if (typeof data !== 'object' || data === null) return ''
  const detail = (data as { detail?: unknown }).detail
  return typeof detail === 'string' ? detail.trim() : ''
}

/* What a rejected call carries.

   The gateway sends two different things: `message` is a machine code the
   client matches on -- `config_validation_error`, `session_not_found` -- and
   `data.detail` is the sentence a person is meant to read. Rejecting with the
   frame as it stands meant every `toast(e.message)` on the page showed the
   code, so naming a knowledge base that already existed reported
   "config_validation_error" and nothing about the name.

   So the rejection is an Error whose message is the sentence when there is
   one, with the code and the original frame kept on it: two callers match on
   `e.code` for a method the gateway does not have, and they must go on
   working. */
const rpcFailure = (error: WireError): RpcError =>
  new RpcError(error.code, detailOf(error.data) || error.message || 'rpc error', error.data, error)

/** A transport over one live WebSocket, rejoining on its own when it drops. */
export class WsTransport implements RpcTransport {
  private readonly url: () => string
  private readonly makeSocket: (url: string) => WebSocketLike
  private readonly probe: () => Promise<boolean>
  private readonly now: () => number
  private readonly timers: Timers

  private readonly pending = new Map<number, Pending>()
  private readonly handlers = new Map<string, Set<NotificationHandler>>()
  private readonly binaryHandlers = new Set<BinaryHandler>()
  private readonly stateListeners = new Set<StateListener>()

  private ws: WebSocketLike | null = null
  private next = 1
  private opened = false
  private rejoining = false
  private timer: number | null = null
  private state: ConnectionState = 'closed'

  constructor(options: WsTransportOptions = {}) {
    this.url = options.url ?? defaultUrl
    this.makeSocket = options.makeSocket ?? defaultSocket
    this.probe = options.probe ?? probeHealth
    this.now = options.now ?? (() => Date.now())
    this.timers = options.timers ?? defaultTimers
  }

  connect(): Promise<boolean> {
    return new Promise((resolve) => {
      const ws = this.makeSocket(this.url())
      ws.binaryType = 'arraybuffer'
      this.ws = ws
      // A rejoin already said what it is doing, once, and its ticks must not
      // overwrite that with a 'connecting' per attempt.
      if (!this.rejoining) this.setState('connecting')
      ws.onopen = () => {
        this.opened = true
        this.setState('open')
        resolve(true)
      }
      ws.onmessage = (ev: { data: unknown }) => {
        this.receive(ev.data)
      }
      ws.onclose = () => {
        const was = this.opened
        this.opened = false
        for (const p of this.pending.values()) {
          p.reject(new RpcError(-1, 'connection closed'))
        }
        this.pending.clear()
        /* A socket that never opened is reported, not interpreted. It means
           one of two very different things -- the gateway refused this
           session, or there is no gateway right now -- and only the caller has
           the context to tell them apart. Deciding here is what made an
           upgrade look like a sign-in failure. */
        if (!was) {
          resolve(false)
          return
        }
        this.rejoin()
      }
    })
  }

  async call<M extends RpcMethod>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>> {
    return (await this.send(method, params)) as ResultOf<M>
  }

  /**
   * @deprecated Only for `raven.mcp.list` and `raven.mcp.set`, which the
   * contract does not declare. Both answer -32601 today; the escape hatch
   * exists so that stays true instead of changing shape on the way out.
   */
  callUnchecked(method: string, params: Record<string, unknown>): Promise<unknown> {
    return this.send(method, params)
  }

  on(method: string, handler: NotificationHandler): () => void {
    const set = this.handlers.get(method) ?? new Set<NotificationHandler>()
    set.add(handler)
    this.handlers.set(method, set)
    return () => set.delete(handler)
  }

  binary(handler: BinaryHandler): () => void {
    this.binaryHandlers.add(handler)
    return () => this.binaryHandlers.delete(handler)
  }

  onState(listener: StateListener): () => void {
    this.stateListeners.add(listener)
    listener(this.state)
    return () => this.stateListeners.delete(listener)
  }

  close(): void {
    this.rejoining = false
    this.cancelTick()
    // Cleared before the socket is told to close, so the close we asked for
    // does not read as a drop and start a rejoin.
    this.opened = false
    this.ws?.close()
    this.setState('closed')
  }

  private send(method: string, params: unknown): Promise<unknown> {
    const ws = this.ws
    if (!this.opened || !ws) {
      /* A socket still shaking hands is not a missing gateway. The page's
         first paint schedules its own loads, so those land in the window
         between `connect()` being called and the socket opening, and the
         reader was told "load failed: not connected" on every single reload.
         Waiting for THIS connect to settle is the whole fix: a socket that is
         closed, closing, or absent still fails fast, and a socket that never
         opens rejects when it closes rather than leaving the caller hanging. */
      if (ws?.readyState === CONNECTING) return this.whenOpen(ws, method, params)
      return Promise.reject(new RpcError(-1, 'not connected'))
    }
    const id = this.next++
    ws.send(JSON.stringify({ jsonrpc: '2.0', id, method, params: params ?? {} }))
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
    })
  }

  /* One call, held until the socket it was made on opens. Registered with
     `addEventListener` rather than by assigning the handlers, because
     `connect()` owns `onopen`/`onclose` and overwriting either would take the
     connection's own bookkeeping with it. `once`, so a call cannot be sent
     twice and a rejected one cannot be settled again. */
  private whenOpen(ws: WebSocketLike, method: string, params: unknown): Promise<unknown> {
    return new Promise((resolve, reject) => {
      ws.addEventListener(
        'open',
        () => {
          this.send(method, params).then(resolve, reject)
        },
        { once: true }
      )
      ws.addEventListener('close', () => reject(new RpcError(-1, 'not connected')), { once: true })
    })
  }

  private receive(data: unknown): void {
    if (data instanceof ArrayBuffer) {
      for (const h of this.binaryHandlers) {
        h(data)
      }
      return
    }
    if (typeof data !== 'string') return
    let frame: WireFrame
    try {
      frame = JSON.parse(data) as WireFrame
    } catch {
      return
    }
    const id = frame.id
    if (id != null) {
      const p = this.pending.get(id)
      if (p) {
        this.pending.delete(id)
        if (frame.error) p.reject(rpcFailure(frame.error))
        else p.resolve(frame.result)
        return
      }
    }
    if (frame.method) {
      for (const h of this.handlers.get(frame.method) ?? []) {
        h(frame.params ?? {})
      }
    }
  }

  /* Keep trying, with backoff, instead of one 1.5s attempt. The one thing
     that reliably takes the gateway away is an upgrade replacing the
     installation under it, and an upgrade is minutes -- a cold one measured
     nine. So a single retry was guaranteed to fire while the backend was
     still absent, fail, and report an expired sign-in that had not expired.

     `probe()` is what separates absent from refused, and only when it says
     refused does this give up and say so. */
  private rejoin(): void {
    if (this.rejoining) return
    this.rejoining = true
    const t0 = this.now()
    let wait = REJOIN_FIRST_WAIT_MS
    let attempt = 0
    const tick = async (): Promise<void> => {
      if (this.now() - t0 > REJOIN_CEILING_MS) {
        this.rejoining = false
        this.setState('auth-failed', { attempt })
        return
      }
      if (await this.connect()) {
        this.rejoining = false
        /* The gateway that came back may be serving a different build than
           the one this page was loaded from -- that is the upgrade case, and
           reloading onto it is the caller's decision, not the transport's. */
        this.emit('reconnected', { attempt })
        return
      }
      attempt += 1
      if (await this.probe()) {
        this.rejoining = false
        this.setState('auth-failed', { attempt })
        return
      }
      this.setState('reconnecting', { attempt })
      wait = Math.min(Math.round(wait * REJOIN_GROWTH), REJOIN_MAX_WAIT_MS)
      this.schedule(tick, wait)
    }
    this.setState('reconnecting', { attempt })
    this.schedule(tick, wait)
  }

  private schedule(tick: () => Promise<void>, wait: number): void {
    this.timer = this.timers.setTimeout(() => {
      this.timer = null
      void tick()
    }, wait)
  }

  private cancelTick(): void {
    if (this.timer !== null) {
      this.timers.clearTimeout(this.timer)
      this.timer = null
    }
  }

  private setState(state: ConnectionState, info?: StateInfo): void {
    this.state = state
    this.emit(state, info)
  }

  /* 'reconnected' is an edge, not a resting state: the socket is open again,
     so a listener attached after the fact must be told 'open'. */
  private emit(state: ConnectionState, info?: StateInfo): void {
    for (const l of this.stateListeners) {
      l(state, info)
    }
  }
}
