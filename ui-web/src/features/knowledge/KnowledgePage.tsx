import { useEffect, useRef, useState } from 'react'
import { useSyncExternalStore } from 'react'

import { t } from '../../shell/bridge'
import { md as mdHtml } from '../../shell/prose'
import { ProviderIcon, ravenIconPath } from '../../shell/provider-mark'
import { open as openSettings, setTab as setSettingsTab } from '../settings/store'
import * as store from './store'

import type { KbBase, KbDoc, KbHit } from './types'
import type { JSX } from 'react'

/* Controls the panel shows because they belong to it, and which nothing is
   behind yet. Disabled rather than inert: a button that looks live and does
   nothing when pressed is worse than one that says it is not ready, and the
   reader can see where the feature will be. */
function Soon({ label, className = 'mini ghost' }: { label: string; className?: string }): JSX.Element {
  return (
    <button className={className} disabled title={t('gui.kb.soon')}>
      {label}
    </button>
  )
}

/* Name and embedding model, the two facts a base is created with. The model
   cannot be changed afterwards -- vector width is fixed when the collection is
   made -- so it is asked for here rather than offered as a setting later. */
function CreateDialog({ model, onClose }: { model: string; onClose: () => void }): JSX.Element {
  const [name, setName] = useState('')
  const [embed, setEmbed] = useState(model)
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => field.current?.focus(), [])

  const submit = (): void => {
    if (!name.trim()) return
    // The empty option means no embedding at all, which makes a different base
    // rather than a default one: sending only the name is what produced a base
    // that was asked for as Disabled and came back carrying the configured
    // model.
    void store.create(name, '', embed !== '')
    onClose()
  }
  return (
    <div className="kbmodal" role="dialog" aria-modal="true" aria-label={t('gui.kb.new_title')}>
      <div className="kbdlg">
        <button className="x" aria-label={t('gui.kb.cancel')} onClick={onClose}>
          &times;
        </button>
        <div className="ttl">{t('gui.kb.new_title')}</div>

        <label className="fl" htmlFor="kbname">
          {t('gui.kb.name')}
        </label>
        <input
          id="kbname"
          ref={field}
          className="kbname"
          value={name}
          placeholder={t('gui.kb.name')}
          onChange={(e) => setName(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit()
            if (e.key === 'Escape') onClose()
          }}
        />

        <label className="fl" htmlFor="kbembed">
          {t('gui.kb.embed_model')}
        </label>
        <select
          id="kbembed"
          className="mini"
          value={embed}
          onChange={(e) => setEmbed(e.currentTarget.value)}
        >
          {/* Disabled is a real choice, not the absence of one: a base nobody
              means to search by vector should not be made to carry an index. */}
          <option value="">{t('gui.kb.embed_off')}</option>
          {model && <option value={model}>{model}</option>}
        </select>
        {!embed && <div className="hint">{t('gui.kb.embed_off_note')}</div>}

        <div className="kbacts">
          <button className="mini ghost" onClick={onClose}>
            {t('gui.kb.cancel')}
          </button>
          <button className="mini" disabled={!name.trim()} onClick={submit}>
            {t('gui.kb.create')}
          </button>
        </div>
      </div>
    </div>
  )
}

function BaseRow({ base, on }: { base: KbBase; on: boolean }): JSX.Element {
  return (
    <button className="kbrow" aria-current={on || undefined} onClick={() => void store.open_(base.id)}>
      <span className="nm">{base.name}</span>
      <span className="who">{t('gui.kb.docs', { n: base.documents })}</span>
    </button>
  )
}

/* Each file type gets its own glyph. ragflow ships a sprite of forty SVG files
   for this column; here every byte of the page is inlined into one file, so a
   sprite of forty would be forty files of weight for a column read at a
   glance. Same 24-grid and same stroke as the settings rail's icons, so they
   sit in one visual family.

   A page outline, and on it either a lettered badge naming the format or a
   drawn mark. Letters for the formats a reader names by their extension --
   TXT, DOCX, XLSX -- because that is the badge every desktop puts on them and
   the one they already read. A drawn mark for the rest, where there is no
   extension a reader thinks in: a picture, a note they typed, a page off the
   web. Markdown gets its own official mark rather than letters, which is the
   one people recognise for it.

   The letters are real text rather than drawn paths, so they stay crisp at
   whatever size the glyph is read at. At 15px in a row none of this is
   legible and the family's colour below is what separates one row from the
   next; the mark earns its keep when the glyph is read up close. */
const PAGE =
  '<path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9l-6-6Z"/><path d="M13 3v6h6"/>'

/* `textLength` makes three letters and four occupy the same width, so DOCX and
   TXT sit on badges of one size rather than one badge per label length. */
const BADGE =
  '<rect x="1.5" y="11.5" width="15" height="8" rx="1.6" fill="currentColor" stroke="none"/>'

const labelled = (text: string): string =>
  `${BADGE}<text x="9" y="17.5" text-anchor="middle" font-size="5.6" font-weight="700" ` +
  `font-family="var(--sans)" fill="var(--ink)" stroke="none" textLength="12.4" ` +
  `lengthAdjust="spacingAndGlyphs">${text}</text>`

/* The Markdown mark itself, from the project's own logo, scaled into the badge
   above. Drawn rather than lettered because "MD" is not what anyone recognises
   for it. */
const MARKDOWN =
  '<path d="M30 98V30h20l20 25 20-25h20v68H90V59L70 84 50 59v39H30z"/>' +
  '<path d="M155 98l-30-33h20V30h20v35h20z"/>'

