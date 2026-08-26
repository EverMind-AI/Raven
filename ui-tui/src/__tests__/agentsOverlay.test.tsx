// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { Text } from '@hermes/ink'
import { render } from 'ink-testing-library'
import React from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import { $overlaySectionsOpen, toggleOverlaySection } from '../app/delegationStore.js'
import { OverlaySection } from '../components/agentsOverlay.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const section = (scope: string, title: string, body: string) =>
  stripAnsi(
    render(
      <OverlaySection defaultOpen id="transcript" scope={scope} t={DEFAULT_THEME} title={title}>
        <Text>{body}</Text>
      </OverlaySection>
    ).lastFrame() ?? ''
  )

describe('OverlaySection', () => {
  beforeEach(() => {
    $overlaySectionsOpen.set({})
  })

  it('holds one open state per run, not one per section name', () => {
    // What a click on run-a's Transcript header does.
    toggleOverlaySection('run-a:transcript', true)

    expect(section('run-a', 'Transcript', 'ALPHA BODY')).not.toContain('ALPHA BODY')
    expect(section('run-b', 'Transcript', 'BETA BODY')).toContain('BETA BODY')
  })

  it('keeps that state when the title changes as the run finishes', () => {
    toggleOverlaySection('run-a:transcript', true)

    // The live and settled headers of the same section: the reader closed it
    // while it ran, and it must not reopen itself the moment the run lands.
    expect(section('run-a', 'Transcript · live', 'BODY')).not.toContain('BODY')
    expect(section('run-a', 'Transcript', 'BODY')).not.toContain('BODY')
  })
})
