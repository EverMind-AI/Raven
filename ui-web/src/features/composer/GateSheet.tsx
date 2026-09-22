/* The permission gate's approval sheet: why the agent is asking, what it wants
 * to do, and the three answers -- deny, the broader grant the request can
 * carry (a saved rule when the runtime offered one, the conversation
 * otherwise), allow once. An answer that leaves something to do leaves a line
 * behind: a saved rule and its undo, or the news that the engine never took
 * the answer.
 *
 * The sheet element, the answers and the document key handler belong to
 * features/composer/approve.ts; this renders its children, for the reason
 * src/chrome/SheetRack.tsx gives. The wording arrives as props: the opener
 * reads the catalogue once when the request lands, so a language flip does not
 * re-word a question already on screen.
 */

import { SheetOption } from '../../chrome/SheetRack'
import { SheetHead } from './AskApproveSheet'

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
  readonly cut: string
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
  /* The gate cuts an oversized account down to what a person reads and says so
     here. Answering about a change you can only see part of is the one thing
     the cap must not cause, so the sheet says it was shortened wherever the
     flag is set -- and says it below the evidence, where the reader has just
     run out of it. */
  const cut = evidence.truncated === true ? <div className="cp-ev-cut">{words.cut}</div> : null
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
        {cut}
      </div>
    )
  }
  if (kind === 'mcp.call') {
    return (
      <div className="what cp-ev">
        <div className="cp-ev-path">{str(evidence.server)}.{str(evidence.tool)}</div>
        <pre className="cp-json">{JSON.stringify(evidence.input ?? {}, null, 2)}</pre>
        {cut}
      </div>
    )
  }
  if (kind === 'shell.exec') {
    return <div className="what">{str(evidence.command) || command}{cut}</div>
  }
  return (
    <div className="what cp-ev">
      <pre className="cp-json">{JSON.stringify(evidence.input ?? evidence, null, 2)}</pre>
      {cut}
    </div>
  )
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
}

export interface LandedProps {
  readonly words: LandedWords
  /** Takes back the rule a saved grant wrote. */
  readonly onUndo?: () => void
}

/* What the sheet becomes once answered: one line, and the button that takes
   the answer back where there is one. */
export function LandedSheet({ words, onUndo }: LandedProps): JSX.Element {
  return (
    <div className="cp-land" role="status">
      <span className="cp-land-text">{words.text}</span>
      {onUndo && words.undo ? <button className="cp-undo" onClick={onUndo}>{words.undo}</button> : null}
    </div>
  )
}