const FILE_ICO: Record<string, string> = {
  file: PAGE,
  image:
    PAGE + '<circle cx="9.5" cy="13.8" r="1.2"/><path d="M7 19l3.4-3.6 2.3 2.2 2.2-2.2L17 19z"/>',
  data: PAGE + '<path d="M10 12.8 7 15.8l3 3M14 12.8l3 3-3 3"/>',
  note:
    PAGE +
    '<path d="M8 13.2h6M8 16h8M8 18.7h4"/>' +
    '<path d="m14.8 19.4 3.9-3.9a1.1 1.1 0 0 1 1.6 1.6l-3.9 3.9-2 .4Z" fill="var(--ink)"/>',
  link:
    PAGE +
    '<path d="M10.6 16.6a1.9 1.9 0 0 1 0-2.7l1.3-1.3a1.9 1.9 0 0 1 2.7 2.7l-.5.5' +
    'M13.4 15.4a1.9 1.9 0 0 1 0 2.7l-1.3 1.3a1.9 1.9 0 0 1-2.7-2.7l.5-.5"/>',
  md:
    PAGE +
    BADGE +
    `<g transform="translate(1.762 11.082) scale(0.0706)" fill="var(--ink)" stroke="none">${MARKDOWN}</g>`,
}

/* Families whose glyph is the format's own name. The label is the file's
   extension rather than the family's, so a .doc is not badged DOCX. */
const LABELLED = new Set(['doc', 'sheet', 'slide', 'pdf', 'txt'])

function fileGlyph(doc: KbDoc): string {
  const family = store.fileFamily(doc)
  if (LABELLED.has(family)) return PAGE + labelled(store.formatLabel(doc))
  return FILE_ICO[family] ?? FILE_ICO.file ?? ''
}

/* What a dialog shows while the work it started is still running.

   A greyed-out button on its own reads as a dialog that ignored the click,
   and the work behind these can take the reader's whole patience: reading a
   web page runs to a 30s timeout. `role="status"` so it is announced rather
   than only drawn. */
function Wait({ label }: { label: string }): JSX.Element {
  return (
    <span className="kbwait" role="status">
      <span className="kbring" aria-hidden="true" />
      {label}
    </span>
  )
}

/* The four kinds of data source, behind one button.

   A menu rather than four buttons, matching the design: three of these open a
   picker or a dialog and one opens a file chooser, so they read as one choice
   with four answers rather than as four separate controls competing for the
   same corner of the panel.

   File and folder are the same input twice. The browser walks a picked
   directory itself and hands back a flat list -- what ragflow's own folder tab
   does -- so nothing on this side recurses, and both arrive at the same
   upload. */
function SourceMenu({ busy }: { busy: boolean }): JSX.Element {
  const [open, setOpen] = useState(false)
  const files = useRef<HTMLInputElement>(null)
  const folder = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (!open) return
    const shut = (): void => setOpen(false)
    const id = setTimeout(() => document.addEventListener('click', shut), 0)
    return () => {
      clearTimeout(id)
      document.removeEventListener('click', shut)
    }
  }, [open])

  /* Cleared after every pick so choosing the same file twice fires again -- a
     retry after a failed index is the same filename. */
  const taken = (el: HTMLInputElement, run: (files: File[]) => void): void => {
    const picked = [...(el.files || [])]
    el.value = ''
    if (picked.length) run(picked)
  }

  return (
    <div className="kbops kbsrc">
      <button
        className="mini"
        disabled={busy}
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation()
          setOpen((v) => !v)
        }}
      >
        + {t('gui.kb.add_source')}
      </button>
      {open && (
        <div className="kbmenu" role="menu">
          <button
            className="mi"
            role="menuitem"
            onClick={() => {
              setOpen(false)
              files.current?.click()
            }}
          >
            {t('gui.kb.src_file')}
          </button>
          <button
            className="mi"
            role="menuitem"
            onClick={() => {
              setOpen(false)
              store.openDialog('note')
            }}
          >
            {t('gui.kb.src_note')}
          </button>
          <button
            className="mi"
            role="menuitem"
            onClick={() => {
              setOpen(false)
              folder.current?.click()
            }}
          >
            {t('gui.kb.src_folder')}
          </button>
          <button
            className="mi"
            role="menuitem"
            onClick={() => {
              setOpen(false)
              store.openDialog('url')
            }}
          >
            {t('gui.kb.src_url')}
          </button>
        </div>
      )}
      <input
        ref={files}
        type="file"
        multiple
        hidden
        onChange={(e) => taken(e.currentTarget, (picked) => void store.uploadAll(picked))}
      />
      {/* `webkitdirectory` is what turns the same chooser into a directory
          chooser; every engine implements it under that prefixed name, and
          `directory` is there for the one that may not. */}
      <input
        ref={folder}
        type="file"
        multiple
        hidden
        {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
        onChange={(e) => taken(e.currentTarget, (picked) => void store.uploadFolder(picked))}
      />
    </div>
  )
}

/* A note: a title nobody has to fill in, and the markdown under it.

   A textarea and not a rich editor. Cherry Studio mounts ProseMirror here, but
   this page inlines every byte of itself into one file, and the preview beside
   it already renders markdown -- so the format the note is stored in is the
   format it is written in, and reading it back is one click away. */
