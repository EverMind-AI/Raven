// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// History recall has to survive being edited. Recalling an entry used to pin
// the composer to that index: typing over it left the index in place, so the
// next arrow key stepped to a neighbouring entry and threw the edit away.
// A double-Esc discard has the mirror duty: the cleared line must be pushed to
// history first, or Up can never bring it back.
//
// Both are driven here through the real composer hook and the real input
// handler with fake stdin. The detach predicate alone would not prove the
// wiring, and the wiring alone would not show which text becomes the draft.

import { renderSync } from '@hermes/ink'
import { rmSync } from 'node:fs'
import React from 'react'
import { PassThrough } from 'stream'
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ComposerActions, ComposerRefs, ComposerState, InputHandlerContext } from '../app/interfaces.js'

import { resetDirectChat } from '../app/directChatStore.js'
import { setInputSelection } from '../app/inputSelectionStore.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useComposerState } from '../app/useComposerState.js'
import { useInputHandlers } from '../app/useInputHandlers.js'
import { buildAppComposer } from '../app/useMainApp.js'
import { useSubmission } from '../app/useSubmission.js'
import { shouldDetachEditedHistoryInput } from '../hooks/useInputHistory.js'
import { DEFAULT_VOICE_RECORD_KEY } from '../lib/platform.js'

// history.ts resolves RAVEN_HOME when it is first imported, which happens
// before any beforeAll could stub it. Point it at a scratch dir up front so
// these tests never touch the real ~/.raven/.raven_history.
const scratch = vi.hoisted(() => {
  const base = process.env.TMPDIR || process.env.TEMP || process.env.TMP || '/tmp'
  const dir = `${base}/raven-input-history-recall-${process.pid}`
  const previous = process.env.RAVEN_HOME

  process.env.RAVEN_HOME = dir

  return { dir, previous }
})

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))
const noop = () => {}

interface Composer {
  actions: ComposerActions
  refs: ComposerRefs
  state: ComposerState
}

interface GatewayCall {
  method: string
  params?: Record<string, unknown>
}

const terminal: InputHandlerContext['terminal'] = {
  hasSelection: false,
  scrollRef: { current: null },
  scrollWithSelection: noop,
  selection: {
    captureScrolledRows: noop,
    clearSelection: noop,
    copySelection: async () => ({}) as never,
    copySelectionNoClear: async () => ({}) as never,
    getState: noop,
    version: () => 0,
    shiftAnchor: noop,
    shiftSelection: noop
  },
  stdout: { rows: 24 } as unknown as NodeJS.WriteStream
}

const cursorAtEnd = (value: string) =>
  setInputSelection({ clear: noop, collapseToEnd: noop, end: value.length, start: value.length, value })

