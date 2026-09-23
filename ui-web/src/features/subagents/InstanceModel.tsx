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

import { useCallback, useEffect, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { show as menuAt } from '../../state/menu'
import { show as toast } from '../../state/toast'
import { defaultProviders as hostProviders, loadDefaultProviders } from '../model/source'
import { offered, withCurrent } from '../model/types'
import * as store from './store'

import type { InstanceRow, SubagentModelChoice, SubagentRow } from './types'
import type { JSX, MouseEvent } from 'react'

interface Held {
  /* This instance's own override, or null -- which is the agent's own choice,
     not a model this host could name. */
  model: string | null
  menu: SubagentModelChoice[]
}

/* The menu for an agent of raven's own: this host's live catalogue, through the
   composer's own rule, because that is what such an agent actually runs on.

   Not the agent's own `availableModels` -- for one of raven's own, that list is
   this same catalogue captured at handshake time, put through the ACP option
   builder: measured once on a probe session, capped at forty ids per provider
   (`MAX_MODELS_PER_PROVIDER`), and stale from the first credential edit after
   it. The agents page stopped drawing that capture; a chip that kept drawing it
   would put two menus on one catalogue, which is the defect either way.

   A third party keeps its own list. Its credentials decide what it can run, and
   raven's ids would be refused by the agent itself. */
const hostMenu = (held: string | null): SubagentModelChoice[] =>
  hostProviders()
    .filter((p) => p.on)
    .flatMap((p) => {
      /* The value carries its provider, because that is all this control sends:
         a row's picker names the provider in a field of its own, and this menu
         has one string per entry. A bare id would be claimed by keyword
         matching at dispatch, which sends it wherever those rules land rather
         than to the account the reader picked it under. */
      const held_ = held && held.startsWith(`${p.id}/`) ? held : null
      return withCurrent(p, offered(p, 'text').map((m) => `${p.id}/${m}`), held_)
        .map((value) => ({ value, name: value.slice(p.id.length + 1) || value, group: p.name }))
    })

/* Which vocabulary this instance's agent takes -- the same `model_source` the
   agents page reads, so the two surfaces cannot disagree about one agent.

   `null` is "not known yet", and it is a state this control has to sit out
   rather than guess through. A roster that has answered always holds at least
   the built-in row, so an empty one means no answer has arrived -- and a pane
   can easily be open before one is asked for: desk restoration opens panes
   straight off the instance list, and the only fetch in the product is the
   agents list's own. Reading '' there would draw the handshake catalogue for
   one of raven's own, and a chip for a product that manages its own model:
   exactly what this control is here to stop. */
const ruleOf = (roster: SubagentRow[], agent: string): string | null =>
  roster.length ? roster.find((r) => r.name === agent)?.model_source || '' : null

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
  /* Watched, not read once: the rule lives on the roster, and the roster can
     arrive after this pane does. Asked for here as well, because the agents
     list is the only thing in the product that asks, and a pane restored onto
     the desk never mounts it. `refreshRoster` is a no-op once one has landed. */
  const s = useSyncExternalStore(store.subscribe, store.get)
  useEffect(() => { store.refreshRoster() }, [])
  const rule = ruleOf(s.roster, agent)

  const read = useCallback(async (): Promise<void> => {
    const source = store.source()
    if (!source.instanceModel || rule === null) return
    /* A row whose model is its own folder's (`fixed`) has no menu to offer here
       either: the agents page draws it as managed by itself, and a chip letting
       a reader pick would be the page saying two things about one agent. */
    if (rule === 'fixed') { setHeld(null); return }
    /* Loaded at boot for the composer's chip; a pane opened before that landed
       asks once itself rather than offering nothing. */
    if (rule === 'raven' && !hostProviders().length) await loadDefaultProviders()
    try {
      const r = await source.instanceModel(agent, handle)
      const now = typeof r.model === 'string' && r.model ? r.model : null
      setHeld({
        model: now,
        menu: rule === 'raven' ? hostMenu(now) : (r.availableModels || []).filter((m) => m && m.value),
      })
    } catch {
      /* An instance whose model cannot be read draws no chip, the way the effort
         chip handles the same failure: a control naming a model the next turn
         may not run on is worse than no control, and the pane is usable without
         one. */
      setHeld(null)
    }
  }, [agent, handle, rule])

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
      const now = typeof r.model === 'string' && r.model ? r.model : null
      setHeld({
        model: now,
        menu: rule === 'raven' ? hostMenu(now) : (r.availableModels || []).filter((m) => m && m.value),
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
      /* A separator where the bucketing changes -- the agent's own for a third
         party, the provider for one of raven's own. A long unbroken run is not
         a menu anyone reads. */
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
