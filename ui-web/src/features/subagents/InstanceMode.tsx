/* One instance's own operating mode: the chip on its pane header.
 *
 * The session tier (`shell/tier.ts`) says what raven asks of every sub-agent it
 * dispatches. This says what it asks of THIS one, and the two are a precedence
 * rather than a pair: `SubagentManager.resolve_mode` reads the instance's
 * override, and falls through to the session's tier when there is none.
 *
 * So the default is not a mode -- it is the ABSENCE of one, and the wire says so
 * with `mode: null`. The row for it reads "Auto", and `clear: true` is what puts
 * an instance back on it. `inherited` is the other half of that reply: what a
 * dispatch would actually run at with no override, already clamped to this
 * agent's own rungs, which is the only way "Auto" can say more than "whatever
 * the session says" -- the session's tier may be one this agent cannot rank.
 *
 * The menu is this AGENT's, not the ladder, and it is read from the agent's own
 * ACP handshake rather than assumed. No shipped agent needs that today, which is
 * worth stating plainly: raven-research declares `medium`/`high`/`max` with
 * labels and descriptions of its own, and every other product ships no `modes/`
 * and so inherits raven's built-in three -- so every menu on the current tree
 * spells the ladder. The seam is not decoration, though: a deployment declares
 * whatever vocabulary it likes, `clamp_tier` recognises only the three ladder
 * names, and rendering the ladder here would offer an agent rungs it never
 * advertised and cannot run.
 *
 * Through the shared menu writer rather than a popover of its own: a pane header
 * is not the composer, and the one-line rows the writer draws are what the model
 * chip already uses, trailing tick and all.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { t } from '../../shell/bridge'
import { show as menuAt } from '../../shell/menu'
import { watch as watchTier } from '../../shell/tier'
import { show as toast } from '../../shell/toast'
import * as store from './store'

import type { JSX, MouseEvent } from 'react'
import type { InstanceRow, SubagentMode } from './types'

interface Held {
  /* This instance's own override, or null when it has none -- which is Auto. */
  mode: string | null
  /* What a dispatch runs at with no override: the session's tier clamped to this
     agent's rungs, or null when nothing is inherited and the agent's own default
     is what runs. */
  inherited: string | null
  menu: SubagentMode[]
}

const named = (menu: SubagentMode[], id: string | null): string => {
  if (!id) return ''
  const found = menu.find((m) => m.id === id)
  return found?.name || id
}

export function InstanceMode({ row }: { row: InstanceRow }): JSX.Element | null {
  const [held, setHeld] = useState<Held | null>(null)
  const { agent, handle } = row

  const read = useCallback(async (): Promise<void> => {
    const source = store.source()
    if (!source.instanceMode) return
    try {
      const r = await source.instanceMode(agent, handle)
      setHeld({
        mode: typeof r.mode === 'string' && r.mode ? r.mode : null,
        inherited: typeof r.inherited === 'string' && r.inherited ? r.inherited : null,
        menu: (r.availableModes || []).filter((m) => m && m.id),
      })
    } catch {
      /* An instance whose mode cannot be read draws no chip. The alternative is
         a control that names a rung the next turn may not run at, and the pane
         is perfectly usable without it. */
      setHeld(null)
    }
  }, [agent, handle])

  /* Re-read when the pane is pointed at a different instance. Not on every
     render of the same one: this is a round trip. */
  useEffect(() => { void read() }, [read])

  /* And when the conversation's tier moves, because under Auto that IS what
     this chip is showing -- `inherited` is the session's tier clamped to this
     agent, so a switch changes it here with nothing on this pane touched. The
     server uses the new one on the instance's next turn; without this the chip
     went on naming the old one, which is the one thing it exists to get right.

     Only under Auto: an instance with an override of its own is not affected by
     the tier, and `inherited` is not what it is showing. Through a ref rather
     than a dependency so the subscription is not torn down and rebuilt every
     time the reader switches this instance's own mode. */
  const autoNow = useRef(false)
  autoNow.current = held?.mode == null
  useEffect(() => watchTier(() => { if (autoNow.current) void read() }), [read])

  if (!held || !held.menu.length) return null

  const auto = held.mode === null
  /* The chip says what this instance will actually run at, which under Auto is
     the inherited rung -- naming "Auto" alone would leave the reader to open the
     menu to learn the one thing the chip is for. */
  const showing = auto ? held.inherited : held.mode
  const label = showing ? named(held.menu, showing) : t('gui.imode.auto')

  const pick = async (next: string | null): Promise<void> => {
    const source = store.source()
    if (!source.instanceSetMode) return
    try {
      const r = await source.instanceSetMode(agent, handle, next)
      setHeld({
        mode: typeof r.mode === 'string' && r.mode ? r.mode : null,
        inherited: typeof r.inherited === 'string' && r.inherited ? r.inherited : null,
        menu: (r.availableModes || []).filter((m) => m && m.id),
      })
    } catch (err) {
      /* The manager refuses a rung this agent does not advertise, naming what it
         does offer. Said out loud and the chip left where it was, rather than
         moved to a mode the next turn will not run at. */
      const detail = (err as { data?: { detail?: string }; message?: string } | null)
      toast(detail?.data?.detail || detail?.message || t('gui.imode.failed'))
    }
  }

  const open = (event: MouseEvent<HTMLButtonElement>): void => {
    event.stopPropagation()
    const at = event.currentTarget.getBoundingClientRect()
    /* Auto first and always present: it is the state an instance starts in, and
       the only way back to it. Its row says where it currently lands, so the
       reader can see what they are choosing between rather than what it is
       called. */
    const inherited = held.inherited ? named(held.menu, held.inherited) : t('gui.imode.agent_default')
    const rows: Array<{ label: string; fn: () => void }> = [{
      label: `${t('gui.imode.auto')} · ${inherited}${auto ? ' ✓' : ''}`,
      fn: () => { void pick(null) },
    }]
    held.menu.forEach((m) => {
      rows.push({
        label: `${m.name || m.id}${m.id === held.mode ? ' ✓' : ''}`,
        fn: () => { void pick(m.id) },
      })
    })
    menuAt(at.left, at.bottom + 6, rows)
  }

  return (
    <button
      className={'pane-imode' + (auto ? ' auto' : '')}
      onPointerDown={(event) => event.stopPropagation()}
      onClick={open}
      aria-haspopup="true"
      aria-label={`${t('gui.imode.title')}: ${label}`}
      title={auto ? t('gui.imode.auto_tip') : t('gui.imode.set_tip')}
    >
      {label}
    </button>
  )
}