function NoteDialog({ doc, busy }: { doc?: KbDoc; busy: boolean }): JSX.Element {
  const stem = (doc?.source || '').replace(/\.md$/i, '')
  const [title, setTitle] = useState(stem)
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(Boolean(doc))
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => field.current?.focus(), [])
  useEffect(() => {
    if (!doc) return
    let alive = true
    /* Read back through the same route the preview uses: an editor that opens
       a note empty and saves would erase it. */
    store
      .readText(doc)
      .then((body) => {
        if (alive) setText(body)
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [doc])

  const close = (): void => store.closeDialog()
  const submit = (): void => {
    if (!text.trim()) return
    if (doc) void store.saveNote(doc, title, text)
    else void store.addNote(title, text)
  }
  const label = doc ? t('gui.kb.note_edit') : t('gui.kb.note_new')
  return (
    <div className="kbmodal" role="dialog" aria-modal="true" aria-label={label}>
      <div className="kbdlg kbwide">
        <button className="x" aria-label={t('gui.kb.cancel')} onClick={close}>
          &times;
        </button>
        <div className="ttl">{label}</div>

        <label className="fl" htmlFor="kbnotetitle">
          {t('gui.kb.note_title')}
        </label>
        <input
          id="kbnotetitle"
          ref={field}
          className="kbname"
          value={title}
          placeholder={t('gui.kb.note_title_hint')}
          onChange={(e) => setTitle(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') close()
          }}
        />

        <label className="fl" htmlFor="kbnotebody">
          {t('gui.kb.note_body')}
        </label>
        <textarea
          id="kbnotebody"
          className="kbnote"
          value={text}
          disabled={loading}
          onChange={(e) => setText(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') close()
          }}
        />
        <div className="hint">{t('gui.kb.note_body_hint')}</div>

        <div className="kbacts">
          {busy && <Wait label={t('gui.kb.note_saving')} />}
          <button className="mini ghost" onClick={close}>
            {t('gui.kb.cancel')}
          </button>
          <button className="mini" disabled={busy || loading || !text.trim()} onClick={submit}>
            {doc ? t('gui.kb.note_save') : t('gui.kb.create')}
          </button>
        </div>
      </div>
    </div>
  )
}

/* One address. The page is read once and kept as text -- said out loud in the
   dialog, because a reader who expects a live mirror of a URL would find a
   stale one without ever being told it was a copy. */
function UrlDialog({ busy }: { busy: boolean }): JSX.Element {
  const [url, setUrl] = useState('')
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => field.current?.focus(), [])

  const close = (): void => store.closeDialog()
  const submit = (): void => {
    if (url.trim()) void store.addUrl(url)
  }
  return (
    <div className="kbmodal" role="dialog" aria-modal="true" aria-label={t('gui.kb.url_new')}>
      <div className="kbdlg">
        <button className="x" aria-label={t('gui.kb.cancel')} onClick={close}>
          &times;
        </button>
        <div className="ttl">{t('gui.kb.url_new')}</div>

        <label className="fl" htmlFor="kburl">
          {t('gui.kb.url_label')}
        </label>
        <input
          id="kburl"
          ref={field}
          className="kbname"
          type="url"
          value={url}
          /* Locked while the page is being read: a second address typed over
             the one in flight would be added under the first one's answer. */
          disabled={busy}
          placeholder="https://"
          onChange={(e) => setUrl(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit()
            if (e.key === 'Escape') close()
          }}
        />
        <div className="hint">{t('gui.kb.url_hint')}</div>

        <div className="kbacts">
          {busy && <Wait label={t('gui.kb.url_reading')} />}
          {/* Cancel stays live. The fetch runs to 30s, and a reader who has
              changed their mind should not be held in front of it -- the
              document still lands if the read succeeds. */}
          <button className="mini ghost" onClick={close}>
            {t('gui.kb.cancel')}
          </button>
          <button className="mini" disabled={busy || !url.trim()} onClick={submit}>
            {t('gui.kb.url_add')}
          </button>
        </div>
      </div>
    </div>
  )
}

/* One result. Collapsed to its heading until asked for, because ten chunks of
   prose at once is a wall rather than a list -- the reader is scanning for
   which document answered before they read any of it. */
function Hit({ hit, at, docs }: { hit: KbHit; at: number; docs: KbDoc[] }): JSX.Element {
  const [open, setOpen] = useState(at === 0)
  /* The row it came from, when it is still there. A search answers from the
     index, and a document deleted since is still in it until the next write --
     so the hit carries its own source to fall back on. */
  const from = docs.find((d) => d.id === hit.document_id)
  const name = from?.source || hit.source || t('gui.kb.recall_gone')
  const family = from ? store.fileFamily(from) : 'file'
  return (
    <div className="kbhit">
      <button className="kbhithd" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="kbhitn">{at + 1}</span>
        <svg
          className={`kbico kbf-${family}`}
          viewBox="0 0 24 24"
          aria-hidden="true"
          dangerouslySetInnerHTML={{ __html: from ? fileGlyph(from) : (FILE_ICO.file ?? '') }}
        />
        <span className="kbhitnm" title={name}>
          {name}
        </span>
        {/* Where in its document the chunk sat. A retrieved fragment read on
            its own says nothing about that, and it is the first thing anyone
            testing recall wants to know. */}
        {hit.total_chunks ? (
          <span className="kbhitix" title={t('gui.kb.recall_chunk', { n: (hit.chunk_index ?? 0) + 1, total: hit.total_chunks })}>
            #{(hit.chunk_index ?? 0) + 1}
          </span>
        ) : null}
        {/* The similarity, not only the rank: a recall test is run to find out
            how near the near thing actually was, and three hits at 0.83 mean
            something different from one at 0.83 over two at 0.31. */}
        <span className="kbhitsc">{hit.score.toFixed(3)}</span>
        <span className="kbhitrk">{t('gui.kb.recall_rank', { n: at + 1 })}</span>
        <span className="kbhitcv" aria-hidden="true">
          {open ? '\u2303' : '\u2304'}
        </span>
      </button>
      {open && <div className="kbhittx">{hit.text}</div>}
    </div>
  )
}

/* The processors a file could be put through on the way in.

   Listed before any of them is wired, because the list is the answer to "what
   will this eventually do" -- and a lone "Don't use" in a select answers
   nothing. Every row is disabled and says why it is not available: `Local
   Document` is ours and would have to be downloaded, the rest are other
   people's services and would have to be configured.

   The marks come from the same asset tree and the same digest as every other
   vendor logo on the page, so they are cached and replaced like the rest. */
const PROCESSORS: Array<{ id: string; name: string; state: string }> = [
  { id: 'raven', name: 'gui.kb.proc_local', state: 'gui.kb.proc_notdl' },
  { id: 'paddleocr', name: 'PaddleOCR', state: 'gui.kb.proc_notcfg' },
  { id: 'mineru', name: 'MinerU', state: 'gui.kb.proc_notcfg' },
  { id: 'doc2x', name: 'Doc2X', state: 'gui.kb.proc_notcfg' },
  { id: 'mistral', name: 'Mistral', state: 'gui.kb.proc_notcfg' },
]

function FileProcessing(): JSX.Element {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    if (!open) return
    const shut = (): void => setOpen(false)
    const id = setTimeout(() => document.addEventListener('click', shut), 0)
    return () => {
      clearTimeout(id)
      document.removeEventListener('click', shut)
    }
  }, [open])

  return (
    <div className="kbproc">
      <button
        className="kbpick"
        aria-expanded={open}
        aria-label={t('gui.kb.set_proc')}
        onClick={(e) => {
          e.stopPropagation()
          setOpen((v) => !v)
        }}
      >
        <span>{t('gui.kb.set_proc_off')}</span>
        <span aria-hidden="true">{'\u2304'}</span>
      </button>
      {open && (
        <div className="kbmenu kbprocm" role="listbox">
          {PROCESSORS.map((row) => (
            <div className="mi kbprocr" role="option" aria-selected={false} aria-disabled="true" key={row.id}>
              {row.id === 'raven' ? (
                <img
                  className="provider-icon"
                  src={ravenIconPath()}
                  data-provider="raven"
                  alt=""
                  aria-hidden="true"
                  draggable="false"
                />
              ) : (
                <ProviderIcon id={row.id} name={row.name} />
              )}
              <span className="nm">{row.name.startsWith('gui.') ? t(row.name) : row.name}</span>
              <span className="st">{t(row.state)}</span>
            </div>
          ))}
          <div className="kbprocs">
            <div className="mi kbprocr" role="option" aria-selected={false} aria-disabled="true">
              <svg className="provider-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
                <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
                <circle cx="16" cy="7" r="2" />
                <circle cx="10" cy="17" r="2" />
              </svg>
              <span className="nm">{t('gui.kb.proc_settings')}</span>
            </div>
            {/* The one that is in force, so the list says which of these the
                base is actually on rather than only what it could be on. */}
            <div className="mi kbprocr kbon" role="option" aria-selected={true} aria-disabled="true">
              <svg className="provider-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
                <circle cx="12" cy="12" r="9" />
                <path d="M5.6 5.6l12.8 12.8" />
              </svg>
              <span className="nm">{t('gui.kb.set_proc_off')}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

/* The circled "i" beside a setting's name.

   A title rather than a popover: the text is one sentence, every platform
   already shows a title on hover and on focus, and a popover here would be a
   second dismissable layer inside a dialog. */
function Help({ text }: { text: string }): JSX.Element {
  return (
    <span className="kbhelp" title={text} tabIndex={0} role="note" aria-label={text}>
      i
    </span>
  )
}

function Field({ label, help, children }: { label: string; help: string; children: JSX.Element }): JSX.Element {
  return (
    <div className="kbset">
      <div className="kbsetl">
        <span>{label}</span>
        <Help text={help} />
      </div>
      {/* The control gets a wrapper of its own rather than being laid out
          directly: the row's rule sizes whatever sits in this slot, and
          applied to a switch instead of around it, it stretched a 38px toggle
          across the dialog. */}
      <div className="kbsetc">{children}</div>
    </div>
  )
}

/* One base's settings.

   The embedding model is shown and not offered: the vector store is sized to
   its width when the base is created, so changing it is a rebuild of every
   vector rather than a preference. The stale-base check exists because that
   mismatch has to be detectable; a dropdown here would be a way to cause it. */
function SettingsDialog({ base, busy }: { base: KbBase; busy: boolean }): JSX.Element {
  const saved = store.settingsOf(base)
  const [topK, setTopK] = useState(saved.top_k)
  const [smart, setSmart] = useState(saved.smart_chunking)
  const [sep, setSep] = useState(saved.separator)
  const [size, setSize] = useState(String(saved.chunk_size))
  const [lap, setLap] = useState(String(saved.chunk_overlap))
  const [adv, setAdv] = useState(true)

  const close = (): void => store.closeSettings()
  const restore = (): void => {
    setTopK(store.DEFAULTS.top_k)
    setSmart(store.DEFAULTS.smart_chunking)
    setSep(store.DEFAULTS.separator)
    setSize(String(store.DEFAULTS.chunk_size))
    setLap(String(store.DEFAULTS.chunk_overlap))
  }
  const save = (): void => {
    void store.saveSettings({
      top_k: topK,
      smart_chunking: smart,
      separator: sep,
      chunk_size: Number(size),
      chunk_overlap: Number(lap),
    })
  }
  /* The two numbers are typed, so they can be mid-edit and empty or nonsense;
     saving those would ask the engine to refuse them. */
  const numbers = Number(size) > 0 && Number(lap) >= 0 && Number(lap) < Number(size)

  return (
    <div className="kbmodal" role="dialog" aria-modal="true" aria-label={t('gui.kb.set_title')}>
      <div className="kbdlg kbwide kbsets">
        <button className="x" aria-label={t('gui.kb.cancel')} onClick={close}>
          &times;
        </button>
        <div className="ttl">{t('gui.kb.set_title')}</div>

        <Field label={t('gui.kb.set_proc')} help={t('gui.kb.set_proc_help')}>
          <FileProcessing />
        </Field>

        <Field label={t('gui.kb.set_embed')} help={t('gui.kb.set_embed_help')}>
          <div className="kbfixed">{base.embedding_model || t('gui.kb.embed_off')}</div>
        </Field>

        <Field label={t('gui.kb.set_topk')} help={t('gui.kb.set_topk_help')}>
          <div className="kbslide">
            <b>{topK}</b>
            <input
              type="range"
              min={1}
              max={50}
              value={topK}
              aria-label={t('gui.kb.set_topk')}
              onChange={(e) => setTopK(Number(e.currentTarget.value))}
            />
            <div className="kbends">
              <span>1</span>
              <span>50</span>
            </div>
          </div>
        </Field>

        <button className="kbadv" aria-expanded={adv} onClick={() => setAdv((v) => !v)}>
          <span>{t('gui.kb.set_adv')}</span>
          <span aria-hidden="true">{adv ? '\u2303' : '\u2304'}</span>
        </button>

        {adv && (
          <>
            <Field label={t('gui.kb.set_smart')} help={t('gui.kb.set_smart_help')}>
              <button
                className="kbtog"
                role="switch"
                aria-checked={smart}
                aria-label={t('gui.kb.set_smart')}
                onClick={() => setSmart((v) => !v)}
              >
                <span />
              </button>
            </Field>
            <Field label={t('gui.kb.set_sep')} help={t('gui.kb.set_sep_help')}>
              {/* Escaped on the way in and out: the separator a reader means is
                  two newlines, and a field cannot hold those as themselves. */}
              <input
                className="kbname"
                value={sep.replace(/\n/g, '\\n').replace(/\t/g, '\\t')}
                disabled={smart}
                onChange={(e) =>
                  setSep(e.currentTarget.value.replace(/\\n/g, '\n').replace(/\\t/g, '\t'))
                }
              />
            </Field>
            <Field label={t('gui.kb.set_size')} help={t('gui.kb.set_size_help')}>
              <div className="kbunit">
                <input
                  className="kbname"
                  type="number"
                  min={64}
                  max={8192}
                  value={size}
                  onChange={(e) => setSize(e.currentTarget.value)}
                />
                <span>{t('gui.kb.set_tokens')}</span>
              </div>
            </Field>
            <Field label={t('gui.kb.set_lap')} help={t('gui.kb.set_lap_help')}>
              <div className="kbunit">
                <input
                  className="kbname"
                  type="number"
                  min={0}
                  max={8192}
                  value={lap}
                  onChange={(e) => setLap(e.currentTarget.value)}
                />
                <span>{t('gui.kb.set_tokens')}</span>
              </div>
            </Field>
            {/* Said here because it is the question a reader has the moment
                they move these: the chunks already in the base were cut by the
                old numbers and stay that way until they are indexed again. */}
            <div className="hint">{t('gui.kb.set_newonly')}</div>
          </>
        )}

        <div className="kbacts kbfoot">
          <button className="mini ghost kbrestore" onClick={restore}>
            {t('gui.kb.set_restore')}
          </button>
          <button className="mini" disabled={busy || !numbers} onClick={save}>
            {t('gui.kb.set_save')}
          </button>
        </div>
      </div>
    </div>
  )
}

/* What this base answers with, for a question nobody is asking it in a turn.

   Its own panel rather than a box over the file list: a recall test is a thing
   a reader does deliberately, reads, and leaves, and the list of files is not
   what they are looking at while they do it. */
function RecallDialog({ s }: { s: ReturnType<typeof store.getState> }): JSX.Element {
  const [text, setText] = useState(s.query)
  const [past, setPast] = useState(false)
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => field.current?.focus(), [])
  const recent = past ? store.history() : []

  const close = (): void => store.closeRecall()
  const run = (q: string): void => {
    setPast(false)
    if (q.trim()) void store.searchNow(q)
  }
  return (
    <div className="kbmodal" role="dialog" aria-modal="true" aria-label={t('gui.kb.recall_title')}>
      <div className="kbdlg kbwide">
        <button className="x" aria-label={t('gui.kb.cancel')} onClick={close}>
          &times;
        </button>
        <div className="ttl">{t('gui.kb.recall_title')}</div>

        <div className="kbask">
          <div className="kbaskf">
            <input
              ref={field}
              className="kbname"
              value={text}
              placeholder={t('gui.kb.recall_ask')}
              onChange={(e) => setText(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') run(text)
                if (e.key === 'Escape') (past ? setPast(false) : close())
              }}
            />
            {/* The questions this browser has asked, and nowhere else: a recall
                query is a scratch question, and whether it has been tried
                before is only useful to the person who typed it. */}
            <button
              className="kbpast"
              aria-label={t('gui.kb.recall_recent')}
              aria-expanded={past}
              onClick={() => setPast((v) => !v)}
            >
              &#8634;
            </button>
            {past && (
              <div className="kbmenu" role="menu">
                {recent.length ? (
                  <>
                    {recent.map((q) => (
                      <button
                        key={q}
                        className="mi"
                        role="menuitem"
                        onClick={() => {
                          setText(q)
                          run(q)
                        }}
                      >
                        {q}
                      </button>
                    ))}
                    <button
                      className="mi danger"
                      role="menuitem"
                      onClick={() => {
                        store.forgetHistory()
                        setPast(false)
                      }}
                    >
                      {t('gui.kb.recall_forget')}
                    </button>
                  </>
                ) : (
                  <div className="hint">{t('gui.kb.recall_recent')}</div>
                )}
              </div>
            )}
          </div>
          <button className="mini" disabled={s.searching || !text.trim()} onClick={() => run(text)}>
            {t('gui.kb.recall_run')}
          </button>
        </div>

        {s.searching && <Wait label={t('gui.kb.recall_run')} />}
        {s.hits && !s.searching ? (
          <div className="kbstats">
            <b>{t('gui.kb.recall_n', { n: s.hits.length })}</b>
            {s.cost && (
              <span
                title={t('gui.kb.recall_cost', { ms: s.cost.search_ms, embed: s.cost.embed_ms })}
              >
                {t('gui.kb.recall_ms', { ms: s.cost.search_ms })}
              </span>
            )}
          </div>
        ) : null}

        <div className="kbhits">
          {s.hits === null ? (
            <div className="hint">{t('gui.kb.recall_empty')}</div>
          ) : s.hits.length ? (
            s.hits.map((hit, at) => (
              <Hit key={`${hit.document_id}-${hit.chunk_index ?? at}`} hit={hit} at={at} docs={s.docs} />
            ))
          ) : (
            <div className="hint">{t('gui.kb.recall_none')}</div>
          )}
        </div>
      </div>
    </div>
  )
}

/* The menu a row's actions live behind. Four of them on every row would be a
   wall of buttons across a table; the two that are wired sit beside the two
   that are not, so the set reads as one feature rather than as a gap. */
function DocMenu({ doc, busy }: { doc: KbDoc; busy: boolean }): JSX.Element {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    if (!open) return
    const shut = (): void => setOpen(false)
    /* Any click outside closes it, and the listener is added on the next tick
       so the click that opened the menu does not immediately close it. */
    const id = setTimeout(() => document.addEventListener('click', shut), 0)
    return () => {
      clearTimeout(id)
      document.removeEventListener('click', shut)
    }
  }, [open])
  return (
    <div className="kbops">
      <button
        className="mini ghost dots"
        aria-label={t('gui.kb.doc_ops', { name: doc.source })}
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation()
          setOpen((v) => !v)
        }}
      >
        &#8943;
      </button>
      {open && (
        <div className="kbmenu" role="menu">
          <Soon label={t('gui.kb.doc_view_chunks')} className="mi" />
          {/* Only a note. Every other origin is a copy of something the reader
              holds elsewhere, and offering to edit it here would make this
              base the only place their change exists. */}
          {doc.origin === 'note' && (
            <button
              className="mi"
              role="menuitem"
              disabled={busy}
              onClick={() => {
                setOpen(false)
                store.openDialog('note', doc)
              }}
            >
              {t('gui.kb.doc_edit_note')}
            </button>
          )}
          {/* Closed on the way out. A menu still standing over the row it just
              acted on hides the status it changed, which is the one thing the
              reader pressed it to see. */}
          <button
            className="mi"
            role="menuitem"
            disabled={busy}
            onClick={() => {
              setOpen(false)
              void store.retry(doc)
            }}
          >
            {t('gui.kb.doc_reindex')}
          </button>
          <Soon label={t('gui.kb.doc_disable')} className="mi" />
          <button
            className="mi danger"
            role="menuitem"
            disabled={busy}
            onClick={() => {
              setOpen(false)
              store.removeDoc(doc)
            }}
          >
            {t('gui.kb.delete')}
          </button>
        </div>
      )}
    </div>
  )
}

