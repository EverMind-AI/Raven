/* The clarify question's interior: the question, the numbered choices and the
 * free-text row the reader answers in -- one question, or a form the reader
 * steps through when the frame brought a whole batch.
 *
 * The sheet element itself belongs to features/composer/clarify.ts, which files
 * it with the rack, decides how many steps there are and holds the document key
 * handler -- so this renders its children and not the container. Two attributes
 * React cannot own on a container it did not create: `data-fold`, which changes,
 * is written here; the rest are set with the element.
 *
 * Nothing is reported until the form is finished. A step keeps what was picked,
 * what was typed and whether it was skipped, so going back to it shows it as it
 * was left, and the last step's Submit reports every step at once -- but not
 * before every step is answered or skipped: a step the reader came back to and
 * emptied is unanswered however far they had got, and a Skip on the last step
 * lands on such a step instead of reporting. The one exception is the question
 * that is on its own: picking an option answers it, because there is nowhere to
 * go back to and that is what the sheet has always done.
 *
 * The field is uncontrolled and listened to natively, for the two reasons the
 * composer's own field states (features/composer/mount.tsx): a component owning
 * the value re-renders a text field the reader is typing into, and the keydown
 * has to be stopped at the input itself so the page's shortcuts do not read what
 * is being typed. The form is read from and written to state/sheetDrafts.ts on
 * every keystroke and every pick, because the interior is unmounted while the
 * reader is in another conversation and the answers have to survive that.
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

import type { ClarifyBatchEntry } from '../../rpc/notifications'
import type { JSX } from 'react'

/* What the key handler in the opener needs from the interior: the field, to keep
   digits out of it and to hand it the caret, the fold, which the number past
   the last choice opens, and the two the step on screen decides -- each
   answering whether it took the key, because only the interior knows how many
   options this step has and whether there is another one to move to. */
export interface ClarifyControls {
  input: HTMLInputElement | null
  setFold: ((v: boolean) => void) | null
  pick: ((n: number) => boolean) | null
  move: ((delta: number) => boolean) | null
}

/** One question of the form, as the opener resolved it from the frame. */
export interface ClarifyStep {
  readonly question: string
  readonly header?: string
  readonly choices: readonly string[]
  readonly recommended?: string
  readonly multi: boolean
}

export interface ClarifyWords {
  readonly fold: string
  readonly unfold: string
  readonly skip: string
  readonly skipAria: string
  readonly skipped: string
  readonly otherPh: string
  readonly answerPh: string
  readonly submit: string
  readonly back: string
  readonly next: string
  readonly recommended: string
  readonly multiHint: string
  /** One per batch position, because the label says which of them it is. */
  readonly stepAria: readonly string[]
}

export interface ClarifyProps {
  readonly host: HTMLElement
  readonly ctl: ClarifyControls
  readonly draft: string
  readonly steps: readonly ClarifyStep[]
  /* Every question of the batch, for the progress row -- including the ones
     this sheet does not ask, which were answered before it was raised and are
     drawn as done. */
  readonly batch: readonly ClarifyBatchEntry[]
  readonly offset: number
  readonly words: ClarifyWords
  readonly done: (answers: string[]) => void
}

interface Filled {
  picked: string[]
  text: string
  skipped: boolean
}

interface Form {
  step: number
  reached: number
  steps: Filled[]
}

/* What the reader left here, or an empty form. Clamped, because the draft
   outlives nothing but this question and a shorter batch would index past its
   end. */
function load(key: string, n: number): Form {
  const held = drafts.read(key)
  const last = n - 1
  const at = (i: number): Filled => ({
    picked: held.steps?.[i]?.picked ?? [],
    text: held.steps?.[i]?.text ?? '',
    skipped: held.steps?.[i]?.skipped ?? false,
  })
  return {
    step: Math.min(Math.max(held.step ?? 0, 0), last),
    reached: Math.min(Math.max(held.reached ?? 0, 0), last),
    steps: Array.from({ length: n }, (_, i) => at(i)),
  }
}

const edited = (form: Form, i: number, edit: Partial<Filled>): Form => ({
  ...form,
  steps: form.steps.map((s, j) => (j === i ? { ...s, ...edit } : s)),
})

/* A multi-select step answers with everything it holds, the typed row included;
   a single-select one answers with the option, or with the text when nothing is
   picked. Empty means the step has no answer yet, which is what the primary
   button reads. */
const answerOf = (step: ClarifyStep, filled: Filled): string => {
  const text = filled.text.trim()
  if (step.multi) return [...filled.picked, text].filter(Boolean).join(', ')
  return filled.picked[0] ?? text
}

