import type { RpcTransport } from './transport'

/* The one data entry point. Nothing builds its own transport: the page
   installs the live one, a test or the demo installs a FixtureTransport, and
   every feature reads whichever is in place through `gateway()`. */

let current: RpcTransport | null = null

export function gateway(): RpcTransport {
  if (!current) throw new Error('no gateway installed')
  return current
}

export function setGateway(transport: RpcTransport | null): void {
  current = transport
}
