// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.
//
// The status ticker of the agent Raven is driving, as it leaks into the
// reasoning stream. Wrapped agents in the hermes / Claude Code / Codex line all
// render "<kaomoji> <verb>..." and some of them put it on the same channel the
// reasoning arrives on, where it lands mid-thought. `cleanThinkingText` strips
// exactly this pair.
//
// Frozen, and deliberately not Raven's own `FACES` / `VERBS`. The two were one
// list once, which made a branding change silently change what gets censored
// out of a model's reasoning -- and moving Raven's words off this vocabulary
// would have stopped filtering the leak it exists for. What belongs here is
// what upstream emits; what Raven shows is a separate decision.

export const NOISE_FACES = [
  '(｡•́︿•̀｡)',
  '(◔_◔)',
  '(¬‿¬)',
  '( •_•)>⌐■-■',
  '(⌐■_■)',
  '(´･_･`)',
  '◉_◉',
  '(°ロ°)',
  '( ˘⌣˘)♡',
  'ヽ(>∀<☆)☆',
  '٩(๑❛ᴗ❛๑)۶',
  '(⊙_⊙)',
  '(¬_¬)',
  '( ͡° ͜ʖ ͡°)',
  'ಠ_ಠ'
]

export const NOISE_VERBS = [
  'pondering',
  'contemplating',
  'musing',
  'cogitating',
  'ruminating',
  'deliberating',
  'mulling',
  'reflecting',
  'processing',
  'reasoning',
  'analyzing',
  'computing',
  'synthesizing',
  'formulating',
  'brainstorming'
]
