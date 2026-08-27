// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { describe, expect, it } from 'vitest'

import { modelSelectCommand } from '../app/useMainApp.js'

describe('modelSelectCommand', () => {
  it('carries --default through the picker that was opened with it', () => {
    // The defect this closes: `/model --default` opened the picker, the
    // selection came back without the flag, and the switch was session-scoped
    // while the transcript said the model changed. The user saw a success for a
    // default they had asked to change and had not.
    expect(modelSelectCommand('claude-opus-4-5', 'anthropic', 'default')).toBe(
      '/model claude-opus-4-5 --provider anthropic --default'
    )
  })

  it('leaves a plain selection scoped to this conversation', () => {
    // The other direction matters as much: adding the flag unasked would move
    // the default for every new session because someone picked a model once.
    expect(modelSelectCommand('claude-opus-4-5', 'anthropic', true)).toBe('/model claude-opus-4-5 --provider anthropic')
  })

  it('treats a closed picker as session scope', () => {
    // `false` is the overlay's resting value. It should never reach here, but
    // the fallback that matters is the narrow one -- widening a scope by
    // accident is the failure worth being unable to have.
    expect(modelSelectCommand('m', 'p', false)).toBe('/model m --provider p')
  })

  it('spells the flag form the parser accepts', () => {
    // `parseModelArg` reads `<id> --provider <name>`; a bare id is refused, and
    // `--default` is stripped before parsing in any position. This is the one
    // spelling that survives all three rules, so the assertion is on the exact
    // string rather than on its parts.
    const command = modelSelectCommand('anthropic/claude-haiku-4-5', 'openrouter', 'default')

    expect(command).toBe('/model anthropic/claude-haiku-4-5 --provider openrouter --default')
    expect(command.replace(/(^|\s)--default(?=\s|$)/g, '').trim()).toBe(
      '/model anthropic/claude-haiku-4-5 --provider openrouter'
    )
  })
})
