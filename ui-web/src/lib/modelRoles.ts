/* Pure model-role metadata and config-reading helpers shared by the settings
 * model roles card and the Skills & Tools page's media tool configuration.
 *
 * Kept under src/lib/ with type-only imports from features/ so neither domain
 * imports the other's internals (scripts/gates/import-direction.test.mjs).
 */
import type { Kind } from '../features/model/types'
import type { ProviderRow, SettingsSnapshot } from '../features/settings/types'

export type RoleId =
  | 'chat' | 'title' | 'memllm'
  | 'embedding' | 'rerank' | 'multimodal' | 'image' | 'speech' | 'video'

export const ROLE_KIND: Record<RoleId, Kind> = {
  chat: 'text', title: 'text', memllm: 'text', multimodal: 'text',
  embedding: 'embedding', rerank: 'reranker', image: 'image', speech: 'audio', video: 'video',
}

export interface Role {
  id: RoleId
  keys?: [string, string]
  everos?: string
  media?: string
  tool?: string
  optional?: boolean
}

export const ROLES: Role[] = [
  { id: 'chat' },
  { id: 'title', keys: ['sessionTitle.model', 'sessionTitle.provider'] },
  { id: 'memllm', everos: 'llm' },
  { id: 'embedding', everos: 'embedding', optional: true },
  { id: 'rerank', everos: 'rerank', optional: true },
  { id: 'multimodal', everos: 'multimodal', optional: true },
  { id: 'image', media: 'image', tool: 'image_generate', optional: true },
  { id: 'speech', media: 'speech', tool: 'text_to_speech', optional: true },
  { id: 'video', media: 'video', tool: 'video_generate', optional: true },
]

export const MEDIA_PROVIDER = 'openrouter'

export const TOOL_ROLE_OF: Record<string, RoleId> = {
  image_generate: 'image',
  text_to_speech: 'speech',
  video_generate: 'video',
  understand_media: 'multimodal',
}

const toSnake = (name: string): string => name.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`)

/* Reads a dotted path, resolving camelCase and snake_case at the top level. */
export const digConfig = (raw: Record<string, unknown>, path: string): unknown => {
  const [head, ...rest] = path.split('.')
  let node: unknown = raw[head!] ?? raw[toSnake(head!)]
  for (const key of rest) node = node && typeof node === 'object' ? (node as Record<string, unknown>)[key] : undefined
  return node
}

export const disabledTools = (raw: Record<string, unknown>): string[] =>
  (digConfig(raw, 'tools.disabledTools') as string[] | undefined) || []

export interface RoleValue {
  model: string
  provider: string
}

export function roleValue(r: Role, snap: SettingsSnapshot): RoleValue | null {
  if (r.id === 'chat') return snap.model ? { model: snap.model, provider: snap.curProvider } : null
  if (r.keys) {
    const model = digConfig(snap.raw, r.keys[0])
    if (typeof model !== 'string' || !model) return null
    const provider = digConfig(snap.raw, r.keys[1])
    return { model, provider: typeof provider === 'string' ? provider : '' }
  }
  if (r.everos) {
    const sec = snap.everos?.sections?.[r.everos]
    if (!sec || !sec.model) return null
    return { model: sec.model, provider: sec.provider || '' }
  }
  if (r.media) {
    const sel = digConfig(snap.raw, `tools.media.${r.media}`) as { model?: string; provider?: string } | undefined
    return sel && sel.model ? { model: sel.model, provider: sel.provider || MEDIA_PROVIDER } : null
  }
  return null
}

function isSelfHost(snap: SettingsSnapshot, id: string): boolean {
  const p = snap.providers.find((x) => x.id === id)
  return !!p && (p.kind === 'local' || p.id === 'custom')
}

export function roleProviders(r: Role, snap: SettingsSnapshot): ProviderRow[] {
  const on = snap.providers.filter((p) => p.on)
  if (r.media === 'image') return on.filter((p) => !!p.imageApi)
  if (r.media === 'video') return on.filter((p) => !!p.videoApi)
  if (r.media) return on.filter((p) => p.id === MEDIA_PROVIDER)
  if (r.everos) {
    if (snap.everos?.available === false) return []
    const role = r.everos
    if (role !== 'rerank') return on
    const offered = on.filter((p) => (snap.everos?.supports?.[p.id] || []).includes(role))
    const pinned = snap.everos?.sections?.rerank?.provider
    return offered.filter((p) => !isSelfHost(snap, p.id) || p.id === pinned)
  }
  return on
}

export function everosLocked(r: Role, snap: SettingsSnapshot): 'foreign' | 'env' | null {
  if (!r.everos) return null
  if (snap.everos?.owned === false) return 'foreign'
  return snap.everos?.sections?.[r.everos]?.env_managed ? 'env' : null
}

export function followsChat(r: Role, snap: SettingsSnapshot): boolean {
  if (r.keys) return true
  return !!r.everos && !!snap.everos?.sections?.[r.everos]?.follows_main
}

export function rolesUsing(snap: SettingsSnapshot, slug: string, model?: string): Role[] {
  const chat = roleValue(ROLES[0]!, snap)
  return ROLES.filter((r) => {
    const own = roleValue(r, snap)
    const v = own || (followsChat(r, snap) ? chat : null)
    return !!v && v.provider === slug && (model === undefined || v.model === model)
  })
}

export const providerName = (snap: SettingsSnapshot, id: string): string => {
  const p = snap.providers.find((x) => x.id === id)
  return p ? p.name : id
}

const setIn = (raw: Record<string, unknown>, path: string, value: unknown): Record<string, unknown> => {
  const [head, ...rest] = path.split('.')
  const cur = raw[head!]
  return {
    ...raw,
    [head!]: rest.length ? setIn(cur && typeof cur === 'object' ? cur as Record<string, unknown> : {}, rest.join('.'), value) : value,
  }
}

export function mediaSelection(
  raw: Record<string, unknown>,
  kind: string,
  model: string,
  provider?: string,
): { model: string; quality: string; provider?: string } {
  const cur = (digConfig(raw, `tools.media.${kind}`) as { quality?: unknown } | undefined) || {}
  const sel = { model, quality: typeof cur.quality === 'string' ? cur.quality : '' }
  const routed = kind === 'image' || kind === 'video'
  return routed && provider !== undefined ? { ...sel, provider: provider === MEDIA_PROVIDER ? '' : provider } : sel
}

export function withRole(r: Role, snap: SettingsSnapshot, model: string, provider: string): SettingsSnapshot {
  if (r.id === 'chat') return { ...snap, model, curProvider: provider }
  if (r.keys) return { ...snap, raw: setIn(setIn(snap.raw, r.keys[0], model), r.keys[1], provider) }
  if (r.everos && snap.everos) {
    const sections = snap.everos.sections || {}
    return { ...snap, everos: { ...snap.everos, sections: { ...sections, [r.everos]: { ...sections[r.everos], model, provider } } } }
  }
  if (r.media) return { ...snap, raw: setIn(snap.raw, `tools.media.${r.media}`, mediaSelection(snap.raw, r.media, model, provider)) }
  return snap
}
