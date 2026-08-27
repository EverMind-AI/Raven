import { describe, expect, it, vi } from 'vitest'

import type { CtrlCState } from '../app/useInputHandlers.js'

import { applyVoiceRecordResponse, decideCtrlC } from '../app/useInputHandlers.js'

const ctrlC = (over: Partial<CtrlCState> = {}): CtrlCState => ({
  busyWithSession: false,
  escapeArmed: false,
  hasPendingInput: false,
  turnActive: false,
  ...over
})

describe('decideCtrlC', () => {
  it('quits only from an idle UI with an empty composer', () => {
    expect(decideCtrlC(ctrlC())).toBe('quit')
  })

  it('clears a pending line instead of quitting', () => {
    expect(decideCtrlC(ctrlC({ hasPendingInput: true }))).toBe('clear-input')
  })

  it('cancels the turn on the first press while one is in flight', () => {
    expect(decideCtrlC(ctrlC({ busyWithSession: true, turnActive: true }))).toBe('cancel-turn')
  })

  it('force-resets on the second press when the cancel produced no terminal event', () => {
    expect(decideCtrlC(ctrlC({ busyWithSession: true, escapeArmed: true, turnActive: true }))).toBe('force-reset')
  })

  it('falls back to the legacy interrupt when busy without a typed turn', () => {
    expect(decideCtrlC(ctrlC({ busyWithSession: true }))).toBe('interrupt-legacy')
  })

  it('never quits while busy, whatever the composer holds', () => {
    for (const hasPendingInput of [false, true]) {
      for (const turnActive of [false, true]) {
        expect(decideCtrlC(ctrlC({ busyWithSession: true, hasPendingInput, turnActive }))).not.toBe('quit')
      }
    }
  })

  it('ignores escapeArmed unless a turn is actually active', () => {
    expect(decideCtrlC(ctrlC({ escapeArmed: true }))).toBe('quit')
    expect(decideCtrlC(ctrlC({ busyWithSession: true, escapeArmed: true }))).toBe('interrupt-legacy')
  })
})

describe('applyVoiceRecordResponse', () => {
  it('reverts optimistic REC state when the gateway reports voice busy', () => {
    const setProcessing = vi.fn()
    const setRecording = vi.fn()
    const sys = vi.fn()

    applyVoiceRecordResponse({ status: 'busy' }, true, { setProcessing, setRecording }, sys)

    expect(setRecording).toHaveBeenCalledWith(false)
    expect(setProcessing).toHaveBeenCalledWith(true)
    expect(sys).toHaveBeenCalledWith('voice: still transcribing; try again shortly')
  })

  it('keeps optimistic REC state for successful recording starts', () => {
    const setProcessing = vi.fn()
    const setRecording = vi.fn()

    applyVoiceRecordResponse({ status: 'recording' }, true, { setProcessing, setRecording }, vi.fn())

    expect(setRecording).not.toHaveBeenCalled()
    expect(setProcessing).not.toHaveBeenCalled()
  })

  it('reverts optimistic REC state when the gateway returns null', () => {
    const setProcessing = vi.fn()
    const setRecording = vi.fn()

    applyVoiceRecordResponse(null, true, { setProcessing, setRecording }, vi.fn())

    expect(setRecording).toHaveBeenCalledWith(false)
    expect(setProcessing).toHaveBeenCalledWith(false)
  })
})
