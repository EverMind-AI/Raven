/* Provider branding and connection state shared by every model-provider list. */

import { open as openUrl } from './open-url'

import type { JSX } from 'react'

const ICONS: Record<string, string> = {
  aihubmix: 'aihubmix',
  anthropic: 'anthropic',
  azure_openai: 'azureai',
  dashscope: 'alibabacloud',
  deepseek: 'deepseek',
  gemini: 'gemini',
  github_copilot: 'githubcopilot',
  groq: 'groq',
  hosted_vllm: 'vllm',
  lm_studio: 'lmstudio',
  minimax: 'minimax',
  minimax_cn_api: 'minimax',
  minimax_cn: 'minimax',
  minimax_global: 'minimax',
  moonshot: 'moonshot',
  nvidia_nim: 'nvidia',
  ollama: 'ollama',
  ollama_chat: 'ollama',
  openai: 'openai',
  openai_codex: 'codex',
  openrouter: 'openrouter',
  siliconflow: 'siliconcloud',
  volcengine: 'volcengine',
  zai: 'zai',
}

export function providerIconPath(id: string): string | null {
  const icon = ICONS[id]
  return icon ? `assets/providers/${icon}.svg` : null
}

export function ProviderIcon({ id, name }: { id: string; name: string }): JSX.Element {
  const src = providerIconPath(id)
  if (src) return <img className="provider-icon" src={src} alt="" aria-hidden="true" draggable="false" data-provider={id} />
  return <span className="provider-icon fallback" aria-hidden="true">{name.trim().charAt(0).toUpperCase() || '?'}</span>
}

export function ProviderLink({ homepage, name }: { homepage?: string; name: string }): JSX.Element {
  if (!homepage) return <span>{name}</span>
  return (
    <a
      className="provider-link"
      href={homepage}
      target="_blank"
      rel="noreferrer"
      aria-label={`${name} homepage`}
      onClick={(event) => {
        event.preventDefault()
        event.stopPropagation()
        openUrl(homepage)
      }}
    >
      <span>{name}</span>
    </a>
  )
}

export function ProviderStatus({ connected, label }: { connected: boolean; label: string }): JSX.Element {
  return (
    <span
      className={'provider-status' + (connected ? ' on' : '')}
      role="img"
      aria-label={label}
      title={label}
    />
  )
}
