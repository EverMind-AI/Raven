// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { OnboardApp } from './OnboardPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { OnboardProvider, OnboardSource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const connected: OnboardProvider = {
  slug: 'anthropic',
  name: 'Anthropic',
  homepage: 'https://anthropic.com/',
  auth_type: 'key',
  authenticated: true,
  models: ['claude-fable-5', 'claude-opus-5']
}

const keyed: OnboardProvider = {
  slug: 'openai',
  name: 'OpenAI',
  auth_type: 'key',
  authenticated: false,
  models: ['gpt-5.2']
}

interface Harness {
  source: OnboardSource
  options: OnboardSource['options']
  saves: Array<[string, string, string]>
  models: Array<[string, string]>
  rechecks: number
}

function install(providers: OnboardProvider[] = [connected, keyed], over: Partial<OnboardSource> = {}): Harness {
  const h: Harness = {
    options: vi.fn(async () => ({ providers })),
    saves: [],
    models: [],
    rechecks: 0,
    source: null as unknown as OnboardSource
  }
  h.source = {
    options: h.options,
    saveKey: async (slug, key, base) => {
      h.saves.push([slug, key, base])
      const provider = providers.find(row => row.slug === slug)
      return { provider: provider ? { ...provider, authenticated: true } : undefined }
    },
    setModel: async (model, provider) => {
      h.models.push([model, provider])
    },
    recheck: async () => {
      h.rechecks += 1
      return true
    },
    ...over
  }
  const shell: Shell = {
    T: (key, vars) => (vars?.err ? `${key}:${vars.err}` : key),
    confirmAsk: () => {},
    showPage: () => {}
  }
  window.RavenShell = shell
  window.DS = { onboard: h.source }
  document.body.innerHTML = '<div id="onb" hidden></div>'
  return h
}

const mount = () => render(<OnboardApp />, { container: document.getElementById('onb')! })
const button = (text: string): HTMLButtonElement =>
  [...document.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent === text)!
const row = (name: string): HTMLButtonElement => {
  const label = [...document.querySelectorAll<HTMLElement>('.ob-row .nm')].find(item => item.textContent === name)!
  return label.closest('.ob-row')!.querySelector<HTMLButtonElement>('.provider-choice-action') || label.closest('button')!
}

const open = async (): Promise<void> => {
  await act(async () => {
    void store.open()
  })
}

const start = async (): Promise<void> => {
  await act(async () => {
    fireEvent.click(button('gui.onb.start'))
  })
}

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.useRealTimers()
  delete window.RavenShell
  delete window.DS
  document.body.innerHTML = ''
})

