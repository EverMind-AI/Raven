// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

export const CONFIRM_COUNTDOWN_SECONDS = 30

export interface CountdownTick {
  autoCancel: boolean
  remaining: number
}

/**
 * Pure one-second step of the confirm countdown.  Returns the next
 * `remaining` value (floored at 0) and whether the floor was reached —
 * at which point the caller must auto-cancel (answer false).
 */
export const tickCountdown = (remaining: number): CountdownTick => {
  const next = Math.max(0, remaining - 1)

  return { autoCancel: next <= 0, remaining: next }
}

export const buildConfirmRespond = (requestId: string, answer: boolean) => ({
  answer,
  request_id: requestId
})
