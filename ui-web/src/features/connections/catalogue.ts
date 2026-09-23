/* The twelve entrances raven can be reached through.
 *
 * Production data, not a fixture: the live source merges `channels.status`
 * onto these same rows, so the ids, the message keys and the scan-login
 * marker are what the channels section draws from in both modes. It lived in
 * the demo shell's fixture table only because that is where the offline page
 * first needed it.
 *
 * Every entrance is named through the message catalogue, in both of the
 * page's languages -- a brand spelled the same in both still has its entry, so
 * what each language calls a row is a decision the catalogue records rather
 * than one a row can skip (catalogue.test.ts holds every entry to it).
 * `chanName` is the one accessor.
 *
 * The rows are one shared, mutable array. Both sources answer with these
 * objects and write onto them -- the fixture so a demo edit sticks across a
 * redraw, the rpc source so a merged status lands on the rows the list is
 * already drawn from -- and holding one array is what makes that true.
 */

import { t } from '../../i18n/t'

import type { ConnChannel } from './types'

interface CatalogueEntry {
  id: string
  key: string
  qrLogin?: boolean
}

const CATALOGUE: readonly CatalogueEntry[] = [
  { id: 'feishu', key: 'gui.chan.feishu' },
  { id: 'wecom', key: 'gui.chan.wecom' },
  { id: 'weixin', key: 'gui.chan.weixin', qrLogin: true },
  { id: 'slack', key: 'gui.chan.slack' },
  { id: 'dingtalk', key: 'gui.chan.dingtalk' },
  { id: 'qq', key: 'gui.chan.qq' },
  { id: 'telegram', key: 'gui.chan.telegram' },
  { id: 'discord', key: 'gui.chan.discord' },
  { id: 'whatsapp', key: 'gui.chan.whatsapp', qrLogin: true },
  { id: 'email', key: 'gui.chan.email' },
  { id: 'matrix', key: 'gui.chan.matrix' },
  { id: 'mochat', key: 'gui.chan.mochat' },
]

export const CHANNELS: ConnChannel[] = CATALOGUE.map((c) => ({ ...c, on: false }))

export const chanName = (c: ConnChannel): string => t(c.key)
