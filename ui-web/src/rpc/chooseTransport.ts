/* Which transport this page talks to, and what is layered over it.
 *
 * One decision, made once, before anything can ask for a gateway. It used to be
 * two: a URL test that decided whether the live half of the page script
 * installed at all (`liveMode`), and a demo layer that registered twenty
 * fixture sources when it did not. The page is one program now -- every feature
 * reads its own `source.ts`, every source speaks the contract -- so the only
 * thing left for the URL to decide is what answers those calls.
 *
 *   - served over http(s) without ?stub=1  ->  the WebSocket to a real raven
 *   - opened from disk, or with ?stub=1     ->  the offline fixture library
 *
 * The two canvases are overrides on top of whichever of those was chosen, so
 * both work on a live page as well as an offline one -- `?desk-demo=1` has
 * always been applied on the live page, and `?onboard=demo` is a first-run flow
 * that writes nothing, which is exactly what there was no way to look at
 * against a real serve before.
 */

import type { RpcTransport } from './transport'

import { demoFixtures, deskDemoOverrides, onboardDemoOverrides } from './fixtures'
import { FixtureTransport } from './fixtureTransport'
import { OverrideTransport } from './overrideTransport'
import { WsTransport } from './wsTransport'

/* Whether this page has a gateway to talk to at all. */
function liveMode(): boolean {
  return /^http/.test(location.protocol) && !/(^|[?&])stub=1/.test(location.search)
}

const asked = (test: RegExp): boolean => test.test(location.search)

/* The clock and the timer the offline library runs on. Named here rather than
   defaulted inside the transport so a test can hand in its own pair and get
   byte-identical answers out. */
const now = (): number => Date.now()
const later = (ms: number, fn: () => void): void => { setTimeout(fn, ms) }

/**
 * The page's transport, canvases included.
 *
 * Called once, from main.tsx, before the page's own wiring -- every source
 * install and the boot sequence itself read `gateway()`.
 */
export function chooseTransport(): RpcTransport {
  const base: RpcTransport = liveMode()
    ? new WsTransport()
    : new FixtureTransport(demoFixtures, { now, timer: later })
  return withCanvases(base)
}

/* The URL-asked canvases, in the order they are layered. */
function withCanvases(base: RpcTransport): RpcTransport {
  let transport = base
  if (asked(/[?&]onboard=demo/)) {
    transport = new OverrideTransport(transport, onboardDemoOverrides(later))
  }
  if (asked(/[?&]desk-demo=1/)) {
    /* The roster is asked of the transport underneath, which is what the block
       this came from asked of the live source: the canvas's agent names are
       mapped onto the agents the install really has, so its panes are headed by
       names the reader recognises. */
    const under = transport
    const roster = (): Promise<string[]> => under.call('subagents.list', { probe: false })
      .then((r) => (r.rows || []).filter((row) => row.enabled).map((row) => row.name).filter(Boolean))
      .catch(() => [])
    transport = new OverrideTransport(transport, deskDemoOverrides(now, later, roster))
  }
  return transport
}
