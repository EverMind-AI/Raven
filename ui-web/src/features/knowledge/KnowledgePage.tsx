import { useEffect, useRef, useState } from 'react'
import { useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { assetStamp } from '../../lib/assetStamp'
import { md } from '../../lib/prose'
import * as lang from '../../state/lang'
import * as menu from '../../state/menu'
import { FolderTree } from './FolderTree'
import { MoveSheet } from './MoveSheet'
import { DEFAULTS } from './source'
import * as store from './store'

import type { KnowledgeState, Tab } from './store'
import type { KbBase, KbChunk, KbDoc, KbFolder, KbModel, KbPage, KbRegion } from './types'
import type React from 'react'
import type { JSX } from 'react'
import './styles.css'

/* The knowledge bases a reader has made, as a grid of cards.
 *
 * One card a base, and everything a card says is a fact the registry holds:
 * its name, the line the reader wrote about it, how many documents are in it,
 * and their two marks. The star sits in the corner because it is one click and
 * the commonest one; everything that is not one click is behind the menu
 * beside it.
 *
 * Three tabs, and only three. `all` and `starred` order by name, which is how
 * a reader scanning for one reads; `recent` orders by when the base was made.
 * A pinned base leads every one of them -- the pin is about order, and a
 * reader who set it meant it wherever they are looking.
 */

function StarGlyph({ on }: { on: boolean }): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill={on ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
      <path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.7l5.9-.9z" strokeLinejoin="round" />
    </svg>
  )
}

function DotsGlyph(): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="5" cy="12" r="1.7" />
      <circle cx="12" cy="12" r="1.7" />
      <circle cx="19" cy="12" r="1.7" />
    </svg>
  )
}

function BookGlyph(): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
      <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5z" />
      <path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5z" />
    </svg>
  )
}

const TABS: ReadonlyArray<{ id: Tab; key: string }> = [
  { id: 'all', key: 'gui.knowledge.tab_all' },
  { id: 'starred', key: 'gui.knowledge.tab_starred' },
  { id: 'recent', key: 'gui.knowledge.tab_recent' },
]

/* What a card's dots raise, in the page's own menu host (state/menu.ts).

   Not a div inside the card, which is what this was: the grid scrolls, so an
   absolutely positioned menu inside it is clipped at the grid's edge and the
   last row of a card near the bottom is simply not there. The shared host is
   at the body, clamps itself to the viewport, and comes down on a pointer
   anywhere else -- three things a card cannot do for itself.

   Delete is two presses. `pick` closes the menu before it runs the row, so the
   first press raises the menu again with the row reworded; the second means
   it. A base takes its documents and its vectors with it and there is no undo
   behind it. */
function raise(base: KbBase, at: DOMRect, confirming = false): void {
  menu.show(at.left, at.bottom + 4, [
    { label: t(base.pinned ? 'gui.knowledge.unpin' : 'gui.knowledge.pin'), fn: () => store.togglePin(base) },
    { label: t('gui.knowledge.settings'), fn: () => store.openSettings(base) },
    {
      label: t(confirming ? 'gui.knowledge.delete_sure' : 'gui.knowledge.delete'),
      bad: true,
      fn: () => (confirming ? store.remove(base) : raise(base, at, true)),
    },
  ])
}

function Card({ base }: { base: KbBase }): JSX.Element {
  return (
    <article
      className={`knowledge-card${base.pinned ? ' knowledge-pinned' : ''}`}
      /* The card is the way into the base. The two controls in its corner stop
         the click, so pressing the star does not also open the documents. */
      onClick={() => store.openBase(base)}
    >
      <div className="knowledge-head">
        <BookGlyph />
        <h3 title={base.name}>{base.name}</h3>
        <button
          className={`knowledge-star${base.starred ? ' knowledge-on' : ''}`}
          aria-pressed={base.starred}
          aria-label={t(base.starred ? 'gui.knowledge.unstar' : 'gui.knowledge.star')}
          title={t(base.starred ? 'gui.knowledge.unstar' : 'gui.knowledge.star')}
          onClick={(e) => {
            e.stopPropagation()
            store.toggleStar(base)
          }}
        >
          <StarGlyph on={base.starred} />
        </button>
        <button
          className="knowledge-dots"
          aria-haspopup="menu"
          aria-label={t('gui.knowledge.more')}
          onClick={(e) => {
            e.stopPropagation()
            raise(base, e.currentTarget.getBoundingClientRect())
          }}
        >
          <DotsGlyph />
        </button>
      </div>
      {/* The reader's own line, or a note that there is not one -- an empty
          space here reads as a card that failed to load. */}
      <p className={`knowledge-desc${base.description ? '' : ' knowledge-none'}`}>
        {base.description || t('gui.knowledge.no_description')}
      </p>
      <footer className="knowledge-foot">
        <span className="knowledge-count">{t('gui.knowledge.documents_n', { n: base.documents })}</span>
        {/* A base with no model cannot be searched at all, which is worth
            saying on the card rather than only inside it. */}
        {!base.embedding_model && <span className="knowledge-warn">{t('gui.knowledge.no_model')}</span>}
      </footer>
    </article>
  )
}

/* Create, and rename. One sheet because they ask for the same two things; what
   differs is whether there is a base behind it yet. */
/* One labelled row of the settings panel: what it is, and what it means.

   The help line is not a tooltip. Every one of these changes what a search
   over this base will answer, and a reader deciding between 2048 and 512 needs
   the sentence in front of them, not behind a hover. */
function Field({ label, help, children }: { label: string; help: string; children: JSX.Element }): JSX.Element {
  return (
    <div className="knowledge-field">
      <div className="knowledge-fieldhd">
        <b>{label}</b>
        {children}
      </div>
      <p className="knowledge-help">{help}</p>
    </div>
  )
}

