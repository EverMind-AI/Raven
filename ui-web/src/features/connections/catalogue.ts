/* The twelve entrances raven can be reached through.
 *
 * Production data, not a fixture: the live source merges `channels.status`
 * onto these same rows, so the ids, the two naming spellings and the
 * scan-login marker are what the page draws from in both modes. It lived in
 * the demo shell's fixture table only because that is where the offline page
 * first needed it.
 *
 * Two spellings on purpose: `key` names a message-catalogue entry for the
 * channels whose English and Chinese names differ, `name` is a brand name used
 * verbatim. `chanName` is the one accessor, so a renderer never has to know
 * which of the two a row carries.
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
  key?: string
  name?: string
  qrLogin?: boolean
}

const CATALOGUE: readonly CatalogueEntry[] = [
  { id: 'feishu', key: 'gui.chan.feishu' },
  { id: 'wecom', key: 'gui.chan.wecom' },
  { id: 'weixin', key: 'gui.chan.weixin', qrLogin: true },
  { id: 'slack', name: 'Slack' },
  { id: 'dingtalk', key: 'gui.chan.dingtalk' },
  { id: 'qq', name: 'QQ' },
  { id: 'telegram', name: 'Telegram' },
  { id: 'discord', name: 'Discord' },
  { id: 'whatsapp', name: 'WhatsApp', qrLogin: true },
  { id: 'email', key: 'gui.chan.email' },
  { id: 'matrix', name: 'Matrix' },
  { id: 'mochat', key: 'gui.chan.mochat' },
]

export const CHANNELS: ConnChannel[] = CATALOGUE.map((c) => ({ ...c, on: false }))

export const chanName = (c: ConnChannel): string => (c.key ? t(c.key) : c.name || '')