export function ClarifySheet(
  { host, ctl, draft, steps, batch, offset, words, done }: ClarifyProps,
): JSX.Element {
  const field = useRef<HTMLInputElement | null>(null)
  /* Both start from what the sheet already holds rather than from a fresh
     question's defaults: this tree mounts again every time the reader comes back
     to the conversation, and the fold they left it in is on the host while the
     form they left in it is in the draft. */
  const [folded, setFolded] = useState(() => host.dataset.fold === 'true')
  const [form, setForm] = useState<Form>(() => load(draft, steps.length))
  /* The field's listeners are registered once per step and fire long after, so
     what they read has to be the form as it stands rather than the one that was
     current when they were registered. */
  const live = useRef<Form>(form)

  /* Synchronous, because the key handler's next statement takes the caret and a
     folded sheet's field cannot be focused. */
  const setFold = useCallback((v: boolean): void => {
    host.dataset.fold = String(v)
    flushSync(() => setFolded(v))
  }, [host])

  const apply = useCallback((next: Form): void => {
    live.current = next
    drafts.write(draft, next)
    flushSync(() => setForm(next))
  }, [draft])

  const finish = useCallback((f: Form): void => {
    done([
      /* The positions answered before this sheet was raised: the caller aligns
         the report with the batch and drops them. */
      ...Array<string>(offset).fill(''),
      ...steps.map((s, i) => {
        const one = f.steps[i] as Filled
        return one.skipped ? words.skipped : answerOf(s, one)
      }),
    ])
  }, [done, offset, steps, words.skipped])

  const go = useCallback((f: Form, to: number): void => {
    apply({ ...f, step: to, reached: Math.max(f.reached, to) })
  }, [apply])

  /* The two arrays are parallel -- `load` builds one entry per step -- so an
     index good for the questions is good for the answers. */
  const answerAt = useCallback(
    (f: Form, i: number): string => answerOf(steps[i] as ClarifyStep, f.steps[i] as Filled),
    [steps],
  )

  /* What Submit waits for: every step answered or skipped. Next reads only the
     step on screen, and the chips can carry the reader past a step they came
     back to and emptied. */
  const complete = useCallback(
    (f: Form): boolean => f.steps.every((one, i) => one.skipped || answerAt(f, i) !== ''),
    [answerAt],
  )

  /* Next and Submit are one control: on the last step it reports the form as
     it stands, everywhere else it moves on from an answered step. */
  const forward = useCallback((): void => {
    const f = live.current
    const i = f.step
    if (i === steps.length - 1) {
      if (complete(f)) finish(f)
      return
    }
    if (!answerAt(f, i)) return
    go(edited(f, i, { skipped: false }), i + 1)
  }, [answerAt, complete, finish, go, steps.length])

  /* Skipping is an answer, not a drop: the engine is waiting either way, and
     what it hears is this sheet's own wording for "no answer". Skipping the
     last step reports the form, unless a step before it is still unanswered,
     in which case the reader lands on that step. */
  const skipStep = useCallback((): void => {
    const f = live.current
    const i = f.step
    const next = edited(f, i, { skipped: true })
    if (i !== steps.length - 1) go(next, i + 1)
    else if (complete(next)) finish(next)
    else go(next, next.steps.findIndex((one, j) => !one.skipped && !answerAt(next, j)))
  }, [answerAt, complete, finish, go, steps.length])

  const choose = useCallback((label: string): void => {
    const f = live.current
    const i = f.step
    const one = f.steps[i] as Filled
    const here = steps[i] as ClarifyStep
    if (here.multi) {
      /* Held in the options' order rather than in the order they were clicked:
         the answer is read as a list, and un-picking one and picking it again
         would otherwise shuffle it. */
      const picked = one.picked.includes(label)
        ? one.picked.filter((p) => p !== label)
        : here.choices.filter((c) => c === label || one.picked.includes(c))
      apply(edited(f, i, { picked, skipped: false }))
      return
    }
    const next = edited(f, i, { picked: [label], skipped: false })
    /* A question asked on its own is answered by the pick -- there is nothing
       to come back for. A step of a form is not: the reader may still change
       it, so the pick carries them forward and the last one waits for Submit. */
    if (steps.length === 1) finish(next)
    else if (i === steps.length - 1) apply(next)
    else go(next, i + 1)
  }, [apply, finish, go, steps])

  const pick = useCallback((n: number): boolean => {
    const one = steps[live.current.step] as ClarifyStep
    if (n <= one.choices.length) { choose(one.choices[n - 1] as string); return true }
    if (n === one.choices.length + 1) { setFold(false); field.current?.focus(); return true }
    return false
  }, [choose, setFold, steps])

  const move = useCallback((delta: number): boolean => {
    const f = live.current
    if (delta < 0) {
      if (f.step === 0) return false
      apply({ ...f, step: f.step - 1 })
      return true
    }
    /* Forward only while there is a step to move to: reporting the form is a
       button the reader presses on purpose, not somewhere an arrow key lands. */
    if (f.step === steps.length - 1) return false
    if (!answerAt(f, f.step)) return false
    forward()
    return true
  }, [answerAt, apply, forward, steps.length])

  useLayoutEffect(() => {
    ctl.input = field.current
    ctl.setFold = setFold
    ctl.pick = pick
    ctl.move = move
  }, [ctl, move, pick, setFold])

  const at = form.step
  useEffect(() => {
    const el = field.current
    if (!el) return undefined
    /* The property, not an attribute: page.html's own fields carry no `value`,
       and a defaultValue would put one on this one. Re-read when the step
       changes: one row serves every question, so it carries whichever answer
       is on screen. */
    el.value = live.current.steps[at]?.text || ''
    const onInput = (): void => {
      const f = live.current
      const i = f.step
      /* Typing into a single-select step IS choosing "other", so it drops the
         option that was picked. */
      const kept = (steps[i] as ClarifyStep).multi || !el.value.trim()
      apply(edited(f, i, { picked: kept ? (f.steps[i] as Filled).picked : [], skipped: false, text: el.value }))
    }
    const onKeyDown = (e: KeyboardEvent): void => {
      /* Stopped here so the page's own shortcuts do not read what is being typed
         into this field. */
      e.stopPropagation()
      if (composing(e)) return
      if (e.key === 'Enter' && el.value.trim()) forward()
    }
    el.addEventListener('input', onInput)
    el.addEventListener('keydown', onKeyDown)
    return () => {
      el.removeEventListener('input', onInput)
      el.removeEventListener('keydown', onKeyDown)
    }
  }, [apply, at, forward, steps])

  const step = steps[at] as ClarifyStep
  const filled = form.steps[at] as Filled
  const foldLabel = folded ? words.unfold : words.fold
  const rowClass = (label: string): string =>
    `${step.multi ? 'cp-multi' : ''}${filled.picked.includes(label) ? ' cp-on' : ''}`.trim()
  return (
    <>
      <div className="hd">
        {batch.length > 1 ? (
          <div className="cp-steps">
            {batch.map((entry, p) => {
              const i = p - offset
              const one = form.steps[i]
              const answered = p < offset || (!!one && (one.skipped || answerAt(form, i) !== ''))
              return (
                <button key={p}
                  className={`cp-step${p === offset + at ? ' cp-on' : ''}${answered ? ' cp-done' : ''}${one?.skipped ? ' cp-skip' : ''}`}
                  aria-label={words.stepAria[p]} aria-current={p === offset + at ? 'step' : undefined}
                  disabled={p < offset || i > form.reached}
                  onClick={() => apply({ ...form, step: i })}>{entry.header || String(p + 1)}</button>
              )
            })}
          </div>
        ) : null}
        <div className="q" onClick={() => { if (host.dataset.fold === 'true') setFold(false) }}>{step.question}</div>
        <button className="ic tipdn" data-tip={foldLabel} aria-label={foldLabel}
          onClick={() => setFold(host.dataset.fold !== 'true')}>
          <Glyph d={CHEVRON_DOWN} cls="cv" />
        </button>
        <button className="ic tipdn" data-tip={words.skip} aria-label={words.skipAria} onClick={skipStep}>
          <Glyph d={CROSS} />
        </button>
      </div>
      <div className="body">
        {step.multi ? <div className="cp-hint">{words.multiHint}</div> : null}
        {step.choices.map((c, i) => (
          <SheetOption key={i} n={i + 1} cls={rowClass(c)} row={{ label: c, run: () => choose(c) }}>
            {c === step.recommended ? <span className="cp-rec">{words.recommended}</span> : null}
            {step.multi ? <span className="cp-box" aria-hidden="true" /> : null}
          </SheetOption>
        ))}
        <div className="other">
          <span className="n">{step.choices.length + 1}</span>
          <input placeholder={step.choices.length ? words.otherPh : words.answerPh} ref={field} />
        </div>
      </div>
      <div className="foot">
        {at > 0 ? <button className="btn cp-back" onClick={() => { move(-1) }}>{words.back}</button> : null}
        <button className="btn" onClick={skipStep}>{words.skip}</button>
        <button className="btn key" onClick={forward}
          disabled={at === steps.length - 1 ? !complete(form) : !answerOf(step, filled)}>
          {at === steps.length - 1 ? words.submit : words.next}
        </button>
      </div>
    </>
  )
}
