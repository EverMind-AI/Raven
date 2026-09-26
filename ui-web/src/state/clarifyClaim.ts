/* Who answers a mid-turn question, when it is not the composer.
 *
 * `ask_user` arrives as a pushed frame and lands, by default, on a sheet above
 * the composer (features/composer/clarify.ts) -- which is right for a question
 * asked inside a conversation the reader is looking at. A page that runs a
 * conversation of its own is the exception: the persona maker asks the engine
 * to design a Persona, and the engine asks back ("there is already one by that
 * name -- replace it?"). That question belongs on the page that asked, not on
 * a composer the reader is not looking at, which is where it went before this
 * module existed: the sheet opened behind the page and the maker sat waiting
 * for a draft that could not arrive until someone answered it.
 *
 * A claim, not a redirect: the page says which conversation is its own, and
 * anything asked about any other conversation still reaches the sheet.
 */

/** One question, as a claimant needs it. */
export interface ClarifyAsk {
  readonly requestId: string
  readonly question: string
  readonly choices: readonly string[]
}

/** What a page does with a question it owns, and how it answers. */
export type Claimant = {
  ask(ask: ClarifyAsk, answer: (text: string) => void): void
  /** The question is over and nobody answered it (timeout, interruption). */
  close(requestId: string): void
}

const claims = new Map<string, Claimant>()

/** Take the questions asked about this conversation. */
export function claim(conversation: string, claimant: Claimant): void {
  if (conversation) claims.set(conversation, claimant)
}

/** Give them back; the sheet takes over again. */
export function release(conversation: string): void {
  claims.delete(conversation)
}

/** The page that owns this conversation's questions, if one does. */
export const claimantFor = (conversation: string): Claimant | undefined =>
  conversation ? claims.get(conversation) : undefined

export function _resetForTests(): void {
  claims.clear()
}
