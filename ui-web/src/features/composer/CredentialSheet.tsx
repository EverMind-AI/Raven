/* The credential card's interior: what to enter, a masked field, save or skip.
 *
 * A secret a tool needs (a vendor key, a channel's token) is typed here and
 * goes from the field to the host, which writes it and tells the waiting tool
 * only that it was saved -- the model never reads it. The sheet element, its
 * key handler and the answer's round trip are features/composer/credential.ts;
 * this renders the children, for the reason src/chrome/SheetRack.tsx gives.
 *
 * The field is uncontrolled on purpose: the value is read once, on save, and
 * cleared as it is read, so it lives in the page no longer than the round trip
 * and never in React state that a devtools snapshot or a re-render could keep.
 */
import { SheetActs, SheetHead } from './AskApproveSheet'

import type { SheetOptionRow } from '../../chrome/SheetRack'
import type { JSX } from 'react'

export interface CredentialWords {
  readonly title: string
  readonly skip: string
  readonly hint: string
  readonly replaces: string
  readonly placeholder: string
}

export interface CredentialProps {
  readonly words: CredentialWords
  /* What the card asks for ("Tavily API key") and, when the label does not
     say, one sentence on what it is for. */
  readonly label: string
  readonly note: string
  /* A value is already set, so what is typed replaces it. */
  readonly replaces: boolean
  /* Why the last value was not saved; the card stays up with it. */
  readonly error: string
  readonly busy: boolean
  readonly opts: readonly SheetOptionRow[]
  readonly onSave: () => void
  readonly onSkip: () => void
}

export function CredentialSheet(
  { words, label, note, replaces, error, busy, opts, onSave, onSkip }: CredentialProps,
): JSX.Element {
  return (
    <>
      <SheetHead title={words.title} deny={words.skip} onDeny={onSkip} />
      <div className="body">
        <div className="what cp-ev">
          <div className="cp-ev-path">{label}</div>
          {note ? <div className="cp-cfg-note">{note}</div> : null}
          <input
            className="cp-cfg-key"
            type="password"
            autoComplete="off"
            spellCheck={false}
            data-credential="1"
            aria-label={label}
            placeholder={words.placeholder}
            disabled={busy}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
                e.preventDefault()
                onSave()
              }
            }}
          />
          <div className="cp-cfg-note">{replaces ? words.replaces : words.hint}</div>
          {error ? <div className="cp-cfg-warn" role="alert">{error}</div> : null}
        </div>
      </div>
      <SheetActs opts={opts} />
    </>
  )
}