/* Relative, because "7 minutes ago" is what a reader checks for after an
   upload; an absolute date is for a column nobody is watching.

   The same thresholds and the same keys as the agents roster
   (features/xa/XaPage.tsx `agoText`), spelled again rather than imported --
   the two islands share no module, and the ratchet in
   scripts/count-shared-globals.mjs is there to keep it that way. An
   unparseable stamp is shown as it came: a row dated "Invalid Date" says less
   than one dated with the string the engine actually sent. */
function ago(iso: string): string {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return iso
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000))
  if (mins < 1) return t('gui.time.ago_now')
  if (mins < 60) return t('gui.time.ago_m', { n: mins })
  const hours = Math.round(mins / 60)
  if (hours < 24) return t('gui.time.ago_h', { n: hours })
  return t('gui.time.ago_d', { n: Math.round(hours / 24) })
}

function DocRow({ doc, busy, picked }: { doc: KbDoc; busy: boolean; picked: boolean }): JSX.Element {
  /* The table is a grid, so a row is six sibling cells rather than an element
     that could carry the state. Every one of them is marked, or a picked row
     would be striped. */
  const td = picked ? 'td kbsel' : 'td'
  return (
    <>
      <div className={`${td} kbtick`}>
        <input
          type="checkbox"
          checked={picked}
          aria-label={t('gui.kb.pick_row', { name: doc.source })}
          onChange={() => store.togglePick(doc.id)}
        />
      </div>
      <div className={`${td} nm`}>
        {/* The glyph is inside the button: the whole cell is the way into the
            file, and an icon beside it that did nothing when clicked would be
            the one part of the row that is not. */}
        <button className="kbopen" title={doc.source} onClick={() => store.openDoc(doc)}>
          <svg
            className={`kbico kbf-${store.fileFamily(doc)}`}
            viewBox="0 0 24 24"
            aria-hidden="true"
            dangerouslySetInnerHTML={{ __html: fileGlyph(doc) }}
          />
          <span>{doc.source}</span>
        </button>
      </div>
      <div className={td}>{store.docType(doc)}</div>
      {/* The reason rides on the status rather than under the row. A red line
          beneath every failure pushed the rows apart and made a list of files
          hard to scan; the status is where a reader is already looking when
          they want to know what went wrong. */}
      <div className={`${td} st s-${doc.status}`} title={doc.error || undefined}>
        {t('gui.kb.doc_' + doc.status)}
      </div>
      <div className={td}>{ago(doc.updated_at)}</div>
      <div className={td}>
        <DocMenu doc={doc} busy={busy} />
      </div>
    </>
  )
}

