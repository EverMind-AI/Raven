/* Provider branding and connection state shared by every model-provider list. */

import { useState } from 'react'

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

/* The vendor a model comes from, mapped onto a logo already in the bundle.
 *
 * A model list under one provider is a list of other people's models: a
 * gateway serves Qwen, DeepSeek, BAAI and a dozen more, and stamping the
 * gateway's own mark on all of them says nothing about any row. Keyed by the
 * vendor segment of the model id, which is what the list is grouped by.
 *
 * Only vendors whose logo is already shipped for a provider of the same name.
 * A vendor with no logo of its own falls back to the provider's, then to its
 * initial -- the same ladder `ProviderIcon` has always used, one level deeper.
 * Adding a vendor here means adding an asset, not just a line. */
const VENDOR_ICONS: Record<string, string> = {
  'alibaba': 'alibabacloud',
  'anthropic': 'anthropic',
  'bytedance': 'volcengine',
  'bytedance-seed': 'volcengine',
  'deepseek': 'deepseek',
  'deepseek-ai': 'deepseek',
  'google': 'gemini',
  'kimi': 'kimi',
  'minimax': 'minimax',
  'minimaxai': 'minimax',
  'moonshot': 'moonshot',
  'moonshotai': 'moonshot',
  'nvidia': 'nvidia',
  'openai': 'openai',
  'openrouter': 'openrouter',
  /* Qwen is Alibaba's and has a mark of its own; wearing Alibaba Cloud's read
     as the wrong brand. Adding the next vendor is this line plus the svg -- a
     row falls back to its initial while an asset is absent rather than showing
     a broken image, see `Mark` below. */
  'qwen': 'qwen',
  'z-ai': 'zai',
  'zai': 'zai',
  'zai-org': 'zai',
}

/* The asset tree's digest, appended to every asset URL.
 *
 * These files live at one unversioned path each, so a replaced drawing lands at
 * exactly the URL its predecessor is cached under -- and a client that decided
 * the old copy was fresh keeps showing it through a rebuild, a server restart
 * and a hard reload. A digest in the query makes a changed file a different
 * URL, which no cache can answer from what it already holds.
 *
 * Absent outside the built page (tests, the vite dev server), where the plain
 * path is what the assertions and the loader both expect. */
const stamp = (): string => {
  const v = (window as unknown as { __ASSETV?: string }).__ASSETV
  return v && v !== '__ASSETV__' ? `?v=${v}` : ''
}

const assetUrl = (icon: string): string => `assets/providers/${icon}.svg${stamp()}`

/* Which vendor made a model, read off its name.
 *
 * A gateway that files ids under a namespace says so in the id itself --
 * `BAAI/bge-m3` -- and that segment is the answer. Plenty do not: Alibaba Cloud
 * serves `qwen-plus`, `deepseek-v4-flash` and `glm-5.2` as flat names, and
 * without this every row on that shelf wore the same provider mark.
 *
 * Anchored on the leading token, so a family name in the middle of an id
 * cannot claim it. Only families whose logo is in the bundle: one that is not
 * there gains nothing from being recognised, and the row falls through to the
 * provider's mark exactly as before.
 */
const VENDOR_BY_NAME: ReadonlyArray<readonly [RegExp, string]> = [
  [/^(?:qwen|qwq|qvq|tongyi)/i, 'qwen'],
  [/^deepseek/i, 'deepseek'],
  [/^claude/i, 'anthropic'],
  [/^(?:gpt|chatgpt|o[134]\b|codex|dall-e|text-embedding-(?:3|ada))/i, 'openai'],
  [/^(?:gemini|gemma|palm|imagen)/i, 'gemini'],
  /* Kimi is Moonshot's product and has its own mark; the company's is what a
     row without a Kimi in it wears. */
  [/^kimi/i, 'kimi'],
  [/^moonshot/i, 'moonshot'],
  [/^(?:minimax|abab)/i, 'minimax'],
  [/^glm/i, 'zai'],
  [/^nemotron/i, 'nvidia'],
  [/^(?:doubao|seed|skylark)/i, 'volcengine'],
]

export function vendorFromName(model: string): string {
  const bare = String(model || '').split('/').pop() || ''
  for (const [pattern, vendor] of VENDOR_BY_NAME) {
    if (pattern.test(bare)) return vendor
  }
  return ''
}

export function vendorIconPath(vendor: string): string | null {
  const icon = VENDOR_ICONS[vendor.toLowerCase()]
  return icon ? assetUrl(icon) : null
}

export function providerIconPath(id: string): string | null {
  const icon = ICONS[id]
  return icon ? assetUrl(icon) : null
}

/* An icon that degrades instead of breaking.
 *
 * The map says which asset a name should wear; whether that file is in the
 * bundle is a separate question, and one this cannot answer before the request.
 * So a load failure falls through to the same initial an unmapped name gets --
 * which is what makes "drop the svg in" the whole of adding a vendor. */
function Mark({ src, tag, letter }: { src: string | null; tag: string; letter: string }): JSX.Element {
  const [broken, setBroken] = useState(false)
  if (src && !broken) {
    return (
      <img
        className="provider-icon"
        src={src}
        alt=""
        aria-hidden="true"
        draggable="false"
        data-provider={tag}
        onError={() => setBroken(true)}
      />
    )
  }
  return (
    <span className="provider-icon fallback" aria-hidden="true">
      {letter.trim().charAt(0).toUpperCase() || '?'}
    </span>
  )
}

export function ProviderIcon({ id, name }: { id: string; name: string }): JSX.Element {
  return <Mark src={providerIconPath(id)} tag={id} letter={name} />
}

/* One row of a model list: the model's own vendor where that is known, the
   provider serving it where it is not. */
export function ModelIcon({
  vendor,
  model,
  provider,
  name,
}: {
  vendor: string
  model?: string
  provider: string
  name: string
}): JSX.Element {
  /* The model's own name first, then the namespace it was published under.
     The name identifies the product and the namespace identifies whoever is
     serving it, and the product is what a reader recognises -- `kimi-k2.6` is
     a Kimi wherever it is listed, including under `moonshotai/`. Where both
     answer they almost always agree, and where only one does it wins. */
  const key = (model ? vendorFromName(model) : '') || vendor
  const own = key ? vendorIconPath(key) : null
  /* The vendor's own mark, else the provider serving it. The initial, when it
     comes to that, is the vendor's: a column of identical letters is the same
     problem as a column of identical logos. */
  return (
    <Mark
      src={own || providerIconPath(provider)}
      tag={own ? key : provider}
      letter={vendor || name}
    />
  )
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

/* A dot for a provider that is connected, and nothing for one that is not.
 *
 * Absence is the clearer signal here: a list is mostly unconfigured providers,
 * and a dim dot on every one of them reads as a row of state to interpret
 * rather than as the handful that are ready. The `label` still travels for the
 * dot that is drawn, so the one piece of state on the row is not colour-only. */
export function ProviderStatus({ connected, label }: { connected: boolean; label: string }): JSX.Element | null {
  if (!connected) return null
  return <span className="provider-status on" role="img" aria-label={label} title={label} />
}
