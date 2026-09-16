import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RpcError } from './transport'
import { WsTransport } from './wsTransport'

import type { ConnectionState } from './transport'

/* The behaviour under test is the whole of the rpc client that used to live in
   ui-web/src/legacy/live/020-rpc.js, so the first five cases are the ones
   scripts/rpc-connect.test.mjs made against that object, with its FakeSocket
   brought along. That file is gone; these are its cases. */

/* Enough of a socket to be opened, closed, written to and fed a frame.
   `readyState` starts CONNECTING, which is the state the page's own first
   loads meet. */
class FakeSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSED = 3

  readyState = FakeSocket.CONNECTING
  binaryType: 'blob' | 'arraybuffer' = 'blob'
  readonly sent: Array<Record<string, unknown>> = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null

  private readonly listeners = new Map<string, Array<{ fn: () => void; once: boolean }>>()

  addEventListener(type: string, fn: () => void, options?: { once?: boolean }): void {
    const list = this.listeners.get(type) ?? []
    list.push({ fn, once: options?.once === true })
    this.listeners.set(type, list)
  }

  send(text: string): void {
    this.sent.push(JSON.parse(text) as Record<string, unknown>)
  }

  close(): void {
    this.closed()
  }

  /* The property handler first, then the listeners -- the order a browser
     uses, and the order the held-call fix depends on: `connect()` assigns
     `onopen` when it makes the socket, so by the time a held call is released
     the client already knows it is open. */
  opened(): void {
    this.readyState = FakeSocket.OPEN
    this.onopen?.()
    this.fire('open')
  }

  closed(): void {
    this.readyState = FakeSocket.CLOSED
    this.onclose?.()
    this.fire('close')
  }

  receive(data: unknown): void {
    this.onmessage?.({ data })
  }

  private fire(type: string): void {
    const list = this.listeners.get(type) ?? []
    this.listeners.set(
      type,
      list.filter((l) => !l.once)
    )
    for (const l of list) {
      l.fn()
    }
  }
}

/** What a socket this harness hands out does on its own, and when. */
type Fate = 'hang' | 'fail' | 'open'

interface Seen {
  state: ConnectionState
  attempt?: number
}

interface Harness {
  transport: WsTransport
  sockets: FakeSocket[]
  /** Every rejoin sleep asked for, in order. */
  delays: number[]
  seen: Seen[]
  probes: () => number
  setFate: (fate: Fate) => void
  setProbe: (answer: boolean) => void
}

function harness(options: { fate?: Fate; probe?: boolean; now?: () => number } = {}): Harness {
  const sockets: FakeSocket[] = []
  const delays: number[] = []
  const seen: Seen[] = []
  let fate: Fate = options.fate ?? 'hang'
  let probeAnswer = options.probe ?? false
  let probes = 0

  const transport = new WsTransport({
    url: () => 'ws://gateway.test/rpc',
    makeSocket: () => {
      const socket = new FakeSocket()
      sockets.push(socket)
      /* A socket settles on a later job, the way a real one does: `connect()`
         has not assigned its handlers yet when this returns. */
      if (fate !== 'hang') {
        void Promise.resolve().then(() => (fate === 'open' ? socket.opened() : socket.closed()))
      }
      return socket
    },
    probe: async () => {
      probes += 1
      return probeAnswer
    },
    now: options.now,
    timers: {
      setTimeout: (fn, ms) => {
        delays.push(ms)
        return globalThis.setTimeout(fn, ms)
      },
      clearTimeout: (id) => {
        globalThis.clearTimeout(id)
      },
    },
  })
  transport.onState((state, info) => seen.push({ state, ...(info ?? {}) }))
  return {
    transport,
    sockets,
    delays,
    seen,
    probes: () => probes,
    setFate: (next) => {
      fate = next
    },
    setProbe: (answer) => {
      probeAnswer = answer
    },
  }
}

const socketAt = (h: Harness, index: number): FakeSocket => {
  const socket = h.sockets[index]
  if (!socket) throw new Error(`no socket ${index}`)
  return socket
}

/** A transport mid-handshake, the way the page makes one. */
function connecting(options: Parameters<typeof harness>[0] = {}): {
  h: Harness
  ws: FakeSocket
  joined: Promise<boolean>
} {
  const h = harness(options)
  const joined = h.transport.connect()
  return { h, ws: socketAt(h, 0), joined }
}

/** A transport whose first socket is open. */
async function connected(options: Parameters<typeof harness>[0] = {}): Promise<{
  h: Harness
  ws: FakeSocket
}> {
  const { h, ws, joined } = connecting(options)
  ws.opened()
  await joined
  return { h, ws }
}

const states = (h: Harness): ConnectionState[] => h.seen.map((s) => s.state)

