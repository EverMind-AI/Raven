/* Drives one module from a test, against fakes.
 *
 * Written for the parts of the legacy page script, which used to be fragments
 * of one concatenated text: a harness could only reach a function by slicing it
 * out and evaluating it with its collaborators passed in as parameters. The
 * layer is gone and every module it held has a home, but the shape this gives a
 * case outlived it -- a collaborator is an import, which `fakes` replaces by
 * module path and export name, and the island bag is an object, which `islands`
 * assigns over.
 *
 * Every load starts from module state as fresh as a reload's, because a module
 * and the ones around it hold real state -- the panel's open view, the session
 * registry, the naming timers -- and a case that ran before must not be visible
 * in the next one.
 */
import { existsSync, readFileSync } from 'node:fs'
/* Off cwd, not off `import.meta.url`: under happy-dom that is an http URL. */
import { resolve } from 'node:path'

import { vi } from 'vitest'

const mocked = new Set()

/* A suffix the specifier in `fakes` is allowed to leave off. */
const SUFFIXES = ['', '.ts', '.tsx', '.mjs']

/* Mocking a path nothing resolves is silently inert -- the part loads with its
   real collaborator and the case still runs, testing the opposite of what it
   says -- so a misspelt key has to be an error here rather than a stand-in
   that never stands in. */
function requireModule(module) {
  const base = resolve(process.cwd(), module)
  if (!SUFFIXES.some((suffix) => existsSync(base + suffix)))
    throw new Error(`fakes key '${module}' names no module under ui-web/`)
}

/**
 * @param importPart a thunk that imports the module under test, e.g.
 *   `() => import('../src/state/session/runtime')`. A thunk rather than a path
 *   so the specifier stays static and Vite can resolve it.
 * @param fakes exports to replace, keyed by the module's path from ui-web
 *   (`'src/state/toast'`). What is not named keeps the real implementation, and
 *   a key that names no module throws rather than standing in for nothing.
 * @param islands members of the island bag (src/islands.ts) to stand in for,
 *   assigned over the real ones one MEMBER at a time: a case names the verbs it
 *   is about and the rest of that island answers as it really does. Assigned
 *   rather than mocked, and taken from the module the reset above just gave the
 *   part: a binding a harness held from an earlier load belongs to that load.
 */
export async function loadPart(importPart, { fakes = {}, islands = {} } = {}) {
  for (const path of mocked) vi.doUnmock(path)
  mocked.clear()
  vi.resetModules()
  for (const [module, exports] of Object.entries(fakes)) {
    const path = `../${module}`
    requireModule(module)
    mocked.add(path)
    /* Descriptors, not a spread: a fake for a binding the part reassigns --
       `viewGen` -- has to be a getter, and spreading one would freeze it at
       whatever it read once. */
    vi.doMock(path, async (original) =>
      Object.defineProperties({ ...(await original()) }, Object.getOwnPropertyDescriptors(exports)))
  }
  const part = await importPart()
  if (Object.keys(islands).length) {
    const bag = (await import('../src/islands')).islands
    for (const [name, members] of Object.entries(islands)) Object.assign(bag[name], members)
  }
  return part
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

/* One module's source, by its path under src/. A few rules are about the shape
   of the source rather than about a behaviour a harness can drive -- which call
   sites carry a branch, which stage re-anchors the clock -- and reading the
   file from here keeps node's own imports out of the type-checked tests. */
export function moduleText(rel) {
  return readFileSync(resolve(process.cwd(), 'src', rel), 'utf8')
}

/* A `$` (src/lib/dom.ts) that answers the document for what the test built
   and a throwaway element for everything else. A case builds the part of the
   page it is about and no more, and a writer reaching for one it did not build
   would throw; one element per unbuilt selector keeps the stand-ins stable
   across calls, so a value stored on one is still there to read back. */
export function looseQuery() {
  const spare = new Map()
  return (selector) => {
    const found = document.querySelector(selector)
    if (found) return found
    if (!spare.has(selector)) spare.set(selector, document.createElement('div'))
    return spare.get(selector)
  }
}
