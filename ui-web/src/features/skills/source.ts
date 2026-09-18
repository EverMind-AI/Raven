/* The skills island's source: the hub half, plus the installed rows the one
 * shared read already holds (../installed/source.ts).
 *
 * skillhub.* is the hub's /skills/search -- 93k skills, paged, filterable by
 * category. Search rejections travel back raw: the island renders them in place
 * with a retry. install and remove toast their own failures here and reject
 * `{handled: true}`, the contract the island's catch reads. Both refresh the
 * live rows through loadExt so every installed surface answers the change.
 */

import { t } from '../../i18n/t'
import { gateway } from '../../rpc/gateway'
import { show as toast } from '../../state/toast'
import { extIsLoaded, extSkills, loadExt } from '../installed/source'

import type { SkillsSource } from './types'

const skillhubErr = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string }
  return (err && err.data && err.data.detail) || (err && err.message) || String(e)
}

/* -- skills: the hub half ---------------------------------------------
   skillhub.* is the hub's /skills/search -- 93k skills, paged, filterable by
   category. Search rejections travel back raw: the island renders them in
   place with a retry. install/remove toast their own failures here and reject
   `{handled: true}`, the contract the island's catch reads. Both refresh the
   live rows through loadExt so every installed surface answers the change. */
export const skillsSource: SkillsSource = {
  search: (p) => gateway().call('skillhub.search', {
    query: p.query, category: p.category, page: p.page, limit: p.limit,
  }),
  detail: (id) => gateway().call('skillhub.detail', { id }),
  install: (id) => gateway().call('skillhub.install', { id })
    .then(() => loadExt().catch(() => {}))
    .catch((e) => {
      toast(t('gui.plug.op_failed', { err: skillhubErr(e) }))
      throw { handled: true }
    }),
  remove: (name) => gateway().call('skillhub.remove', { name })
    .then(() => loadExt().catch(() => {}))
    .catch((e) => {
      toast(t('gui.plug.op_failed', { err: skillhubErr(e) }))
      throw { handled: true }
    }),
  installed: () => extSkills(),
  /* Whether the one boot-time read actually landed. Without it an empty
     list means both "nothing is installed" and "the read never happened",
     and the page has to draw the same nothing for a working install and a
     broken socket. */
  loaded: () => extIsLoaded(),
}
