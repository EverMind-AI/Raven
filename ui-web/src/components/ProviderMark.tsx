/* Provider branding and connection state shared by every model-provider list. */

import { useState } from 'react'

import { open as openUrl } from '../lib/openUrl'

import type { JSX } from 'react'

const ICONS: Record<string, string> = {
  ai302: 'ai302',
  aihubmix: 'aihubmix',
  radeon_cloud: 'amd',
  aionly: 'aionly',
  alayanew: 'alayanew',
  anthropic: 'anthropic',
  azure_openai: 'azureai',
  baichuan: 'baichuan',
  /* Zhipu's own mark, not Z.ai's: same vendor, two platforms and two logos. */
  bigmodel: 'zhipu',
  /* Not a provider: the rail's Zhipu family addresses its heading mark by
     this name, the company's rather than either platform's. */
  gpustack: 'gpustack',
  lanyun: 'lanyun',
  ocoolai: 'ocoolai',
  ovms: 'ovms',
  ph8: 'ph8',
  xirang: 'xirang',
  zhipu: 'zhipu',
  baidu_cloud: 'baiducloud',
  burncloud: 'burncloud',
  cerebras: 'cerebras',
  dashscope: 'alibabacloud',
  deepseek: 'deepseek',
  dmxapi: 'dmxapi',
  doc2x: 'doc2x',
  fireworks_ai: 'fireworks',
  gemini: 'gemini',
  github_copilot: 'githubcopilot',
  groq: 'groq',
  hosted_vllm: 'vllm',
  huggingface: 'huggingface',
  lm_studio: 'lmstudio',
  longcat: 'longcat',
  minimax: 'minimax',
  minimax_cn_api: 'minimax',
  minimax_cn: 'minimax',
  minimax_global: 'minimax',
  mineru: 'mineru',
  mistral: 'mistral',
  modelscope: 'modelscope',
  moonshot: 'moonshot',
  nvidia_nim: 'nvidia',
  ollama: 'ollama',
  ollama_chat: 'ollama',
  openai: 'openai',
  openai_codex: 'codex',
  openrouter: 'openrouter',
  paddleocr: 'paddleocr',
  perplexity: 'perplexity',
  poe: 'poe',
  ppio: 'ppio',
  qiniu: 'qiniu',
  siliconflow: 'siliconcloud',
  sophnet: 'sophnet',
  stepfun: 'stepfun',
  together_ai: 'together',
  /* Tencent Cloud's product, and its mark is the one it ships under. */
  tokenhub: 'tencentcloud',
  volcengine: 'volcengine',
  xai: 'xai',
  xiaomi_mimo: 'xiaomimimo',
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
  /* Also the asset names `VENDOR_BY_NAME` answers with, because that table and
     this one are read through the same lookup: a family recognised by its
     model name arrives here as the asset it should wear, and every other value
     that table produces happens to be spelled the same as a namespace. Without
     these two, `gemini-3.1-pro` on any shelf but Gemini's own -- and any
     `doubao`/`seed` off VolcEngine's -- resolved to nothing and wore the
     gateway's mark instead of the model maker's. */
  'gemini': 'gemini',
  'volcengine': 'volcengine',
  /* The namespaces the added shelves publish under. Hugging Face, Together,
     Fireworks and Poe all file a model under whoever made it, so these are
     what the id's middle segment actually says. */
  'baidu': 'wenxin',
  'cerebras': 'cerebras',
  'longcat': 'longcat',
  'meta': 'meta',
  'meta-llama': 'meta',
  'mistral': 'mistral',
  'mistralai': 'mistral',
  'perplexity': 'perplexity',
  'stepfun': 'stepfun',
  'stepfun-ai': 'stepfun',
  'grok': 'grok',
  'xai': 'xai',
  'xiaomi': 'xiaomimimo',
  'xiaomimimo': 'xiaomimimo',
  'wenxin': 'wenxin',
  /* Zhipu's own spelling of its namespace, which the snapshot uses and no
     entry above covered -- `zai-org` and `z-ai` are the other two. */
  'zhipuai': 'zai',
  'zhipu': 'zhipu',
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

/* Marks that ship a second drawing for the dark theme, and marks that instead
   lean on a filter.
 *
 * Both are facts about the files, derived from their shapes by
 * tests/test_ui_provider_marks.py rather than kept by hand. A vendor logo is
 * mostly its own colours and wants neither -- it is legible on both grounds and
 * a filter would only take the brand somewhere it does not go. What needs
 * answering is ink: a mark drawn in black disappears on a dark ground. The
 * vendor's own dark drawing is the right answer where there is one, and an
 * invert is the fallback where there is not. */
const DARK_PAIRED = new Set([
  'cerebras',
  'codex',
  'githubcopilot',
  'grok',
  'kimi',
  'longcat',
  'minimax',
  'moonshot',
  'ocoolai',
  'ollama',
  'openai',
  'openrouter',
  'poe',
  'xai',
  'xiaomimimo',
  'zai',
  'zhipu',
])

const TONES: Record<string, 'mono' | 'hybrid' | 'mono-white'> = {
  amd: 'mono-white',
  anthropic: 'hybrid',
  mineru: 'hybrid',
  vllm: 'mono',
}

const darkUrl = (icon: string): string | null =>
  DARK_PAIRED.has(icon) ? `assets/providers/${icon}-dark.svg${stamp()}` : null

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
  /* Grok is xAI's product and has its own mark, the same split Kimi and
     Moonshot have above: the company's mark is what the provider row wears. */
  [/^grok/i, 'grok'],
  /* Mistral ships six families under names that only rhyme -- Magistral,
     Devstral, Pixtral, Voxtral, Ministral, Mixtral -- and none of them
     contains "mistral", so a prefix on the company name alone would leave
     most of its own shelf unmarked. */
  [/^(?:mistral|magistral|devstral|pixtral|codestral|voxtral|ministral|mixtral|open-mi[sx]tral)/i, 'mistral'],
  [/^sonar/i, 'perplexity'],
  [/^step-/i, 'stepfun'],
  [/^ernie/i, 'wenxin'],
  [/^longcat/i, 'longcat'],
  [/^mimo/i, 'xiaomimimo'],
  /* Meta's mark, not LlamaIndex's: the id names the weights. Last of the
     family rules, so a reseller's own prefix on a Llama still wins. */
  [/^llama/i, 'meta'],
]

