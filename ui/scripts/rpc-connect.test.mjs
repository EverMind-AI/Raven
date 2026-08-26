/* A call made while the socket is still opening waits for it, rather than
 * telling the reader the gateway is not there.
 *
 * The rpc client is a fragment of the live IIFE, so it is evaluated here the
 * way boot-order.test.mjs evaluates the demo boot: in a Function sandbox with
 * the globals it reads at load time, which hands back the object itself. That
 * is the only way this layer is reachable from a test at all, and this is the
 * one behaviour in it a reader sees every single reload.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const src = readFileSync(new URL('../src/live/020-rpc.js', import.meta.url), 'utf8')

/* Enough of a socket to be opened, closed and written to. `readyState` starts
   CONNECTING, which is the state the page's own first loads meet. */
class FakeSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSED = 3

  constructor() {
    this.readyState = FakeSocket.CONNECTING
    this.sent = []
    this.listeners = { open: [], close: [] }
  }

  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn) }

  send(text) { this.sent.push(JSON.parse(text)) }

  /* The property handler first, then the listeners -- the order a browser
     uses, and the order the fix depends on: `connect()` assigns `onopen` when
     it makes the socket, so by the time a held call is released the client
     already knows it is open. */
  opened() {
    this.readyState = FakeSocket.OPEN
    if (this.onopen) this.onopen()
    this.listeners.open.forEach((fn) => fn())
  }

  closed() {
    this.readyState = FakeSocket.CLOSED
    if (this.onclose) this.onclose()
    this.listeners.close.forEach((fn) => fn())
  }
}

function client() {
  const build = new Function(
    'navigator', 'window', 'document', 'location', 'WebSocket', 'T', 'showStatus', 'fetch',
    'hideSplash', 'shellReady', 'failureBar', 'upShade', 'setTimeout',
    `${src}\nreturn rpc;`,
  )
  return build(
    { userAgent: 'test' }, {}, { querySelector: () => null },
    { protocol: 'http:', host: 'localhost:1' }, FakeSocket,
    (key) => key, () => {}, () => Promise.resolve({ ok: false }),
    () => {}, () => {}, () => ({ say: () => {} }), () => ({ say: () => {}, close: () => {} }),
    /* The rejoin's own backoff must not run: this test is about one connect. */
    () => 0,
  )
}

/* A client mid-handshake, made the way the page makes one: `connect()` owns the
   socket and its two handlers. */
function connecting() {
  const rpc = client()
  const joined = rpc.connect()
  return { rpc, ws: rpc.ws, joined }
}

describe('a call made before the socket is open', () => {
  it('is sent once the handshake finishes', async () => {
    const { rpc, ws } = connecting()

    const answer = rpc.call('settings.get', { a: 1 })
    /* Nothing on the wire yet, and nothing reported to the reader either. */
    expect(ws.sent).toEqual([])

    ws.opened()
    await Promise.resolve()
    expect(ws.sent).toEqual([{ jsonrpc: '2.0', id: 1, method: 'settings.get', params: { a: 1 } }])

    /* And it is a real pending call, answered by the frame that comes back. */
    rpc.pending.get(1).resolve({ ok: true })
    await expect(answer).resolves.toEqual({ ok: true })
  })

  it('fails when that socket closes without ever opening', async () => {
    /* The gateway is genuinely absent. A held call must not hang for the life
       of the page waiting for a handshake that is not coming. */
    const { rpc, ws } = connecting()

    const answer = rpc.call('settings.get', {})
    ws.closed()

    await expect(answer).rejects.toMatchObject({ message: 'not connected' })
  })

  it('still fails fast when there is no socket at all', async () => {
    const rpc = client()

    await expect(rpc.call('settings.get', {})).rejects.toMatchObject({ message: 'not connected' })
  })

  it('still fails fast on a socket that has closed', async () => {
    const { rpc, ws } = connecting()
    ws.closed()

    await expect(rpc.call('settings.get', {})).rejects.toMatchObject({ message: 'not connected' })
  })

  it('holds each caller separately', async () => {
    const { rpc, ws } = connecting()

    rpc.call('ext.list', {})
    rpc.call('settings.get', {})
    ws.opened()
    await Promise.resolve()

    expect(ws.sent.map((f) => f.method)).toEqual(['ext.list', 'settings.get'])
  })
})
