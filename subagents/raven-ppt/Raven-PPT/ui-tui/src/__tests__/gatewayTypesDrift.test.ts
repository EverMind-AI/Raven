// SPDX-License-Identifier: MIT
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * The picker payload is declared twice: once generated from the OpenRPC schema
 * (`rpc/generated.ts`), and once by hand in `gatewayTypes.ts`, which predates it
 * and which the test stubs rely on for its all-optional fields.
 *
 * Two declarations of one contract drift, and the drift is silent in the
 * direction that matters: a field added to the schema reaches the wire, the
 * hand-written type does not know about it, and the component reading that type
 * cannot see the data it is being sent.
 *
 * Merging them is a real change -- the generated shape has required fields the
 * stubs do not build -- so until then this keeps the copy honest.
 */

function propertyNames(source: string, interfaceName: string): Set<string> {
  const start = source.indexOf(`export interface ${interfaceName} {`)
  if (start < 0) {
    throw new Error(`${interfaceName} not found`)
  }
  const body = source.slice(start, source.indexOf('\n}', start))
  const names = new Set<string>()
  for (const line of body.split('\n').slice(1)) {
    const match = /^\s{2}([A-Za-z_][A-Za-z0-9_]*)\??\s*:/.exec(line)
    if (match) {
      names.add(match[1])
    }
  }
  return names
}

function read(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')
}

describe('ModelOptionProvider', () => {
  it('declares every property the generated contract sends', () => {
    const generated = propertyNames(read('../rpc/generated.ts'), 'ModelOptionProvider')
    const handWritten = propertyNames(read('../gatewayTypes.ts'), 'ModelOptionProvider')

    expect(generated.size).toBeGreaterThan(0)
    const missing = [...generated].filter(name => !handWritten.has(name)).sort()
    expect(missing, `add these to gatewayTypes.ts ModelOptionProvider: ${missing.join(', ')}`).toEqual([])
  })
})
