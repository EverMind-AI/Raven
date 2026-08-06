// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it, vi } from 'vitest'

import type { ModelOptionProvider } from '../gatewayTypes.js'

import { ModelPicker } from '../components/modelPicker.js'
import { DEFAULT_THEME } from '../theme.js'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const waitForFrame = async (h: Pick<Harness, 'frame'>, text: string) => {
  for (let i = 0; i < 20; i++) {
    if (h.frame().includes(text)) {
      return
    }
    await delay(30)
  }
  expect(h.frame()).toContain(text)
}

const ESC_RE = new RegExp(String.fromCharCode(27), 'g')

// ink emits cursor-forward moves (CSI nC) in place of spaces for alignment, so
// strip every CSI sequence and collapse whitespace into single spaces before
// matching on screen text.
const normalize = (raw: string) =>
  raw
    .replace(new RegExp(`${String.fromCharCode(27)}\\[[0-9;?<>=]*[a-zA-Z]`, 'g'), ' ')
    .replace(new RegExp(`${String.fromCharCode(27)}\\][^\\u0007]*\\u0007?`, 'g'), ' ')
    .replace(ESC_RE, ' ')
    .replace(/\s+/g, ' ')

const ENTER = '\r'
const DOWN = '[B'

const ESCAPE = String.fromCharCode(27)

const anthropic: ModelOptionProvider = {
  auth_type: 'key',
  authenticated: true,
  is_current: true,
  key_env: 'ANTHROPIC_API_KEY',
  models: ['claude-sonnet-4-6'],
  name: 'Anthropic',
  needs_api_base: false,
  slug: 'anthropic',
  total_models: 1
}

const ollama: ModelOptionProvider = {
  auth_type: 'local',
  authenticated: false,
  is_current: false,
  key_env: null,
  models: [],
  name: 'Ollama (local)',
  needs_api_base: true,
  slug: 'ollama_chat',
  total_models: 0,
  warning: 'enter the server address to activate'
}

const custom: ModelOptionProvider = {
  auth_type: 'endpoint',
  authenticated: false,
  is_current: false,
  key_env: null,
  models: [],
  name: 'Custom',
  needs_api_base: true,
  slug: 'custom',
  total_models: 0,
  warning: 'set key + base to activate'
}

const deepseek: ModelOptionProvider = {
  auth_type: 'key',
  authenticated: true,
  is_current: true,
  key_env: 'DEEPSEEK_API_KEY',
  models: ['deepseek-chat'],
  name: 'DeepSeek',
  needs_api_base: false,
  slug: 'deepseek',
  total_models: 1
}

const oauthProvider: ModelOptionProvider = {
  auth_type: 'oauth',
  authenticated: false,
  is_current: false,
  key_env: null,
  models: [],
  name: 'MiniMax Global',
  needs_api_base: false,
  slug: 'minimax_global',
  total_models: 0,
  warning: 'run `raven provider login minimax-global` to authenticate'
}

interface MountExtras {
  /** What the spawned `raven provider login` resolves to. */
  launch?: { code: null | number; error?: string }
  /** The provider list every `model.options` call after the first one returns. */
  nextProviders?: ModelOptionProvider[]
}

interface Harness {
  frame: () => string
  gw: { request: ReturnType<typeof vi.fn> }
  onCancel: ReturnType<typeof vi.fn>
  launchedInsideSuspend: () => boolean
  launcher: ReturnType<typeof vi.fn>
  onSelect: ReturnType<typeof vi.fn>
  optionsCalls: () => number
  suspend: ReturnType<typeof vi.fn>
  type: (s: string) => Promise<void>
  unmount: () => void
}

