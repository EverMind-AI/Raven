// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

const TAGS = ['think', 'reasoning', 'thinking', 'thought', 'REASONING_SCRATCHPAD'] as const

export interface SplitReasoning {
  reasoning: string
  text: string
}

export function splitReasoning(input: string): SplitReasoning {
  let text = input
  const reasoning: string[] = []

  for (const tag of TAGS) {
    const paired = new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>\\s*`, 'gi')
    text = text.replace(paired, (_m, inner: string) => {
      const trimmed = inner.trim()

      if (trimmed) {
        reasoning.push(trimmed)
      }

      return ''
    })

    const unclosed = new RegExp(`<${tag}>([\\s\\S]*)$`, 'i')
    text = text.replace(unclosed, (_m, inner: string) => {
      const trimmed = inner.trim()

      if (trimmed) {
        reasoning.push(trimmed)
      }

      return ''
    })
  }

  return {
    reasoning: reasoning.join('\n\n').trim(),
    text: text.trim()
  }
}

// A reasoning burst that carries no actual words is noise, not thought: some
// models (e.g. minimax-m3 during a mechanical tool loop) emit reasoning_content
// that is just "." / ".\n.\n" placeholder dots. Require at least one letter or
// digit in any script.
export const hasMeaningfulReasoning = (input: string) => /[\p{L}\p{N}]/u.test(input)

export const hasReasoningTag = (input: string) => {
  for (const tag of TAGS) {
    if (input.includes(`<${tag}>`)) {
      return true
    }
  }

  return false
}
