import { describe, expect, it } from 'vitest'

import { parseDagReceipt, parseSpawnReceipt } from './nestedRun'

describe('parseSpawnReceipt', () => {
  it('reads the label and the manager\'s own id out of the receipt', () => {
    expect(parseSpawnReceipt("Subagent [research CO] started (id: 1a021575). I'll notify you when it completes."))
      .toEqual({ label: 'research CO', taskId: '1a021575' })
  })

  it('reads the outer receipt when the label itself quotes another one', () => {
    expect(parseSpawnReceipt('Subagent [inspect log: started (id: deadbeef)] started (id: 1a021575). '))
      .toEqual({ label: 'inspect log: started (id: deadbeef)', taskId: '1a021575' })
  })

  it('answers null for anything else', () => {
    expect(parseSpawnReceipt('Refused: delegation is paused.')).toBeNull()
    expect(parseSpawnReceipt('')).toBeNull()
  })
})

describe('parseDagReceipt', () => {
  it('reads the run id out of the graph tool\'s own receipt', () => {
    expect(parseDagReceipt('DAG 20260919T120000000000Z-ab12: 3 nodes started')).toEqual({
      runId: '20260919T120000000000Z-ab12',
    })
  })

  it('reads the run id out of a playbook receipt, which has no node count', () => {
    expect(parseDagReceipt("DAG 20260919T120000000000Z-ab12: started 'nightly-checks' (4 steps); "
      + 'results will be delivered when the run completes.')).toEqual({
      runId: '20260919T120000000000Z-ab12',
    })
  })

  it('answers null for a receipt that never says the run started', () => {
    expect(parseDagReceipt('DAG run r1: done')).toBeNull()
    expect(parseDagReceipt('Error running DAG r1: boom')).toBeNull()
    expect(parseDagReceipt('')).toBeNull()
  })
})