const attempts = (h: Harness, state: ConnectionState): Array<number | undefined> =>
  h.seen.filter((s) => s.state === state).map((s) => s.attempt)

const failure = async (answer: Promise<unknown>): Promise<RpcError> => {
  const error: unknown = await answer.catch((e: unknown) => e)
  expect(error).toBeInstanceOf(RpcError)
  return error as RpcError
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('a call made before the socket is open', () => {
  it('is sent once the handshake finishes', async () => {
    const { h, ws } = connecting()

    const answer = h.transport.call('session.list', { limit: 3 })
    /* Nothing on the wire yet, and nothing reported to the reader either. */
    expect(ws.sent).toEqual([])

    ws.opened()
    await Promise.resolve()
    expect(ws.sent).toEqual([
      { jsonrpc: '2.0', id: 1, method: 'session.list', params: { limit: 3 } },
    ])

    ws.receive(JSON.stringify({ jsonrpc: '2.0', id: 1, result: { sessions: [] } }))
    await expect(answer).resolves.toEqual({ sessions: [] })
  })

  it('fails when that socket closes without ever opening', async () => {
    /* The gateway is genuinely absent. A held call must not hang for the life
       of the page waiting for a handshake that is not coming. */
    const { h, ws } = connecting()

    const answer = h.transport.call('session.list', {})
    ws.closed()

    expect((await failure(answer)).message).toBe('not connected')
  })

  it('still fails fast when there is no socket at all', async () => {
    const h = harness()

    const error = await failure(h.transport.call('session.list', {}))
    expect(error.message).toBe('not connected')
    expect(error.code).toBe(-1)
  })

  it('still fails fast on a socket that has closed', async () => {
    const { h, ws } = connecting()
    ws.closed()

    expect((await failure(h.transport.call('session.list', {}))).message).toBe('not connected')
  })

  it('holds each caller separately', async () => {
    const { h, ws } = connecting()

    void h.transport.call('ext.list', {})
    void h.transport.call('session.list', {})
    ws.opened()
    await Promise.resolve()

    expect(ws.sent.map((f) => f.method)).toEqual(['ext.list', 'session.list'])
  })
})

describe('connect', () => {
  it('reports a socket that closed before it ever opened, without interpreting it', async () => {
    const { ws, joined, h } = connecting()
    ws.closed()

    await expect(joined).resolves.toBe(false)
    /* No rejoin: only the caller knows whether this is a refusal or an
       absence, so nothing was scheduled here. */
    expect(h.delays).toEqual([])
  })

  it('resolves true on open and says so once', async () => {
    const { h, ws, joined } = connecting()
    ws.opened()

    await expect(joined).resolves.toBe(true)
    expect(states(h)).toEqual(['closed', 'connecting', 'open'])
  })
})

describe('a call on an open socket', () => {
  it('resolves with the frame result', async () => {
    const { h, ws } = await connected()

    const answer = h.transport.call('cron.list', {})
    expect(ws.sent).toEqual([{ jsonrpc: '2.0', id: 1, method: 'cron.list', params: {} }])
    ws.receive(JSON.stringify({ jsonrpc: '2.0', id: 1, result: { jobs: [] } }))

    await expect(answer).resolves.toEqual({ jobs: [] })
  })

  it('sends an undeclared name as given, and keeps its -32601', async () => {
    const { h, ws } = await connected()

    const answer = h.transport.callUnchecked('raven.mcp.list', {})
    expect(ws.sent.map((f) => f.method)).toEqual(['raven.mcp.list'])
    ws.receive(
      JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        error: { code: -32601, message: 'method not found' },
      })
    )

    expect((await failure(answer)).code).toBe(-32601)
  })

  it('rejects every pending call when the connection drops', async () => {
    const { h, ws } = await connected()

    const answer = h.transport.call('cron.list', {})
    ws.closed()

    const error = await failure(answer)
    expect(error.code).toBe(-1)
    expect(error.message).toBe('connection closed')
  })
})

describe('a rejected call', () => {
  const reject = async (h: Harness, ws: FakeSocket, error: unknown): Promise<RpcError> => {
    const answer = h.transport.call('cron.list', {})
    ws.receive(JSON.stringify({ jsonrpc: '2.0', id: 1, error }))
    return await failure(answer)
  }

  it('reads the sentence a person is meant to see, not the machine code', async () => {
    const { h, ws } = await connected()

    const error = await reject(h, ws, {
      code: -32602,
      message: 'config_validation_error',
      data: { detail: '  a base named notes already exists  ' },
    })

    expect(error.message).toBe('a base named notes already exists')
    expect(error.code).toBe(-32602)
    expect(error.data).toEqual({ detail: '  a base named notes already exists  ' })
    expect(error.rpc).toEqual({
      code: -32602,
      message: 'config_validation_error',
      data: { detail: '  a base named notes already exists  ' },
    })
  })

  it('falls back to the message when the detail is not a sentence', async () => {
    const { h, ws } = await connected()

    const error = await reject(h, ws, { code: -32602, message: 'session_not_found', data: { detail: 7 } })

    expect(error.message).toBe('session_not_found')
  })

  it('has something to say even for a frame carrying neither', async () => {
    const { h, ws } = await connected()

    expect((await reject(h, ws, { code: -32603 })).message).toBe('rpc error')
  })
})

