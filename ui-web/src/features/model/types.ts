/* The default-model picker's rows and its DataSource contract.
 *
 * One provider carries more than this page shows -- the settings island reads
 * the key state, the auth kind and the warnings off the same objects -- so the
 * shape here is the shared one, and this feature only reads the three fields it
 * draws from.
 */

export interface Provider {
  id: string
  name: string
  models: string[]
  /* Authenticated. A provider without an account is not offered: picking one of
     its models would fail on the next turn rather than at the click. */
  on: boolean
  kind?: string
}

export interface ModelSource {
  providers(): Provider[]
  /* Send it. The provider is required: a model id does not name whose
     credential serves it, so the backend refuses a switch without one -- the
     column the model was chosen from is that answer. ``scope`` says whether the
     switch is for this conversation or the global default, carried from the
     opener rather than guessed from whether a conversation is open. Rejecting is
     meaningful: the picker rolls the local pick back. Resolving to ``'staged'``
     means the pick was held rather than applied (a draft has no session yet),
     and the picker words its toast accordingly. */
  persist(m: string, provider: string, scope: 'session' | 'default'): Promise<void | 'staged'>
  /* The settings door, for the picker's own footer. Only offered when the
     picker was opened from the composer chip, since the settings page opening
     itself is not a way out of it. */
  openSettings(): void
}
