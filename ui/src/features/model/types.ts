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
  current(): string
  /* Show `m` as chosen without waiting for the server. Kept apart from persist
     below because the picker sets it twice on a failure -- forward, then back --
     and neither of those is a write to the config. */
  setLocal(m: string): void
  /* Send it. Rejecting is meaningful: the picker rolls the local pick back. */
  persist(m: string): Promise<void>
  /* The settings door, for the picker's own footer. Only offered when the
     picker was opened from the composer chip, since the settings page opening
     itself is not a way out of it. */
  openSettings(): void
}
