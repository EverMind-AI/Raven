/* Which model one instance answers with: the chip beside its effort chip.
 *
 * The pair is deliberate. `InstanceMode` says how hard this instance tries and
 * this says what does the trying, and a reader meets them as one row of
 * per-instance settings rather than as two unrelated controls.
 *
 * Where they differ is what "unset" means, and that difference is the whole
 * reason this is not a copy. A cleared mode falls through to the session's
 * tier, which this host chose and can therefore name -- so that chip draws the
 * inherited rung. A cleared model falls through to whatever the agent picked
 * for itself, which this host cannot see: the agent reports a `currentValue`
 * for the throwaway session a capability probe opened, and that is the state of
 * that session rather than a fact about this instance. Naming it here would put
 * a model on screen this conversation may never have been on, so unset is drawn
 * as "the agent's own" and nothing is invented.
 *
 * The menu is the agent's, measured from its own handshake: the values are
 * opaque provider-qualified ids and the names beside them are far shorter, so
 * the chip shows the name and sends the value.
 *
 * Through the shared menu writer, like the effort chip next to it. The writer
 * draws one flat list, so the agent's grouping survives as a separator rather
 * than as nesting.
 */

import { useCallback, useEffect, useState } from 'react'

import { t } from '../../i18n/t'
import { show as menuAt } from '../../state/menu'
import { show as toast } from '../../state/toast'
import * as store from './store'

import type { InstanceRow, SubagentModelChoice } from './types'
import type { JSX, MouseEvent } from 'react'

interface Held {
  /* This instance's own override, or null -- which is the agent's own choice,
     not a model this host could name. */
  model: string | null
  menu: SubagentModelChoice[]
}

/* What the agent asked to be shown. Falling back to the value is the honest
   last resort: it is long and provider-qualified, but it is what was actually
   offered, and inventing a prettier spelling would name a model under a name
   the agent never gave it. */
const named = (menu: SubagentModelChoice[], value: string | null): string => {
  if (!value) return ''
  const found = menu.find((m) => m.value === value)
  return found?.name || value
}

export function InstanceModel({ row }: { row: InstanceRow }): JSX.Element | null {
  const [held, setHeld] = useState<Held | null>(null)
  const { agent, handle } = row

  const read = useCallback(async (): Promise<void> => {
    const source = store.source()
    if (!source.instanceModel) return
    try {
      const r = await source.instanceModel(agent, handle)
      setHeld({
        model: typeof r.model === 'string' && r.model ? r.model : null,
        menu: (r.availableModels || []).filter((m) => m && m.value),
      })
    } catch {
      /* An instance whose model cannot be read draws no chip, the way the effort
         chip handles the same failure: a control naming a model the next turn
         may not run on is worse than no control, and the pane is usable without
         one. */
      setHeld(null)
    }
  }, [agent, handle])

  /* Re-read when the pane is pointed at a different instance, and not on every
     render of the same one: this is a round trip. No tier subscription like the
     effort chip's -- nothing outside this instance moves what it is showing,
     because there is nothing inherited to move. */
  useEffect(() => { void read() }, [read])

  /* No menu, no chip. A cli agent has no such option and neither does an acp
     agent that advertises none, and for a reader those are the same fact. */
  if (!held || !held.menu.length) return null

  const own = held.model === null
  const label = own ? t('gui.imodel.agent') : named(held.menu, held.model)

  const pick = async (next: string | null): Promise<void> => {
    const source = store.source()
    if (!source.instanceSetModel) return
    try {
      const r = await source.instanceSetModel(agent, handle, next)
      setHeld({
        model: typeof r.model === 'string' && r.model ? r.model : null,
        menu: (r.availableModels || []).filter((m) => m && m.value),
      })
    } catch (err) {
      /* The manager refuses a value this agent does not advertise, and the agent
         itself refuses one it will not write -- both arrive typed. Said out loud
         and the chip left where it was, rather than moved to a model the next
         turn will not run on. */
      const detail = (err as { data?: { detail?: string }; message?: string } | null)
      toast(detail?.data?.detail || detail?.message || t('gui.imodel.failed'))
    }
  }

  const open = (event: MouseEvent<HTMLButtonElement>): void => {
    event.stopPropagation()
    const at = event.currentTarget.getBoundingClientRect()
    /* The agent's own first and always present: it is the state an instance
       starts in and the only way back to it. */
    const rows: Array<{ label: string; fn: () => void } | '-'> = [{
      label: `${t('gui.imodel.agent')}${own ? ' ✓' : ''}`,
      fn: () => { void pick(null) },
    }]
    let group = ''
    held.menu.forEach((m) => {
      /* A separator where the agent's own bucketing changes. Forty ids in one
         unbroken run is not a menu anyone reads. */
      if ((m.group || '') !== group) {
        group = m.group || ''
        rows.push('-')
      }
      rows.push({
        label: `${m.name || m.value}${m.value === held.model ? ' ✓' : ''}`,
        fn: () => { void pick(m.value) },
      })
    })
    menuAt(at.left, at.bottom + 6, rows)
  }

  return (
    <button
      className={'pane-imodel' + (own ? ' auto' : '')}
      onPointerDown={(event) => event.stopPropagation()}
      onClick={open}
      aria-haspopup="true"
      aria-label={`${t('gui.imodel.title')}: ${label}`}
      title={own ? t('gui.imodel.agent_tip') : t('gui.imodel.set_tip')}
    >
      {label}
    </button>
  )
}
