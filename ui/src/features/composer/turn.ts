/* The current conversation's turn phase and its pure transition function. */

export type TurnPhase = 'idle' | 'sending' | 'streaming' | 'waiting' | 'cancelling'

interface ResumeState {
  phase: 'sending' | 'streaming'
  cancellable: boolean
}

export interface TurnSnapshot {
  phase: TurnPhase
  cancellable: boolean
  resume: ResumeState | null
}

export type TurnEvent =
  | { type: 'send' }
  | { type: 'stream'; cancellable: boolean }
  | { type: 'wait' }
  | { type: 'resume' }
  | { type: 'cancel' }
  | { type: 'idle' }

const idle = (): TurnSnapshot => ({ phase: 'idle', cancellable: false, resume: null })

let state = idle()

const copy = (value: TurnSnapshot): TurnSnapshot => ({
  phase: value.phase,
  cancellable: value.cancellable,
  resume: value.resume ? { ...value.resume } : null,
})

export function reduce(value: TurnSnapshot, event: TurnEvent): TurnSnapshot {
  if (event.type === 'idle') return idle()
  if (event.type === 'send') return { phase: 'sending', cancellable: true, resume: null }
  if (event.type === 'stream') {
    return { phase: 'streaming', cancellable: event.cancellable, resume: null }
  }
  if (event.type === 'cancel') {
    if (value.phase === 'idle') return value
    return { phase: 'cancelling', cancellable: false, resume: null }
  }
  if (event.type === 'wait') {
    if (value.phase === 'idle' || value.phase === 'cancelling' || value.phase === 'waiting') return value
    return {
      phase: 'waiting',
      cancellable: value.cancellable,
      resume: { phase: value.phase, cancellable: value.cancellable },
    }
  }
  if (value.phase !== 'waiting' || !value.resume) return value
  return { phase: value.resume.phase, cancellable: value.resume.cancellable, resume: null }
}

export function dispatch(event: TurnEvent): void {
  state = reduce(state, event)
}

export const phase = (): TurnPhase => state.phase

export const busy = (): boolean => state.phase !== 'idle'

export const cancellable = (): boolean => state.cancellable && state.phase !== 'cancelling'

export const snapshot = (): TurnSnapshot => copy(state)

export function restore(value: TurnSnapshot): void {
  state = copy(value)
}

export function _resetForTests(): void {
  state = idle()
}