describe('notifications', () => {
  it('fan out to every handler, and detach removes one', async () => {
    const { h, ws } = await connected()
    const got: unknown[] = []
    const detach = h.transport.on('turn.delta', (p) => got.push(p))
    h.transport.on('turn.delta', (p) => got.push(p))

    ws.receive(JSON.stringify({ jsonrpc: '2.0', method: 'turn.delta', params: { text: 'x' } }))
    detach()
    ws.receive(JSON.stringify({ jsonrpc: '2.0', method: 'turn.delta' }))

    expect(got).toEqual([{ text: 'x' }, { text: 'x' }, {}])
  })
})

describe('binary frames', () => {
  it('go to the binary handlers only, until detached', async () => {
    const { h, ws } = await connected()
    const got: number[] = []
    const detach = h.transport.binary((buf) => got.push(buf.byteLength))

    ws.receive(new ArrayBuffer(4))
    detach()
    ws.receive(new ArrayBuffer(8))

    expect(got).toEqual([4])
  })
})

describe('onState', () => {
  it('reports the current state immediately', () => {
    const h = harness()

    expect(h.seen).toEqual([{ state: 'closed' }])
  })
})

describe('rejoin', () => {
  /* The page has to survive an upgrade replacing the gateway under it, which
     is minutes, so the backoff and the ceiling are the behaviour -- not an
     implementation detail of a retry. */
  it('backs off 1.6x per attempt up to 8s', async () => {
    const { h, ws } = await connected()
    h.setFate('fail')

    ws.closed()
    expect(h.delays).toEqual([1500])
    for (const slept of [1500, 2400, 3840, 6144, 8000]) {
      await vi.advanceTimersByTimeAsync(slept)
    }

    expect(h.delays).toEqual([1500, 2400, 3840, 6144, 8000, 8000])
  })

  it('counts the failed attempts it reports', async () => {
    const { h, ws } = await connected()
    h.setFate('fail')

    ws.closed()
    expect(attempts(h, 'reconnecting')).toEqual([0])
    await vi.advanceTimersByTimeAsync(1500)

    expect(attempts(h, 'reconnecting')).toEqual([0, 1])
  })

  it('stops hoping after twenty minutes', async () => {
    let clock = 0
    const { h, ws } = await connected({ now: () => clock })
    h.setFate('fail')

    ws.closed()
    clock = 1_200_001
    await vi.advanceTimersByTimeAsync(1500)

    expect(h.seen.at(-1)).toEqual({ state: 'auth-failed', attempt: 0 })
    /* Gave up before opening another socket, and scheduled nothing more. */
    expect(h.sockets.length).toBe(1)
    expect(h.delays).toEqual([1500])
  })

  it('calls a refusal a refusal when the gateway answers over HTTP', async () => {
    const { h, ws } = await connected()
    h.setFate('fail')
    h.setProbe(true)

    ws.closed()
    await vi.advanceTimersByTimeAsync(1500)
    expect(h.seen.at(-1)).toEqual({ state: 'auth-failed', attempt: 1 })

    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.probes()).toBe(1)
    expect(h.delays).toEqual([1500])
    expect(h.sockets.length).toBe(2)
  })

  it('reports a reconnect and is ready for the next drop', async () => {
    const { h, ws } = await connected()
    h.setFate('open')

    ws.closed()
    await vi.advanceTimersByTimeAsync(1500)

    expect(states(h)).toEqual([
      'closed',
      'connecting',
      'open',
      'reconnecting',
      'open',
      'reconnected',
    ])

    /* The rejoin flag is clear, so a second drop starts its own rejoin. */
    socketAt(h, 1).closed()
    expect(h.delays).toEqual([1500, 1500])
  })

  it('is cancelled by close', async () => {
    const { h, ws } = await connected()
    h.setFate('fail')

    ws.closed()
    expect(h.delays).toEqual([1500])
    h.transport.close()
    await vi.advanceTimersByTimeAsync(60_000)

    expect(h.sockets.length).toBe(1)
    expect(states(h).at(-1)).toBe('closed')
  })
})
