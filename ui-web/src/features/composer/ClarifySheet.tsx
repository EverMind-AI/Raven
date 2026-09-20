/* The clarify question's interior: the question, the numbered choices and the
 * free-text row the reader answers in.
 *
 * The sheet element itself belongs to features/composer/clarify.ts, which files
 * it with the rack and holds the document key handler -- so this renders its
 * children and not the container. Two attributes React cannot own on a
 * container it did not create: `data-fold`, which changes, is written here;
 * the rest are set with the element.
 *
 * The field is uncontrolled and listened to natively, for the two reasons the
 * composer's own field states (features/composer/mount.tsx): a component owning
 * the value re-renders a text field the reader is typing into, and the keydown
 * has to be stopped at the input itself so the page's shortcuts do not read what
 * is being typed. Its text is read from and written to state/sheetDrafts.ts on
 * every keystroke, because the interior is unmounted while the reader is in
 * another conversation and the answer has to survive that.
 *
 * The wording is the opener's: it reads the catalogue once when the question
 * arrives, so a language flip does not re-word a sheet already on screen -- which
 * is what the imperative builder did.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'

import { SheetOption } from '../../chrome/SheetRack'
import { CHEVRON_DOWN, CROSS, Glyph } from '../../components/Ico'
import * as drafts from '../../state/sheetDrafts'
import { composing } from './store'

import type { JSX } from 'react'

/* What the key handler in the opener needs from the interior: the field, to keep
   digits out of it and to hand it the caret, and the fold, which the number past
   the last choice opens. */
export interface ClarifyControls {
  input: HTMLInputElement | null
  setFold: ((v: boolean) => void) | null
}

export interface ClarifyWords {
  readonly fold: string
  readonly unfold: string
  readonly skip: string
  readonly skipAria: string
  readonly placeholder: string
  readonly submit: string
}

export interface ClarifyProps {
  readonly host: HTMLElement
  readonly ctl: ClarifyControls
  readonly draft: string
  readonly question: string
  readonly choices: readonly string[]
  readonly words: ClarifyWords
  readonly done: (text: string) => void
  readonly skip: () => void
}

export function ClarifySheet(
  { host, ctl, draft, question, choices, words, done, skip }: ClarifyProps,
): JSX.Element {
  const field = useRef<HTMLInputElement | null>(null)
  /* Both start from what the sheet already holds rather than from a fresh
     question's defaults: this tree mounts again every time the reader comes back
     to the conversation, and the fold they left it in is on the host while the
     answer they left in it is in the draft. */
  const [folded, setFolded] = useState(() => host.dataset.fold === 'true')
  const [answerable, setAnswerable] = useState(() => !!drafts.read(draft).text?.trim())

  /* Synchronous, because the key handler's next statement takes the caret and a
     folded sheet's field cannot be focused. */
  const setFold = useCallback((v: boolean): void => {
    host.dataset.fold = String(v)
    flushSync(() => setFolded(v))
  }, [host])

  useLayoutEffect(() => {
    ctl.input = field.current
    ctl.setFold = setFold
  }, [ctl, setFold])

  useEffect(() => {
    const el = field.current
    if (!el) return undefined
    /* The property, not an attribute: page.html's own fields carry no `value`,
       and a defaultValue would put one on this one. */
    el.value = drafts.read(draft).text || ''
    const onInput = (): void => {
      drafts.write(draft, { text: el.value })
      flushSync(() => setAnswerable(!!el.value.trim()))
    }
    const onKeyDown = (e: KeyboardEvent): void => {
      /* Stopped here so the page's own shortcuts do not read what is being typed
         into this field. */
      e.stopPropagation()
      if (composing(e)) return
      if (e.key === 'Enter' && el.value.trim()) done(el.value.trim())
    }
    el.addEventListener('input', onInput)
    el.addEventListener('keydown', onKeyDown)
    return () => {
      el.removeEventListener('input', onInput)
      el.removeEventListener('keydown', onKeyDown)
    }
  }, [draft, done])

  const foldLabel = folded ? words.unfold : words.fold
  return (
    <>
      <div className="hd">
        <div className="q" onClick={() => { if (host.dataset.fold === 'true') setFold(false) }}>{question}</div>
        <button className="ic tipdn" data-tip={foldLabel} aria-label={foldLabel}
          onClick={() => setFold(host.dataset.fold !== 'true')}>
          <Glyph d={CHEVRON_DOWN} cls="cv" />
        </button>
        <button className="ic tipdn" data-tip={words.skip} aria-label={words.skipAria} onClick={skip}>
          <Glyph d={CROSS} />
        </button>
      </div>
      <div className="body">
        {choices.map((c, i) => (
          <SheetOption key={i} n={i + 1} row={{ label: c, run: () => done(c) }} />
        ))}
        <div className="other">
          <span className="n">{choices.length + 1}</span>
          <input placeholder={words.placeholder} ref={field} />
        </div>
      </div>
      <div className="foot">
        <button className="btn" onClick={skip}>{words.skip}</button>
        <button className="btn key" disabled={!answerable}
          onClick={() => {
            const v = field.current?.value.trim()
            if (v) done(v)
          }}>{words.submit}</button>
      </div>
    </>
  )
}