/* Markdown, rendered rather than framed.

   Through the shell's own `md`, which is what the transcript and the workspace
   already draw with, so a document reads the same wherever it is opened -- and
   which escapes the source before anything else, so an upload cannot put
   markup of its own into the page. */
function MarkdownView({ doc }: { doc: KbDoc }): JSX.Element {
  const [text, setText] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    setText(null)
    setErr(null)
    store
      .readText(doc)
      /* Guarded because a reader can close one document and open another
         before the first answers, and the slower answer must not land in the
         pane that has moved on. */
      .then((t) => live && setText(t))
      .catch((e: unknown) => live && setErr((e as Error)?.message || String(e)))
    return () => {
      live = false
    }
  }, [doc.id])

  if (err !== null) {
    return (
      <div className="empty-note">
        <div className="ttl">{err}</div>
      </div>
    )
  }
  if (text === null) return <div className="empty-note" />
  return <div className="kbprose prose" dangerouslySetInnerHTML={{ __html: mdHtml(text) }} />
}

/* The original file, framed.

   One iframe for every kind the browser can draw, and the gateway converts the
   office formats to PDF before it gets here -- so the page needs no viewer
   library of its own, which matters more than it sounds: this bundle is
   inlined into a single HTML file, and a PDF renderer alone would be megabytes
   of it.

   The frame carries no `sandbox` attribute on purpose. Every response already
   arrives under a CSP sandbox, which is what gives it an opaque origin; the
   attribute as well would stop the browser's own PDF viewer, which is
   script-driven, from drawing anything at all. */
