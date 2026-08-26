import { useState } from 'react'
import { useSyncExternalStore } from 'react'

import { t } from '../../shell/bridge'
import * as store from './store'

import type { KbBase, KbDoc, KbHit } from './types'
import type { JSX } from 'react'

function Creator({ busy }: { busy: boolean }): JSX.Element {
  const [name, setName] = useState('')
  const submit = (): void => {
    void store.create(name)
    setName('')
  }
  return (
    <div className="kbnew">
      <input
        className="kbname"
        value={name}
        placeholder={t('gui.kb.new_hint')}
        onChange={(e) => setName(e.currentTarget.value)}
        /* Enter submits, because a one-field form where it does not is a form
           people retype into. */
        onKeyDown={(e) => {
          if (e.key === 'Enter') submit()
        }}
      />
      <button className="mini" disabled={busy || !name.trim()} onClick={submit}>
        {t('gui.kb.new')}
      </button>
    </div>
  )
}

function Row({ base }: { base: KbBase }): JSX.Element {
  return (
    <div className="kbrow" onClick={() => void store.open_(base.id)}>
      <div className="nm">{base.name}</div>
      <div className="st">
        <span className="who">{t('gui.kb.docs', { n: base.documents })}</span>
        {/* The model the base was built with, not the one configured now. A
            base outlives a config change and the mismatch has to be visible. */}
        <span className="mdl">{base.embedding_model}</span>
      </div>
      {base.description ? <div className="ds">{base.description}</div> : null}
      <button
        className="mini ghost"
        title={t('gui.kb.delete')}
        onClick={(e) => {
          /* The row opens the base; deleting is not opening. */
          e.stopPropagation()
          store.remove(base)
        }}
      >
        {t('gui.kb.delete')}
      </button>
    </div>
  )
}

function DocRow({ doc }: { doc: KbDoc }): JSX.Element {
  return (
    <div className="kbdoc">
      <div className="nm">{doc.source}</div>
      <div className="st">
        <span className="who">{t('gui.kb.doc_' + doc.status)}</span>
        {doc.chunk_count ? <span className="mdl">{t('gui.kb.chunks', { n: doc.chunk_count })}</span> : null}
      </div>
      {/* The reason travels with the row: a failed document that does not say
          why sends the reader to a log they may not have. */}
      {doc.error ? <div className="ds">{doc.error}</div> : null}
    </div>
  )
}

function Hit({ hit }: { hit: KbHit }): JSX.Element {
  return (
    <div className="kbhit">
      {/* Two decimals: a similarity is for ranking by eye, and more digits
          invite reading precision that is not there. */}
      <div className="st">
        <span className="mdl">{hit.score.toFixed(2)}</span>
      </div>
      <div className="ds">{hit.text}</div>
    </div>
  )
}

function Panel({ base }: { base: KbBase }): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  return (
    <>
      <div className="kbhd">
        <button className="mini ghost back" onClick={() => store.back()}>
          {t('gui.kb.back')}
        </button>
        <b>{base.name}</b>
        <span className="mdl">{base.embedding_model}</span>
      </div>
      <div className="kbtools">
        <label className="mini">
          {t('gui.kb.upload')}
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
        <input
          className="kbask"
          value={s.query}
          placeholder={t('gui.kb.ask')}
          onChange={(e) => void store.search(e.currentTarget.value)}
        />
      </div>
      {s.hits !== null ? (
        s.hits.length ? (
          <div className="kbhits">
            {s.hits.map((h, i) => (
              <Hit key={`${h.document_id}:${i}`} hit={h} />
            ))}
          </div>
        ) : (
          <div className="empty-note">
            <div className="ttl">{t('gui.kb.no_hits')}</div>
          </div>
        )
      ) : s.docs.length ? (
        <div className="kbdocs">
          {s.docs.map((d) => (
            <DocRow key={d.id} doc={d} />
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
    return (
      <div className="empty-note">
        <div className="ttl">{t('gui.kb.unconfigured')}</div>
      </div>
    )
  }

  const open = s.openId ? s.bases.find((b) => b.id === s.openId) : undefined
  if (open) return <Panel base={open} />

  return (
    <>
      <Creator busy={s.busy} />
      {s.bases.length ? (
        <div className="kblist">
          {s.bases.map((base) => (
            <Row key={base.id} base={base} />
          ))}
        </div>
      ) : (
        <div className="empty-note">
          <div className="ttl">{t('gui.kb.none')}</div>
        </div>
      )}
    </>
  )
}
