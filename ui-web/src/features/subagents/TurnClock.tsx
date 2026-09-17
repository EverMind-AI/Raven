/* How long the turn this instance is answering has been running.
 *
 * The same number the main transcript puts beside a running card, in the same
 * spelling (`lib/duration.ts`) and on the same self-stopping clock
 * (`lib/tick.ts`), so a reader who has learned to read one has learned both.
 *
 * The instance's own lifetime would have been the easy number and the wrong
 * one: it is a different fact wearing the same clothes, and a pane header is
 * exactly where the two would be confused. What the server publishes is the
 * turn's start and only while there is a turn -- `turnStartedAtMs` absent is
 * "answering nothing", which is why this draws nothing rather than freezing on
 * a last value when the run ends.
 *
 * Not `updatedAtMs`, which was the tempting field already on the row: every
 * registry write stamps it, so a clock counting from it would jump whenever a
 * binding commit or a graph-origin write touched the record.
 */

import { formatDuration } from '../../lib/duration'
import { t } from '../../i18n/t'
import { useTick } from '../../lib/tick'

import type { InstanceRow } from './types'
import type { JSX } from 'react'

export function TurnClock({ row }: { row: InstanceRow }): JSX.Element | null {
  const began = row.turnStartedAtMs
  const live = typeof began === 'number' && began > 0
  /* Called before the early return, or the hook count changes with the row's
     state and React unmounts the pane around it. `0` is never read when the
     clock is off. */
  const elapsed = useTick(live, live ? began : 0)
  if (!live) return null
  /* Held back below a second, the way the transcript holds its own: a clock
     that opens on "0.0s" reads as broken rather than as new. */
  if (elapsed < 1000) return null
  const spelled = formatDuration(elapsed)
  return (
    <span className="pane-turnms" title={t('gui.ws.instance_turn_elapsed', { d: spelled })}>
      {spelled}
    </span>
  )
}