function DocViewer({ doc }: { doc: KbDoc }): JSX.Element {
  const kind = store.previewKind(doc)
  return (
    <>
      <div className="kbvhd">
        <button className="kbback" aria-label={t('gui.kb.back')} onClick={() => store.closeDoc()}>
          &#8592;
        </button>
        <b title={doc.source}>{doc.source}</b>
        {/* A captured page is a copy taken once. Without the address it came
            from, a reader looking at a stale copy has no way back to the
            live one -- and nothing else on the page says where it came from.
            `noreferrer` because this document is not the reader's own. */}
        {doc.origin === 'url' && doc.origin_ref && (
          <a className="kbfrom" href={doc.origin_ref} target="_blank" rel="noopener noreferrer">
            {doc.origin_ref}
          </a>
        )}
      </div>
      {kind === 'markdown' ? (
        <MarkdownView doc={doc} />
      ) : kind === 'none' ? (
        <div className="empty-note">
          <div className="ttl">{t('gui.kb.no_preview')}</div>
          {/* A download rather than a wall of bytes: the file is still theirs
              to open, in whatever does know the format. */}
          <a className="mini" href={store.previewUrl(doc)} download={doc.source}>
            {t('gui.kb.download')}
          </a>
        </div>
      ) : (
        <iframe className="kbframe" src={store.previewUrl(doc)} title={doc.source} />
      )}
    </>
  )
}

