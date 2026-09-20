// @vitest-environment happy-dom
/* What a tool row reports as its state, and which question decides it.
 *
 * `on` is an accessor over `tools.disabledTools` for a switchable tool, which
 * is the right question there and a meaningless one for a tool no switch on
 * the page can move: `tool_search` is reported by `ext.list` precisely because
 * the loop did not register it, and it is not in that list either, so the
 * accessor answered that it was on. The page drew a running tool with its
 * thumb on the checked side and announced it that way.
 */
import { describe, expect, it } from 'vitest'

import { mkToolRow } from './source'

import type { ResultOf } from '../../rpc/generated'

type ExtToolRow = ResultOf<'ext.list'>['tools'][number]

const wire = (over: Partial<ExtToolRow>): ExtToolRow => ({
  name: 'tool_search', description: '', enabled: false, mcp_server: null, needs: null, builtin: true, ...over,
} as ExtToolRow)

describe('a tool row built from the wire', () => {
  it('keeps the reported state for a tool no switch can move', () => {
    expect(mkToolRow(wire({ name: 'tool_search', enabled: false, builtin: true })).on).toBe(false)
    expect(mkToolRow(wire({ name: 'tool_call', enabled: true, builtin: true })).on).toBe(true)
  })

  it('asks the disabled list for a tool whose switch works', () => {
    /* Nothing has been switched off in this test, so a switchable tool reads
       as on whatever the wire said -- that list is what its switch writes, and
       `enabled` can be false for a reason the switch does not own. */
    expect(mkToolRow(wire({ name: 'cancel_dag', enabled: true, builtin: false })).on).toBe(true)
  })
})
