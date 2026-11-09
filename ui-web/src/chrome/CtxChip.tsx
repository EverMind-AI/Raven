/* The composer's context ring: one chip in the bar under the field.
 *
 * A file of its own rather than a few lines of <Dock/>, because what it renders
 * is the whole of a store (src/state/ctxWindow.ts) -- the showing, the two
 * warmth classes, the dash left to go and the one sentence the hover pill and
 * the accessible name share. Every one of those used to be a write by id.
 *
 * Both attributes are dropped rather than emptied while no window is known:
 * page.html serves the chip with neither, and a data-* that appears at boot
 * would move what the region goldens record.
 *
 * The click opens the popover (./CtxPopover.tsx), which reads the same store.
 */

import { useSyncExternalStore } from 'react'

import * as ctx from '../state/ctxWindow'

import type { JSX } from 'react'

export function CtxChip(): JSX.Element {
  const s = useSyncExternalStore(ctx.subscribe, ctx.get)
  return (
    <button
      className={`chip ctx${s.warm ? ' warm' : ''}${s.hot ? ' hot' : ''}`}
      id="ctxChip"
      hidden={!s.shown}
      data-tip={s.tip ?? undefined}
      aria-label={s.tip ?? undefined}
      aria-haspopup="true"
      aria-expanded={s.open ? 'true' : 'false'}
      onClick={() => ctx.toggle()}
    >
      <svg className="ring" viewBox="0 0 20 20" aria-hidden="true">
        <circle className="bg" cx="10" cy="10" r="7.6" />
        <circle className="fg" cx="10" cy="10" r="7.6" strokeDashoffset={s.offset ?? undefined} />
      </svg>
    </button>
  )
}
