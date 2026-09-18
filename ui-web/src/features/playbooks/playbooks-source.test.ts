// @vitest-environment happy-dom
/* The live playbooks adapter, against the names the store actually calls.
 *
 * The component tests draw the page from a fixture that already carries the
 * store's names, so they cannot see a live adapter that exports different ones:
 * the tab takes its unsupported branch and renders an empty list against a real
 * gateway, which looks exactly like a machine with no stints on it. Shipped
 * once, with two halves wrong on one line -- the method name and the key it
 * unwrapped from the answer.
 */

// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'

import { describe, expect, it, vi } from 'vitest'

const liveSource = readFileSync('src/live/167-playbooks.js', 'utf8')
const storeSource = readFileSync('src/features/playbooks/store.ts', 'utf8')

/* Read out of the store rather than listed here: a list would be a second
   place to keep the names in step, which is the failure this test is for. */
function namesTheStoreCalls(): string[] {
  const found = new Set<string>()
  for (const match of storeSource.matchAll(/source\(\)\.([A-Za-z_$][\w$]*)/g)) {
    found.add(match[1]!)
  }
  return [...found].sort()
}

function adapter(call: ReturnType<typeof vi.fn>): Record<string, (...args: never[]) => unknown> {
  const DS: Record<string, unknown> = {}
  new Function('DS', 'rpc', liveSource)(DS, { call })
  return DS.playbooks as Record<string, (...args: never[]) => unknown>
}

describe('the live playbooks adapter', () => {
  it('answers every name the store reaches for', () => {
    const source = adapter(vi.fn())

    const missing = namesTheStoreCalls().filter(name => typeof source[name] !== 'function')

    expect(missing, 'the store would take the unsupported branch for these').toEqual([])
  })

  it('unwraps the list from the field the method answers with', async () => {
    /* The answer is an object so it can grow a field beside the array, so the
       adapter names that field -- and named the one the method used to use. */
    const call = vi.fn().mockResolvedValue({ stints: [{ stint_id: 'stint-a' }] })
    const source = adapter(call)

    const rows = await (source.stints as () => Promise<unknown[]>)()

    expect(call).toHaveBeenCalledWith('playbooks.stints.list', {})
    expect(rows).toEqual([{ stint_id: 'stint-a' }])
  })

  it('answers an empty list rather than undefined when the engine carries none', async () => {
    const source = adapter(vi.fn().mockResolvedValue({}))

    expect(await (source.stints as () => Promise<unknown[]>)()).toEqual([])
  })
})
