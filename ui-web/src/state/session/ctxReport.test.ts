// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setCode } from '../../i18n/t'
import { _resetForTests, get } from '../ctxChip'
import { applyUsage } from './ctxReport'

beforeEach(() => {
  resetTranslator()
  setCode('en')
  _resetForTests()
})

afterEach(() => {
  _resetForTests()
  resetTranslator()
})

describe('a usage report on the context ring', () => {
  it('draws the window the report measured', () => {
    applyUsage({ context_used: 7000, context_max: 40000 })
    expect(get().tip).toBe('Context 7.0k/40k 18%')
  })

  it('stands the fallback in only for a report that measured no window', () => {
    applyUsage({ context_used: 0, context_max: 40000 }, 3100)
    expect(get().tip).toBe('Context 3.1k/40k 8%')
    applyUsage({ context_used: 7000, context_max: 40000 }, 3100)
    expect(get().tip).toBe('Context 7.0k/40k 18%')
  })

  it("keeps a resumed session's zero, which is an empty session and not a gap", () => {
    applyUsage({ context_used: 7000, context_max: 40000 })
    applyUsage({ context_used: 0, context_max: 40000 })
    expect(get().tip).toBe('Context 0/40k 0%')
  })

  it('moves nothing without a report', () => {
    applyUsage({ context_used: 7000, context_max: 40000 })
    applyUsage(undefined)
    expect(get().tip).toBe('Context 7.0k/40k 18%')
  })
})
