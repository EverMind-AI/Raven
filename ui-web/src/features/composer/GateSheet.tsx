/* The permission gate's approval sheet: why the agent is asking, what it wants
 * to do, and the three answers -- deny, allow always (only when the runtime
 * offered a rule to save), allow once. After an answer the sheet lands: one
 * line saying what happened, with the undo a saved rule earns and the note a
 * refusal invites.
 *
 * The sheet element, the answers and the document key handler belong to
 * features/composer/approve.ts; this renders its children, for the reason
 * src/chrome/SheetRack.tsx gives. The wording arrives as props: the opener
 * reads the catalogue once when the request lands, so a language flip does not
 * re-word a question already on screen.
 */
import { useEffect, useRef } from 'react'

import { SheetOption } from '../../chrome/SheetRack'
import { SheetHead } from './AskApproveSheet'
import { composing } from './store'

import type { SheetOptionRow } from '../../chrome/SheetRack'
import type { JSX } from 'react'

/** The tool's own account of the call, as the engine sent it (raven/contracts/tool.py). */
export type Evidence = Record<string, unknown>

export interface GateWords {
  readonly title: string
  readonly why: string
  readonly deny: string
  readonly created: string
  readonly nodiff: string
}

export interface GateProps {
  readonly kind: string
  readonly evidence: Evidence
  readonly command: string
  readonly words: GateWords
  readonly opts: readonly SheetOptionRow[]
  readonly onDeny: () => void
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '')

const diffClass = (line: string): string =>
  line.startsWith('+') ? 'cp-add' : line.startsWith('-') ? 'cp-del' : line.startsWith('@@') ? 'cp-hunk' : ''

/* What is being judged, by kind: the command verbatim, a path and the diff the
   write would make, an MCP tool and its input -- or, for a tool the page has no
   layout for, the arguments as they are. */
function EvidenceBlock(
  { kind, evidence, command, words }: { kind: string; evidence: Evidence; command: string; words: GateWords },
): JSX.Element {
  if (kind === 'file.write') {
    /* The file header names the path the line above already shows, and a
       two-line change should not spend its room on it. */
    const lines = str(evidence.diff).split('\n').filter((l) => !l.startsWith('--- ') && !l.startsWith('+++ '))
    return (
      <div className="what cp-ev">
        <div className="cp-ev-path">{str(evidence.path)}{evidence.created ? ` · ${words.created}` : ''}</div>
        {str(evidence.diff)
          ? (
            <pre className="cp-diff">
              {lines.map((line, i) => <span key={i} className={diffClass(line)}>{line}{'\n'}</span>)}
            </pre>
          )
          : <div className="cp-ev-none">{words.nodiff}</div>}
      </div>
    )
  }
  if (kind === 'mcp.call') {
    return (
      <div className="what cp-ev">
        <div className="cp-ev-path">{str(evidence.server)}.{str(evidence.tool)}</div>
        <pre className="cp-json">{JSON.stringify(evidence.input ?? {}, null, 2)}</pre>
      </div>
    )
  }
  if (kind === 'shell.exec') return <div className="what">{str(evidence.command) || command}</div>
  return <pre className="what cp-json">{JSON.stringify(evidence.input ?? evidence, null, 2)}</pre>
}

export function GateSheet({ kind, evidence, command, words, opts, onDeny }: GateProps): JSX.Element {
  return (
    <>
      <SheetHead title={words.title} deny={words.deny} onDeny={onDeny} />
      <div className="body">
        <div className="cp-why">{words.why}</div>
        <EvidenceBlock kind={kind} evidence={evidence} command={command} words={words} />
        <div className="cp-acts">
          {opts.map((row, i) => <SheetOption key={i} n={i + 1} row={row} />)}
        </div>
      </div>
    </>
  )
}

export interface LandedWords {
  readonly text: string
  readonly undo?: string
  readonly notePh?: string
}

export interface LandedProps {
  readonly words: LandedWords
  /** Takes back the rule a saved grant wrote. */
  readonly onUndo?: () => void
  /** Sends the sentence typed after a refusal. */
  readonly onNote?: (text: string) => void
  /** Called on every keystroke in the note field, so the sheet's own clock is
      put back: a reader still typing has not finished, and taking the field
      away under them loses the sentence with it. */
  readonly onTyping?: () => void
}

/* What the sheet becomes once answered. The note field is uncontrolled and
   listened to natively, for the reasons ClarifySheet gives: a component owning
   the value re-renders a field the reader is typing into, and the keydown has
   to stop at the input so the page's shortcuts do not read what is typed. */
export function LandedSheet({ words, onUndo, onNote, onTyping }: LandedProps): JSX.Element {
  const field = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    const el = field.current
    if (!el || !onNote) return undefined
    const onKeyDown = (e: KeyboardEvent): void => {
      e.stopPropagation()
      onTyping?.()
      if (composing(e)) return
      if (e.key === 'Enter' && el.value.trim()) onNote(el.value.trim())
    }
    el.addEventListener('keydown', onKeyDown)
    return () => el.removeEventListener('keydown', onKeyDown)
  }, [onNote, onTyping])

  return (
    <div className="cp-land" role="status">
      <span className="cp-land-text">{words.text}</span>
      {onUndo && words.undo ? <button className="cp-undo" onClick={onUndo}>{words.undo}</button> : null}
      {onNote ? <input className="cp-note" placeholder={words.notePh} aria-label={words.notePh} ref={field} /> : null}
    </div>
  )
}
