/* The waiting and status copy both front ends draw while work is under way:
   every English word there starts with a capital, and the tail row says one
   short word once only delegated work is left. */
import { describe, expect, it } from 'vitest'

import catalog from '../../../i18n/messages.json'
import verbSpec from '../../../ui-shared/verbs.json'

const ui = (catalog as { ui: Record<string, { en?: string }> }).ui

const WAITING_KEYS = [
  'gui.live.waiting_subagents',
  'gui.panel.loading_subagents',
  'gui.panel.loading_skills',
  'gui.panel.loading_sessions',
  'gui.panel.loading_models',
  'gui.panel.loading',
  'gui.panel.queued_none',
  'gui.panel.waiting_first',
  'gui.panel.boot_summon',
  'gui.panel.boot_loop',
  'gui.panel.boot_tools',
  'gui.cron.running_x',
  'gui.hub.reading',
  'gui.hub.working',
  'gui.meter.running',
  'gui.pb.cred_authorizing',
  'gui.pb.stint_live',
  'gui.pb.stint_unanswered',
  'gui.pb.stint_status_running',
  'gui.dag.st_pending',
  'gui.dag.st_running',
]

describe('the waiting copy', () => {
  it('capitalises every rotating verb', () => {
    const verbs = verbSpec.verbs
    expect(verbs).toHaveLength(14)
    expect(verbs[0]).toBe('Circling')
    expect(verbs[13]).toBe('Plotting')
    expect(verbs.filter((v) => !/^[A-Z]/.test(v))).toEqual([])
  })

  it('capitalises the waiting and status copy', () => {
    const lower = WAITING_KEYS.filter((k) => !/^[A-Z]/.test(ui[k]?.en ?? ''))
    expect(lower).toEqual([])
  })

  it('says Waiting on the tail row', () => {
    expect(ui['gui.live.waiting_subagents']?.en).toBe('Waiting')
    expect(ui['gui.live.orbs_label']?.en).toBe('{n} subagents running: {names}')
  })
})
