// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.
//
// The glyph the status ticker turns while a turn runs, in `kaomoji` style.
//
// Every one of them is the same bird wearing a different expression: the beak
// (a theta) is the constant, so the column reads as one character reacting
// rather than a bag of unrelated faces. Not a filter list -- what gets stripped
// out of a leaked ticker lives in `tickerNoise.ts`, deliberately apart.

export const FACES = [
  '(・Θ・)',
  '(◉Θ◉)',
  '(¬Θ¬)',
  '(°Θ°)',
  '(-Θ-)',
  '(˘Θ˘)',
  '(๑Θ‿Θ๑)',
  '(⌐Θ_Θ)',
  'ヽ(Θ∀Θ)ﾉ',
  '(Θ_Θ)',
  '(っΘ‿Θ)っ',
  '(◔Θ◔)'
]
