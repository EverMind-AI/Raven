/* What a model can do, drawn as icons, for every list that offers models.
 *
 * The registry publishes four display facts (see `providers/registry_data.py`)
 * and this draws two of them. Input and output modalities are deliberately not
 * a second icon row: every modality beyond text is already a capability --
 * image in is `image-recognition`, image out is `image-generation` -- so a row
 * of both says everything twice and leaves the reader deciding which pair
 * disagrees. They ride in the row's title instead, where "reads text, image"
 * is a sentence rather than a rebus.
 *
 * An absent tag means the registry publishes nothing, never that the model
 * cannot: no icon is the honest rendering of "unknown", and a crossed-out one
 * would state a fact nobody has.
 */

import { t } from './bridge'

import type { JSX } from 'react'

/* Drawn in this order wherever they appear together, so two models carrying
   the same tags produce the same row and the eye can compare down a column. */
const ORDER = [
  'reasoning',
  'function-call',
  'structured-output',
  'image-recognition',
  'audio-recognition',
  'video-recognition',
  'file-input',
  'image-generation',
  'audio-generation',
  'video-generation',
  'embedding',
  'rerank',
  'computer-use',
] as const

const LABELS: Record<string, string> = {
  'reasoning': 'gui.model.cap.reasoning',
  'function-call': 'gui.model.cap.function_call',
  'structured-output': 'gui.model.cap.structured_output',
  'image-recognition': 'gui.model.cap.image_in',
  'audio-recognition': 'gui.model.cap.audio_in',
  'video-recognition': 'gui.model.cap.video_in',
  'file-input': 'gui.model.cap.file_in',
  'image-generation': 'gui.model.cap.image_out',
  'audio-generation': 'gui.model.cap.audio_out',
  'video-generation': 'gui.model.cap.video_out',
  'embedding': 'gui.model.cap.embedding',
  'rerank': 'gui.model.cap.rerank',
  'computer-use': 'gui.model.cap.computer_use',
}

/* One 16x16 stroke drawing per capability, defined once per page and referenced
   by `use`. A long provider list draws five of these per row, and inlining the
   paths put a thousand path nodes in a popover that has to open in one frame. */
const GLYPHS: Record<string, JSX.Element> = {
  /* Not a capability -- the add-model drawer's type row names a kind, and
     `text` is the one kind with no capability of the same shape. It is in the
     sprite because it is drawn the same way and belongs in the same table. */
  'text': (
    <>
      <path d="M3.4 4.2V3h9.2v1.2" />
      <path d="M8 3v10M5.9 13h4.2" />
    </>
  ),
  'reasoning': (
    <>
      <path d="M8 2.2l1.15 3.15L12.3 6.5 9.15 7.65 8 10.8 6.85 7.65 3.7 6.5l3.15-1.15z" />
      <path d="M12.2 10.4l.5 1.3 1.3.5-1.3.5-.5 1.3-.5-1.3-1.3-.5 1.3-.5z" />
    </>
  ),
  'function-call': (
    <>
      <path d="M10.4 2.3a3.4 3.4 0 0 0-3.2 4.4l-4.5 4.5a1.6 1.6 0 0 0 2.3 2.3l4.5-4.5a3.4 3.4 0 0 0 4.4-3.2l-2.2 2.2-2-.5-.5-2z" />
    </>
  ),
  'structured-output': (
    <>
      <rect x="2.6" y="3" width="10.8" height="10" rx="2" />
      <path d="M5.2 6.2h5.6M5.2 8.5h5.6M5.2 10.8h3.2" />
    </>
  ),
  'image-recognition': (
    <>
      <rect x="2.4" y="3.4" width="11.2" height="9.2" rx="1.8" />
      <path d="M2.9 10.6l2.7-2.5 2.3 2.1 2.1-1.9 2.9 2.6" />
      <circle cx="10.4" cy="6.2" r="1" />
    </>
  ),
  'audio-recognition': (
    <>
      <rect x="6.2" y="2.3" width="3.6" height="7" rx="1.8" />
      <path d="M4 7.6a4 4 0 0 0 8 0M8 11.6v2.1" />
    </>
  ),
  'video-recognition': (
    <>
      <rect x="2.4" y="3.6" width="11.2" height="8.8" rx="1.8" />
      <path d="M6.7 6.4l3.5 1.6-3.5 1.6z" />
    </>
  ),
  'file-input': (
    <>
      <path d="M4.2 2.6h4.4l3.2 3.2v7.6H4.2z" />
      <path d="M8.6 2.6v3.2h3.2" />
    </>
  ),
  'image-generation': (
    <>
      <rect x="2.4" y="3.4" width="8.6" height="8.6" rx="1.8" />
      <path d="M2.9 10.2l2.3-2.2 2 1.8 1.6-1.5 2.2 2" />
      <path d="M12.4 2l.6 1.6 1.6.6-1.6.6-.6 1.6-.6-1.6-1.6-.6 1.6-.6z" />
    </>
  ),
  'audio-generation': (
    <>
      <path d="M3 6.4h2.2L8.4 4v8L5.2 9.6H3z" />
      <path d="M10.6 6a3 3 0 0 1 0 4M12.6 4.4a5.4 5.4 0 0 1 0 7.2" />
    </>
  ),
  'video-generation': (
    <>
      <rect x="2.4" y="3.8" width="8.6" height="8.4" rx="1.8" />
      <path d="M6 6.6l2.6 1.4L6 9.4z" />
      <path d="M12.6 2.2l.55 1.5 1.5.55-1.5.55-.55 1.5-.55-1.5-1.5-.55 1.5-.55z" />
    </>
  ),
  'embedding': (
    <>
      <path d="M3 13L13 3" />
      <path d="M9.4 3H13v3.6" />
      <circle cx="4.2" cy="6" r="1" />
      <circle cx="7" cy="11.4" r="1" />
    </>
  ),
  'rerank': (
    <>
      <path d="M5 12.8V3.6M5 3.6L3 5.8M5 3.6l2 2.2" />
      <path d="M11 3.2v9.2M11 12.4l2-2.2M11 12.4l-2-2.2" />
    </>
  ),
  'computer-use': (
    <>
      <rect x="2.2" y="3.2" width="11.6" height="7.6" rx="1.6" />
      <path d="M6.4 13.2h3.2M8 10.8v2.4" />
    </>
  ),
}

