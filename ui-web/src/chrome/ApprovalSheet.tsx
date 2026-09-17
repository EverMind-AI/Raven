/* The permission gate's approval sheet: the command, a note that rides a
 * refusal, and the five answers -- allow once, allow for this session, allow and
 * save a prefix rule, deny, deny and stop.
 *
 * The sheet element, the answers and the document key handler belong to
 * features/composer/approve.ts; this renders its children, for the reason
 * src/chrome/SheetRack.tsx gives.
 *
 * Both fields are uncontrolled and listened to natively -- a component owning
 * the value re-renders a field the reader is typing into -- and both are read
 * from and written to state/sheetDrafts.ts on every keystroke, because the
 * interior is unmounted while the reader is in another conversation. The opener
 * still reads the elements themselves when an answer is sent: what it owes the
 * model is what stands in the field at that moment.
 */
import { useEffect, useLayoutEffect, useRef } from 'react'

import { SheetHead } from './ApproveSheet'
import { SheetOption } from './SheetRack'
import * as drafts from '../state/sheetDrafts'

import type { SheetOptionRow } from './SheetRack'
import type { JSX } from 'react'

/* What the opener reads when it answers: the note the refusal carries, and the
   prefix as the reader left it. Elements rather than values, because the two are
   the same fields the reader is typing into and an answer takes their contents
   at the moment it is sent. */
export interface ApprovalControls {
  note: HTMLInputElement | null
  pattern: HTMLInputElement | null
}

export interface ApprovalWords {
  readonly title: string
  readonly deny: string
  readonly notePh: string
  readonly patternFor: string
}

export interface ApprovalProps {
  readonly ctl: ApprovalControls
  readonly draft: string
  readonly command: string
  readonly words: ApprovalWords
  readonly opts: readonly SheetOptionRow[]
  /** The prefix the runtime offered, which a draft overrides. */
  readonly suggested?: string
  readonly onDeny: () => void
  readonly onSaveRule: () => void
}

export function ApprovalSheet(
  { ctl, draft, command, words, opts, suggested, onDeny, onSaveRule }: ApprovalProps,
): JSX.Element {
  const note = useRef<HTMLInputElement | null>(null)
  const pattern = useRef<HTMLInputElement | null>(null)

  /* Before the effects below, so an answer given in the same task as the mount
     already has the fields to read. */
  useLayoutEffect(() => {
    ctl.note = note.current
    ctl.pattern = pattern.current
  }, [ctl])

  useEffect(() => {
    const el = note.current
    if (!el) return undefined
    el.value = drafts.read(draft).note || ''
    const onInput = (): void => drafts.write(draft, { note: el.value })
    el.addEventListener('input', onInput)
    return () => el.removeEventListener('input', onInput)
  }, [draft])

  useEffect(() => {
    const el = pattern.current
    if (!el) return undefined
    /* The suggestion is the served value; a draft beats it, the empty string
       included -- an emptied prefix saves nothing, and re-offering the
       suggestion on the way back would undo that. */
    el.value = drafts.read(draft).pattern ?? (suggested || '')
    const onInput = (): void => drafts.write(draft, { pattern: el.value })
    const onClick = (e: Event): void => e.stopPropagation()
    const onKeyDown = (e: KeyboardEvent): void => {
      /* Typing in the prefix must not pick the row, and Enter there saves. */
      if (e.key === 'Enter') {
        e.preventDefault()
        onSaveRule()
      }
    }
    el.addEventListener('input', onInput)
    el.addEventListener('click', onClick)
    el.addEventListener('keydown', onKeyDown)
    return () => {
      el.removeEventListener('input', onInput)
      el.removeEventListener('click', onClick)
      el.removeEventListener('keydown', onKeyDown)
    }
  }, [draft, suggested, onSaveRule])

  return (
    <>
      <SheetHead title={words.title} deny={words.deny} onDeny={onDeny} />
      <div className="body">
        <div className="what">{command}</div>
        <input className="note-in" placeholder={words.notePh} aria-label={words.notePh} ref={note} />
        {opts.map((row, i) => (
          <SheetOption key={i} n={i + 1} row={row}>
            {/* The persisted grant's prefix rides inside its own row: an input
                the reader may edit, and an emptied one saves nothing. */}
            {row.rule
              ? <input className="note-in pattern-in" aria-label={words.patternFor}
                  title={words.patternFor} ref={pattern} />
              : null}
          </SheetOption>
        ))}
      </div>
    </>
  )
}