describe('the onboarding island', () => {
  it('renders nothing until boot asks it to open', () => {
    install()
    mount()
    expect(document.querySelector('.ob')).toBeNull()
    expect((document.getElementById('onb') as HTMLElement).hidden).toBe(true)
  })

  it('opens through DS and lists the providers the source returns', async () => {
    const h = install()
    mount()
    await open()
    expect((document.getElementById('onb') as HTMLElement).hidden).toBe(false)
    expect(document.querySelector('.ob-step')?.getAttribute('data-center')).toBe('1')
    await start()
    expect(h.options).toHaveBeenCalledTimes(1)
    expect([...document.querySelectorAll('.ob-row .nm')].map(item => item.textContent)).toEqual(['Anthropic', 'OpenAI'])
    expect(document.querySelector('.ob-row .bd.on')?.textContent).toBe('gui.onb.connected')
    const rows = [...document.querySelectorAll<HTMLElement>('.ob-row')]
    expect(rows[0]!.querySelector('.nm')?.firstElementChild?.getAttribute('src')).toBe('assets/providers/anthropic.svg')
    expect(rows[0]!.querySelector('.provider-link')?.getAttribute('href')).toBe('https://anthropic.com/')
    expect(rows[0]!.lastElementChild?.className).toBe('provider-status on')
    expect(rows[1]!.lastElementChild?.className).toBe('provider-status')
  })

  it('takes a key before offering models and hands the exact fields to the source', async () => {
    const h = install()
    mount()
    await open()
    await start()
    fireEvent.click(row('OpenAI'))
    const next = button('gui.onb.next')
    expect(next.disabled).toBe(true)
    fireEvent.input(document.querySelector<HTMLInputElement>('.ob-in')!, { target: { value: 'secret' } })
    expect(next.disabled).toBe(false)
    await act(async () => {
      fireEvent.click(next)
    })
    expect(h.saves).toEqual([['openai', 'secret', '']])
    expect(document.querySelector('.ob-row .nm')?.textContent).toBe('gpt-5.2')
  })

  it('requires only a base URL for a local provider', async () => {
    const local: OnboardProvider = {
      slug: 'ollama',
      name: 'Ollama',
      auth_type: 'local',
      authenticated: false,
      needs_api_base: true,
      models: ['qwen3:32b']
    }
    const h = install([local])
    mount()
    await open()
    await start()
    fireEvent.click(row('Ollama'))
    const fields = document.querySelectorAll<HTMLInputElement>('.ob-form .ob-in')
    expect(fields).toHaveLength(1)
    expect(fields[0]!.type).toBe('text')
    fireEvent.input(fields[0]!, { target: { value: 'http://127.0.0.1:11434' } })
    await act(async () => {
      fireEvent.click(button('gui.onb.next'))
    })
    expect(h.saves).toEqual([['ollama', '', 'http://127.0.0.1:11434']])
  })

  it('prefills the LM Studio endpoint and accepts it without an API key', async () => {
    const local: OnboardProvider = {
      slug: 'lm_studio',
      name: 'LM Studio',
      auth_type: 'local',
      authenticated: false,
      needs_api_base: true,
      default_api_base: 'http://localhost:1234/v1',
      models: ['lm-studio/qwen3-8b']
    }
    const h = install([local])
    mount()
    await open()
    await start()
    fireEvent.click(row('LM Studio'))
    const base = document.querySelector<HTMLInputElement>('.ob-form .ob-in')!
    expect(base.value).toBe('http://localhost:1234/v1')
    expect(button('gui.onb.next').disabled).toBe(false)
    await act(async () => {
      fireEvent.click(button('gui.onb.next'))
    })
    expect(h.saves).toEqual([['lm_studio', '', 'http://localhost:1234/v1']])
  })

  it('shows the MiniMax CN endpoint and requires its API key', async () => {
    const cn: OnboardProvider = {
      slug: 'minimax_cn_api',
      name: 'MiniMax (CN)',
      auth_type: 'endpoint',
      authenticated: false,
      needs_api_base: false,
      default_api_base: 'https://api.minimaxi.com/v1/',
      models: ['minimax-cn-api/MiniMax-M3']
    }
    const h = install([cn])
    mount()
    await open()
    await start()
    fireEvent.click(row('MiniMax (CN)'))
    const fields = document.querySelectorAll<HTMLInputElement>('.ob-form .ob-in')
    expect(fields).toHaveLength(2)
    expect(fields[1]!.value).toBe('https://api.minimaxi.com/v1/')
    expect(button('gui.onb.next').disabled).toBe(true)
    fireEvent.input(fields[0]!, { target: { value: 'K-CN' } })
    expect(button('gui.onb.next').disabled).toBe(false)
    await act(async () => {
      fireEvent.click(button('gui.onb.next'))
    })
    expect(h.saves).toEqual([['minimax_cn_api', 'K-CN', 'https://api.minimaxi.com/v1/']])
  })

  it('re-probes OAuth without leaving the credentials step until it is connected', async () => {
    const oauth: OnboardProvider = {
      slug: 'minimax_global',
      name: 'MiniMax Global',
      auth_type: 'oauth',
      authenticated: false,
      models: ['MiniMax-M2.5']
    }
    let checks = 0
    install([oauth], {
      options: async () => ({ providers: [{ ...oauth, authenticated: checks++ >= 2 }] })
    })
    mount()
    await open()
    await start()
    fireEvent.click(row('MiniMax Global'))
    await act(async () => {
      fireEvent.click(button('gui.onb.oauth_check'))
    })
    expect(document.querySelector('.ob-err')?.textContent).toBe('gui.onb.oauth_wait')
    await act(async () => {
      fireEvent.click(button('gui.onb.oauth_check'))
    })
    expect(document.querySelector('.ob-row .nm')?.textContent).toBe('MiniMax-M2.5')
  })

  it('filters a long model list and preserves the selected marker', async () => {
    const many = {
      ...connected,
      models: ['m0', 'm1', 'm2', 'm3', 'm4', 'm5', 'm6', 'm7', 'm8']
    }
    install([many])
    mount()
    await open()
    await start()
    fireEvent.click(row('Anthropic'))
    const filter = document.querySelector<HTMLInputElement>('.ob-in')!
    fireEvent.input(filter, { target: { value: 'm8' } })
    expect([...document.querySelectorAll('.ob-row .nm')].map(item => item.textContent)).toEqual(['m8'])
    fireEvent.click(row('m8'))
    expect(document.querySelector('.ob-row')?.getAttribute('data-sel')).toBe('1')
  })

  it('persists the chosen model, rechecks setup, fades, and resolves the opening', async () => {
    vi.useFakeTimers()
    const h = install([connected])
    mount()
    let resolved = false
    await act(async () => {
      void store.open().then(() => {
        resolved = true
      })
    })
    await start()
    fireEvent.click(row('Anthropic'))
    fireEvent.click(row('claude-opus-5'))
    await act(async () => {
      fireEvent.click(button('gui.onb.finish'))
    })
    expect(h.models).toEqual([['claude-opus-5', 'anthropic']])
    expect(h.rechecks).toBe(1)
    expect(document.querySelector('.ob-t')?.textContent).toBe('gui.onb.done_t')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500)
    })
    expect(document.getElementById('onb')?.dataset.off).toBe('1')
    expect(resolved).toBe(false)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500)
    })
    expect(resolved).toBe(true)
    expect(document.querySelector('.ob')).toBeNull()
    expect((document.getElementById('onb') as HTMLElement).hidden).toBe(true)
  })

  it('keeps source failures in the current step', async () => {
    install([], { options: async () => Promise.reject({ data: { detail: 'offline' } }) })
    mount()
    await open()
    await start()
    expect(document.querySelector('.ob-t')?.textContent).toBe('gui.onb.welcome_t')
    expect(document.querySelector('.ob-err')?.textContent).toBe('gui.onb.err_generic:offline')
    expect(button('gui.onb.start').disabled).toBe(false)
  })
})