/* Injected once by whichever list draws first; a second copy would repeat every
   id in the document, and the first definition is the one `use` resolves. */
export function ModelTagDefs(): JSX.Element {
  return (
    <svg className="model-tag-defs" aria-hidden="true" focusable="false">
      <defs>
        {Object.keys(GLYPHS).map((name) => (
          <symbol key={name} id={`mtag-${name}`} viewBox="0 0 16 16">
            {GLYPHS[name]}
          </symbol>
        ))}
      </defs>
    </svg>
  )
}

/* One glyph out of the same sprite, for a surface that labels it itself -- the
   add-model drawer, whose toggles carry the word beside the drawing. Same
   table as the icon row on purpose: a person who ticks a brain and then sees a
   sparkle has to work out that they are the same thing. */
export function TagGlyph({ name }: { name: string }): JSX.Element {
  return (
    <svg className="model-tag" aria-hidden="true" focusable="false">
      <use href={`#mtag-${name}`} />
    </svg>
  )
}

/* 128000 -> 128K, 1000000 -> 1M. A window is read as a size, not counted: the
   exact token figure is noise on every row of a list and the rounded one is
   what a person compares. */
export function contextLabel(tokens: number): string {
  if (tokens >= 1_000_000) {
    const millions = tokens / 1_000_000
    return `${millions >= 10 || Number.isInteger(millions) ? Math.round(millions) : millions.toFixed(1)}M`
  }
  if (tokens >= 1000) return `${Math.round(tokens / 1000)}K`
  return String(tokens)
}

export interface ModelTagFacts {
  capabilities?: string[]
  input_modalities?: string[]
  output_modalities?: string[]
  context_window?: number
}

function modalitySentence(facts: ModelTagFacts): string {
  const name = (m: string): string => t(`gui.model.modality.${m}`, undefined, m)
  const parts: string[] = []
  if (facts.input_modalities?.length) {
    parts.push(t('gui.model.reads', { list: facts.input_modalities.map(name).join(', ') }))
  }
  if (facts.output_modalities?.length) {
    parts.push(t('gui.model.writes', { list: facts.output_modalities.map(name).join(', ') }))
  }
  return parts.join(' · ')
}

export function ModelTags({ facts }: { facts: ModelTagFacts | undefined }): JSX.Element | null {
  if (!facts) return null
  const drawn = ORDER.filter((name) => facts.capabilities?.includes(name))
  const window = facts.context_window
  if (!drawn.length && !window) return null
  return (
    <span className="model-tags" title={modalitySentence(facts) || undefined}>
      {drawn.map((name) => {
        const label = t(LABELS[name] as string, undefined, name)
        return (
          <svg key={name} className="model-tag" role="img" aria-label={label}>
            <title>{label}</title>
            <use href={`#mtag-${name}`} />
          </svg>
        )
      })}
      {window ? (
        <span className="model-window" title={t('gui.model.context_window', { tokens: String(window) })}>
          {contextLabel(window)}
        </span>
      ) : null}
    </span>
  )
}
