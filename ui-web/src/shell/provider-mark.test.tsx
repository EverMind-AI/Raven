// @vitest-environment happy-dom
/* Provider marks must cover the backend catalog and expose status without relying on color. */

import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ModelIcon, ProviderIcon, ProviderLink, ProviderStatus, providerIconPath } from './provider-mark'

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

  it('draws and labels a dot only for a provider that is connected', () => {
    const view = render(
      <><ProviderStatus connected label="Connected" /><ProviderStatus connected={false} label="Not connected" /></>,
    )
    const dots = view.container.querySelectorAll('.provider-status')

    /* One dot, not two dimmed differently: a list is mostly unconfigured
       providers, and a mark on every one of them reads as a row of state to
       interpret rather than as the few that are ready. */
    expect(dots.length).toBe(1)
    expect(dots[0]?.className).toBe('provider-status on')
    expect(dots[0]?.getAttribute('aria-label')).toBe('Connected')
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
describe('the model icon', () => {
  it('shows the vendor initial while its asset is missing from the bundle', () => {
    /* The map says which asset a name should wear; whether the file is there is
       a separate question, and one nothing can answer before the request. A
       failed load has to read as "no logo for this vendor", not as a broken
       image -- which is what makes dropping the svg in the whole of adding
       one. */
    const view = render(<ModelIcon vendor="qwen" provider="siliconflow" name="SiliconFlow" />)
    const img = view.container.querySelector('img')!

    expect(img.getAttribute('src')).toBe('assets/providers/qwen.svg')
    fireEvent.error(img)

    expect(view.container.querySelector('img')).toBeNull()
    expect(view.container.querySelector('.provider-icon.fallback')?.textContent).toBe('Q')
  })

  it('marks a row with its own vendor rather than the provider serving it', () => {
    /* A gateway's model list is a list of other people's models. Stamping the
       gateway's mark on all of them says nothing about any row. */
    const view = render(<ModelIcon vendor="deepseek-ai" provider="siliconflow" name="SiliconFlow" />)

    expect(view.container.querySelector('img')?.getAttribute('src')).toBe('assets/providers/deepseek.svg')
    expect(view.container.querySelector('img')?.getAttribute('data-provider')).toBe('deepseek-ai')
  })

  it('reads the vendor off a flat model name when the id has no namespace', () => {
    /* Alibaba Cloud serves `qwen-plus`, `deepseek-v4-flash` and `glm-5.2` as
       flat names. Without this every row on that shelf wore the same provider
       mark, which says nothing about any of them. */
    const of = (model: string): string | null | undefined =>
      render(<ModelIcon vendor="" model={model} provider="dashscope" name="Alibaba Cloud" />)
        .container.querySelector('img')
        ?.getAttribute('src')

    expect(of('dashscope/qwen-plus')).toBe('assets/providers/qwen.svg')
    expect(of('dashscope/qvq-max')).toBe('assets/providers/qwen.svg')
    expect(of('dashscope/deepseek-v4-flash')).toBe('assets/providers/deepseek.svg')
    expect(of('dashscope/glm-5.2')).toBe('assets/providers/zai.svg')
  })

  it('lets the product name win over the namespace, and anchors the match', () => {
    /* The name identifies the product and the namespace identifies whoever is
       serving it. `kimi-k2.6` is a Kimi wherever it is listed, including under
       `moonshotai/`, and Kimi's mark is the one a reader recognises. */
    const src = (vendor: string, model: string): string | null | undefined =>
      render(<ModelIcon vendor={vendor} model={model} provider="siliconflow" name="SiliconFlow" />)
        .container.querySelector('img')
        ?.getAttribute('src')

    expect(src('moonshotai', 'openrouter/moonshotai/kimi-k2.6')).toBe('assets/providers/kimi.svg')
    /* The company keeps its own where no product claims the row. */
    expect(src('moonshotai', 'moonshot/moonshot-v1-128k')).toBe('assets/providers/moonshot.svg')
    /* Where both answer they agree, which is the ordinary case. */
    expect(src('deepseek-ai', 'x/deepseek-ai/DeepSeek-V3')).toBe('assets/providers/deepseek.svg')
    /* Anchored: a family named in the middle of an id has not made that model,
       so the namespace is left to answer. */
    expect(src('', 'dashscope/my-qwen-clone')).toBe('assets/providers/siliconcloud.svg')
    /* And a name nothing recognises stays with the provider serving it. */
    expect(src('', 'dashscope/text-embedding-v4')).toBe('assets/providers/siliconcloud.svg')
  })

  it('falls back to the provider when the vendor has no logo of its own', () => {
    const view = render(<ModelIcon vendor="FunAudioLLM" provider="siliconflow" name="SiliconFlow" />)

    expect(view.container.querySelector('img')?.getAttribute('src')).toBe('assets/providers/siliconcloud.svg')
  })

  it('falls back to the vendor initial, not the provider one', () => {
    /* A row of identical letters is the same problem as a row of identical
       logos, so the letter comes from the vendor. */
    const view = render(<ModelIcon vendor="Kwai-Kolors" provider="custom" name="Custom gateway" />)

    expect(view.container.querySelector('img')).toBeNull()
    expect(view.container.querySelector('.provider-icon.fallback')?.textContent).toBe('K')
  })

  it('uses the provider when a model carries no vendor at all', () => {
    /* A provider that publishes flat ids -- deepseek/deepseek-v4-pro -- has no
       vendor segment to read. */
    const view = render(<ModelIcon vendor="" provider="deepseek" name="DeepSeek" />)

    expect(view.container.querySelector('img')?.getAttribute('src')).toBe('assets/providers/deepseek.svg')
  })
})