const mount = (
  providers: ModelOptionProvider[],
  requestImpl?: (m: string, p: any) => unknown,
  extras?: MountExtras
): Harness => {
  const onSelect = vi.fn()
  const onCancel = vi.fn()
  let optionsCalls = 0
  const request = vi.fn((method: string, params: Record<string, unknown>) => {
    if (method === 'model.options') {
      optionsCalls += 1
      const list = optionsCalls > 1 && extras?.nextProviders ? extras.nextProviders : providers

      return Promise.resolve({ model: 'claude-sonnet-4-6', provider: 'anthropic', providers: list })
    }

    return Promise.resolve(requestImpl ? requestImpl(method, params) : {})
  })
  const gw = { request } as unknown as { request: typeof request }

  // The handoff is asserted through these two rather than through the screen:
  // `frame()` accumulates every write, so the command line the provider row
  // already printed as a warning would match a screen assertion either way.
  let insideSuspend = false
  let launchedInsideSuspend = false
  const launcher = vi.fn(async (_args: string[]) => {
    launchedInsideSuspend = insideSuspend

    return extras?.launch ?? { code: 0 }
  })
  const suspend = vi.fn(async (run: () => Promise<void>) => {
    insideSuspend = true
    try {
      await run()
    } finally {
      insideSuspend = false
    }
  })

  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(
    <ModelPicker
      gw={gw as never}
      launcher={launcher as never}
      onCancel={onCancel}
      onSelect={onSelect}
      sessionId="tui:session-1"
      suspend={suspend as never}
      t={DEFAULT_THEME}
    />,
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  return {
    frame: () => normalize(output),
    gw: gw as never,
    launchedInsideSuspend: () => launchedInsideSuspend,
    launcher,
    onCancel,
    onSelect,
    optionsCalls: () => optionsCalls,
    suspend,
    type: async (s: string) => {
      stdin.write(s)
      await delay(30)
    },
    unmount: () => {
      instance.unmount()
      instance.cleanup()
    }
  }
}

describe('ModelPicker', () => {
  it('opens with the cursor on the provider in use', async () => {
    // The selection is located in the list the first screen shows. Finding it
    // among all of them instead put it past the end of the configured ones, and
    // a selection past the end highlights nothing -- the picker opened with no
    // cursor. Asserted by pressing Enter with no arrow keys first: whatever the
    // cursor is on is what opens.
    const cfg = (slug: string, name: string, authed: boolean, cur = false): ModelOptionProvider =>
      ({
        auth_type: 'key',
        authenticated: authed,
        is_current: cur,
        key_env: null,
        models: authed ? [`${slug}/m1`] : [],
        name,
        needs_api_base: false,
        slug,
        total_models: authed ? 1 : 0
      }) as ModelOptionProvider

    // DeepSeek is index 3 of five, but index 1 of the two that are set up.
    const h = mount([
      cfg('openrouter', 'OpenRouter', true),
      cfg('openai', 'OpenAI', false),
      cfg('anthropic', 'Anthropic', false),
      cfg('deepseek', 'DeepSeek', true, true),
      cfg('gemini', 'Gemini', false)
    ])
    await delay(80)

    await h.type(ENTER)
    const frame = h.frame()
    expect(frame).toContain('deepseek/m1')
    expect(frame).not.toContain('openrouter/m1')
    h.unmount()
  })

  it('lists what is set up, and puts the rest behind one row', async () => {
    // Opening the picker used to mean scrolling twenty-one rows to reach the two
    // or three that can actually serve a model. A provider with no credentials is
    // not something to switch to, so it moves one level down.
    const h = mount([anthropic, custom, ollama, oauthProvider])
    await delay(60)

    const first = h.frame()
    expect(first).toContain('Anthropic')
    expect(first).toContain('add a provider')
    expect(first).toContain('3 not set up')
    expect(first).not.toContain('Custom')
    expect(first).not.toContain('Ollama')

    // Down once lands on the add row -- the only other row at this level.
    await h.type(DOWN)
    await h.type(ENTER)

    const second = h.frame()
    expect(second).toContain('Add a provider')
    expect(second).toContain('Ollama')

    // Esc returns to level one rather than closing the picker.
    await h.type('\u001b')
    await delay(30)
    expect(h.frame()).toContain('Select pr')
    h.unmount()
  })

  it('offers a local deployment its address, and says it needs no key', async () => {
    // The picker knew two credential shapes and reported every non-OAuth provider
    // as taking an API key, so a local deployment was offered a key prompt it
    // cannot use and never the address it is reached by -- the one thing it needs.
    const h = mount([anthropic, ollama])
    await delay(60)

    // Level one lists what is set up, so the unconfigured one is behind the row
    // that opens level two.
    expect(h.frame()).not.toContain('Ollama')
    await h.type(DOWN)
    await h.type(ENTER)

    expect(h.frame()).toContain('(no address)')
    await h.type(ENTER)

    // Asserted on the field labels rather than the "Configure <name>" heading:
    // at this depth the fake terminal's escape stripping eats the odd character
    // out of the accumulated frame, and the labels are what the screen is for.
    const frame = h.frame()
    expect(frame).toContain('Ollama (local)')
    expect(frame).toContain('Server address')
    // No key field at all: the server ignores one, so asking reads as a blocker.
    expect(frame).not.toContain('API key')

    // And it submits with no key. The client demanded one unconditionally, so
    // this was unreachable even after the backend stopped requiring it.
    await h.type('http://gpu-box:11434')
    await h.type(ENTER)
    await delay(30)

    expect(h.gw.request).toHaveBeenCalledWith(
      'model.save_key',
      expect.objectContaining({ api_base: 'http://gpu-box:11434', slug: 'ollama_chat' })
    )
    const call = (h.gw.request as unknown as { mock: { calls: unknown[][] } }).mock.calls.find(
      c => c[0] === 'model.save_key'
    )
    expect((call?.[1] as { api_key?: string }).api_key).toBe('')
    h.unmount()
  })

  it('shows the api_base field and requires it for a needs_api_base provider', async () => {
    const h = mount([anthropic, custom])
    await delay(60)

    // Custom is not set up, so it lives behind the add row.
    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)

    const keyFrame = h.frame()
    expect(keyFrame).toContain('Custom')
    expect(keyFrame).toContain('API key')
    expect(keyFrame).toContain('API base (required)')

    // Type a key, advance to api_base via Enter, then submit with empty base.
    await h.type('sk-test')
    await h.type(ENTER)
    await h.type(ENTER)

    expect(h.frame()).toContain('API base URL is required')
    expect(h.gw.request).not.toHaveBeenCalledWith('model.save_key', expect.anything())

    // Fill the base and submit — save_key carries api_base.
    await h.type('https://api.example.com')
    await h.type(ENTER)

    expect(h.gw.request).toHaveBeenCalledWith(
      'model.save_key',
      expect.objectContaining({ api_base: 'https://api.example.com', api_key: 'sk-test', slug: 'custom' })
    )

    h.unmount()
  })

  it('sends OAuth providers to the sign-in screen, not the key prompt', async () => {
    const h = mount([anthropic, oauthProvider])
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    expect(h.frame()).toContain('raven provider login')

    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    // A browser flow has no key to paste, so the key stage stays out of it.
    expect(h.gw.request).not.toHaveBeenCalledWith('model.save_key', expect.anything())
    expect(h.launcher).not.toHaveBeenCalled()

    h.unmount()
  })

  it('lets a key with a q in it be typed', async () => {
    // Bare `q` closes the overlay, which on the key screen meant the character
    // dismissed the picker instead of landing in the field: any key containing a
    // `q` was impossible to enter.
    const h = mount([anthropic, custom])
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Tab switches field')

    // One key at a time: a whole string arrives as a single input event, which
    // never equals the bare `q` the overlay closes on.
    for (const ch of 'sk-q1') {
      await h.type(ch)
    }

    // Asserted on the callback rather than the field: ink redraws
    // incrementally, so the accumulated output never holds the whole mask.
    expect(h.onCancel).not.toHaveBeenCalled()

    h.unmount()
  })

  it('runs `provider login` inside the Ink suspend on the sign-in screen', async () => {
    const h = mount([anthropic, oauthProvider])
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    await h.type(ENTER)
    await delay(60)

    expect(h.launcher).toHaveBeenCalledWith(['provider', 'login', 'minimax-global'])
    expect(h.suspend).toHaveBeenCalledTimes(1)
    expect(h.launchedInsideSuspend()).toBe(true)

    h.unmount()
  })

  it('lands on the signed-in provider after a sign-in that took', async () => {
    // An unconfigured provider sits BEFORE the target, so the index into the
    // configured list differs from the index into the whole response. Reading the
    // wrong one lands on a provider the user did not sign in to -- and every
    // fixture where the target comes first hides that, because the two agree.
    const signedIn = { ...oauthProvider, authenticated: true, models: ['MiniMax-M2'], total_models: 1 }
    const h = mount([custom, oauthProvider, anthropic], undefined, {
      nextProviders: [custom, signedIn, anthropic]
    })
    await delay(60)

    // Level one is the configured list (anthropic) plus the add row after it;
    // inside the add list the target is the second unconfigured provider.
    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(DOWN)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')
    expect(h.optionsCalls()).toBe(1)

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')

    expect(h.optionsCalls()).toBe(2)
    await h.type(ENTER)
    await delay(30)

    // The model belongs to that provider alone, so it cannot have come from
    // whoever else the cursor might have landed on.
    expect(h.onSelect).toHaveBeenCalledWith('MiniMax-M2', 'minimax_global')

    h.unmount()
  })

  it('says so when disconnecting the provider the session is using', async () => {
    // The model id survives the disconnect and still names this provider, so the
    // config is left pointing at one that cannot answer -- and the startup gate
    // reads that as "not set up".
    const h = mount([{ ...deepseek, is_current: true }, oauthProvider])
    await delay(60)

    await h.type('d')
    await waitForFrame(h, 'serves your current model')

    h.unmount()
  })

  it('keeps the sign-in screen when the login command fails', async () => {
    const h = mount([anthropic, oauthProvider], undefined, { launch: { code: 1 } })
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    await h.type(ENTER)
    await waitForFrame(h, 'exited with code 1')

    // A failed login is not a refetch, and it does not move the user anywhere.
    expect(h.optionsCalls()).toBe(1)
    expect(h.frame()).not.toContain('step 2/2')

    h.unmount()
  })

  it('returns from the sign-in screen to the list it was opened from', async () => {
    const h = mount([anthropic, oauthProvider])
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    await h.type(ESCAPE)
    await delay(30)

    // Asserted by what the next Enter reaches, not by screen text: `frame()`
    // accumulates, so the add list it came from is on screen either way. One
    // level too far out lands on the configured list, whose first row is a
    // provider with models.
    await h.type(ENTER)
    await h.type(ENTER)
    await delay(60)

    expect(h.launcher).toHaveBeenCalledWith(['provider', 'login', 'minimax-global'])
    expect(h.frame()).not.toContain('step 2/2')

    h.unmount()
  })

  it('re-anchors the cursor when a sign-in empties the row it came from', async () => {
    // The sign-in target leaves the unconfigured list on success, so the row
    // under the cursor is now a different provider. Answering "did the set-up
    // happen?" from that row sends the user back into the add list, pointing at
    // somebody they never touched.
    const other = { ...oauthProvider, name: 'Second Unconfigured', slug: 'second_unconfigured' }
    const h = mount([anthropic, oauthProvider, other], undefined, {
      nextProviders: [
        anthropic,
        { ...oauthProvider, authenticated: true, models: ['MiniMax-M2'], total_models: 1 },
        other
      ]
    })
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')

    // Asserted through the launcher rather than the screen: `frame()` accumulates
    // and the fake terminal drops the odd character, so a title assertion matches
    // nothing either way. Backing out of the model list reaches the configured
    // list; a cursor left pointing into the list the provider vacated would start
    // a login for whoever took its index instead.
    await h.type(ESCAPE)
    await h.type(ENTER)
    await h.type(ENTER)
    await delay(60)

    expect(h.launcher).toHaveBeenCalledTimes(1)
    expect(h.launcher).not.toHaveBeenCalledWith(['provider', 'login', 'second-unconfigured'])

    h.unmount()
  })

  it('reports a sign-in that leaves no credentials behind', async () => {
    const h = mount([anthropic, oauthProvider])
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    await h.type(ENTER)
    await waitForFrame(h, 'still finds no credentials')

    expect(h.optionsCalls()).toBe(2)

    h.unmount()
  })

  it('adds a model name via model.add_model and refreshes the list', async () => {
    const h = mount([anthropic], (method, params) => {
      if (method === 'model.add_model') {
        return { provider: { ...anthropic, models: [...anthropic.models!, params.model], total_models: 2 } }
      }

      return {}
    })
    await delay(60)

    // Enter the authenticated anthropic provider's model stage.
    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')

    // 'a' opens the add-model sub-input.
    await h.type('a')
    expect(h.frame()).toContain('Type the full model id')

    await h.type('claude-opus-4')
    await h.type(ENTER)

    expect(h.gw.request).toHaveBeenCalledWith(
      'model.add_model',
      expect.objectContaining({ model: 'claude-opus-4', slug: 'anthropic' })
    )

    h.unmount()
  })

  it('removes the selected model name via model.remove_model', async () => {
    const twoModels = { ...anthropic, models: ['claude-sonnet-4-6', 'claude-opus-4'], total_models: 2 }
    const h = mount([twoModels], method => {
      if (method === 'model.remove_model') {
        return { provider: { ...twoModels, models: ['claude-opus-4'], total_models: 1 } }
      }

      return {}
    })
    await delay(60)

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')
    expect(h.frame()).toContain('claude-sonnet-4-6')

    // Delete the highlighted (first) model.
    await h.type('d')

    expect(h.gw.request).toHaveBeenCalledWith(
      'model.remove_model',
      expect.objectContaining({ model: 'claude-sonnet-4-6', slug: 'anthropic' })
    )

    h.unmount()
  })

  it('emits a structured model + provider selection on Enter', async () => {
    const h = mount([anthropic])
    await delay(60)

    await h.type(ENTER)
    await h.type(ENTER)

    expect(h.onSelect).toHaveBeenCalledWith('claude-sonnet-4-6', 'anthropic')

    h.unmount()
  })
})
