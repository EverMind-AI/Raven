/* Drives one part of src/legacy/ from a test, against fakes.
 *
 * The parts used to be fragments of one concatenated script, so a harness
 * could only reach a function by slicing its text out of the assembled layer
 * and evaluating it with its collaborators passed in as parameters. They are
 * modules now: a collaborator is an import, which `fakes` replaces by part and
 * export name, and the names the page hangs on window are what `globals`
 * stands in for.
 *
 * Every load starts from module state as fresh as a reload's, because a part
 * holds real state -- `viewGen`, `parkedTurns`, the naming timers -- and a
 * case that ran before must not be visible in the next one.
 */
import { readdirSync, readFileSync } from 'node:fs'
/* Off cwd, not off `import.meta.url`: under happy-dom that is an http URL. */
import { resolve } from 'node:path'

import { vi } from 'vitest'

const mocked = new Set()

/**
 * @param importPart a thunk that imports the part under test, e.g.
 *   `() => import('../src/legacy/live/050-turn.js')`. A thunk rather than a
 *   path so the specifier stays static and Vite can resolve it.
 * @param fakes exports to replace, keyed by part path under src/legacy/
 *   (`'demo/050-rail.js'`). What is not named keeps the real implementation.
 * @param globals names to publish on the global object, standing in for what
 *   main.tsx puts on window.
 */
export async function loadPart(importPart, { fakes = {}, globals = {} } = {}) {
  for (const path of mocked) vi.doUnmock(path)
  mocked.clear()
  vi.resetModules()
  for (const [part, exports] of Object.entries(fakes)) {
    const path = `../src/legacy/${part}`
    mocked.add(path)
    /* Descriptors, not a spread: a fake for a binding the part reassigns --
       `viewGen` -- has to be a getter, and spreading one would freeze it at
       whatever it read once. */
    vi.doMock(path, async (original) =>
      Object.defineProperties({ ...(await original()) }, Object.getOwnPropertyDescriptors(exports)))
  }
  Object.assign(globalThis, globals)
  return importPart()
}

/* The transport the freshly loaded graph will speak through, with both call
   paths answering from `handler`. A FixtureTransport because it already is the
   push side -- `emit`, `emitBinary` and `setState` drive a notification, a
   binary frame and a connection-state change into the page -- and answering
   from a handler rather than from recorded fixtures is what lets a harness
   defer a call and settle it in whatever order the race under test needs. */
export async function fakeGateway(handler) {
  const { FixtureTransport } = await import('../src/rpc/fixtureTransport')
  const { setGateway } = await import('../src/state/gateway')
  const transport = new FixtureTransport({})
  transport.call = handler
  transport.callUnchecked = handler
  setGateway(transport)
  return transport
}

/* The parts of one layer ('demo' / 'live') in the order
   src/legacy/index.js installs them, which is the order the concatenated
   script ran them in. A few rules are about the shape of the source rather
   than about a behaviour a harness can drive, and the install order is what
   they are stated against. */
export function partNames(layer) {
  const index = readFileSync(resolve(process.cwd(), 'src/legacy/index.js'), 'utf8')
  const pattern = new RegExp(`^import \\* as \\w+ from '\\./${layer}/([^']+)'$`, 'gm')
  const installed = [...index.matchAll(pattern)].map((m) => m[1])
  if (!installed.length) throw new Error(`src/legacy/index.js installs no ${layer} part`)
  const onDisk = readdirSync(resolve(process.cwd(), 'src/legacy', layer)).filter((n) => n.endsWith('.js'))
  const missing = onDisk.filter((name) => !installed.includes(name))
  if (missing.length) throw new Error(`src/legacy/index.js installs no ${layer}/${missing[0]}`)
  return installed
}

/** One layer's parts as [name, text] pairs, in install order. */
export function partTexts(layer) {
  return partNames(layer).map((name) => [
    name,
    readFileSync(resolve(process.cwd(), 'src/legacy', layer, name), 'utf8'),
  ])
}

/* A `$` that answers the document for what the test built and a throwaway
   element for everything else. An install() wires dozens of controls the case
   under test has nothing to do with, and `$('#x').onclick = fn` on a missing
   one throws; one element per unbuilt selector keeps the stand-ins stable
   across calls, so a handler stored on one is still there to read back. */
export function looseQuery() {
  const spare = new Map()
  return (selector) => {
    const found = document.querySelector(selector)
    if (found) return found
    if (!spare.has(selector)) spare.set(selector, document.createElement('div'))
    return spare.get(selector)
  }
}
