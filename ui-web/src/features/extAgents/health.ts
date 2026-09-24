/* One health verdict for a row, read by the card's dot and the sheet's status
 * line alike. Each surface used to derive its own from different facts -- the
 * card from the probe verdict, the sheet from the last test -- which is how one
 * row came to wear a gold dot on the card and a green one in the sheet. The
 * order in `healthOf` is the whole of the policy, written once: what is
 * happening now, then a write the server refused, then a test that failed,
 * then a probe with a caveat, then a row that is simply on.
 *
 * `Shown` and its readers live here too: the section a row is drawn in is the
 * first thing the verdict is derived from.
 */

import { t } from '../../i18n/t'
import { byOf } from './catalogue'
import { sectionOf } from './source'
import * as store from './store'

import type { ExtAgentsState, Failure } from './store'
import type { ExtAgentRow } from './types'

/* The states a row and its sheet are drawn in. `pending` and `failed` are
   this page's own, about a write in flight or refused; the other three are the
   section the server's facts put the row in. */
export type Shown = 'pending' | 'failed' | 'missing' | 'on' | 'off'

export function shownOf(row: ExtAgentRow, s: ExtAgentsState): Shown {
  if (row.name in s.joining) return 'pending'
  if (s.failed[row.name]) return 'failed'
  const section = sectionOf(row)
  return section === 'missing' ? 'missing' : section === 'on' ? 'on' : 'off'
}

/* The word a row wears while its own write is in flight. One `pending` covers
   every write this page makes, so the word comes from the write rather than
   from the state: a switch-on waits on the readiness ping and says it is
   testing, a switch-off waits on its own write alone and says it is
   disconnecting, and the rest -- a key, a model, a description -- keep the
   older word, being neither. */
export function pendingLabel(row: ExtAgentRow, s: ExtAgentsState): string {
  const write = s.joining[row.name]
  if (!write) return 'gui.agent.setup_connecting'
  if (store.probes(write)) return 'gui.agent.testing'
  return store.disconnects(write) ? 'gui.agent.disconnecting' : 'gui.agent.setup_connecting'
}

/* Which write a refusal refused, which decides its words when no fix is named:
   a switch-off is not a connect (`disconnects` says so for the pending ring
   too), and an edit is neither. The sheet asks the same question, so a card and
   its sheet cannot say two things about one refusal. */
export function refusedWrite(failed: Failure): 'connect' | 'disconnect' | 'save' {
  if (store.disconnects(failed)) return 'disconnect'
  return failed.op === 'model' || failed.op === 'update' ? 'save' : 'connect'
}

/* What a refused write comes to, in the reader's language: the fix the server
   named, by its kind, or what failed when it named none. The server's own
   sentence is English, and cut to the two lines a card has it said neither
   what went wrong nor what to do -- so it is shown only in the sheet, folded
   under the fix. */
export function whatFailed(row: ExtAgentRow, failed: Failure): string {
  const agent = row.name
  const kind = failed.remedy?.kind
  if (kind === 'sign_in') return t('gui.agent.bad_sign_in', { agent })
  if (kind === 'setup') return t('gui.agent.bad_setup', { agent })
  if (kind === 'api_key') return t('gui.agent.bad_api_key', { agent })
  if (kind === 'download') return t('gui.agent.bad_download', { agent })
  if (kind === 'model') return t('gui.agent.bad_model', { agent })
  if (kind === 'billing') return t('gui.agent.bad_billing', { agent })
  if (kind === 'quota') return t('gui.agent.bad_quota', { agent })
  if (kind === 'network') return t('gui.agent.bad_network', { agent })
  if (kind === 'silent') return t('gui.agent.bad_silent', { agent })
  if (kind === 'upgrade') return t('gui.agent.bad_upgrade', { agent })
  if (kind === 'exited') return t('gui.agent.bad_exited', { agent })
  const write = refusedWrite(failed)
  return t(write === 'save' ? 'gui.agent.bad_save' : write === 'disconnect' ? 'gui.agent.bad_disconnect' : 'gui.agent.bad_connect', {
    agent,
  })
}

export type Tone = 'busy' | 'bad' | 'warn' | 'good' | 'none'

export interface Health {
  tone: Tone
  /* The verdict in the reader's language: the dot's accessible name, and the
     sheet's line wherever the sheet has no fuller block for it. Empty for
     `none`, which has nothing to say. */
  label: string
  /* Which fact made a `bad`: a write the server refused, or the last test. */
  from?: 'write' | 'test'
}

/* The dot's class for a tone; `none` is never drawn. */
export function ledClass(tone: Tone): string {
  return tone === 'busy'
    ? 'extAgents-led extAgents-led-busy'
    : tone === 'bad'
      ? 'extAgents-led extAgents-led-bad'
      : tone === 'warn'
        ? 'extAgents-led extAgents-led-warn'
        : 'extAgents-led'
}

/* A failed test outranks a caveat because it is the stronger measurement. A
   caveat outranks a test that passed, because the probe's caveats are about
   now -- a launch config that changed since that test, a menu never measured,
   a model the endpoint no longer lists -- and a pass from before answers none
   of them; the test that clears a caveat is the next one, whose re-read
   probes again. A test under way counts whichever side started it: this
   page's own flag, or the row's. `missing` keeps its own line -- "not found"
   outranks a verdict measured before the executable went away -- so no test
   is reported on it. */
export function healthOf(row: ExtAgentRow, s: ExtAgentsState): Health {
  const shown = shownOf(row, s)
  const testing = s.testing.includes(row.name) || row.test_running
  if (shown === 'pending' || testing) {
    return { tone: 'busy', label: t(testing ? 'gui.agent.testing_head' : pendingLabel(row, s)) }
  }
  const failed = s.failed[row.name]
  if (failed) return { tone: 'bad', label: whatFailed(row, failed), from: 'write' }
  if (row.last_test_ok === false && shown !== 'missing') {
    return { tone: 'bad', label: t('gui.agent.hd_on_test_bad'), from: 'test' }
  }
  if (shown !== 'on') return { tone: 'none', label: '' }
  if (!row.builtin && row.probe_status === 'attention') return { tone: 'warn', label: t('gui.agent.hd_on_attention') }
  return { tone: 'good', label: row.last_test_ok ? t('gui.agent.hd_on_tested') : t('gui.agent.hd_on_by', { by: byOf(row) }) }
}