/* What the base is tuned to.

   Everything here except the model is a number the chunker or the search
   reads. The model is shown and not offered: changing it means every document
   cut and embedded again, which is not something a panel should do by being
   saved.
*/
function SettingsPanel({ base, busy, models }: { base: KbBase; busy: boolean; models: KbModel[] | null }): JSX.Element {
  const [topK, setTopK] = useState(base.top_k)
  const [smart, setSmart] = useState(base.smart_chunking)
  const [sep, setSep] = useState(base.separator)
  const [size, setSize] = useState(String(base.chunk_size))
  const [lap, setLap] = useState(String(base.chunk_overlap))
  const [table, setTable] = useState(String(base.table_context_size))
  const [figure, setFigure] = useState(String(base.image_context_size))
  const [model, setModel] = useState(base.embedding_model)

  /* A base with nothing indexed has no vectors to lose, so the model is a
     choice rather than a fact. Once it holds pieces, moving it means the
     collection made again at the new width and every document re-cut and
     re-embedded -- a rebuild, not a setting, and not something a panel should
     do by being saved. Keyed on pieces and not on documents: a base whose
     uploads all failed holds nothing, whatever the file count says. */
  const movable = base.chunks === 0
  /* Grouped by who serves them, because a model id is only half an address:
     the same name under two accounts is two different endpoints, and a flat
     list of ids would make the reader guess which one they are picking. */
  const byProvider = new Map<string, { name: string; models: KbModel[] }>()
  for (const row of models ?? []) {
    const group = byProvider.get(row.provider) ?? { name: row.providerName, models: [] }
    group.models.push(row)
    byProvider.set(row.provider, group)
  }
  /* The one it is on, where the catalogue does not carry it: a base built on a
     model since removed still has to be able to say what it is on, and a
     picker that dropped it would silently offer to move the base off it. */
  const known = (models ?? []).some((row) => row.id === model)
  const orphan = model && !known ? model : ''

  const restore = (): void => {
    setTopK(DEFAULTS.top_k)
    setSmart(DEFAULTS.smart_chunking)
    setSep(DEFAULTS.separator)
    setSize(String(DEFAULTS.chunk_size))
    setLap(String(DEFAULTS.chunk_overlap))
    setTable(String(DEFAULTS.table_context_size))
    setFigure(String(DEFAULTS.image_context_size))
  }

  /* Both are typed, so they can be mid-edit and empty or nonsense. An overlap
     as big as the piece would make every piece the one before it. */
  const numbers =
    Number(size) > 0 &&
    Number(lap) >= 0 &&
    Number(lap) < Number(size) &&
    Number(table) >= 0 &&
    Number(figure) >= 0

  const save = (): void => {
    void store.write(base, {
      top_k: topK,
      smart_chunking: smart,
      separator: sep,
      chunk_size: Number(size),
      chunk_overlap: Number(lap),
      table_context_size: Number(table),
      image_context_size: Number(figure),
      /* Sent only where it moved: this key is a rebuild, and writing the same
         model back would queue every document for nothing. */
      /* Sent only where it moved: this key is a rebuild, and writing the same
         model back would queue every document for nothing. The account comes
         off the row the reader picked, because the same model id under two
         accounts is two endpoints. */
      ...(movable && model !== base.embedding_model
        ? {
            embedding_model: model,
            embedding_provider: (models ?? []).find((row) => row.id === model)?.provider ?? '',
          }
        : {}),
    })
    store.closeSheet()
  }

  return (
    <div className="knowledge-scrim" onClick={() => store.closeSheet()}>
      <div className="knowledge-sheet knowledge-sets" onClick={(e) => e.stopPropagation()}>
        <h2>{t('gui.knowledge.set_title')}</h2>

        <Field
          label={t('gui.knowledge.set_embed')}
          help={t(movable ? 'gui.knowledge.set_embed_free' : 'gui.knowledge.set_embed_help')}
        >
          {movable ? (
            <select
              className="knowledge-small knowledge-wide"
              value={model}
              aria-label={t('gui.knowledge.set_embed')}
              onChange={(e) => setModel(e.currentTarget.value)}
            >
              {/* Off first: it is the one choice that is not a model, and a
                  reader looking for it should not have to read the catalogue
                  to find out it is there. */}
              <option value="">{t('gui.knowledge.set_embed_none')}</option>
              {orphan && <option value={orphan}>{orphan}</option>}
              {[...byProvider].map(([slug, group]) => (
                <optgroup key={slug} label={group.name}>
                  {group.models.map((row) => (
                    <option key={`${slug}/${row.id}`} value={row.id}>
                      {row.label}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          ) : (
            <span className="knowledge-fixed">
              {base.embedding_model || t('gui.knowledge.set_embed_off')}
            </span>
          )}
        </Field>

        <Field label={t('gui.knowledge.set_topk')} help={t('gui.knowledge.set_topk_help')}>
          <span className="knowledge-slide">
            <input
              type="range"
              min={1}
              max={50}
              value={topK}
              aria-label={t('gui.knowledge.set_topk')}
              onChange={(e) => setTopK(Number(e.currentTarget.value))}
            />
            <b>{topK}</b>
          </span>
        </Field>

        <Field label={t('gui.knowledge.set_smart')} help={t('gui.knowledge.set_smart_help')}>
          <button
            className="knowledge-chunkon"
            role="switch"
            aria-checked={smart}
            aria-label={t('gui.knowledge.set_smart')}
            onClick={() => setSmart((on) => !on)}
          >
            <span className="knowledge-knob" />
          </button>
        </Field>

        <Field label={t('gui.knowledge.set_sep')} help={t('gui.knowledge.set_sep_help')}>
          {/* Escaped on the way in and out: the separator a reader means is two
              newlines, and a one-line field cannot hold those as themselves. */}
          <input
            className="knowledge-small"
            value={sep.replace(/\n/g, '\\n').replace(/\t/g, '\\t')}
            disabled={smart}
            aria-label={t('gui.knowledge.set_sep')}
            onChange={(e) => setSep(e.currentTarget.value.replace(/\\n/g, '\n').replace(/\\t/g, '\t'))}
          />
        </Field>

        <Field label={t('gui.knowledge.set_size')} help={t('gui.knowledge.set_size_help')}>
          <span className="knowledge-unit">
            <input
              className="knowledge-small"
              type="number"
              min={64}
              max={8192}
              value={size}
              aria-label={t('gui.knowledge.set_size')}
              onChange={(e) => setSize(e.currentTarget.value)}
            />
            <span>{t('gui.knowledge.set_tokens')}</span>
          </span>
        </Field>

        <Field label={t('gui.knowledge.set_lap')} help={t('gui.knowledge.set_lap_help')}>
          <span className="knowledge-unit">
            <input
              className="knowledge-small"
              type="number"
              min={0}
              max={8192}
              value={lap}
              aria-label={t('gui.knowledge.set_lap')}
              onChange={(e) => setLap(e.currentTarget.value)}
            />
            <span>{t('gui.knowledge.set_tokens')}</span>
          </span>
        </Field>

        <Field label={t('gui.knowledge.set_table')} help={t('gui.knowledge.set_table_help')}>
          <span className="knowledge-unit">
            <input
              className="knowledge-small"
              type="number"
              min={0}
              max={2048}
              value={table}
              aria-label={t('gui.knowledge.set_table')}
              onChange={(e) => setTable(e.currentTarget.value)}
            />
            <span>{t('gui.knowledge.set_tokens')}</span>
          </span>
        </Field>

        <Field label={t('gui.knowledge.set_figure')} help={t('gui.knowledge.set_figure_help')}>
          <span className="knowledge-unit">
            <input
              className="knowledge-small"
              type="number"
              min={0}
              max={2048}
              value={figure}
              aria-label={t('gui.knowledge.set_figure')}
              onChange={(e) => setFigure(e.currentTarget.value)}
            />
            <span>{t('gui.knowledge.set_tokens')}</span>
          </span>
        </Field>

        {/* Said here because it is the question a reader has the moment they
            move these. */}
        <p className="knowledge-help">{t('gui.knowledge.set_newonly')}</p>
        {!numbers && <p className="knowledge-err">{t('gui.knowledge.set_range')}</p>}

        <div className="knowledge-actions">
          <button className="mini ghost knowledge-restore" onClick={restore}>
            {t('gui.knowledge.set_restore')}
          </button>
          <button className="mini ghost" onClick={() => store.closeSheet()}>
            {t('gui.cancel')}
          </button>
          <button className="mini" disabled={busy || !numbers} onClick={save}>
            {t('gui.save')}
          </button>
        </div>
      </div>
    </div>
  )
}

function Sheet({ base }: { base: KbBase | null }): JSX.Element {
  const [name, setName] = useState(base?.name ?? '')
  const [description, setDescription] = useState(base?.description ?? '')
  const field = useRef<HTMLInputElement>(null)

  useEffect(() => {
    field.current?.focus()
  }, [])

  const save = (): void => {
    if (base) void store.write(base, { name: name.trim(), description: description.trim() })
    else void store.create(name, description)
    if (base) store.closeSheet()
  }

  return (
    <div className="knowledge-scrim" onClick={() => store.closeSheet()}>
      <div className="knowledge-sheet" onClick={(e) => e.stopPropagation()}>
        <h2>{t(base ? 'gui.knowledge.settings' : 'gui.knowledge.create')}</h2>
        <label>
          <span>{t('gui.knowledge.name')}</span>
          <input
            ref={field}
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
            onKeyDown={(e) => e.key === 'Enter' && save()}
          />
        </label>
        <label>
          <span>{t('gui.knowledge.description')}</span>
          <textarea value={description} onChange={(e) => setDescription(e.currentTarget.value)} rows={3} />
        </label>
        <div className="knowledge-actions">
          <button className="mini ghost" onClick={() => store.closeSheet()}>
            {t('gui.cancel')}
          </button>
          <button className="mini" disabled={!name.trim()} onClick={save}>
            {t(base ? 'gui.save' : 'gui.knowledge.create')}
          </button>
        </div>
      </div>
    </div>
  )
}

/* The path across the top of a base, and the switch in the middle of it.

   The base's name is a button because it is the one part of the path a reader
   can change: it raises the page's own menu with every base on it and the
   current one ticked, so switching is one press from inside the base rather
   than a trip back to the grid. */
function Path({ base, bases }: { base: KbBase; bases: KbBase[] }): JSX.Element {
  const switchTo = (at: DOMRect): void => {
    menu.show(
      at.left,
      at.bottom + 4,
      bases.map((row) => ({
        label: row.name,
        on: row.id === base.id,
        fn: () => store.openBase(row),
      })),
    )
  }
  return (
    <nav className="knowledge-path" aria-label={t('gui.knowledge.title')}>
      <button className="knowledge-crumb" onClick={() => store.closeBase()}>
        {t('gui.knowledge.title')}
      </button>
      <span className="knowledge-sep" aria-hidden="true">
        /
      </span>
      <button
        className="knowledge-crumb knowledge-switch"
        aria-haspopup="menu"
        onClick={(e) => switchTo(e.currentTarget.getBoundingClientRect())}
      >
        {base.name}
        <span className="knowledge-caret" aria-hidden="true">
          &#9662;
        </span>
      </button>
      <span className="knowledge-sep" aria-hidden="true">
        /
      </span>
      <span className="knowledge-here">{t('gui.knowledge.documents')}</span>
    </nav>
  )
}

/** How big a file is, in the unit a reader reads it in. */
function sizeOf(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`
  return `${bytes} B`
}

/** When it last changed, to the minute. Longer is noise in a column. */
function whenOf(iso: string): string {
  if (!iso) return ''
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`
}

/* The formats that have a drawing of their own, from RAGFlow's set (Apache-2.0;
   see NOTICES.md). A page with the format's letters on a coloured band -- the
   same object for every file, so a column of them reads as one column.

   Only the ones this tree can actually ingest were copied: an icon for a
   format no parser here accepts is a drawing nobody can ever see. The few
   Raven takes that RAGFlow has no icon for fall through to the drawing
   below. */
const DRAWN = new Set([
  'csv', 'doc', 'docx', 'gif', 'html', 'jpeg', 'jpg', 'json', 'md',
  'pdf', 'png', 'ppt', 'pptx', 'tiff', 'txt', 'xls', 'xlsx', 'xml',
])

/* Spellings of one format. `markdown` and `md` are the same file to every
   parser here, and so are `htm`/`html` and `tif`/`tiff`. */
const SAME: Readonly<Record<string, string>> = {
  htm: 'html',
  markdown: 'md',
  mdx: 'md',
  tif: 'tiff',
  yml: 'txt',
  yaml: 'txt',
  rst: 'txt',
}

/** The suffix a document was uploaded under, lowercased and without the dot. */
function suffixOf(source: string): string {
  const dot = source.lastIndexOf('.')
  return dot > 0 ? source.slice(dot + 1).toLowerCase() : ''
}

/** Which drawing a document gets, or empty for the fallback. */
export function iconOf(source: string): string {
  const suffix = suffixOf(source)
  const named = SAME[suffix] ?? suffix
  return DRAWN.has(named) ? named : ''
}

/* A file's mark. The drawing where there is one, and a page with the suffix
   written on it where there is not -- same shape, so a list of mixed formats
   stays one column of one object. */
function FileGlyph({ doc }: { doc: KbDoc }): JSX.Element {
  const icon = iconOf(doc.source)
  if (icon) {
    return <img className="knowledge-file" src={`assets/file-icon/${icon}.svg${assetStamp()}`} alt="" aria-hidden="true" />
  }
  return (
    <span className="knowledge-file knowledge-plain" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
        <path d="M14 3H7a1.6 1.6 0 0 0-1.6 1.6v14.8A1.6 1.6 0 0 0 7 21h10a1.6 1.6 0 0 0 1.6-1.6V7.6z" />
        <path d="M14 3v4.6h4.6" />
      </svg>
      <span className="knowledge-filemark">{suffixOf(doc.source).slice(0, 4).toUpperCase() || 'FILE'}</span>
    </span>
  )
}

/* What a document's dots raise.

   Three rows, not the seven a fuller product offers. Download is the file
   route this page already serves from; Rebuild is `documents.index`, which
   cuts and embeds it again; Delete is the one that needs asking twice. The
   rest of that menu -- move to a folder, batch manage, view a trace -- names
   things this tree does not have, and a menu row that cannot do what it says
   is worse than one that is not there. */
function raiseDoc(doc: KbDoc, at: DOMRect, confirming = false): void {
  menu.show(at.left, at.bottom + 4, [
    { label: t('gui.knowledge.download'), fn: () => window.open(store.fileUrl(doc.id), '_blank', 'noopener') },
    { label: t('gui.knowledge.rebuild'), fn: () => void store.reindex(doc) },
    { label: t('gui.knowledge.move'), fn: () => store.openMove(doc) },
    '-',
    {
      label: t(confirming ? 'gui.knowledge.doc_delete_sure' : 'gui.knowledge.doc_delete'),
      bad: true,
      fn: () => (confirming ? void store.removeDoc(doc) : raiseDoc(doc, at, true)),
    },
  ])
}

function DocDots({ doc }: { doc: KbDoc }): JSX.Element {
  return (
    <button
      className="knowledge-dots"
      aria-haspopup="menu"
      aria-label={t('gui.knowledge.more')}
      onClick={(e) => {
        e.stopPropagation()
        raiseDoc(doc, e.currentTarget.getBoundingClientRect())
      }}
    >
      <DotsGlyph />
    </button>
  )
}

/* What the indexing made of this document, and -- on hover -- why.

   The sentence the server wrote can be a paragraph: a parse that refused names
   the page it stopped on and what it found there. Printed under the name it
   set the height of every row around it and pushed the list's own columns out
   of line, for a sentence that matters on the one document in forty that
   failed. It belongs on the word it explains.

   A warning rides the same way. It is the same kind of sentence -- why the
   status is what it is -- and a document that indexed with half its figures
   unread says `Ready` either way, so the caveat has nowhere else to live. */
function Status({ doc }: { doc: KbDoc }): JSX.Element {
  const why = doc.error || doc.warning
  /* Worked out before the markup: a template literal in `className` is read
     for class names (scripts/check-class-namespace.mjs), and a conditional
     inside one is a class name that only sometimes exists. */
  const marks = why ? `knowledge-status knowledge-${doc.status} knowledge-why` : `knowledge-status knowledge-${doc.status}`
  return (
    <span className={marks} title={why || undefined}>
      {t(`gui.knowledge.status_${doc.status}`)}
    </span>
  )
}

/* A document as a card: what it is, and the two facts that fit under it. */
function DocCard({ doc }: { doc: KbDoc }): JSX.Element {
  return (
    <article className="knowledge-doccard" onClick={() => store.openDoc(doc)}>
      <div className="knowledge-head">
        <FileGlyph doc={doc} />
        <h3 title={doc.source}>{doc.source}</h3>
        <DocDots doc={doc} />
      </div>
      <footer className="knowledge-foot">
        <span className="knowledge-count">{whenOf(doc.updated_at)}</span>
        <Status doc={doc} />
      </footer>
    </article>
  )
}

/* The same document as a row: the columns the header names, in its order. */
function DocRow({ doc }: { doc: KbDoc }): JSX.Element {
  return (
    <li className="knowledge-doc" onClick={() => store.openDoc(doc)}>
      <FileGlyph doc={doc} />
      <span className="knowledge-docname" title={doc.source}>
        {doc.source}
      </span>
      <span className="knowledge-docmeta">{sizeOf(doc.size)}</span>
      <Status doc={doc} />
      <span className="knowledge-docmeta knowledge-when">{whenOf(doc.updated_at)}</span>
      <DocDots doc={doc} />
    </li>
  )
}

/* The four ways a document gets into a base, behind one button.

   Upload and Upload folder are the same path with one file or many: the bytes
   go up through `fs.upload` and the knowledge method takes the path from
   there, because bytes never ride inside an RPC message. Import from URL and
   Online edit are their own methods -- a fetched page and a typed note are
   documents the same way an upload is, which is why they land in the same
   list. */
function pick(multiple: boolean, then: (files: File[]) => void): void {
  const input = document.createElement('input')
  input.type = 'file'
  input.multiple = multiple
  /* A folder upload is the same picker with one attribute: the browser hands
     back every file under what was chosen, and each goes up on its own. */
  if (multiple) input.setAttribute('webkitdirectory', '')
  input.addEventListener('change', () => then([...(input.files ?? [])]))
  input.click()
}

/** The bytes of one file, as the transport wants them. */
function base64Of(file: File): Promise<string> {
  return new Promise((done, fail) => {
    const reader = new FileReader()
    reader.onerror = () => fail(new Error(String(reader.error)))
    /* `readAsDataURL` gives `data:<type>;base64,<payload>`; the payload is
       what the method takes. */
    reader.onload = () => done(String(reader.result).split(',')[1] ?? '')
    reader.readAsDataURL(file)
  })
}

async function send(files: File[]): Promise<void> {
  for (const file of files) await store.upload(file.name, await base64Of(file))
}

function raiseAdd(at: DOMRect): void {
  menu.show(at.left, at.bottom + 4, [
    { label: t('gui.knowledge.add_file'), fn: () => pick(false, (files) => void send(files)) },
    { label: t('gui.knowledge.add_folder'), fn: () => pick(true, (files) => void send(files)) },
    { label: t('gui.knowledge.add_url'), fn: () => store.openSheet({ kind: 'url' }) },
    { label: t('gui.knowledge.add_note'), fn: () => store.openSheet({ kind: 'note' }) },
  ])
}

/* Cards or rows, as the reader last chose. */
function ViewToggle({ view }: { view: store.View }): JSX.Element {
  /* Worked out before the markup rather than inside `className`: a string
     literal in a className expression is a class name as far as
     scripts/check-class-namespace.mjs is concerned, and `'grid'` and `'list'`
     are the view's names, not classes. */
  const onGrid = view === 'grid'
  const cur = (on: boolean): string => (on ? 'knowledge-cur' : '')
  return (
    <div className="knowledge-views" role="group" aria-label={t('gui.knowledge.view')}>
      <button
        className={cur(onGrid)}
        aria-pressed={onGrid}
        title={t('gui.knowledge.view_grid')}
        aria-label={t('gui.knowledge.view_grid')}
        onClick={() => store.setView('grid')}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
          <rect x="4" y="4" width="7" height="7" rx="1.4" />
          <rect x="13" y="4" width="7" height="7" rx="1.4" />
          <rect x="4" y="13" width="7" height="7" rx="1.4" />
          <rect x="13" y="13" width="7" height="7" rx="1.4" />
        </svg>
      </button>
      <button
        className={cur(!onGrid)}
        aria-pressed={!onGrid}
        title={t('gui.knowledge.view_list')}
        aria-label={t('gui.knowledge.view_list')}
        onClick={() => store.setView('list')}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
          <path d="M4 7h16M4 12h16M4 17h16" strokeLinecap="round" />
        </svg>
      </button>
    </div>
  )
}

/* -- one document, over the whole page ---------------------------------
   Two halves of one question: the file as it was written, and the pieces the
   index actually holds of it. Side by side because the second is only
   meaningful against the first -- a chunk list alone says nothing about where
   a cut landed, and a document alone says nothing about what a search can
   find in it.
*/

/* The file itself, as a column of its own pages.

   Pictures rather than the browser's PDF frame, for two things the frame
   cannot do. It reads where to open from the URL once, when it loads, so
   following a reader from piece to piece meant loading the file again on every
   press; and nothing can be drawn on top of it, because it is another document
   in another frame with its own painting.

   Here the panel owns what is on screen: scrolling to a piece is a scroll, and
   the region it was cut from is a box laid over the picture. */
function PagesView({ doc, pages, marks, aim }: {
  doc: KbDoc
  pages: KbPage[]
  /** The regions of the piece being read, which are the ones drawn on. */
  marks: KbRegion[]
  aim: KnowledgeState['aim']
}): JSX.Element {
  const host = useRef<HTMLDivElement>(null)

  /* Scrolled by this panel rather than by the element: `scrollIntoView` moves
     whichever ancestor it has to, which on a page of two scrolling halves
     takes the other half with it. `nth` is in the key so that pressing the
     same piece twice comes back here twice. */
  useEffect(() => {
    const box = host.current
    if (!box || !aim) return
    const page = box.querySelector<HTMLElement>(`[data-page="${aim.page}"]`)
    if (!page) return
    const sheet = pages.find((row) => row.number === aim.page)
    /* Down the page by the same fraction the region sits at, less a margin, so
       what is being read lands under the top edge rather than against it. */
    const into = sheet ? (aim.top / sheet.height) * page.offsetHeight : 0
    box.scrollTo({ top: Math.max(0, page.offsetTop + into - 24), behavior: 'smooth' })
  }, [aim, pages])

  return (
    <div className="knowledge-pages" ref={host}>
      {pages.map((sheet) => (
        <div
          key={sheet.number}
          className="knowledge-sheet2"
          data-page={sheet.number}
          /* The shape before the picture: the boxes are laid out from the page
             sizes, so the column has its full height from the start and a
             scroll to page forty does not land short because pages one to
             thirty-nine had not loaded yet. */
          style={{ aspectRatio: `${sheet.width} / ${sheet.height}` }}
        >
          <img
            loading="lazy"
            src={store.pageUrl(doc.id, sheet.number)}
            alt={t('gui.knowledge.chunk_page', { n: sheet.number })}
          />
          {marks
            .filter((box) => box.page_number === sheet.number)
            .map((box, at) => (
              /* In fractions of the page, not pixels: the picture is the page
                 scaled by one number, so a region's share of the width is its
                 share of the picture however big it is drawn. */
              <span
                key={at}
                className="knowledge-mark"
                style={{
                  left: `${(box.x0 / sheet.width) * 100}%`,
                  top: `${(box.top / sheet.height) * 100}%`,
                  width: `${((box.x1 - box.x0) / sheet.width) * 100}%`,
                  height: `${((box.bottom - box.top) / sheet.height) * 100}%`,
                }}
              />
            ))}
          <span className="knowledge-pagenum">{sheet.number}</span>
        </div>
      ))}
    </div>
  )
}

/** The file as it stands, drawn however its format can be. */
function DocPreview({ doc, aim }: { doc: KbDoc; aim: KnowledgeState['aim'] }): JSX.Element {
  const kind = store.previewKind(doc)
  if (kind === 'markdown') return <MarkdownView doc={doc} />
  if (kind === 'none') {
    return (
      <div className="empty-note">
        <div className="ttl">{t('gui.knowledge.no_preview')}</div>
        {/* A download rather than a wall of bytes: the file is still theirs to
            open, in whatever does know the format. */}
        <a className="mini" href={store.fileUrl(doc.id)} download={doc.source}>
          {t('gui.knowledge.download')}
        </a>
      </div>
    )
  }
  /* Keyed on where it is being sent, which loads the frame again rather than
     re-pointing the one that is there.

     The browser's PDF viewer reads `#page=` and `view=` when it loads the
     document and never again: setting `src` to the same URL with a different
     fragment moves nothing, and navigating the frame's own `contentWindow`
     returns without an error and also moves nothing. Both were tried against
     the viewer. A fresh frame is what it answers to.

     The cost is a reload of the file per press. It is served from the gateway
     on this machine, and the alternative is a viewer of our own -- a megabyte
     of it, in a page that inlines every byte it has. */
  return (
    <iframe
      key={aim ? `${aim.page}:${Math.round(aim.top)}:${aim.nth}` : 'whole'}
      className="knowledge-frame"
      src={store.previewUrl(doc, aim)}
      title={doc.source}
    />
  )
}

/* Markdown is drawn rather than framed: the gateway serves .md as text/plain
   -- correctly, it is text -- and a frame then shows the hashes and the pipes,
   which is the file rather than the document. */
function MarkdownView({ doc }: { doc: KbDoc }): JSX.Element {
  const [text, setText] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setText(null)
    setFailed(null)
    store
      .readText(doc)
      /* Guarded: a reader can close one document and open another before the
         first answers, and the slower answer must not land in the panel that
         has moved on. */
      .then((body) => live && setText(body))
      .catch((e: unknown) => live && setFailed((e as Error)?.message || String(e)))
    return () => {
      live = false
    }
    /* The row rather than its id: what is read depends on the name too -- the
       suffix is what says whether this is markdown at all -- and a row
       replaced by a rebuild is a file worth reading again. */
  }, [doc])

  if (failed !== null) {
    return (
      <div className="empty-note">
        <div className="ttl">{failed}</div>
      </div>
    )
  }
  if (text === null) return <div className="empty-note" />
  return <div className="knowledge-prose prose" dangerouslySetInnerHTML={{ __html: md(text) }} />
}

/* One indexed piece.

   Numbered from 1, the way a reader counts. What the badges say is what the
   parser found and nothing more: a page only where the format has pages, a
   layout type only where the source marked one -- absent means the format does
   not know, never that the value is zero. */
function ChunkRow({ chunk, doc, full, picked, aimed }: {
  chunk: KbChunk
  doc: KbDoc
  full: boolean
  picked: boolean
  aimed: boolean
}): JSX.Element {
  const pages =
    chunk.page_number === null
      ? ''
      : chunk.page_end === null
        ? t('gui.knowledge.chunk_page', { n: chunk.page_number })
        : t('gui.knowledge.chunk_pages', { a: chunk.page_number, b: chunk.page_end })

  /* A piece written before chunk ids existed has nothing to address it, so
     every control that names one is off. It is still read, and the title says
     why it cannot be touched rather than leaving a dead checkbox. */
  const addressable = !!chunk.chunk_id

  const marks = [chunk.enabled ? '' : 'knowledge-off', aimed ? 'knowledge-cur' : ''].filter(Boolean).join(' ')

  return (
    <article
      className={marks ? `knowledge-chunk ${marks}` : 'knowledge-chunk'}
      /* One click takes the file beside it to where this piece was cut from;
         two open it for rewriting. The double is the rarer act, which is why
         it is the one that needs asking for twice. */
      onClick={() => store.aimAt(chunk)}
      onDoubleClick={() => chunk.chunk_id && store.edit(chunk.chunk_id)}
    >
      <header className="knowledge-chunkhd">
        {/* Every control on this line stops the click: the row itself takes
            the preview to this piece, and ticking one is not asking to be
            taken anywhere. */}
        <input
          type="checkbox"
          checked={picked}
          disabled={!addressable}
          title={addressable ? undefined : t('gui.knowledge.chunk_noid')}
          aria-label={t('gui.knowledge.chunk_picked', { n: chunk.chunk_index + 1 })}
          onClick={(e) => e.stopPropagation()}
          onChange={() => store.pick(chunk)}
        />
        <span className="knowledge-chunkix">#{chunk.chunk_index + 1}</span>
        {chunk.layout_type && <span className="knowledge-chunkty">{chunk.layout_type}</span>}
        {pages && <span className="knowledge-chunkpg">{pages}</span>}
        {chunk.manual && <span className="knowledge-chunkty">{t('gui.knowledge.chunk_manual')}</span>}
        {chunk.heading_path.length > 0 && (
          <span className="knowledge-chunkpath" title={chunk.heading_path.join(' > ')}>
            {chunk.heading_path.join(' > ')}
          </span>
        )}
        {/* On or off, as one press. It is the commonest thing done to a piece
            and the only one that is reversible without retyping it, which is
            why it sits on the row rather than behind the menu. */}
        <button
          className="knowledge-chunkon"
          role="switch"
          aria-checked={chunk.enabled}
          disabled={!addressable}
          title={t(chunk.enabled ? 'gui.knowledge.chunk_disable' : 'gui.knowledge.chunk_enable')}
          aria-label={t(chunk.enabled ? 'gui.knowledge.chunk_disable' : 'gui.knowledge.chunk_enable')}
          onClick={(e) => {
            e.stopPropagation()
            void store.switchChunks([chunk.chunk_id], !chunk.enabled)
          }}
        >
          <span className="knowledge-knob" />
        </button>
        <button
          className="knowledge-dots"
          aria-haspopup="menu"
          disabled={!addressable}
          aria-label={t('gui.knowledge.more')}
          onClick={(e) => {
            e.stopPropagation()
            raiseChunk(chunk, e.currentTarget.getBoundingClientRect())
          }}
        >
          <DotsGlyph />
        </button>
      </header>
      {/* The region of the page this piece was cut from, stored at index time.
          Lazy because a document of two hundred pieces is two hundred pictures,
          and a reader reads a few of them. */}
      {chunk.has_crop && chunk.chunk_id && (
        <Crop src={store.cropUrl(doc.id, chunk.chunk_id)} alt={t('gui.knowledge.chunk_crop', { n: chunk.chunk_index + 1 })} />
      )}
      <p className={full ? 'knowledge-chunktx' : 'knowledge-chunktx knowledge-clip'}>{chunk.text}</p>
    </article>
  )
}

/* The region of the page a piece was cut from.

   A fifth of the column at rest, because a reader scanning twenty pieces is
   reading their text and glancing at the shapes; the whole of it on hover,
   for the one they stop at.

   The big copy is positioned rather than grown in place: the list scrolls, so
   an image that got bigger where it sat would push every piece below it down
   under the reader's cursor. `position: fixed` also puts it outside the
   scroller, which would otherwise clip it -- and fixed means the coordinates
   are the viewport's, which is what `getBoundingClientRect` hands back. */
function Crop({ src, alt }: { src: string; alt: string }): JSX.Element {
  const [at, setAt] = useState<{ left: number; top: number } | null>(null)

  const raise = (e: React.MouseEvent<HTMLElement>): void => {
    const box = e.currentTarget.getBoundingClientRect()
    /* To the left of the thumbnail, over the file it came from, and never off
       the top of the screen. Its own width is not known until it has loaded,
       so the room it gets is the room there is to the left of here. */
    setAt({ left: 12, top: Math.max(12, Math.min(box.top, window.innerHeight - 320)) })
  }

  return (
    <span className="knowledge-cropwrap" onMouseEnter={raise} onMouseLeave={() => setAt(null)}>
      <img className="knowledge-crop" loading="lazy" src={src} alt={alt} />
      {at && (
        <span className="knowledge-cropbig" style={{ left: at.left, top: at.top }}>
          <img src={src} alt={alt} />
        </span>
      )}
    </span>
  )
}

/* What a piece's dots raise. Rewriting it re-embeds it; deleting it takes it
   out of the index for good, which is why that one asks twice -- the same
   two-press shape the document and base menus use. */
function raiseChunk(chunk: KbChunk, at: DOMRect, confirming = false): void {
  menu.show(at.left, at.bottom + 4, [
    { label: t('gui.knowledge.chunk_edit'), fn: () => store.edit(chunk.chunk_id) },
    {
      label: t(chunk.enabled ? 'gui.knowledge.chunk_disable' : 'gui.knowledge.chunk_enable'),
      fn: () => void store.switchChunks([chunk.chunk_id], !chunk.enabled),
    },
    '-',
    {
      label: t(confirming ? 'gui.knowledge.chunk_delete_sure' : 'gui.knowledge.chunk_delete'),
      bad: true,
      fn: () => (confirming ? void store.deleteChunks([chunk.chunk_id]) : raiseChunk(chunk, at, true)),
    },
  ])
}

/* One piece's text, being written. The same editor for a rewrite and for a
   new piece: both are a box of text with two ways out of it. */
function ChunkEditor({ text, rows = 6, onSave, onDrop }: {
  text: string
  rows?: number
  onSave: (text: string) => void
  onDrop: () => void
}): JSX.Element {
  const [draft, setDraft] = useState(text)
  return (
    <div className="knowledge-chunkedit">
      <textarea
        autoFocus
        rows={rows}
        placeholder={t('gui.knowledge.chunk_text')}
        value={draft}
        onChange={(e) => setDraft(e.currentTarget.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onDrop()
        }}
      />
      <div className="knowledge-actions">
        <button className="mini ghost" onClick={onDrop}>
          {t('gui.cancel')}
        </button>
        <button className="mini" disabled={!draft.trim()} onClick={() => onSave(draft)}>
          {t('gui.save')}
        </button>
      </div>
    </div>
  )
}

/* The window a piece is rewritten in.

   A window rather than the row itself: a piece is often longer than the four
   lines its row shows, and editing it in place would put a box the size of a
   paragraph where a reader was reading a list. This opens over the page with
   room to see the whole of what they are changing. */
function ChunkSheet({ chunk }: { chunk: KbChunk }): JSX.Element {
  return (
    <div className="knowledge-scrim" onClick={() => store.edit('')}>
      <div className="knowledge-sheet knowledge-chunksheet" onClick={(e) => e.stopPropagation()}>
        <h2>{t('gui.knowledge.chunk_edit')}</h2>
        <ChunkEditor
          text={chunk.text}
          rows={14}
          onSave={(text) => void store.updateChunk(chunk, text)}
          onDrop={() => store.edit('')}
        />
      </div>
    </div>
  )
}

/* What the list is filtered and acted on by.

   The batch verbs replace the counts rather than sitting beside them: a bar
   that shows both leaves a reader working out which of the two numbers the
   buttons apply to. */
function ChunkBar({ s }: { s: KnowledgeState }): JSX.Element {
  const [term, setTerm] = useState(s.chunkQuery)
  const rows = s.chunks ?? []
  const ids = rows.map((row) => row.chunk_id).filter(Boolean)
  const allPicked = ids.length > 0 && ids.every((id) => s.picked.includes(id))
  /* Worked out before the markup rather than inside `className`: a string
     literal in a className expression is a class name as far as
     scripts/check-class-namespace.mjs is concerned, and 'all' is a filter's
     name, not a class. */
  const narrowed = s.chunkFilter !== 'all'

  /* Typed at, not submitted: a reader filtering a list expects it to narrow
     as they type. Held for a beat so that a word is one query rather than
     five, and re-armed on every keystroke. */
  useEffect(() => {
    if (term === s.chunkQuery) return
    const at = window.setTimeout(() => store.setChunkQuery(term), 300)
    return () => window.clearTimeout(at)
  }, [term, s.chunkQuery])

  const raiseFilter = (at: DOMRect): void => {
    const rowFor = (id: store.ChunkFilter, key: string) => ({
      label: t(key),
      on: s.chunkFilter === id,
      fn: () => store.setChunkFilter(id),
    })
    menu.show(at.left, at.bottom + 4, [
      rowFor('all', 'gui.knowledge.chunk_any'),
      rowFor('on', 'gui.knowledge.chunk_on'),
      rowFor('off', 'gui.knowledge.chunk_off_only'),
    ])
  }

  return (
    <div className="knowledge-chunkbar">
      <label className="knowledge-chunkall">
        <input
          type="checkbox"
          checked={allPicked}
          disabled={ids.length === 0}
          onChange={(e) => store.pickAll(e.currentTarget.checked)}
        />
        <span>{s.picked.length ? t('gui.knowledge.chunk_picked', { n: s.picked.length }) : t('gui.knowledge.chunk_all')}</span>
      </label>

      {s.picked.length > 0 ? (
        <div className="knowledge-batch">
          <button className="mini ghost" onClick={() => void store.switchChunks(s.picked, true)}>
            {t('gui.knowledge.chunk_enable')}
          </button>
          <button className="mini ghost" onClick={() => void store.switchChunks(s.picked, false)}>
            {t('gui.knowledge.chunk_disable')}
          </button>
          <button className="mini ghost knowledge-bad" onClick={() => void store.deleteChunks(s.picked)}>
            {t('gui.knowledge.chunk_delete')}
          </button>
        </div>
      ) : (
        <>
          {/* How much of a piece a row shows. A reader scanning for one wants
              the short form; a reader checking where a cut landed wants all of
              it. Its own class rather than the document list's toggle: that
              one is two icons in fixed 30px boxes, and these are words. */}
          <div className="knowledge-seg" role="group" aria-label={t('gui.knowledge.chunks')}>
            <button
              className={s.chunkFull ? 'knowledge-cur' : ''}
              aria-pressed={s.chunkFull}
              onClick={() => store.setChunkFull(true)}
            >
              {t('gui.knowledge.chunk_full')}
            </button>
            <button
              className={s.chunkFull ? '' : 'knowledge-cur'}
              aria-pressed={!s.chunkFull}
              onClick={() => store.setChunkFull(false)}
            >
              {t('gui.knowledge.chunk_clip')}
            </button>
          </div>

          <input
            className="knowledge-chunkq"
            type="search"
            placeholder={t('gui.knowledge.chunk_search')}
            aria-label={t('gui.knowledge.chunk_search')}
            value={term}
            onChange={(e) => setTerm(e.currentTarget.value)}
          />

          {/* The two of them as one block, held to the right-hand end. Apart,
              the slack between them let the `+` wrap to a line of its own
              while the funnel stayed behind. */}
          <span className="knowledge-chunktools">
            <button
              className={narrowed ? 'knowledge-chunkfilter knowledge-cur' : 'knowledge-chunkfilter'}
              aria-haspopup="menu"
              title={t('gui.knowledge.chunk_filter')}
              aria-label={t('gui.knowledge.chunk_filter')}
              onClick={(e) => raiseFilter(e.currentTarget.getBoundingClientRect())}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <path d="M4 7h16M7 12h10M10 17h4" strokeLinecap="round" />
              </svg>
            </button>

            <button
              className="knowledge-add"
              title={t('gui.knowledge.chunk_new')}
              aria-label={t('gui.knowledge.chunk_new')}
              onClick={() => store.draft(true)}
            >
              +
            </button>
          </span>
        </>
      )}
    </div>
  )
}

/* Which slice of the pieces is on screen, and the way to the next.

   Shown only where there is more than one page of them: a pager under a list
   that is all there is says nothing and costs a line. */
function ChunkPager({ page, total }: { page: number; total: number }): JSX.Element | null {
  const last = Math.ceil(total / store.CHUNK_PAGE)
  if (last <= 1) return null
  const from = (page - 1) * store.CHUNK_PAGE + 1
  return (
    <div className="knowledge-pager">
      <button
        className="mini ghost"
        disabled={page <= 1}
        aria-label={t('gui.knowledge.chunk_prev')}
        onClick={() => store.setChunkPage(page - 1)}
      >
        &#8249;
      </button>
      <span>{t('gui.knowledge.chunk_range', { a: from, b: Math.min(total, from + store.CHUNK_PAGE - 1), n: total })}</span>
      <button
        className="mini ghost"
        disabled={page >= last}
        aria-label={t('gui.knowledge.chunk_next')}
        onClick={() => store.setChunkPage(page + 1)}
      >
        &#8250;
      </button>
    </div>
  )
}

/* The pieces the file was cut into, in reading order -- the chunker's own
   numbering as it walks the sections a parser produced, so the sequence down
   this list is the sequence in the document beside it. Unless a query is in
   the box: what a search answers is ordered by how well each piece answered
   it, and that ranking is the only thing the answer has to say.
*/
function ChunkList({ doc, s }: { doc: KbDoc; s: KnowledgeState }): JSX.Element {
  return (
    <section className="knowledge-chunks" aria-label={t('gui.knowledge.chunks')}>
      <header className="knowledge-chunkshd">
        <b>{t('gui.knowledge.chunks')}</b>
        <span className="knowledge-count">{t('gui.knowledge.chunks_total', { n: s.chunkTotal })}</span>
      </header>

      <ChunkBar s={s} />

      {s.drafting && (
        <div className="knowledge-chunk">
          <ChunkEditor text="" onSave={(text) => void store.createChunk(text)} onDrop={() => store.draft(false)} />
        </div>
      )}

      {s.chunksFailed ? (
        <div className="empty-note">
          <div className="ttl">{s.chunksFailed}</div>
        </div>
      ) : s.chunks === null ? (
        <div className="empty-note">
          <div className="ttl">{t('gui.knowledge.loading')}</div>
        </div>
      ) : s.chunks.length === 0 ? (
        /* Two different empties: a document with nothing in the index at all,
           and a filter that admits none of what is. A reader who has typed a
           query is owed the second sentence, not the first. */
        <div className="empty-note">
          <div className="ttl">
            {t(
              s.chunkQuery.trim() || s.chunkFilter !== 'all'
                ? 'gui.knowledge.chunk_nomatch'
                : 'gui.knowledge.no_chunks',
            )}
          </div>
        </div>
      ) : (
        <div className="knowledge-chunklist">
          {s.chunks.map((chunk) => (
            <ChunkRow
              key={chunk.chunk_id || chunk.chunk_index}
              chunk={chunk}
              doc={doc}
              full={s.chunkFull}
              picked={s.picked.includes(chunk.chunk_id)}
              aimed={s.aimed !== '' && s.aimed === (chunk.chunk_id || String(chunk.chunk_index))}
            />
          ))}
        </div>
      )}

      <ChunkPager page={s.chunkPage} total={s.chunkTotal} />
    </section>
  )
}

function DocView({ doc, s }: { doc: KbDoc; s: KnowledgeState }): JSX.Element {
  const edited = s.editing ? (s.chunks ?? []).find((row) => row.chunk_id === s.editing) : undefined
  /* The piece being read, whose regions are what gets covered. */
  const marks = (s.chunks ?? []).find((row) => (row.chunk_id || String(row.chunk_index)) === s.aimed)?.regions ?? []
  /* Captured at the document so the Escape that closes the file is not also
     the one that leaves the page behind it (state/escapeOrder.ts holds that
     table, and this layer is above every row in it). */
  useEffect(() => {
    const esc = (e: globalThis.KeyboardEvent): void => {
      if (e.key !== 'Escape') return
      e.preventDefault()
      e.stopPropagation()
      /* Read now rather than closed over: this listener is registered once,
         and what is on top of the page has changed since. The editor closes
         before the document does, one layer per press. */
      const now = store.get()
      if (now.editing || now.drafting) store.draft(false)
      else store.closeDoc()
    }
    document.addEventListener('keydown', esc, true)
    return () => document.removeEventListener('keydown', esc, true)
  }, [])

  return (
    <div className="knowledge-page knowledge-view">
      <div className="knowledge-bar">
        <nav className="knowledge-path" aria-label={t('gui.knowledge.title')}>
          <button className="knowledge-crumb" onClick={() => store.closeDoc()}>
            {t('gui.knowledge.documents')}
          </button>
          <span className="knowledge-sep" aria-hidden="true">
            /
          </span>
          <span className="knowledge-here" title={doc.source}>
            {doc.source}
          </span>
        </nav>
        <div className="knowledge-tools">
          <DocDots doc={doc} />
        </div>
      </div>

      <div className="knowledge-split">
        <div className="knowledge-orig">
          {/* Its pages where it has them, and the frame where it has not: a
              text file, a note, anything LibreOffice cannot render. Null is
              still reading, and drawing the frame then would load the file for
              nothing a moment before replacing it. */}
          {s.pages === null ? (
            <div className="empty-note">
              <div className="ttl">{t('gui.knowledge.loading')}</div>
            </div>
          ) : s.pages.length > 0 ? (
            <PagesView doc={doc} pages={s.pages} marks={marks} aim={s.aim} />
          ) : (
            <DocPreview doc={doc} aim={s.aim} />
          )}
        </div>
        <ChunkList doc={doc} s={s} />
      </div>

      {edited && <ChunkSheet chunk={edited} />}
    </div>
  )
}

function Documents({
  base,
  bases,
  docs,
  view,
  folders,
  folder,
  tree,
}: {
  base: KbBase
  bases: KbBase[]
  docs: KbDoc[] | null
  view: store.View
  folders: KbFolder[]
  folder: string
  tree: boolean
}): JSX.Element {
  const here = folders.find((row) => row.id === folder)
  const rows = docs === null ? null : docs.filter((doc) => doc.folder_id === folder)
  const inRoot = (docs ?? []).filter((doc) => !doc.folder_id).length

  return (
    <div className="knowledge-page">
      <div className="knowledge-bar">
        <Path base={base} bases={bases} />
        <div className="knowledge-tools">
          <ViewToggle view={view} />
          {/* How this base is tuned. On the documents page rather than only
              behind the card menu: it is where a reader stands when they
              wonder why a search answered the way it did. */}
          <button
            className="knowledge-gear"
            aria-label={t('gui.knowledge.settings_open')}
            title={t('gui.knowledge.settings_open')}
            onClick={() => store.openSettings(base)}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
              <path d="M12 2.9l7.9 4.55v9.1L12 21.1 4.1 16.55v-9.1z" strokeLinejoin="round" />
              <circle cx="12" cy="12" r="2.6" />
            </svg>
          </button>
          <button
            className="knowledge-add"
            aria-haspopup="menu"
            aria-label={t('gui.knowledge.add')}
            title={t('gui.knowledge.add')}
            onClick={(e) => raiseAdd(e.currentTarget.getBoundingClientRect())}
          >
            +
          </button>
        </div>
      </div>

      <div className="knowledge-split">
        {tree ? (
          <FolderTree folders={folders} folder={folder} rootCount={inRoot} />
        ) : (
          <button
            className="knowledge-fshow"
            aria-label={t('gui.knowledge.folders_show')}
            title={t('gui.knowledge.folders_show')}
            onClick={() => store.toggleTree()}
          >
            &#187;
          </button>
        )}

        <div className="knowledge-files">
          {/* Where in the base the list is standing. One level, so it is Root
              or Root and one name. */}
          <nav className="knowledge-where" aria-label={t('gui.knowledge.folders')}>
            <button className="knowledge-crumb" onClick={() => store.setFolder('')}>
              {t('gui.knowledge.root')}
            </button>
            {here && (
              <>
                <span className="knowledge-sep" aria-hidden="true">
                  /
                </span>
                <span className="knowledge-here">{here.name}</span>
              </>
            )}
          </nav>

          {rows === null ? (
            <div className="empty-note">
              <div className="ttl">{t('gui.knowledge.loading')}</div>
            </div>
          ) : rows.length === 0 ? (
            <div className="empty-note">
              <div className="ttl">{t('gui.knowledge.no_documents')}</div>
            </div>
          ) : view !== 'grid' ? (
            <ul className="knowledge-docs">
              {/* The column names, so a row's four values are read as columns
                  rather than as a run of text. */}
              <li className="knowledge-dochead" aria-hidden="true">
                <span className="knowledge-docname">{t('gui.knowledge.col_name')}</span>
                <span className="knowledge-docmeta">{t('gui.knowledge.col_size')}</span>
                <span className="knowledge-status">{t('gui.knowledge.col_status')}</span>
                <span className="knowledge-docmeta knowledge-when">{t('gui.knowledge.col_updated')}</span>
              </li>
              {rows.map((doc) => (
                <DocRow key={doc.id} doc={doc} />
              ))}
            </ul>
          ) : (
            <div className="knowledge-grid">
              {rows.map((doc) => (
                <DocCard key={doc.id} doc={doc} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/* A page to fetch, or a note to write. Two of the four ways in that take
   words rather than a file, and they ask for the same shape: one line and, for
   a note, a body. */
function AddSheet({ kind }: { kind: 'url' | 'note' }): JSX.Element {
  const [line, setLine] = useState('')
  const [body, setBody] = useState('')
  const note = kind === 'note'

  const save = (): void => {
    if (note) void store.addNote(line, body)
    else void store.addUrl(line)
    store.closeSheet()
  }

  return (
    <div className="knowledge-scrim" onClick={() => store.closeSheet()}>
      <div className="knowledge-sheet" onClick={(e) => e.stopPropagation()}>
        <h2>{t(note ? 'gui.knowledge.add_note' : 'gui.knowledge.add_url')}</h2>
        <label>
          <span>{t(note ? 'gui.knowledge.note_title' : 'gui.knowledge.url')}</span>
          <input
            autoFocus
            value={line}
            onChange={(e) => setLine(e.currentTarget.value)}
            onKeyDown={(e) => e.key === 'Enter' && !note && save()}
          />
        </label>
        {note && (
          <label>
            <span>{t('gui.knowledge.note_text')}</span>
            <textarea value={body} onChange={(e) => setBody(e.currentTarget.value)} rows={6} />
          </label>
        )}
        <div className="knowledge-actions">
          <button className="mini ghost" onClick={() => store.closeSheet()}>
            {t('gui.cancel')}
          </button>
          <button className="mini" disabled={!line.trim()} onClick={save}>
            {t('gui.save')}
          </button>
        </div>
      </div>
    </div>
  )
}

export function KnowledgeApp(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  const s = useSyncExternalStore(store.subscribe, store.get)
  const rows = store.shown(s)

  /* Inside a base, the grid is replaced rather than covered: the path across
     the top is the way back, and a card grid behind a document list would be
     two lists of different things in one column. */
  /* A file takes the whole page, folder panel included, rather than a column
     beside the list: a document is what the reader came to look at, and the
     widest thing on the screen should be the thing being read. What the list
     offers is one button away. */
  if (s.viewing) return <DocView doc={s.viewing} s={s} />

  if (s.opened) {
    return (
      <>
        <Documents
          base={s.opened}
          bases={s.bases ?? []}
          docs={s.documents}
          view={s.view}
          folders={s.folders}
          folder={s.folder}
          tree={s.tree}
        />
        {s.moving && <MoveSheet doc={s.moving} folders={s.folders} />}
        {s.sheet?.kind === 'url' && <AddSheet kind="url" />}
        {s.sheet?.kind === 'note' && <AddSheet kind="note" />}
        {s.sheet?.kind === 'settings' && <SettingsPanel base={s.sheet.base} busy={s.busy} models={s.models} />}
      </>
    )
  }

  return (
    <div className="knowledge-page">
      <div className="knowledge-bar">
        <div className="knowledge-tabs" role="tablist">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              role="tab"
              aria-selected={s.tab === tab.id}
              className={s.tab === tab.id ? 'knowledge-cur' : ''}
              onClick={() => store.setTab(tab.id)}
            >
              {t(tab.key)}
            </button>
          ))}
        </div>
        <button className="mini knowledge-new" onClick={() => store.openCreate()}>
          {t('gui.knowledge.create')}
        </button>
      </div>

      {s.failed ? (
        <div className="empty-note">
          <div className="ttl">{s.failed}</div>
        </div>
      ) : s.bases === null ? (
        /* Still reading. Different from the empty list a machine with no bases
           answers with, and saying so beats a grid that flashes empty. */
        <div className="empty-note">
          <div className="ttl">{t('gui.knowledge.loading')}</div>
        </div>
      ) : rows.length === 0 ? (
        <div className="empty-note">
          <div className="ttl">{t(s.tab === 'starred' ? 'gui.knowledge.none_starred' : 'gui.knowledge.none')}</div>
        </div>
      ) : (
        <div className="knowledge-grid">
          {rows.map((base) => (
            <Card key={base.id} base={base} />
          ))}
        </div>
      )}

      {s.sheet?.kind === 'create' && <Sheet base={null} />}
      {s.sheet?.kind === 'settings' && <SettingsPanel base={s.sheet.base} busy={s.busy} models={s.models} />}
    </div>
  )
}
