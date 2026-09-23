/* Channel branding for the entrances list: each app's own mark, keyed by channel
   id the way AgentMark.tsx keys by preset. */

import { useState } from 'react'

import { assetStamp } from '../lib/assetStamp'

import type { JSX } from 'react'

/* Keyed by channel id -- the adapter's package name -- never by the row's name,
   which is whatever the reader's language calls it. tests/test_ui_channel_marks.py
   holds this table equal to the adapters and to the files
   scripts/refresh_channel_marks.py writes, and that script is where each file's
   source is recorded.

   The PNGs are the apps' own store icons and mochat.svg carries a tile of its
   own, so each fills the frame. `inset` is the one mark that is not a tile:
   Matrix publishes its bracketed m on nothing, so it sits on the frame's plate. */
const MARKS: Record<string, { file: string; inset?: true }> = {
  dingtalk: { file: 'dingtalk.png' },
  discord: { file: 'discord.png' },
  feishu: { file: 'feishu.png' },
  matrix: { file: 'matrix.svg', inset: true },
  mochat: { file: 'mochat.svg' },
  qq: { file: 'qq.png' },
  slack: { file: 'slack.png' },
  telegram: { file: 'telegram.png' },
  wecom: { file: 'wecom.png' },
  weixin: { file: 'weixin.png' },
  whatsapp: { file: 'whatsapp.png' },
}

/* Entrances that are a protocol rather than anyone's brand: a mail provider's
   logo would name one inbox out of every mail host the form accepts. */
const GENERIC = new Set(['email'])

/* What an entrance whose own mark cannot be drawn wears instead. Plain on
   purpose: an id this table has no file for is a gap to fix, not a thing to
   name, and a letter in a coloured square named it -- with the reader's own
   language, so a Chinese entrance wore its first character as if it were an
   initial. */
function PlainIcon(): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M20.5 12c0 4-3.8 7.2-8.5 7.2-1 0-2-.15-2.9-.42L4.5 20.5l1.3-3.6C4.3 15.6 3.5 13.9 3.5 12c0-4 3.8-7.2 8.5-7.2s8.5 3.2 8.5 7.2Z" />
    </svg>
  )
}

function MailIcon(): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="3.5" y="5.5" width="17" height="13" rx="2.5" />
      <path d="m4.5 7.5 7.5 5.5 7.5-5.5" />
    </svg>
  )
}

/* One frame per entrance. An entrance with no mark draws the plain one: an id
   nobody drew, or a file the request could not find. The second is only known
   once it fails, so the failure is remembered against the URL rather than the
   slot, and a row redrawn as another channel starts clean. `hasOwn`, not a bare
   index: every object literal answers `constructor` from its prototype. */
export function ChannelMark({ id }: { id: string }): JSX.Element {
  const [failed, setFailed] = useState<string | null>(null)
  if (GENERIC.has(id)) {
    return (
      <span className="channel-mark channel-mark-inset" aria-hidden="true">
        <MailIcon />
      </span>
    )
  }
  const mark = Object.hasOwn(MARKS, id) ? MARKS[id] : undefined
  const src = mark ? `assets/channels/${mark.file}${assetStamp()}` : null
  if (!mark || !src || failed === src) {
    return (
      <span className="channel-mark channel-mark-inset" aria-hidden="true">
        <PlainIcon />
      </span>
    )
  }
  return (
    <span className={mark.inset ? 'channel-mark channel-mark-inset' : 'channel-mark'} aria-hidden="true">
      <img src={src} alt="" draggable="false" onError={() => setFailed(src)} />
    </span>
  )
}