function BasePanel({ base, s }: { base: KbBase; s: ReturnType<typeof store.getState> }): JSX.Element {
  /* The file takes the whole panel rather than opening beside the table: a
     document is what the reader came to look at, and half a page of it is not
     worth keeping a list they can get back to with one button. */
  if (s.viewing) return <DocViewer doc={s.viewing} />
  /* Every row, and at least one: an empty list whose header tick reads "on"
     would offer to act on nothing. */
  const all = s.docs.length > 0 && s.picked.length === s.docs.length
  return (
    <>
      <div className="kbhd">
        <b>{base.name}</b>
        {/* The model this base was built with, not the one configured now. A
            base outlives a config change, its vector width is fixed at
            creation, and a mismatch is why a search stops answering -- so it
            stays on screen rather than being something to go and look up. */}
        {/* The model this base was built with, not the one configured now: a
            base outlives a config change, its width is fixed at creation, and
            a mismatch is why a search stops answering. A base made without one
            says so, rather than borrowing today's model to fill the space. */}
        <span className="mdl">{base.embedding_model || t('gui.kb.embed_off')}</span>
        {/* A base with no embedding model has no vectors to search, and
            `search` skips it rather than failing -- which from here would look
            like a base that answers nothing to every question. Disabled and
            said out loud instead. */}
        {base.embedding_model ? (
          <button className="mini ghost" onClick={() => store.openRecall()}>
            {t('gui.kb.recall_test')}
          </button>
        ) : (
          <button className="mini ghost" disabled title={t('gui.kb.recall_off')}>
            {t('gui.kb.recall_test')}
          </button>
        )}
        {/* A glyph rather than a word: the header already carries the base's
            name and its model, and a row of text buttons after those reads as
            more text. The name is on the control for anyone not reading the
            drawing -- a pointer, a screen reader, a keyboard. */}
        <button
          className="mini ghost kbgear"
          aria-label={t('gui.kb.settings')}
          title={t('gui.kb.settings')}
          onClick={() => store.openSettings()}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M3 6.5h6M15 6.5h6M3 12h12M19 12h2M3 17.5h4M13 17.5h8" />
            <path d="M12 4.3v4.4M17 9.8v4.4M10 15.3v4.4" />
          </svg>
        </button>
      </div>
      {/* One row, two jobs: what the base is, or what is picked out of it.
          Both at once would put two counts and four controls on one line, and
          adding a data source is not something a reader reaches for in the
          middle of choosing which files to be rid of. */}
      {s.picked.length ? (
        <div className="kbsub kbpicked">
          <span className="who">{t('gui.kb.picked_n', { n: s.picked.length })}</span>
          <button className="mini ghost" disabled={s.busy} onClick={() => void store.reindexPicked()}>
            {t('gui.kb.docs_reindex')}
          </button>
          <button className="mini ghost danger" disabled={s.busy} onClick={() => store.removePicked()}>
            {t('gui.kb.delete')}
          </button>
        </div>
      ) : (
        <div className="kbsub">
          <span className="who">
            {s.adding
              ? t('gui.kb.adding_n', { done: s.adding.done + 1, total: s.adding.total })
              : t('gui.kb.updated_when', { when: ago(base.updated_at) })}
          </span>
          <SourceMenu busy={s.busy} />
        </div>
      )}
      {s.docs.length ? (
        <div className="kbtable">
          <div className="th kbtick">
            <input
              type="checkbox"
              checked={all}
              aria-label={t('gui.kb.pick_all')}
              onChange={() => store.pickAll(!all)}
            />
          </div>
          <div className="th">{t('gui.kb.col_name')}</div>
          <div className="th">{t('gui.kb.col_type')}</div>
          <div className="th">{t('gui.kb.col_status')}</div>
          <div className="th">{t('gui.kb.col_updated')}</div>
          <div className="th" />
          {s.docs.map((d) => (
            <DocRow key={d.id} doc={d} busy={s.busy} picked={s.picked.includes(d.id)} />
          ))}
        </div>
      ) : (
        <div className="empty-note">
          <div className="ttl">{t('gui.kb.no_docs')}</div>
        </div>
      )}
      {/* Over the panel rather than over a strip of it: a reader dragging a
          file at a list is aiming at the list, and a target smaller than what
          they are aiming at is a target they miss. */}
      {s.dragDepth > 0 && (
        <div className="kbdrop">
          <span>{t('gui.kb.drop_here')}</span>
        </div>
      )}
    </>
  )
}

