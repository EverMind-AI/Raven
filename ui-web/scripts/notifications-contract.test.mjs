/* The notification table is the server's list, plus exactly one name.
 *
 * `rpc-schema/openrpc.json` declares calls, not pushes, so nothing generated
 * covers the eleven names in src/rpc/notifications.ts. A handler registered
 * under a name the gateway never sends is not an error anywhere -- it is a
 * surface that silently stops working -- so the two ends are compared here
 * instead: the table against raven/acp/updates.py's SIDE_CHANNEL_METHODS,
 * which is the server-side roster of the pushes that are not subscription
 * events, and the table against every name the page actually registers --
 * which is the legacy layer plus the session pipeline, the two places a
 * handler is installed.
 *
 * The Python file is read, never written: this whole refactor stays inside
 * ui-web/.
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { partNames } from './legacy-part.mjs'

import { NOTIFICATION_METHODS } from '../src/rpc/notifications'

/* The one name the page listens for that the server's list does not carry: an
   older gateway pushes the screencast frame as base64 JSON instead of sending
   a binary one, so the handler has to exist while such a gateway may answer. */
const EXTRA = ['browser.frame']

/* The subscription envelope. Not a side channel -- its payload is
   {subscription_id, event} -- so it is absent from both lists and allowed at a
   registration site on its own. */
const ENVELOPE = 'event'

/** The frozenset in raven/acp/updates.py, by name. */
function sideChannelMethods() {
  const text = readFileSync(resolve(process.cwd(), '../raven/acp/updates.py'), 'utf8')
  const open = text.indexOf('SIDE_CHANNEL_METHODS = frozenset(')
  if (open < 0) throw new Error('raven/acp/updates.py: SIDE_CHANNEL_METHODS not found')
  const close = text.indexOf('\n)', open)
  if (close < 0) throw new Error('raven/acp/updates.py: SIDE_CHANNEL_METHODS is not closed')
  return [...text.slice(open, close).matchAll(/"([^"]+)"/g)].map((m) => m[1])
}

/* Where a push handler can be installed: the page's own wiring, which registers
   the seven that are not a turn's, the session pipeline, which took the
   subscription envelope and the five requests that block a turn, and what is
   left of the two legacy layers. */
const SITES = [
  'state/install.ts',
  'state/session/pipeline.ts',
  ...['demo', 'live'].flatMap((layer) => partNames(layer).map((name) => `legacy/${layer}/${name}`)),
]

/** Every [file:line, name] the page registers a push handler for. */
function registered() {
  const found = []
  for (const rel of SITES) {
    const text = readFileSync(resolve(process.cwd(), 'src', rel), 'utf8')
    const sf = ts.createSourceFile(rel, text, ts.ScriptTarget.ES2022, true,
      rel.endsWith('.ts') ? ts.ScriptKind.TS : ts.ScriptKind.JS)
    const walk = (node) => {
      /* `gateway().on(...)`, which is what tells it apart from any other
         object's method of that name. */
      const callee = ts.isCallExpression(node) ? node.expression : null
      const isOn = callee
        && ts.isPropertyAccessExpression(callee)
        && callee.name.text === 'on'
        && ts.isCallExpression(callee.expression)
        && ts.isIdentifier(callee.expression.expression)
        && callee.expression.expression.text === 'gateway'
      if (isOn) {
        const first = node.arguments[0]
        const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1
        found.push([
          `${rel}:${line}`,
          first && ts.isStringLiteral(first) ? first.text : `<not a literal: ${first ? first.getText(sf) : 'no argument'}>`,
        ])
      }
      ts.forEachChild(node, walk)
    }
    walk(sf)
  }
  return found
}

describe('the notification table', () => {
  const server = sideChannelMethods()

  it('read the server list at all', () => {
    /* A parse that found nothing would make every assertion below vacuous. */
    expect(server.length).toBeGreaterThanOrEqual(10)
  })

  it('carries every side channel the server declares', () => {
    const missing = server.filter((name) => !NOTIFICATION_METHODS.includes(name))
    expect(missing).toEqual([])
  })

  it('carries nothing beyond them but the legacy frame push', () => {
    const extra = NOTIFICATION_METHODS.filter((name) => !server.includes(name))
    expect([...extra].sort()).toEqual([...EXTRA].sort())
  })

  it('names each one once', () => {
    expect(new Set(NOTIFICATION_METHODS).size).toBe(NOTIFICATION_METHODS.length)
  })
})

describe('the names the page registers', () => {
  const sites = registered()

  it('reaches the registration sites at all', () => {
    expect(sites.length).toBeGreaterThanOrEqual(11)
  })

  it('are all in the table, or the subscription envelope', () => {
    const allowed = new Set([...NOTIFICATION_METHODS, ENVELOPE])
    const unknown = sites.filter(([, name]) => !allowed.has(name)).map(([at, name]) => `${at} ${name}`)
    expect(unknown).toEqual([])
  })
})