export function vendorFromName(model: string): string {
  const bare = String(model || '').split('/').pop() || ''
  for (const [pattern, vendor] of VENDOR_BY_NAME) {
    if (pattern.test(bare)) return vendor
  }
  return ''
}

const vendorIcon = (vendor: string): string | undefined => VENDOR_ICONS[vendor.toLowerCase()]

export function vendorIconPath(vendor: string): string | null {
  const icon = vendorIcon(vendor)
  return icon ? assetUrl(icon) : null
}

export function providerIconPath(id: string): string | null {
  const icon = ICONS[id]
  return icon ? assetUrl(icon) : null
}

/* Raven's own mark, for a row that is this installation rather than a vendor.
   Not under `providers/` -- it is not one -- but it wants the same digest, or
   a replaced drawing stays cached under the URL its predecessor held. */
export function ravenIconPath(): string {
  return `assets/raven.svg${stamp()}`
}

/* An icon that degrades instead of breaking.
 *
 * The map says which asset a name should wear; whether that file is in the
 * bundle is a separate question, and one this cannot answer before the request.
 * So a load failure falls through to the same initial an unmapped name gets --
 * which is what makes "drop the svg in" the whole of adding a vendor. */
function Mark({ src, icon, tag, letter }: {
  src: string | null
  icon?: string | null
  tag: string
  letter: string
}): JSX.Element {
  const [broken, setBroken] = useState(false)
  if (src && !broken) {
    const dark = icon ? darkUrl(icon) : null
    const tone = icon ? TONES[icon] : undefined
    /* Both drawings are in the markup and the stylesheet shows one, because
       which theme is on is a CSS question here: nothing publishes it to React,
       and `system` follows the OS without anyone being told. The light one
       keeps the error handler -- a pair that 404s should fall back to the
       initial exactly as a lone mark does. */
    return (
      <>
        <img
          className={'provider-icon' + (dark ? ' mark-light' : '')}
          src={src}
          alt=""
          aria-hidden="true"
          draggable="false"
          data-provider={tag}
          {...(tone ? { 'data-tone': tone } : {})}
          onError={() => setBroken(true)}
        />
        {dark && (
          <img
            className="provider-icon mark-dark"
            src={dark}
            alt=""
            aria-hidden="true"
            draggable="false"
            data-provider={tag}
          />
        )}
      </>
    )
  }
  return (
    <span className="provider-icon fallback" aria-hidden="true">
      {letter.trim().charAt(0).toUpperCase() || '?'}
    </span>
  )
}

export function ProviderIcon({ id, name }: { id: string; name: string }): JSX.Element {
  return <Mark src={providerIconPath(id)} icon={ICONS[id]} tag={id} letter={name} />
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
  /* Whichever mark won decides which pair and tone apply -- the model's own
     where it has one, the provider's where it does not. */
  return (
    <Mark
      src={own || providerIconPath(provider)}
      icon={own ? vendorIcon(key) : ICONS[provider]}
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