/* Dropped files, flattened.

   A dropped folder arrives as a directory entry rather than a file, so it has
   to be walked here -- unlike a picked one, which the chooser walks itself.
   `webkitGetAsEntry` is how every engine exposes that, and a drop of plain
   files never reaches the recursion. */
interface FsEntry {
  isFile: boolean
  isDirectory: boolean
  file(cb: (f: File) => void, err?: (e: unknown) => void): void
  createReader(): { readEntries(cb: (entries: FsEntry[]) => void, err?: (e: unknown) => void): void }
}

async function walk(entry: FsEntry, out: File[]): Promise<void> {
  if (entry.isFile) {
    const file = await new Promise<File | null>((resolve) => entry.file(resolve, () => resolve(null)))
    if (file) out.push(file)
    return
  }
  if (!entry.isDirectory) return
  const reader = entry.createReader()
  /* readEntries answers in batches and signals the end with an empty one;
     reading it once returns the first hundred of a large directory. */
  for (;;) {
    const batch = await new Promise<FsEntry[]>((resolve) =>
      reader.readEntries(resolve, () => resolve([])),
    )
    if (!batch.length) return
    for (const child of batch) await walk(child, out)
  }
}

async function droppedFiles(transfer: DataTransfer): Promise<File[]> {
  const items = [...(transfer.items || [])]
  const entries = items
    .map((i) => (i as unknown as { webkitGetAsEntry?: () => FsEntry | null }).webkitGetAsEntry?.() ?? null)
    .filter((e): e is FsEntry => Boolean(e))
  if (!entries.length) return [...(transfer.files || [])]
  const out: File[] = []
  for (const entry of entries) await walk(entry, out)
  return out
}

export function KnowledgeApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  const [creating, setCreating] = useState(false)

  if (s.failed) {
    /* The reason, not an empty list: an unreachable engine and a deployment
       with no bases look identical otherwise, and only one of them is fine. */
    return (
      <div className="empty-note">
        <div className="ttl">{s.failed}</div>
      </div>
    )
  }
  if (!s.loaded) return <div className="empty-note" />
  if (s.status && !s.status.configured) {
    /* Naming the state is not enough: the endpoint is set in a section of
       the settings dialog, and a reader told only that one is missing has to
       go looking. Through the settings store rather than a new shell verb:
       the island owns both halves already, and the ratchet in
       ui-web/scripts/count-shared-globals.mjs exists to stop an island asking
       the legacy layer for what it can reach directly. */
    return (
      <div className="empty-note">
        <div className="ttl">{t('gui.kb.unconfigured')}</div>
        <div className="ds">{t('gui.kb.unconfigured_where')}</div>
        <button
          className="mini"
          onClick={() => {
            setSettingsTab('memory')
            void openSettings()
          }}
        >
          {t('gui.kb.unconfigured_go')}
        </button>
      </div>
    )
  }

  const open = s.openId ? s.bases.find((b) => b.id === s.openId) : undefined
  return (
    <>
      <div className="kbsplit">
        <div className="kbrail">
          <button className="mini kbadd" disabled={s.busy} onClick={() => setCreating(true)}>
            + {t('gui.kb.new')}
          </button>
          {s.bases.length ? (
            s.bases.map((base) => <BaseRow key={base.id} base={base} on={base.id === s.openId} />)
          ) : (
            <div className="hint">{t('gui.kb.none')}</div>
          )}
        </div>
        <div
          className="kbpane"
          /* Only while a base is open and nothing is already going up: a drop
             onto the "pick a base" panel has nowhere to land, and a second
             batch on top of a running one would interleave two uploads. */
          onDragEnter={(e) => {
            if (!open || s.busy || !e.dataTransfer.types.includes('Files')) return
            e.preventDefault()
            store.dragEnter()
          }}
          onDragOver={(e) => {
            if (!open || s.busy || !e.dataTransfer.types.includes('Files')) return
            /* Both halves are needed: without preventDefault the browser
               navigates to the dropped file instead of letting the page have
               it, and `copy` is what makes the cursor say so. */
            e.preventDefault()
            e.dataTransfer.dropEffect = 'copy'
          }}
          onDragLeave={() => store.dragLeave()}
          onDrop={(e) => {
            if (!open || s.busy) return
            e.preventDefault()
            store.dragEnd()
            void droppedFiles(e.dataTransfer).then((files) => {
              if (files.length) void store.uploadFolder(files)
            })
          }}
        >
          {open ? (
            <BasePanel base={open} s={s} />
          ) : (
            <div className="empty-note">
              <div className="ttl">{t('gui.kb.pick_base')}</div>
            </div>
          )}
        </div>
      </div>
      {creating && <CreateDialog model={s.status?.model ?? ''} onClose={() => setCreating(false)} />}
      {s.dialog?.kind === 'note' && <NoteDialog doc={s.dialog.doc} busy={s.busy} />}
      {s.dialog?.kind === 'url' && <UrlDialog busy={s.busy} />}
      {s.recall && open && <RecallDialog s={s} />}
      {s.settings && open && <SettingsDialog base={open} busy={s.busy} />}
    </>
  )
}
