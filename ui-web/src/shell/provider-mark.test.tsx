// @vitest-environment happy-dom
/* Provider marks must cover the backend catalog and expose status without relying on color. */

import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ProviderIcon, ProviderLink, ProviderStatus, providerIconPath } from './provider-mark'

const BRANDED = [
  'azure_openai', 'openrouter', 'aihubmix', 'siliconflow', 'volcengine',
  'anthropic', 'openai', 'openai_codex', 'github_copilot', 'deepseek',
  'gemini', 'zai', 'dashscope', 'moonshot', 'minimax', 'minimax_cn_api', 'minimax_global',
  'minimax_cn', 'nvidia_nim', 'hosted_vllm', 'lm_studio', 'ollama_chat', 'groq',
]

afterEach(cleanup)

describe('provider marks', () => {
  it('maps every branded provider in the backend catalog to a bundled asset', () => {
    for (const id of BRANDED) expect(providerIconPath(id), id).toMatch(/^assets\/providers\/.+\.svg$/)
  })

  it('uses an initial instead of a broken image for custom providers', () => {
    const view = render(<ProviderIcon id="custom" name="Custom gateway" />)

    expect(view.container.querySelector('img')).toBeNull()
    expect(view.container.querySelector('.provider-icon.fallback')?.textContent).toBe('C')
  })

  it('labels connected and disconnected dots for non-visual users', () => {
    const view = render(
      <><ProviderStatus connected label="Connected" /><ProviderStatus connected={false} label="Not connected" /></>,
    )
    const dots = view.container.querySelectorAll('.provider-status')

    expect(dots[0]?.className).toBe('provider-status on')
    expect(dots[0]?.getAttribute('aria-label')).toBe('Connected')
    expect(dots[1]?.className).toBe('provider-status')
    expect(dots[1]?.getAttribute('aria-label')).toBe('Not connected')
  })

  it('links a provider name without triggering its surrounding action', () => {
    let selected = false
    const opener = vi.spyOn(window, 'open').mockImplementation(() => null)
    const view = render(
      <div onClick={() => { selected = true }}>
        <ProviderLink name="Gemini" homepage="https://gemini.google.com/" />
      </div>,
    )
    const link = view.getByRole('link', { name: 'Gemini homepage' })

    expect(link.getAttribute('href')).toBe('https://gemini.google.com/')
    expect(link.getAttribute('target')).toBe('_blank')
    fireEvent.click(link)
    expect(selected).toBe(false)
    expect(opener).toHaveBeenCalledWith('https://gemini.google.com/', '_blank', 'noopener')
    opener.mockRestore()
  })

  it('leaves custom provider names as plain text when there is no homepage', () => {
    const view = render(<ProviderLink name="Custom" />)

    expect(view.queryByRole('link')).toBeNull()
    expect(view.getByText('Custom')).toBeTruthy()
  })
})
