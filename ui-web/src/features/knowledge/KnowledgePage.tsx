import { useEffect, useRef, useState } from 'react'
import { useSyncExternalStore } from 'react'

import { t } from '../../shell/bridge'
import { md as mdHtml } from '../../shell/prose'
import { open as openSettings, setTab as setSettingsTab } from '../settings/store'
import * as store from './store'

import type { KbBase, KbDoc } from './types'
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

function DocRow({ doc, busy }: { doc: KbDoc; busy: boolean }): JSX.Element {
  return (
    <>
      <div className="td nm">
        {/* The name is the way in, which is where a reader reaches for it --
            the menu's Preview Source is the same action under a label. */}
        <button className="kbopen" title={doc.source} onClick={() => store.openDoc(doc)}>
          {doc.source}
        </button>
      </div>
      <div className="td">{t('gui.kb.doc_type_file')}</div>
      {/* The reason rides on the status rather than under the row. A red line
          beneath every failure pushed the rows apart and made a list of files
          hard to scan; the status is where a reader is already looking when
          they want to know what went wrong. */}
      <div className={`td st s-${doc.status}`} title={doc.error || undefined}>
        {t('gui.kb.doc_' + doc.status)}
      </div>
      <div className="td">{ago(doc.updated_at)}</div>
      <div className="td">
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
        <Soon label={t('gui.kb.recall_test')} />
        <Soon label={t('gui.kb.settings')} />
      </div>
      <div className="kbsub">
        <span className="who">{t('gui.kb.updated_when', { when: ago(base.updated_at) })}</span>
        {/* A file is a data source, so this is the button the design asks for
            doing the one thing the engine already supports, rather than a
            placeholder beside a working upload that had nowhere else to go.
            The other source kinds join it here when they exist. */}
        <label className="mini kbsrc">
          + {t('gui.kb.add_source')}
          <input
            type="file"
            hidden
            disabled={s.busy}
            onChange={(e) => {
              const file = e.currentTarget.files && e.currentTarget.files[0]
              /* Cleared so choosing the same file twice fires again -- a retry
                 after a failed index is the same filename. */
              e.currentTarget.value = ''
              if (file) void store.upload(file)
            }}
          />
        </label>
      </div>
      {s.docs.length ? (
        <div className="kbtable">
          <div className="th">{t('gui.kb.col_name')}</div>
          <div className="th">{t('gui.kb.col_type')}</div>
          <div className="th">{t('gui.kb.col_status')}</div>
          <div className="th">{t('gui.kb.col_updated')}</div>
          <div className="th" />
          {s.docs.map((d) => (
            <DocRow key={d.id} doc={d} busy={s.busy} />
          ))}
        </div>
      ) : (
        <div className="empty-note">
          <div className="ttl">{t('gui.kb.no_docs')}</div>
        </div>
      )}
    </>
  )
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
        <div className="kbpane">
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
    </>
  )
}
