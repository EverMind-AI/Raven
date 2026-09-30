/* -- personas: the rpc source ------------------------------------------
   The page renderer is the persona island (ui-web/src/features/persona/); this
   module only knows how to speak the contract.

   The wall reads the same listing the playbook library answers and narrows it
   in the island (./store.ts's `isPersona`), rather than asking for a second
   surface: giving personas their own list method would let two answers
   disagree about the same file.

   Making one is an ordinary turn with `playbook_mode: 'persona'`, in a
   conversation of its own. The turn generates a Harness and the engine holds it
   as a draft (raven/agent/loop/wiring.py), so nothing is written until `save`
   here -- which is the whole point of the page. */

import { gateway } from '../../rpc/gateway'

import type { PersonaLine, PersonaRow, PersonaSource, PersonaStep } from './types'

export const personaSource: PersonaSource = {
  list: () => gateway().call('playbooks.list', {}).then((r) => (r.playbooks || []) as PersonaRow[]),

  openMaker: () => gateway().call('session.create', {}).then((r) => r.session_id),

  describe: (sessionKey, text) =>
    gateway().call('turn.send', { session_key: sessionKey, content: text, playbook_mode: 'persona' })
      .then(() => undefined),

  /* Read back rather than streamed: this page draws what was said, not how it
     arrived, and the turn's frames belong to the transcript island. */
  history: (sessionKey) =>
    gateway().call('session.resume', { session_id: sessionKey }).then((r) =>
      (r.messages || [])
        .filter((m) => (m.role === 'user' || m.role === 'assistant') && (m.text || '').trim())
        .map((m) => ({ role: m.role as PersonaLine['role'], text: (m.text || '').trim() })),
    ),

  /* The turn's own frames, filtered to this page's conversation. `gateway().on`
     keeps a set of handlers, so this listens beside the transcript's rather
     than in place of it -- and the frames are routed by subscription, which is
     why the page takes one of its own (state/session/registry.ts does the same
     for a conversation on screen). */
  watch: async (sessionKey, onStep) => {
    const sub = await gateway().call('turn.subscribe', { session_key: sessionKey })
    const id = sub.subscription_id
    const step = (kind: PersonaStep['kind'], label = ''): void => onStep({ kind, label })
    const off = gateway().on('event', (frame: unknown) => {
      const f = frame as { subscription_id?: string; event?: { type?: string; payload?: { name?: string } } }
      if (f.subscription_id !== id) return
      const type = f.event?.type
      if (type === 'turn.started' || type === 'thinking.delta') step('thinking')
      else if (type === 'tool.start') step('tool', f.event?.payload?.name || '')
      else if (type === 'token.delta') step('saying')
      else if (type === 'message.complete') step('done')
      else if (type === 'error') step('error')
    })
    return () => {
      off()
      gateway().call('turn.unsubscribe', { subscription_id: id }).catch(() => {})
    }
  },

  draft: (sessionKey) =>
    gateway().call('playbooks.draft', { session_key: sessionKey })
      .then((r) => (r.draft as PersonaRow | undefined) || null),

  save: (sessionKey, name) =>
    gateway().call('playbooks.draft_save', name ? { session_key: sessionKey, name } : { session_key: sessionKey })
      .then((r) => r.name),

  discard: (sessionKey) =>
    gateway().call('playbooks.draft_discard', { session_key: sessionKey }).then(() => undefined),

  remove: (name) => gateway().call('playbooks.delete', { name }).then(() => undefined),
}
