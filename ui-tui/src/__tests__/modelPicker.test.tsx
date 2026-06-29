// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ModelPicker } from '../components/modelPicker.js'
import type { ModelOptionProvider } from '../gatewayTypes.js'
import { DEFAULT_THEME } from '../theme.js'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

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

const anthropic: ModelOptionProvider = {
  auth_type: 'api_key',
  authenticated: true,
  is_current: true,
  key_env: 'ANTHROPIC_API_KEY',
  models: ['claude-sonnet-4-6'],
  name: 'Anthropic',
  needs_api_base: false,
  slug: 'anthropic',
  total_models: 1,
}

const custom: ModelOptionProvider = {
  auth_type: 'api_key',
  authenticated: false,
  is_current: false,
  key_env: null,
  models: [],
  name: 'Custom',
  needs_api_base: true,
  slug: 'custom',
  total_models: 0,
  warning: 'set key + base to activate',
}

const oauthProvider: ModelOptionProvider = {
  auth_type: 'oauth',
  authenticated: false,
  is_current: false,
  key_env: null,
  models: [],
  name: 'OAuth Vendor',
  needs_api_base: false,
  slug: 'oauthvendor',
  total_models: 0,
  warning: 'run raven model to authenticate',
}

interface Harness {
  frame: () => string
  gw: { request: ReturnType<typeof vi.fn> }
  onSelect: ReturnType<typeof vi.fn>
  type: (s: string) => Promise<void>
  unmount: () => void
}

const mount = (providers: ModelOptionProvider[], requestImpl?: (m: string, p: any) => unknown): Harness => {
  const onSelect = vi.fn()
  const request = vi.fn((method: string, params: Record<string, unknown>) => {
    if (method === 'model.options') {
      return Promise.resolve({ model: 'claude-sonnet-4-6', provider: 'anthropic', providers })
    }

    return Promise.resolve(requestImpl ? requestImpl(method, params) : {})
  })
  const gw = { request } as unknown as { request: typeof request }

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
      onCancel={() => {}}
      onSelect={onSelect}
      sessionId="tui:session-1"
      t={DEFAULT_THEME}
    />,
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream,
    }
  )

  return {
    frame: () => normalize(output),
    gw: gw as never,
    onSelect,
    type: async (s: string) => {
      stdin.write(s)
      await delay(30)
    },
    unmount: () => {
      instance.unmount()
      instance.cleanup()
    },
  }
}

describe('ModelPicker', () => {
  it('shows the api_base field and requires it for a needs_api_base provider', async () => {
    const h = mount([anthropic, custom])
    await delay(60)

    // Move to the custom provider (index 1) and enter the key stage.
    await h.type(DOWN)
    await h.type(ENTER)

    const keyFrame = h.frame()
    expect(keyFrame).toContain('Configure Custom')
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

  it('gates OAuth providers: no key prompt, warning shown', async () => {
    const h = mount([anthropic, oauthProvider])
    await delay(60)

    await h.type(DOWN)
    const providerFrame = h.frame()
    expect(providerFrame).toContain('run raven model to authenticate')

    await h.type(ENTER)

    // Still on the provider stage — no key prompt was opened.
    expect(h.frame()).not.toContain('Configure OAuth Vendor')
    expect(h.gw.request).not.toHaveBeenCalledWith('model.save_key', expect.anything())

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
    expect(h.frame()).toContain('step 2/2')

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
    const h = mount([twoModels], (method) => {
      if (method === 'model.remove_model') {
        return { provider: { ...twoModels, models: ['claude-opus-4'], total_models: 1 } }
      }

      return {}
    })
    await delay(60)

    await h.type(ENTER)
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