const mount = () => {
  const stdin = new PassThrough()
  const stdout = new PassThrough()
  const submitRef = { current: (_value: string) => {} }
  const latest: { current: null | Composer } = { current: null }
  const calls: GatewayCall[] = []
  const gw = {
    request: async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      return null
    }
  } as unknown as InputHandlerContext['gateway']['gw']
  const gateway = { gw, rpc: async () => null } as unknown as InputHandlerContext['gateway']

  Object.assign(stdout, { columns: 80, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: noop, setRawMode: noop, unref: noop })
  stdout.resume()

  function Harness() {
    const composer = useComposerState({ gw, submitRef })

    // Layout effect, not render: the compiler flags writes to outer variables
    // during render, and renderSync commits layout effects before returning.
    React.useLayoutEffect(() => {
      latest.current = composer
    })

    // Beside the composer, as the page has it: the submission path owns the
    // text that reaches history, and the input handler owns the keys that
    // recall it.
    const submission = useSubmission({
      appendMessage: noop,
      composerActions: composer.actions,
      composerRefs: composer.refs,
      composerState: composer.state,
      gw,
      maybeGoodVibes: noop,
      setLastUserMsg: noop,
      slashRef: { current: () => false },
      submitRef,
      sys: noop
    })

    useInputHandlers({
      actions: {
        answerClarify: noop,
        appendMessage: noop,
        die: noop,
        dispatchSubmission: submission.dispatchSubmission,
        guardBusySessionSwitch: () => false,
        newSession: noop,
        sys: noop
      },
      composer,
      gateway,
      terminal,
      voice: {
        enabled: false,
        recordKey: DEFAULT_VOICE_RECORD_KEY,
        recording: false,
        setProcessing: noop,
        setRecording: noop,
        setVoiceEnabled: noop
      },
      wheelStep: 1
    })

    return null
  }

  const instance = renderSync(<Harness />, {
    patchConsole: false,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  return {
    calls,
    composer: () => latest.current!,
    press: async (sequence: string, wait = 80) => {
      stdin.write(sequence)
      await delay(wait)
    },
    submit: (value: string) => submitRef.current(value),
    unmount: () => {
      instance.unmount()
      instance.cleanup()
    }
  }
}

beforeEach(() => {
  resetUiState()
  resetOverlayState()
  resetDirectChat()
  setInputSelection(null)
})

afterAll(() => {
  if (scratch.previous === undefined) {
    delete process.env.RAVEN_HOME
  } else {
    process.env.RAVEN_HOME = scratch.previous
  }

  rmSync(scratch.dir, { force: true, recursive: true })
})

describe('shouldDetachEditedHistoryInput', () => {
  const history = ['older message', 'line one\nline two']

  it('detaches a recalled entry as soon as the user edits it', () => {
    expect(shouldDetachEditedHistoryInput(1, history, 'line one edited\nline two')).toBe(true)
  })

  it('keeps an unchanged recalled entry in history navigation', () => {
    expect(shouldDetachEditedHistoryInput(1, history, 'line one\nline two')).toBe(false)
  })

  it('does not detach an ordinary draft', () => {
    expect(shouldDetachEditedHistoryInput(null, history, 'new draft')).toBe(false)
  })
})

describe('the composer change handler the layout receives', () => {
  it('is updateInput, not the raw setInput', () => {
    const updateInput = vi.fn()
    const setInput = vi.fn()
    const composer = buildAppComposer({
      cols: 80,
      composerActions: { setInput, updateInput } as unknown as ComposerActions,
      composerState: {
        compIdx: 0,
        completions: [],
        input: 'recalled',
        inputBuf: [],
        queueEditIdx: null,
        queuedDisplay: []
      } as unknown as ComposerState,
      empty: false,
      pagerPageSize: 10,
      submit: noop,
      voiceRecordKey: DEFAULT_VOICE_RECORD_KEY
    })

    // setInput would move the caret without detaching the history index, so
    // the next Up would throw the user's edit away.
    expect(composer.updateInput).toBe(updateInput)
  })
})

describe('editing a recalled history entry', () => {
  it('detaches it and restores the edit as the draft after a full cycle', async () => {
    const app = mount()

    try {
      app.composer().actions.pushHistory('first entry')
      app.composer().actions.pushHistory('second entry')

      // Up from an empty composer recalls the newest entry.
      cursorAtEnd('')
      await app.press('\x1b[A')

      expect(app.composer().state.input).toBe('second entry')
      expect(app.composer().state.historyIdx).toBe(app.composer().refs.historyRef.current.length - 1)

      app.composer().actions.updateInput('second entry, edited')
      await delay(20)

      // The edit is no longer a window onto a history entry.
      expect(app.composer().state.input).toBe('second entry, edited')
      expect(app.composer().state.historyIdx).toBeNull()

      // Up still walks history, and the edited text is saved as the draft.
      cursorAtEnd('second entry, edited')
      await app.press('\x1b[A')

      expect(app.composer().state.input).toBe('second entry')

      // Down past the newest entry hands the edit back instead of losing it.
      cursorAtEnd('second entry')
      await app.press('\x1b[B')

      expect(app.composer().state.input).toBe('second entry, edited')
      expect(app.composer().state.historyIdx).toBeNull()
    } finally {
      app.unmount()
    }
  })
})

describe('double-Esc clearing a draft', () => {
  it('pushes the cleared line to history so Up can recall it', async () => {
    const app = mount()

    try {
      app.composer().actions.updateInput('a draft worth keeping')
      await delay(20)

      expect(app.composer().state.input).toBe('a draft worth keeping')

      cursorAtEnd('a draft worth keeping')
      await app.press('\x1b') // arm
      await app.press('\x1b') // clear

      expect(app.composer().state.input).toBe('')

      cursorAtEnd('')
      await app.press('\x1b[A')

      expect(app.composer().state.input).toBe('a draft worth keeping')
    } finally {
      app.unmount()
    }
  })
})

describe('double-Esc clearing a collapsed paste', () => {
  it('pushes the expanded body to history, so Up recalls text and not a dead label', async () => {
    const app = mount()

    try {
      app.composer().actions.setPasteSnips([{ label: '[[paste 1]]', text: 'the body' }])
      app.composer().actions.updateInput('please read [[paste 1]]')
      await delay(20)

      cursorAtEnd('please read [[paste 1]]')
      await app.press('\x1b') // arm
      await app.press('\x1b') // clear

      expect(app.composer().state.input).toBe('')
      expect(app.composer().state.pasteSnips).toEqual([])

      // The snippets are gone, so a label in history could never be expanded
      // again; recalling it would send "[[paste 1]]" to the model verbatim.
      cursorAtEnd('')
      await app.press('\x1b[A')

      expect(app.composer().state.input).toBe('please read the body')
    } finally {
      app.unmount()
    }
  })
})

describe('history content for a collapsed paste', () => {
  it('stores and recalls the pasted body, and the sent text is expanded exactly once', async () => {
    const app = mount()

    try {
      patchUiState({ sid: 'sess-1' })

      // The first pasted body holds the second label: expanding the text a
      // second time would replace it with "MUST NOT LEAK", so a one-pass
      // result proves the expanded string never re-enters the expander.
      app.composer().actions.setPasteSnips([
        { label: '[[paste 1]]', text: 'the body with [[paste 2]] inside' },
        { label: '[[paste 2]]', text: 'MUST NOT LEAK' }
      ])
      await delay(20)

      app.submit('please read [[paste 1]]')
      await delay(40)

      const resolved = 'please read the body with [[paste 2]] inside'

      expect(app.composer().refs.historyRef.current.at(-1)).toBe(resolved)

      const submitted = app.calls.filter(call => call.method === 'prompt.submit')

      expect(submitted).toHaveLength(1)
      expect(submitted[0]!.params).toMatchObject({ text: resolved })

      // Recall hands the body back, not the label.
      cursorAtEnd('')
      await app.press('\x1b[A')

      expect(app.composer().state.input).toBe(resolved)
    } finally {
      app.unmount()
    }
  })
})
