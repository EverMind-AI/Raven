/* The plugins island's source: the market half, and the rows the one shared
   read already holds (../installed/source.ts).

   plughub.* / plug.* are this page's own calls; the installed plugins and MCP
   servers it lists come from that read, which three pages share. The renderer
   beside this is features/plugins/. */

import { code as LANG } from '../../i18n/t'
import { t } from '../../i18n/t'
import { gateway } from '../../rpc/gateway'
import { show as toast } from '../../state/toast'
import { extIsLoaded, extPlugins, loadExt } from '../installed/source'

import type { DetailEntry, PluginsSource } from './types'

const pmText = (v: unknown): string =>
  (v && typeof v === 'object' ? (v as Record<string, string>)[LANG] || (v as Record<string, string>).en || '' : String(v || ''))

export const pmErrText = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string }
  return (err && err.data && err.data.detail) || (err && err.message) || String(e)
}

/* The hub serves i18n objects for its text fields; which language wins is
   decided here, once, so the island only ever sees plain strings. */
export function pmNormEntry(entry: DetailEntry): DetailEntry {
  return {
    ...entry,
    name: pmText(entry.name),
    summary: pmText(entry.summary),
    description: pmText(entry.description),
    contributes: (entry.contributes || []).map((c) => {
      if (c.kind !== 'mcp' || !c.auth || !c.auth.fields) return c
      return { ...c, auth: { ...c.auth, fields: c.auth.fields.map((f) => ({ ...f, label: pmText(f.label) })) } }
    }),
  }
}

export const pluginsSource: PluginsSource = {
  // A search failure is rendered in the page (market_down + retry), not
  // toasted -- the island owns that surface.
  search: (q, category) => gateway().call('plughub.search', { q, category })
    .then((r) => ({ items: r.items || [], categories: r.categories || [] }))
    .catch((e) => { throw new Error(pmErrText(e)) }),
  detail: (id) => gateway().call('plughub.detail', { id })
    /* `plughub.detail` declares its entry as a free JSON object, so the shape
       the card reads is the island's own; the normaliser is what makes the two
       agree, and nothing but the three text fields is touched. */
    .then((r) => ({ entry: pmNormEntry(r.item as unknown as DetailEntry), installed: !!r.installed }))
    .catch((e) => {
      toast(t('gui.plug.op_failed', { err: pmErrText(e) }))
      throw { handled: true }
    }),
  // Failure is NOT toasted here: whether it lands in the progress sheet or
  // a toast depends on whether the sheet is showing, which only the island
  // knows. The error detail travels normalized.
  install: (id, form) => gateway().call('plug.install', { id, form: form || {} })
    .catch((e) => { throw new Error(pmErrText(e)) }),
  // Same shape: the user-facing uninstall toasts from the island, and the
  // silent auth rollback swallows the failure -- one call serves both.
  remove: (name) => gateway().call('plug.remove', { name })
    .then(() => loadExt())
    .catch((e) => { throw new Error(pmErrText(e)) }),
  toggle: (name, enabled) => gateway().call('plug.toggle', { name, enabled })
    .then((r) => r.mcp || null)
    .catch((e) => {
      toast(t('gui.plug.op_failed', { err: pmErrText(e) }))
      return loadExt().catch(() => {}).then(() => { throw { handled: true } })
    }),
  // The row is the loader's own object: assigning state runs its setter,
  // which persists plugins.disabled through settings.set.
  togglePy: async (row, on) => { row.state = on ? 'on' : 'off' },
  auth: (name) => gateway().call('plug.auth', { name })
    .then((r) => r.mcp || null)
    .catch((e) => {
      toast(t('gui.plug.op_failed', { err: pmErrText(e) }))
      throw { handled: true }
    }),
  /* The two names the contract does not declare, so they cannot be typed
     calls: a resident gateway answers both with -32601 and the card shows
     that refusal, which is the behaviour to keep until the manual-add path
     has a declared method (rpc-schema/openrpc.json has no raven.mcp.*). */
  manual: async (name, address) => {
    const listed = await gateway().callUnchecked('raven.mcp.list', {}) as { servers?: unknown[] }
    await gateway().callUnchecked('raven.mcp.set', {
      servers: [...(listed.servers || []), { name, address }],
    })
    await loadExt()
  },
  rows: () => extPlugins(),
  /* See skillsSource.loaded: the same one boot-time read feeds both. */
  loaded: () => extIsLoaded(),
  reload: () => loadExt(),
}
